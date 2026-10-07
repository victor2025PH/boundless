"""Find a Facebook Like control on a screencap without a fixed coordinate.

Wallpaper 09 (CHINAMI-B, 2026-10-08) shows the post action bar as icons and
counts only: no Like / Comment / Share words. Text is one signal. A tap also
needs a second signal, or a high-confidence thumbs-up template match.

Signals
    text        Like / Gusto / I-like / 赞 on a row that also has Comment and Share
                (Komento / Ibahagi, 评论 / 分享). Liked / Nagustuhan / 已赞 is not a target.
    template    Bundled light/dark thumbs-up rasters, several sizes. Matched by
                normalized correlation against icon blobs. Stdlib only.
    structure   A row of 2–4 evenly spaced icons sitting under a reaction-count
                row (OCR tokens such as 92k / 1.5k, or a shorter glyph row).
                The leftmost slot is the Like candidate.

Tap rule: template score >= TEMPLATE_HIGH, or at least two signals whose
centers agree. Otherwise no target (never the deprecated like_button).

Text engine: optional ``rapidocr-onnxruntime`` (PP-OCRv4 reads Latin and CJK,
so Tagalog and 赞 do not need a Windows OCR language pack). Windows.Media.Ocr
is not used. The import is lazy. A missing text engine leaves the text signal
empty; templates and the action bar still run. ``ocr_unavailable`` is only when
the template pack and the text engine are both missing — that is the case where
the only remaining aim would be the fixed coordinate, which real likes refuse.

Empty feed: after the swipe budget, no count row, no action bar, and no Like
text row. Phrases such as "Something went wrong" / "Stories couldn't load"
mark the same outcome so the scheduler can send the account to warm-up.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TEMPLATE_HIGH = 0.82
TEMPLATE_MED = 0.58
AGREE_X_PM = 72
AGREE_Y_PM = 56
DEFAULT_LIKE_SWIPES = 3
FB_BLUE = (24, 119, 242)
FB_BLUE_TOL = 48

_TEMPLATE_DIR = Path(__file__).with_name("like_templates")
_RAW_ORDERS = {1: "rgba", 2: "rgba", 5: "bgra"}
_LIKE = {"like", "gusto", "i-like", "i like", "赞"}
_LIKED = {"liked", "nagustuhan", "已赞"}
_COMMENT = {"comment", "komento", "评论"}
_SHARE = {"share", "ibahagi", "分享"}
_EMPTY_PHRASES = (
    "something went wrong",
    "stories couldn't load",
    "stories could not load",
    "no posts",
    "nothing to show",
)
_SCALES = (("s", 16, 20), ("m", 20, 26), ("l", 24, 30))


def _norm_token(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    raw = text.strip().casefold().replace("’", "'").replace("`", "'")
    return " ".join(raw.split())


def _count_token(text: str) -> bool:
    s = _norm_token(text).replace(" ", "")
    if not s or not s[0].isdigit():
        return False
    body = s
    if body[-1] in "km":
        body = body[:-1]
    body = body.replace(",", "")
    if body.count(".") > 1:
        return False
    num = body.replace(".", "")
    return bool(num) and num.isdigit() and len(s) <= 8


def _pm(px: float, size: int) -> int:
    if size <= 0:
        return 0
    v = int(round(float(px) * 1000.0 / float(size)))
    if v < 0:
        return 0
    if v > 1000:
        return 1000
    return v


def _margin(size: int) -> int:
    return max(4, int(round(size * 0.005)))


def _y_tol(height: int) -> int:
    return max(8, int(round(height * 0.03)))


def _fully_inside(x0: int, y0: int, x1: int, y1: int, width: int, height: int) -> bool:
    mx, my = _margin(width), _margin(height)
    return x0 >= mx and y0 >= my and x1 <= width - 1 - mx and y1 <= height - 1 - my


def frame_size(raw: bytes) -> Tuple[int, int, int, str]:
    """(width, height, header_len, order). Raises ValueError on a bad screencap."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 12:
        raise ValueError("bad frame")
    w, h, fmt = struct.unpack_from("<III", raw, 0)
    if not (0 < w <= 8192 and 0 < h <= 8192):
        raise ValueError("bad frame")
    hdr = len(raw) - w * h * 4
    order = _RAW_ORDERS.get(fmt)
    if hdr not in (12, 16) or order is None:
        raise ValueError("bad frame")
    return w, h, hdr, order


