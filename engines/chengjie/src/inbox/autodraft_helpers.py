"""auto-draft 富化回调抽取（Stage 2，从 main.py initialize() 原样迁出，行为不变）。

enrich_auto_draft(assistant, draft_svc, _ad_app, _ad_store, conv, text, draft_id, mode)：
异步用会话历史 + 人设产线富化自动草稿；_ad_app=web_app, _ad_store=inbox_store。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

# 图文连发补识（Phase1.3）：拟稿时向前回扫多少条消息找「还没识别过」的入站媒体行。
# 窗口大小读 `inbox.auto_draft.media_backscan`（缺省本默认值；≤1 = 关闭回扫，退化为
# 「只看最新一条入站」旧行为）。覆盖「先发图/语音、紧跟一句话」的常见节奏；
# 窗口过大会把很久前的旧媒体也拉来识别。
_MEDIA_BACKSCAN_DEFAULT = 5


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
            for r in _ad_store.list_recent_messages(cid, limit=30):
                msgs.append({
                    "direction": r.get("direction") or "in",
                    "text": r.get("text") or "",
                    "media_type": r.get("media_type") or "",
                    "media_ref": r.get("media_ref") or "",
                    "message_id": r.get("message_id") or "",
                    "ts": r.get("ts") or 0,
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
            and _peer_media_ref
            and not _peer_media_desc
        ):
            try:
                from src.integrations.protocol_bridge import (
                    static_media_ref_to_path,
                )
                _img_path = static_media_ref_to_path(
                    _peer_media_ref
                )
                _tc = getattr(
                    getattr(_ad_app, "state", None),
                    "telegram_client",
                    None,
                )
                if _img_path and _tc is not None and hasattr(
                    _tc, "_get_image_content"
                ):
                    _desc = await _tc._get_image_content(
                        _img_path
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
                                text=f"[图片内容] {_peer_media_desc}",
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
                        _cap = last.strip()
                        if _cap.startswith("[图片") or _cap in (
                                "[贴纸]", "[媒体]", ""):
                            _cap = ""
                        _ifull = (
                            f"{_cap}\n[图片内容] {_peer_media_desc}"
                            if _cap
                            else f"[图片内容] {_peer_media_desc}"
                        )
                        if last.strip() in (
                                "[图片]", "[贴纸]", "[媒体]", "") or _cap:
                            last = _ifull
                        for _hm in reversed(history or []):
                            if isinstance(_hm, dict) and _hm.get(
                                    "role") == "user":
                                _hc = str(
                                    _hm.get("content") or "").strip()
                                if _hc in (
                                    "[图片]", "[贴纸]", "[媒体]", "",
                                ) or (
                                    _hc
                                    and not _hc.startswith("[图片内容]")
                                ):
                                    _hm["content"] = _ifull
                                break
                        assistant.logger.info(
                            "[AutoDraft] 图片识别补全: %s",
                            _peer_media_desc[:80],
                        )
            except Exception:
                assistant.logger.debug(
                    "[AutoDraft] 图片识别补全失败",
                    exc_info=True,
                )
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
        # 语言决策收敛到 generate_persona_reply（单一事实源，
        # 含短消息防误切）；这里不再各自重复检测，直接采信其
        # 返回的 reply_lang 落库 draft_lang。
        out = await generate_persona_reply(
            app=_ad_app, platform=platform, chat_key=chat_key,
            last_inbound=last, history=history,
            persona_id=_persona_id,
            risk_level=_risk_level,
            media_type=_peer_media_type,
            media_ref=_peer_media_ref,
            media_desc=_peer_media_desc,
            conversation_id=cid,
            peer_audio_emotion=_peer_audio_emotion,
            account_id=account_id,
        )
        if out.get("ok") and out.get("reply"):
            done = draft_svc.enrich_draft(
                draft_id, reply_text=out["reply"],
                reply_lang=str(out.get("reply_lang") or "zh"),
                automation_mode=mode,
            )
            if done:
                # L3 缓冲话术：草稿被定级为需人审（L3+）而不会自动发时，先「接住」客户
                # ——自动已读 + 一句安全缓冲话术，避免人审挂起期客户被沉默晾着（真实事故）。
                await _maybe_holding_after_enrich(
                    assistant, draft_svc, draft_id, conv, text, mode,
                    reply_lang=str(out.get("reply_lang") or ""))
                return
        # 生成失败/为空 → 兜底放行（规则模板占位）
        draft_svc.release_enriching_draft(draft_id)
    except Exception:
        assistant.logger.debug(
            "[AutoDraft] 人设补全失败，兜底放行 draft_id=%s",
            draft_id, exc_info=True)
        try:
            draft_svc.release_enriching_draft(draft_id)
        except Exception:
            pass


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
        # 是「该用什么语言跟客户说」的权威结论；仅当其缺失时才回落检测客户原话（原话可能仍是
        # 未转写的 [语音] 占位，检测不可靠，故降为兜底）。
        _lang = reply_lang or ""
        if not _lang:
            try:
                from src.ai.translation_service import detect_language as _dl
                _det = _dl(str(peer_text or ""))
                if _det and _det != "unknown":
                    _lang = _det
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


def make_auto_draft_cb(
    cfg: AutoDraftConfig, draft_svc, store, loop, enrich_fn, logger,
    *, app_config=None,
):
    """构造入站新消息 -> 自动草稿生成回调(从 main.py initialize() 原样迁出)。

    cfg=纯配置;draft_svc/store/loop/enrich_fn/logger=运行时依赖。
    app_config=完整配置树（可选）——供首条入站 bootstrap 持久化 auto_ai 档位。
    返回的回调签名 (conv, text)->None 与 register_new_inbound_cb 契约一致。"""
    def _auto_draft_cb(conv: dict, text: str) -> None:
        if conv.get("platform", "") in cfg.skip:
            return
        if cfg.skip_groups:
            try:
                from src.inbox.ingest import is_group_conversation
                if is_group_conversation(conv):
                    return
            except Exception:
                pass
        if cfg.min_len > 0 and len(str(text or "").strip()) < cfg.min_len:
            return
        # 每会话档位：坐席显式设置 > 全局 auto_draft.automation_mode。
        # Phase13：首条入站 bootstrap 持久化 auto_ai → UI/让位/System Z 口径一致。
        # 须在 companion 双轨判定之前解析——仅 auto_ai 时 A 线直发、System Z 让位；
        # review/manual 时 A 线停、System Z 拟稿（或静音），否则 UI「手动」无效。
        mode = cfg.mode
        try:
            cid = str(conv.get("conversation_id") or "")
            if cid and store is not None:
                if app_config is not None:
                    from src.inbox.automation_mode import (
                        maybe_bootstrap_automation_mode,
                    )
                    mode = maybe_bootstrap_automation_mode(
                        store, cid, app_config)
                else:
                    explicit = store.get_automation_mode_if_set(cid)
                    if explicit is not None:
                        mode = explicit
        except Exception:
            pass
        # 平台档位上限封顶（链路不稳时降级但不停）：如 Messenger 置
        # review → auto_ai 会话被降为 review（仍拟稿、强制人审、不自动发），
        # 坐席显式 manual 仍保持 manual。空配置 = 不封顶（零行为变更）。
        _ceil = cfg.platform_ceilings.get(
            str(conv.get("platform") or "").lower())
        if _ceil:
            try:
                from src.inbox.drafts import cap_automation_mode
                mode = cap_automation_mode(mode, _ceil)
            except Exception:
                logger.debug(
                    "[AutoDraft] 平台档位封顶失败（忽略）", exc_info=True)
        # 账号业务线封顶（融合实例：翻译线账号 → review，AI 仍拟稿、强制人审、
        # 绝不自动发）。封顶链 = 会话显式 > 账号业务线 > 平台 > 全局，各级只降不升；
        # 账号无标签 / 注册表未就绪 = 不封顶（零行为变更）。
        try:
            from src.integrations.account_registry import cached_business_line
            _bl = cached_business_line(
                str(conv.get("platform") or ""),
                str(conv.get("account_id") or ""))
            _bl_ceil = (cfg.business_line_ceilings or {}).get(_bl) if _bl else None
            if _bl_ceil:
                from src.inbox.drafts import cap_automation_mode
                mode = cap_automation_mode(mode, _bl_ceil)
        except Exception:
            logger.debug(
                "[AutoDraft] 业务线档位封顶失败（忽略）", exc_info=True)
        # Sprint1 双轨互斥（收窄）：companion 持有的 TG 号在 **auto_ai** 时由 A 线
        # 直发 → System Z 抑制防双发。坐席切到 review/manual/multi_choice 后 A 线
        # 让位，本回调继续拟稿（manual 下方早退），收件箱档位才真正生效。
        try:
            if str(conv.get("platform") or "") == "telegram" and app_config is not None:
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
                        return
        except Exception:
            logger.debug("[AutoDraft] companion 双轨互斥判定失败（忽略）", exc_info=True)
        if mode == "manual":
            return
        draft_id = draft_svc.auto_generate_draft(
            conv, text, automation_mode=mode, enrich=cfg.enrich
        )
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
            ),
            draft_svc, _ad_store, _ad_loop, _enrich_auto_draft, assistant.logger,
            app_config=assistant.config.config or {},
        )
        assistant.inbox_store.register_new_inbound_cb(_auto_draft_cb)
        assistant.logger.info(
            "AutoDraft 已启用（per-conv 优先, 全局默认 mode=%s min_len=%s "
            "persona_enrich=%s skip=%s skip_groups=%s）",
            _ad_mode, _ad_min_len, _ad_enrich, _ad_skip, _ad_skip_groups,
        )
    else:
        assistant.logger.info("AutoDraft 已禁用（auto_draft.enabled=false）")
