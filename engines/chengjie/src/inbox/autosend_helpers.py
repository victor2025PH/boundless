"""autosend 语音回调抽取（Stage 2，从 main.py initialize() 原样迁出，行为不变）。

autosend_voice(assistant, platform, account_id, chat_key, text) -> bool：
全自动语音（gated, 默认关）；只依赖 assistant（config/inbox_store/logger/_web_loop）+ 参数。
"""
from __future__ import annotations

import asyncio
from typing import Any  # noqa: F401


async def autosend_voice(assistant, platform, account_id, chat_key, text,
                         *, sent_text=None) -> bool:
    """全自动语音（gated, 默认关）：按策略把本条回复转 TTS
    语音经 orch.send_media 发出。返回 True=已作为语音发出；
    False=未发（未启用/不满足策略/合成或投递失败）→ 调用方回落文本。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。

    ``text`` 应是**翻译前的人设原文**——语音的长度判定与 TTS 合成都用它
    （克隆声念人设母语才自然；此前误用英文译文判长度 → 恒 too_long 静默回落文本）。
    ``sent_text``（可选）=文本分支实际要发的译文；与 ``text`` 不同说明出站翻译
    生效（客户语言≠人设语言），此时仅在对方上一条也是语音（对等回应，听得懂）
    才继续发语音，避免给纯外语文字客户发人设母语语音。"""
    _cfg = assistant.config.config or {}
    from src.inbox.voice_autosend import (
        resolve_voice_autosend_cfg,
        decide_voice, stage_voice_file,
        record_voice_sent, record_voice_fallback,
        record_voice_decision,
        persona_allowed_for_voice,
        pop_synth_failure_reason,
        resolve_defer_during_image,
    )
    _vb = resolve_voice_autosend_cfg(_cfg)
    if not _vb.get("enabled"):
        return False
    # 反双发：仅对**编排器管理**的账号发语音。原生 standalone
    # Telegram（camille_test）不归编排器 → owns_media=False →
    # 这里早退，让原生 voice_reply 独占（无双发）；编排器协议/
    # 官方号用裸 client + reply-hook，无原生语音 → System Z 接手。
    from src.integrations.account_orchestrator import (
        get_orchestrator as _go,
    )
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 上下文信号采集：when_peer_voice 用 peer_voice;
    # smart 档额外用「频率 + 客户此刻情绪 + 危机 + 亲密度」做情境评分。
    # 一次 list_recent_messages 复用算 peer_voice + 频率 + 客户末条文本。
    _peer_voice = False
    _peer_text = ""
    _voice_ratio = 0.0
    _peer_emo = ""
    _peer_emo_int = -1.0
    _intimacy = 0.0
    _crisis_block = False
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is not None:
            _cid = _cidf(platform, account_id, chat_key)
            try:
                _win = int(
                    ((_vb.get("smart") or {}).get("recent_window"))
                    or 6)
            except Exception:
                _win = 6
            _recent = _st.list_recent_messages(
                _cid, limit=max(_win, 6)) or []
            # peer_voice + 客户末条入站文本（危机判定用）
            for _m in reversed(_recent):
                if str(_m.get("direction") or "in") == "in":
                    _peer_voice = str(
                        _m.get("media_type") or ""
                    ).lower() in ("voice", "audio")
                    _peer_text = str(_m.get("text") or "")
                    break
            # recent_voice_ratio：近窗口 outbound 语音占比（频率刹车，保证"克制"）
            _outs = [
                _m for _m in _recent
                if str(_m.get("direction") or "") == "out"][-_win:]
            if _outs:
                _vc = sum(
                    1 for _m in _outs
                    if str(_m.get("media_type") or "").lower()
                    in ("voice", "audio"))
                _voice_ratio = _vc / float(len(_outs))
            # 客户此刻情绪 + 亲密度代理（conversation_meta 落库）
            try:
                _cm = _st.get_conv_meta(_cid) or {}
                _peer_emo = str(_cm.get("last_emotion") or "")
                _peer_emo_int = float(
                    _cm.get("last_emotion_intensity", -1.0))
                # 亲密度弱代理：聊得越多越熟（真 intimacy 在 contacts
                # 子系统/可能未启用 → msg_count 归一近似，0~1）。
                _mc = float(_cm.get("msg_count") or 0)
                _intimacy = max(0.0, min(1.0, _mc / 50.0))
            except Exception:
                pass
            # 危机：对客户末条入站文本跑权威 detect_crisis（severe/
            # elevated → 不机械发语音，走安全网；比 last_risk 落库更准）。
            try:
                if _peer_text:
                    from src.utils.wellbeing_guard import (
                        detect_crisis as _dc,
                    )
                    _crisis_block = str(
                        (_dc(_peer_text) or {}).get("level")
                        or "none").lower() in (
                        "severe", "elevated")
            except Exception:
                _crisis_block = False
    except Exception:
        _peer_voice = False
    # 语言闸门：出站翻译生效（实际发出的译文 ≠ 人设原文 → 客户语言 ≠ 人设语言）时，
    # 仅当对方上一条也是语音（对等回应，说明对方听得懂人设语言）才继续；否则回落译文
    # 文本——给只打外语文字的客户发人设母语语音只会露馅。
    if (sent_text and str(sent_text).strip()
            and str(sent_text).strip() != str(text or "").strip()
            and not _peer_voice):
        record_voice_decision(False, "lang_mismatch")
        assistant.logger.debug(
            "[autosend voice] 判文字 reason=lang_mismatch（出站翻译生效且对方"
            "未发语音）platform=%s acct=%s", platform, account_id)
        return False
    _vdec = decide_voice(
        _vb, text, peer_sent_voice=_peer_voice,
        recent_voice_ratio=_voice_ratio,
        peer_emotion=_peer_emo,
        peer_emotion_intensity=_peer_emo_int,
        intimacy=_intimacy,
        crisis_block=_crisis_block,
    )
    record_voice_decision(
        _vdec.send_voice, _vdec.reason)
    if not _vdec.send_voice:
        # 可观测性：对方发了语音却判文字属「意外回落」（如 too_long/crisis），升 INFO
        # 便于排查「为什么没回语音」；常规判文字记 debug 防刷屏。
        _log = (assistant.logger.info if _peer_voice
                else assistant.logger.debug)
        _log(
            "[autosend voice] 判文字 reason=%s score=%.2f peer_voice=%s "
            "len=%d platform=%s acct=%s",
            _vdec.reason, _vdec.score, _peer_voice,
            len(str(text or "").strip()), platform, account_id)
        return False
    # GPU 出图进行中 → defer 语音（防 7852/显存争用；回落文字，下轮可再试语音）
    if resolve_defer_during_image(_cfg, _vb):
        try:
            from src.inbox.image_autosend import image_gen_inflight as _igi
            if _igi() > 0:
                record_voice_fallback("image_in_flight")
                assistant.logger.info(
                    "[autosend voice] 发图进行中 defer 语音 → 回落文字 "
                    "inflight=%d platform=%s acct=%s",
                    _igi(), platform, account_id)
                return False
        except Exception:
            pass
    # 账号级人设（声音克隆 voice_profile 来源）。编排器
    # Telegram 协议号 meta 常无 persona_id（_pid 空）→ 用
    # 共享解析器按 meta.persona_id → meta.persona_ids[0] →
    # config[platform].persona_ids[0] 统一回退（根治复数/单数
    # 命名不匹配：sync 写 persona_ids 而旧代码读 persona_id →
    # 空 _real_pid → 灰度白名单误拦真声、回落纯文本的根因）。
    from src.ai.persona_voice import (
        resolve_account_persona_id as _rapi,
    )
    _pid = _rapi(_cfg, platform, account_id)
    # 解析真实人设（_pid 空时按 chat_key 绑定/默认回退），与
    # stage_voice_file 内部同口径（同 chat_key/account）。
    _real_pid = _pid
    try:
        from src.ai.persona_voice import (
            resolve_effective_voice_context as _revc,
        )
        _ctx0 = _revc(
            _cfg, persona_id=_pid or None,
            account_persona_id=_pid or None,
            chat_key=str(chat_key),
            contact_key=str(chat_key),
            platform=platform, account_id=account_id)
        _real_pid = str(
            _ctx0.get("persona_id") or _pid or "")
    except Exception:
        _real_pid = _pid
    # Phase2 人设级灰度白名单：名单非空时仅放行名单内人设发
    # 语音，名单外回落纯文本（正常回落，不计 fallback——未合成）。
    if not persona_allowed_for_voice(_vb, _real_pid):
        assistant.logger.info(
            "[autosend voice] 人设 %s 不在灰度白名单 → 回落"
            "文本 platform=%s acct=%s", _real_pid or "?",
            platform, account_id)
        return False
    # 至此策略已判定「该发语音」：合成/投递的成败计入指标。
    # P3：传 chat_key（端用户身份）→ 按会员档分层路由 TTS
    # 后端（VIP→旗舰，免费→降级省成本）；monetization 未就绪
    # → tier=None → 不路由（零行为变更）。
    _staged = await stage_voice_file(
        _cfg, platform, account_id, _real_pid, text,
        contact_key=str(chat_key))
    if not _staged:
        _fail_reason = pop_synth_failure_reason()
        record_voice_fallback(_fail_reason)
        assistant.logger.info(
            "[autosend voice] 合成失败回落文本 reason=%s platform=%s acct=%s",
            _fail_reason, platform, account_id)
        return False
    _local, _url, _smeta = _staged

    async def _vcoro():
        # caption="" → 客户收纯语音；inbox_text=text →
        # 坐席台会话里显示「自动语音念了什么」(转写)。
        return await _orch.send_media(
            platform, account_id, chat_key,
            media_path=_local, media_url=_url,
            media_type="voice", caption="",
            inbox_text=text)

    _wl = getattr(assistant, "_web_loop", None)
    if _wl is not None and _wl.is_running():
        _vf = asyncio.run_coroutine_threadsafe(_vcoro(), _wl)
        _vres = await asyncio.wrap_future(_vf)
    else:
        _vres = await _vcoro()
    _ok = bool(
        isinstance(_vres, dict) and _vres.get("delivered"))
    if _ok:
        _dur = 0
        try:
            from src.client.voice_sender import (
                probe_audio_duration_ms as _probe,
            )
            _dur = int(_probe(_local) or 0)
        except Exception:
            _dur = 0
        record_voice_sent(_dur, synth_meta={
            **_smeta,
            "audio_duration_ms": _dur,
            "persona_id": _real_pid,
        })
        assistant.logger.info(
            "[autosend voice] 已发语音 platform=%s acct=%s pid=%s "
            "dur=%sms provider=%s fallback=%s synth_len=%s trunc=%s",
            platform, account_id, _real_pid or "-",
            _dur,
            _smeta.get("provider") or "?",
            _smeta.get("fallback_from") or "-",
            _smeta.get("synth_text_len") or 0,
            "yes" if _smeta.get("truncation_suspect") else "no",
        )
        if _smeta.get("fallback_from"):
            assistant.logger.warning(
                "[autosend voice] 克隆回落 edge provider=%s fallback_from=%s "
                "platform=%s acct=%s",
                _smeta.get("provider"), _smeta.get("fallback_from"),
                platform, account_id)
    else:
        record_voice_fallback("deliver_failed")
        assistant.logger.info(
            "[autosend voice] 投递失败回落文本 platform=%s acct=%s",
            platform, account_id)
    return _ok


