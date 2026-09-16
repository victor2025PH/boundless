"""autosend 语音回调抽取（Stage 2，从 main.py initialize() 原样迁出，行为不变）。

autosend_voice(assistant, platform, account_id, chat_key, text) -> bool：
全自动语音（gated, 默认关）；只依赖 assistant（config/inbox_store/logger/_web_loop）+ 参数。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401


def _safe_voice_inbox_text(text: str) -> str:
    """语音镜像落库前剥系统标签。只动语音念稿，不动「[图片] 配文」这类占位。"""
    src = str(text or "")
    if not src.strip() or ("[" not in src and "【" not in src):
        return src
    try:
        from src.ai.outbound_text_guard import strip_system_labels
        out, hits = strip_system_labels(src)
        return out if hits else src
    except Exception:
        return src


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
        effective_voice_block,
        record_voice_sent, record_voice_fallback,
        record_voice_decision,
        persona_allowed_for_voice,
        pop_synth_failure_reason,
        resolve_defer_during_image,
    )
    # 平台触发覆写（P1）：voice.platform_triggers = {platform: trigger}——
    # 在唯一决策点套用，Messenger 置 never 即只关一个平台的语音。
    _vb = effective_voice_block(resolve_voice_autosend_cfg(_cfg), platform)
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
    from src.inbox.voice_session_guard import (
        resolve_voice_session_cfg as _rvsc,
        should_quiet_after_voice as _sqav,
        voice_peer_lang_conflict as _vplc,
        mark_voice_delivered as _mvd,
    )
    _session_cfg = _rvsc(_vb)
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
    _cid = ""
    _recent = []
    _peer_lang = ""
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
                _cid, limit=max(_win, 12)) or []
            # peer_voice + 客户末条入站文本（危机判定用）
            for _m in reversed(_recent):
                if str(_m.get("direction") or "in") == "in":
                    _peer_voice = str(
                        _m.get("media_type") or ""
                    ).lower() in ("voice", "audio")
                    _peer_text = str(_m.get("text") or "")
                    break
            try:
                from src.inbox.outbound_translate import peer_language_hint
                _peer_lang = peer_language_hint(_st, _cid) or ""
            except Exception:
                _peer_lang = ""
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
                # P1-198 续（2026-08-02）：坐席人工「情绪低落」标注（TTL 内）→
                # 语音安抚加权。voice_fitness 的 peer_emotion 轴只按强度计分
                # （w=0.20），人工标注视同高强度负面（≥0.7）——「该被声音安抚」
                # 跟随坐席判断；「积极开朗」不反向压低（不对称覆写）。
                try:
                    from src.inbox.effective_mood import (
                        GATE_EMOTION_NEG,
                        manual_negative_active,
                        record_mood_consume,
                        resolve_mood_steering_cfg,
                    )
                    _ms_v = resolve_mood_steering_cfg(_cfg)
                    if _ms_v["enabled"] and manual_negative_active(
                            _cm, now=time.time(),
                            ttl_hours=_ms_v["ttl_hours"]):
                        _peer_emo = GATE_EMOTION_NEG
                        _peer_emo_int = max(_peer_emo_int, 0.7)
                        record_mood_consume("voice")
                except Exception:
                    pass
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
    # 跨草稿静默窗：语音已达且无新入站 → 本轮不再发语音（文本侧在 deliver 同闸）。
    _vk0 = _cid or f"{platform}:{account_id}:{chat_key}"
    # A3 熔断（#150，JZPBAC 21:03-21:09 两自家 AI 号语音乒乓 4 条/分钟无终止）：
    # 同会话短窗语音达阈值 → 降级文字并冷却；voice_burst_guard 只告警不止损是根因。
    try:
        from src.client.voice_burst_guard import voice_degrade_reason as _vdr
        _burst_why = _vdr(_vk0, _vb)
    except Exception:
        _burst_why = ""
    if _burst_why:
        record_voice_decision(False, _burst_why)
        assistant.logger.warning(
            "[autosend voice] 判文字 reason=%s（语音连发熔断，降级文字）"
            "platform=%s acct=%s", _burst_why, platform, account_id)
        return False
    if _sqav(
        conv_key=_vk0, recent_messages=_recent,
        quiet_after_sec=_session_cfg.get("quiet_after_sec", 90),
    ):
        record_voice_decision(False, "voice_quiet")
        assistant.logger.info(
            "[autosend voice] 判文字 reason=voice_quiet（语音已达无新入站）"
            "platform=%s acct=%s", platform, account_id)
        return False
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
    # peer 语种硬闸（翻译关/失败时的盲区）：外语客户 + 念稿实质中文 → 拒语音。
    if _session_cfg.get("peer_lang_gate", True) and not _peer_voice:
        _pl_why = _vplc(str(text or ""), _peer_lang)
        if _pl_why:
            record_voice_decision(False, _pl_why)
            assistant.logger.info(
                "[autosend voice] 判文字 reason=%s peer_lang=%s platform=%s acct=%s",
                _pl_why, _peer_lang or "?", platform, account_id)
            return False
    # 客户点名要语音/唱歌（复用 wants_media 的 voice 轴）→ 强制语音（P0-5）。
    _peer_req_voice = False
    try:
        from src.ai.outbound_promise_guard import wants_media as _wm2
        _peer_req_voice = (_wm2(_peer_text) == "voice")
    except Exception:
        _peer_req_voice = False
    _vdec = decide_voice(
        _vb, text, peer_sent_voice=_peer_voice,
        peer_requested_voice=_peer_req_voice,
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
    # 生效人设（声音克隆 voice_profile 来源）。编排器
    # Telegram 协议号 meta 常无 persona_id（_pid 空）→ 用
    # 共享解析器按 会话覆写 → meta.persona_id → meta.persona_ids[0] →
    # config[platform].persona_ids[0] 统一回退（根治复数/单数
    # 命名不匹配：sync 写 persona_ids 而旧代码读 persona_id →
    # 空 _real_pid → 灰度白名单误拦真声、回落纯文本的根因；
    # 2026-07-26 起会话级覆写让声与文同源，换绑后语音跟着换人）。
    from src.ai.persona_voice import (
        resolve_effective_persona_id as _repi,
    )
    _pid = _repi(_cfg, platform, account_id, str(chat_key))
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
    # ── P0-4 分条语音（2026-08-03）：像真人一样连发 2-3 条短语音，替代 20-30s
    # 一整条。stage_voice_parts 不满足（未开/文本短/切不出两条/任一条合成失败）
    # → None → 走下方单条整段旧路径，行为绝不劣化。条间等待=「下一条要录完才能
    # 发」的物理节奏（part_gap_seconds，确定性）；中途投递失败=已发算数、剩余丢弃
    # （与 A 线 split_send 同语义——绝不重发已出口的条目）。
    _staged_parts = None
    try:
        from src.inbox.voice_autosend import stage_voice_parts
        _staged_parts = await stage_voice_parts(
            _cfg, platform, account_id, _real_pid, text,
            contact_key=str(chat_key))
    except Exception:
        assistant.logger.debug(
            "[autosend voice] 分条 staging 异常 → 回落单条", exc_info=True)
        _staged_parts = None
    if _staged_parts and len(_staged_parts) >= 2:
        from src.ai.persona_voice import persona_display_name as _pdn
        from src.inbox.voice_autosend import part_gap_seconds as _pgap
        _v_sender2 = _pdn(_real_pid)
        _wl2 = getattr(assistant, "_web_loop", None)
        _sp_cfg = _vb.get("split_send") if isinstance(
            _vb.get("split_send"), dict) else {}
        try:
            _gap_max = float(_sp_cfg.get("gap_max_sec", 6.0) or 6.0)
        except (TypeError, ValueError):
            _gap_max = 6.0
        _sent_n = 0
        _total_dur = 0
        for _idx, (_plocal, _purl, _pmeta) in enumerate(_staged_parts):
            _pdur = 0
            try:
                from src.client.voice_sender import (
                    probe_audio_duration_ms as _probe2,
                )
                _pdur = int(_probe2(_plocal) or 0)
            except Exception:
                _pdur = 0
            _ptext = _safe_voice_inbox_text(
                str(_pmeta.get("part_text") or "").strip() or str(text))
            if _idx:
                await asyncio.sleep(_pgap(_ptext, _pdur, gap_max_sec=_gap_max))

            async def _pcoro(_pl=_plocal, _pu=_purl, _pt=_ptext):
                return await _orch.send_media(
                    platform, account_id, chat_key,
                    media_path=_pl, media_url=_pu,
                    media_type="voice", caption="",
                    inbox_text=_pt, sender_name=_v_sender2)

            if _wl2 is not None and _wl2.is_running():
                _pf = asyncio.run_coroutine_threadsafe(_pcoro(), _wl2)
                _pres = await asyncio.wrap_future(_pf)
            else:
                _pres = await _pcoro()
            if not (isinstance(_pres, dict) and _pres.get("delivered")):
                assistant.logger.warning(
                    "[autosend voice] 分条第 %d/%d 条投递失败（已发 %d 条算数，"
                    "剩余丢弃）platform=%s acct=%s",
                    _idx + 1, len(_staged_parts), _sent_n, platform, account_id)
                break
            _sent_n += 1
            _total_dur += _pdur
        if _sent_n <= 0:
            record_voice_fallback("deliver_failed")
            assistant.logger.info(
                "[autosend voice] 分条首条投递失败回落文本 platform=%s acct=%s",
                platform, account_id)
            return False
        _first_meta = dict(_staged_parts[0][2] or {})
        record_voice_sent(_total_dur, synth_meta={
            **_first_meta,
            "audio_duration_ms": _total_dur,
            "persona_id": _real_pid,
            "parts_total": len(_staged_parts),
            "parts_sent": _sent_n,
        })
        assistant.logger.info(
            "[autosend voice] 已分条发语音 parts=%d/%d total_dur=%sms "
            "provider=%s pid=%s platform=%s acct=%s",
            _sent_n, len(_staged_parts), _total_dur,
            _first_meta.get("provider") or "?", _real_pid or "-",
            platform, account_id)
        _vk = _cid or f"{platform}:{account_id}:{chat_key}"
        _mvd(_vk)
        try:
            from src.client.voice_burst_guard import note_voice_send as _nvs
            # 分条按「每次投递」记窗；阈值 > max_parts 才告警（默认 3）
            for _ in range(_sent_n):
                _nvs(_vk, {
                    "burst_alert": (_vb.get("burst_alert")
                                   if isinstance(_vb.get("burst_alert"), dict)
                                   else {"enabled": True}),
                })
        except Exception:
            pass
        return True

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

    # P1-3：镜像行带「谁的音色」（语音人设显示名，best-effort 空串安全）
    from src.ai.persona_voice import persona_display_name
    _v_sender = persona_display_name(_real_pid)

    async def _vcoro():
        # caption="" → 客户收纯语音；inbox_text=剥过标签的念稿 →
        # 坐席台会话里显示「自动语音念了什么」(转写)，且下一轮历史不再教模型抄标签。
        return await _orch.send_media(
            platform, account_id, chat_key,
            media_path=_local, media_url=_url,
            media_type="voice", caption="",
            inbox_text=_safe_voice_inbox_text(text), sender_name=_v_sender)

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
        _vk = _cid or f"{platform}:{account_id}:{chat_key}"
        _mvd(_vk)
        try:
            from src.client.voice_burst_guard import note_voice_send as _nvs
            _nvs(_vk, {
                "burst_alert": (_vb.get("burst_alert")
                               if isinstance(_vb.get("burst_alert"), dict)
                               else {"enabled": True}),
            })
        except Exception:
            pass
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
    # 生效人设（与 autosend voice 同口径解析，含会话覆写）
    from src.ai.persona_voice import resolve_effective_persona_id as _repi
    _pid = _repi(_cfg, platform, account_id, str(chat_key))
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
                         assume_scene: str = "",
                         directive_override=None) -> bool:
    """全自动「按需发图」（gated, 默认关）：客户最近一条在要图/命中关键词时，
    优先发人设注册相册(关键词/通用池, 图或视频, 秒发)，否则回落生成
    (自拍相册/openai、物体图 text2img)，经 orch.send_media 发出。返回
    True=已作为图/视频发出（跳过语音/文本）；False=未发→回落。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。
    ``assume_intent="selfie"``＝承诺兑现路径：出站文本承诺了发照片，跳过
    peer_text 意图判定强制走自拍链（预算/关系闸门照常）。
    ``assume_scene``（P0 一致性）＝承诺句里点名的场景（promised_scene 抽取）：
    兑现必须贴承诺场景（相册场景类硬匹配/生成带场景），杜绝随机人像顶包。
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
    # Q-39 A（#327 / #323）：顺带取该入站的 media_type / message_id——图入站只看客户配文，
    # 同 mid 相册匹配只跑一次（image_autosend 意图闸消费）。
    _peer_text = ""
    _history: list = []
    _inbound_mt = ""
    _inbound_mid = ""
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
                _inbound_mt = str(_m.get("media_type") or "")   # Q-39 A
                _inbound_mid = str(_m.get("message_id") or "")  # Q-39 A
                break
    except Exception:
        return False
    if not _peer_text:
        return False
    # 会话语言（#143 0902）：配文必须按会话语言/客户语言画像生成——此前 lang
    # 恒空，固定配文池永远落中文（「手机里存的这张…」发英文客户实锤）；LLM 配文
    # 又被指示「跟随对方消息语言」，而贴纸/图片轮的对方消息是系统中文识别标注。
    # 判定走 resolve_reply_language（#74 同源证据口径：系统注入行不算语言证据）；
    # 判不出返回空串＝旧行为（中文默认），出口另有 #64 出站语种兜底。
    _conv_lang = ""
    try:
        # R87 #329：会话「发→X」显式铆定（B67）≻ 自动判定——客户打中文、铆「发→日」时配文按日文口径
        from src.ai.sendpoint_guard import outbound_lang_pin as _olp
        _conv_lang = str(_olp(platform, account_id, chat_key) or "")
    except Exception:
        _conv_lang = ""
    if not _conv_lang:
        try:
            from src.inbox.persona_reply import resolve_reply_language
            _conv_lang = resolve_reply_language(_peer_text, _history, default="")
        except Exception:
            _conv_lang = ""
    # 生效人设（相册分册 / 出图 prompt 来源），与语音同口径解析（含会话覆写）。
    from src.ai.persona_voice import (
        resolve_effective_persona_id as _repi,
    )
    _pid = _repi(_cfg, platform, account_id, str(chat_key))
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

        async def _caption(_kind, _subject, _scene="", _freshness="fresh",
                           _wanted=""):
            from src.ai.companion_selfie import (
                build_photo_caption_instruction as _bc,
            )
            return await _ai.chat(_bc(
                _peer_text, kind=_kind, subject=_subject,
                persona_name=_pname, scene=_scene,
                freshness=_freshness, wanted_subject=_wanted,
                # #143：配文语言钉会话语言（贴纸/图片轮的对方消息是系统中文
                # 标注，「跟随对方消息语言」会配出中文发外语客户）
                reply_lang=_conv_lang))

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
        # #259 P-3：回执契约——把平台 message_id 一并交回图链（真发成功=回执，
        # 不是 dispatch；``image_autosend._send_result`` 兼容旧 bool）。
        if isinstance(_res, dict) and _res.get("delivered"):
            return {"delivered": True,
                    "message_id": str(_res.get("message_id") or "")}
        return {"delivered": False}

    # Phase20 已发媒体日志合流（A/B 线同一份）：deliver 与 draft 同属 worker 线程
    # 串行流程，ContextStore（draft 已在读写）此处追加安全。发图成功 → 记
    # {note, scene, series} 进 user_context._media_sent_log —— draft 的场景注入块
    # 【最近发过的照片】/「跟上次一样的」复刻/衣着连续窗（P1）三处通用。
    # skill_manager 缺席时静默跳过。
    _sm_ref = getattr(assistant, "skill_manager", None)

    def _on_sent(_note: str, _scene: str, _series: str = "") -> None:
        if _sm_ref is None:
            return
        try:
            _uc = _sm_ref._get_user_context(str(chat_key))
            _sm_ref._record_media_sent(
                _uc, note=_note, scene=_scene, series=_series)
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
        # #143：lang 此前恒缺 → 固定配文池永远取中文；现按会话语言取
        # （registry caption_i18n / caption_album 双语池同口径）。
        lang=_conv_lang,
        assume_intent=str(assume_intent or ""),
        assume_scene=str(assume_scene or ""),
        directive_override=directive_override,
        requested_scene=_req_scene,
        on_sent=_on_sent,
        # Q-39 A（#327 / #323）：入站形态 + 消息 id 进意图闸 / 同 mid 去重
        inbound_media_type=_inbound_mt,
        inbound_mid=_inbound_mid)

