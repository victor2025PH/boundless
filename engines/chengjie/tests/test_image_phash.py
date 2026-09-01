"""pHash 近重复指纹门禁（实施90）：重编码/缩放必须落在近重复带内，无关图必须远。"""
import io

import pytest

pytest.importorskip("PIL")
pytest.importorskip("numpy")

from PIL import Image, ImageDraw

from src.companion.image_phash import (
    NEAR_DUP_MAX_HAMMING,
    expand_ids_by_phash,
    hamming_hex,
    is_near_dup,
    nearest,
    phash_bytes,
)


def _photo_like(seed: int = 0, size=(640, 480)) -> Image.Image:
    """确定性"照片感"图（渐变 + 几何块），seed 换布局。纯生成零外部素材。"""
    w, h = size
    img = Image.new("RGB", size)
    px = img.load()
    for y in range(h):
        for x in range(0, w, 4):     # 步进 4 提速，渐变仍平滑
            v = (x * 255 // w, y * 255 // h, ((x + y + seed * 37) % 255))
            for dx in range(4):
                if x + dx < w:
                    px[x + dx, y] = v
    d = ImageDraw.Draw(img)
    d.ellipse([60 + seed * 30, 50, 300 + seed * 20, 260], fill=(220, 180, 140))
    d.rectangle([350 - seed * 15, 200 + seed * 25, 590, 430], fill=(40, 90, 160))
    return img


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _jpeg_bytes(img: Image.Image, q: int) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=q)
    return buf.getvalue()


def test_identity_and_format():
    h = phash_bytes(_png_bytes(_photo_like()))
    assert len(h) == 16
    int(h, 16)  # 合法 hex
    assert phash_bytes(_png_bytes(_photo_like())) == h  # 确定性


def test_reencode_and_resize_are_near_dups():
    img = _photo_like()
    h0 = phash_bytes(_png_bytes(img))
    h_jpeg = phash_bytes(_jpeg_bytes(img, 60))
    assert hamming_hex(h0, h_jpeg) <= NEAR_DUP_MAX_HAMMING
    half = img.resize((320, 240), Image.LANCZOS)
    h_half = phash_bytes(_jpeg_bytes(half, 80))
    assert hamming_hex(h0, h_half) <= NEAR_DUP_MAX_HAMMING


def test_different_images_are_far():
    h0 = phash_bytes(_png_bytes(_photo_like(0)))
    h1 = phash_bytes(_png_bytes(_photo_like(5)))
    assert hamming_hex(h0, h1) > NEAR_DUP_MAX_HAMMING
    assert not is_near_dup(h0, h1)


def test_invalid_inputs_soft():
    assert phash_bytes(b"") == ""
    assert phash_bytes(b"not an image at all") == ""
    assert hamming_hex("", "0" * 16) == 999
    assert hamming_hex("zz" * 8, "0" * 16) == 999
    assert not is_near_dup("", "")


def test_nearest():
    img = _photo_like()
    h0 = phash_bytes(_png_bytes(img))
    cands = {
        "a": phash_bytes(_jpeg_bytes(img, 55)),
        "b": phash_bytes(_png_bytes(_photo_like(7))),
        "c": "",  # 无指纹条目被跳过
    }
    cid, dist = nearest(h0, cands)
    assert cid == "a" and dist <= NEAR_DUP_MAX_HAMMING
    assert nearest(h0, {}) == ("", 999)


def test_expand_ids_by_phash_family():
    img = _photo_like()
    rows = [
        {"id": "orig", "phash": phash_bytes(_png_bytes(img))},
        {"id": "reenc", "phash": phash_bytes(_jpeg_bytes(img, 50))},
        {"id": "other", "phash": phash_bytes(_png_bytes(_photo_like(9)))},
        {"id": "nohash", "phash": ""},
    ]
    fam = expand_ids_by_phash(rows, {"orig"})
    assert fam == {"orig", "reenc"}
    # seed 无指纹 → 只按原 id（软降级）
    rows2 = [{"id": "x", "phash": ""}, {"id": "y", "phash": rows[0]["phash"]}]
    assert expand_ids_by_phash(rows2, {"x"}) == {"x"}
    assert expand_ids_by_phash(rows, set()) == set()