async def autosend_video(assistant, platform, account_id, chat_key, text) -> bool:
    """全自动「数字人口播视频」（gated, 默认关）：客户明确要视频（或对方刚发了视频）时，
    调 AvatarHub 口播数字人生成「人设本人念这段话」的 MP4 经 orch.send_media 发出。
    返回 True=已作为视频发出（跳过语音/文本）；False=未发（未启用/不满足/合成或投递失败）
    → 调用方回落语音/文本。一处生效全平台。

    与 autosend_voice 同护栏：仅编排器管理的账号（owns_media）；危机场景不发；人设灰度
    白名单；每会话每日频率上限。视频最贵 → 默认 trigger=on_request（仅客户要才发）。"""
    _cfg = assistant.config.config or {}
    from src.inbox.video_autosend import (
        resolve_video_autosend_cfg, decide_video, stage_video_file,
        persona_allowed_for_video, bump_daily,
        record_video_sent, record_video_fallback, record_video_decision,
    )
    _vb = resolve_video_autosend_cfg(_cfg)
    if not _vb.get("enabled"):
        return False
    # 反双发：仅对编排器管理且支持发媒体的账号（与语音/图片同口径）。
    from src.integrations.account_orchestrator import get_orchestrator as _go
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 上下文：客户最近一条入站文本（判要视频）+ 是否发了视频（对等）+ 危机。
    _peer_text = ""
    _peer_video = False
    _crisis_block = False
    _conv_key = ""
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is not None:
            _conv_key = _cidf(platform, account_id, chat_key)
            _recent = _st.list_recent_messages(_conv_key, limit=6) or []
            for _m in reversed(_recent):
                if str(_m.get("direction") or "in") == "in":
                    _peer_text = str(_m.get("text") or "")
                    _peer_video = str(_m.get("media_type") or "").lower() in (
                        "video", "video_note", "animation")
                    break
            try:
                if _peer_text:
                    from src.utils.wellbeing_guard import detect_crisis as _dc
                    _crisis_block = str(
                        (_dc(_peer_text) or {}).get("level") or "none"
                    ).lower() in ("severe", "elevated")
            except Exception:
                _crisis_block = False
    except Exception:
        _peer_text, _peer_video = "", False
    _send_video, _reason = decide_video(
        _vb, text, peer_text=_peer_text, peer_sent_video=_peer_video,
        conv_key=_conv_key, crisis_block=_crisis_block)
    record_video_decision(_send_video, _reason)
    if not _send_video:
        _log = (assistant.logger.info if (_peer_video or _reason == "daily_cap")
                else assistant.logger.debug)
        _log("[autosend video] 判非视频 reason=%s platform=%s acct=%s",
             _reason, platform, account_id)
        return False
    # 账号级人设（与 autosend voice 同口径解析）
    from src.ai.persona_voice import resolve_account_persona_id as _rapi
    _pid = _rapi(_cfg, platform, account_id)
    _real_pid = _pid
    try:
        from src.ai.persona_voice import resolve_effective_voice_context as _revc
        _ctx0 = _revc(_cfg, persona_id=_pid or None, account_persona_id=_pid or None,
                      chat_key=str(chat_key), contact_key=str(chat_key),
                      platform=platform, account_id=account_id)
        _real_pid = str(_ctx0.get("persona_id") or _pid or "")
    except Exception:
        _real_pid = _pid
    if not persona_allowed_for_video(_vb, _real_pid):
        assistant.logger.info(
            "[autosend video] 人设 %s 不在灰度白名单 → 回落 platform=%s acct=%s",
            _real_pid or "?", platform, account_id)
        return False
    # 合成（跨机调 AvatarHub .176；失败回落语音/文本）
    _staged = await stage_video_file(
        _cfg, platform, account_id, _real_pid, text, video_block=_vb)
    if not _staged:
        record_video_fallback("synth_failed")
        assistant.logger.info(
            "[autosend video] 合成失败回落 platform=%s acct=%s", platform, account_id)
        return False
    _local, _url = _staged

    async def _vcoro():
        return await _orch.send_media(
            platform, account_id, chat_key,
            media_path=_local, media_url=_url,
            media_type="video", caption="", inbox_text=text)

    _wl = getattr(assistant, "_web_loop", None)
    if _wl is not None and _wl.is_running():
        _vf = asyncio.run_coroutine_threadsafe(_vcoro(), _wl)
        _vres = await asyncio.wrap_future(_vf)
    else:
        _vres = await _vcoro()
    _ok = bool(isinstance(_vres, dict) and _vres.get("delivered"))
    if _ok:
        bump_daily(_conv_key)
        record_video_sent()
        assistant.logger.info(
            "[autosend video] 已发数字人视频 platform=%s acct=%s persona=%s",
            platform, account_id, _real_pid or "?")
    else:
        record_video_fallback("deliver_failed")
        assistant.logger.info(
            "[autosend video] 投递失败回落 platform=%s acct=%s", platform, account_id)
    return _ok