def _luma_at(raw: bytes, hdr: int, width: int, order: str, x: int, y: int) -> int:
    px = raw[hdr + (y * width + x) * 4: hdr + (y * width + x) * 4 + 3]
    if order == "bgra":
        r, g, b = px[2], px[1], px[0]
    else:
        r, g, b = px[0], px[1], px[2]
    return (int(r) * 3 + int(g) * 4 + int(b) * 1) // 8


def _rgb_at(raw: bytes, hdr: int, width: int, order: str, x: int, y: int) -> Tuple[int, int, int]:
    px = raw[hdr + (y * width + x) * 4: hdr + (y * width + x) * 4 + 3]
    if len(px) < 3:
        raise ValueError("bad frame")
    if order == "bgra":
        return px[2], px[1], px[0]
    return px[0], px[1], px[2]


def _near_blue(rgb: Sequence[int]) -> bool:
    return all(abs(int(a) - int(b)) <= FB_BLUE_TOL for a, b in zip(rgb, FB_BLUE))


def _box_of(item: Any) -> Optional[Tuple[str, int, int, int, int, float]]:
    """Normalize one OCR item to (text, x0, y0, x1, y1, score)."""
    text = ""
    score = 1.0
    x0 = y0 = x1 = y1 = None
    if isinstance(item, dict):
        text = _norm_token(item.get("text") if "text" in item else item.get("label"))
        if "score" in item and isinstance(item.get("score"), (int, float)) and not isinstance(item.get("score"), bool):
            score = float(item["score"])
        if all(k in item for k in ("x0", "y0", "x1", "y1")):
            x0, y0, x1, y1 = item["x0"], item["y0"], item["x1"], item["y1"]
        elif "box" in item:
            return _box_of((item.get("box"), text, score))
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        box, text = item[0], _norm_token(item[1])
        if len(item) >= 3 and isinstance(item[2], (int, float)) and not isinstance(item[2], bool):
            score = float(item[2])
        if isinstance(box, (list, tuple)) and box and isinstance(box[0], (list, tuple)):
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
            x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        elif isinstance(box, (list, tuple)) and len(box) == 4 and all(isinstance(n, (int, float)) for n in box):
            x0, y0, x1, y1 = box
    if not text or score < 0.4 or x0 is None:
        return None
    try:
        a, b, c, d = int(x0), int(y0), int(x1), int(y1)
    except (TypeError, ValueError):
        return None
    if c < a:
        a, c = c, a
    if d < b:
        b, d = d, b
    return text, a, b, c, d, score


def normalize_ocr_boxes(items: Any) -> List[Dict[str, Any]]:
    if items is None:
        return []
    # rapidocr returns (list, elapse) or just a list
    if isinstance(items, tuple) and items and isinstance(items[0], list):
        items = items[0]
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        parsed = _box_of(item)
        if parsed is None:
            continue
        text, x0, y0, x1, y1, score = parsed
        out.append({"text": text, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "score": score})
    return out


