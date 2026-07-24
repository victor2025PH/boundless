"""出站媒体「去重微扰」（anti-ban）——每次发送生成字节唯一、视觉无差的副本。

背景（2026 业界实证的封号面）：把**同一个文件**（如人设自拍）发给成百上千个联系人，
所有消息的文件哈希完全相同——这是 WhatsApp/Meta 内容模式识别的强垃圾信号。业界通行做法
是「给每次发送的媒体加不可感知的微小变化」，让文件哈希各不相同、破掉这个信号。

本模块正是这一层：``perturb_for_send`` 对**每次发送**产出一个字节唯一、肉眼无差的**临时副本**，
调用方把它当 media_path 交给 worker 发送、发完删除。canonical 的 /static 原图不动（仍供收件箱
展示、且是下次微扰的源）。所以「同一张图发 N 个人 → N 个不同哈希」。

设计铁律：
- **默认关**（``outbound_media.dedup.enabled`` 缺省 false）→ 零行为变更。
- 仅图片族（image/photo/sticker）生效；语音(TTS 本就每次不同)/视频/文件跳过（视频重编码昂贵，
  留作后续）。
- **软失败**：PIL 缺失 / 解码失败 / 任何异常 → 返回原路径、不生成副本，绝不阻断发送。
- **保证哈希真变**：优先注入随机元数据（PNG tEXt / JPEG comment），再叠加几个像素 ±1 抖动，
  最后 md5 校验；仍与原文件同哈希则判失败回落原图（不发一个「没微扰成功」却自以为成功的副本）。
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 可观测（进程内累计）：反封号去重「开了到底在不在工作」此前完全静默——
# 运营开 outbound_media.dedup 后无从知道微扰了几次、回落几次（PIL/ffmpeg 缺失或哈希没变）。
# 这里计数并经 media_capability.media_runtime_signals 透出到自检卡，把静默变可见。
# 只在**已启用且在范围**（真的尝试微扰）时记账；未启用/非媒体不计（那是正常态，非"失败"）。
_METRICS: Dict[str, Any] = {
    "image_perturbed": 0, "video_perturbed": 0,
    "fallback": 0, "fallback_reasons": {}, "last_ts": 0.0,
}
_METRICS_LOCK = threading.Lock()


def _record_perturb(kind: str) -> None:
    try:
        with _METRICS_LOCK:
            key = "video_perturbed" if kind == "video" else "image_perturbed"
            _METRICS[key] = int(_METRICS[key]) + 1
            _METRICS["last_ts"] = time.time()
    except Exception:
        pass


def _record_fallback(reason: str) -> None:
    try:
        with _METRICS_LOCK:
            _METRICS["fallback"] = int(_METRICS["fallback"]) + 1
            r = str(reason or "unknown").strip() or "unknown"
            bucket = _METRICS.setdefault("fallback_reasons", {})
            bucket[r] = int(bucket.get(r, 0)) + 1
            _METRICS["last_ts"] = time.time()
    except Exception:
        pass


def dedup_metrics_snapshot() -> Dict[str, Any]:
    """反封号去重的进程内累计快照（供自检卡/观测）。"""
    with _METRICS_LOCK:
        snap = dict(_METRICS)
        snap["fallback_reasons"] = dict(_METRICS.get("fallback_reasons") or {})
        snap["total_perturbed"] = (
            int(_METRICS["image_perturbed"]) + int(_METRICS["video_perturbed"]))
        return snap


_IMAGE_KINDS = {"image", "photo", "sticker"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
# 视频族走 ffmpeg 流复制（不重编码、秒级无损，只改容器字节）。gif/动态贴纸不在范围
# （PIL 会拍平动画、ffmpeg gif 处理不稳），出站也罕见——保守跳过。
_VIDEO_KINDS = {"video", "video_note"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def dedup_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """读 ``outbound_media.dedup.enabled``（默认 False，opt-in）。"""
    om = ((config or {}).get("outbound_media") or {}).get("dedup") or {}
    return bool(om.get("enabled", False))


def _scope_kind(media_type: str, path: str) -> str:
    """归一去重范围：'image' | 'video' | ''（不处理）。"""
    mt = str(media_type or "").strip().lower()
    if mt in _IMAGE_KINDS:
        return "image"
    if mt in _VIDEO_KINDS:
        return "video"
    if not mt:
        # media_type 缺失时按扩展名兜底
        ext = os.path.splitext(str(path or ""))[1].lower()
        if ext in _IMAGE_EXTS:
            return "image"
        if ext in _VIDEO_EXTS:
            return "video"
    return ""


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _jitter_pixels(img: Any, n: int = 6) -> None:
    """对 n 个随机像素做 ±1 抖动（就地，肉眼不可见）。img 需为可写像素模式。"""
    try:
        px = img.load()
        w, h = img.size
        if w <= 0 or h <= 0:
            return
        bands = len(img.getbands())
        for _ in range(max(1, n)):
            x = secrets.randbelow(w)
            y = secrets.randbelow(h)
            cur = px[x, y]
            if isinstance(cur, tuple):
                lst = list(cur)
                i = secrets.randbelow(len(lst))
                lst[i] = max(0, min(255, lst[i] + (1 if secrets.randbits(1) else -1)))
                px[x, y] = tuple(lst)
            else:  # 单通道（L/P）
                _ = bands
                px[x, y] = max(0, min(255, int(cur) + (1 if secrets.randbits(1) else -1)))
    except Exception:
        logger.debug("[media_dedup] 像素抖动失败（忽略）", exc_info=True)


def _save_with_random_meta(img: Any, out_path: str, ext: str) -> None:
    """按格式保存并注入随机元数据（保证字节唯一，视觉零变化）。"""
    e = ext.lower()
    token = secrets.token_hex(8)
    if e in (".jpg", ".jpeg"):
        q = 90 + secrets.randbelow(7)  # 90..96 随机质量，进一步扰动熵编码
        try:
            img.convert("RGB").save(out_path, format="JPEG", quality=q,
                                    comment=token.encode("ascii"))
        except TypeError:
            img.convert("RGB").save(out_path, format="JPEG", quality=q)
    elif e == ".png":
        try:
            from PIL import PngImagePlugin
            meta = PngImagePlugin.PngInfo()
            meta.add_text("x-nonce", token)
            img.save(out_path, format="PNG", pnginfo=meta)
        except Exception:
            img.save(out_path, format="PNG")
    elif e == ".webp":
        img.save(out_path, format="WEBP", quality=92 + secrets.randbelow(6))
    elif e == ".gif":
        img.save(out_path, format="GIF")
    elif e == ".bmp":
        img.save(out_path, format="BMP")
    else:
        img.save(out_path)


def _perturb_image(src: str, ext: str, src_hash: str) -> Tuple[Optional[str], str]:
    """图片微扰（PIL：随机元数据 + 像素抖动）。返回 (临时副本路径|None, reason)。
    reason 在成功时为 ""；失败时为 no_pillow / hash_unchanged / error（供观测分桶）。"""
    try:
        from PIL import Image
    except Exception:
        logger.debug("[media_dedup] Pillow 不可用，跳过图片微扰")
        return None, "no_pillow"
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="wa_dedup_", suffix=ext, delete=False) as tf:
            tmp_path = tf.name
        for attempt in range(2):  # 最多 2 轮，校验哈希确已变化
            with Image.open(src) as im:
                im = im.convert("RGBA") if (ext == ".png" and im.mode in ("P", "LA")) else im.copy()
                _jitter_pixels(im, n=6 + attempt * 6)
                _save_with_random_meta(im, tmp_path, ext)
            if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0 \
                    and _md5(tmp_path) != src_hash:
                return tmp_path, ""
        _safe_unlink(tmp_path)
        return None, "hash_unchanged"
    except Exception:
        logger.debug("[media_dedup] 图片微扰失败", exc_info=True)
        _safe_unlink(tmp_path)
        return None, "error"


def _perturb_video(src: str, ext: str, src_hash: str) -> Tuple[Optional[str], str]:
    """视频微扰（ffmpeg 流复制 + 随机容器元数据）：**不重编码**，秒级、画质无损，
    只让容器字节唯一。返回 (路径|None, reason)；失败 reason=no_ffmpeg/hash_unchanged/error。"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.debug("[media_dedup] ffmpeg 不可用，跳过视频微扰")
        return None, "no_ffmpeg"
    tmp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="wa_dedup_", suffix=(ext if ext in _VIDEO_EXTS else ".mp4"),
            delete=False) as tf:
            tmp_path = tf.name
        nonce = secrets.token_hex(8)
        # -c copy 流复制（不转码）；改容器级 comment 元数据即足以变哈希、视觉零损。
        # +faststart 让 mp4 moov 前置（也扰动字节布局）；超时护栏防卡死。
        cmd = [
            ffmpeg, "-nostdin", "-y", "-i", src,
            "-map", "0", "-c", "copy", "-map_metadata", "0",
            "-metadata", "comment=" + nonce,
            "-movflags", "+faststart", tmp_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)
        if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0 \
                and _md5(tmp_path) != src_hash:
            return tmp_path, ""
        # +faststart 对非 mp4 容器可能报错/空输出 → 再试一次不带 movflags
        _safe_unlink(tmp_path)
        with tempfile.NamedTemporaryFile(
            prefix="wa_dedup_", suffix=(ext if ext in _VIDEO_EXTS else ".mp4"),
            delete=False) as tf:
            tmp_path = tf.name
        cmd2 = [
            ffmpeg, "-nostdin", "-y", "-i", src,
            "-map", "0", "-c", "copy", "-map_metadata", "0",
            "-metadata", "comment=" + secrets.token_hex(8), tmp_path,
        ]
        subprocess.run(cmd2, capture_output=True, timeout=120)
        if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0 \
                and _md5(tmp_path) != src_hash:
            return tmp_path, ""
        _safe_unlink(tmp_path)
        return None, "hash_unchanged"
    except Exception:
        logger.debug("[media_dedup] 视频微扰失败", exc_info=True)
        _safe_unlink(tmp_path)
        return None, "error"


