"""全自动语音回复（System Z autosend 的 TTS 出站，Phase 全自动语音）。

把「全自动聊天 + 翻译 + 语音」凑齐成闭环：之前统一收件箱 autosend 只发**文本**，
auto-voice 仅存在于原生 TG 客户端（``telegram.voice_reply``）与 RPA 设备号
（``voice_output.auto_voice``）。本模块给 **System Z 全自动 autosend** 补上「按策略把
回复转 TTS 语音」的能力，一处生效、全平台共用（经 ``orch.send_media(media_type="voice")``，
Telegram/WhatsApp/Messenger/LINE/Instagram 均可，见 official_api_worker.send_media）。

设计（复用既有件，避免重复造轮子）：
- 语音配置：``persona_voice.resolve_voice_cfg``（人设 voice_profile → 声音克隆/后端）。
- 合成：``ai.tts_pipeline.TTSPipeline``；格式：``client.voice_sender.convert_to_ogg_opus``。
- 落盘：``protocol_bridge.save_outbound_media``（与坐席「发送语音」同一出站媒体目录）。
- 触发护栏：仿 ``client.sender._maybe_send_voice_reply``（trigger / 长度上限 / 失败回落文本）。

**默认关**（``inbox.l2_autosend.voice.enabled=false``）→ 全自动仍纯文本，零行为变更。
任何环节失败都返回「不发语音」让调用方回落文本，绝不卡住全自动主流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.ai.voice_fitness import VoiceDecision

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 200
_VALID_TRIGGERS = ("never", "always", "when_peer_voice", "smart")

_CLONE_BACKENDS = frozenset({
    "avatar_clone", "minicpm_clone", "voice_clone_command", "coqui_http",
})
_EDGE_FALLBACK_PROVIDERS = frozenset({
    "edge_tts", "openai", "elevenlabs", "pyttsx3",
})

# 最近一次合成失败原因（stage 返回 None 时 autosend 读此写入 metrics）
_LAST_SYNTH_FAIL: Dict[str, str] = {}
_LAST_SYNTH_FAIL_LOCK = threading.Lock()


def pop_synth_failure_reason() -> str:
    """取出并清空最近一次 stage 合成失败原因（默认 synth_failed）。"""
    with _LAST_SYNTH_FAIL_LOCK:
        reason = str(_LAST_SYNTH_FAIL.pop("reason", "") or "").strip()
    return reason or "synth_failed"


def _set_synth_failure(reason: str) -> None:
    with _LAST_SYNTH_FAIL_LOCK:
        _LAST_SYNTH_FAIL["reason"] = str(reason or "synth_failed").strip() or "synth_failed"


def _avatar_voice_policy(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """全局语音降级策略块 ``avatar_voice.policy``（A/B/主动线共用默认）。"""
    try:
        pol = ((config or {}).get("avatar_voice") or {}).get("policy")
        return dict(pol) if isinstance(pol, dict) else {}
    except Exception:
        return {}


def no_edge_fallback_enabled(voice_block: Optional[Dict[str, Any]]) -> bool:
    """块内显式 ``no_edge_fallback`` 值（block-only，不含全局继承）。"""
    return bool((voice_block or {}).get("no_edge_fallback"))


def defer_during_image_enabled(voice_block: Optional[Dict[str, Any]]) -> bool:
    """发图 GPU 占用期间 B 线 defer 语音（回落文字）。默认开（block-only）。"""
    vb = voice_block or {}
    if "defer_during_image" in vb:
        return bool(vb.get("defer_during_image"))
    return True


def resolve_no_edge_fallback(
    config: Optional[Dict[str, Any]],
    local_block: Optional[Dict[str, Any]] = None,
) -> bool:
    """no_edge_fallback 策略解析（A/B/主动线单一口径）：

    局部块（``inbox.l2_autosend.voice`` / ``telegram.voice_reply``）显式设置**优先**，
    否则回落全局 ``avatar_voice.policy.no_edge_fallback``（默认 False）。这样运营只需
    在全局配一次，各链路自动继承；个别链路仍可就地覆写。
    """
    lb = local_block or {}
    if "no_edge_fallback" in lb:
        return bool(lb.get("no_edge_fallback"))
    return bool(_avatar_voice_policy(config).get("no_edge_fallback", False))


def resolve_defer_during_image(
    config: Optional[Dict[str, Any]],
    local_block: Optional[Dict[str, Any]] = None,
) -> bool:
    """发图 GPU 占用期间 defer 语音策略解析（A/B 线单一口径）：

    局部块显式值优先，否则取全局 ``avatar_voice.policy.defer_during_image``，
    两者皆缺 → 默认开。

    ⚠ 前提辨析（2026-07-14 Phase22 复核）：本策略仅在**发图与语音克隆共享同一 GPU**
    （单机部署：本地 ComfyUI + 本地 7852 同卡）时才有收益——此时并发出图会抢显存拖垮
    合成。若**发图在独立主机/GPU**（如 ComfyUI 在 176/5090、7852 克隆在本机 3060），
    ``_IMAGE_GEN_INFLIGHT`` 是全局计数 → 会因**别的会话**在出图而把**本会话**语音降级为
    文字，无 GPU 收益反伤语音体验。故默认 True 保守护住单机部署，分离拓扑应经 overlay
    置 False（见 config.local ``avatar_voice.policy.defer_during_image``）。
    """
    lb = local_block or {}
    if "defer_during_image" in lb:
        return bool(lb.get("defer_during_image"))
    pol = _avatar_voice_policy(config)
    if "defer_during_image" in pol:
        return bool(pol.get("defer_during_image"))
    return True


def is_edge_fallback_result(provider: str, fallback_from: str) -> bool:
    prov = str(provider or "").strip().lower()
    fb = str(fallback_from or "").strip().lower()
    return bool(fb) and prov in _EDGE_FALLBACK_PROVIDERS


def should_reject_voice_tts_result(result: Any, *, no_edge: bool) -> bool:
    """A/B 线共用：no_edge 时拒收 edge 兜底合成结果。"""
    if not no_edge:
        return False
    extra = getattr(result, "extra", None) or {}
    return is_edge_fallback_result(
        str(getattr(result, "provider", "") or ""),
        str(extra.get("fallback_from") or ""),
    )


def _nudge_7852_boot(config: Dict[str, Any]) -> None:
    try:
        from src.ai.avatar_voice import nudge_emotion_tts_boot
        nudge_emotion_tts_boot(config)
    except Exception:
        pass


def _peek_prerender_hit(
    config: Dict[str, Any],
    persona_id: str,
    text: str,
    voice_cfg: Dict[str, Any],
) -> bool:
    """预渲染命中探测（7852 宕机时仍可零 GPU 发克隆声）。"""
    try:
        from src.ai.tts_pipeline import clean_text_for_tts
        from src.ai.voice_prerender import find_prerendered
        av = (voice_cfg.get("avatar_voice")
              or (config or {}).get("avatar_voice")
              or {})
        pre_cfg = av.get("prerender") if isinstance(av.get("prerender"), dict) else {}
        if not pre_cfg.get("enabled", True):
            return False
        ref = str((voice_cfg.get("voice_profile") or {}).get(
            "reference_audio_path") or "").strip()
        cleaned = clean_text_for_tts(text)
        if not cleaned:
            return False
        return find_prerendered(
            persona_id, cleaned,
            base_dir=str(pre_cfg.get("base_dir") or "assets/voices"),
            ref_path=ref,
        ) is not None
    except Exception:
        return False


def _avatar_clone_ready(config: Dict[str, Any]) -> bool:
    try:
        av_cfg = (config or {}).get("avatar_voice") or {}
        if not av_cfg.get("enabled"):
            return True
        from src.ai.avatar_voice import AvatarVoiceClient
        return AvatarVoiceClient(av_cfg).health_ok(use_cache=True)
    except Exception:
        return False


def preflight_voice_synth(
    config: Dict[str, Any],
    voice_block: Dict[str, Any],
    persona_id: str,
    text: str,
    *,
    voice_cfg: Dict[str, Any],
) -> Optional[str]:
    """克隆优先且禁 edge 回落时，合成前快检。返回 skip 原因；None=可继续。

    预渲染命中不受 7852 状态影响（零 GPU 克隆声仍可发）。
    """
    if not resolve_no_edge_fallback(config, voice_block):
        return None
    backend = str(voice_cfg.get("backend") or "").strip().lower()
    if backend not in _CLONE_BACKENDS:
        return None
    if _peek_prerender_hit(config, persona_id, text, voice_cfg):
        return None
    if backend == "avatar_clone" and not _avatar_clone_ready(config):
        _nudge_7852_boot(config)
        return "7852_unready"
    return None


def _reject_edge_fallback(meta: VoiceStageMeta, no_edge: bool) -> bool:
    """True=应拒发（克隆不可达却走了 edge 兜底）。"""
    if not no_edge:
        return False
    prov = str(meta.get("provider") or "").strip().lower()
    fb = str(meta.get("fallback_from") or "").strip().lower()
    if not fb:
        return False
    return prov in _EDGE_FALLBACK_PROVIDERS


# stage_voice_file 成功时第三元：合成/投递可观测元数据（日志 + metrics 共用）
VoiceStageMeta = Dict[str, Any]

# ── 全自动语音可观测性（进程内累计；供 /api/drafts/autosend-status 暴露）──────────
# 只在「策略已判定该发语音」之后计数：sent=真发出语音；fallback=合成/投递失败回落文本。
# 灰度时不必逐会话翻聊天记录即可监控自动语音是否在工作、回落原因与最近时长。
_METRICS: Dict[str, Any] = {
    "sent": 0, "fallback": 0, "last_reason": "",
    "last_ts": 0.0, "last_duration_ms": 0,
    # 合成后端观测（2026-07-14：诊断「克隆 vs edge 兜底」）
    "last_provider": "", "last_fallback_from": "",
    "last_synth_text_len": 0, "last_persona_id": "",
    "truncation_suspects": 0, "truncation_retries": 0,
    "provider_counts": {}, "fallback_counts": {},
    "fallback_reasons": {},
    # Stage4 决策观测：每次「该不该发语音」判定结果 + 原因分布（调阈值 / 看灰度命中率）。
    "voice_chosen": 0, "text_chosen": 0,
    "decision_reasons": {}, "last_decision": "",
}
_METRICS_LOCK = threading.Lock()


def _bump_counter(bucket: Dict[str, int], key: str) -> None:
    k = str(key or "").strip() or "unknown"
    bucket[k] = int(bucket.get(k, 0)) + 1


def record_voice_sent(
    duration_ms: int = 0,
    *,
    synth_meta: Optional[VoiceStageMeta] = None,
) -> None:
    meta = dict(synth_meta or {})
    with _METRICS_LOCK:
        _METRICS["sent"] = int(_METRICS["sent"]) + 1
        _METRICS["last_ts"] = time.time()
        if duration_ms and duration_ms > 0:
            _METRICS["last_duration_ms"] = int(duration_ms)
        prov = str(meta.get("provider") or "")
        if prov:
            _METRICS["last_provider"] = prov
            _bump_counter(_METRICS.setdefault("provider_counts", {}), prov)
        fb = str(meta.get("fallback_from") or "")
        if fb:
            _METRICS["last_fallback_from"] = fb
            _bump_counter(_METRICS.setdefault("fallback_counts", {}), fb)
        if meta.get("synth_text_len"):
            _METRICS["last_synth_text_len"] = int(meta["synth_text_len"])
        if meta.get("persona_id"):
            _METRICS["last_persona_id"] = str(meta["persona_id"])
        if meta.get("truncation_suspect"):
            _METRICS["truncation_suspects"] = int(
                _METRICS.get("truncation_suspects", 0)) + 1
        if meta.get("truncation_retried"):
            _METRICS["truncation_retries"] = int(
                _METRICS.get("truncation_retries", 0)) + 1


def record_voice_fallback(reason: str) -> None:
    r = str(reason or "").strip() or "unknown"
    with _METRICS_LOCK:
        _METRICS["fallback"] = int(_METRICS["fallback"]) + 1
        _METRICS["last_reason"] = r
        _bump_counter(_METRICS.setdefault("fallback_reasons", {}), r)
        _METRICS["last_ts"] = time.time()


def record_voice_decision(send_voice: bool, reason: str) -> None:
    """记一次「该不该发语音」判定（voice/text 计数 + 原因分布），供 autosend-status 观测。

    与 sent/fallback 不同：sent/fallback 是「已决定发语音」后的合成投递结果，本函数记的是
    更上游的**决策本身**——含「判文字」的占比与原因（如 low_fitness/too_long/unspeakable），
    用于灰度期看语音占比是否符合"克制"手感、按 reason 分布调阈值。
    """
    with _METRICS_LOCK:
        key = "voice_chosen" if send_voice else "text_chosen"
        _METRICS[key] = int(_METRICS.get(key, 0)) + 1
        r = str(reason or "")
        reasons = _METRICS.setdefault("decision_reasons", {})
        reasons[r] = int(reasons.get(r, 0)) + 1
        _METRICS["last_decision"] = ("voice:" if send_voice else "text:") + r


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        snap = dict(_METRICS)
        snap["decision_reasons"] = dict(_METRICS.get("decision_reasons") or {})
        snap["fallback_reasons"] = dict(_METRICS.get("fallback_reasons") or {})
        return snap


def resolve_voice_autosend_cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``inbox.l2_autosend.voice`` 块（缺失返回空 dict → enabled 视为 false）。"""
    try:
        return dict(
            (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("voice")
            or {}
        )
    except Exception:
        return {}


def decide_voice(
    voice_block: Dict[str, Any],
    text: str,
    *,
    peer_sent_voice: bool = False,
    recent_voice_ratio: float = 0.0,
    peer_emotion: str = "",
    peer_emotion_intensity: float = -1.0,
    intimacy: float = 0.0,
    crisis_block: bool = False,
) -> VoiceDecision:
    """决策本条回复发**语音**还是**文字**，返回带 ``reason`` 的 VoiceDecision（供观测）。

    统一 4 档 trigger 与 smart 评分的**单一入口**；``should_send_voice`` 是其布尔投影。
    - ``enabled=false`` / 空文本 / 长度越界 → 文字（reason: disabled/empty/too_short/too_long）。
    - ``trigger``：``never`` / ``always`` / ``when_peer_voice``（默认，对等）/ ``smart``。
    - ``smart``：委托 ``ai.voice_fitness.voice_fitness``——按**回复情绪 + 客户此刻情绪 +
      亲密度 + 频率**综合评分，``score ≥ threshold`` 才语音。调用方采集并传入
      ``recent_voice_ratio`` / ``peer_emotion*`` / ``intimacy`` / ``crisis_block``
      （缺省退化为"仅回复情绪 + 对等回应"，仍安全可用）。参数见 ``voice_block['smart']``。
    """
    vb = voice_block or {}
    if not bool(vb.get("enabled")):
        return VoiceDecision(False, 0.0, "disabled")
    t = (text or "").strip()
    if not t:
        return VoiceDecision(False, 0.0, "empty")
    n = len(t)
    try:
        min_chars = int(vb.get("min_chars", 1) or 1)
        max_chars = int(vb.get("max_chars", _DEFAULT_MAX_CHARS) or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        min_chars, max_chars = 1, _DEFAULT_MAX_CHARS
    if n < min_chars:
        return VoiceDecision(False, 0.0, "too_short")
    if n > max_chars:
        return VoiceDecision(False, 0.0, "too_long")
    trigger = str(vb.get("trigger", "when_peer_voice") or "when_peer_voice").lower()
    if trigger not in _VALID_TRIGGERS:
        trigger = "when_peer_voice"
    if trigger == "never":
        return VoiceDecision(False, 0.0, "trigger_never")
    if trigger == "always":
        return VoiceDecision(True, 1.0, "trigger_always")
    if trigger == "smart":
        from src.ai.voice_fitness import voice_fitness
        smart_cfg = vb.get("smart") if isinstance(vb.get("smart"), dict) else {}
        # voice_block 的 max_chars 已在上方护栏过；并进 smart cfg 保持同一长度口径。
        merged = {"max_chars": max_chars, **smart_cfg}
        return voice_fitness(
            t, peer_sent_voice=peer_sent_voice,
            recent_voice_ratio=recent_voice_ratio,
            peer_emotion=peer_emotion,
            peer_emotion_intensity=peer_emotion_intensity,
            intimacy=intimacy, crisis_block=crisis_block, cfg=merged)
    # when_peer_voice（默认）：你发语音我回语音
    if peer_sent_voice:
        return VoiceDecision(True, 1.0, "peer_voice")
    return VoiceDecision(False, 0.0, "no_peer_voice")


def should_send_voice(
    voice_block: Dict[str, Any],
    text: str,
    *,
    peer_sent_voice: bool = False,
    recent_voice_ratio: float = 0.0,
    peer_emotion: str = "",
    peer_emotion_intensity: float = -1.0,
    intimacy: float = 0.0,
    crisis_block: bool = False,
) -> bool:
    """``decide_voice`` 的布尔投影（向后兼容）。含 reason 的完整决策见 ``decide_voice``。"""
    return decide_voice(
        voice_block, text, peer_sent_voice=peer_sent_voice,
        recent_voice_ratio=recent_voice_ratio, peer_emotion=peer_emotion,
        peer_emotion_intensity=peer_emotion_intensity, intimacy=intimacy,
        crisis_block=crisis_block).send_voice


def persona_allowed_for_voice(
    voice_block: Dict[str, Any], persona_id: Optional[str]
) -> bool:
    """人设级灰度闸门：本人设是否获准发自动语音（与长度/trigger 决策正交）。

    ``persona_allowlist`` 缺省/空 → 不限制（True，向后兼容：所有人设按各自 voice_profile
    发声）。非空 → 仅名单内人设放行，名单外回落纯文本。灰度期用 ``[lin_xiaoyu]`` 把真声
    语音收敛到单一人设，放量时清空名单即可。``persona_id`` 应为**解析后**的真实人设 id
    （编排器号 meta 常无 persona_id → 调用方须先按会话绑定/默认解析再传入）。
    """
    vb = voice_block or {}
    allow = vb.get("persona_allowlist")
    if not allow:
        return True
    try:
        names = {str(x).strip() for x in allow if str(x).strip()}
    except TypeError:
        return True
    if not names:
        return True
    return bool(persona_id) and str(persona_id).strip() in names


async def _synth_ogg(config: Dict[str, Any], persona_id: str, text: str,
                     *, out_dir: str, contact_key: Optional[str] = None,
                     platform: str = "telegram",
                     account_id: Optional[str] = None) -> Tuple[Optional[str], VoiceStageMeta]:
    """合成 TTS → 转 OGG/Opus，返回 ``(本地路径, 元数据)``；失败路径为 ``(None, meta)``。

    元数据含 provider / fallback_from / synth_text_len / truncation_* 供日志与看板。
    """
    meta: VoiceStageMeta = {
        "provider": "", "fallback_from": "", "voice": "",
        "synth_text_len": len(str(text or "").strip()),
        "persona_id": str(persona_id or ""),
        "truncation_suspect": False, "truncation_retried": False,
        "latency_ms": 0,
    }
    _vb = resolve_voice_autosend_cfg(config)
    _no_edge = resolve_no_edge_fallback(config, _vb)
    try:
        from src.ai.persona_voice import resolve_effective_voice_context
        voice_ctx = resolve_effective_voice_context(
            config or {}, persona_id=persona_id or None,
            chat_key=contact_key, contact_key=contact_key,
            platform=platform, account_id=account_id, text=text)
        voice_cfg = voice_ctx.get("voice_cfg") or {}
    except Exception:
        logger.debug("[voice_autosend] resolve_voice_cfg 失败", exc_info=True)
        _set_synth_failure("resolve_voice_failed")
        return None, meta
    # 语言路由（粤语→粤语音色 + follow_text 音色跟随文本语种）：主动选择而非
    # 兜底降级 → 免受 no_edge 拒发/快检。必须在 preflight 之前：路由命中后
    # backend 已非克隆类，"克隆不可达"快检自然放行。
    _lang_route = ""
    try:
        from src.ai.lang_voice_route import is_reject_tag, route_voice_cfg_for_text
        voice_cfg, _lang_route = route_voice_cfg_for_text(voice_cfg, text, config)
        if is_reject_tag(_lang_route):
            # 拒发守卫：文本语种明确但无匹配音色——发错语言的语音比不发更糟
            # （生产实锤：ja 音色念中文被客户当"讲日语"投诉）→ 回落文字。
            meta["lang_route"] = _lang_route
            _set_synth_failure("lang_mismatch")
            logger.info(
                "[voice_autosend] 语言不匹配拒发语音（%s）pid=%s len=%d → 回落文字",
                _lang_route, persona_id, meta["synth_text_len"])
            return None, meta
        if _lang_route:
            meta["lang_route"] = _lang_route
            _no_edge = False
    except Exception:
        logger.debug("[voice_autosend] 语言路由异常（忽略）", exc_info=True)
    skip = preflight_voice_synth(
        config, _vb, persona_id, text, voice_cfg=voice_cfg)
    if skip:
        meta["preflight_skip"] = skip
        _set_synth_failure(skip)
        logger.info(
            "[voice_autosend] 合成前快检跳过 reason=%s pid=%s len=%d",
            skip, persona_id, meta["synth_text_len"])
        return None, meta
    voice_cfg["enabled"] = True
    voice_cfg["out_dir"] = out_dir
    if _no_edge:
        voice_cfg["fallback_on_error"] = False
    if "tts_cache" not in voice_cfg:
        voice_cfg["tts_cache"] = {"enabled": False}
    # 生成层口语版（Phase G B 线）：草稿生成时 LLM 已同步产出的「说话版」——
    # 凭草稿全文哈希取用（翻译/改写过的文本取不到 → 正常走 TTS 前口语化链）。
    _spoken = None
    try:
        from src.ai.spoken_variant import take_spoken_variant
        _spoken = take_spoken_variant(text)
    except Exception:
        _spoken = None
    synth_src = _spoken or text
    if _spoken:
        meta["spoken_variant"] = True
        logger.info(
            "[voice_autosend] 命中生成层口语版（len=%d→%d）pid=%s",
            len(str(text or "").strip()), len(_spoken), persona_id)
    try:
        from src.ai.tts_pipeline import (
            TTSPipeline,
            flatten_tts_clauses,
            suspect_tts_truncation,
        )
        tts = TTSPipeline(voice_cfg)
        result = await tts.synthesize(
            synth_src, timeout_sec=45.0, emotion=voice_ctx.get("emotion"),
            pre_colloquialized=bool(_spoken))
    except Exception:
        logger.debug("[voice_autosend] TTS 合成异常", exc_info=True)
        _set_synth_failure("synth_exception")
        return None, meta
    if not getattr(result, "ok", False) or not getattr(result, "audio_path", ""):
        meta["provider"] = str(getattr(result, "provider", "") or "")
        meta["error"] = str(getattr(result, "error", "") or "")
        _set_synth_failure(str(meta["error"] or "synth_failed"))
        return None, meta

    meta["provider"] = str(result.provider or "")
    meta["voice"] = str(result.voice or "")
    meta["fallback_from"] = str((result.extra or {}).get("fallback_from") or "")
    meta["latency_ms"] = int(getattr(result, "latency_ms", 0) or 0)
    if _reject_edge_fallback(meta, _no_edge):
        meta["edge_rejected"] = True
        _set_synth_failure("edge_rejected")
        logger.warning(
            "[voice_autosend] no_edge_fallback 拒发 edge provider=%s "
            "fallback_from=%s pid=%s",
            meta["provider"], meta["fallback_from"], persona_id)
        return None, meta
    if result.duration_sec and result.duration_sec > 0:
        meta["wav_duration_ms"] = int(float(result.duration_sec) * 1000)

    # 截断嫌疑 → 压平句读重试一次（仅克隆类；edge 回落不重试）
    # 注意口径：实际合成的是 synth_src（命中口语版时=口语文本），嫌疑判定与
    # 压平都按 synth_src 走，防止拿书面版时长预期误判口语版音频截断。
    try:
        dur_ms = int(meta.get("wav_duration_ms") or 0)
        if suspect_tts_truncation(
                synth_src, dur_ms, provider=meta["provider"]):
            meta["truncation_suspect"] = True
            flat = flatten_tts_clauses(synth_src)
            if flat and flat != str(synth_src or "").strip():
                meta["truncation_retried"] = True
                retry = await tts.synthesize(
                    flat, timeout_sec=45.0, emotion=voice_ctx.get("emotion"),
                    pre_colloquialized=bool(_spoken))
                if (getattr(retry, "ok", False)
                        and getattr(retry, "audio_path", "")
                        and (retry.duration_sec or 0) > (result.duration_sec or 0)):
                    result = retry
                    meta["provider"] = str(retry.provider or meta["provider"])
                    meta["fallback_from"] = str(
                        (retry.extra or {}).get("fallback_from")
                        or meta["fallback_from"])
                    meta["latency_ms"] = int(
                        meta["latency_ms"]) + int(retry.latency_ms or 0)
                    if retry.duration_sec and retry.duration_sec > 0:
                        meta["wav_duration_ms"] = int(
                            float(retry.duration_sec) * 1000)
                    meta["synth_text_len"] = len(flat)
                    logger.info(
                        "[voice_autosend] 截断嫌疑→压平重试 ok "
                        "provider=%s dur_ms=%s→%s",
                        meta["provider"],
                        dur_ms, meta.get("wav_duration_ms"))
    except Exception:
        logger.debug("[voice_autosend] 截断重试异常", exc_info=True)

    # 最终质量闸门（2026-07-15 乱码语音防线，与原生 voice_reply 同口径）：
    # 换行启发式 + 压平重试都救不回来的截断坏音（时长低于文本物理最快语速）
    # → 宁缺毋滥，放弃语音（调用方回落文字草稿）。配置同形：
    # ``inbox.l2_autosend.voice.quality_gate.{enabled,min_sec_per_unit,min_units}``。
    _qg_bad = False
    _qg_why = ""
    try:
        from src.ai.tts_quality import looks_truncated, resolve_quality_gate
        _qg = resolve_quality_gate(_vb)
        if _qg["enabled"]:
            _qg_bad, _qg_why = looks_truncated(
                synth_src, float(result.duration_sec or 0),
                min_sec_per_unit=_qg["min_sec_per_unit"],
                min_units=_qg["min_units"])
    except Exception:
        _qg_bad = False
    if _qg_bad:
        meta["truncation_rejected"] = True
        _set_synth_failure("truncation_rejected")
        logger.warning(
            "[voice_autosend] 疑似截断坏音(%s) → 拒发回落文字 pid=%s",
            _qg_why, persona_id)
        try:
            from src.ai.avatar_voice_stats import get_avatar_voice_stats
            get_avatar_voice_stats().record_truncation_reject()
        except Exception:
            pass
        try:
            os.unlink(result.audio_path)
        except Exception:
            pass
        return None, meta

    audio_path = result.audio_path
    try:
        from src.client.voice_sender import convert_to_ogg_opus
        converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path, delete_src=True)
        if converted:
            return converted, meta
    except Exception:
        logger.debug("[voice_autosend] OGG 转码失败，按原格式", exc_info=True)
    return audio_path, meta


async def stage_voice_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    persona_id: str,
    text: str,
    *,
    out_dir: Optional[str] = None,
    contact_key: Optional[str] = None,
) -> Optional[Tuple[str, str, VoiceStageMeta]]:
    """合成语音并落到出站媒体目录，返回 ``(本地路径, /static URL, meta)``；失败 None。

    调用方据此 ``orch.send_media(media_path=local, media_url=url, media_type="voice")``。
    ``contact_key``（端用户身份）传入后按会员档分层路由 TTS 后端（默认 None=不路由）。
    """
    od = out_dir or str(Path(tempfile.gettempdir()) / "autosend_voice")
    audio_path, meta = await _synth_ogg(
        config, persona_id, text, out_dir=od, contact_key=contact_key,
        platform=platform, account_id=account_id)
    if not audio_path:
        _set_synth_failure("empty_audio")
        return None
    try:
        with open(audio_path, "rb") as fh:
            data = fh.read()
    except Exception:
        logger.debug("[voice_autosend] 读取合成音频失败", exc_info=True)
        _set_synth_failure("read_audio_failed")
        return None
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass
    if not data:
        _set_synth_failure("empty_audio_bytes")
        return None
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(audio_path), data)
        return (local, url, meta)
    except Exception:
        logger.debug("[voice_autosend] 落出站媒体失败", exc_info=True)
        _set_synth_failure("save_media_failed")
        return None


__all__ = [
    "resolve_voice_autosend_cfg", "decide_voice", "should_send_voice",
    "persona_allowed_for_voice", "stage_voice_file", "VoiceStageMeta",
    "record_voice_sent", "record_voice_fallback", "record_voice_decision",
    "metrics_snapshot", "no_edge_fallback_enabled", "preflight_voice_synth",
    "pop_synth_failure_reason", "defer_during_image_enabled",
    "resolve_no_edge_fallback", "resolve_defer_during_image",
    "should_reject_voice_tts_result", "is_edge_fallback_result",
]
