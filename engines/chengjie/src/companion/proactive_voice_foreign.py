"""主动开场外语语音（Phase15）：非中文会话用 edge_tts 多语神经声，不占 7852 克隆 GPU。

克隆声只有人设中文参考音；主动开场文案已是客户语言（build_proactive_prompt），
此处用 edge 把**同一句外文稿**念出来——比纯文本更活，又避免中文语音露馅。
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.ai.lang_voice_route import EDGE_VOICE_BY_LANG as _SHARED_EDGE_VOICES

logger = logging.getLogger(__name__)

# BCP47 前缀 → edge 默认神经声：单一事实源在 lang_voice_route.EDGE_VOICE_BY_LANG
# （follow_text 出站路由与主动外语开场共用一张表；此处剔除 zh——本模块语义是
# 「非中文会话才用 edge 外语声」，zh 由克隆链负责）。
_DEFAULT_EDGE_VOICE: Dict[str, str] = {
    k: v for k, v in _SHARED_EDGE_VOICES.items() if k != "zh"
}

_ZH_PREFIXES = frozenset(
    ("zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "unknown", ""))


def peer_lang_prefix(peer_language: str) -> str:
    return str(peer_language or "").strip().lower().split("-")[0]


def is_chinese_peer_language(peer_language: str) -> bool:
    pl = str(peer_language or "").strip().lower()
    if not pl or pl in _ZH_PREFIXES:
        return True
    return pl.startswith("zh")


def resolve_foreign_voice_cfg(
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """``companion.proactive_topic.voice.foreign`` 块。"""
    try:
        v = (((config or {}).get("companion") or {}).get("proactive_topic") or {}).get("voice") or {}
        fb = v.get("foreign") if isinstance(v.get("foreign"), dict) else {}
        return dict(fb or {})
    except Exception:
        return {}


def foreign_voice_allowed(
    foreign_cfg: Dict[str, Any],
    peer_language: str,
) -> bool:
    if not bool((foreign_cfg or {}).get("enabled", False)):
        return False
    if is_chinese_peer_language(peer_language):
        return False
    prefix = peer_lang_prefix(peer_language)
    allow = foreign_cfg.get("languages")
    if isinstance(allow, str):
        allow = [allow]
    if allow:
        allowed = {str(x).strip().lower().split("-")[0] for x in allow if str(x).strip()}
        return prefix in allowed
    return prefix in _DEFAULT_EDGE_VOICE


def pick_edge_voice(foreign_cfg: Dict[str, Any], peer_language: str) -> str:
    prefix = peer_lang_prefix(peer_language)
    overrides = (foreign_cfg or {}).get("edge_voices") or {}
    if isinstance(overrides, dict):
        v = overrides.get(prefix) or overrides.get(peer_language)
        if v:
            return str(v)
    return _DEFAULT_EDGE_VOICE.get(prefix, "en-US-JennyNeural")


async def stage_foreign_voice_file(
    config: Dict[str, Any],
    platform: str,
    account_id: str,
    text: str,
    *,
    peer_language: str,
) -> Optional[Tuple[str, str]]:
    """edge_tts 合成 → OGG → 出站媒体目录。失败 None（调用方回落文本）。"""
    fb = resolve_foreign_voice_cfg(config)
    if not foreign_voice_allowed(fb, peer_language):
        return None
    t = str(text or "").strip()
    if not t:
        return None
    voice = pick_edge_voice(fb, peer_language)
    od = str(Path(tempfile.gettempdir()) / "proactive_foreign_voice")
    try:
        from src.ai.tts_pipeline import TTSPipeline
        tts = TTSPipeline({
            "enabled": True,
            "backend": "edge_tts",
            "voice": voice,
            "format": "mp3",
            "out_dir": od,
            "fallback_on_error": False,
        })
        result = await tts.synthesize(t, timeout_sec=30.0)
    except Exception:
        logger.debug("[proactive] foreign edge TTS 异常", exc_info=True)
        return None
    if not getattr(result, "ok", False) or not getattr(result, "audio_path", ""):
        return None
    audio_path = result.audio_path
    try:
        from src.client.voice_sender import convert_to_ogg_opus
        converted = await asyncio.to_thread(convert_to_ogg_opus, audio_path, delete_src=True)
        if converted:
            audio_path = converted
    except Exception:
        logger.debug("[proactive] foreign OGG 转码失败", exc_info=True)
    try:
        with open(audio_path, "rb") as fh:
            data = fh.read()
    except Exception:
        return None
    finally:
        try:
            os.remove(audio_path)
        except Exception:
            pass
    if not data:
        return None
    try:
        from src.integrations.protocol_bridge import save_outbound_media
        local, url, _mt = save_outbound_media(
            platform, account_id, os.path.basename(audio_path), data)
        return (local, url)
    except Exception:
        logger.debug("[proactive] foreign 落出站媒体失败", exc_info=True)
        return None


__all__ = [
    "is_chinese_peer_language",
    "foreign_voice_allowed",
    "stage_foreign_voice_file",
    "resolve_foreign_voice_cfg",
]
