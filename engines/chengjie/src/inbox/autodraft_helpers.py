"""auto-draft 富化回调抽取（Stage 2，从 main.py initialize() 原样迁出，行为不变）。

enrich_auto_draft(assistant, draft_svc, _ad_app, _ad_store, conv, text, draft_id, mode)：
异步用会话历史 + 人设产线富化自动草稿；_ad_app=web_app, _ad_store=inbox_store。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

# 图文连发补识（Phase1.3）：拟稿时向前回扫多少条消息找「还没识别过」的入站媒体行。
# 窗口大小读 `inbox.auto_draft.media_backscan`（缺省本默认值；≤1 = 关闭回扫，退化为
# 「只看最新一条入站」旧行为）。覆盖「先发图/语音、紧跟一句话」的常见节奏；
# 窗口过大会把很久前的旧媒体也拉来识别。
_MEDIA_BACKSCAN_DEFAULT = 5

# 拟稿等图的轮询步长（秒）。测试 monkeypatch 调小加速；预算见
# media_enrich.media_wait_sec_from_cfg（inbox.auto_draft.media_wait_sec）。
_MEDIA_WAIT_TICK_SEC = 1.5


def _media_desc_prefix(kind: str) -> str:
    """识别描述并入正文/回写消息行时的标记前缀（与 media_enrich 产出同口径）。

    #143（0902）：贴纸轮曾统一写 ``[图片内容]``——贴纸性在正文/历史里丢失，
    下游把表情包当真实照片评论（「贴纸里的猫」实锤）。``[贴纸内容]`` 在
    ``inbound_enrich._MEDIA_PREFIX_PATTERNS`` 里回解析为 sticker。
    """
    return "[贴纸内容]" if str(kind or "").strip().lower() == "sticker" \
        else "[图片内容]"


def _fold_image_desc(last: str, history: list, desc: str,
                     kind: str = "image") -> str:
    """把图片/贴纸识别描述并入待回复正文与 history 占位行（Phase1.3 口径），返回新 last。

    识别描述只走 media_desc 辅助块时模型反应明显弱于「描述在正文」——
    describe 直识与等待环重读（并行链路回写）两条来源共用本函数，口径一致。"""
    _mk = _media_desc_prefix(kind)
    _cap = str(last or "").strip()
    if _cap.startswith(("[图片", "[贴纸")) or _cap in ("[媒体]", ""):
        _cap = ""
    _ifull = (
        f"{_cap}\n{_mk} {desc}" if _cap else f"{_mk} {desc}"
    )
    if str(last or "").strip() in ("[图片]", "[贴纸]", "[媒体]", "") or _cap:
        last = _ifull
    for _hm in reversed(history or []):
        if isinstance(_hm, dict) and _hm.get("role") == "user":
            _hc = str(_hm.get("content") or "").strip()
            if _hc in (
                "[图片]", "[贴纸]", "[媒体]", "",
            ) or (
                _hc and not _hc.startswith(("[图片内容]", "[贴纸内容]"))
            ):
                _hm["content"] = _ifull
            break
    return last


async def enrich_auto_draft(assistant, draft_svc, _ad_app, _ad_store, conv: dict, text: str, draft_id: str, mode: str) -> None:
    """异步：拉历史 → 人设产线生成正文 → enrich_draft 收尾。

    失败任意环节都兜底 release（保留规则模板占位，降级旧行为），
    保证停泊草稿不会卡死在 enriching。"""
    try:
        from src.inbox.inbound_enrich import _match_media_prefix
        from src.inbox.persona_reply import (
            generate_persona_reply, normalize_history, trim_stale_history,
        )
        cid = str(conv.get("conversation_id") or "")
        platform = str(conv.get("platform") or "")
        chat_key = str(conv.get("chat_key") or "")
        account_id = str(conv.get("account_id") or "default")
        msgs = []
        _peer_media_type = ""
        _peer_media_ref = ""
        _peer_media_desc = ""
        _peer_msg_id = ""  # 语音转录回写目标行
        try:
            from src.ai.context_depth import history_fetch_limit as _hfl
            for r in _ad_store.list_recent_messages(
                    cid, limit=_hfl(assistant.config.config or {}, 30)):
                msgs.append({
                    "direction": r.get("direction") or "in",
                    "text": r.get("text") or "",
                    "media_type": r.get("media_type") or "",
                    "media_ref": r.get("media_ref") or "",
                    "message_id": r.get("message_id") or "",
                    "ts": r.get("ts") or 0,
                    # 实施72 P2：合成时间戳标记透传 → trim_stale_history 给这类
                    # 行打「[补收的历史消息，时间不详]」，防 LLM 把补收当刚说的
                    "approx_ts": r.get("approx_ts") or 0,
                })
            # 时间断层修剪：隔了几天的旧对话只留少量并打「[N天前]」标记，
            # 根治旧话题被当成刚说的（如 10 天前的英语梗 → "你突然换英语啦"幻觉）。
            msgs = trim_stale_history(msgs)
            for r in reversed(msgs):
                if str(r.get("direction") or "") == "in":
                    _peer_media_type = str(
                        r.get("media_type") or ""
                    )
                    _peer_media_ref = str(
                        r.get("media_ref") or ""
                    )
                    _peer_msg_id = str(
                        r.get("message_id") or ""
                    )
                    _lt = str(r.get("text") or "")
                    _pk, _pdesc = _match_media_prefix(_lt)
                    if _pdesc:
                        _peer_media_desc = _pdesc
                    elif _lt.startswith("[图片内容]"):
                        _peer_media_desc = _lt.replace(
                            "[图片内容]", "", 1
                        ).strip()
                    break
        except Exception:
            msgs = []
        history, last = normalize_history(msgs)
        if not last:
            last = str(text or "")
        _peer_audio_emotion = None  # 语音声学情绪（SER）
        if (
            _peer_media_type in ("image", "photo", "sticker")
            and not _peer_media_desc
        ):
            # 拟稿等图（2026-08-23「把人看成猫」P0）：照片行落库与 media_ref/媒体
            # 文件就绪之间有秒级窗口（边车先落行、后补媒体），旧代码在此窗内直接
            # 盲拟——ref 为空时连降级分支都进不去（无诚实指令），LLM 自由发挥断言
            # 画面。现在在 media_wait_sec 预算内等：ref 出现 / 文件可解析 / 并行
            # 链路把「[图片内容] …」写进消息行，等到才识别生成；等不到再走
            # degrade/hold——degrade 只吃「真识别失败」，不再吃「还没来得及识」。
            # 同图去重锁（try_mark_desc_inflight）防「图+紧跟一句话」两轮拟稿
            # 对同一张图双调 VLM：后来者只轮询等首轮的回写。
            try:
                from src.inbox.media_enrich import (
                    media_wait_sec_from_cfg,
                    try_mark_desc_inflight,
                )
                from src.integrations.protocol_bridge import (
                    static_media_ref_to_path,
                )
                _wait_deadline = time.monotonic() + media_wait_sec_from_cfg(
                    assistant.config.config or {})
                _iw_key = f"{cid}|{_peer_media_ref or _peer_msg_id}"
                _i_own = try_mark_desc_inflight(_iw_key)
                _img_path = ""
                while True:
                    if _peer_media_desc:
                        break
                    if _peer_media_ref and _i_own:
                        _img_path = str(static_media_ref_to_path(
                            _peer_media_ref) or "")
                        if _img_path or _peer_media_ref.lower().startswith(
                                ("http://", "https://")):
                            # 远程 CDN 引用不等文件（旧语义：本层不下载）
                            break
                    if time.monotonic() >= _wait_deadline:
                        break
                    await asyncio.sleep(_MEDIA_WAIT_TICK_SEC)
                    # 重读该行：迟到的 media_ref / 并行链路（A 线收图即识、
                    # 协议直发线、另一拟稿轮）回写的描述都在这里被接住。
                    try:
                        for _r2 in _ad_store.list_recent_messages(
                                cid, limit=10):
                            if _peer_msg_id and str(
                                    _r2.get("message_id") or ""
                            ) != _peer_msg_id:
                                continue
                            if not _peer_msg_id and str(
                                    _r2.get("direction") or "") != "in":
                                continue
                            _t2 = str(_r2.get("text") or "")
                            _pk2, _pd2 = _match_media_prefix(_t2)
                            if _pd2:
                                _peer_media_desc = _pd2
                            elif _t2.startswith("[图片内容]"):
                                _peer_media_desc = _t2.replace(
                                    "[图片内容]", "", 1).strip()
                            if not _peer_media_ref:
                                _peer_media_ref = str(
                                    _r2.get("media_ref") or "")
                            break
                    except Exception:
                        assistant.logger.debug(
                            "[AutoDraft] 等图重读失败（忽略）", exc_info=True)
                _tc = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client",
                    None,
                )
                if _img_path and not _peer_media_desc:
                    if _tc is not None and hasattr(_tc, "_get_image_content"):
                        _desc = await _tc._get_image_content(
                            _img_path
                        )
                    else:
                        # 桌面/协议部署可能没有 A 线 client 对象——识别不该被
                        # 它绑架，走平台无关共享识别层（同一 VisionClient）。
                        from src.inbox.media_enrich import (
                            enrich_inbound_media_text as _eimt,
                        )
                        _ignored_text, _desc = await _eimt(
                            media_type=_peer_media_type,
                            media_ref=_peer_media_ref,
                            caption="",
                            config=assistant.config.config or {},
                        )
                    if _desc:
                        _peer_media_desc = str(_desc).strip()
                        # 识别结果回写收件箱消息行（与语音/视频分支对等，补齐此前缺口）：
                        # 让坐席台/时间线/媒体卡看到「[图片内容] …」而非裸 [图片] 占位。
                        # 写成带前缀格式，供前端媒体卡 _splitMediaDesc 剥离出「AI 识图」摘要。
                        # only_if_empty=True 幂等：仅覆盖占位行，不踩已有真实内容/协议线已回写的。
                        try:
                            _ad_store.update_message_text(
                                cid,
                                message_id=_peer_msg_id,
                                media_ref=_peer_media_ref,
                                text=(f"{_media_desc_prefix(_peer_media_type)}"
                                      f" {_peer_media_desc}"),
                                only_if_empty=True,
                            )
                        except Exception:
                            assistant.logger.debug(
                                "[AutoDraft] 图片识别回写消息失败（忽略）",
                                exc_info=True,
                            )
                        # 2026-07-26 Phase1.3：识别结果并入待回复正文 + 历史，
                        # 与 Telegram A 线（入站即成 "[图片内容] …"）及本文件
                        # 语音/视频分支同口径。此前图片描述只走 media_desc 辅助
                        # 块、正文停留 [图片] 占位——模型对占位正文的回应明显弱
                        # 于描述在正文（WA/协议线识图「答非所问」的直接根源）。
                        last = _fold_image_desc(
                            last, history, _peer_media_desc,
                            kind=_peer_media_type)
                        assistant.logger.info(
                            "[AutoDraft] 图片识别补全: %s",
                            _peer_media_desc[:80],
                        )
                elif _peer_media_desc:
                    # 等待环从消息行重读到的描述（并行链路已回写）：同样并入
                    # 正文与 history（与直识路径同口径）；不再回写、不再识别。
                    last = _fold_image_desc(last, history, _peer_media_desc,
                                            kind=_peer_media_type)
                    assistant.logger.info(
                        "[AutoDraft] 等图拿到并行回写描述: %s",
                        _peer_media_desc[:80],
                    )
            except Exception:
                assistant.logger.debug(
                    "[AutoDraft] 图片识别补全失败",
                    exc_info=True,
                )
        # 识别失败处置（实施56 P1，2026-08-22）：media_degrade_reply 开 → 放行
        # 拟稿，产线经 inbound_enrich 给无 desc 媒体挂媒体块（ai_client 自带
        # 「自然承认收到+温和追问」话术，诚实降级不装懂）；关（默认）→ 维持
        # 08-17 无兜底纪律：拦下 + 上报 + 取消本条拟稿。
        _degrade_ok = False
        # 图片分支不再要求 media_ref 非空（2026-08-23）：边车「先落行后补 ref」的
        # 窗口内，ref 为空的图片行此前两个分支都进不去 → 裸生成无诚实指令 →
        # 盲猜画面（「这图是你家猫吗」实锤）。看得见 media_type=图 就必须
        # 走降级或扣留，绝不裸拟。语音分支维持原条件（转写链无此事故形态）。
        if not _peer_media_desc and (
                _peer_media_type in ("image", "photo", "sticker")
                or (_peer_media_ref and _peer_media_type in ("voice", "audio"))):
            try:
                from src.inbox.media_enrich import media_degrade_reply_enabled
                _degrade_ok = media_degrade_reply_enabled(
                    assistant.config.config or {})
            except Exception:
                _degrade_ok = False
        if (
            _peer_media_type in ("image", "photo", "sticker")
            and not _peer_media_desc
            and _degrade_ok
        ):
            assistant.logger.info(
                "[AutoDraft] 图片未识别 → 降级诚实拟稿"
                "（media_degrade_reply）cid=%s", cid)
        elif (
            _peer_media_type in ("image", "photo", "sticker")
            and not _peer_media_desc
        ):
            # 无兜底：没看懂图就不拟稿、不放行模板自动发。
            try:
                from src.ops.delivery_block import report_block
                report_block(
                    "vision", reason="enrich_failed",
                    platform=platform, conversation_id=cid)
            except Exception:
                assistant.logger.debug(
                    "[AutoDraft] vision hold 上报失败", exc_info=True)
            assistant.logger.warning(
                "[AutoDraft] 图片未看懂 → 取消本条自动拟稿 cid=%s", cid)
            try:
                _ad_store.update_draft_status(
                    draft_id, status="cancelled",
                    decided_by="vision_hold",
                    expected_statuses=("pending", "enriching"),
                )
            except Exception:
                try:
                    draft_svc.release_enriching_draft(draft_id)
                except Exception:
                    pass
            return
        elif (
            _peer_media_type in ("voice", "audio")
            and _peer_media_ref
            and not _peer_media_desc
        ):
            # 语音转录补全（与图片 Vision 描述对等）：入站语音
            # 转成文本喂人设产线。缺这步 AI 只见「[语音]」占位，
            # 会搪塞「我听不了语音」而非接住内容。
            try:
                from src.integrations.protocol_bridge import (
                    static_media_ref_to_path,
                )
                _voice_path = static_media_ref_to_path(
                    _peer_media_ref
                )
                _tc = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client",
                    None,
                )
                _vtr = getattr(
                    _tc, "voice_transcriber", None,
                ) if _tc is not None else None
                if _vtr is None:
                    # 宿主启动后才热开 ASR → 懒建兜底（同 media_enrich 口径）
                    from src.inbox.media_enrich import lazy_voice_transcriber
                    _vtr = lazy_voice_transcriber(
                        assistant.config.config or {})
                if _voice_path and _vtr is not None:
                    _vlang = str(
                        (assistant.config.get(
                            "voice_recognition", {},
                        ) or {}).get("language", "auto")
                    ) or "auto"
                    _vtxt = await _vtr.transcribe_voice_message(
                        str(_voice_path), _vlang,
                    )
                    if _vtxt and str(_vtxt).strip():
                        _peer_media_desc = str(_vtxt).strip()
                        # 转录文本即「对方说的话」→ 直接作为待回复
                        # 文本（替换 [语音] 占位），让意图/语言/
                        # 回复都基于真实内容（含回对语言）。
                        if last.strip() in (
                            "[语音]", "[媒体]", "",
                        ):
                            last = _peer_media_desc
                        # 修复 history 与转录不一致：把历史末条用户
                        #「[语音]」占位补成转录文本，避免语言切换误判。
                        for _hm in reversed(history or []):
                            if isinstance(_hm, dict) and _hm.get(
                                "role") == "user":
                                if str(_hm.get("content") or "").strip() in (
                                    "[语音]", "[媒体]", ""):
                                    _hm["content"] = _peer_media_desc
                                break
                        assistant.logger.info(
                            "[AutoDraft] 语音转录补全: %s",
                            _peer_media_desc[:80],
                        )
                        # 音频情绪识别（SER）：从声学语气听情绪，
                        # 与原生 TG 路径对齐（best-effort，软降级）。
                        try:
                            _se_cfg = (assistant.config.get(
                                'speech_emotion', {}) or {})
                            if _se_cfg.get('enabled'):
                                from src.ai.speech_emotion import (
                                    get_speech_emotion_recognizer)
                                from src.ai.speech_emotion_stats import (
                                    get_speech_emotion_stats)
                                _ser = get_speech_emotion_recognizer(
                                    _se_cfg)
                                _sres = await _ser.recognize_async(
                                    str(_voice_path))
                                _mc = float(_se_cfg.get(
                                    'min_confidence', 0.5) or 0.5)
                                _peer_audio_emotion = (
                                    _sres.as_emotion_dict(
                                        min_confidence=_mc))
                                get_speech_emotion_stats().record(
                                    ok=_sres.ok,
                                    emotion=_sres.emotion,
                                    confident=bool(
                                        _peer_audio_emotion and
                                        _peer_audio_emotion.get(
                                            'confident')),
                                    remote=str(
                                        _sres.model or ''
                                    ).startswith('remote:'))
                                if _peer_audio_emotion and \
                                        _peer_audio_emotion.get(
                                            'confident'):
                                    assistant.logger.info(
                                        "[AutoDraft] 声学情绪: %s "
                                        "score=%.2f",
                                        _peer_audio_emotion.get(
                                            'raw_label'),
                                        _peer_audio_emotion.get(
                                            'score') or 0.0)
                        except Exception:
                            assistant.logger.debug(
                                "[AutoDraft] 音频情绪识别失败",
                                exc_info=True)
                        # 转录回写入站消息行：坐席台/时间线即时看到
                        # 「对方说了什么」而非空白/[语音]占位（转录已在
                        # 此异步路径完成，回写零额外成本、不阻塞主循环、
                        # 不重复转录）。only_if_empty 防踩已有内容。
                        try:
                            _ad_store.update_message_text(
                                cid,
                                message_id=_peer_msg_id,
                                media_ref=_peer_media_ref,
                                text=_peer_media_desc,
                                only_if_empty=True,
                            )
                        except Exception:
                            assistant.logger.debug(
                                "[AutoDraft] 转录回写消息失败",
                                exc_info=True,
                            )
                    else:
                        assistant.logger.warning(
                            "[AutoDraft] 语音转录空结果 ref=%s",
                            _peer_media_ref,
                        )
            except Exception:
                assistant.logger.warning(
                    "[AutoDraft] 语音转录补全失败 ref=%s",
                    _peer_media_ref,
                    exc_info=True,
                )
        if (
            _peer_media_type in ("voice", "audio")
            and _peer_media_ref
            and not _peer_media_desc
            and _degrade_ok
        ):
            # 降级诚实拟稿：未转写语音不置 desc → inbound_enrich 判非转写语音
            # → 挂媒体块（ai_client「内容暂无法识别，自然承认+追问」话术）。
            assistant.logger.info(
                "[AutoDraft] 语音未转写 → 降级诚实拟稿"
                "（media_degrade_reply）cid=%s", cid)
        elif (
            _peer_media_type in ("voice", "audio")
            and _peer_media_ref
            and not _peer_media_desc
        ):
            try:
                from src.ops.delivery_block import report_block
                report_block(
                    "asr", reason="enrich_failed",
                    platform=platform, conversation_id=cid)
            except Exception:
                assistant.logger.debug(
                    "[AutoDraft] asr hold 上报失败", exc_info=True)
            assistant.logger.warning(
                "[AutoDraft] 语音未转写 → 取消本条自动拟稿 cid=%s", cid)
            try:
                _ad_store.update_draft_status(
                    draft_id, status="cancelled",
                    decided_by="asr_hold",
                    expected_statuses=("pending", "enriching"),
                )
            except Exception:
                try:
                    draft_svc.release_enriching_draft(draft_id)
                except Exception:
                    pass
            return
        elif (
            _peer_media_type in ("video", "video_note", "animation", "gif")
            and _peer_media_ref
            and not _peer_media_desc
        ):
            # 视频理解补全（B 线 worker 入站时已 enrich 则跳过；此处兜底旧消息/失败路径）
            try:
                from src.integrations.protocol_bridge import (
                    static_media_ref_to_path,
                )
                from src.ai.inbound_video import (
                    compose_video_inbound_text,
                    understand_video_file,
                )
                _video_path = static_media_ref_to_path(_peer_media_ref)
                _tc = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client", None,
                )
                _cfg = assistant.config.config or {}
                _vtr = getattr(_tc, "voice_transcriber", None) if _tc else None
                if _vtr is None:
                    # 宿主启动后才热开 ASR → 懒建兜底（同 media_enrich 口径）
                    from src.inbox.media_enrich import lazy_voice_transcriber
                    _vtr = lazy_voice_transcriber(_cfg)
                if _video_path:
                    _vdesc = await understand_video_file(
                        str(_video_path),
                        vision_config=_cfg.get("vision") or {},
                        voice_transcriber=_vtr,
                        speech_emotion_config=_cfg.get("speech_emotion") or {},
                        voice_recognition_config=_cfg.get("voice_recognition") or {},
                    )
                    if _vdesc and str(_vdesc).strip():
                        _peer_media_desc = str(_vdesc).strip()
                        _cap = last.strip()
                        if _cap.startswith("[视频") or _cap in ("[媒体]", ""):
                            _cap = ""
                        _vfull = compose_video_inbound_text(
                            caption=_cap, video_desc=_peer_media_desc,
                        )
                        if last.strip() in ("[视频]", "[媒体]", "") or _cap:
                            last = _vfull
                        for _hm in reversed(history or []):
                            if isinstance(_hm, dict) and _hm.get(
                                    "role") == "user":
                                _hc = str(_hm.get("content") or "").strip()
                                if _hc in ("[视频]", "[媒体]", "") or (
                                        _hc and not _hc.startswith("[视频内容]")
                                ):
                                    _hm["content"] = _vfull
                                break
                        assistant.logger.info(
                            "[AutoDraft] 视频理解补全: %s",
                            _peer_media_desc[:80],
                        )
                        try:
                            _ad_store.update_message_text(
                                cid,
                                message_id=_peer_msg_id,
                                media_ref=_peer_media_ref,
                                text=_vfull,
                                only_if_empty=True,
                            )
                        except Exception:
                            assistant.logger.debug(
                                "[AutoDraft] 视频描述回写失败",
                                exc_info=True,
                            )
                    else:
                        assistant.logger.warning(
                            "[AutoDraft] 视频理解空结果 ref=%s",
                            _peer_media_ref,
                        )
            except Exception:
                assistant.logger.warning(
                    "[AutoDraft] 视频理解补全失败 ref=%s",
                    _peer_media_ref,
                    exc_info=True,
                )
        # —— 图文连发补识（2026-07-26 Phase1.3）——
        # 上面三个分支只富化「最新一条入站」：客户先发图（/语音/视频）紧跟一句话
        # 时，触发拟稿的是那句话，媒体行永远停在占位（识别从未发生）→ 模型对刚收
        # 到的媒体毫无感知。这里向前回扫最近 N 条消息（N 读 inbox.auto_draft.
        # media_backscan，默认 _MEDIA_BACKSCAN_DEFAULT；≤1 关闭回扫=旧行为），找最近
        # 一条「还没识别过」的入站媒体行，用共享识别层补识：结果回写消息行（坐席
        # 可见）+ 精确替换 history 对应占位（模型可见）。不动 last（当前待回复消息
        # 语义不变）；每次拟稿至多补一条控制成本；全程软失败。
        _backfill_desc = ""  # 回扫补识出的描述（供下方 media_desc 接线）
        _backfill_kind = ""
        try:
            try:
                _backscan_n = int(
                    ((assistant.config.config or {}).get("inbox", {})
                     .get("auto_draft", {}) or {})
                    .get("media_backscan", _MEDIA_BACKSCAN_DEFAULT)
                )
            except Exception:
                _backscan_n = _MEDIA_BACKSCAN_DEFAULT
            from src.inbox.media_enrich import (
                enrich_inbound_media_text,
                is_placeholder_only,
            )
            _bf_kinds = {
                "image", "photo", "sticker", "voice", "audio",
                "video", "video_note", "animation", "gif",
            }
            _bf_seen = 0
            for _r in (reversed(msgs) if _backscan_n > 1 else ()):
                _bf_seen += 1
                if _bf_seen > _backscan_n:
                    break
                if str(_r.get("direction") or "") != "in":
                    continue
                _bmid = str(_r.get("message_id") or "")
                if _bmid and _bmid == _peer_msg_id:
                    continue  # 最新入站行已由上方主分支处理
                _bmt = str(_r.get("media_type") or "").lower()
                _bref = str(_r.get("media_ref") or "")
                if _bmt not in _bf_kinds or not _bref:
                    continue
                _brt = str(_r.get("text") or "")
                if _brt.strip() and not is_placeholder_only(_brt):
                    continue  # 已有 caption/识别结果 → 不重复识别
                _tc_bf = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client", None,
                )
                _vtr_bf = getattr(
                    _tc_bf, "voice_transcriber", None,
                ) if _tc_bf is not None else None
                _bf_text, _bf_desc = await enrich_inbound_media_text(
                    media_type=_bmt, media_ref=_bref, caption="",
                    config=assistant.config.config or {},
                    voice_transcriber=_vtr_bf,
                )
                if _bf_desc:
                    _backfill_desc = str(_bf_desc).strip()
                    _backfill_kind = _bmt
                    try:
                        _ad_store.update_message_text(
                            cid,
                            message_id=_bmid,
                            media_ref=_bref,
                            text=_bf_text,
                            only_if_empty=True,
                        )
                    except Exception:
                        assistant.logger.debug(
                            "[AutoDraft] 媒体补识回写失败（忽略）",
                            exc_info=True,
                        )
                    # 精确定位该行在 history 里的下标：按 normalize_history
                    # 同一规则重放（非空文本/媒体占位各占一条，顺序不变），
                    # 多张同类占位（如连发两图）也不会替换错行。
                    try:
                        _hidx = -1
                        _cursor = 0
                        for _mm in msgs:
                            _mt2 = str(_mm.get("text") or "").strip()
                            if not _mt2 and (
                                _mm.get("media_type")
                                or _mm.get("media_ref")
                            ):
                                try:
                                    from src.integrations.protocol_bridge \
                                        import media_placeholder
                                    _mt2 = media_placeholder(
                                        str(_mm.get("media_type") or ""))
                                except Exception:
                                    _mt2 = "[媒体]"
                            if not _mt2:
                                continue
                            if _mm is _r:
                                _hidx = _cursor
                                break
                            _cursor += 1
                        if (
                            0 <= _hidx < len(history or [])
                            and isinstance(history[_hidx], dict)
                            and history[_hidx].get("role") == "user"
                            and is_placeholder_only(
                                str(history[_hidx].get("content") or ""))
                        ):
                            history[_hidx]["content"] = _bf_text
                    except Exception:
                        assistant.logger.debug(
                            "[AutoDraft] 媒体补识历史替换失败（忽略）",
                            exc_info=True,
                        )
                    assistant.logger.info(
                        "[AutoDraft] 补识此前媒体 kind=%s: %s",
                        _bmt, str(_bf_desc)[:80],
                    )
                break  # 只补最近一条未识别媒体行（成本上限=每稿一次识别）
        except Exception:
            assistant.logger.debug(
                "[AutoDraft] 媒体补识扫描失败（忽略）", exc_info=True)
        # 图在前文在后：补识描述除已替换 history 占位（全路径通用通道）外，再照带
        # media_desc 送产线（统一引擎经 apply_inbound_enrichments → ai_client 媒体
        # 块），并附一条简短注记声明「媒体是之前那条、当前待回复是最新文字」——
        # 防模型把最新文字误当媒体消息回应。仅当最新入站行本身不是媒体时接线
        # （媒体行自有上方主分支语义，不混两条媒体的描述）；media_type 保持空，
        # 忠实于「当前待回复的是文字」。
        if _backfill_desc and not _peer_media_type and not _peer_media_desc:
            _bf_label = {
                "voice": "语音", "audio": "语音",
                "video": "视频", "video_note": "视频",
                "animation": "动图", "gif": "动图",
                "sticker": "贴纸",
            }.get(_backfill_kind, "图片")
            _peer_media_desc = (
                f"{_backfill_desc}\n（注：这条{_bf_label}是对方在最新一句"
                "文字之前发来的，当前待回复的是对方最新那句文字）"
            )
        # 生效人设（单一事实源，与 autosend voice 同口径）：
        # 会话覆写(开关开) → meta.persona_id → meta.persona_ids[0] →
        # config 默认。根治复数/单数不匹配导致的空 persona；
        # 2026-07-26 起同时消费会话级覆写（cp-persona 换绑立即生效）。
        _persona_id = ""
        try:
            from src.ai.persona_voice import (
                resolve_effective_persona_id as _repi,
            )
            _persona_id = _repi(
                assistant.config.config or {},
                platform, account_id, str(chat_key or ""),
            )
        except Exception:
            _persona_id = ""
        # 风险分档（单一事实源）：草稿创建时已算好 risk_level，
        # 取出透传给统一引擎 → 低风险走快路省延迟、中/高风险吃满全栈。
        _risk_level = ""
        try:
            _drow = draft_svc.get_draft(draft_id) or {}
            _risk_level = str(_drow.get("risk_level") or "")
        except Exception:
            _risk_level = ""
        # Q-21 B（Y82GWM / #302，2026-09-12）：起草前先算一份会话语言计划
        # （outbound_translate.build_conv_lang_plan：D-M3 六级原序 + 客户明确请求 + 账号先验；
        # 一行 [lang-plan] 日志），reply_lang **只读它**——此前生成侧 resolve_reply_language
        # 与出站侧 resolve_outbound_lang 各判各的，英文「Hi」全自动起草出中文且日志无从归因。
        # 计划判不出（unknown）→ reply_lang="" 交回产线旧 default（不阻断拟稿）。
        _plan_lang = ""
        try:
            from src.inbox.outbound_translate import build_conv_lang_plan
            _plan_lang = str(build_conv_lang_plan(
                cid, store=_ad_store, cfg_root=assistant.config.config or {},
                platform=platform, account_id=account_id, chat_key=chat_key,
                history=history).reply_lang or "")
        except Exception:
            assistant.logger.debug("[AutoDraft] lang-plan 失败（产线按旧 default）", exc_info=True)
        out = await generate_persona_reply(
            app=_ad_app, platform=platform, chat_key=chat_key,
            last_inbound=last, history=history,
            persona_id=_persona_id,
            reply_lang=_plan_lang,
            risk_level=_risk_level,
            media_type=_peer_media_type,
            media_ref=_peer_media_ref,
            media_desc=_peer_media_desc,
            conversation_id=cid,
            peer_audio_emotion=_peer_audio_emotion,
            account_id=account_id,
            # P3：入站 mid（TG message.id / 协议 wamid…）贯通案例锚点
            inbound_msg_id=str(_peer_msg_id or "").strip(),
        )
        # B51 反复读闸（实施64 P1-2，`_285` 实录「把对方原话当成要发的回复」）：
        # 出稿与对方上一条近逐字 → 重生成一次（温度抖动通常即破复读）；复发 →
        # 判生成失败走兜底放行（规则模板占位进人审），绝不把鹦鹉稿发出去。
        try:
            from src.ai.reply_echo_guard import reply_echoes_inbound
            if (out.get("ok") and out.get("reply")
                    and reply_echoes_inbound(str(out.get("reply") or ""), last)):
                assistant.logger.warning(
                    "[AutoDraft] 反复读闸命中 cid=%s：出稿≈对方原话 %r → 重生成一次",
                    cid, str(out.get("reply") or "")[:60])
                out2 = await generate_persona_reply(
                    app=_ad_app, platform=platform, chat_key=chat_key,
                    last_inbound=last, history=history,
                    persona_id=_persona_id,
                    reply_lang=_plan_lang,
                    risk_level=_risk_level,
                    media_type=_peer_media_type,
                    media_ref=_peer_media_ref,
                    media_desc=_peer_media_desc,
                    conversation_id=cid,
                    peer_audio_emotion=_peer_audio_emotion,
                    account_id=account_id,
                    inbound_msg_id=str(_peer_msg_id or "").strip(),
                )
                if (out2.get("ok") and out2.get("reply")
                        and not reply_echoes_inbound(
                            str(out2.get("reply") or ""), last)):
                    out = out2
                else:
                    assistant.logger.warning(
                        "[AutoDraft] 反复读闸复发 cid=%s → 判生成失败兜底放行", cid)
                    out = {"ok": False}
        except Exception:
            assistant.logger.debug(
                "[AutoDraft] 反复读闸异常（放行原稿）", exc_info=True)
        if out.get("ok") and out.get("reply"):
            # 盲断言闸门（2026-08-23 P0-2）：无识图描述的图片轮，出稿不得断言/
            # 猜测/夸赞画面内容——降级指令是软约束，LLM 违约（「这图是你家猫吗」
            # 实锤）就把整稿换成诚实追问。有描述时不干预（描述真提到猫时说猫没错）。
            if (_peer_media_type in ("image", "photo", "sticker")
                    and not _peer_media_desc):
                try:
                    from src.inbox.media_enrich import (
                        blind_image_assertion, honest_image_ask,
                    )
                    if blind_image_assertion(str(out.get("reply") or "")):
                        assistant.logger.info(
                            "[AutoDraft] 盲断言拦截 cid=%s：%r → 诚实追问",
                            cid, str(out.get("reply") or "")[:60])
                        out["reply"] = honest_image_ask(
                            str(out.get("reply_lang") or "zh"), seed=cid)
                except Exception:
                    assistant.logger.debug(
                        "[AutoDraft] 盲断言闸门异常（放行原稿）", exc_info=True)
            done = draft_svc.enrich_draft(
                draft_id, reply_text=out["reply"],
                reply_lang=str(out.get("reply_lang") or "zh"),
                automation_mode=mode,
            )
            # Q-14 B：本会话 AI 成功出稿 → 清「AI 本轮未生成」灰标
            try:
                from src.inbox.ai_fail_marker import clear as _aif_clear
                _aif_clear(_ad_store, cid)
            except Exception:
                pass
            if done:
                # L3 缓冲话术：草稿被定级为需人审（L3+）而不会自动发时，先「接住」客户
                # ——自动已读 + 一句安全缓冲话术，避免人审挂起期客户被沉默晾着（真实事故）。
                await _maybe_holding_after_enrich(
                    assistant, draft_svc, draft_id, conv, text, mode,
                    reply_lang=str(out.get("reply_lang") or ""))
                return
        # 生成失败/为空 → 兜底放行（规则模板占位）
        draft_svc.release_enriching_draft(draft_id)
        # Q-14 B：失败对坐席可见——接住点落一行 [ai] fail + 会话灰标（不引入任何罐头句）
        _mark_ai_fail(_ad_app, _ad_store, cid, draft_id)
    except Exception:
        assistant.logger.debug(
            "[AutoDraft] 人设补全失败，兜底放行 draft_id=%s",
            draft_id, exc_info=True)
        try:
            draft_svc.release_enriching_draft(draft_id)
        except Exception:
            pass
        try:
            _mark_ai_fail(_ad_app, _ad_store, cid, draft_id)
        except Exception:
            pass


def _mark_ai_fail(app: Any, store: Any, cid: str, draft_id: str) -> None:
    """Q-14 B：把 ``AIClient`` 记下的失败原因（timeout / connect / gateway_5xx / no_key /
    empty）搬到会话灰标 ``last_ai_fail``；ai_client 无记录（失败在 AI 之外）→ unknown。绝不抛。"""
    try:
        from src.inbox.ai_fail_marker import consume_and_mark
        _st = getattr(getattr(app, "state", None), "inbox_store", None) or store
        _ai = getattr(getattr(app, "state", None), "ai_client", None)
        if _ai is None:
            _sm = getattr(getattr(app, "state", None), "skill_manager", None)
            _ai = getattr(_sm, "ai_client", None)
        consume_and_mark(_ai, _st, str(cid or ""), draft_id=str(draft_id or ""))
    except Exception:
        logging.getLogger(__name__).debug("[AutoDraft] ai_fail 灰标写入失败（忽略）", exc_info=True)


async def _maybe_holding_after_enrich(
    assistant, draft_svc, draft_id: str, conv: dict, peer_text: str, mode: str,
    *, reply_lang: str = "",
) -> None:
    """草稿富化定级后，若落到「需人审、不自动发」的等级（默认 L3+）且会话为全自动档，
    发 L3 缓冲话术（自动已读 + 安全短语）先接住客户。best-effort，绝不抛。

    只对 ``auto_ai`` 会话生效：review/manual 会话有坐席主动处理，无需机器缓冲。"""
    try:
        if str(mode or "") != "auto_ai":
            return
        from src.inbox.holding_reply import (
            resolve_holding_cfg, maybe_send_holding_reply,
        )
        _hb = resolve_holding_cfg(assistant.config.config or {})
        if not _hb.get("enabled"):
            return
        _levels = {str(x).upper() for x in (_hb.get("levels") or ["L3"])}
        _drow = draft_svc.get_draft(draft_id) or {}
        _lvl = str(_drow.get("autopilot_level") or "").upper()
        if _lvl not in _levels:
            return
        # 缓冲话术语言：**优先人设产线解析出的 reply_lang**——它已综合转写/短消息防误切，
        # 是「该用什么语言跟客户说」的权威结论；缺失时回落证据口径的会话语言
        # （P3-198：peer_language_hint＝入站投票→持久列→出站参照——旧的裸
        # detect(peer_text) 会把中性消息统计成 en、把系统注入的中文加注当客户语言）。
        _lang = reply_lang or ""
        if not _lang:
            try:
                from src.inbox.outbound_translate import peer_language_hint
                _lang = peer_language_hint(
                    getattr(assistant, "inbox_store", None),
                    str(conv.get("conversation_id") or ""))
            except Exception:
                pass
        await maybe_send_holding_reply(
            assistant,
            str(conv.get("platform") or ""),
            str(conv.get("account_id") or "default"),
            str(conv.get("chat_key") or ""),
            str(conv.get("conversation_id") or ""),
            peer_text=str(peer_text or ""),
            lang=_lang,
        )
    except Exception:
        assistant.logger.debug(
            "[holding] L3 缓冲话术调度失败 draft_id=%s", draft_id, exc_info=True)


@dataclass(frozen=True)
class AutoDraftConfig:
    """auto_draft 纯配置(从 inbox.auto_draft 读出,不含运行时依赖)。"""
    mode: str
    min_len: int
    skip: set
    platform_ceilings: dict
    skip_groups: bool
    enrich: bool
    # 融合实例 P1：账号业务线 → 档位上限（inbox.auto_draft.business_line_modes；
    # 内置默认 translation→review：翻译线账号 AI 只拟稿人审后发，绝不自动发送。
    # 账号未打 business_line 标签 = 不封顶 = 旧行为）。
    business_line_ceilings: dict = field(
        default_factory=lambda: {"translation": "review"})
    # 群/频道 P1：skip_group_chats 是否也信「弱证据群」（messenger/instagram 的
    # 网页 DOM 启发式）。默认 False＝弱证据群照常拟稿（宁可让真群的草稿进队列，
    # 也不让被误判的私聊客户静默无草稿）；真群噪音压过误判风险时置 True 复旧。
    skip_groups_trust_weak: bool = False


DRAFT_STATUS_CONSUMED = "consumed"
_CONSUME_SCAN_LIMIT = 20


def consume_drafts_on_agent_send(
    store: Any, conversation_id: str, *, draft_id: str = "", by: str = "agent_send",
) -> list:
    """Q-10 A（#267，CV4E22 / RU89S6）：坐席人工发送成功 → 该会话在途草稿置 ``consumed``。

    语义：坐席已经替这条入站回过话了（不论是原样采用 Tab、改过再发，还是自己另写），
    面板上那条 AI 稿就没有存在的意义——**仅新入站才产生新稿**（``upsert_draft`` 单行复用，
    下一条入站会把本行重生为 pending）。「忽略」（前端 dismiss / reject）语义不变。

    为什么放服务端而不是靠前端 ``_cdraftResolveAdopted`` 的 fire-and-forget cancel：
    前端 POST resolve 不 await 就立刻 GET pending 刷草稿条，旧稿先一步被顶回；且
    桌面壳 / 其它端发送不走那段 JS。放在 send 成功路径里同步落库，前端随后的刷新
    必然看不到它。

    只碰 ``pending`` / ``enriching``（``update_draft_status`` 的原子闸门），已投递 / 已处置
    的终态一律不动；``draft_id`` 给了（前端采用的那条）优先，再扫会话其余在途稿。
    best-effort：store 缺方法 / 异常 → 返回已消费列表（可能为空），绝不影响发送。
    返回本次置 consumed 的 draft_id 列表。
    """
    if store is None:
        return []
    cid = str(conversation_id or "").strip()
    log = logging.getLogger("ai_chat_assistant.autodraft")
    candidates: list = []
    seen: set = set()
    did0 = str(draft_id or "").strip()
    if did0:
        candidates.append(did0)
        seen.add(did0)
    if cid:
        # 单行复用约定：inbox 源草稿 id 恒为 inbox:<conversation_id>
        canon = f"inbox:{cid}"
        if canon not in seen:
            candidates.append(canon)
            seen.add(canon)
        lister = getattr(store, "list_drafts", None)
        if callable(lister):
            for status in ("pending", "enriching"):
                try:
                    rows = lister(conversation_id=cid, status=status,
                                  limit=_CONSUME_SCAN_LIMIT) or []
                except Exception:
                    log.debug("[draft] 列举在途草稿失败（忽略）conv=%s", cid, exc_info=True)
                    continue
                for r in rows:
                    did = str((r or {}).get("draft_id") or "").strip()
                    if did and did not in seen:
                        candidates.append(did)
                        seen.add(did)
    updater = getattr(store, "update_draft_status", None)
    if not callable(updater):
        return []
    consumed: list = []
    for did in candidates:
        try:
            if updater(did, status=DRAFT_STATUS_CONSUMED, decided_by=by):
                consumed.append(did)
                log.info("[draft] consumed draft_id=%s by=%s conv=%s", did, by, cid or "-")
        except Exception:
            log.debug("[draft] 置 consumed 失败（忽略）draft_id=%s", did, exc_info=True)
    return consumed


def _a_line_on_duty(app_config, conv: dict, text: str) -> bool:
    """A 线此刻是否会直发本条消息（与 telegram_client 的班表闸同一判定）。

    True = 在班或危机穿透（A 线直发 → System Z 应让位防双发）；
    False = 休息中被扣（A 线不发 → System Z 必须接住拟稿）。
    异常按 True（保持旧让位行为，宁可少拟稿不可双发）。
    """
    try:
        from src.inbox.work_hours_gate import (
            should_hold_auto_reply,
            work_schedule_cfg,
        )
        return should_hold_auto_reply(
            work_schedule_cfg(app_config), "telegram",
            str(conv.get("account_id") or "default"),
            peer_text=str(text or "")) == ""
    except Exception:
        return True


_warmup_logged: set = set()


def _log_warmup_cap_once(platform: str, account_id: str, cap) -> None:
    """预热封顶首次生效时播报一次（每账号每进程一次）。

    刻意不是每条消息都打：预热窗内该账号的每条入站都会命中，逐条打就是刷屏，运维反而
    看不见。但**完全不打**更糟——「草稿怎么突然都要人审了」会变成一次无头悬案，故首次
    命中时把原因、账号年龄、关闸办法一次讲清。``cap`` = effective_automation.ModeCap
    （layer=warmup，detail 带 age_h、until_ts=预热窗结束时刻）。
    """
    k = f"{platform}:{account_id}"
    if k in _warmup_logged:
        return
    _warmup_logged.add(k)
    try:
        left_h = max(0.0, (float(getattr(cap, "until_ts", 0.0) or 0.0)
                           - time.time()) / 3600.0)
        logging.getLogger("ai_chat_assistant.autodraft").info(
            "[AutoDraft] 冷启动预热封顶生效：账号 %s（%s）→ 自动回复降级为 "
            "review（AI 拟稿、人审后发）。预热窗还剩 %.1fh 自动恢复；"
            "如需立刻恢复全自动："
            "companion.proactive_topic.cold_start.warmup_review: false",
            k, str(getattr(cap, "detail", "") or ""), left_h)
    except Exception:
        pass


def make_auto_draft_cb(
    cfg: AutoDraftConfig, draft_svc, store, loop, enrich_fn, logger,
    *, app_config=None,
):
    """构造入站新消息 -> 自动草稿生成回调(从 main.py initialize() 原样迁出)。

    cfg=纯配置;draft_svc/store/loop/enrich_fn/logger=运行时依赖。
    app_config=完整配置树（可选）——供首条入站 bootstrap 持久化 auto_ai 档位。
    返回的回调签名 (conv, text)->None 与 register_new_inbound_cb 契约一致；
    ``skip_companion_yield``（仅复班补觉重拟用，keyword-only 不影响注册契约）
    = 旁路 companion 双轨互斥的让位——重拟的消息 A 线当时休息已跳过，
    让位=作废后无人重拟=静默丢回复。"""
    def _auto_draft_cb(
        conv: dict, text: str, *, skip_companion_yield: bool = False,
    ) -> None:
        if conv.get("platform", "") in cfg.skip:
            return
        # leadbus 线索占位符不拟稿（2026-08-18）：`[线索捕获] <昵称>` 是系统合成的
        # 「捕获到一个线索」标记，不是客户发言——对它拟回复必然是无意义稿（LLM 被
        # 占位文本带偏），auto_ai 会话还会被 AutosendWorker 真发出去（本次事故：
        # 52 条线索 → 28 次投递撞「WhatsApp 服务未启用」刷 WARNING）。线索的首触达
        # 属坐席认领 / 获客侧 RPA 的职责，不属入站自动回复。前缀与 leadbus_routes
        # 合成处同源（LEAD_CAPTURE_PREFIX），判定异常一律放行（护栏不伤主链）。
        try:
            from src.integrations.leadbus_account import is_lead_capture_text
            if is_lead_capture_text(text):
                return
        except Exception:
            pass
        # 群/频道拟稿闸（P1 2026-08-20 收口 group_draft_skip 单源）：skip 开 →
        # 群一律不拟稿，**除非**坐席经 confirm_group 闸显式确认过全自动——否则
        # A 线让位（l2 deliver=on）+ 本处早退 = 双让死锁，确认过的群永远哑火。
        try:
            from src.inbox.automation_mode import group_draft_skip
            if group_draft_skip(
                conv, store, bool(cfg.skip_groups),
                trust_weak_evidence=bool(
                    getattr(cfg, "skip_groups_trust_weak", False)),
            ):
                return
        except Exception:
            # 兜底路径同样尊重「弱证据不静默」——否则单源函数导入失败时，
            # 被误判成群的私聊客户又掉回无草稿黑洞（兜底不该比主路更危险）。
            if cfg.skip_groups:
                try:
                    from src.inbox.ingest import is_group_conversation
                    from src.inbox.automation_mode import group_evidence_is_weak
                    _cid = str(conv.get("conversation_id") or "")
                    if is_group_conversation(conv) and not (
                            group_evidence_is_weak(_cid)
                            and not getattr(cfg, "skip_groups_trust_weak", False)):
                        return
                except Exception:
                    pass
        # min_len 活读（P1 2026-08-02）：cfg.min_len 是构造期快照，设置页写 overlay
        # 后热重载传导不到冻结的 dataclass——有 app_config（生产恒有，持 config 根
        # 引用、热重载就地 merge 可见新值）时以活值为准，异常/缺失回落快照。
        _min_len = cfg.min_len
        if app_config is not None:
            try:
                _min_len = int(((app_config.get("inbox") or {}).get(
                    "auto_draft") or {}).get("min_text_len", cfg.min_len))
            except (TypeError, ValueError):
                _min_len = cfg.min_len
        if _min_len > 0 and len(str(text or "").strip()) < _min_len:
            return
        # 工作时间班表·休息期不拟稿档（inbox.work_schedule.off_hours.
        # generate_drafts=false，默认 true=照常拟稿）：置 false = 休息期彻底
        # 静默省 LLM——注意复班**不补**这段消息（补觉只重拟「已有」的扣留稿），
        # 危机消息不受影响（should_hold 内 severe/elevated 穿透）。放在
        # peer_bot_guard 之前：纯函数判定比守卫的 DB 读更便宜。
        if app_config is not None and not skip_companion_yield:
            try:
                from src.inbox.work_hours_gate import (
                    off_hours_cfg,
                    should_hold_auto_reply,
                    work_schedule_cfg,
                )
                _ws = work_schedule_cfg(app_config)
                _ws_hold = ""
                if _ws.get("enabled"):
                    _ws_hold = should_hold_auto_reply(
                        _ws, str(conv.get("platform") or ""),
                        str(conv.get("account_id") or "default"),
                        peer_text=str(text or ""))
                if _ws_hold and not off_hours_cfg(_ws)["generate_drafts"]:
                    logger.info(
                        "[AutoDraft] 休息期不拟稿 cid=%s"
                        "（work_schedule.off_hours.generate_drafts=false）",
                        conv.get("conversation_id"))
                    return
                if _ws_hold:
                    # Q-4（#267 D-Q1）：休息期入站 → 稿照拟但 autosend 会扣住；在这里
                    # 落一条可检索的 hold 日志（同会话同「到点」只打一次），会话列表
                    # 标签「作息外 · 到点重新拟稿」由读侧按同口径计算。
                    from src.inbox.work_hours_gate import log_off_hours_hold
                    log_off_hours_hold(
                        str(conv.get("conversation_id") or ""), _ws,
                        str(conv.get("platform") or ""),
                        str(conv.get("account_id") or "default"))
            except Exception:
                logger.debug(
                    "[AutoDraft] 班表拟稿节流判定失败（放行）", exc_info=True)
        # ── 对方机器人守卫（P0 2026-08-03 / P2 2026-08-04）：确定级（Telegram
        # bot 账号）/复读/秒回熔断/每日预算 → **LLM 拟稿之前**判（才真省钱），
        # 确定级会话降 manual（bootstrap 只在无显式档位时写，不会打回来）。
        # P2：预算触顶的**真人**会话不再静默跳过（198 事故：A 线停 + 拟稿也停
        # = 客户被已读不回、坐席零感知），改软停 _pbg_soft → 下方把档位封顶
        # review：System Z 照常拟稿、AutosendWorker 不自动发、待审徽章可见。
        # 全平台生效；守卫默认关，异常一律放行。
        _pbg_soft = False
        try:
            from src.inbox.peer_bot_guard import guard_auto_draft_action
            _pbg_reason, _pbg_soft = guard_auto_draft_action(
                conv=conv, store=store, config=app_config)
            if _pbg_reason and not _pbg_soft:
                logger.info(
                    "[AutoDraft] peer_bot_guard 跳过拟稿 cid=%s reason=%s",
                    conv.get("conversation_id"), _pbg_reason)
                return
            if _pbg_soft:
                logger.info(
                    "[AutoDraft] peer_bot_guard 预算软停：本轮转人审拟稿 "
                    "cid=%s reason=%s",
                    conv.get("conversation_id"), _pbg_reason)
        except Exception:
            _pbg_soft = False
            logger.debug(
                "[AutoDraft] peer_bot_guard 检查失败（放行）", exc_info=True)
        # 每会话档位：坐席显式设置 > 全局 auto_draft.automation_mode。
        # Phase13：首条入站 bootstrap 持久化 auto_ai → UI/让位/System Z 口径一致。
        # 须在 companion 双轨判定之前解析——仅 auto_ai 时 A 线直发、System Z 让位；
        # review/manual 时 A 线停、System Z 拟稿（或静音），否则 UI「手动」无效。
        mode = cfg.mode
        # M-2 E（#235）：记下「会话显式档 / 账号层档」两个事实，L1 原因推导要用
        _l1_explicit = None
        _l1_account_layer = None
        try:
            cid = str(conv.get("conversation_id") or "")
            if cid and store is not None:
                try:
                    _l1_explicit = store.get_automation_mode_if_set(cid)
                except Exception:
                    _l1_explicit = None
                if app_config is not None:
                    from src.inbox.automation_mode import (
                        _account_mode_layer,
                        maybe_bootstrap_automation_mode,
                    )
                    mode = maybe_bootstrap_automation_mode(
                        store, cid, app_config)
                    if _l1_explicit is None:
                        try:
                            _l1_account_layer = _account_mode_layer(cid, app_config)
                        except Exception:
                            _l1_account_layer = None
                else:
                    explicit = store.get_automation_mode_if_set(cid)
                    if explicit is not None:
                        mode = explicit
        except Exception:
            pass
        # 档位封顶（平台 / 账号业务线 / 冷启动预热）——2026-08-07 收口为单一
        # 事实源 effective_automation.compute_mode_caps：A 线档位闸、
        # GET /api/unified-inbox/automation 的 effective 段、tools/why_no_reply.py
        # 排障 CLI 与本处同源消费（「对人展示的口径必须与护栏行为完全一致」，
        # 与 approve_blocked 预判徽标同一哲学）。逐层语义与旧内联实现等价：
        # 平台表活读（platform_modes 键缺席=构造期快照、显式 {}=运营清空）、
        # 业务线默认 translation→review（活读 business_line_modes，快照兜底）、
        # 冷启动预热判不出不封顶（存量账号零行为变更）、各层 fail-open。
        # 旧实现的三段内联代码与历史注释见本文件 git 历史（2026-08-03/04 落地）。
        try:
            from src.inbox.effective_automation import (
                apply_mode_caps,
                compute_mode_caps,
            )
            _caps_applied = []
            mode, _caps_applied = apply_mode_caps(mode, compute_mode_caps(
                platform=str(conv.get("platform") or ""),
                account_id=str(conv.get("account_id") or ""),
                config=app_config,
                platform_ceilings_fallback=cfg.platform_ceilings,
                business_line_ceilings_fallback=cfg.business_line_ceilings,
                # 实施72 P2：会话级信息给 ⑥「外机自有号对端」层（防跨机 AI 自聊）
                chat_key=str(conv.get("chat_key") or ""),
                peer_name=str(conv.get("display_name") or ""),
            ))
            for _cap in _caps_applied:
                if _cap.layer == "warmup":
                    _log_warmup_cap_once(
                        str(conv.get("platform") or "telegram"),
                        str(conv.get("account_id") or ""), _cap)
        except Exception:
            logger.debug(
                "[AutoDraft] 档位封顶失败（忽略）", exc_info=True)
        # #142 拦截不再静默：总闸（deliver_paused ⑦ 层）把本会话的「全自动」
        # 压成人审＝这条拟稿注定只写不发。记下命中细节，待草稿落库后写一条
        # 可见审计事件（draft_audit_log），不再只靠顶栏小标签。
        _gate_cap_detail = ""
        try:
            for _cap in _caps_applied:
                if _cap.layer == "deliver_paused":
                    _gate_cap_detail = str(_cap.detail or "deliver_paused")
                    break
        except Exception:
            _gate_cap_detail = ""
        # 预算软停封顶（peer_bot_guard P2）：auto_ai → review。放在 companion
        # 双轨互斥**之前**——封顶后 allows_direct_autosend(mode)=False，互斥
        # 判定自然不再让位（A 线已按预算停发，System Z 必须接管拟稿，否则
        # 就是 198 事故的「两边都让、无人拟稿」）；同时 AutosendWorker 只发
        # auto_ai 档草稿，review 草稿天然进人审队列。manual 仍保持 manual。
        if _pbg_soft:
            try:
                from src.inbox.drafts import cap_automation_mode
                mode = cap_automation_mode(mode, "review")
            except Exception:
                if mode == "auto_ai":
                    mode = "review"
        # Sprint1 双轨互斥（收窄）：companion 持有的 TG 号在 **auto_ai** 时由 A 线
        # 直发 → System Z 抑制防双发。坐席切到 review/manual/multi_choice 后 A 线
        # 让位，本回调继续拟稿（manual 下方早退），收件箱档位才真正生效。
        # 工作时间班表感知（2026-08-04）：A 线休息中不会直发（telegram_client
        # 同一 should_hold 判定让位）→ 此时**不让位、照常拟稿**，稿由
        # AutosendWorker 的班表闸扣到复班——否则隔夜消息两头都不管；
        # 危机穿透时 A 线照发 → 照旧让位防双发。skip_companion_yield=
        # 复班补觉重拟：消息当时已被 A 线跳过，必须旁路让位。
        try:
            if (str(conv.get("platform") or "") == "telegram"
                    and app_config is not None and not skip_companion_yield):
                from src.integrations.telegram_companion_worker import (
                    companion_runtime_enabled,
                )
                from src.inbox.automation_mode import allows_direct_autosend
                if companion_runtime_enabled(app_config) and allows_direct_autosend(mode):
                    from src.integrations.account_orchestrator import (
                        get_orchestrator_if_running,
                    )
                    _orch = get_orchestrator_if_running()
                    if _orch is not None and _orch.owns(
                            "telegram", str(conv.get("account_id") or "default")):
                        if _a_line_on_duty(app_config, conv, text):
                            return
        except Exception:
            logger.debug("[AutoDraft] companion 双轨互斥判定失败（忽略）", exc_info=True)
        if mode == "manual":
            return
        # Q-3（#264 A/B/E，调用侧）：① 触发源 reason——worker 侧经 draft_trigger.note 登记的
        # catchup_regen / risk_hold_regen，缺省 new_inbound；② 会话级风险持有 / 「需人工」标在场
        # → 本稿封顶 review（L1 人审），重新起草不得归零（XBGPBN 23:55:42 那条 L2 的口子）。
        # drafts.py 不动（Q-2 的）：它拿到的 automation_mode 已是 review。任何异常按原档放行。
        _cid_q3 = str(conv.get("conversation_id") or "")
        _trigger_q3 = "new_inbound"
        try:
            from src.inbox.draft_trigger import pop as _trig_pop
            _trigger_q3 = _trig_pop(_cid_q3) or "new_inbound"
        except Exception:
            _trigger_q3 = "new_inbound"
        _rh_cap = ""
        try:
            from src.inbox.risk_hold import active as _rh_active
            _rh_cap = str(_rh_active(store, _cid_q3) or "")
            if not _rh_cap and store is not None and hasattr(store, "get_conv_tags"):
                from src.integrations.protocol_autoreply import HANDOFF_TAG as _hp_tag
                if _hp_tag in list(store.get_conv_tags(_cid_q3) or []):
                    _rh_cap = "needs_human"
        except Exception:
            _rh_cap = ""
        if _rh_cap and mode == "auto_ai":
            mode = "review"
            logger.info("[policy] conv=%s risk_hold=%s forced=L1 stage=autodraft trigger=%s",
                        _cid_q3, _rh_cap, _trigger_q3)
        logger.info("[draft] trigger conv=%s reason=%s mode=%s", _cid_q3, _trigger_q3, mode)
        # 预算分子（peer_bot_guard P2）：只数「将自动投递」的拟稿轮次——
        # auto_ai 档 L2 会被 AutosendWorker 真发；review/multi_choice 是
        # 人审，人的决定不占 AI 预算。软停轮已封顶 review，天然不计。
        # companion 互斥让位的会话在上方已 return（那些轮由 A 线守卫计）。
        try:
            from src.inbox.automation_mode import (
                allows_direct_autosend as _pbg_allows,
            )
            if _pbg_allows(mode):
                from src.inbox.peer_bot_guard import note_auto_reply
                note_auto_reply(store, str(conv.get("conversation_id") or ""))
        except Exception:
            logger.debug("[AutoDraft] 预算台账计数失败（忽略）", exc_info=True)
        # M-2 E（#235 / D-M10）：这条稿若是 L1（mode≠auto_ai），先把「为什么要人确认」
        # 算成原因码登记到进程注册表——drafts.py 的 L1 日志行（D-M10 唯一许可的一行）
        # 从这里 peek；草稿落库后再写审计行 + 按 draft_id 登记（重启后 /api/drafts 仍可读）。
        _l1_reason = ""
        # Q-21 B（#302）：先登记一份会话语言计划（不带历史、不打日志——enrich 侧带历史的那次
        # 才是权威并打 [lang-plan]），让 derive_l1_reason 的 lang_unknown 与起草语言同一口径：
        # 英文「Hi」按文字系统判 en ＝ 对方语言有证据，不再报 lang_unknown。
        try:
            from src.inbox.outbound_translate import build_conv_lang_plan
            build_conv_lang_plan(
                str(conv.get("conversation_id") or ""), store=store, cfg_root=app_config or {},
                platform=str(conv.get("platform") or ""),
                account_id=str(conv.get("account_id") or "default"),
                chat_key=str(conv.get("chat_key") or ""), log=False)
        except Exception:
            logger.debug("[AutoDraft] lang-plan（预判）失败（忽略）", exc_info=True)
        try:
            from src.inbox.l1_reason import derive_l1_reason, note as _l1_note
            _l1_reason = derive_l1_reason(
                mode=mode, caps_applied=_caps_applied, conv=conv, store=store,
                peer_text=str(text or ""), explicit_mode=_l1_explicit,
                account_layer=_l1_account_layer)
            if _rh_cap and mode != "auto_ai":
                _l1_reason = "risk_hold"     # Q-3：封顶原因就是会话级风险持有，不再猜证据
            if _l1_reason:
                _l1_note(str(conv.get("conversation_id") or ""), _l1_reason)
        except Exception:
            _l1_reason = ""
        draft_id = draft_svc.auto_generate_draft(
            conv, text, automation_mode=mode, enrich=cfg.enrich
        )
        if draft_id and _l1_reason:
            try:
                from src.inbox.l1_reason import note as _l1_note2
                _l1_note2(str(draft_id), _l1_reason)
                if store is not None and hasattr(store, "record_draft_audit"):
                    store.record_draft_audit(
                        draft_id, autopilot_level="L1", action="l1_reason",
                        agent_id="autodraft", reason=_l1_reason,
                        conversation_id=str(conv.get("conversation_id") or ""))
            except Exception:
                logger.debug("[AutoDraft] L1 原因登记失败（忽略）", exc_info=True)
        # #142 可见事件：这条稿被真发总闸拦下（会话档全自动、总闸关 → 只写
        # 不发）。落 draft_audit_log（审计页/会话近期决策可查）；每条被拦稿
        # 一行＝与事实等量，不刷屏。best-effort，绝不影响拟稿主链。
        if draft_id and _gate_cap_detail and store is not None:
            try:
                store.record_draft_audit(
                    draft_id,
                    autopilot_level="L2",
                    action="gate_intercepted",
                    agent_id="autosend_gate",
                    reason=f"deliver_paused:{_gate_cap_detail}",
                    conversation_id=str(conv.get("conversation_id") or ""),
                )
            except Exception:
                logger.debug(
                    "[AutoDraft] 总闸拦截审计写入失败（忽略）", exc_info=True)
        # enrich：草稿已停泊（enriching），异步走人设产线补全正文
        if draft_id and cfg.enrich:
            try:
                asyncio.run_coroutine_threadsafe(
                    enrich_fn(conv, text, draft_id, mode),
                    loop,
                )
            except Exception:
                # 调度失败 → 立即兜底放行，避免卡 enriching
                logger.debug(
                    "[AutoDraft] 补全调度失败，兜底放行", exc_info=True)
                try:
                    draft_svc.release_enriching_draft(draft_id)
                except Exception:
                    pass
    return _auto_draft_cb


def setup_auto_draft(assistant, draft_svc, web_app):
    """装配 auto_draft 子系统并注册入站新消息回调(Stage3,从 initialize() 原样迁出)。

    enabled=false 仅记日志返回;否则读配置 -> AutoDraftConfig -> make_auto_draft_cb ->
    register_new_inbound_cb。web_app 供 enrich 走人设产线时取 telegram_client。"""
    _ad_cfg = (assistant.config.config or {}).get(
        "inbox", {}
    ).get("auto_draft", {}) or {}
    if _ad_cfg.get("enabled", True):
        _ad_mode = str(_ad_cfg.get("automation_mode", "auto_ai"))
        _ad_min_len = int(_ad_cfg.get("min_text_len", 0))
        _ad_skip = set(_ad_cfg.get("skip_platforms", []) or [])
        # 平台档位上限（比 skip_platforms 更细）：某平台链路不稳时
        # 降级而非全关——如 {messenger: review} 让 Messenger 仍拟稿、
        # 强制人审、绝不自动发。空 dict = 不封顶（旧行为）。
        _ad_platform_ceilings = {
            str(k).lower(): str(v).lower()
            for k, v in (
                _ad_cfg.get("platform_modes", {}) or {}
            ).items()
        }
        # 账号业务线档位上限（融合实例 P1）：默认 translation→review。
        # config 可覆盖/扩展（inbox.auto_draft.business_line_modes: {translation: review}）；
        # 显式置空 dict 即关闭业务线封顶。
        _ad_bl_raw = _ad_cfg.get("business_line_modes")
        if isinstance(_ad_bl_raw, dict):
            _ad_bl_ceilings = {
                str(k).lower(): str(v).lower() for k, v in _ad_bl_raw.items()
            }
        else:
            _ad_bl_ceilings = {"translation": "review"}
        # 源头止血：群/频道会话默认不入人审草稿队列（默认关=旧行为）。
        # 群消息本非 1:1 客服场景，生成 L3/L4 待审草稿只会长期无人处置、
        # 反复触发 SLA 铃铛，故提供开关从源头跳过。
        _ad_skip_groups = bool(_ad_cfg.get("skip_group_chats", False))
        # 弱证据群（messenger/instagram 网页 DOM 启发式）是否也照 skip 静默。
        # 默认 False：把「私聊被误判成群 → 客户永久无草稿且看板无痕」这条不可见
        # 失败换成「真群草稿进队列」这条可见失败。真群噪音受不了时置 true。
        _ad_skip_groups_weak = bool(
            _ad_cfg.get("skip_group_chats_trust_weak_evidence", False))
        # Phase 2：自动草稿正文走人设产线（与手动「生成草稿」同源）。
        # 默认开；关闭则回落旧规则模板（向后兼容）。
        _ad_enrich = bool(_ad_cfg.get("persona_enrich", True))
        _ad_store = assistant.inbox_store
        _ad_app = web_app
        try:
            _ad_loop = asyncio.get_running_loop()
        except RuntimeError:
            _ad_loop = asyncio.get_event_loop()

        async def _enrich_auto_draft(conv, text, draft_id, mode) -> None:
            return await enrich_auto_draft(assistant, draft_svc, _ad_app, _ad_store, conv, text, draft_id, mode)
        _auto_draft_cb = make_auto_draft_cb(
            AutoDraftConfig(
                mode=_ad_mode, min_len=_ad_min_len, skip=_ad_skip,
                platform_ceilings=_ad_platform_ceilings,
                skip_groups=_ad_skip_groups, enrich=_ad_enrich,
                business_line_ceilings=_ad_bl_ceilings,
                skip_groups_trust_weak=_ad_skip_groups_weak,
            ),
            draft_svc, _ad_store, _ad_loop, _enrich_auto_draft, assistant.logger,
            app_config=assistant.config.config or {},
        )
        # 拟稿回调挂 app.state：复班补觉重拟（AutosendWorker）经它走**原产线**
        # 重拟陈稿（bootstrap 在 setup_auto_draft 之后回填给 worker）。
        # 刻意挂裸回调而非 merger.push——重拟不是实时入站，无需爆发合并。
        try:
            web_app.state.auto_draft_cb = _auto_draft_cb
        except Exception:
            pass
        # 入站爆发合并（inbound_merge，2026-08-02 P0，默认关）：客户几秒内连发
        # 多条 → 各自触发独立拟稿 → 同义双发（WA 实锤占比 64%）。开启后新入站先
        # 进静默窗，窗内后续消息并入、到点用合并 peer_text 只拟一次稿。
        _im_cfg = None
        try:
            from src.inbox.inbound_debounce import (
                InboundMerger,
                resolve_merge_cfg,
            )
            _im_cfg = resolve_merge_cfg(_ad_cfg)
        except Exception:
            assistant.logger.debug("[inbound_merge] 配置解析失败（按关闭）",
                                   exc_info=True)
        if _im_cfg and _im_cfg.get("enabled"):
            _merger = InboundMerger(
                _auto_draft_cb,
                window_sec=_im_cfg["window_sec"],
                max_wait_sec=_im_cfg["max_wait_sec"],
                max_texts=_im_cfg["max_texts"],
                frag_max_wait_sec=_im_cfg["frag_max_wait_sec"],
                frag_max_texts=_im_cfg["frag_max_texts"],
                # K-3 F：语音条单独静默窗（连发语音不再逐条触发拟稿再被 fresh-guard 放弃）
                voice_window_sec=_im_cfg.get("voice_window_sec"),
                # O-1 D（D-O4）：静默窗 8–15s 随机（恒定到点是节拍器）
                window_max_sec=_im_cfg.get("window_max_sec"),
            )
            assistant.inbox_store.register_new_inbound_cb(_merger.push)
            try:
                web_app.state.inbound_merger = _merger  # 观测挂点
            except Exception:
                pass
            assistant.logger.info(
                "[inbound_merge] 入站爆发合并已启用（window=%.1f–%.1fs voice_window=%.1fs "
                "max_wait=%.1fs max_texts=%d）",
                _im_cfg["window_sec"],
                float(_im_cfg.get("window_max_sec") or _im_cfg["window_sec"]),
                float(_im_cfg.get("voice_window_sec") or 0),
                _im_cfg["max_wait_sec"], _im_cfg["max_texts"])
        else:
            assistant.inbox_store.register_new_inbound_cb(_auto_draft_cb)
        assistant.logger.info(
            "AutoDraft 已启用（per-conv 优先, 全局默认 mode=%s min_len=%s "
            "persona_enrich=%s skip=%s skip_groups=%s(weak_evidence=%s)）",
            _ad_mode, _ad_min_len, _ad_enrich, _ad_skip, _ad_skip_groups,
            "skip" if _ad_skip_groups_weak else "draft",
        )
    else:
        assistant.logger.info("AutoDraft 已禁用（auto_draft.enabled=false）")
