"""入站视频理解（A/B 线共用单一瓶颈）。

抽关键帧 → Vision 看画面 + 抽音轨 → ASR/SER，合并为 ``[视频内容] 画面：… 语音：…``
供 inbound_enrich / ai_client 消费。全部软失败，绝不阻断入站主链路。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_INBOUND_VIDEO_MAX_BYTES = 20 * 1024 * 1024


def resolve_inbound_video_max_bytes(config: Optional[Dict[str, Any]]) -> int:
    """读 ``telegram.inbound_video_max_bytes``；缺省 20MB。"""
    tg = (config or {}).get("telegram") or {}
    try:
        v = int(tg.get("inbound_video_max_bytes") or 0)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return DEFAULT_INBOUND_VIDEO_MAX_BYTES


def tg_has_video_media(message: Any) -> bool:
    return bool(
        getattr(message, "video", None)
        or getattr(message, "video_note", None)
        or getattr(message, "animation", None)
    )


def compose_video_inbound_text(*, caption: str = "", video_desc: str = "") -> str:
    """组装入站视频文本（纯函数）。

    - 纯视频：``[视频内容] …`` 或占位 ``[视频]``
    - 带 caption：caption 保留在前，视频块换行追加（Phase5 caption 视频也抽帧）
    """
    cap = str(caption or "").strip()
    desc = str(video_desc or "").strip()
    if desc:
        block = f"[视频内容] {desc}"
        return f"{cap}\n{block}" if cap else block
    if cap:
        return cap
    return "[视频]"


def vision_usable(vision_config: Optional[Dict[str, Any]]) -> bool:
    vcfg = vision_config or {}
    if not vcfg.get("enabled", True):
        return False
    if str(vcfg.get("provider") or "").lower() == "zhipu":
        return bool(vcfg.get("api_key") or vcfg.get("zhipu_api_key"))
    if vcfg.get("base_url") or vcfg.get("base_urls"):
        return True
    return bool(vcfg.get("api_key"))


async def understand_video_file(
    video_path: str,
    *,
    vision_config: Optional[Dict[str, Any]] = None,
    voice_transcriber: Any = None,
    speech_emotion_config: Optional[Dict[str, Any]] = None,
    voice_recognition_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """理解本地视频文件 → 「画面：… 语音：…」综合描述；都空返回 None。"""
    from src.ai.inbound_video_stats import get_inbound_video_stats

    stats = get_inbound_video_stats()
    stats.record_attempt()
    if not vision_usable(vision_config) and not voice_transcriber:
        stats.record_outcome("no_backend")
        return None

    loop = asyncio.get_event_loop()
    visual_desc = await _video_visual_desc(
        video_path, loop, vision_config or {},
    )
    audio_text, audio_emotion = await _video_audio_understand(
        video_path, loop,
        voice_transcriber=voice_transcriber,
        speech_emotion_config=speech_emotion_config or {},
        voice_recognition_config=voice_recognition_config or {},
    )

    parts: List[str] = []
    if visual_desc:
        parts.append(f"画面：{visual_desc}")
    if audio_text:
        emo = f"（说话语气：{audio_emotion}）" if audio_emotion else ""
        parts.append(f"语音：{audio_text}{emo}")
    if not parts:
        stats.record_outcome("empty")
        return None
    stats.record_outcome("ok")
    return " ".join(parts)[:2400]


async def enrich_tg_video_payload(
    message: Any,
    payload: Dict[str, Any],
    *,
    config: Optional[Dict[str, Any]] = None,
    voice_transcriber: Any = None,
) -> Dict[str, Any]:
    """B 线：已下载视频 media_ref → 补全 payload.text（含 caption 视频）。"""
    from src.ai.inbound_video_stats import get_inbound_video_stats
    from src.integrations.protocol_bridge import static_media_ref_to_path

    stats = get_inbound_video_stats()
    media_type = str((payload or {}).get("media_type") or "").lower()
    media_ref = str((payload or {}).get("media_ref") or "")
    caption = str((payload or {}).get("text") or "").strip()

    if media_type not in ("video", "gif") and not tg_has_video_media(message):
        return payload

    if not media_ref:
        if tg_has_video_media(message):
            stats.record_outcome("oversize_or_skip")
        if not caption:
            payload = dict(payload or {})
            payload["text"] = "[视频]"
        return payload

    path = static_media_ref_to_path(media_ref)
    if not path or not Path(path).exists():
        stats.record_outcome("no_file")
        if not caption:
            payload = dict(payload or {})
            payload["text"] = "[视频]"
        return payload

    cfg = config or {}
    vcfg = cfg.get("vision") or {}
    try:
        desc = await understand_video_file(
            path,
            vision_config=vcfg,
            voice_transcriber=voice_transcriber,
            speech_emotion_config=cfg.get("speech_emotion") or {},
            voice_recognition_config=cfg.get("voice_recognition") or {},
        )
    except Exception:
        logger.debug("[inbound_video] enrich 失败", exc_info=True)
        stats.record_outcome("fail")
        desc = None

    payload = dict(payload or {})
    payload["text"] = compose_video_inbound_text(caption=caption, video_desc=desc or "")
    return payload


async def _video_visual_desc(
    video_path: str, loop, vision_config: Dict[str, Any],
) -> Optional[str]:
    if not vision_usable(vision_config):
        return None
    try:
        from src.utils.video_frames import extract_frames_montage
    except Exception:
        logger.debug("[inbound_video] video_frames 不可用", exc_info=True)
        return None
    try:
        frames = int(vision_config.get("video_frames", 4) or 4)
    except Exception:
        frames = 4
    montage_path = str(Path(video_path).with_suffix(".montage.jpg"))
    try:
        res = await loop.run_in_executor(
            None,
            lambda: extract_frames_montage(video_path, montage_path, frames=frames),
        )
        if not res:
            return None
        _mp, dur, n = res
        v_prompt = (
            vision_config.get("video_prompt")
            or "这是一段视频里按时间先后均匀抽取的若干帧拼成的图（从左到右、从上到下为时间顺序）。"
            "请综合各帧，用中文简要描述这段视频的主要内容、画面里的人/物/场景和正在发生的事；"
            "若有文字/商品/价格也一并读出。不要逐帧罗列，直接给整体概述。"
        )
        from src.vision_client import VisionClient as _VC
        text, tag = await _VC.describe_image_with_ollama_zhipu_fallback(
            vision_config, vision_config, montage_path, prompt=v_prompt,
        )
        if text and text.strip():
            logger.info(
                "[inbound_video] 画面解析成功 tag=%s frames=%s len=%s",
                tag, n, len(text),
            )
            return text.strip()[:1600]
        return None
    except Exception:
        logger.warning("[inbound_video] 画面解析失败", exc_info=True)
        return None
    finally:
        try:
            Path(montage_path).unlink(missing_ok=True)
        except Exception:
            pass


async def _video_audio_understand(
    video_path: str,
    loop,
    *,
    voice_transcriber: Any,
    speech_emotion_config: Dict[str, Any],
    voice_recognition_config: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    if not voice_transcriber:
        return None, None
    try:
        from src.utils.video_frames import extract_audio_wav
    except Exception:
        return None, None
    wav_path = str(Path(video_path).with_suffix(".audio.wav"))
    try:
        got = await loop.run_in_executor(
            None, lambda: extract_audio_wav(video_path, wav_path),
        )
        if not got:
            return None, None
        language = str((voice_recognition_config or {}).get("language") or "auto")
        transcript = None
        try:
            transcript = await voice_transcriber.transcribe_voice_message(
                wav_path, language,
            )
        except Exception:
            logger.warning("[inbound_video] 音轨转写失败", exc_info=True)
        if transcript:
            transcript = str(transcript).strip()[:1200]
        emotion_label = None
        try:
            if speech_emotion_config.get("enabled") and transcript:
                from src.ai.speech_emotion import get_speech_emotion_recognizer
                ser = get_speech_emotion_recognizer(speech_emotion_config)
                res = await ser.recognize_async(wav_path)
                min_conf = float(speech_emotion_config.get("min_confidence", 0.5) or 0.5)
                emo = res.as_emotion_dict(min_confidence=min_conf)
                if emo and emo.get("confident"):
                    emotion_label = emo.get("raw_label") or None
        except Exception:
            logger.debug("[inbound_video] SER 失败（忽略）", exc_info=True)
        return transcript, emotion_label
    finally:
        try:
            Path(wav_path).unlink(missing_ok=True)
        except Exception:
            pass


__all__ = [
    "DEFAULT_INBOUND_VIDEO_MAX_BYTES",
    "resolve_inbound_video_max_bytes",
    "tg_has_video_media",
    "compose_video_inbound_text",
    "vision_usable",
    "understand_video_file",
    "enrich_tg_video_payload",
]
