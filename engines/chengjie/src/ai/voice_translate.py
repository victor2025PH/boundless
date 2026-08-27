"""P58-2：语音转写（ASR）→ 翻译 服务。

复用现有 `AudioPipeline`（faster-whisper / OpenAI ASR，自带 circuit breaker +
在线兜底）做转写，再把文本喂给 `TranslationService`（自动术语强制 + 品牌保护）。
ASR 用量走 P58 通用 `ProviderStats` 的 "asr" namespace；结果按媒体 hash 缓存。

设计要点：
- ``transcribe_fn`` 可注入（异步 ``(path) -> TranscribeResult-like``），单测无需真模型。
- 永不抛异常给上层：失败返回 ``ok=False`` + reason。
- 隐私：不持久化音频字节；临时文件由调用方清理。
"""

from __future__ import annotations

import base64
import logging
import os
import tempfile
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from src.ai.media_text_cache import get_media_text_cache, hash_file
from src.ai.provider_stats import get_provider_stats
from src.ai.translation_service import TranslationService, detect_language

logger = logging.getLogger(__name__)

_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25MB（与常见在线 ASR 上限一致）
_ALLOWED_MIME = {
    "audio/ogg", "audio/opus", "audio/mpeg", "audio/mp3", "audio/mp4",
    "audio/m4a", "audio/x-m4a", "audio/wav", "audio/x-wav", "audio/webm",
    "audio/amr", "audio/aac",
}
_MIME_SUFFIX = {
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a", "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/webm": ".webm", "audio/amr": ".amr", "audio/aac": ".aac",
}

# transcribe_fn: async (path) -> result with .ok/.text/.language/.latency_ms/.model/.extra
TranscribeFn = Callable[[str], Awaitable[Any]]