async def autosend_image(assistant, platform, account_id, chat_key, text,
                         assume_intent: str = "",
                         directive_override=None) -> bool:
    """全自动「按需发图」（gated, 默认关）：客户最近一条在要图/命中关键词时，
    优先发人设注册相册(关键词/通用池, 图或视频, 秒发)，否则回落生成
    (自拍相册/openai、物体图 text2img)，经 orch.send_media 发出。返回
    True=已作为图/视频发出（跳过语音/文本）；False=未发→回落。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。
    ``assume_intent="selfie"``＝承诺兑现路径：出站文本承诺了发照片，跳过
    peer_text 意图判定强制走自拍链（预算/关系闸门照常）。
    ``directive_override``＝主 LLM 的 [PHOTO …] 发图指令（photo_directive，
    2026-07-14）：意图+场景直通生成链，跳过关键词/相册判定。"""
    _cfg = assistant.config.config or {}
    from src.inbox.image_autosend import (
        resolve_image_autosend_cfg, run_autosend_image,
    )
    _scfg = resolve_image_autosend_cfg(_cfg)
    if not _scfg.get("enabled", False):
        return False
    # 反双发：仅对**编排器管理**且支持发媒体的账号发图（与语音同口径）。
    from src.integrations.account_orchestrator import (
        get_orchestrator as _go,
    )
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 客户最近一条入站文本（判要图）+ 近窗口历史（上下文抽主体）。
    _peer_text = ""
    _history: list = []
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is None:
            return False
        _cid = _cidf(platform, account_id, chat_key)
        _recent = _st.list_recent_messages(
            _cid, limit=12) or []
        for _m in _recent:
            _t = str(_m.get("text") or "")
            if _t:
                _history.append({
                    "role": "user" if str(
                        _m.get("direction") or "in") == "in"
                    else "assistant",
                    "content": _t})
        for _m in reversed(_recent):
            if (str(_m.get("direction") or "in") == "in"
                    and str(_m.get("text") or "")):
                _peer_text = str(_m.get("text"))
                break
    except Exception:
        return False
    if not _peer_text:
        return False
    # 账号级人设（相册分册 / 出图 prompt 来源），与语音同口径解析。
    from src.ai.persona_voice import (
        resolve_account_persona_id as _rapi,
    )
    _pid = _rapi(_cfg, platform, account_id)
    _real_pid = _pid
    try:
        from src.ai.persona_voice import (
            resolve_effective_voice_context as _revc,
        )
        _ctx0 = _revc(
            _cfg, persona_id=_pid or None,
            account_persona_id=_pid or None,
            chat_key=str(chat_key),
            contact_key=str(chat_key),
            platform=platform, account_id=account_id)
        _real_pid = str(
            _ctx0.get("persona_id") or _pid or "")
    except Exception:
        _real_pid = _pid
    # 物体图可选 LLM 精炼 prompt（heuristic 抽主体不稳时；仅生成回落用到）。
    _refine = None
    _ai = getattr(assistant, "ai_client", None)
    if (_scfg.get("contextual_images_llm_prompt", False)
            and _ai is not None):
        async def _refine():
            from src.ai.contextual_image import (
                build_llm_prompt_refine_instruction as _bi,
            )
            return await _ai.chat(
                _bi(_peer_text, _history))

    # 文图协同配文（可选，默认开）：发图时让 LLM 在**明知照片已发出**的
    # 前提下按客户原话写配文，替代「等我去拍」类与图矛盾的草稿文本。
    _caption = None
    if (_scfg.get("llm_caption", True) and _ai is not None):
        _pname = ""
        try:
            from src.utils.persona_manager import PersonaManager as _PM
            _p = _PM.get_instance().get_persona_by_id(_real_pid) or {}
            _pname = str(_p.get("name") or "")
        except Exception:
            _pname = ""

        async def _caption(_kind, _subject, _scene=""):
            from src.ai.companion_selfie import (
                build_photo_caption_instruction as _bc,
            )
            return await _ai.chat(_bc(
                _peer_text, kind=_kind, subject=_subject,
                persona_name=_pname, scene=_scene))

    # 发送 marshalling：把 orch.send_media 投到 web loop（与语音同口径）。
    async def _send_fn(_mp, _mu, _mt, _cap, _inbox):
        async def _coro():
            return await _orch.send_media(
                platform, account_id, chat_key,
                media_path=_mp, media_url=_mu,
                media_type=_mt, caption=_cap,
                inbox_text=_inbox)
        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(
                _coro(), _wl)
            _res = await asyncio.wrap_future(_f)
        else:
            _res = await _coro()
        return bool(
            isinstance(_res, dict)
            and _res.get("delivered"))

    # Phase20 已发媒体日志合流（A/B 线同一份）：deliver 与 draft 同属 worker 线程
    # 串行流程，ContextStore（draft 已在读写）此处追加安全。发图成功 → 记
    # {note, scene} 进 user_context._media_sent_log —— draft 的场景注入块
    # 【最近发过的照片】与「跟上次一样的」复刻两线通用。skill_manager 缺席时静默跳过。
    _sm_ref = getattr(assistant, "skill_manager", None)

    def _on_sent(_note: str, _scene: str) -> None:
        if _sm_ref is None:
            return
        try:
            _uc = _sm_ref._get_user_context(str(chat_key))
            _sm_ref._record_media_sent(_uc, note=_note, scene=_scene)
            _sm_ref._context_store.mark_dirty(str(chat_key))
            _sm_ref._context_store.flush(str(chat_key))
        except Exception:
            assistant.logger.debug(
                "[autosend image] 媒体日志写入失败（忽略）", exc_info=True)

    # 「跟上次一样的」（B 线版）：客户点名复刻上次场景 → 从媒体日志取 scene。
    _req_scene = ""
    if _sm_ref is not None:
        try:
            from src.ai.companion_selfie import wants_same_scene as _wss
            if _wss(_peer_text):
                _req_scene = _sm_ref._last_sent_media_scene(
                    _sm_ref._get_user_context(str(chat_key)))
        except Exception:
            _req_scene = ""

    return await run_autosend_image(
        _cfg, platform, account_id, chat_key,
        _real_pid, _peer_text, _history,
        send_fn=_send_fn, ai_text=text,
        llm_refine=_refine, llm_caption=_caption,
        assume_intent=str(assume_intent or ""),
        directive_override=directive_override,
        requested_scene=_req_scene,
        on_sent=_on_sent)