async def _depromise_autosend_text(
    assistant, text: str, kind: str, *, media_context: bool = False,
    sent_claim: bool = False,
) -> str:
    """撤回未兑现的媒体承诺/断言（出站前最后修正）：LLM 重写（任意语言可靠）→
    正则句级剥离 → 语言对齐兜底话术。绝不返回空串（空文本没法投递）。

    只在「文本承诺/声称发了照片语音、且真发失败或未启用」时才被调用——正常文本
    永远不经过这里（零副作用）。``media_context``＝客户本轮在索要媒体，据此一并
    剥「已发」断言句（claim；2026-07-29 对练实证：撤回后残留「这不就来了嘛」）。
    ``sent_claim``（#171）＝命中「我刚发了/你该收到了/我再发一次」式过去时假声明
    且近窗无媒体真发：重写指令与兜底话术改用「这边没发出去、稍后补」的如实口径，
    正则回落一并剥 sent-claim 句。"""
    from src.ai.outbound_promise_guard import (
        build_promise_rewrite_instruction, deflection_line,
        detect_media_claim, detect_media_promise, detect_sent_claim,
        strip_media_claims, strip_media_promises, strip_sent_claims,
    )
    _ai = getattr(assistant, "ai_client", None)
    if _ai is not None:
        try:
            out = str(await _ai.chat(
                build_promise_rewrite_instruction(
                    text, kind, sent_claim=sent_claim)) or "")
            out = out.strip().strip('"“”「」').strip()
            # 重写合格判定：非空、长度不失控、且确实不再含承诺**或断言**（防阳奉阴违）
            if (out and len(out) <= max(200, len(str(text or "")) * 3)
                    and not detect_media_promise(out)
                    and not detect_media_claim(out, media_context=media_context)
                    and not (sent_claim and detect_sent_claim(out))):
                return out
        except Exception:
            assistant.logger.debug(
                "[promise_guard] LLM 撤回重写失败，回落正则剥离", exc_info=True)
    stripped = strip_media_promises(text)
    stripped = strip_media_claims(stripped, media_context=media_context)
    if sent_claim:
        stripped = strip_sent_claims(stripped)
    return stripped if stripped.strip() else deflection_line(
        text, kind, sent_claim=sent_claim)


