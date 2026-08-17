"""贴纸规范化管线（纯函数，Pillow；2026-08-17 表情包主线）。

把任意来源图（上传 png/jpg/webp/gif、入站收藏的 webp）统一成**四平台可发**的
规格化产物：

- ``webp``：512×512 透明底方形画布 contain 居中（Telegram 要求至少一边=512、
  WhatsApp 贴纸要求 512×512——方形一次满足两家）；动图保留动画帧（WhatsApp
  原生支持 animated webp）。
- ``png``：静态首帧回退件（LINE/Messenger 等无原生贴纸能力的平台按图片发；
  webp 在个别老客户端兼容性不如 png）。
- ``gif``：仅动图产出（best-effort）——Telegram 对 animated webp 无「视频贴纸」
  语义，动图在 TG 走 ``send_animation(gif)`` 观感最好；源是 GIF 直接保留原字节
  （零转码损失），源是 animated webp 则由帧重组（软失败 None，TG 退静态）。

设计约束：
- 零 IO、零全局态——输入 bytes 输出 bytes，路由/测试可独立驱动；
- Pillow 是 requirements 既有依赖（bazi K 线/persona_media 探测已用），
  ffmpeg **刻意不引入**——帧级操作 PIL 全够，少一个环境依赖面；
- 失败语义＝抛 ``StickerNormalizeError(reason)``，reason 为机器码
  （``not_image`` / ``too_large_input`` / ``encode_failed`` / ``too_large_output``），
  路由层映射 i18n 文案。
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 画布边长：TG（一边须=512、另一边≤512）与 WA（512×512）的交集取方形。
CANVAS = 512
# 输入上限：贴纸源图超过 8MB 基本是误传原图/长视频 gif，直接拒（体验上传前端另有提示）。
MAX_INPUT_BYTES = 8 * 1024 * 1024
# 动图帧数上限：超帧截断（不是拒绝）——聊天贴纸 >120 帧没有正当场景，只有体积灾难。
MAX_FRAMES = 120
# 产物 webp 目标/硬上限：超目标走质量阶梯重编码，仍超硬上限才拒。
TARGET_WEBP_BYTES = 480 * 1024
HARD_WEBP_BYTES = 1024 * 1024

# 允许的上传扩展名（路由层先按它拦一道；真正的判定是 PIL 能否打开）。
ALLOWED_INPUT_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})


class StickerNormalizeError(ValueError):
    """规范化失败；``reason`` 为机器码，供路由映射 i18n。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason or "encode_failed")


def _fit_canvas(frame: Any) -> Any:
    """单帧 → RGBA 512×512 透明画布 contain 居中。"""
    from PIL import Image
    fr = frame.convert("RGBA")
    w, h = fr.size
    if w <= 0 or h <= 0:
        raise StickerNormalizeError("not_image")
    scale = min(CANVAS / w, CANVAS / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    if (nw, nh) != (w, h):
        fr = fr.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    canvas.paste(fr, ((CANVAS - nw) // 2, (CANVAS - nh) // 2), fr)
    return canvas


def _encode_webp(
    frames: List[Any], durations: List[int], *, animated: bool,
) -> bytes:
    """质量阶梯编码 webp（动/静同入口）。超硬上限抛 too_large_output。"""
    for quality in (90, 75, 60):
        buf = io.BytesIO()
        try:
            if animated and len(frames) > 1:
                frames[0].save(
                    buf, format="WEBP", save_all=True,
                    append_images=frames[1:], duration=durations,
                    loop=0, quality=quality, method=4)
            else:
                frames[0].save(buf, format="WEBP", quality=quality, method=4)
        except Exception as exc:  # noqa: BLE001
            raise StickerNormalizeError("encode_failed") from exc
        out = buf.getvalue()
        if len(out) <= TARGET_WEBP_BYTES:
            return out
    if len(out) <= HARD_WEBP_BYTES:
        return out
    # 动图最后一搏：隔帧抽稀再按最低质量走一次（时长语义靠 duration 翻倍保住）
    if animated and len(frames) > 2:
        thin_frames = frames[::2]
        thin_durs = [min(10000, d * 2) for d in durations[::2]]
        buf = io.BytesIO()
        try:
            thin_frames[0].save(
                buf, format="WEBP", save_all=True,
                append_images=thin_frames[1:], duration=thin_durs,
                loop=0, quality=60, method=4)
        except Exception as exc:  # noqa: BLE001
            raise StickerNormalizeError("encode_failed") from exc
        out = buf.getvalue()
        if len(out) <= HARD_WEBP_BYTES:
            return out
    raise StickerNormalizeError("too_large_output")


def _extract_frames(im: Any) -> Tuple[List[Any], List[int]]:
    """逐帧展开动图 → (512 画布帧列表, 每帧时长 ms)。超帧截断。"""
    from PIL import ImageSequence
    frames: List[Any] = []
    durations: List[int] = []
    for idx, frame in enumerate(ImageSequence.Iterator(im)):
        if idx >= MAX_FRAMES:
            break
        frames.append(_fit_canvas(frame))
        try:
            dur = int(frame.info.get("duration", 100) or 100)
        except Exception:
            dur = 100
        durations.append(max(20, min(10000, dur)))
    return frames, durations


def normalize_sticker(data: bytes, *, source_ext: str = "") -> Dict[str, Any]:
    """任意源图 → 规范化贴纸产物。

    返回 ``{"webp": bytes, "png": bytes, "gif": bytes|None,
    "animated": bool, "width": int, "height": int}``；
    width/height 恒为画布边长（512×512）。
    失败抛 :class:`StickerNormalizeError`。
    """
    if not data:
        raise StickerNormalizeError("not_image")
    if len(data) > MAX_INPUT_BYTES:
        raise StickerNormalizeError("too_large_input")
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
    except StickerNormalizeError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise StickerNormalizeError("not_image") from exc

    animated = bool(getattr(im, "is_animated", False)) and \
        int(getattr(im, "n_frames", 1) or 1) > 1
    if animated:
        frames, durations = _extract_frames(im)
    else:
        frames, durations = [_fit_canvas(im)], [100]
    if not frames:
        raise StickerNormalizeError("not_image")

    webp = _encode_webp(frames, durations, animated=animated)

    # png 回退件＝首帧（静态平台/老客户端兜底）
    png_buf = io.BytesIO()
    try:
        frames[0].save(png_buf, format="PNG", optimize=True)
    except Exception as exc:  # noqa: BLE001
        raise StickerNormalizeError("encode_failed") from exc
    png = png_buf.getvalue()

    gif: Optional[bytes] = None
    if animated:
        src_fmt = str(getattr(im, "format", "") or "").upper()
        if src_fmt == "GIF" or str(source_ext or "").lower() == ".gif":
            gif = data  # 源即 GIF：原字节零损保留（TG send_animation 直用）
        else:
            # animated webp 等其它动图源 → 由画布帧重组 GIF（best-effort）
            try:
                gbuf = io.BytesIO()
                frames[0].save(
                    gbuf, format="GIF", save_all=True,
                    append_images=frames[1:], duration=durations,
                    loop=0, disposal=2)
                gif = gbuf.getvalue()
            except Exception:
                logger.debug("[sticker_normalize] GIF 重组失败（TG 将退静态）",
                             exc_info=True)
                gif = None

    return {
        "webp": webp, "png": png, "gif": gif,
        "animated": animated, "width": CANVAS, "height": CANVAS,
    }