def _center(x0: int, y0: int, x1: int, y1: int) -> Tuple[float, float]:
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def text_hits(boxes: Sequence[Dict[str, Any]], width: int, height: int) -> List[Dict[str, Any]]:
    """Like-rows. Each hit is the Like token center. Already-liked rows are omitted."""
    rows: List[List[Dict[str, Any]]] = []
    tol = _y_tol(height)
    usable = []
    for box in boxes:
        if not isinstance(box, dict):
            continue
        token = _norm_token(box.get("text"))
        if token not in _LIKE | _LIKED | _COMMENT | _SHARE:
            continue
        usable.append(box)
    usable.sort(key=lambda b: (int(b["y0"]) + int(b["y1"])) / 2.0)
    for box in usable:
        cy = (int(box["y0"]) + int(box["y1"])) / 2.0
        placed = False
        for group in rows:
            gy = sum((int(b["y0"]) + int(b["y1"])) / 2.0 for b in group) / len(group)
            if abs(cy - gy) <= tol:
                group.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    hits = []
    for group in rows:
        likes = [b for b in group if _norm_token(b.get("text")) in _LIKE]
        if any(_norm_token(b.get("text")) in _LIKED for b in group):
            continue
        comments = [b for b in group if _norm_token(b.get("text")) in _COMMENT]
        shares = [b for b in group if _norm_token(b.get("text")) in _SHARE]
        if not likes or not comments or not shares:
            continue
        like = min(likes, key=lambda b: int(b["x0"]))
        full = all(_fully_inside(int(b["x0"]), int(b["y0"]), int(b["x1"]), int(b["y1"]), width, height)
                   for b in (like, comments[0], shares[0]))
        cx, cy = _center(int(like["x0"]), int(like["y0"]), int(like["x1"]), int(like["y1"]))
        hits.append({
            "source": "text", "x_pm": _pm(cx, width), "y_pm": _pm(cy, height),
            "score": 1.0, "label": _norm_token(like.get("text")), "full": full,
            "x": cx, "y": cy,
        })
    hits.sort(key=lambda h: (h["y_pm"], h["x_pm"]))
    return hits


def _count_row_ys(boxes: Sequence[Dict[str, Any]], height: int) -> List[float]:
    tol = _y_tol(height)
    counts = []
    for box in boxes:
        if isinstance(box, dict) and _count_token(str(box.get("text") or "")):
            counts.append(box)
    counts.sort(key=lambda b: (int(b["y0"]) + int(b["y1"])) / 2.0)
    rows: List[List[Dict[str, Any]]] = []
    for box in counts:
        cy = (int(box["y0"]) + int(box["y1"])) / 2.0
        placed = False
        for group in rows:
            gy = sum((int(b["y0"]) + int(b["y1"])) / 2.0 for b in group) / len(group)
            if abs(cy - gy) <= tol:
                group.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    ys = []
    for group in rows:
        if len(group) < 2:
            continue
        ys.append(sum((int(b["y0"]) + int(b["y1"])) / 2.0 for b in group) / len(group))
    return ys


