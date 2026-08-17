"""协议语音条（PTT）格式硬闸 —— 纯函数为主，零网络。

背景（2026-08-04 智拓机 .198 事故）
====================================
WhatsApp Baileys ``/send-media`` 对 ``media_type=voice`` **硬编码**
``mimetype: audio/ogg; codecs=opus`` + ``ptt: true``。上游
``voice_autosend`` 在 ``convert_to_ogg_opus`` 失败时却静默回落 WAV/MP3
仍按 voice 发出 → 服务端建得了气泡，客户手机解码失败弹「无法下载音频」。

本模块把「可当 PTT 发出」收成单一事实源：
- 魔数快检（``OggS`` + ``OpusHead``）——桌面打包态无 ffprobe CLI 也能拦
- 可选 ffprobe 复核（有则用，无则 fail-open 只认魔数）
- ``ensure_ptt_ogg``：转码 + 校验；**失败一律 None**，禁止原格式 passthrough

B 线 autosend / 编排器 WA 出站 / Baileys 边车共用同一判据口径。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 经 Baileys / Cloud API 以 PTT 语音条发出的平台——格式不合规 = 客户侧死气泡。
# Telegram 原生 pyrogram send_voice 也偏好 ogg/opus，B 线统一走本闸（宁回落文字）。
PTT_STRICT_PLATFORMS = frozenset({
    "whatsapp", "whatsapp_cloud", "telegram", "line", "messenger", "instagram",
})

_MIN_PTT_BYTES = 64


def looks_like_ogg_opus(data: bytes) -> bool:
    """字节级快检：Ogg 容器 + OpusHead（不依赖 ffprobe）。

    OpusHead 通常落在首个 / 前几个 page 内；扫前 512 字节覆盖正常封装。
    """
    if not data or len(data) < _MIN_PTT_BYTES:
        return False
    if data[:4] != b"OggS":
        return False
    window = data[: min(len(data), 512)]
    return b"OpusHead" in window


def looks_like_ogg_opus_file(path: str) -> bool:
    """读文件头做魔数快检；缺文件 / 读失败 → False。"""
    p = Path(path or "")
    if not p.is_file():
        return False
    try:
        with p.open("rb") as fh:
            head = fh.read(512)
    except Exception:
        return False
    return looks_like_ogg_opus(head)


def ffprobe_codec_is_opus(path: str) -> Optional[bool]:
    """ffprobe 音频 codec == opus → True；明确其它 → False；不可用/失败 → None。"""
    if shutil.which("ffprobe") is None:
        return None
    p = Path(path or "")
    if not p.is_file():
        return False
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(p),
            ],
            capture_output=True, text=True, timeout=20,
        )
        if r.returncode != 0:
            return None
        codec = (r.stdout or "").strip().lower()
        if not codec:
            return None
        return codec == "opus"
    except Exception:
        return None


def ptt_ready_reason(path: str) -> str:
    """可发 PTT → ``""``；否则返回短 reason（供 metrics / fallback 记账）。"""
    p = Path(path or "")
    if not p.is_file():
        return "ptt_missing_file"
    try:
        size = p.stat().st_size
    except Exception:
        return "ptt_stat_failed"
    if size < _MIN_PTT_BYTES:
        return "ptt_too_small"
    if not looks_like_ogg_opus_file(str(p)):
        # 扩展名是 .ogg 但魔数不对 = 假 ogg（事故放大器）
        if p.suffix.lower() in (".ogg", ".opus"):
            return "ptt_ogg_magic_mismatch"
        return "ptt_not_ogg_opus"
    probed = ffprobe_codec_is_opus(str(p))
    if probed is False:
        return "ptt_codec_not_opus"
    return ""


def is_ptt_ready(path: str) -> bool:
    return ptt_ready_reason(path) == ""


def platform_requires_ptt(platform: str) -> bool:
    return str(platform or "").strip().lower() in PTT_STRICT_PLATFORMS


def ensure_ptt_ogg(
    src_path: str,
    *,
    delete_src: bool = False,
    application: str = "voip",
    platform: str = "",
) -> Tuple[Optional[str], str]:
    """把任意音频收成可发 PTT 的 ogg/opus。

    返回 ``(path, reason)``：
    - 成功：``(ogg_path, "")``
    - 失败：``(None, reason)``——**绝不**回落原 WAV/MP3

    ``platform`` 仅用于日志；闸门对所有 B 线语音出站一视同仁（见模块 docstring）。
    """
    src = Path(src_path or "")
    if not src.is_file():
        return None, "ptt_missing_file"

    # 已合规 → 直接放行（仍过一遍 reason，吃到 ffprobe 否决）
    why = ptt_ready_reason(str(src))
    if not why:
        return str(src), ""

    # convert_to_ogg_opus 已同步：假 .ogg 会重编码到 *_opus.ogg，不再盲信扩展名
    try:
        from src.client.voice_sender import convert_to_ogg_opus
        converted = convert_to_ogg_opus(
            str(src), delete_src=False, application=application)
    except Exception:
        logger.debug("[voice_ptt_gate] convert raised", exc_info=True)
        converted = None

    if not converted:
        if delete_src:
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass
        return None, why if why.startswith("ptt_") else "ptt_convert_failed"

    why2 = ptt_ready_reason(converted)
    if why2:
        try:
            if converted != str(src):
                Path(converted).unlink(missing_ok=True)
        except Exception:
            pass
        if delete_src:
            try:
                src.unlink(missing_ok=True)
            except Exception:
                pass
        return None, why2 or "ptt_post_convert_invalid"

    if delete_src and Path(converted).resolve() != src.resolve():
        try:
            src.unlink(missing_ok=True)
        except Exception:
            pass

    plat = str(platform or "").strip().lower()
    if plat:
        logger.info(
            "[voice_ptt_gate] ready platform=%s file=%s",
            plat, Path(converted).name)
    return converted, ""


__all__ = [
    "PTT_STRICT_PLATFORMS",
    "looks_like_ogg_opus",
    "looks_like_ogg_opus_file",
    "ffprobe_codec_is_opus",
    "ptt_ready_reason",
    "is_ptt_ready",
    "platform_requires_ptt",
    "ensure_ptt_ogg",
]
