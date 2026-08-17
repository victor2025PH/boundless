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


# ── C2 跨链路理解缓存（2026-07-22）────────────────────────────────────────
# 背景：同一条入站视频会被**直发线**（protocol_autoreply→media_enrich）和
# **全自动草稿链**（autodraft_helpers）各理解一遍——真机实测同文件 5 秒内两次
# 完整「抽帧+VLM+音轨 ASR」（两次 VLM 输出还不一致）。按「路径+mtime+size」
# 记忆成品描述，第二链路直接复用；短 TTL 防陈旧。进程内 dict + 锁，绝不落盘。
_UNDERSTAND_CACHE: Dict[str, Tuple[float, Optional[str]]] = {}
_UNDERSTAND_CACHE_LOCK = asyncio.Lock()
_UNDERSTAND_CACHE_TTL = 600.0   # 10 分钟：覆盖双链路窗口，也容错人工重放
_UNDERSTAND_CACHE_MAX = 64


def _understand_cache_key(video_path: str) -> str:
    try:
        st = Path(video_path).stat()
        return f"{video_path}|{int(st.st_mtime)}|{st.st_size}"
    except OSError:
        return ""


async def understand_video_file(
    video_path: str,
    *,
    vision_config: Optional[Dict[str, Any]] = None,
    voice_transcriber: Any = None,
    speech_emotion_config: Optional[Dict[str, Any]] = None,
    voice_recognition_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """理解本地视频文件 → 「画面：… 语音：…」综合描述；都空返回 None。

    同一文件（路径+mtime+size）10 分钟内重复调用直接回缓存成品——
    直发线与全自动草稿链各理解一遍的双倍 GPU 消耗由此消除。
    """
    from src.ai.inbound_video_stats import get_inbound_video_stats

    stats = get_inbound_video_stats()
    _ck = _understand_cache_key(video_path)
    if _ck:
        async with _UNDERSTAND_CACHE_LOCK:
            hit = _UNDERSTAND_CACHE.get(_ck)
            if hit is not None and (asyncio.get_event_loop().time() - hit[0]
                                    ) < _UNDERSTAND_CACHE_TTL:
                logger.info("[inbound_video] 命中理解缓存（跨链路复用）")
                # 只记 outcome 不记 attempt：attempts/success_rate 语义保持
                # "真实理解尝试"，缓存命中作为独立观测项。
                stats.record_outcome("cache_hit")
                return hit[1]

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
    result: Optional[str]
    if not parts:
        stats.record_outcome("empty")
        result = None
    else:
        stats.record_outcome("ok")
        result = " ".join(parts)[:2400]

    # 空结果不缓存：可能是后端瞬时不可用，下一链路应有机会重试
    if _ck and result is not None:
        async with _UNDERSTAND_CACHE_LOCK:
            if len(_UNDERSTAND_CACHE) >= _UNDERSTAND_CACHE_MAX:
                _oldest = min(_UNDERSTAND_CACHE.items(), key=lambda kv: kv[1][0])[0]
                _UNDERSTAND_CACHE.pop(_oldest, None)
            _UNDERSTAND_CACHE[_ck] = (asyncio.get_event_loop().time(), result)
    return result


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
    # 多图直喂（2026-08-15，vision.video_multi_image 默认关）：帧列表逐张独立分辨率
    # 进 VLM（qwen*-vl 原生多图），比宫格把 N 帧挤进一张图精细；帧数走独立旋钮
    # video_frames_multi（默认 4）——多图 token 预算必须收在 Ollama /v1 默认
    # n_ctx=4096 内（4×640≈3k tokens），盲目跟随 video_frames 提帧会 400 超窗。
    # 任何失败静默回落下方宫格路径——多图是增强不是替代。
    if vision_config.get("video_multi_image"):
        try:
            frames_multi = int(vision_config.get("video_frames_multi", 4) or 4)
        except Exception:
            frames_multi = 4
        desc = await _video_visual_desc_multi(
            video_path, loop, vision_config, frames=frames_multi)
        if desc:
            return desc
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


async def _video_visual_desc_multi(
    video_path: str, loop, vision_config: Dict[str, Any], *, frames: int,
) -> Optional[str]:
    """多图直喂路径：抽帧列表 → VisionClient.describe_images（仅 OpenAI 兼容端）。

    返回 None＝调用方回落宫格；本函数自身软失败，绝不抛。帧临时目录自清理。
    """
    try:
        from src.utils.video_frames import extract_frames_list
        from src.vision_client import VisionClient as _VC
    except Exception:
        return None
    import shutil
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="vmulti_")
    try:
        res = await loop.run_in_executor(
            None,
            lambda: extract_frames_list(video_path, tmpdir, frames=frames),
        )
        if not res:
            return None
        paths, _dur = res
        if len(paths) < 2:
            return None
        v_prompt = (
            vision_config.get("video_prompt_multi")
            or (f"以下 {len(paths)} 张图片是同一段视频按时间先后顺序均匀抽取的帧"
                "（第一张最早、最后一张最晚）。请综合各帧，用中文简要描述这段视频的"
                "主要内容、画面里的人/物/场景、正在发生的事及其变化；"
                "若有文字/商品/价格也一并读出。不要逐帧罗列，直接给整体概述。")
        )
        cli = _VC(dict(vision_config))
        if not cli.initialize():
            return None
        text = await cli.describe_images(paths, prompt=v_prompt)
        if text and text.strip():
            logger.info(
                "[inbound_video] 画面解析成功(多图直喂) frames=%s len=%s",
                len(paths), len(text),
            )
            return text.strip()[:1600]
        logger.info("[inbound_video] 多图直喂空答/失败，回落宫格路径")
        return None
    except Exception:
        logger.warning("[inbound_video] 多图直喂异常，回落宫格路径", exc_info=True)
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