async def _depromise_autosend_text(assistant, text: str, kind: str) -> str:
    """撤回未兑现的媒体承诺（出站前最后修正）：LLM 重写（任意语言可靠）→
    正则句级剥离 → 语言对齐兜底话术。绝不返回空串（空文本没法投递）。

    只在「文本承诺了发照片/语音、且真发失败或未启用」时才被调用——正常文本
    永远不经过这里（零副作用）。"""
    from src.ai.outbound_promise_guard import (
        build_promise_rewrite_instruction, deflection_line,
        detect_media_promise, strip_media_promises,
    )
    _ai = getattr(assistant, "ai_client", None)
    if _ai is not None:
        try:
            out = str(await _ai.chat(
                build_promise_rewrite_instruction(text, kind)) or "")
            out = out.strip().strip('"“”「」').strip()
            # 重写合格判定：非空、长度不失控、且确实不再含承诺（防 LLM 阳奉阴违）
            if (out and len(out) <= max(200, len(str(text or "")) * 3)
                    and not detect_media_promise(out)):
                return out
        except Exception:
            assistant.logger.debug(
                "[promise_guard] LLM 撤回重写失败，回落正则剥离", exc_info=True)
    stripped = strip_media_promises(text)
    return stripped if stripped.strip() else deflection_line(text, kind)


