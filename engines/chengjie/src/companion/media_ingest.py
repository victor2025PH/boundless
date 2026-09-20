"""相册照片上传预处理（实施90）：EXIF 抽取→剥离 + 拍摄地/季节提示（纯函数为主）。

两个动机，一次遍历同时满足：

1. **隐私**：真人素材照的 EXIF（GPS 坐标/拍摄时间/机型）此前原样落盘并经
   ``/static`` 直服——下载方可还原拍摄者行踪。照片入库前一律剥离元数据
   （方向位先应用再剥，防剥完横躺）。
2. **数据**：被剥掉的 EXIF 恰是季节/地点的最高置信数据源——拍摄月 + GPS
   （反查半球与国别）在丢弃前抽成**结论级提示**（只留 月份/国别/季节，
   绝不外泄原始坐标），供自动打标（VLM 结论的旁证/补缺）。

保守语义：任何解析失败回落原字节 + 空提示（上传永不因预处理受阻）；
GIF（多为动图）不做重编码剥离——PIL 重存会破坏动画，且 GIF 几乎不带 EXIF。
季节提示只有 GPS 在场（半球可知）才推导——只凭月份猜半球会把悉尼一月
标成冬天（错误的高置信比没有更糟）。
"""
from __future__ import annotations

import io
import logging
import math
import re
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# EXIF tag ids（PIL 数字键）：DateTimeOriginal 常见两种落位（Exif IFD 0x9003 /
# 平铺进 base），另收 base DateTime 0x0132 兜底；GPS IFD 指针 0x8825；方向位 0x0112。
_TAG_DT_ORIGINAL = 0x9003
_TAG_DT = 0x0132
_IFD_EXIF = 0x8769
_IFD_GPS = 0x8825
_TAG_ORIENTATION = 0x0112

_DT_RE = re.compile(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})")

# GPS→国别：最近预置城市 ≤ 此距离才采信（CITY_PRESETS 覆盖主要运营城市，
# 150km 能盖住都会圈；太远宁可不判——反查国界不是本模块的事）。
_GPS_COUNTRY_MAX_KM = 150.0


def _parse_dt(v: Any) -> Tuple[int, int]:
    """EXIF 时间串 → (year, month)；解析不了 (0, 0)。"""
    m = _DT_RE.match(str(v or "").strip())
    if not m:
        return 0, 0
    try:
        y, mo = int(m.group(1)), int(m.group(2))
    except ValueError:
        return 0, 0
    if not (1 <= mo <= 12) or y < 1990:
        return 0, 0
    return y, mo


def _rational(v: Any) -> float:
    try:
        if isinstance(v, tuple) and len(v) == 2:
            return float(v[0]) / float(v[1] or 1)
        return float(v)
    except Exception:
        return 0.0


def _gps_deg(triplet: Any, ref: Any) -> Optional[float]:
    """GPS 度分秒三元组 + 半球引用 → 十进制度；坏数据 None。"""
    try:
        parts = list(triplet)
        if len(parts) < 1:
            return None
        deg = _rational(parts[0])
        mins = _rational(parts[1]) if len(parts) > 1 else 0.0
        secs = _rational(parts[2]) if len(parts) > 2 else 0.0
        val = deg + mins / 60.0 + secs / 3600.0
        if str(ref or "").strip().upper() in ("S", "W"):
            val = -val
        if not (-180.0 <= val <= 180.0):
            return None
        return val
    except Exception:
        return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def gps_to_country(lat: Any, lon: Any) -> str:
    """GPS 坐标 → 最近预置城市（≤150km）的国别码；覆盖不到返回 ""（不猜）。"""
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return ""
    try:
        from src.companion.persona_location import CITY_PRESETS
    except Exception:
        return ""
    best_cc, best_km = "", 1e9
    for info in CITY_PRESETS.values():
        try:
            d = _haversine_km(la, lo, float(info["lat"]), float(info["lon"]))
        except Exception:
            continue
        if d < best_km:
            best_km, best_cc = d, str(info.get("country") or "")
    return best_cc if best_km <= _GPS_COUNTRY_MAX_KM else ""


