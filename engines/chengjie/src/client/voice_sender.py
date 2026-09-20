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
# 路径解析统一走 ffmpeg_resolver（实施49 P1-11）：客户桌面包把 ffmpeg/ffprobe
# 打进 resources/ffmpeg/，冻结态按相对布局找到包内二进制；内部部署仍回落 PATH。

def _ffmpeg_exe() -> Optional[str]:
    try:
        from src.utils.ffmpeg_resolver import ffmpeg_path
        return ffmpeg_path()
    except Exception:
        return shutil.which("ffmpeg")


def _ffprobe_exe() -> Optional[str]:
    try:
        from src.utils.ffmpeg_resolver import ffprobe_path
        return ffprobe_path()
    except Exception:
        return shutil.which("ffprobe")


def _ffmpeg_available() -> bool:
    return _ffmpeg_exe() is not None


def ogg_opus_duration_ms(path: str) -> Optional[int]:
    """纯 python 解析 Ogg/Opus 时长（毫秒）：末页 granule position / 48k。

    #130（2026-09-01）：ffprobe 缺失的客户机（#101 已实锤存在）对 .ogg 语音
    完全测不出时长 → Telegram 语音条只能靠对端客户端自己猜。Opus 的 granule
    恒为 48kHz 采样单位（RFC 7845 §4），读最后一个 OggS 页头即得真时长——
    零依赖、零解码。非 Ogg/解析失败返回 None（调用方回落其它探测层）。
    """
    p = Path(path)
    try:
        if not p.is_file():
            return None
        with open(p, "rb") as f:
            head = f.read(4)
            if head != b"OggS":
                return None
            # 从文件尾部找最后一个页头（页最大 ~64KB，翻倍窗口保险）
            size = p.stat().st_size
            f.seek(max(0, size - 128 * 1024))
            tail = f.read()
        idx = tail.rfind(b"OggS")
        if idx < 0 or idx + 14 > len(tail):
            return None
        granule = int.from_bytes(tail[idx + 6: idx + 14], "little", signed=True)
        if granule <= 0:
            return None
        return int(round(granule * 1000.0 / 48000.0))
    except Exception:
        return None


def probe_audio_duration_ms(path: str) -> Optional[int]:
    """探测音频时长（毫秒）：ffprobe → 纯 python 兜底（wav/mp3/ogg）。

    LINE 音频消息（``audio``）要求 ``duration`` 毫秒整数；官方通道语音出站据此填值。
    #130：ffprobe 缺失/失败不再直接放弃——wav/mp3 走 tts_pipeline 的纯 python
    解析、ogg 走本模块 granule 解析，让「对端实收时长==工作台时长」不依赖
    客户机装了 ffmpeg 全家桶。全部失败才返回 ``None``。
    """
    p = Path(path)
    if not p.is_file():
        return None
    _probe = _ffprobe_exe()
    if _probe is not None:
        try:
            r = subprocess.run(
                [
                    _probe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(p),
                ],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode == 0:
                secs = float((r.stdout or "").strip() or 0.0)
                if secs > 0:
                    return int(round(secs * 1000))
        except Exception:
            pass
    # 纯 python 兜底（#130）：wav/mp3 复用 tts_pipeline 轻量解析；ogg 读 granule
    try:
        from src.ai.tts_pipeline import compute_audio_duration_sec
        secs, _src = compute_audio_duration_sec(str(p))
        if secs and secs > 0:
            return int(round(secs * 1000))
    except Exception:
        pass
    return ogg_opus_duration_ms(str(p))


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
    _ff = _ffmpeg_exe()
    if _ff is None:
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
                _ff, "-y",
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
    caption: Optional[str] = None,
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
    if caption:
        send_kw["caption"] = str(caption)

    try:
        msg = await _invoke_on_client_loop(client, send_kw)
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


# 语音上传必须跑在 client 自己的 loop 上（2026-08-16 实锤修复）：
# pyrogram 上传走独立媒体 session，session 内部 run_in_executor 的 Future 绑定
# session 创建时所在的 loop——跨 loop await 直接
# 「Task got Future attached to a different loop」（8/13 主动语音 6 次、8/16 A 线
# 分条语音 1 次，均在合成成功后死于上传）。文本 send_message 走主 session 恰好
# 同 loop 所以从不复现，语音是唯一撞上媒体 session loop 亲和的路径。
# 同 loop 时零开销直等（旧行为）；跨 loop 时经 run_coroutine_threadsafe 封送到
# client.loop（与本仓 web 路由/头像下载的既有跨 loop 惯例同款）。
_CROSS_LOOP_SEND_TIMEOUT_SEC = 180.0


async def _invoke_on_client_loop(client: Any, send_kw: dict) -> Any:
    # 2026-08-21 B14：改经 client_bound_loop 解析（session.loop 才是真身，
    # Client.loop 构造/启动分环时撒谎；解析点还会把它扶正——save_file 内部
    # 用 Client.loop 起上传 worker，不扶正则封送对了照样跨环炸 Queue）。
    try:
        from src.integrations.telegram_companion_worker import client_bound_loop
        client_loop = client_bound_loop(client)
    except Exception:
        client_loop = getattr(client, "loop", None)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if (
        client_loop is not None
        and running is not None
        and client_loop is not running
        and getattr(client_loop, "is_running", lambda: False)()
    ):
        logger.info(
            "[voice_sender] cross-loop send → marshalled to client loop")
        fut = asyncio.run_coroutine_threadsafe(
            client.send_voice(**send_kw), client_loop)
        return await asyncio.wait_for(
            asyncio.wrap_future(fut), timeout=_CROSS_LOOP_SEND_TIMEOUT_SEC)
    return await client.send_voice(**send_kw)
