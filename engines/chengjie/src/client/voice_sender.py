"""TelegramVoiceSender — send OGG/Opus voice notes via pyrogram.

Responsibilities:
- OGG/Opus format conversion from mp3/wav via ffmpeg (soft-fail if missing)
- pyrogram ``client.send_voice()`` wrapper
- Temporary file cleanup

Example::

    from src.client.voice_sender import send_telegram_voice
    ok = await send_telegram_voice(client, chat_id, "/tmp/reply.mp3", duration=8)
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── ffmpeg helpers ───────────────────────────────────────────────────────────

def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def probe_audio_duration_ms(path: str) -> Optional[int]:
    """用 ffprobe 探测音频时长（毫秒）。ffprobe 缺失/失败/无效返回 ``None``。

    LINE 音频消息（``audio``）要求 ``duration`` 毫秒整数；官方通道语音出站据此填值。
    """
    if shutil.which("ffprobe") is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(p),
            ],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return None
        secs = float((r.stdout or "").strip() or 0.0)
        if secs <= 0:
            return None
        return int(round(secs * 1000))
    except Exception:
        return None


def convert_to_ogg_opus(
    src_path: str,
    *,
    delete_src: bool = False,
    application: str = "voip",
) -> Optional[str]:
    """Convert an audio file to OGG/Opus using ffmpeg.

    Returns the path to the ``.ogg`` file on success, ``None`` on failure.
    If *delete_src* is ``True`` the original file is removed after conversion.
    If the source is already a real Ogg/Opus (``OggS``+``OpusHead``) the path
    is returned unchanged; a ``.ogg`` suffix alone is not trusted.

    ``application``：``voip``=Telegram 语音条稳态（默认）；``audio``=音乐档，
    频带更宽、听感更「立体」（Phase D 活人感，部分客户端波形同样正常）。
    """
    if not _ffmpeg_available():
        logger.warning("[voice_sender] ffmpeg not found — OGG conversion skipped")
        return None

    src = Path(src_path)
    if not src.is_file():
        logger.warning("[voice_sender] source file not found: %s", src_path)
        return None

    # 仅当魔数确认为 OggS+OpusHead 才原样放行。扩展名 .ogg 但内容是
    # WAV/MP3 的「假 ogg」会骗过旧逻辑，经 Baileys 硬标 opus 后客户侧无法下载
    # （2026-08-04 智拓 .198 事故）。魔数不合 → 继续走重编码。
    if src.suffix.lower() == ".ogg":
        try:
            from src.client.voice_ptt_gate import looks_like_ogg_opus_file
            if looks_like_ogg_opus_file(str(src)):
                return src_path
        except Exception:
            # 闸模块异常时保守重编码，绝不盲信扩展名
            pass

    dst = src.with_suffix(".ogg")
    if dst == src or src.suffix.lower() == ".ogg":
        dst = src.parent / (src.stem + "_opus.ogg")

    try:
        app = str(application or "voip").strip().lower()
        if app not in ("voip", "audio"):
            app = "voip"
        # 48k 采样 + 单声道 + application 档（voip/audio）
        r = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(src),
                "-c:a", "libopus",
                "-b:a", "48k",
                "-ar", "48000",
                "-ac", "1",
                "-application", app,
                "-vbr", "on",
                "-compression_level", "5",
                str(dst),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            logger.warning(
                "[voice_sender] ffmpeg rc=%d: %s",
                r.returncode, (r.stderr or "")[:300],
            )
            return None
        if not dst.is_file() or dst.stat().st_size == 0:
            logger.warning("[voice_sender] ffmpeg produced empty output: %s", dst)
            return None
        if delete_src:
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass
        logger.info("[voice_sender] converted %s → %s", src.name, dst.name)
        return str(dst)
    except subprocess.TimeoutExpired:
        logger.warning("[voice_sender] ffmpeg timed out for %s", src_path)
        return None
    except Exception as ex:
        logger.warning("[voice_sender] ffmpeg error: %s", ex)
        return None


def resolve_opus_application(cfg: Optional[Dict[str, Any]] = None) -> str:
    """``telegram.voice_reply.opus.application`` → ``voip`` | ``audio``（非法回落 voip）。"""
    cfg = cfg or {}
    tg = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    vr = tg.get("voice_reply") if isinstance(tg.get("voice_reply"), dict) else {}
    opus = vr.get("opus") if isinstance(vr.get("opus"), dict) else {}
    app = str(opus.get("application") or "voip").strip().lower()
    return app if app in ("voip", "audio") else "voip"


# ── Main send helper ─────────────────────────────────────────────────────────

async def send_telegram_voice(
    client: Any,
    chat_id: Any,
    audio_path: str,
    *,
    duration: Optional[int] = None,
    reply_to_message_id: Optional[int] = None,
    opus_application: str = "voip",
) -> Any:
    """Send a voice note via ``pyrogram client.send_voice()``.

    Converts to OGG/Opus if needed; falls back to sending the original file
    as an audio document if conversion is unavailable.

    Returns a **truthy** value on success, falsy (``None``/``False``) on
    failure——成功时尽量返回 pyrogram 的 ``Message``（其 ``.id`` 供出站收件箱
    镜像做 platform_msg_id 主键去重，2026-08-02 分条语音逐条镜像需要），拿不到
    Message 时退回 ``True``。旧调用方按 bool 语义使用（``if sent:``）不受影响。
    """
    path = Path(audio_path)
    if not path.is_file():
        logger.error("[voice_sender] audio file not found: %s", audio_path)
        return None

    ogg_path: Optional[str] = None
    cleanup_ogg = False

    if path.suffix.lower() != ".ogg":
        converted = await asyncio.to_thread(
            convert_to_ogg_opus, audio_path, application=opus_application)
        if converted:
            ogg_path = converted
            cleanup_ogg = True
        else:
            ogg_path = audio_path
            logger.warning(
                "[voice_sender] no OGG conversion available, sending %s as-is", path.suffix
            )
    else:
        ogg_path = audio_path

    send_kw: dict = {"chat_id": chat_id, "voice": ogg_path}
    if duration is not None and duration > 0:
        send_kw["duration"] = int(duration)
    if reply_to_message_id is not None:
        send_kw["reply_to_message_id"] = int(reply_to_message_id)

    try:
        msg = await client.send_voice(**send_kw)
        logger.info(
            "[voice_sender] sent voice chat_id=%s file=%s dur=%s",
            chat_id, Path(ogg_path).name, duration,
        )
        # 成功但 API 返回空（极少数分支）也必须回 truthy——绝不把已送达判成失败。
        return msg if msg is not None else True
    except Exception as ex:
        logger.error("[voice_sender] send_voice failed chat_id=%s: %s", chat_id, ex)
        return None
    finally:
        if cleanup_ogg and ogg_path != audio_path:
            try:
                Path(ogg_path).unlink(missing_ok=True)
            except Exception:
                pass