def decode_audio_to_temp(audio_b64: str) -> Tuple[Optional[str], str]:
    """把（可带 data URL 头的）base64 音频落临时文件。返回 (path|None, reason)。"""
    raw = str(audio_b64 or "").strip()
    if not raw:
        return None, "empty"
    mime = "audio/ogg"
    if raw.startswith("data:"):
        header, _, payload = raw.partition(",")
        if not payload:
            return None, "bad_data_url"
        mime = header[5:].split(";")[0].strip().lower() or mime
        raw = payload
    if mime not in _ALLOWED_MIME:
        return None, f"unsupported_mime:{mime}"
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        return None, "decode_failed"
    if not data:
        return None, "empty_after_decode"
    if len(data) > _MAX_AUDIO_BYTES:
        return None, "too_large"
    suffix = _MIME_SUFFIX.get(mime, ".ogg")
    try:
        fd, path = tempfile.mkstemp(prefix="voicexl_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path, "ok"
    except Exception as exc:  # noqa: BLE001
        return None, f"write_failed:{type(exc).__name__}"


# ── P6（2026-08-18）：ASR 缓存学会带分段 ─────────────────────────────────
# 缓存值仍是字符串（MediaTextCache 契约不动）：带分段的条目存版本化 JSON 信封
# （前缀识别），旧纯文本值原样兼容。修「同一音频第一次上传有字幕、第二次上传
# （缓存命中）字幕消失」的语义不一致。
_CACHE_SEG_PREFIX = "ASRJ1:"


def _cache_pack(text: str, segments: list) -> str:
    """有分段 → 信封；无分段 → 纯文本（与历史格式一致，别的消费者零感知）。"""
    if not segments:
        return text
    import json as _json
    try:
        return _CACHE_SEG_PREFIX + _json.dumps(
            {"text": text, "segments": segments}, ensure_ascii=False)
    except Exception:
        return text


def _cache_unpack(raw: Any) -> Tuple[str, list]:
    """返回 (text, segments)。坏信封 → ("", [])＝调用方按 miss 重跑（绝不把信封串当转写）。"""
    s = str(raw or "")
    if not s.startswith(_CACHE_SEG_PREFIX):
        return s, []
    import json as _json
    try:
        d = _json.loads(s[len(_CACHE_SEG_PREFIX):])
        text = str(d.get("text") or "")
        segs = [x for x in (d.get("segments") or []) if isinstance(x, dict)]
        return text, segs
    except Exception:
        return "", []


class VoiceTranslateService:
    def __init__(self, translation_service: TranslationService, transcribe_fn: TranscribeFn) -> None:
        self._xlate = translation_service
        self._transcribe = transcribe_fn

    async def translate_voice(
        self,
        audio_path: str,
        *,
        target_lang: str = "zh",
        source_lang: str = "",
        style: str = "chat",
        want_segments: bool = False,
    ) -> Dict[str, Any]:
        """``want_segments=True``＝调用方需要分段（SRT）：缓存命中但缺分段时按 miss
        重跑一次 ASR 并升级缓存信封（transcribe fn 也须以 want_segments 构建）。"""
        stats = get_provider_stats("asr", "asr")
        cache = get_media_text_cache()
        h = hash_file(audio_path)
        ck = f"asr:{h}" if h else ""
        cached = cache.get(ck) if ck else None

        transcript = ""
        language = ""
        asr_model = "cache"
        asr_cached = False
        segments: list = []   # P3：分段时间戳（鲜 ASR opt-in 或 P6 信封命中时非空）
        if cached is not None:
            c_text, c_segs = _cache_unpack(cached)
            if c_text and (c_segs or not want_segments):
                transcript, asr_cached, segments = c_text, True, c_segs
            else:
                cached = None   # 要分段但缓存没有 / 坏信封 → 按 miss 重跑升级
        if cached is None:
            t0 = time.monotonic()
            try:
                rv = await self._transcribe(audio_path)
            except Exception as exc:  # noqa: BLE001
                stats.record("asr", ok=False, latency_ms=int((time.monotonic() - t0) * 1000))
                logger.warning("ASR 调用异常: %s", exc)
                return {"ok": False, "reason": "asr_error", "asr_tag": f"error:{type(exc).__name__}"}

            transcript = (getattr(rv, "text", "") or "").strip()
            language = getattr(rv, "language", "") or ""
            asr_model = getattr(rv, "model", "") or "asr"
            lat = getattr(rv, "latency_ms", None)
            if not isinstance(lat, int):
                lat = int((time.monotonic() - t0) * 1000)
            ok = bool(getattr(rv, "ok", False) and transcript)
            stats.record(asr_model, ok=ok, latency_ms=lat)
            extra = getattr(rv, "extra", None) or {}
            if extra.get("fallback_used"):
                stats.record_fallback()
            segments = list(extra.get("segment_list") or [])
            if not ok:
                reason = "no_speech" if getattr(rv, "ok", False) else "asr_failed"
                return {"ok": False, "reason": reason,
                        "asr_error": (getattr(rv, "error", "") or "")[:200],
                        "transcript": ""}
            if transcript and ck:
                cache.put(ck, _cache_pack(transcript, segments))

        if not transcript:
            return {"ok": False, "reason": "no_speech", "transcript": ""}

        src = source_lang or language or detect_language(transcript)
        result = await self._xlate.translate(
            transcript, target_lang=target_lang, source_lang=src, style=style,
        )
        out: Dict[str, Any] = {
            "ok": bool(result.ok),
            "transcript": transcript,
            "asr_language": language,
            "asr_model": asr_model,
            "asr_cached": asr_cached,
            "source_lang": result.source_lang,
            "translation": result.to_dict(),
        }
        if segments:
            out["segments"] = segments   # SRT 消费方（P4）；P6 起信封命中的缓存路径也带
        return out


# P4-fix（2026-08-18）：**刻意不用全局单例** get_audio_pipeline——那是「首调定型」
# 语义，而 RPA 各链会拿自己的嵌套 audio_pipeline 配置（实例里全是 enabled:false 的
# 死默认）先调即抢注，统一收件箱语音翻译整个进程周期被禁用态单例反杀。此处按
# **配置指纹**缓存独立管线：openai 后端＝轻 HTTP 客户端零成本；faster_whisper
# 同配置仍只载一次模型。双向隔离，谁也污染不了谁。
_AP_CACHE: Dict[str, Any] = {}
_AP_CACHE_MAX = 8


def _pipeline_for(audio_cfg: Dict[str, Any]):
    import hashlib
    import json as _json

    key = hashlib.sha1(_json.dumps(
        audio_cfg or {}, sort_keys=True, default=str).encode()).hexdigest()
    ap = _AP_CACHE.get(key)
    if ap is None:
        from src.ai.audio_pipeline import AudioPipeline
        if len(_AP_CACHE) >= _AP_CACHE_MAX:
            _AP_CACHE.clear()   # 配置变体极少；粗暴清空防理论上的无界增长
        ap = AudioPipeline(dict(audio_cfg or {}))
        _AP_CACHE[key] = ap
    return ap


def resolve_effective_audio_cfg(full_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """翻译工具链的**有效** ASR 配置（B27 2026-08-21，内测实录「处理失败:
    语音转写未启用 (config.audio_pipeline.enabled)」）。

    客户包顶层 ``audio_pipeline`` 是死默认 ``enabled:false``（重启无效），而托管
    ASR（``hosted_gateway.ensure_hosted_asr``）只注 ``voice_recognition`` 段——
    收件箱语音消息识别好好的，翻译工具却报「未启用」。此处收口三条路由共用的
    配置解析：

    - ``audio_pipeline.enabled`` 开 → 原样返回（内部部署/自配管线零变化）；
    - 关，但 ``voice_recognition._hosted_asr`` 标记在（ensure_hosted_asr 注入，
      热重载经 env 回放重放，标记即真相）→ 从注入形状派生网关转写配置：
      gateway-first 形态取主位 base_url/model，LAN-first 形态取 fallback 里的
      ``_hosted_asr_entry``；设备令牌**当次**从 ``AITR_HOSTED_AI_KEY`` 解析
      （AudioPipeline 的 openai 后端不认 "hosted" 占位——它把 api_key 原样塞给
      OpenAI 客户端；令牌 30 天换新，派生按请求发生 + _AP_CACHE 按配置指纹分桶，
      换新自然换管线）；
    - 两者皆无 / 令牌未领 → ``{}``（调用方按未接入处理，报错走人话 i18n）。
    """
    full = full_cfg or {}
    ap = full.get("audio_pipeline")
    ap = dict(ap) if isinstance(ap, dict) else {}
    if ap.get("enabled", False):
        return ap
    vr = full.get("voice_recognition")
    vr = vr if isinstance(vr, dict) else {}
    if not vr.get("_hosted_asr"):
        return {}
    hosted_base = ""
    hosted_model = ""
    if str(vr.get("api_key") or "").strip().lower() == "hosted":
        # gateway-first：主位就是网关
        hosted_base = str(vr.get("base_url") or "").strip()
        hosted_model = str(vr.get("model") or "").strip()
    else:
        # LAN-first：网关在 fallback 链里（_hosted_asr_entry 标记）
        fb = vr.get("fallback")
        entries = fb if isinstance(fb, list) else ([fb] if isinstance(fb, dict) else [])
        for e in entries:
            if isinstance(e, dict) and e.get("_hosted_asr_entry"):
                hosted_base = str(e.get("base_url") or "").strip()
                hosted_model = str(e.get("model") or "").strip()
                break
    if not hosted_base:
        hosted_base = str(os.environ.get("AITR_HOSTED_ASR_BASE_URL") or "").strip()
    token = str(os.environ.get("AITR_HOSTED_AI_KEY") or "").strip()
    if not hosted_base or not token:
        return {}
    return {
        "enabled": True,
        "backend": "openai",
        "base_url": hosted_base.rstrip("/"),
        "api_key": token,
        "model": hosted_model or "large-v3-turbo",
        "language": "auto",
        "_derived_hosted_asr": True,
    }


def build_audio_transcribe_fn(audio_cfg: Dict[str, Any],
                              *, want_segments: bool = False) -> TranscribeFn:
    """生产用 transcribe fn：按配置指纹取独立 AudioPipeline（见 _pipeline_for 注释）。

    ``want_segments=True``＝SRT 消费方（视频/音频文件翻译）opt-in 分段时间戳；
    语音主链默认 False 零变化。
    """

    async def _tr(audio_path: str):
        ap = _pipeline_for(audio_cfg)
        return await ap.transcribe_file(audio_path, want_segments=want_segments)

    return _tr


__all__ = [
    "VoiceTranslateService",
    "decode_audio_to_temp",
    "build_audio_transcribe_fn",
    "resolve_effective_audio_cfg",
]