def _blob_dict(x0: int, y0: int, x1: int, y1: int) -> Dict[str, int]:
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def extract_blobs(raw: bytes) -> List[Dict[str, int]]:
    """Icon-sized edge components. Empty on a flat frame."""
    try:
        width, height, hdr, order = frame_size(raw)
    except ValueError:
        return []
    step = 1
    while width // step > 420:
        step *= 2
    sw, sh = width // step, height // step
    if sw < 16 or sh < 16:
        return []
    gray = [0] * (sw * sh)
    for y in range(sh):
        sy = min(height - 1, y * step)
        row = sy * width
        for x in range(sw):
            sx = min(width - 1, x * step)
            gray[y * sw + x] = _luma_at(raw, hdr, width, order, sx, sy)
    edge = bytearray(sw * sh)
    for y in range(1, sh - 1):
        for x in range(1, sw - 1):
            c = gray[y * sw + x]
            diff = max(
                abs(c - gray[y * sw + x - 1]),
                abs(c - gray[y * sw + x + 1]),
                abs(c - gray[(y - 1) * sw + x]),
                abs(c - gray[(y + 1) * sw + x]),
            )
            if diff >= 26:
                edge[y * sw + x] = 1
    seen = bytearray(sw * sh)
    blobs: List[Dict[str, int]] = []
    min_side = max(6, int(width * 0.012))
    max_side = max(min_side + 4, int(width * 0.14))
    for start, bit in enumerate(edge):
        if not bit or seen[start]:
            continue
        stack = [start]
        seen[start] = 1
        minx = maxx = start % sw
        miny = maxy = start // sw
        area = 0
        while stack:
            i = stack.pop()
            area += 1
            x, y = i % sw, i // sw
            if x < minx:
                minx = x
            if x > maxx:
                maxx = x
            if y < miny:
                miny = y
            if y > maxy:
                maxy = y
            if area > 8000:
                break
            x, y = i % sw, i // sw
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                nx, ny = x + dx, y + dy
                if nx < 0 or ny < 0 or nx >= sw or ny >= sh:
                    continue
                j = ny * sw + nx
                if edge[j] and not seen[j]:
                    seen[j] = 1
                    stack.append(j)
        if area < 8:
            continue
        x0, y0 = minx * step, miny * step
        x1, y1 = min(width - 1, (maxx + 1) * step), min(height - 1, (maxy + 1) * step)
        bw, bh = x1 - x0, y1 - y0
        if bw < min_side or bh < min_side or bw > max_side or bh > max_side:
            continue
        if bh > 0 and not 0.35 <= (bw / bh) <= 2.8:
            continue
        blobs.append(_blob_dict(x0, y0, x1, y1))
    if len(blobs) > 80:
        target = max(min_side * min_side, int((width * 0.045) ** 2))
        blobs.sort(key=lambda b: abs((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) - target))
        blobs = blobs[:80]
    return blobs


def _rows_of(blobs: Sequence[Dict[str, int]], height: int) -> List[List[Dict[str, int]]]:
    tol = max(6, int(height * 0.025))
    ordered = sorted(blobs, key=lambda b: (b["y0"] + b["y1"]) / 2.0)
    rows: List[List[Dict[str, int]]] = []
    for blob in ordered:
        cy = (blob["y0"] + blob["y1"]) / 2.0
        placed = False
        for group in rows:
            gy = sum((b["y0"] + b["y1"]) / 2.0 for b in group) / len(group)
            if abs(cy - gy) <= tol:
                group.append(blob)
                placed = True
                break
        if not placed:
            rows.append([blob])
    for group in rows:
        group.sort(key=lambda b: b["x0"])
    return rows


def _even_icon_row(group: Sequence[Dict[str, int]]) -> bool:
    if not 2 <= len(group) <= 4:
        return False
    widths = [b["x1"] - b["x0"] for b in group]
    heights = [b["y1"] - b["y0"] for b in group]
    if min(widths) <= 0 or min(heights) <= 0:
        return False
    if max(widths) / min(widths) > 1.85 or max(heights) / min(heights) > 1.85:
        return False
    centers = [(b["x0"] + b["x1"]) / 2.0 for b in group]
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    if not gaps or min(gaps) <= max(widths) * 0.6:
        return False
    if max(gaps) / min(gaps) > 1.75:
        return False
    return True