async def autosend_bazi_kline(assistant, platform, account_id, chat_key, text) -> bool:
    """全自动「人生 K 线」出图（gated，companion.bazi.enabled+kline；独立于 selfie 开关）：
    客户最近一条入站在求运势曲线图时渲染 PNG 经 orch.send_media 发出。返回
    True=已作为图发出（跳过语音/文本）；False=未发→回落正常草稿流。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。"""
    _cfg = assistant.config.config or {}
    _bcfg = ((_cfg.get("companion") or {}).get("bazi") or {})
    if not (isinstance(_bcfg, dict) and _bcfg.get("enabled", False)
            and _bcfg.get("kline", True)):
        return False
    # 反双发：仅对编排器管理且支持发媒体的账号发图（与自拍/语音同口径）。
    from src.integrations.account_orchestrator import (
        get_orchestrator as _go,
    )
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 客户最近一条入站文本（判求图意图 + 同轮生辰）。
    _peer_text = ""
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is None:
            return False
        _cid = _cidf(platform, account_id, chat_key)
        _recent = _st.list_recent_messages(_cid, limit=12) or []
        for _m in reversed(_recent):
            if (str(_m.get("direction") or "in") == "in"
                    and str(_m.get("text") or "")):
                _peer_text = str(_m.get("text"))
                break
    except Exception:
        return False
    if not _peer_text:
        return False

    # 生辰解析回调：与草稿注入路径同一记忆键口径（user_id=chat_key, chat_id=""）。
    def _resolve_birth():
        _sm = getattr(assistant, "skill_manager", None)
        if _sm is None:
            return None
        try:
            _key = _sm._episodic_storage_key(str(chat_key), "", platform)
            return _sm.resolve_birth_info(_key) if _key else None
        except Exception:
            return None

    # 发送 marshalling：把 orch.send_media 投到 web loop（与自拍/语音同口径）。
    async def _send_fn(_mp, _mu, _mt, _cap, _inbox):
        async def _coro():
            return await _orch.send_media(
                platform, account_id, chat_key,
                media_path=_mp, media_url=_mu,
                media_type=_mt, caption=_cap,
                inbox_text=_inbox)
        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(_coro(), _wl)
            _res = await asyncio.wrap_future(_f)
        else:
            _res = await _coro()
        return bool(isinstance(_res, dict) and _res.get("delivered"))

    from src.inbox.image_autosend import run_autosend_kline
    return await run_autosend_kline(
        _cfg, platform, account_id, _peer_text,
        send_fn=_send_fn, resolve_birth=_resolve_birth)