def perturb_for_send(
    media_path: str, media_type: str, config: Optional[Dict[str, Any]] = None,
) -> Tuple[str, bool]:
    """按需产出一个「视觉无差、字节唯一」的临时副本用于本次发送。

    Returns ``(path_to_send, is_temp)``：
      - 未启用 / 不在范围 / 依赖不可用 / 失败 → ``(原 media_path, False)``（调用方照发原文件）。
      - 成功 → ``(临时副本路径, True)``，调用方发完应删除该临时文件。

    图片走 PIL（元数据 + 像素抖动）；视频走 ffmpeg 流复制（改容器元数据，不重编码）。
    绝不抛异常。
    """
    if not dedup_enabled(config):
        return media_path, False
    src = str(media_path or "")
    if not src or not os.path.isfile(src):
        return media_path, False
    kind = _scope_kind(media_type, src)
    if not kind:
        return media_path, False
    ext = os.path.splitext(src)[1].lower()
    try:
        src_hash = _md5(src)
    except Exception:
        return media_path, False

    if kind == "image":
        out, reason = _perturb_image(src, ext or ".jpg", src_hash)
    else:  # video
        out, reason = _perturb_video(src, ext or ".mp4", src_hash)
    if out:
        _record_perturb(kind)
        logger.debug("[media_dedup] 微扰成功(%s) %s → %s", kind, src, out)
        return out, True
    # 已启用且在范围内却没微扰成功 = 真实回落（记原因，供自检卡暴露"开了但没生效"）
    _record_fallback(reason or "unknown")
    return media_path, False


def _safe_unlink(path: Optional[str]) -> None:
    if not path:
        return
    try:
        if os.path.isfile(path):
            os.remove(path)
    except Exception:
        logger.debug("[media_dedup] 清理临时文件失败（忽略）: %s", path, exc_info=True)


def cleanup_temp(path: Optional[str], is_temp: bool) -> None:
    """发送完成后清理临时副本（is_temp=False 时 no-op）。"""
    if is_temp:
        _safe_unlink(path)


__all__ = [
    "dedup_enabled", "perturb_for_send", "cleanup_temp", "dedup_metrics_snapshot",
]