def structure_hits(blobs: Sequence[Dict[str, int]], boxes: Sequence[Dict[str, Any]],
                   width: int, height: int) -> List[Dict[str, Any]]:
    """Leftmost slot of each action bar under a reaction-count row."""
    count_ys = _count_row_ys(boxes, height)
    rows = _rows_of(blobs, height)
    icon_rows = [g for g in rows if _even_icon_row(g)]
    hits = []
    lo = max(8.0, height * 0.012)
    hi = height * 0.14

    def _cy(group: Sequence[Dict[str, int]]) -> float:
        return sum((b["y0"] + b["y1"]) / 2.0 for b in group) / len(group)

    def _mh(group: Sequence[Dict[str, int]]) -> float:
        hs = sorted(b["y1"] - b["y0"] for b in group)
        return float(hs[len(hs) // 2])

    for group in icon_rows:
        cy = _cy(group)
        mh = _mh(group)
        above = list(count_ys)
        for other in rows:
            if other is group or len(other) < 2:
                continue
            gap = cy - _cy(other)
            if lo <= gap <= hi and _mh(other) <= mh * 0.85:
                above.append(_cy(other))
        if not above or not any(lo <= (cy - ay) <= hi for ay in above):
            continue
        if not all(_fully_inside(b["x0"], b["y0"], b["x1"], b["y1"], width, height) for b in group):
            continue
        left = group[0]
        cx, cyy = _center(left["x0"], left["y0"], left["x1"], left["y1"])
        hits.append({
            "source": "structure", "x_pm": _pm(cx, width), "y_pm": _pm(cyy, height),
            "score": 1.0, "label": "icon", "full": True, "x": cx, "y": cyy,
        })
    hits.sort(key=lambda h: (h["y_pm"], h["x_pm"]))
    return hits


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    rows = []
    for y in range(height):
        rows.append(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3])
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
    png += _png_chunk(b"IEND", b"")
    path.write_bytes(png)


def _read_png(data: bytes) -> Tuple[int, int, bytes]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("bad png")
    pos = 8
    width = height = None
    idat = []
    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, color, comp, filt, inter = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or color != 2 or comp or filt or inter:
                raise ValueError("bad png")
        elif tag == b"IDAT":
            idat.append(chunk)
        elif tag == b"IEND":
            break
    if not width or not height or not idat:
        raise ValueError("bad png")
    raw = zlib.decompress(b"".join(idat))
    stride = width * 3
    rgb = bytearray()
    i = 0
    for _y in range(height):
        if i >= len(raw) or raw[i] != 0:
            raise ValueError("bad png")
        i += 1
        rgb += raw[i:i + stride]
        i += stride
    return width, height, bytes(rgb)


def render_thumb(width: int, height: int, *, dark: bool) -> bytes:
    """Small thumbs-up raster. Light: dark glyph on a light pad. Dark: the inverse."""
    bg = (32, 34, 38) if dark else (246, 246, 246)
    ink = (236, 237, 239) if dark else (55, 58, 64)
    out = bytearray()
    for y in range(height):
        ny = (y + 0.5) / height
        for x in range(width):
            nx = (x + 0.5) / width
            on = False
            if 0.40 <= nx <= 0.62 and 0.06 <= ny <= 0.50:
                on = True
            if 0.16 <= nx <= 0.84 and 0.34 <= ny <= 0.74:
                on = True
            if 0.28 <= nx <= 0.72 and 0.66 <= ny <= 0.92:
                on = True
            out += bytes(ink if on else bg)
    return bytes(out)


def write_default_templates(dest: Optional[Path] = None) -> Path:
    folder = dest or _TEMPLATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        for name, w, h in _SCALES:
            rgb = render_thumb(w, h, dark=(theme == "dark"))
            _write_png(folder / f"like_{theme}_{name}.png", w, h, rgb)
    return folder


def load_templates(folder: Optional[Path] = None) -> List[Dict[str, Any]]:
    src = folder or _TEMPLATE_DIR
    found = []
    if not src.is_dir():
        return []
    for path in sorted(src.glob("like_*.png")):
        try:
            w, h, rgb = _read_png(path.read_bytes())
        except (OSError, ValueError, zlib.error):
            continue
        luma = [(rgb[i] * 3 + rgb[i + 1] * 4 + rgb[i + 2]) // 8 for i in range(0, len(rgb), 3)]
        found.append({"name": path.stem, "w": w, "h": h, "luma": luma, "edge": _edge_map(luma, w, h)})
    return found


def templates_available(folder: Optional[Path] = None) -> bool:
    names = {item["name"] for item in load_templates(folder)}
    need = {f"like_{theme}_{name}" for theme in ("light", "dark") for name, _w, _h in _SCALES}
    return need <= names


def _ncc(a: Sequence[int], b: Sequence[int]) -> float:
    n = len(a)
    if n == 0 or n != len(b):
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    va = vb = cov = 0.0
    for x, y in zip(a, b):
        dx, dy = x - ma, y - mb
        va += dx * dx
        vb += dy * dy
        cov += dx * dy
    if va < 1.0 or vb < 1.0:
        return 0.0
    return cov / ((va ** 0.5) * (vb ** 0.5))


def _edge_map(luma: Sequence[int], width: int, height: int) -> List[int]:
    """Horizontal plus vertical gradient. A plain square and a thumb separate here."""
    out = [0] * (width * height)
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            c = int(luma[y * width + x])
            out[y * width + x] = abs(c - int(luma[y * width + x - 1])) + abs(c - int(luma[(y - 1) * width + x]))
    return out


def _gray(raw: bytes) -> Optional[Tuple[int, int, int, str, List[int]]]:
    try:
        width, height, hdr, order = frame_size(raw)
    except ValueError:
        return None
    luma = [0] * (width * height)
    for y in range(height):
        for x in range(width):
            luma[y * width + x] = _luma_at(raw, hdr, width, order, x, y)
    return width, height, hdr, order, luma


def template_hits(raw: bytes, blobs: Sequence[Dict[str, int]],
                  templates: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    decoded = _gray(raw)
    if decoded is None:
        return []
    width, height, _hdr, _order, luma = decoded
    pack = list(templates) if templates is not None else load_templates()
    if not pack:
        return []
    hits = []
    for blob in blobs:
        x0 = max(0, int(blob["x0"]))
        y0 = max(0, int(blob["y0"]))
        x1 = min(width - 1, int(blob["x1"]))
        y1 = min(height - 1, int(blob["y1"]))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        cx, cy = _center(x0, y0, x1, y1)
        best = 0.0
        best_name = ""
        best_xy = (cx, cy)
        for tmpl in pack:
            tw, th = int(tmpl["w"]), int(tmpl["h"])
            # Exact-size windows around the blob center. A loose edge box is
            # not resized onto the template; that washes the score out.
            for dy in (-2, 0, 2):
                for dx in (-2, 0, 2):
                    left = int(round(cx - tw / 2.0)) + dx
                    top = int(round(cy - th / 2.0)) + dy
                    if left < 0 or top < 0 or left + tw > width or top + th > height:
                        continue
                    patch = [luma[(top + yy) * width + (left + xx)] for yy in range(th) for xx in range(tw)]
                    score = _ncc(_edge_map(patch, tw, th), tmpl["edge"])
                    if score > best:
                        best = score
                        best_name = str(tmpl["name"])
                        best_xy = (left + tw / 2.0, top + th / 2.0)
        if best < TEMPLATE_MED:
            continue
        hx, hy = best_xy
        hits.append({
            "source": "template", "x_pm": _pm(hx, width), "y_pm": _pm(hy, height),
            "score": best, "label": best_name,
            "full": _fully_inside(int(hx - 2), int(hy - 2), int(hx + 2), int(hy + 2), width, height),
            "x": hx, "y": hy,
        })
    hits.sort(key=lambda h: (-h["score"], h["y_pm"], h["x_pm"]))
    return hits


def _agree(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return abs(int(a["x_pm"]) - int(b["x_pm"])) <= AGREE_X_PM and abs(int(a["y_pm"]) - int(b["y_pm"])) <= AGREE_Y_PM


def _tap_point(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    prefer = [h for h in group if h["source"] == "template"] or [h for h in group if h["source"] == "structure"] or list(group)
    anchor = prefer[0]
    sources = sorted({str(h["source"]) for h in group})
    labels = [str(h.get("label") or "") for h in group if h.get("source") == "text"]
    return {
        "x": int(anchor["x_pm"]), "y": int(anchor["y_pm"]),
        "signals": "+".join(sources),
        "label": labels[0] if labels else str(anchor.get("label") or sources[0]),
        "score": max(float(h["score"]) for h in group),
        "full": all(bool(h.get("full")) for h in group),
    }


def fuse_signals(text: Sequence[Dict[str, Any]], templates: Sequence[Dict[str, Any]],
                 structure: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Topmost fully visible target. High template, or two agreeing signals."""
    pool = [h for h in list(text) + list(templates) + list(structure) if h.get("full")]
    candidates = []
    used = set()
    for i, hit in enumerate(pool):
        if hit["source"] == "template" and float(hit["score"]) >= TEMPLATE_HIGH:
            candidates.append(_tap_point([hit]))
            used.add(i)
    for i, a in enumerate(pool):
        group = [a]
        sources = {a["source"]}
        for b in pool[i + 1:]:
            if b["source"] in sources:
                continue
            if _agree(a, b):
                group.append(b)
                sources.add(b["source"])
        if len(sources) >= 2:
            candidates.append(_tap_point(group))
    if not candidates:
        return None
    full = [c for c in candidates if c.get("full")]
    if not full:
        return None
    full.sort(key=lambda c: (c["y"], c["x"], -c["score"]))
    return full[0]


def empty_phrase(boxes: Sequence[Dict[str, Any]]) -> bool:
    blob = " ".join(_norm_token(b.get("text")) for b in boxes if isinstance(b, dict))
    return any(phrase in blob for phrase in _EMPTY_PHRASES)


def post_evidence(boxes: Sequence[Dict[str, Any]], blobs: Sequence[Dict[str, int]],
                  width: int, height: int) -> bool:
    if text_hits(boxes, width, height):
        return True
    if _count_row_ys(boxes, height):
        return True
    if structure_hits(blobs, boxes, width, height):
        return True
    # An icon row with no count line is still a post, not an empty feed.
    # It is not enough on its own to tap.
    return any(_even_icon_row(group) for group in _rows_of(blobs, height))


def locate_like_row(raw: bytes, boxes: Any = ()) -> Dict[str, Any]:
    """Locate a Like target. ``target`` is None when the bar is not confident.

    Return keys: target ({x, y, signals, label, score} permille), post (bool),
    empty_phrase (bool). x/y are the icon center when a template or the action
    bar took part, otherwise the Like-word center.
    """
    norm = normalize_ocr_boxes(boxes)
    try:
        width, height, _hdr, _order = frame_size(raw)
    except ValueError:
        return {"target": None, "post": False, "empty_phrase": empty_phrase(norm)}
    blobs = extract_blobs(raw)
    text = text_hits(norm, width, height)
    structure = structure_hits(blobs, norm, width, height)
    templ = template_hits(raw, blobs)
    # Drop a candidate whose center is already the liked blue.
    decoded = _gray(raw)
    if decoded is not None:
        _w, _h, hdr, order, _luma = decoded

        def _blue_hit(hit: Dict[str, Any]) -> bool:
            x, y = int(round(hit["x"])), int(round(hit["y"]))
            if not (0 <= x < width and 0 <= y < height):
                return False
            try:
                return _near_blue(_rgb_at(raw, hdr, width, order, x, y))
            except ValueError:
                return False

        text = [h for h in text if not _blue_hit(h)]
        structure = [h for h in structure if not _blue_hit(h)]
        templ = [h for h in templ if not _blue_hit(h)]
    target = fuse_signals(text, templ, structure)
    if target is not None:
        target = {k: target[k] for k in ("x", "y", "signals", "label", "score")}
    return {
        "target": target,
        "post": post_evidence(norm, blobs, width, height),
        "empty_phrase": empty_phrase(norm),
    }


def like_state_changed(before: bytes, after: bytes, x: int, y: int,
                       before_boxes: Any = (), after_boxes: Any = ()) -> bool:
    """True when the tapped control turns into a liked label or Facebook blue.

    A frame that merely changes somewhere else is not a like.
    """
    try:
        bw, bh, bhdr, border = frame_size(before)
        aw, ah, ahdr, aorder = frame_size(after)
    except ValueError:
        return False
    if (bw, bh) != (aw, ah):
        return False
    bb = normalize_ocr_boxes(before_boxes)
    aa = normalize_ocr_boxes(after_boxes)

    def _liked_near(boxes: Sequence[Dict[str, Any]]) -> bool:
        for box in boxes:
            if _norm_token(box.get("text")) not in _LIKED:
                continue
            cx, cy = _center(int(box["x0"]), int(box["y0"]), int(box["x1"]), int(box["y1"]))
            if abs(_pm(cx, bw) - _pm(x, bw)) <= AGREE_X_PM and abs(_pm(cy, bh) - _pm(y, bh)) <= AGREE_Y_PM:
                return True
        return False

    if _liked_near(aa) and not _liked_near(bb):
        return True
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            xx, yy = x + dx, y + dy
            if not (0 <= xx < bw and 0 <= yy < bh):
                continue
            try:
                old = _rgb_at(before, bhdr, bw, border, xx, yy)
                new = _rgb_at(after, ahdr, aw, aorder, xx, yy)
            except ValueError:
                continue
            if _near_blue(new) and not _near_blue(old):
                return True
            if new[2] >= old[2] + 36 and new[2] >= new[0] + 28 and new[2] >= new[1] + 12:
                return True
    return False


_RAPID_STATE = {"tried": False, "fn": None}


def rapidocr_available() -> bool:
    fn = load_rapidocr()
    return fn is not None


def load_rapidocr():
    """Lazy text reader. None when rapidocr-onnxruntime is not installed."""
    if _RAPID_STATE["tried"]:
        return _RAPID_STATE["fn"]
    _RAPID_STATE["tried"] = True
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception:
        _RAPID_STATE["fn"] = None
        return None
    try:
        engine = RapidOCR()
    except Exception:
        _RAPID_STATE["fn"] = None
        return None

    def _read(raw: bytes):
        decoded = _gray(raw)
        if decoded is None:
            return []
        width, height, _hdr, _order, luma = decoded
        # RapidOCR accepts a numpy image. Build one only after the import works.
        try:
            import numpy as np
        except Exception:
            return []
        arr = np.array(luma, dtype="uint8").reshape((height, width))
        got = engine(arr)
        return normalize_ocr_boxes(got)

    _RAPID_STATE["fn"] = _read
    return _read


def read_text_boxes(raw: bytes) -> List[Dict[str, Any]]:
    fn = load_rapidocr()
    if fn is None:
        return []
    try:
        return normalize_ocr_boxes(fn(raw))
    except Exception:
        return []


def vision_stack_ready(*, ocr: Any = None, template_dir: Optional[Path] = None) -> bool:
    """True when a real like can see the button without the fixed coordinate.

    Templates alone are enough (icon rows have no Like word). A caller-supplied
    OCR callable is enough for tests. RapidOCR is optional.
    """
    if templates_available(template_dir):
        return True
    if ocr is False or ocr is None:
        return rapidocr_available() if ocr is None else False
    return callable(ocr)


__all__ = [
    "AGREE_X_PM", "AGREE_Y_PM", "DEFAULT_LIKE_SWIPES", "FB_BLUE", "FB_BLUE_TOL",
    "TEMPLATE_HIGH", "TEMPLATE_MED", "empty_phrase", "extract_blobs", "fuse_signals",
    "like_state_changed", "load_rapidocr", "load_templates", "locate_like_row",
    "normalize_ocr_boxes", "post_evidence", "rapidocr_available", "read_text_boxes",
    "render_thumb", "structure_hits", "template_hits", "templates_available",
    "text_hits", "vision_stack_ready", "write_default_templates",
]