def _is_desktop_account(platform, account_id) -> bool:
    """会话账号是否为内嵌「桌面/扩展」模式（无服务端 worker）。"""
    try:
        from src.integrations.account_registry import (
            get_account_registry as _gar,
        )
        _row = _gar().get(platform, account_id) or {}
        return str(_row.get("mode") or "") == "desktop"
    except Exception:
        return False


def build_autosend_translate_cb(assistant, web_app):
    """构造 AutosendWorker 出站自动翻译回调（投递前把 AI 回复译成客户语言）。

    从 main.py initialize() 原样抽出（行为不变）：未启用/装配失败返回 None。
    translation_service 懒取（worker 真正投递时早已挂到 web_app.state）。
    """
    try:
        from src.inbox.outbound_translate import (
            parse_outbound_translate_cfg as _parse_otx_cfg,
        )
        _otx_cfg = _parse_otx_cfg(assistant.config.config or {})
        if not _otx_cfg.get("enabled"):
            return None
        _otx_src = _otx_cfg.get("source_lang") or "zh"
        _otx_style = _otx_cfg.get("style") or "chat"

        async def _autosend_translate(item, _src=_otx_src, _style=_otx_style):
            from src.inbox.outbound_translate import (
                translate_outbound_text as _tot,
            )
            _ts = getattr(web_app.state, "translation_service", None)
            if _ts is None:
                return str(item.get("text", ""))
            return await _tot(
                item, translation_service=_ts,
                store=assistant.inbox_store,
                source_lang=_src, style=_style)

        assistant.logger.info(
            "AutosendWorker 出站自动翻译已启用（src=%s）", _otx_src)
        return _autosend_translate
    except Exception:
        assistant.logger.debug("出站自动翻译装配跳过", exc_info=True)
        return None


def build_autosend_mark_read_cb(assistant):
    """构造 AutosendWorker 投递前「已读回执」回调（拟人「先看后回」）。

    真人一定是先看到消息（对端出现已读）、想一会儿、再回——此前全自动直接投递，
    客户端上「消息还是未读却收到了回复」是最扎眼的机器人破绽。回调经编排器
    ``orch.mark_read`` 分发到受管 worker（当前 Telegram 协议号 pyrogram
    read_chat_history；WA/LINE/Messenger worker 暂不支持 → 静默 False；RPA 设备号
    由 RPA 打开会话时天然已读，不经此路径）。

    ``inbox.l2_autosend.mark_read_before_reply``（默认 true）置 false 可关闭。
    与发送同口径：编排器 client 活在 web 线程 loop 上 → 跨线程调度执行。
    """
    _cfg = assistant.config.config or {}
    _as_cfg = ((_cfg.get("inbox") or {}).get("l2_autosend") or {})
    if not bool(_as_cfg.get("mark_read_before_reply", True)):
        return None
    from src.integrations.account_orchestrator import get_orchestrator as _go

    async def _mark_read(platform, account_id, chat_key):
        _orch = _go(_cfg)

        async def _coro():
            return await _orch.mark_read(platform, account_id, str(chat_key))

        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(_coro(), _wl)
            return await asyncio.wrap_future(_f)
        return await _coro()

    return _mark_read


def build_autosend_typing_cb(assistant):
    """构造 AutosendWorker 投递延迟期「正在输入」状态回调（拟人打字气泡）。

    真人回复前对端会看到「对方正在输入…」。此前全自动在打字延迟(3-12s)期间无任何提示，
    延迟结束消息突然出现，仍显机械。回调经编排器 ``orch.send_chat_action`` 分发到受管
    worker（当前 Telegram 协议号 pyrogram send_chat_action；其余 worker 暂不支持 → 静默）。

    ``inbox.l2_autosend.typing_indicator``（默认 true）置 false 可关闭。与发送同口径：
    编排器 client 活在 web 线程 loop 上 → 跨线程调度执行。
    """
    _cfg = assistant.config.config or {}
    _as_cfg = ((_cfg.get("inbox") or {}).get("l2_autosend") or {})
    if not bool(_as_cfg.get("typing_indicator", True)):
        return None
    from src.integrations.account_orchestrator import get_orchestrator as _go

    async def _typing(platform, account_id, chat_key, action="typing"):
        _orch = _go(_cfg)

        async def _coro():
            return await _orch.send_chat_action(
                platform, account_id, str(chat_key), action)

        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(_coro(), _wl)
            return await asyncio.wrap_future(_f)
        return await _coro()

    return _typing