def derive_exif_hints(
    year: int, month: int, lat: Optional[float], lon: Optional[float],
) -> Dict[str, Any]:
    """EXIF 原始值 → 结论级提示（纯函数）。

    返回 ``{"year", "month", "country", "season"}``：country/season 只有 GPS
    在场才推导（半球已知）；season 另需月份。绝不携带原始坐标。
    """
    out: Dict[str, Any] = {
        "year": int(year or 0), "month": int(month or 0),
        "country": "", "season": "",
    }
    if lat is None or lon is None:
        return out
    out["country"] = gps_to_country(lat, lon)
    if out["month"]:
        try:
            import datetime as _dt
            from src.companion.media_taxonomy import season_for
            hemi = "south" if float(lat) < 0 else "north"
            out["season"] = season_for(
                _dt.datetime(out["year"] or 2000, out["month"], 15),
                hemisphere=hemi)
        except Exception:
            out["season"] = ""
    return out


def _extract_exif(im: Any) -> Dict[str, Any]:
    """PIL Image → {year, month, lat, lon, orientation}（全软失败）。"""
    out: Dict[str, Any] = {"year": 0, "month": 0, "lat": None, "lon": None,
                           "orientation": 1}
    try:
        ex = im.getexif()
    except Exception:
        return out
    if not ex:
        return out
    try:
        out["orientation"] = int(ex.get(_TAG_ORIENTATION) or 1)
    except Exception:
        out["orientation"] = 1
    # 拍摄时间：Exif IFD 的 DateTimeOriginal → 平铺 0x9003 → base DateTime
    for source in (
        lambda: ex.get_ifd(_IFD_EXIF).get(_TAG_DT_ORIGINAL),
        lambda: ex.get(_TAG_DT_ORIGINAL),
        lambda: ex.get(_TAG_DT),
    ):
        try:
            y, mo = _parse_dt(source())
        except Exception:
            y, mo = 0, 0
        if mo:
            out["year"], out["month"] = y, mo
            break
    try:
        gps = ex.get_ifd(_IFD_GPS)
    except Exception:
        gps = None
    if gps:
        lat = _gps_deg(gps.get(2), gps.get(1))
        lon = _gps_deg(gps.get(4), gps.get(3))
        if lat is not None and lon is not None:
            out["lat"], out["lon"] = lat, lon
    return out


_SAVE_FORMAT = {
    ".jpg": ("JPEG", {"quality": 95}),
    ".jpeg": ("JPEG", {"quality": 95}),
    ".png": ("PNG", {"optimize": True}),
    ".webp": ("WEBP", {"quality": 95, "method": 4}),
}


def process_photo_bytes(data: bytes, ext: str) -> Dict[str, Any]:
    """照片入库预处理：抽 EXIF 提示 → 应用方向位 → 重编码剥元数据。

    返回 ``{"bytes", "stripped", "hints"}``：
    - ``bytes``＝应落盘的字节（失败/GIF/无需处理时＝原字节）；
    - ``stripped``＝是否发生了重编码剥离；
    - ``hints``＝:func:`derive_exif_hints` 结论级提示（永远有键，可能全空）。
    """
    empty = {"bytes": data, "stripped": False,
             "hints": derive_exif_hints(0, 0, None, None)}
    e = str(ext or "").lower()
    if e == ".gif" or e not in _SAVE_FORMAT:
        return empty
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        return empty
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            raw = _extract_exif(im)
            hints = derive_exif_hints(
                raw["year"], raw["month"], raw["lat"], raw["lon"])
            has_exif = bool(im.getexif()) or bool(
                getattr(im, "info", {}).get("exif"))
            needs_transpose = raw.get("orientation", 1) not in (0, 1)
            if not has_exif and not needs_transpose:
                # 本就干净：原字节直存（零画质损失、零重编码）
                return {"bytes": data, "stripped": False, "hints": hints}
            try:
                im = ImageOps.exif_transpose(im)
            except Exception:
                pass
            fmt, kwargs = _SAVE_FORMAT[e]
            if fmt == "JPEG" and im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, fmt, **kwargs)   # 不带 exif/pnginfo ＝ 元数据剥净
            out = buf.getvalue()
            if not out:
                return {"bytes": data, "stripped": False, "hints": hints}
            return {"bytes": out, "stripped": True, "hints": hints}
    except Exception:
        logger.debug("[media_ingest] 照片预处理失败，按原样落盘", exc_info=True)
        return empty


__all__ = [
    "process_photo_bytes", "derive_exif_hints", "gps_to_country",
]
