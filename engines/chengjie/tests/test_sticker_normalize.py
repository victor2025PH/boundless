"""贴纸规范化管线门禁（sticker_normalize，纯函数）。"""
import io

import pytest

from src.inbox.sticker_normalize import (
    CANVAS,
    MAX_INPUT_BYTES,
    StickerNormalizeError,
    normalize_sticker,
)

PIL = pytest.importorskip("PIL", reason="Pillow 未安装（requirements 既有依赖）")
from PIL import Image  # noqa: E402


def _png_bytes(w=300, h=200, color=(255, 0, 0, 255)) -> bytes:
    im = Image.new("RGBA", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _jpg_bytes(w=1024, h=768) -> bytes:
    im = Image.new("RGB", (w, h), (30, 144, 255))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    return buf.getvalue()


def _gif_bytes(frames=4, w=200, h=200) -> bytes:
    ims = [Image.new("RGB", (w, h), (i * 40 % 255, 80, 120)) for i in range(frames)]
    buf = io.BytesIO()
    ims[0].save(buf, format="GIF", save_all=True, append_images=ims[1:],
                duration=80, loop=0)
    return buf.getvalue()


def test_static_png_normalized_to_512_webp_and_png():
    out = normalize_sticker(_png_bytes(), source_ext=".png")
    assert out["animated"] is False
    assert out["gif"] is None
    assert out["width"] == CANVAS and out["height"] == CANVAS
    webp = Image.open(io.BytesIO(out["webp"]))
    assert webp.format == "WEBP" and webp.size == (CANVAS, CANVAS)
    png = Image.open(io.BytesIO(out["png"]))
    assert png.format == "PNG" and png.size == (CANVAS, CANVAS)


def test_jpg_input_ok_and_transparent_padding_squares_canvas():
    out = normalize_sticker(_jpg_bytes(1024, 256), source_ext=".jpg")
    webp = Image.open(io.BytesIO(out["webp"])).convert("RGBA")
    assert webp.size == (CANVAS, CANVAS)
    # 上下留白应为透明（contain 居中；1024x256 → 宽顶满、上下 padding）
    assert webp.getpixel((CANVAS // 2, 4))[3] == 0
    assert webp.getpixel((CANVAS // 2, CANVAS - 4))[3] == 0


def test_animated_gif_keeps_animation_and_original_gif():
    src = _gif_bytes(frames=5)
    out = normalize_sticker(src, source_ext=".gif")
    assert out["animated"] is True
    assert out["gif"] == src, "GIF 源必须零损保留（TG send_animation 直用原件）"
    webp = Image.open(io.BytesIO(out["webp"]))
    assert webp.format == "WEBP"
    assert bool(getattr(webp, "is_animated", False)) is True
    assert int(getattr(webp, "n_frames", 1)) >= 2
    # png 回退件是静态首帧
    png = Image.open(io.BytesIO(out["png"]))
    assert png.format == "PNG" and png.size == (CANVAS, CANVAS)


def test_deterministic_output_for_dedup():
    a = normalize_sticker(_png_bytes(), source_ext=".png")
    b = normalize_sticker(_png_bytes(), source_ext=".png")
    assert a["webp"] == b["webp"], "同源必须产出同字节——sha256 去重的前提"


def test_garbage_rejected_as_not_image():
    with pytest.raises(StickerNormalizeError) as ei:
        normalize_sticker(b"definitely not an image bytes" * 10)
    assert ei.value.reason == "not_image"


def test_empty_rejected():
    with pytest.raises(StickerNormalizeError):
        normalize_sticker(b"")


def test_oversized_input_rejected():
    with pytest.raises(StickerNormalizeError) as ei:
        normalize_sticker(b"\x89PNG" + b"0" * (MAX_INPUT_BYTES + 1))
    assert ei.value.reason == "too_large_input"