def build_autosend_callbacks(assistant, web_app, deliver_enabled):
    """构造 AutosendWorker 的 (send_callback, translate_callback)。

    从 main.py initialize() 原样抽出（行为不变）。deliver 编排「按需发图→语音→
    文本/桌面受控出站」三级投递；deliver_enabled=False 时 send_cb=None（仅 DB
    标记+审计，不发客户）。translate_cb 见 build_autosend_translate_cb。
    """
    send_cb = None
    if deliver_enabled:
        from types import SimpleNamespace as _SNS
        from src.inbox.channel_adapters import (
            send_via_adapters as _send_via,
            default_inbox_adapters as _dia,
        )
        _send_adapters = _dia()
        _send_shim = _SNS(app=web_app)
        _assistant_ref = assistant

        async def _try_autosend_voice(platform, account_id, chat_key, text,
                                      sent_text=None):
            return await autosend_voice(
                _assistant_ref, platform, account_id, chat_key, text,
                sent_text=sent_text)

        async def _try_autosend_video(platform, account_id, chat_key, text):
            return await autosend_video(_assistant_ref, platform, account_id, chat_key, text)

        async def _autosend_deliver(
            platform, account_id, chat_key, text, original_text=None
        ):
            # text=实际要发的文本（出站翻译生效时为译文）；original_text=翻译前人设原文
            # （worker 经签名探测透传）。语音分支用原文判定+合成，文本/桌面分支用译文。
            # ── LLM 发图指令（photo_directive，2026-07-14 决策权上移）────────────
            # 草稿 LLM 读过完整上下文，正文末行 [PHOTO …] 标记=它判定该发图+给了
            # 对话内场景。先解析并**剥净**两份文本（标记泄漏给客户=穿帮；出站翻译
            # 可能把标记译成中文变体，剥离正则已覆盖）。指令在下方图链里执行。
            _photo_directive = None
            try:
                from src.ai.photo_directive import (
                    extract_photo_directive, resolve_intent_mode,
                )
                _orig_for_parse = str(original_text or "")
                text, _pd_text = extract_photo_directive(str(text or ""))
                _stripped_orig, _pd_orig = extract_photo_directive(_orig_for_parse)
                if original_text is not None:
                    original_text = _stripped_orig
                _pd = _pd_text or _pd_orig
                _iscfg0 = (((_assistant_ref.config.config or {}).get(
                    "companion") or {}).get("selfie") or {})
                if _pd and resolve_intent_mode(_iscfg0) != "keyword":
                    _photo_directive = _pd
                    _assistant_ref.logger.info(
                        "[photo_directive] autosend LLM指令 kind=%s scene=%r",
                        _pd.get("kind"), (_pd.get("scene") or "")[:120])
            except Exception:
                _assistant_ref.logger.debug(
                    "[photo_directive] autosend 解析异常（忽略）", exc_info=True)
            # 命理「人生 K 线」最优先（gated，独立于 selfie 开关）：求运势曲线是
            # 最具体的结构化意图，先于泛化要图判定；未开/不满足/失败 → 继续。
            try:
                if await autosend_bazi_kline(
                    _assistant_ref, platform, account_id, chat_key, text
                ):
                    return {"ok": True, "delivered_as": "image"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend kline] 失败，回落图/语音/文本", exc_info=True)
            # 全自动「按需发图」优先（gated）：LLM 指令直通生成，或对方在要照片时
            # 关键词链出图；成功即作为图片发出、跳过语音/文本；失败 → 继续。
            try:
                if await autosend_image(
                    _assistant_ref, platform, account_id, chat_key, text,
                    directive_override=_photo_directive,
                ):
                    return {"ok": True, "delivered_as": "image"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend image] 失败，回落语音/文本", exc_info=True)
            # 出站媒体承诺守卫（companion.media_promise_guard，默认开）：
            # 发图判定看的是**客户入站文本**，而 LLM 草稿却可能自己承诺「等我拍/
            # 发你一张」——两边从不核对（实录事故：客户质问"你快拍啊是不是骗我"）。
            # 走到这=正常图链没发出：文本若承诺了照片 → 先尝试**兑现**
            # （assume_intent 强制自拍链，预算/关系闸门照常）；仍发不出 → **撤回**
            # （重写/剥离），文本、语音（念的就是这段文本）都不再对客户撒谎。
            _promised = ""
            _pg = {}
            try:
                _pg = (((_assistant_ref.config.config or {}).get(
                    "companion", {}) or {}).get(
                    "media_promise_guard", {}) or {})
                if _pg.get("enabled", True):
                    from src.ai.outbound_promise_guard import (
                        detect_media_promise as _dmp,
                    )
                    _promised = (_dmp(str(original_text or ""))
                                 or _dmp(str(text or "")))
            except Exception:
                _promised = ""
            if _promised == "image":
                from src.inbox.image_autosend import (
                    record_promise_event as _rpe,
                )
                _rpe("detected")
                if _pg.get("fulfill", True):
                    try:
                        if await autosend_image(
                            _assistant_ref, platform, account_id,
                            chat_key, text, assume_intent="selfie",
                        ):
                            _rpe("fulfilled")
                            _assistant_ref.logger.info(
                                "[promise_guard] 文本承诺发图 → 已兑现为真实"
                                "图片投递 platform=%s acct=%s",
                                platform, account_id)
                            return {"ok": True, "delivered_as": "image"}
                    except Exception:
                        _assistant_ref.logger.debug(
                            "[promise_guard] 兑现发图失败", exc_info=True)
                text = await _depromise_autosend_text(
                    _assistant_ref, text, "image")
                # 原文含未兑现承诺：语音/视频分支改念撤回后的文本（防克隆声念出谎话）
                original_text = None
                _rpe("retracted")
                _assistant_ref.logger.info(
                    "[promise_guard] 发图承诺无法兑现 → 已撤回改写文本 "
                    "platform=%s acct=%s", platform, account_id)
            # 全自动数字人视频（gated，默认关；客户明确要视频才发）：成功即作为视频发出、
            # 跳过语音/文本；未启用/不满足/失败 → 继续走语音/文本。视频念**翻译前原文**
            # （数字人念人设母语，与语音同口径）。
            try:
                _clip_text = str(original_text or "").strip() or text
                if await _try_autosend_video(
                    platform, account_id, chat_key, _clip_text
                ):
                    return {"ok": True, "delivered_as": "video"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend video] 失败，回落语音/文本", exc_info=True)
            # 全自动语音优先（gated）：成功即作为语音发出；
            # 未启用/不满足/失败 → 回落到下面的文本投递（零行为变更）。
            # 语音念**翻译前原文**（人设克隆声念母语；长度判定同口径），
            # 并把译文一并传入做语言闸门（外语文字客户不发语音）。
            try:
                _voice_text = str(original_text or "").strip() or text
                if await _try_autosend_voice(
                    platform, account_id, chat_key, _voice_text,
                    sent_text=text,
                ):
                    return {"ok": True, "delivered_as": "voice"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend voice] 失败，回落文本", exc_info=True)
            # 语音承诺撤回：文本承诺「发你条语音」而上面的语音分支没发成
            # （未启用/概率闸/语言闸/合成失败）→ 文本出站前剥掉语音承诺。
            if _promised == "voice":
                from src.inbox.image_autosend import (
                    record_promise_event as _rpe2,
                )
                _rpe2("detected")
                text = await _depromise_autosend_text(
                    _assistant_ref, text, "voice")
                _rpe2("retracted")
                _assistant_ref.logger.info(
                    "[promise_guard] 发语音承诺未兑现 → 已撤回改写文本 "
                    "platform=%s acct=%s", platform, account_id)
            # D4：桌面内嵌账号无服务端 worker，send_via_adapters 发不出去。
            # desktop_bridge 开启时把回复路由到「受控出站队列」——enqueue 内部
            # 先过 send-gate/kill-switch 闸门，通过才落队列，由桌面壳/扩展轮询
            # DOM 发送。被闸门拦截则返回 ok=False，让 worker 记 autosend_failed。
            try:
                _cfg = _assistant_ref.config.config or {}
                _br = ((_cfg.get("inbox", {}) or {}).get(
                    "l2_autosend", {}) or {}).get(
                    "desktop_bridge", {}) or {}
                if (_br.get("enabled", False)
                        and _is_desktop_account(platform, account_id)):
                    from src.inbox.desktop_outbound import (
                        get_desktop_outbound_queue as _gdoq,
                    )
                    from src.integrations.account_registry import (
                        get_account_registry as _gar2,
                    )
                    # 人审模式（review_mode）：命令落 held 等运营放行，
                    # 而非直接 pending 自动发。仍先过闸门（受控不变式）。
                    _review = bool(_br.get("review_mode", False))
                    _res = _gdoq().enqueue(
                        platform, account_id, chat_key, text,
                        config=_cfg, registry=_gar2(),
                        hold=_review,
                    )
                    if _res.get("enqueued"):
                        return {"ok": True,
                                "delivered_as": (
                                    "desktop_review"
                                    if _res.get("status") == "held"
                                    else "desktop_queued"),
                                "id": _res.get("id")}
                    return {"ok": False,
                            "error": "blocked:" + str(
                                _res.get("blocked") or "")}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend desktop_bridge] 路由失败", exc_info=True)
            # AutosendWorker 跑在主 loop；而协议号(telegram/whatsapp
            # pyrogram/Baileys)的 worker 由编排器经 FastAPI startup
            # 钩子启动，活在「web 线程的 web_loop」上。直接在主 loop
            # await orch.send → client.send_message 会触发
            # "Future attached to a different loop"。故：编排器拥有
            # 该账号时，把整次投递调度到 web_loop 执行再跨线程取回。
            def _make_coro():
                return _send_via(
                    _send_shim, platform, account_id,
                    chat_key, text, _send_adapters,
                )
            _wl = getattr(_assistant_ref, "_web_loop", None)
            _orch_owns = False
            try:
                from src.integrations.account_orchestrator import (
                    get_orchestrator as _get_orch,
                )
                _orch_owns = _get_orch(
                    _assistant_ref.config.config or {}
                ).owns(platform, account_id)
            except Exception:
                _orch_owns = False
            if (_orch_owns and _wl is not None
                    and _wl.is_running()):
                _fut = asyncio.run_coroutine_threadsafe(
                    _make_coro(), _wl
                )
                return await asyncio.wrap_future(_fut)
            return await _make_coro()

        send_cb = _autosend_deliver
    translate_cb = build_autosend_translate_cb(assistant, web_app)
    return send_cb, translate_cb