def _media_sent_recently(
    assistant, platform: str, account_id: str, chat_key: str, *,
    kind: str = "image", window_sec: float = 1800.0, now: Optional[float] = None,
) -> bool:
    """B 线「近窗内该会话是否真发过媒体」（#171 已发假声明的真伪判据）。

    读 inbox 出站消息镜像（``list_recent_messages`` 的 ``direction=out`` +
    ``media_type``）——所有编排器 send_media 路径都镜像到这里，相册/生成/坐席
    手发一律可见，比只查 ``persona_media_sends`` 账本（仅注册相册）覆盖全。
    ``kind``＝image 看 image/photo/video，voice 看 voice/audio。取数异常 → False
    （宁可多拦一次「刚发了」——紧跟着会先尝试真发一张，拦错代价只是多送一张图）。
    """
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is None:
            return False
        _now = float(now if now is not None else time.time())
        _types = (("voice", "audio") if kind == "voice"
                  else ("image", "photo", "video"))
        _rc = _st.list_recent_messages(
            _cidf(platform, account_id, chat_key), limit=24) or []
        for _m in _rc:
            if str(_m.get("direction") or "in") != "out":
                continue
            if str(_m.get("media_type") or "") not in _types:
                continue
            try:
                _mts = float(_m.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            if _mts > 0 and (_now - _mts) <= max(0.0, float(window_sec)):
                return True
    except Exception:
        return False
    return False


# ── #259 P-3 媒体承诺执行型：识别 → 动作（B 线投递层）────────────────────────
# 6SRA2B 实录三轮：14:49:20 真发一张 → 14:49:51 第二句照片配文体放行但无图 →
# 客户「Where? I didn't get the other picture」被认出 lie_caught 却只给 hint →
# 14:50 / 14:51 两句传输借口谎话。守卫「看见了却没拦」的根因是识别与动作脱节：
# ① 模型的 [PHOTO] 请求失败后正文仍按「图已发」写；② 第二句配文体不在词表；
# ③ 已发假声明对照 30 分钟账本被判「真话」（2 分钟前真发过一张）；④ lie_caught
# 只注 hint 让模型自己编。下面四个函数把四处接成固定动作。

def _conv_rows(assistant, platform: str, account_id: str, chat_key: str,
               limit: int = 12) -> List[Dict[str, Any]]:
    """该会话最近消息（inbox 镜像，按时间正序；取不到 → []）。"""
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is None:
            return []
        return list(_st.list_recent_messages(
            _cidf(platform, account_id, chat_key), limit=limit) or [])
    except Exception:
        return []


def _media_pending_state(rows: List[Dict[str, Any]], *, now: Optional[float] = None,
                         window_sec: float = 24 * 3600.0) -> Dict[str, Any]:
    """客户是否在**等一张图**：窗内 AI 出站含承诺/断言/offer（``promised_ts``）或
    真发过媒体（``media_ts``）。C 段据此决定「重发上一张」还是「强制实话」，
    也是 lie_caught 弱词（Where? / 哪呢）的采信闸。"""
    from src.ai.outbound_promise_guard import (
        detect_media_offer, detect_photo_caption_claim,
    )
    _now = float(now if now is not None else time.time())
    out = {"promised_ts": 0.0, "media_ts": 0.0}
    for m in rows or []:
        if str(m.get("direction") or "in") != "out":
            continue
        try:
            ts = float(m.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0 or (_now - ts) > window_sec:
            continue
        if str(m.get("media_type") or "") in ("image", "photo", "video"):
            out["media_ts"] = max(out["media_ts"], ts)
            continue
        txt = str(m.get("text") or "")
        if txt and (detect_photo_caption_claim(txt) or detect_media_offer(txt)):
            out["promised_ts"] = max(out["promised_ts"], ts)
    out["pending"] = bool(out["promised_ts"] or out["media_ts"])
    return out


def _tag_needs_human(assistant, platform: str, account_id: str, chat_key: str,
                     reason: str) -> bool:
    """转人工 + 「需人工」标签（与 O-1 C 客服腔守卫 / drafts.py review 同一入口）。"""
    try:
        from src.integrations.protocol_autoreply import tag_needs_human
        return bool(tag_needs_human(
            getattr(assistant, "inbox_store", None),
            {"platform": platform, "account_id": account_id, "chat_key": chat_key},
            reason=reason, source="system"))
    except Exception:
        assistant.logger.debug("[media_complaint] tag_needs_human 失败", exc_info=True)
        return False


async def _lie_caught_fixed_action(
    assistant, platform: str, account_id: str, chat_key: str, text: str,
) -> Optional[Dict[str, Any]]:
    """C 段：客户最新入站是 ``lie_caught``（没收到 / 哪呢 / no picture）→ **固定动作**，
    不再给 hint 让模型编：

    ① 本会话最近一次真发的媒体可重发（回执账本 / 相册投放账本）→ 重发同一张 +
       模板短话 → ``{"action": "resend", "delivered": True}``；
    ② 从未真发过 → 强制实话模板替换正文（禁「加载慢 / 再试 / 网络」）→
       ``{"action": "honest", "text": <模板>}``；
    ③ 连续第二次 lie_caught → 转人工 + 标签，本条不发 → ``{"action": "handoff"}``。
    不适用（最新入站不是 lie_caught / 客户并未在等图）→ None，走正常链。
    """
    from src.ai.companion_selfie import detect_media_complaint
    rows = _conv_rows(assistant, platform, account_id, chat_key, limit=16)
    ins = [m for m in rows if str(m.get("direction") or "in") == "in"]
    if not ins:
        return None
    last_in = ins[-1]
    if str(last_in.get("media_type") or "") in ("image", "photo"):
        return None
    st = _media_pending_state(rows)
    if not st["pending"]:
        return None
    kind = detect_media_complaint(str(last_in.get("text") or ""), media_pending=True)
    if kind != "lie_caught":
        return None
    from src.ai.outbound_promise_guard import (
        lie_caught_honest_line, lie_caught_resend_caption,
    )
    # ③ 复诉：上一条入站也是 lie_caught（客户连说两次没收到）→ 人来接
    prev_in = ins[-2] if len(ins) >= 2 else {}
    repeat = bool(prev_in) and detect_media_complaint(
        str(prev_in.get("text") or ""), media_pending=True) == "lie_caught"
    if repeat:
        _tag_needs_human(assistant, platform, account_id, chat_key,
                         reason="media_lie_caught_repeat")
        assistant.logger.warning(
            "[media_complaint] kind=lie_caught action=handoff media_id=%s "
            "platform=%s acct=%s text=%r", "-", platform, account_id,
            str(last_in.get("text") or "")[:60])
        return {"action": "handoff"}
    # ① 真发过 → 重发上一张（真发＝回执，caption 是模板不经 LLM）
    from src.inbox.image_autosend import resolve_last_sent_media
    ck = f"{platform}:{account_id}:{chat_key}"
    media = resolve_last_sent_media(ck) if st["media_ts"] else {}
    if media.get("path") or media.get("url"):
        cap = lie_caught_resend_caption(text)
        try:
            from src.integrations.account_orchestrator import get_orchestrator as _go
            _orch = _go(assistant.config.config or {})

            async def _coro():
                return await _orch.send_media(
                    platform, account_id, chat_key,
                    media_path=str(media.get("path") or ""),
                    media_url=str(media.get("url") or ""),
                    media_type=str(media.get("media_type") or "image"),
                    caption=cap, inbox_text=("[图片] " + cap).strip())

            _wl = getattr(assistant, "_web_loop", None)
            if _wl is not None and _wl.is_running():
                _res = await asyncio.wrap_future(
                    asyncio.run_coroutine_threadsafe(_coro(), _wl))
            else:
                _res = await _coro()
        except Exception:
            assistant.logger.debug("[media_complaint] 重发上一张异常", exc_info=True)
            _res = None
        if isinstance(_res, dict) and _res.get("delivered"):
            from src.inbox.image_autosend import note_media_receipt
            note_media_receipt(
                ck, media_id=str(media.get("media_id") or ""),
                path=str(media.get("path") or ""), url=str(media.get("url") or ""),
                media_type=str(media.get("media_type") or "image"),
                mid=str(_res.get("message_id") or ""))
            assistant.logger.info(
                "[media_complaint] kind=lie_caught action=resend media_id=%s mid=%s "
                "platform=%s acct=%s", media.get("media_id") or "-",
                _res.get("message_id") or "-", platform, account_id)
            return {"action": "resend", "delivered": True}
        assistant.logger.info(
            "[media_complaint] kind=lie_caught action=resend_failed media_id=%s → honest "
            "platform=%s acct=%s", media.get("media_id") or "-", platform, account_id)
    # ② 从未真发 / 重发失败 → 强制实话
    honest = lie_caught_honest_line(text)
    assistant.logger.info(
        "[media_complaint] kind=lie_caught action=honest media_id=%s platform=%s acct=%s",
        media.get("media_id") or "-", platform, account_id)
    return {"action": "honest", "text": honest}


async def _media_promise_action(
    assistant, text: str, kind: str, *, media_context: bool = False,
    sent_claim: bool = False, photos_disabled: bool = False,
    photo_request_failed: bool = False,
) -> Tuple[str, str]:
    """B 段：承诺 / 断言句本轮无已回执 send_media → ``strip → rewrite → review``。

    返回 ``(text, action)``：``strip``＝正则剥句后仍有干净正文；``rewrite``＝剥空后
    LLM 重写一次且不再命中；``review``＝仍命中 / 无 LLM——调用方转人审，本条不发。
    ``photos_disabled``（D 段）＝人设无发图能力 / 无图可发：剥空不走「卖关子」，
    直接换**诚实拒绝**模板（action 仍记 strip，模板已过检测器）。
    ``photo_request_failed``（A 段）＝模型的 send_photo/[PHOTO] 请求没能真发：正文
    整体是按「图已发」写的配文，词表未必认得出 → 先把「无图可发」回喂模型重生一句
    不含照片措辞的回复；LLM 不可用 → 强制媒体语境剥句 → 诚实模板。
    """
    from src.ai.outbound_promise_guard import (
        build_photo_unsent_rewrite_instruction, build_promise_rewrite_instruction,
        deflection_line, detect_media_claim, detect_media_promise,
        detect_sent_claim, honest_no_photo_line, strip_media_claims,
        strip_media_promises, strip_sent_claims,
    )
    if photo_request_failed:
        media_context = True

    def _clean(t: str) -> bool:
        return bool(t.strip()) and not (
            detect_media_promise(t)
            or detect_media_claim(t, media_context=media_context)
            or ((sent_claim or media_context) and detect_sent_claim(t)))

    if photo_request_failed:
        _ai0 = getattr(assistant, "ai_client", None)
        if _ai0 is not None:
            try:
                out0 = str(await _ai0.chat(
                    build_photo_unsent_rewrite_instruction(text)) or "")
                out0 = out0.strip().strip('"“”「」').strip()
                if (out0 and len(out0) <= max(200, len(str(text or "")) * 3)
                        and _clean(out0)):
                    return out0, "rewrite"
            except Exception:
                assistant.logger.debug(
                    "[media-bind] 无图回喂重生失败，回落剥句", exc_info=True)
        s0 = strip_sent_claims(strip_media_claims(
            strip_media_promises(text), media_context=True))
        if s0 != str(text or "") and _clean(s0):
            return s0, "strip"
        return honest_no_photo_line(text), "strip"

    stripped = strip_media_promises(text)
    stripped = strip_media_claims(stripped, media_context=media_context)
    if sent_claim or media_context:
        stripped = strip_sent_claims(stripped)
    if _clean(stripped):
        return stripped, "strip"
    if photos_disabled and kind == "image":
        return honest_no_photo_line(text), "strip"
    if sent_claim:
        # 客户刚说「没收到」：剥空后如实说「没发出去」，不卖关子（#171 口径）
        return deflection_line(text, kind, sent_claim=True), "strip"
    _ai = getattr(assistant, "ai_client", None)
    if _ai is not None:
        try:
            out = str(await _ai.chat(build_promise_rewrite_instruction(
                text, kind, sent_claim=sent_claim)) or "")
            out = out.strip().strip('"“”「」').strip()
            if (out and len(out) <= max(200, len(str(text or "")) * 3)
                    and _clean(out)):
                return out, "rewrite"
        except Exception:
            assistant.logger.debug(
                "[media_promise] LLM 重写失败", exc_info=True)
    return "", "review"


async def autosend_bazi_kline(assistant, platform, account_id, chat_key, text) -> bool:
    """全自动「人生 K 线」出图（gated，companion.bazi.enabled+kline；独立于 selfie 开关）：
    客户最近一条入站在求运势曲线图时渲染 PNG 经 orch.send_media 发出。返回
    True=已作为图发出（跳过语音/文本）；False=未发→回落正常草稿流。
    一处生效全平台（telegram/whatsapp/messenger/line/ig）。"""
    _cfg = assistant.config.config or {}
    from src.fatex.config import fatex_cfg as _fx_cfg
    _bcfg = _fx_cfg(_cfg)  # FateX 合并视图（fatex.* 优先，companion.bazi 兼容）
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

    # 生辰解析回调（P0-2 根治，2026-07-25）：走 FateX 作用域解析器——
    # ① FateX 独立库（账号隔离结构化行）→ ② 账号作用域记忆键 → ③ 存量裸键。
    # 旧实现漏传 account_id → 键与写入侧失配，客户给过生辰仍被反复追问。
    def _resolve_birth():
        _sm = getattr(assistant, "skill_manager", None)
        if _sm is None:
            return None
        try:
            if hasattr(_sm, "resolve_birth_info_scoped"):
                return _sm.resolve_birth_info_scoped(
                    platform, account_id, str(chat_key))
            _key = _sm._episodic_storage_key(
                str(chat_key), "", platform, account_id=account_id)
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


async def autosend_song(assistant, platform, account_id, chat_key, text="") -> bool:
    """全自动「清唱」（gated，``companion.singing.enabled`` 默认关，2026-08-22 P0）：
    客户最近一条入站在点名要听歌时，直接发**预渲染**的人设清唱段（hub 唱歌工作室
    ``dry_vocal`` 产物，夜间 ``scripts/song_prerender.py`` 备货），经
    orch.send_media 以语音消息发出。返回 True=已发出（本轮草稿被替代，语义同
    autosend_image）；False=未发 → 回落图/语音/文本正常链。

    刻意不做（P0，改前先读 song_stock 模块 docstring）：
    - 运行时现场合成（176 白天 VRAM 高水位 + 合成 30s+；无备货就诚实回落）；
    - 无备货时的冒充（说话声念歌词＝本能力要修的实录穿帮，绝不回退到它）；
    - 点名曲目翻唱（版权红线；词表只认「要你唱」不认「唱某某的歌」）。
    """
    _cfg = assistant.config.config or {}
    from src.companion.song_stock import (
        SONG_STICKY_SEC, day_start_ts, detect_song_request, find_stock_file,
        get_song_ledger, get_song_stats, load_song_manifest, pick_song,
        requested_song_scene, resolve_singing_cfg, song_gate_verdict,
        song_sticky_from_texts, song_topic_state, stock_root, templates_dir,
    )
    _scfg = resolve_singing_cfg(_cfg)
    if not _scfg.get("enabled", False):
        return False
    # 反双发：仅编排器管理且支持发媒体的账号（与自拍/语音/K线同口径）。
    from src.integrations.account_orchestrator import (
        get_orchestrator as _go,
    )
    _orch = _go(_cfg)
    if not _orch.owns_media(platform, account_id):
        return False
    # 客户最近一条入站文本（判「要歌」意图；意图判定看入站，不看草稿文本）
    # + 粘性窗内的既往入站（实施66 P0-2：「不行，必须唱」追加逼唱补判）。
    _peer_text = ""
    _prior_in: list = []
    try:
        from src.inbox.normalizer import conv_id as _cidf
        _st = getattr(assistant, "inbox_store", None)
        if _st is None:
            return False
        _cid = _cidf(platform, account_id, chat_key)
        _recent = _st.list_recent_messages(_cid, limit=8) or []
        import time as _t_sw
        _now_sw = _t_sw.time()
        for _m in reversed(_recent):
            if str(_m.get("direction") or "in") != "in":
                continue
            _txt = str(_m.get("text") or "")
            if not _txt:
                continue
            if not _peer_text:
                _peer_text = _txt
                continue
            _ts_sw = float(_m.get("ts") or 0)
            if _ts_sw and (_now_sw - _ts_sw) > SONG_STICKY_SEC:
                continue
            _prior_in.append(_txt)
    except Exception:
        return False
    if not _peer_text:
        return False
    _state = song_topic_state(
        _peer_text, sticky=song_sticky_from_texts(_prior_in))
    if _state != "demand":
        return False
    if not detect_song_request(_peer_text):
        get_song_stats().bump("sticky_demand")
    # 被动逼唱压力（P0-4）：窗内 strict 次数 + 本条 → ≥2 豁免冷却（日帽仍在）
    _pressure = 1 + sum(1 for _t in _prior_in if detect_song_request(_t))
    # 定制求歌让位（实施58 P2）：对方要的是「写/唱一首关于我们的」定制歌，
    # 拿现货顶上=答非所问——交回文本链（draft intake 已建单+可承诺 hint），
    # 绝不用现货冒充定制。custom 未开闸时维持旧行为（现货能唱就唱）。
    try:
        from src.companion.song_orders import (
            detect_custom_song_request as _dcsr2,
            resolve_custom_cfg as _rcc2,
        )
        if _rcc2(_cfg).get("enabled") and _dcsr2(_peer_text):
            get_song_stats().bump("custom_yield")
            return False
    except Exception:
        pass
    _stats = get_song_stats()
    _stats.bump("requests")
    # 生效人设（含会话覆写），与图/语音同口径解析。
    from src.ai.persona_voice import (
        resolve_effective_persona_id as _repi,
        resolve_effective_voice_context as _revc,
    )
    _pid = _repi(_cfg, platform, account_id, str(chat_key))
    _real_pid = _pid
    try:
        _ctx0 = _revc(
            _cfg, persona_id=_pid or None, account_persona_id=_pid or None,
            chat_key=str(chat_key), contact_key=str(chat_key),
            platform=platform, account_id=account_id)
        _real_pid = str(_ctx0.get("persona_id") or _pid or "")
    except Exception:
        _real_pid = _pid
    if not _real_pid:
        _stats.bump("no_persona")
        return False
    # 频控（日上限 + 冷却；按会话记账）。
    import time as _time
    _now = _time.time()
    _ledger = get_song_ledger()
    _ok, _why = song_gate_verdict(
        _scfg,
        today_count=_ledger.count_since(_cid, day_start_ts(_now)),
        last_ts=_ledger.last_ts(_cid), now=_now,
        demand_pressure=_pressure)
    if not _ok:
        _stats.bump(_why)
        return False
    if _why == "pressure_exempt":
        _stats.bump("pressure_exempt")
    # 选曲：只在**有备货**的模板里挑（部分渲染态不误选）；语言粗判（han 占比）
    # + 场景提示（生日/晚安）+ 重复窗避重；全被排除 → 宁可不唱。
    _templates = load_song_manifest(templates_dir(_scfg))
    if not _templates:
        _stats.bump("no_template")
        return False
    _sroot = stock_root(_scfg)
    _stocked = [t for t in _templates
                if find_stock_file(_real_pid, t.id, sroot=_sroot)]
    if not _stocked:
        _stats.bump("no_stock")
        return False
    _lang = "zh"
    try:
        _han = sum(1 for ch in _peer_text if "\u4e00" <= ch <= "\u9fff")
        _alpha = sum(1 for ch in _peer_text if ch.isascii() and ch.isalpha())
        if _han == 0 and _alpha >= 4:
            _lang = "en"
    except Exception:
        _lang = "zh"
    _tmpl = pick_song(
        _stocked, lang=_lang,
        scene_hint=requested_song_scene(_peer_text),
        exclude_ids=_ledger.recent_template_ids(
            _cid, float(_scfg.get("repeat_window_days", 7) or 7), now=_now),
        variety_key=_cid,
        day_key=_time.strftime("%Y%m%d", _time.localtime(_now)),
        allow_lang_fallback=bool(_scfg.get("allow_lang_fallback", False)))
    if _tmpl is None:
        _stats.bump("no_pick")
        return False
    _spath = find_stock_file(_real_pid, _tmpl.id, sroot=_sroot)
    if _spath is None:
        _stats.bump("no_stock")
        return False
    try:
        _data = _spath.read_bytes()
    except Exception:
        assistant.logger.debug("[autosend song] 备货读取失败 %s", _spath,
                               exc_info=True)
        _stats.bump("send_failed")
        return False
    # 落出站媒体目录拿 /static URL（坐席工作台可回听），以语音消息发出。
    from src.integrations.protocol_bridge import save_outbound_media
    try:
        _local, _url, _ = save_outbound_media(
            platform, account_id, f"song_{_tmpl.id}{_spath.suffix}", _data)
    except Exception:
        assistant.logger.debug("[autosend song] 出站媒体落盘失败", exc_info=True)
        _stats.bump("send_failed")
        return False
    _first_line = next((ln.strip() for ln in str(_tmpl.lyrics or "").splitlines()
                        if ln.strip()), "")
    _inbox = f"[唱歌]《{_tmpl.title}》" + (f" ♪ {_first_line[:40]}" if _first_line else "")
    # 止损话术（实施58 P3-1）：唱歌嗓≠说话嗓的柔性铺垫配文，音色修复后可关
    from src.companion.song_stock import framing_caption as _fcap
    _caption = _fcap(_scfg, conv_id=_cid)

    async def _coro():
        return await _orch.send_media(
            platform, account_id, chat_key,
            media_path=_local, media_url=_url,
            media_type="voice", caption=_caption, inbox_text=_inbox)
    try:
        _wl = getattr(assistant, "_web_loop", None)
        if _wl is not None and _wl.is_running():
            _f = asyncio.run_coroutine_threadsafe(_coro(), _wl)
            _res = await asyncio.wrap_future(_f)
        else:
            _res = await _coro()
        _sent = bool(isinstance(_res, dict) and _res.get("delivered"))
    except Exception:
        assistant.logger.debug("[autosend song] 发送异常", exc_info=True)
        _sent = False
    if not _sent:
        _stats.bump("send_failed")
        return False
    _ledger.record(_cid, _tmpl.id, now=_now)
    _stats.note_sent(_tmpl.id, _real_pid)
    # 已发媒体日志合流（与图链 _on_sent 同口径）：草稿链「你最近发过的照片/媒体」
    # 事实块 + 防「唱完失忆」（客户追问「刚唱的什么」）。skill_manager 缺席静默跳过。
    try:
        _sm_ref = getattr(assistant, "skill_manager", None)
        if _sm_ref is not None:
            _uc = _sm_ref._get_user_context(str(chat_key))
            _sm_ref._record_media_sent(
                _uc, note=f"[唱歌]《{_tmpl.title}》", scene="",
                series=f"song:{_tmpl.id}")
            _sm_ref._context_store.mark_dirty(str(chat_key))
            _sm_ref._context_store.flush(str(chat_key))
    except Exception:
        assistant.logger.debug("[autosend song] 媒体日志写入失败（忽略）",
                               exc_info=True)
    assistant.logger.info(
        "[autosend song] 唱段已发 persona=%s tmpl=%s conv=%s file=%s",
        _real_pid, _tmpl.id, _cid, _spath.name)
    return True


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


def _guard_translated_lang_mix(assistant, src_text: str, out_text):
    """译文出口再过一次混语守卫（B125，2026-08-28）。

    B121 的 ``outbound_text_guard`` 挂在**出稿口**（skill_manager 的 A/B 两线），
    而出站翻译发生在那之后——译文自此再没有任何语种检查。于是 MT 把源文里的
    代词/短词漏译留在英文句里（实录「You know I'm here, same 我」「im 我」），
    整条直发客户。守卫本身认得这个形状（latin≥6 且 ≥3×CJK ⇒ hard），只是从来
    没被喂到译文。

    只跑 ``lang_mix`` + ``system_label``：旁白与无出处引用是**生成端**语义，已在
    出稿口处置完毕，拿 MT 产物重跑既无意义又会把命中数记重。系统标签例外——
    2026-09-12 事故里 MT 把「[我方语音消息]」译成「[Voice message from our side]」，
    出稿口剥过中文、译文又把标签加回来。任何异常/守卫判空一律返回译文原值——
    翻译链的 HOLD 语义（None）必须原样透传，绝不能被守卫改写成放行。
    """
    if not isinstance(out_text, str) or not out_text.strip():
        return out_text
    try:
        from src.ai.outbound_text_guard import (
            apply_outbound_text_guard as _guard,
            resolve_cfg as _guard_cfg,
        )
        _c = _guard_cfg(assistant.config.config or {})
        _do_mix = bool(_c.get("lang_mix", True))
        _do_lab = bool(_c.get("system_label", True))
        if not (_c.get("enabled", True) and (_do_mix or _do_lab)):
            return out_text
        _cleaned, _meta = _guard(
            out_text,
            {"enabled": True, "monologue": False, "lang_mix": _do_mix,
             "unfounded_recall": False, "system_label": _do_lab})
        if _cleaned != out_text:
            if _meta.get("system_label_hits"):
                assistant.logger.warning(
                    "[autosend] 译文系统标签已剥除 %r：%r → %r（源文 %r）",
                    [h[:40] for h in _meta["system_label_hits"][:3]],
                    out_text, _cleaned, src_text)
            else:
                assistant.logger.warning(
                    "[autosend] 译文混语已剥除（%s）：%r → %r（源文 %r）",
                    _meta.get("lang_mix") or "hard", out_text, _cleaned, src_text)
            return _cleaned
        if _meta.get("lang_mix") == "hard_kept":
            # 剥后残句会更糟（守卫的安全阀），如实留痕便于回看 MT 质量
            assistant.logger.warning(
                "[autosend] 译文混语命中但剥除不安全，原样投递：%r", out_text)
    except Exception:
        assistant.logger.debug("译文混语守卫跳过", exc_info=True)
    return out_text


def build_autosend_translate_cb(assistant, web_app):
    """构造 AutosendWorker 出站翻译/语言硬闸回调（投递前的语言正确性最后防线）。

    两种模式（P1-198，2026-08-03）：
      - ``translate.enabled=true``  → 常规出站翻译（旧行为）：译成客户语言 +
        CJK 冲突 HOLD 护栏；
      - ``translate.enabled=false`` 而 ``lang_gate.enabled``（默认开）→
        **gate-only 语言硬闸**：常规消息原样放行（尊重运营关闭翻译的决定），
        仅 CJK↔非 CJK 冲突时抢救翻译 / HOLD——修「翻译一关、7/31 的 CJK
        护栏一起消失，错语言草稿裸奔」的防护空窗。
    两者都关才返回 None。translation_service 懒取（worker 真正投递时早已挂到
    web_app.state）。回调带 ``gate_only`` 属性供 status 快照如实上报模式。
    """
    try:
        from src.inbox.outbound_translate import (
            parse_outbound_lang_gate_cfg as _parse_gate_cfg,
            parse_outbound_translate_cfg as _parse_otx_cfg,
        )
        _cfg_root = assistant.config.config or {}
        _otx_cfg = _parse_otx_cfg(_cfg_root)
        _gate_cfg = _parse_gate_cfg(_cfg_root)
        if not _otx_cfg.get("enabled") and not _gate_cfg.get("enabled"):
            return None
        _gate_only = not _otx_cfg.get("enabled")
        _otx_src = _otx_cfg.get("source_lang") or "zh"
        _otx_style = _otx_cfg.get("style") or "chat"

        async def _redraft_in_lang(item, target):
            """Q-39 B（#326）：翻译三步都空 → 按 lang-plan 目标语让人设链**重起草一次**
            （generate_persona_reply(reply_lang=target)，全套人设 / 守卫同门）。任何异常回空串。"""
            try:
                from src.inbox.normalizer import conv_id as _cidf_r
                from src.inbox.persona_reply import (
                    generate_persona_reply as _gpr, normalize_history as _nh,
                )
                _st_r = getattr(assistant, "inbox_store", None)
                if _st_r is None:
                    return ""
                _plat = str(item.get("platform") or "")
                _acct = str(item.get("account_id") or "default")
                _ck = str(item.get("chat_key") or "")
                _cid_r = str(item.get("conversation_id") or "") or _cidf_r(_plat, _acct, _ck)
                _rows = _st_r.list_recent_messages(_cid_r, limit=20) or []
                _hist, _last_in = _nh(_rows)
                if not str(_last_in or "").strip():
                    return ""
                _res = await _gpr(
                    app=web_app, platform=_plat, chat_key=_ck, last_inbound=_last_in,
                    history=_hist, reply_lang=str(target or ""), conversation_id=_cid_r,
                    account_id=_acct)
                if not (isinstance(_res, dict) and _res.get("ok")):
                    return ""
                return str(_res.get("reply") or "").strip()
            except Exception:
                assistant.logger.debug("[xlate] redraft 失败（忽略）", exc_info=True)
                return ""

        async def _autosend_translate(
            item, _src=_otx_src, _style=_otx_style, _go=_gate_only,
        ):
            from src.inbox.outbound_translate import (
                translate_outbound_text as _tot,
            )
            _ts = getattr(web_app.state, "translation_service", None)
            if _ts is None:
                return str(item.get("text", ""))
            # M-1 B #234（D-M3）：目标语言五级决策需要「客户档案语言」（contacts 店）与
            # 「人设对外默认语言」（实时配置 → 会话绑定人设）两级的数据源；缺席即该级跳过。
            try:
                _cstore = getattr(getattr(assistant, "contacts", None), "store", None)
            except Exception:
                _cstore = None
            try:
                _cfg_root = assistant.config.config or {}
            except Exception:
                _cfg_root = {}
            _out = await _tot(
                item, translation_service=_ts,
                store=assistant.inbox_store,
                source_lang=_src, style=_style, gate_only=_go,
                contacts_store=_cstore, cfg_root=_cfg_root,
                redraft=_redraft_in_lang)   # Q-39 B：三步重试后按目标语重起草
            return _guard_translated_lang_mix(
                assistant, str(item.get("text", "")), _out)

        _autosend_translate.gate_only = _gate_only
        if _gate_only:
            assistant.logger.info(
                "AutosendWorker 语言硬闸已启用（gate-only：常规翻译关闭，仅拦 CJK 冲突）")
        else:
            assistant.logger.info(
                "AutosendWorker 出站自动翻译已启用（src=%s）", _otx_src)
        return _autosend_translate
    except Exception:
        assistant.logger.debug("出站自动翻译装配跳过", exc_info=True)
        return None


def build_autosend_mark_read_cb(assistant, *, always: bool = False):
    """构造 AutosendWorker 投递前「已读回执」回调（拟人「先看后回」）。

    真人一定是先看到消息（对端出现已读）、想一会儿、再回——此前全自动直接投递，
    客户端上「消息还是未读却收到了回复」是最扎眼的机器人破绽。回调经编排器
    ``orch.mark_read`` 分发到受管 worker（TG 协议号 pyrogram read_chat_history、
    WA Baileys ``/read``、LINE ``sendChatChecked``；Messenger web worker 暂不支持
    → 静默 False；RPA 设备号由 RPA 打开会话时天然已读，不经此路径）。

    ⚠ 时机很要紧：本回调在**投递前一刻**调用，所以是「已读→紧接着回复」。LINE 文化里
    「已讀不回」格外扎人，若哪天把它挪到入站阶段就会造出那个效果。

    ``inbox.l2_autosend.mark_read_before_reply``（默认 true）置 false 可关闭。
    ``always=True`` 跳过该构造期开关（P1 2026-08-02）：AutosendWorker 在 bootstrap
    只装配一次，构造期 return None 会把开关**冻结到重启**；worker 现自持运行时开关
    （``apply_humanize_flags`` 可热更），故它要的是「回调永远在、开关在 worker 手里」。
    其余按调用重建的消费方（reply_bubbles 逐条投递现建现用）保持默认 False——
    它们的构造期检查每次投递都会重跑，本就等价于热读。
    与发送同口径：编排器 client 活在 web 线程 loop 上 → 跨线程调度执行。
    """
    _cfg = assistant.config.config or {}
    _as_cfg = ((_cfg.get("inbox") or {}).get("l2_autosend") or {})
    if not always and not bool(_as_cfg.get("mark_read_before_reply", True)):
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


def record_text_outreach(assistant, platform, account_id, chat_key, kind,
                         note=""):
    """P2 A/B 归因：全自动文本投递落 outreach_log（batch=autosend_text:<kind>）。

    kind ∈ bubbles|single。与 proactive_topic:{kind} 同机制——之后经
    ``outreach_response_stats`` 读「投递后 N 天内对方是否回话」，autosend-status
    的 ``bubbles_ab`` 段直读对比。best-effort：任何异常不影响投递结果。
    """
    try:
        st = getattr(assistant, "inbox_store", None)
        if st is None or not hasattr(st, "record_outreach"):
            return
        from src.inbox.normalizer import conv_id
        st.record_outreach(
            conv_id(platform, account_id, str(chat_key)),
            batch_id=f"autosend_text:{kind}",
            platform=platform, account_id=account_id,
            status="sent", note=str(note or ""),
        )
    except Exception:
        pass


def build_autosend_typing_cb(assistant, *, always: bool = False):
    """构造 AutosendWorker 投递延迟期「正在输入」状态回调（拟人打字气泡）。

    真人回复前对端会看到「对方正在输入…」。此前全自动在打字延迟(3-12s)期间无任何提示，
    延迟结束消息突然出现，仍显机械。回调经编排器 ``orch.send_chat_action`` 分发到受管
    worker（当前 Telegram 协议号 pyrogram send_chat_action；其余 worker 暂不支持 → 静默）。

    ``inbox.l2_autosend.typing_indicator``（默认 true）置 false 可关闭。
    ``always=True`` 语义同 ``build_autosend_mark_read_cb``：给一次性装配的
    AutosendWorker 用（开关由 worker 运行时自持、可热更），逐次重建的消费方
    （reply_bubbles）保持默认。与发送同口径：编排器 client 活在 web 线程 loop 上
    → 跨线程调度执行。
    """
    _cfg = assistant.config.config or {}
    _as_cfg = ((_cfg.get("inbox") or {}).get("l2_autosend") or {})
    if not always and not bool(_as_cfg.get("typing_indicator", True)):
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


def build_autosend_callbacks(assistant, web_app, deliver_enabled, *,
                             origin: str = "auto"):
    """构造 AutosendWorker 的 (send_callback, translate_callback)。

    从 main.py initialize() 原样抽出（行为不变）。deliver 编排「按需发图→语音→
    文本/桌面受控出站」三级投递；deliver_enabled=False 时 send_cb=None（仅 DB
    标记+审计，不发客户）。translate_cb 见 build_autosend_translate_cb。

    ``origin``（P1 2026-08-12 人工预留额度）：人工通过草稿的投递链传 ``manual``
    （坐席明示决定，额度与手动发送端点同待遇——用满 recommended_cap）；
    缺省 ``auto``＝L2 自动链在 ``cap - reserve_for_manual`` 让路。只影响文本
    主路的额度道；图/语音子链维持 auto 口径（保守：宁少发不超发）。
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
        _send_origin = str(origin or "auto")

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
            # 清唱短路（gated，companion.singing 默认关，2026-08-22）：客户点名
            # 要听歌 → 直发预渲染人设唱段（零现场合成）；无备货/频控中 → 继续
            # 回落图/语音/文本——绝不用说话声念歌词冒充唱（实录穿帮，勿回退）。
            try:
                if await autosend_song(
                    _assistant_ref, platform, account_id, chat_key, text
                ):
                    return {"ok": True, "delivered_as": "voice"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend song] 失败，回落图/语音/文本", exc_info=True)
            # #259 P-3 C 段：客户最新入站在说「没收到」（lie_caught）→ 固定动作
            # （重发上一张 / 强制实话 / 复诉转人工），不再让模型带 hint 自己编——
            # 6SRA2B 14:50「maybe it took a moment to load」/ 14:51「let me try
            # sending it again」两句谎话都是 hint 式纠偏下编出来的。守卫总闸同
            # media_promise_guard.enabled；lie_caught_action=false 可单独回旧行为。
            _pg0 = {}
            _lie_honest = False
            try:
                _pg0 = (((_assistant_ref.config.config or {}).get(
                    "companion", {}) or {}).get("media_promise_guard", {}) or {})
                if _pg0.get("enabled", True) and _pg0.get("lie_caught_action", True):
                    _lie = await _lie_caught_fixed_action(
                        _assistant_ref, platform, account_id, chat_key, text)
                    if _lie and _lie.get("action") == "resend":
                        return {"ok": True, "delivered_as": "image"}
                    if _lie and _lie.get("action") == "handoff":
                        return {"ok": True,
                                "delivered_as": "suppressed_media_complaint_handoff"}
                    if _lie and _lie.get("action") == "honest":
                        text = str(_lie.get("text") or "")
                        original_text = None
                        _photo_directive = None
                        _lie_honest = True
            except Exception:
                _assistant_ref.logger.debug(
                    "[media_complaint] lie_caught 固定动作异常（放行正常链）", exc_info=True)
            # 全自动「按需发图」优先（gated）：LLM 指令直通生成，或对方在要照片时
            # 关键词链出图；成功即作为图片发出、跳过语音/文本；失败 → 继续。
            # #259 P-3 A 段（文字绑动作）：模型的 [PHOTO] 是**请求**不是事实——
            # 真发（回执）成功配文才随图出；请求失败则正文里「刚拍的 / here it is」
            # 全是空头，强制进承诺链改写（不再依赖词表能不能认出那句配文）。
            _img_sent = False
            _directive_unfulfilled = False
            try:
                if not _lie_honest and await autosend_image(
                    _assistant_ref, platform, account_id, chat_key, text,
                    directive_override=_photo_directive,
                ):
                    _img_sent = True
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend image] 失败，回落语音/文本", exc_info=True)
            if _photo_directive is not None or _img_sent:
                try:
                    from src.inbox.image_autosend import last_media_receipt as _lmr
                    _rc_mid = str((_lmr(f"{platform}:{account_id}:{chat_key}") or {}).get(
                        "mid") or "") if _img_sent else ""
                except Exception:
                    _rc_mid = ""
                _assistant_ref.logger.info(
                    "[media-bind] conv=%s:%s:%s requested=%d sent=%d mid=%s caption_allowed=%d",
                    platform, account_id, chat_key,
                    1 if _photo_directive is not None else 0, 1 if _img_sent else 0,
                    _rc_mid or "-", 1 if _img_sent else 0)
            if _img_sent:
                return {"ok": True, "delivered_as": "image"}
            if _photo_directive is not None:
                _directive_unfulfilled = True
            # 出站媒体承诺守卫（companion.media_promise_guard，默认开）：
            # 发图判定看的是**客户入站文本**，而 LLM 草稿却可能自己承诺「等我拍/
            # 发你一张」——两边从不核对（实录事故：客户质问"你快拍啊是不是骗我"）。
            # 走到这=正常图链没发出：文本若承诺了照片 → 先尝试**兑现**
            # （assume_intent 强制自拍链，预算/关系闸门照常）；仍发不出 → **撤回**
            # （重写/剥离），文本、语音（念的就是这段文本）都不再对客户撒谎。
            _promised = ""
            _mctx = False
            _sent_claim = False
            _bind_reason = ""
            _q39_img_no_ask = False   # Q-39 A①（#327）：客户刚发了图且没要图 → 承诺只撤回不兑现
            _pg = {}
            try:
                _pg = (((_assistant_ref.config.config or {}).get(
                    "companion", {}) or {}).get(
                    "media_promise_guard", {}) or {})
                if _pg.get("enabled", True):
                    from src.ai.outbound_promise_guard import (
                        detect_media_claim as _dmc,
                        detect_media_promise as _dmp,
                        wants_media as _wm,
                    )
                    # media_context：客户最近入站在**索要**媒体 → 才把「已发」断言
                    # 当谎判。粘性（扫最近几条入站，非仅当轮——「快点发来睇下」这类
                    # 无名词追问单看当轮会漏，话题从几轮前就是要图）；但**客户刚发了
                    # 图**时抑制（那轮 AI 多半在评论对方的图，「这张真好看」不能误剥）。
                    try:
                        from src.inbox.normalizer import conv_id as _cidf2
                        _st2 = getattr(_assistant_ref, "inbox_store", None)
                        if _st2 is not None:
                            _rc = _st2.list_recent_messages(
                                _cidf2(platform, account_id, chat_key),
                                limit=6) or []
                            _ins = [m for m in _rc
                                    if str(m.get("direction") or "in") == "in"]
                            _last_in = _ins[-1] if _ins else {}
                            _last_is_img = str(
                                _last_in.get("media_type") or "") in (
                                "image", "photo")
                            # Q-39 A①（#327 9PYWPG）：入站是图（media_type / 识图行 /
                            # 占位）且客户配文无索图 → 兑现链不放行，LLM 的「分享旧照」
                            # 承诺交下方 _media_promise_action 撤回改写。
                            try:
                                from src.inbox.image_send_gate import (
                                    compute_image_intent as _q39_gate,
                                )
                                _q39_g = _q39_gate(
                                    str(_last_in.get("text") or ""), None,
                                    inbound_media_type=str(
                                        _last_in.get("media_type") or ""),
                                    assume_intent="selfie")
                                _q39_img_no_ask = bool(
                                    _q39_g.inbound_image and not _q39_g.intent)
                            except Exception:
                                _q39_img_no_ask = False
                            # 悬置语义（实施69）：窗内**客户要过媒体**或 **AI 自己
                            # 承诺/offer 过发图**（词表已补「给你拍一张X」语序）都算
                            # 语境——AI 先开的空头支票同样让客户进入等待态；但其后
                            # 已有媒体真发出 → 语境闭合（「刚发的那张」是真话，
                            # 不能剥）。旧版只扫入站且无闭合判定：请求已被兑现后
                            # 门还开着，真话有被误剥的暴露面，一并修掉。
                            from src.ai.outbound_promise_guard import (
                                detect_media_offer as _dmo69,
                            )
                            _req_ts = 0.0
                            _media_ts = 0.0
                            for _m in _rc:
                                _mts = float(_m.get("ts") or 0)
                                _mtxt = str(_m.get("text") or "")
                                if str(_m.get("direction") or "in") == "in":
                                    if _wm(_mtxt):
                                        _req_ts = max(_req_ts, _mts)
                                else:
                                    if _dmp(_mtxt) or _dmo69(_mtxt):
                                        _req_ts = max(_req_ts, _mts)
                                    if str(_m.get("media_type") or "") in (
                                            "image", "photo", "video"):
                                        _media_ts = max(_media_ts, _mts)
                            _mctx = ((not _last_is_img) and _req_ts > 0
                                     and _media_ts < _req_ts)
                    except Exception:
                        _mctx = False
                    _promised = (_dmp(str(original_text or ""))
                                 or _dmp(str(text or "")))
                    # 「将发」承诺 + 「已发」断言合流（claim 也走兑现优先→撤回兜底）
                    if not _promised:
                        _promised = (_dmc(str(original_text or ""),
                                          media_context=_mctx)
                                     or _dmc(str(text or ""),
                                             media_context=_mctx))
                    # #171 过去时假声明（「I just sent it, you should have it
                    # now」「let me try sending it again」）：不吃 media_context
                    # （实录里门全程没开），真伪对照近窗媒体真发镜像——近
                    # window_min 内真发过＝真话放行；没发＝谎 → 同走兑现优先
                    # →撤回兜底，只是撤回口径改「这边没发出去、稍后补」。
                    if not _promised:
                        _scg = _pg.get("sent_claim", {}) or {}
                        if not isinstance(_scg, dict):
                            _scg = {}
                        if _scg.get("enabled", True):
                            from src.ai.outbound_promise_guard import (
                                detect_sent_claim as _dsc,
                            )
                            _sck = (_dsc(str(original_text or ""))
                                    or _dsc(str(text or "")))
                            if _sck and not _media_sent_recently(
                                    _assistant_ref, platform, account_id,
                                    chat_key, kind=_sck,
                                    window_sec=float(
                                        _scg.get("window_min", 30) or 30) * 60.0):
                                _promised = _sck
                                _sent_claim = True
                                from src.inbox.image_autosend import (
                                    record_sent_claim_event as _rsce0,
                                )
                                _rsce0("detected")
                                _assistant_ref.logger.info(
                                    "[promise_guard] 出站含「已发」假声明（%s）"
                                    "且近窗无媒体真发 → 走兑现/撤回 platform=%s "
                                    "acct=%s", _sck, platform, account_id)
                    # #259 P-3 A 段：[PHOTO] 请求失败 → 正文按「图已发」写的措辞
                    # 一律视作未兑现承诺（不依赖词表能否认出那句配文）。
                    if not _promised and _directive_unfulfilled:
                        _promised = "image"
                        _bind_reason = "directive_unfulfilled"
                    # #259 P-3 E 段：同会话 5 分钟内刚真发过一张、本轮又是一句
                    # 照片配文体（「Just took this one for you—」）却没图 → 必须再
                    # 附图（下方兑现链），否则改写。wants_media 语境闸在图发出后已
                    # 闭合，这里强制媒体语境判（14:49:51 放行的根因）。
                    if not _promised and not _lie_honest:
                        try:
                            _e_raw = _pg.get("second_caption_window_sec", 300)
                            _e_win = float(300 if _e_raw is None else _e_raw)
                        except (TypeError, ValueError):
                            _e_win = 300.0
                        if _e_win > 0:
                            _e_kind = ""
                            for _t in (str(original_text or ""), str(text or "")):
                                _e_kind = _dmp(_t) or _dmc(_t, media_context=True)
                                if _e_kind:
                                    break
                            if _e_kind == "image" and _media_sent_recently(
                                    _assistant_ref, platform, account_id, chat_key,
                                    kind="image", window_sec=_e_win):
                                _promised = "image"
                                _mctx = True
                                _bind_reason = "second_caption_in_window"
                                _assistant_ref.logger.info(
                                    "[media_promise] conv=%s:%s:%s claims_send=1 "
                                    "attached=0 reason=second_caption_in_window "
                                    "window=%.0fs → 必须附图否则改写",
                                    platform, account_id, chat_key, _e_win)
            except Exception:
                _promised = ""
            if _promised == "image":
                from src.inbox.image_autosend import (
                    record_promise_event as _rpe,
                    record_sent_claim_event as _rsce,
                )
                if not _sent_claim:
                    _rpe("detected")
                # #259 P-3 D 段：人设无发图能力（capabilities.photos=false）→ 承诺句
                # 剥空时换**诚实拒绝**模板（非「卖关子」）。兑现仍照打图链（图链内部
                # 同一 SSOT 闸会拒；这里只决定改写口径，判不出人设按「关」）。
                _photos_off = False
                try:
                    from src.ai.persona_voice import (
                        resolve_effective_persona_id as _repi_p3,
                    )
                    from src.companion.photo_capability import (
                        persona_photos_enabled_by_id as _ppe_p3,
                    )
                    _pid_p3 = _repi_p3(
                        _assistant_ref.config.config or {}, platform, account_id,
                        str(chat_key))
                    _photos_off = not _ppe_p3(_pid_p3)
                except Exception:
                    _photos_off = False
                if _q39_img_no_ask:
                    # Q-39 A①：客户刚发了图、没要图——不跟发相册去「兑现」，直接走撤回改写
                    _assistant_ref.logger.info(
                        "[media_promise] conv=%s:%s:%s claims_send=1 attached=0 "
                        "action=retract reason=inbound_image_no_intent（客户发图我方不跟发）",
                        platform, account_id, chat_key)
                    _bind_reason = _bind_reason or "inbound_image_no_intent"
                if _pg.get("fulfill", True) \
                        and _bind_reason != "directive_unfulfilled" \
                        and not _q39_img_no_ask:
                    # P0 一致性：承诺句点名了场景（「拍张海边的发你」）→ 兑现
                    # 必须贴场景（相册场景类硬匹配/生成带场景），随机人像不算兑现。
                    # directive_unfulfilled：图链刚按模型请求试过一次失败，不复烧。
                    _pscene = ""
                    try:
                        from src.ai.outbound_promise_guard import (
                            promised_scene as _psc,
                        )
                        _pscene = (_psc(str(original_text or ""))
                                   or _psc(str(text or "")))
                    except Exception:
                        _pscene = ""
                    try:
                        if await autosend_image(
                            _assistant_ref, platform, account_id,
                            chat_key, text, assume_intent="selfie",
                            assume_scene=_pscene,
                        ):
                            (_rsce if _sent_claim else _rpe)("fulfilled")
                            _assistant_ref.logger.info(
                                "[promise_guard] 文本%s发图 → 已兑现为真实"
                                "图片投递 platform=%s acct=%s",
                                "「已发」假声明" if _sent_claim else "承诺",
                                platform, account_id)
                            _assistant_ref.logger.info(
                                "[media_promise] conv=%s:%s:%s claims_send=1 "
                                "attached=1 action=fulfill reason=%s",
                                platform, account_id, chat_key, _bind_reason or "-")
                            return {"ok": True, "delivered_as": "image"}
                    except Exception:
                        _assistant_ref.logger.debug(
                            "[promise_guard] 兑现发图失败", exc_info=True)
                # B 段：无已回执 send_media → strip → rewrite → review
                _new_text, _pact = await _media_promise_action(
                    _assistant_ref, text, "image", media_context=_mctx,
                    sent_claim=_sent_claim, photos_disabled=_photos_off,
                    photo_request_failed=(_bind_reason == "directive_unfulfilled"))
                (_rsce if _sent_claim else _rpe)("retracted")
                # R87 P1-3：同会话承诺未兑现跟轮记账——30 分钟内 ≥2 次 → 下一轮拟稿注入
                # 「绝不再承诺发图」硬提示（skill_manager 拟稿前读 promise_streak_hint）
                _pstreak = 0
                try:
                    from src.inbox.image_send_gate import note_promise_retracted as _npr
                    _pstreak = _npr(f"{platform}:{account_id}:{chat_key}")
                except Exception:
                    _pstreak = 0
                _assistant_ref.logger.info(
                    "[media_promise] conv=%s:%s:%s claims_send=1 attached=0 action=%s "
                    "reason=%s photos=%s sent_claim=%s streak=%d",
                    platform, account_id, chat_key, _pact, _bind_reason or "-",
                    "off" if _photos_off else "on", _sent_claim, _pstreak)
                if _pact == "review" or not str(_new_text or "").strip():
                    _tag_needs_human(
                        _assistant_ref, platform, account_id, chat_key,
                        reason="media_promise_unfulfilled")
                    return {"ok": True,
                            "delivered_as": "suppressed_media_promise_review"}
                text = _new_text
                # 原文含未兑现承诺：语音/视频分支改念撤回后的文本（防克隆声念出谎话）
                original_text = None
                _assistant_ref.logger.info(
                    "[promise_guard] 发图%s无法兑现 → 已撤回改写文本(%s) "
                    "platform=%s acct=%s",
                    "「已发」假声明" if _sent_claim else "承诺", _pact,
                    platform, account_id)
            elif _promised == "video_call":
                # A2（2026-07-22）：视频通话承诺没有兑现路径（无此能力），
                # 直接撤回改写（真机实录 AI 曾声称「WhatsApp 视频都开到㗎」，
                # 客户真拨即穿帮）。
                from src.inbox.image_autosend import (
                    record_promise_event as _rpe_vc,
                )
                _rpe_vc("detected")
                text = await _depromise_autosend_text(
                    _assistant_ref, text, "video_call", media_context=_mctx)
                original_text = None
                _rpe_vc("retracted")
                _assistant_ref.logger.info(
                    "[promise_guard] 视频通话承诺（无此能力）→ 已撤回改写文本 "
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
            # 跨草稿「语音后静默」：上一轮语音已达且客户无新话 → 整轮抑制
            # （含文本/气泡），避免孤儿二稿叠发（2026-08-04 .198）。
            try:
                from src.inbox.normalizer import conv_id as _cid_q
                from src.inbox.voice_autosend import (
                    resolve_voice_autosend_cfg as _rva_q,
                    effective_voice_block as _evb_q,
                )
                from src.inbox.voice_session_guard import (
                    resolve_voice_session_cfg as _rvsc_q,
                    should_quiet_after_voice as _sqav_q,
                )
                _vb_q = _evb_q(
                    _rva_q(_assistant_ref.config.config or {}), platform)
                _scfg_q = _rvsc_q(_vb_q)
                _cid_quiet = _cid_q(platform, account_id, chat_key)
                _st_q = getattr(_assistant_ref, "inbox_store", None)
                _recent_q = []
                if _st_q is not None:
                    _recent_q = _st_q.list_recent_messages(
                        _cid_quiet, limit=12) or []
                if _sqav_q(
                    conv_key=_cid_quiet, recent_messages=_recent_q,
                    quiet_after_sec=_scfg_q.get("quiet_after_sec", 90),
                ):
                    _assistant_ref.logger.info(
                        "[autosend] voice_quiet 抑制孤儿二稿（语音已达无新入站）"
                        " platform=%s acct=%s", platform, account_id)
                    return {
                        "ok": True,
                        "delivered_as": "suppressed_voice_quiet",
                    }
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend] voice_quiet 判定跳过", exc_info=True)
            # 文字假唱剥离（实施66 P0-3，B 线出站兜底）：唱歌 hint 是概率性
            # 防御——草稿仍打出「那我唱了+♪歌词引用」强表演体时句级剥离
            # （宣告+唱词双证据、零语境依赖防误伤；弱形态由 hint 与 A 线
            # 5c2s 守卫覆盖）。**必须在语音分支之前**：假唱文本被克隆声念
            # 出去＝「说话声念歌词」原始事故形态回魂。剥空换台阶句。
            try:
                from src.companion.song_stock import (
                    detect_song_performance as _dsp_b,
                    get_song_stats as _gss_b,
                    song_deflection_line as _sdl_b,
                    strip_song_performance as _ssp_b,
                )
                if text and _dsp_b(text):
                    _gss_b().bump("claim_blocked_b")
                    _t2 = _ssp_b(text)
                    text = _t2 if _t2.strip() else _sdl_b(
                        "zh", key=str(chat_key))
                    original_text = None  # 语音分支不得念假唱原文
                    _assistant_ref.logger.info(
                        "[song] B 线草稿含文字假唱，表演段已剥离 "
                        "platform=%s acct=%s", platform, account_id)
            except Exception:
                pass
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
                    if _sent_claim and _promised == "voice":
                        try:
                            from src.inbox.image_autosend import (
                                record_sent_claim_event as _rsce_v,
                            )
                            _rsce_v("fulfilled")
                        except Exception:
                            pass
                    return {"ok": True, "delivered_as": "voice"}
            except Exception:
                _assistant_ref.logger.debug(
                    "[autosend voice] 失败，回落文本", exc_info=True)
            # 语音承诺撤回：文本承诺「发你条语音」而上面的语音分支没发成
            # （未启用/概率闸/语言闸/合成失败）→ 文本出站前剥掉语音承诺。
            if _promised == "voice":
                from src.inbox.image_autosend import (
                    record_promise_event as _rpe2,
                    record_sent_claim_event as _rsce2,
                )
                if not _sent_claim:
                    _rpe2("detected")
                text = await _depromise_autosend_text(
                    _assistant_ref, text, "voice", media_context=_mctx,
                    sent_claim=_sent_claim)
                (_rsce2 if _sent_claim else _rpe2)("retracted")
                _assistant_ref.logger.info(
                    "[promise_guard] 发语音%s未兑现 → 已撤回改写文本 "
                    "platform=%s acct=%s",
                    "「已发」假声明" if _sent_claim else "承诺",
                    platform, account_id)
            # 时空接轨（B 线）：当地墙钟 vs 时段问候/错城现居
            try:
                _cfg_w = _assistant_ref.config.config or {}
                _wcfg = ((_cfg_w.get("companion") or {}).get(
                    "world_clock_guard") or {})
                if not (isinstance(_wcfg, dict)
                        and _wcfg.get("enabled") is False):
                    from src.companion.world_clock_guard import (
                        apply_world_clock_guard as _wcg,
                    )
                    from src.ai.persona_voice import (
                        resolve_effective_persona_id as _repi_w,
                    )
                    from src.utils.persona_manager import (
                        PersonaManager as _PM_w,
                    )
                    _persona = None
                    try:
                        _wpid = _repi_w(
                            _cfg_w, platform, account_id, str(chat_key))
                        if _wpid:
                            _persona = (
                                _PM_w.get_instance()
                                .get_persona_by_id(str(_wpid)) or None)
                    except Exception:
                        _persona = None
                    _local = None
                    try:
                        from src.companion.persona_location import (
                            resolve_persona_now as _rpn,
                        )
                        if isinstance(_persona, dict):
                            _local = _rpn(_persona)
                    except Exception:
                        _local = None
                    _new, _winfo = _wcg(
                        text, persona=_persona, local_now=_local)
                    if _winfo.get("changed") and str(_new or "").strip():
                        text = _new
                        _assistant_ref.logger.info(
                            "[world_clock_guard] daypart=%s wrong_place=%s "
                            "platform=%s acct=%s",
                            _winfo.get("daypart_conflict"),
                            _winfo.get("wrong_place"),
                            platform, account_id)
            except Exception:
                _assistant_ref.logger.debug(
                    "[world_clock_guard] 跳过", exc_info=True)
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
                    # 桥路径没有分条能力（DOM 整段一次发出）：bubbles 开启时
                    # 拟稿是「每行一句」多行合同，原样入队＝一条消息带结构化
                    # 换行（2026-08-08 客户实锤质疑的 AI 感形态）→ 折叠成
                    # 自然单段再入队（与协议直发链同款收口，2026-08-09）。
                    try:
                        from src.inbox.reply_split import (
                            collapse_paragraphs as _clp_dt,
                            parse_bubbles_cfg as _pbc_dt,
                        )
                        if (_pbc_dt(_cfg).get("enabled")
                                and "\n" in str(text or "")):
                            text = _clp_dt(text) or text
                    except Exception:
                        pass
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

            # 引用回复决策（#37 I-4 D2 基线 → O-4 #254 规则引擎，2026-09-08）：
            # 确定性、不走 LLM，随机用会话级种子；必引 burst/stale、基线 random、
            # 禁引 short_quick/streak/hourly_cap 全在纯函数 reply_quote_policy 里，
            # 这里只取消息行、算一次、落一行 [quote] 日志。能力位关（LINE/Messenger）
            # 或 RPA 回落路径 → rule=unsupported 也落日志。任何异常＝不引用（绝不阻断投递）。
            _quote_ref = None
            try:
                from src.inbox import reply_quote_policy as _rqp
                from src.inbox.normalizer import conv_id as _cidf_q
                _qcfg = _rqp.parse_quote_cfg(_assistant_ref.config.config or {})
                _q_cid = _cidf_q(platform, account_id, chat_key)
                if not _qcfg.get("enabled"):
                    _assistant_ref.logger.debug(
                        "[quote] conv=%s reply_to_mid=- reason=none rule=disabled "
                        "platform=%s", _q_cid, platform)
                else:
                    if not _rqp.platform_allows_quote(
                            platform, _qcfg, orch_owns=_orch_owns):
                        _qdec = _rqp.unsupported_decision(
                            platform, orch_owns=_orch_owns)
                    else:
                        _q_store = getattr(_assistant_ref, "inbox_store", None)
                        # 40 行够覆盖「每小时 ≤6 / 连引冷却」的出站历史 + 连发串
                        _q_rows = (_q_store.list_recent_messages(_q_cid, limit=40)
                                   if _q_store is not None else [])
                        # text=实发文本（出站翻译后＝客户语言，与入站同语）；
                        # original_text=人设原文（与入站 translated_text 同语）——
                        # 两路都比，跨语会话也能算出相关性。
                        _qdec = _rqp.decide_quote(
                            _q_rows, str(text or ""), cfg=_qcfg,
                            reply_alt=str(original_text or ""), conv_key=_q_cid)
                    _rqp.record_decision(_qdec)
                    _quote_ref = _qdec.as_reply_to()
                    _assistant_ref.logger.info(
                        "%s", _rqp.format_log(_qdec, conv=_q_cid, platform=platform))
            except Exception:
                _quote_ref = None
                _assistant_ref.logger.debug(
                    "[quote] 决策异常（不引用）", exc_info=True)

            async def _send_one(_txt: str, _reply_to=None):
                def _make_coro(_rt):
                    return _send_via(
                        _send_shim, platform, account_id,
                        chat_key, _txt, _send_adapters,
                        origin=_send_origin, reply_to=_rt,
                    )

                async def _run(_rt):
                    if (_orch_owns and _wl is not None
                            and _wl.is_running()):
                        _fut = asyncio.run_coroutine_threadsafe(
                            _make_coro(_rt), _wl
                        )
                        return await asyncio.wrap_future(_fut)
                    return await _make_coro(_rt)

                if not _reply_to:
                    return await _run(None)
                # 带引用发送失败（目标消息已删/id 无效/worker 拒绝）→ 去引用重发
                # 一次：引用是锦上添花，投递才是底线（M-3 A 出站结果口径：不丢消息）。
                try:
                    _r = await _run(_reply_to)
                except Exception as _qex:  # noqa: BLE001
                    _assistant_ref.logger.info(
                        "[quote] 带引用发送失败，去引用重发 rule=fallback_plain "
                        "platform=%s: %s", platform, _qex)
                    try:
                        from src.inbox import reply_quote_policy as _rqp2
                        _rqp2.record_fallback_plain()
                    except Exception:
                        pass
                    return await _run(None)
                try:
                    from src.inbox import reply_quote_policy as _rqp3
                    _rqp3.record_applied(_r)
                except Exception:
                    pass
                return _r

            # 文本短句分条（inbox.reply_style.bubbles）：翻译后的 text 已是客户
            # 可读文本。仅编排器路径拆——RPA runner 自有 human_pacing，再拆会
            # 双重切碎。失败语义对齐语音 split_send：已发算数、剩余丢弃。
            _parts = [str(text or "")]
            _holdout_parts = 0   # P3 随机保留组：>0=本可拆 N 条但被抽中强制整段
            try:
                from src.inbox.reply_split import (
                    collapse_paragraphs as _cp,
                    parse_bubbles_cfg as _pbc,
                    plan_bubble_gaps as _pbg,
                    record_bubble_send as _rbs,
                    should_split_for_delivery as _ssd,
                    split_reply_parts as _srp,
                )
                _bcfg = _pbc(_assistant_ref.config.config or {})
                if _ssd(
                    cfg=_bcfg, platform=platform, chat_key=chat_key,
                    orch_owns=_orch_owns,
                ):
                    _split = _srp(
                        str(text or ""),
                        max_parts=int(_bcfg["max_parts"]),
                        max_chars=int(_bcfg["max_chars"]),
                        min_tail_chars=int(_bcfg["min_tail_chars"]),
                        min_total_chars=int(_bcfg["min_total_chars"]),
                        per_sentence=bool(_bcfg.get("per_sentence")),
                        explicit_newline_only=bool(
                            _bcfg.get("explicit_newline_only", True)),
                    )
                    if len(_split) >= 2:
                        _hp = float(_bcfg.get("holdout_pct") or 0.0)
                        if _hp > 0:
                            import random as _rnd
                            if _rnd.random() < _hp:
                                _holdout_parts = len(_split)
                        if _holdout_parts:
                            _assistant_ref.logger.info(
                                "[reply_bubbles] 保留组抽中，整段发送 "
                                "parts=%d platform=%s", _holdout_parts, platform)
                            # 保留组＝「这回合像一口气说完」→ 必须折叠成单段：
                            # bubbles 开启时拟稿合同是「每行一句」，多行原样单条
                            # 发出＝「一条消息带结构化换行」（2026-08-08 客户实锤
                            # 的 AI 感形态），比拆条更糟（2026-08-09 修）。
                            _parts = [_cp(str(text or "")) or str(text or "")]
                        else:
                            _parts = _split
                    elif _split:
                        # 拆不出第二条（#210 短回复门 / 无显式换行）：用纯函数给的
                        # 单条——它已把多行折成单段，不让「每行一句」合同的换行
                        # 原样漏进一条消息里。
                        _parts = [_split[0]]
            except Exception:
                _assistant_ref.logger.debug(
                    "[reply_bubbles] 拆条失败，回落单条", exc_info=True)
                _parts = [str(text or "")]
                _holdout_parts = 0

            if len(_parts) <= 1:
                # 单条分支发 _parts[0]（保留组时是折叠后的单段，非原始多行文本）
                _single_text = _parts[0] if _parts else str(text or "")
                _res_single = await _send_one(_single_text, _quote_ref)
                if not (isinstance(_res_single, dict) and (
                        _res_single.get("ok") is False
                        or _res_single.get("delivered") is False
                        or _res_single.get("blocked"))):
                    if _holdout_parts:
                        record_text_outreach(
                            _assistant_ref, platform, account_id, chat_key,
                            "holdout", note=f"parts={_holdout_parts}")
                    else:
                        record_text_outreach(
                            _assistant_ref, platform, account_id, chat_key,
                            "single")
                return _res_single

            _typing = None
            try:
                _typing = build_autosend_typing_cb(_assistant_ref)
            except Exception:
                _typing = None
            # 条间打断守卫前置解析（fresh_guard 同闸门，2026-08-05）：条间隔现可达
            # 20s，客户此间插话（任意文本/媒体）→ 停发剩余条——真人被打断就是停手。
            # 首条已发出＝客户已有回复，无「取消后零回复」断链风险，判据用宽口径
            # interrupted_by_inbound（与创建侧 find_superseding_inbound 刻意不同）。
            # 任何解析失败＝守卫不激活（照发，绝不阻断投递）。
            _bub_started = time.time()
            _bub_store = None
            _bub_cid = ""
            try:
                from src.inbox.draft_fresh_guard import (
                    parse_fresh_guard_cfg as _pfg_bub,
                )
                if _pfg_bub(_assistant_ref.config.config or {}).get("enabled"):
                    from src.inbox.normalizer import conv_id as _cidf_bub
                    _bub_store = getattr(_assistant_ref, "inbox_store", None)
                    _bub_cid = _cidf_bub(platform, account_id, chat_key)
            except Exception:
                _bub_store = None
                _bub_cid = ""
            # 条间隔预排（2026-08-09）：整组一次估值 → total_budget_sec 等比压缩
            # 保节奏形状（防 per_sentence 5 条×20s 把一条回复拖到 80s+）。
            _gaps: list = []
            try:
                _gaps = _pbg(
                    _parts,
                    gap_sec_lo=float(_bcfg["gap_sec_lo"]),
                    gap_sec_hi=float(_bcfg["gap_sec_hi"]),
                    per_char_sec=float(_bcfg["per_char_sec"]),
                    latin_per_char_sec=_bcfg.get("latin_per_char_sec"),
                    max_gap_sec=float(_bcfg.get("max_gap_sec", 6.0)),
                    total_budget_sec=float(
                        _bcfg.get("total_budget_sec") or 0.0),
                )
            except Exception:
                _gaps = []
            _sent_n = 0
            _last_res: dict = {}
            for _i, _part in enumerate(_parts):
                if _i > 0:
                    # 条间节奏 = 想（静默）+ 打下一条（挂「正在输入」续挂，
                    # >5s 间隔气泡不断续）——与首条前的拟人序列同一节奏模型。
                    # 兜底 3.0＝gap_sec_lo 时代缺省（规划器异常不回机关枪）
                    _gap = (_gaps[_i - 1]
                            if _i - 1 < len(_gaps) else 3.0)
                    try:
                        from src.integrations.humanize_metrics import (
                            record_bubble_gap as _rbg,
                        )
                        _rbg("autosend", platform, _gap)
                    except Exception:
                        pass
                    try:
                        from src.inbox.humanize import (
                            estimate_typing_lead as _etl,
                            run_presend_humanization as _rph,
                        )
                        _tp_part = None
                        if _typing is not None:
                            async def _tp_part(_action, _p=platform,
                                               _a=account_id, _c=chat_key):
                                await _typing(_p, _a, _c)
                        await _rph(
                            delay=_gap, action="typing", typing=_tp_part,
                            sleep=asyncio.sleep,
                            typing_lead_sec=_etl(
                                _part,
                                per_char_sec=float(_bcfg["per_char_sec"]),
                                latin_per_char_sec=_bcfg.get(
                                    "latin_per_char_sec")),
                        )
                    except Exception:
                        await asyncio.sleep(_gap)
                    # 条间打断复查：间隔睡完、发出前最后看一眼——对方在这几秒
                    # 里说话了就停手，剩余内容丢弃（插话触发的新稿自带完整上下文）。
                    if _bub_store is not None and _bub_cid:
                        try:
                            from src.inbox.draft_fresh_guard import (
                                interrupted_by_inbound as _ibi,
                            )
                            if _ibi(
                                _bub_store.list_recent_messages(
                                    _bub_cid, limit=5),
                                started_ts=_bub_started,
                            ) is not None:
                                _assistant_ref.logger.info(
                                    "[reply_bubbles] 条间客户插话，停发剩余 "
                                    "%d/%d 条 platform=%s",
                                    len(_parts) - _sent_n, len(_parts), platform)
                                try:
                                    _rbs("autosend", _sent_n, partial=True)
                                except Exception:
                                    pass
                                record_text_outreach(
                                    _assistant_ref, platform, account_id,
                                    chat_key, "bubbles",
                                    note=(f"parts={_sent_n}/{len(_parts)}"
                                          " interrupted"))
                                return {
                                    "ok": True,
                                    "delivered_as": "text_bubbles",
                                    "parts_sent": _sent_n,
                                    "parts_total": len(_parts),
                                    "partial": True,
                                    "interrupted": True,
                                }
                        except Exception:
                            _assistant_ref.logger.debug(
                                "[reply_bubbles] 条间打断判定异常（继续发）",
                                exc_info=True)
                try:
                    # 仅首条带引用（与坐席手动分条 _deliver_bubble_parts 同语义）
                    _last_res = await _send_one(
                        _part, _quote_ref if _i == 0 else None) or {}
                except Exception as _ex:
                    if _sent_n > 0:
                        _assistant_ref.logger.warning(
                            "[reply_bubbles] 中途失败，已发 %d/%d platform=%s: %s",
                            _sent_n, len(_parts), platform, _ex)
                        try:
                            _rbs("autosend", _sent_n, partial=True)
                        except Exception:
                            pass
                        record_text_outreach(
                            _assistant_ref, platform, account_id, chat_key,
                            "bubbles", note=f"parts={_sent_n}/{len(_parts)}")
                        return {
                            "ok": True,
                            "delivered_as": "text_bubbles",
                            "parts_sent": _sent_n,
                            "parts_total": len(_parts),
                            "partial": True,
                        }
                    raise
                if isinstance(_last_res, dict) and (
                    _last_res.get("ok") is False
                    or _last_res.get("delivered") is False
                    or _last_res.get("blocked")
                ):
                    if _sent_n > 0:
                        try:
                            _rbs("autosend", _sent_n, partial=True)
                        except Exception:
                            pass
                        record_text_outreach(
                            _assistant_ref, platform, account_id, chat_key,
                            "bubbles", note=f"parts={_sent_n}/{len(_parts)}")
                        return {
                            "ok": True,
                            "delivered_as": "text_bubbles",
                            "parts_sent": _sent_n,
                            "parts_total": len(_parts),
                            "partial": True,
                        }
                    return _last_res
                _sent_n += 1
            _assistant_ref.logger.info(
                "[reply_bubbles] 分条已发 %d platform=%s acct=%s",
                _sent_n, platform, account_id)
            try:
                _rbs("autosend", _sent_n)
            except Exception:
                pass
            record_text_outreach(
                _assistant_ref, platform, account_id, chat_key,
                "bubbles", note=f"parts={_sent_n}")
            return {
                "ok": True,
                "delivered_as": "text_bubbles",
                "parts_sent": _sent_n,
                "parts_total": len(_parts),
            }

        send_cb = _autosend_deliver
    translate_cb = build_autosend_translate_cb(assistant, web_app)
    return send_cb, translate_cb


def build_autosend_support_kwargs(assistant, web_app) -> dict:
    """投递模式的支撑件全家桶（P1 2026-08-22「一键全自动」热接线用）。

    与 bootstrap 装配 AutosendWorker 时的 persona_resolver / dup_guard /
    fresh_guard / work_schedule provider / pilot_guard 同口径（活读 config 根的
    闭包语义一致），供 ``AutosendWorker.apply_send_callbacks`` 在 deliver 运行时
    翻开时一次注入——deliver_only 兜底实例构造时没有这些件，热升格必须补齐。
    任何单件构造失败按缺省（None/关闭）软降级，绝不让热接线整体失败。
    """
    out: dict = {}

    def _persona_resolver(platform, account_id, chat_key="",
                          _cfg=assistant.config.config or {}):
        try:
            from src.ai.persona_voice import (
                resolve_effective_persona_id as _repi,
            )
            return _repi(_cfg, platform, account_id, str(chat_key or "")) or ""
        except Exception:
            return ""

    out["persona_resolver"] = _persona_resolver
    try:
        from src.inbox.outbound_dup_guard import (
            attach_rewrite_fn, resolve_guard_cfg,
        )
        out["dup_guard_cfg"] = attach_rewrite_fn(
            resolve_guard_cfg(assistant.config.config or {}),
            getattr(assistant, "ai_client", None))
    except Exception:
        out["dup_guard_cfg"] = None
    try:
        from src.inbox.draft_fresh_guard import parse_fresh_guard_cfg
        out["fresh_guard_cfg"] = parse_fresh_guard_cfg(
            assistant.config.config or {})
    except Exception:
        out["fresh_guard_cfg"] = None

    def _ws_provider(_cm=assistant.config):
        try:
            from src.inbox.work_hours_gate import work_schedule_cfg
            return work_schedule_cfg(getattr(_cm, "config", None) or {})
        except Exception:
            return {}

    out["work_schedule_provider"] = _ws_provider

    def _pilot_guard(platform, account_id, _cm=assistant.config):
        try:
            from src.integrations.surface_fusion import (
                autosend_blocked,
                note_pilot_yield,
            )
            blocked = autosend_blocked(
                getattr(_cm, "config", None) or {}, platform, account_id)
            if blocked:
                note_pilot_yield(platform, account_id, "autosend")
            return blocked
        except Exception:
            return False

    out["pilot_guard"] = _pilot_guard
    return out


def make_autosend_rewire(assistant, web_app):
    """构造「autosend 热接线」闭包（bootstrap 注册到 ``app.state.autosend_rewire``）。

    为什么存在（P1 2026-08-22）：deliver/worker 此前是**构造期冻结**——能力看板 /
    向导 / 值守开关写完 overlay 后，真发要等下次重启才生效（impl49 B37 实录
    「开了全自动还是不回复」三成因之一）。路由在写 overlay 成功后 best-effort
    调本闭包，把 bootstrap 同款回调注入运行中的 worker：

      - deliver 翻开且 worker 未武装 → 建 send/translate/mark_read/typing 回调
        + 支撑件全家桶一次注入（deliver_only 兜底实例热升格同路）；
      - deliver 翻关且 worker 已武装 → 撤 send_callback（自动链停发；人工链
        与人工投递回调不动——「人的明示决定」不受 deliver 管）；
      - ``l2_autosend.enabled`` 已开而自动循环未跑 → ``ensure_auto_loop`` 升格
        （二次启动由 run() 的防双循环护栏兜底）。

    返回摘要 dict（honest response：rewired / loop_started / reason），
    自吞一切异常——热接线失败不影响 overlay 已写入（重启后仍会生效）。
    """

    def _rewire() -> dict:
        try:
            worker = getattr(web_app.state, "autosend_worker", None)
            if worker is None:
                return {"rewired": False, "reason": "no_worker"}
            cfg_root = assistant.config.config or {}
            l2 = (cfg_root.get("inbox") or {}).get("l2_autosend") or {}
            deliver = bool(l2.get("deliver", False))
            armed = bool(getattr(worker, "_send_callback", None) is not None)
            out: dict = {"rewired": False, "deliver": deliver}
            if deliver and not armed:
                _s_cb, _t_cb = build_autosend_callbacks(
                    assistant, web_app, True)
                worker.apply_send_callbacks(
                    send_callback=_s_cb,
                    translate_callback=_t_cb,
                    mark_read_callback=build_autosend_mark_read_cb(
                        assistant, always=True),
                    typing_callback=build_autosend_typing_cb(
                        assistant, always=True),
                    **build_autosend_support_kwargs(assistant, web_app),
                )
                out["rewired"] = True
                assistant.logger.info(
                    "[autosend] deliver 已热接线（真发即时生效，无需重启）")
            elif not deliver and armed:
                worker.apply_send_callbacks(send_callback=None)
                out["rewired"] = True
                assistant.logger.info(
                    "[autosend] deliver 已热撤线（自动链停发；人工链不受影响）")
            if (bool(l2.get("enabled", True))
                    and not bool(getattr(worker, "_running", False))
                    and hasattr(worker, "ensure_auto_loop")):
                if worker.ensure_auto_loop():
                    out["loop_started"] = True
                    # deliver_only 兜底实例装配时从不注册 L2 事件唤醒（它本就
                    # 不跑循环）——热升格后必须补上，否则新草稿只能等 60s 轮询
                    # 兜底（「点了全自动，第一条回复慢一分钟」的隐性台阶）。
                    # 幂等守卫：重复 rewire 不重复注册（重复注册只是多 set 一次
                    # event 无害，但没必要攒回调链）。
                    store = getattr(web_app.state, "inbox_store", None)
                    if (store is not None
                            and not getattr(worker, "_l2_cb_registered", False)
                            and hasattr(store, "register_l2_callback")):
                        try:
                            store.register_l2_callback(worker.notify_new_l2)
                            worker._l2_cb_registered = True
                        except Exception:
                            assistant.logger.debug(
                                "[autosend] 热升格 L2 回调注册失败（轮询兜底仍在）",
                                exc_info=True)
                    assistant.logger.info(
                        "[autosend] 自动循环已热启动（原 deliver_only/停用实例升格）")
            return out
        except Exception:
            assistant.logger.warning(
                "[autosend] 热接线失败（overlay 已写入，重启后仍会生效）",
                exc_info=True)
            return {"rewired": False, "reason": "error"}

    return _rewire
