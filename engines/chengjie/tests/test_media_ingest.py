"""上传预处理门禁（实施90）：EXIF 抽取→剥离、方向位应用、GPS→国别、缩略图。"""
import io

import pytest

pytest.importorskip("PIL")

from PIL import Image

from src.companion.media_ingest import (
    derive_exif_hints,
    gps_to_country,
    process_photo_bytes,
)
from src.companion.media_probe import make_photo_thumbnail


def _jpeg_with_exif(size=(20, 10), *, dt="2026:01:15 10:00:00",
                    orientation=1) -> bytes:
    img = Image.new("RGB", size, (200, 120, 80))
    ex = Image.Exif()
    ex[0x0132] = dt          # DateTime
    ex[0x9003] = dt          # DateTimeOriginal（平铺落位，常见于手机图）
    ex[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=ex)
    return buf.getvalue()


def test_strip_exif_and_extract_month():
    raw = _jpeg_with_exif()
    out = process_photo_bytes(raw, ".jpg")
    assert out["stripped"] is True
    assert out["hints"]["year"] == 2026 and out["hints"]["month"] == 1
    # 落盘字节必须再无 EXIF
    with Image.open(io.BytesIO(out["bytes"])) as im:
        assert len(dict(im.getexif())) == 0


def test_orientation_applied_before_strip():
    raw = _jpeg_with_exif(size=(20, 10), orientation=6)  # 需转 90°
    out = process_photo_bytes(raw, ".jpg")
    assert out["stripped"] is True
    with Image.open(io.BytesIO(out["bytes"])) as im:
        assert im.size == (10, 20)


def test_clean_png_fast_path_keeps_bytes():
    img = Image.new("RGB", (16, 16), (10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    raw = buf.getvalue()
    out = process_photo_bytes(raw, ".png")
    assert out["stripped"] is False
    assert out["bytes"] == raw


def test_gif_and_unknown_ext_passthrough():
    img = Image.new("P", (8, 8))
    buf = io.BytesIO()
    img.save(buf, "GIF")
    raw = buf.getvalue()
    out = process_photo_bytes(raw, ".gif")
    assert out["bytes"] == raw and out["stripped"] is False
    out2 = process_photo_bytes(b"whatever", ".bin")
    assert out2["bytes"] == b"whatever" and out2["stripped"] is False


def test_garbage_bytes_soft():
    out = process_photo_bytes(b"not an image", ".jpg")
    assert out["bytes"] == b"not an image"
    assert out["stripped"] is False
    assert out["hints"]["country"] == ""


def test_gps_to_country():
    assert gps_to_country(49.28, -123.12) == "CA"   # 温哥华
    assert gps_to_country(35.68, 139.69) == "JP"    # 东京
    assert gps_to_country(0.0, -160.0) == ""        # 太平洋中间：不猜
    assert gps_to_country("junk", 1) == ""
    assert gps_to_country(999, 0) == ""


def test_derive_exif_hints_season_needs_gps():
    # 有 GPS（北半球）+ 一月 → winter；悉尼一月 → summer
    h1 = derive_exif_hints(2026, 1, 49.28, -123.12)
    assert h1["country"] == "CA" and h1["season"] == "winter"
    h2 = derive_exif_hints(2026, 1, -33.87, 151.21)
    assert h2["country"] == "AU" and h2["season"] == "summer"
    # 无 GPS → 半球未知，绝不猜季节
    h3 = derive_exif_hints(2026, 1, None, None)
    assert h3["season"] == "" and h3["country"] == ""
    assert h3["month"] == 1 and h3["year"] == 2026


def test_make_photo_thumbnail(tmp_path):
    src = tmp_path / "big.png"
    Image.new("RGB", (1200, 800), (90, 90, 200)).save(src)
    out = tmp_path / "big.thumb.webp"
    assert make_photo_thumbnail(str(src), str(out)) is True
    with Image.open(out) as im:
        assert im.format == "WEBP"
        assert max(im.size) <= 480
    # 源缺失 → False 软失败
    assert make_photo_thumbnail(str(tmp_path / "nope.png"),
                                str(tmp_path / "x.webp")) is False
