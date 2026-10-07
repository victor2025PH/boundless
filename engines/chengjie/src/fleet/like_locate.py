"""Find a Facebook Like control on a screencap without a fixed coordinate.

Wallpaper 09 (CHINAMI-B, 2026-10-08) shows the post action bar as icons and
counts only: no Like / Comment / Share words. 0.3.18 locates that icon-only
bar without rapidocr. Text is one signal when a text engine is installed. A
tap still needs a second agreeing signal, or a high-confidence thumbs-up
template match. One weak signal is never a tap.

Signals
    label       Read-only ``uiautomator dump`` content-desc / text / resource-id.
                Same needles as huoke's feed Like scan (like / いいね / 赞), plus
                the icon-only content-desc React, Tagalog (gusto / gustuhin),
                and the Chinese 点赞 / 讚 / 喜欢 forms. A resource-id that names
                like or react (including ``feed_story_…like``) counts the same
                way. Liked, comment, share, and the reaction names are not
                targets. A label is not a tap by itself: a thumbs-up template
                at the same place has to agree. Optional. A dump that fails or
                comes back empty leaves this signal empty.
    position    Leftmost button of a hierarchy row whose other buttons are
                Comment / Share (Komento / Ibahagi, 评论 / 分享). This is the
                icon-only bar when the Like glyph itself has no label. It
                agrees with a screenshot signal (template, action bar, or
                silhouette), not with a label from the same dump. Not a tap
                by itself.
    text        Like / Gusto / I-like / 赞 on a row that also has Comment and Share
                (Komento / Ibahagi, 评论 / 分享). Liked / Nagustuhan / 已赞 is not a target.
                Optional. The default build does not import rapidocr, cv2, or onnx.
    template    Bundled thumbs-up rasters: the original light/dark blocks plus
                outline and filled glyphs at phone scales. Matched by normalized
                edge correlation. Stdlib only. A window is tried only when the
                template is close to the blob size, so a small block is not
                scored against the empty center of a large outline icon.
    structure   Leftmost slot of a row of 2–4 evenly spaced icons. The row is
                anchored either under a reaction-count line (OCR tokens such as
                92k / 1.5k, or a shorter glyph row) or, with no text at all,
                just above the comment composer (a wide short pill). Order is
                react, comment, share, and sometimes send.
    shape       The leftmost cluster's silhouette: narrow stem left of a wider
                palm, wrist narrower than the palm. A square, a round comment
                bubble, and a share arrow do not pass. This is the second
                signal when the template only partly matches the live icon.

Tap rule: template score >= TEMPLATE_HIGH, or at least two signals whose
centers agree. Shape and template describe the same glyph, so those two alone
do not agree — the bar (structure) or the Like word (text) has to take part.
A hierarchy label agrees only with a template, not with the bar or the
silhouette by itself. A label without a template does not cancel a separate
position+screenshot pair. Otherwise no target (never the deprecated
like_button). ``locate_like_row`` also returns ``diag`` for ``like_probe``.
The diagnostic does not change the tap.

Text engine: optional ``rapidocr-onnxruntime`` (PP-OCRv4 reads Latin and CJK,
so Tagalog and 赞 do not need a Windows OCR language pack). Windows.Media.Ocr
is not used. The import is lazy and happens only inside ``load_rapidocr``.
A missing text engine leaves the text signal empty; templates, the composer
bar, and the thumb silhouette still run. ``ocr_unavailable`` is only when the
template pack and the text engine are both missing — that is the case where
the only remaining aim would be the fixed coordinate, which real likes refuse.

Empty feed: after the swipe budget, no count row, no action bar, and no Like
text row. Phrases such as "Something went wrong" / "Stories couldn't load"
mark the same outcome so the scheduler can send the account to warm-up.
"""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TEMPLATE_HIGH = 0.82
# Recorded as a template hit, not enough on its own. Squares on the bundled
# glyphs score about 0.32; comment bubbles and arrows score under 0.25.
TEMPLATE_MED = 0.50
AGREE_X_PM = 72
AGREE_Y_PM = 56
DEFAULT_LIKE_SWIPES = 3
FB_BLUE = (24, 119, 242)
FB_BLUE_TOL = 48
# Template vs blob size. A 16px block on a 56px outline reads the empty center.
_TEMPLATE_SIZE_LO = 0.60
_TEMPLATE_SIZE_HI = 1.55

_TEMPLATE_DIR = Path(__file__).with_name("like_templates")
# Edge correlation collapses when the window is one pixel off, and a
# downsampled blob is quantized by the screencap step. Search a small box
# around the blob center, nearest offsets first.
_TEMPLATE_OFFSETS = tuple(sorted(
    ((dx, dy) for dx in range(-5, 6) for dy in range(-5, 6)),
    key=lambda p: (abs(p[0]) + abs(p[1]), abs(p[0]), abs(p[1])),
))
_RAW_ORDERS = {1: "rgba", 2: "rgba", 5: "bgra"}
_LIKE = {"like", "gusto", "i-like", "i like", "赞"}
_LIKED = {"liked", "nagustuhan", "已赞"}
_COMMENT = {"comment", "komento", "评论"}
_SHARE = {"share", "ibahagi", "分享"}
# Huoke's feed scan (facebook.py, the Like-button bounds pass) plus the
# icon-only content-desc "React" and the Tagalog / Chinese labels seen on
# icon-only bars. Longer needles come first so 点赞 is not recorded as 赞.
# "reactions" is a count, not the button.
_LABEL_NEEDLE = (
    ("thumbs up", "thumbs-up"),
    ("thumbs-up", "thumbs-up"),
    ("like", "like"),
    ("いいね", "いいね"),
    ("点赞", "点赞"),
    ("點贊", "點贊"),
    ("讚", "讚"),
    ("喜欢", "喜欢"),
    ("喜歡", "喜歡"),
    ("赞", "赞"),
    ("gustuhin", "gustuhin"),
    ("gusto", "gusto"),
)
_LABEL_EXCLUDE = (
    "liked", "unlike", "nagustuhan", "已赞", "取消",
    "comment", "komento", "评论", "コメント", "magkomento",
    "share", "ibahagi", "分享", "シェア",
    "love", "haha", "wow", "sad", "angry", "reactions",
)
_COMMENT_NEEDLE = ("comment", "komento", "评论", "コメント", "magkomento")
_SHARE_NEEDLE = ("share", "ibahagi", "分享", "シェア")
_LIKED_NEEDLE = ("liked", "unlike", "nagustuhan", "已赞", "取消")
_REACTION_NEEDLE = ("love", "haha", "wow", "sad", "angry", "care")
_DIAG_NODES = 24
_DIAG_STR = 80
_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
_EMPTY_PHRASES = (
    "something went wrong",
    "stories couldn't load",
    "stories could not load",
    "no posts",
    "nothing to show",
)
_SCALES = (("s", 16, 20), ("m", 20, 26), ("l", 24, 30))
# Outline and filled glyphs. ``s`` fits the 180px test frame (blob cap is
# ~14% of width). The larger steps match xxhdpi action icons (~48–72px).
_ICON_SCALES = (
    ("s", 18, 22),
    ("m", 24, 30),
    ("l", 32, 40),
    ("xl", 44, 54),
    ("xxl", 56, 68),
    ("xxxl", 72, 88),
)


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


def _attr_blob(*parts: str) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip()).casefold().replace("’", "'")


def _like_token_in(part: str) -> str:
    """Like / React token in one attribute. The caller already applied excludes."""
    low = part.strip().casefold().replace("’", "'")
    if not low:
        return ""
    if "react" in re.findall(r"[a-z]+", low):
        return "react"
    for needle, token in _LABEL_NEEDLE:
        if needle in low:
            return token
    return ""


def _like_label_match(desc: str, text: str, resource_id: str) -> Tuple[str, str]:
    """``(token, by)`` for one node. ``by`` is content-desc, text, or resource-id.

    ``liked`` / comment / share / the reaction names / a ``reactions`` count
    are not the button. A resource-id is checked with the same words because
    some builds put the name there and leave the visible text empty
    (``like_button``, ``feed_story_ufi_like``). ``feed_story`` alone is not a
    Like control.
    """
    blob = _attr_blob(desc, text, resource_id)
    if not blob or any(needle in blob for needle in _LABEL_EXCLUDE):
        return "", ""
    for by, part in (("content-desc", desc), ("text", text), ("resource-id", resource_id)):
        token = _like_token_in(part)
        if token:
            return token, by
    return "", ""


def _like_label_token(desc: str, text: str, resource_id: str) -> str:
    return _like_label_match(desc, text, resource_id)[0]


def _button_max(width: int, height: int) -> Tuple[int, int]:
    """Huoke's 300×200 box, widened on a phone-width frame.

    An xxhdpi action button is often about a third of a 1080px screen, so the
    old 300px cap dropped it. The floor stays 300 on the small test frames.
    A full-width banner still does not pass (cap 480×280).
    """
    max_w = min(480, max(300, int(width * 0.42)))
    max_h = min(280, max(200, int(height * 0.16)))
    return max_w, max_h


def _button_box_ok(bw: int, bh: int, width: int, height: int) -> bool:
    if bw < 20 or bh < 20:
        return False
    max_w, max_h = _button_max(width, height)
    return bw <= max_w and bh <= max_h


def _clip_diag(value: Any, n: int = _DIAG_STR) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isprintable() and ch not in "<>")
    return text[:n]


def _parse_hierarchy(hierarchy: Any) -> Optional[ET.Element]:
    if not isinstance(hierarchy, str) or "<hierarchy" not in hierarchy:
        return None
    start = hierarchy.find("<hierarchy")
    end = hierarchy.rfind("</hierarchy>")
    if start < 0 or end < 0:
        return None
    chunk = hierarchy[start:end + len("</hierarchy>")]
    if len(chunk) > 2_000_000:
        return None
    try:
        return ET.fromstring(chunk)
    except ET.ParseError:
        return None


def _dump_status(hierarchy: Any) -> str:
    if hierarchy is None or hierarchy == "":
        return "empty"
    if not isinstance(hierarchy, str) or not hierarchy.strip():
        return "empty"
    if "<hierarchy" not in hierarchy:
        return "bad"
    return "ok" if _parse_hierarchy(hierarchy) is not None else "bad"


def hierarchy_text_ok(hierarchy: Any) -> bool:
    """True when ``hierarchy`` contains a parseable ``<hierarchy>`` document."""
    return _dump_status(hierarchy) == "ok"


def _bar_role(desc: str, text: str, resource_id: str) -> str:
    blob = _attr_blob(desc, text, resource_id)
    if not blob:
        return ""
    if any(needle in blob for needle in _LIKED_NEEDLE):
        return "liked"
    if _like_label_token(desc, text, resource_id):
        return "like"
    if any(needle in blob for needle in _COMMENT_NEEDLE):
        return "comment"
    if any(needle in blob for needle in _SHARE_NEEDLE):
        return "share"
    if any(needle in blob for needle in _REACTION_NEEDLE):
        return "reaction"
    return ""


def _drop_containers(nodes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop a button that holds another button, so a bar wrapper is not the slot."""
    kept: List[Dict[str, Any]] = []
    for node in nodes:
        area = (node["x1"] - node["x0"]) * (node["y1"] - node["y0"])
        holds = False
        for other in nodes:
            if other is node:
                continue
            other_area = (other["x1"] - other["x0"]) * (other["y1"] - other["y0"])
            if area <= other_area * 1.4:
                continue
            if node["x0"] <= other["x"] <= node["x1"] and node["y0"] <= other["y"] <= node["y1"]:
                holds = True
                break
        if not holds:
            kept.append(node)
    return kept


def _hierarchy_buttons(hierarchy: Any, width: int, height: int) -> List[Dict[str, Any]]:
    """Button-sized nodes, including a wrapper that also carries the label."""
    root = _parse_hierarchy(hierarchy)
    if root is None:
        return []
    found: List[Dict[str, Any]] = []
    for node in root.iter():
        desc = str(node.attrib.get("content-desc") or "")
        text = str(node.attrib.get("text") or "")
        rid = str(node.attrib.get("resource-id") or "")
        bounds = str(node.attrib.get("bounds") or "").strip()
        match = _BOUNDS_RE.fullmatch(bounds)
        if match is None:
            continue
        x0, y0, x1, y1 = (int(v) for v in match.groups())
        bw, bh = x1 - x0, y1 - y0
        if not _button_box_ok(bw, bh, width, height):
            continue
        if not _fully_inside(x0, y0, x1, y1, width, height):
            continue
        token, by = _like_label_match(desc, text, rid)
        cx, cy = _center(x0, y0, x1, y1)
        found.append({
            "bounds": bounds, "class": str(node.attrib.get("class") or ""),
            "content_desc": desc, "resource_id": rid, "text": text,
            "x0": x0, "y0": y0, "x1": x1, "y1": y1, "x": cx, "y": cy,
            "label": token, "by": by, "role": _bar_role(desc, text, rid),
        })
    return found


def _button_rows(nodes: Sequence[Dict[str, Any]], height: int) -> List[List[Dict[str, Any]]]:
    tol = _y_tol(height)
    ordered = sorted(nodes, key=lambda n: n["y"])
    rows: List[List[Dict[str, Any]]] = []
    for node in ordered:
        placed = False
        for group in rows:
            gy = sum(item["y"] for item in group) / len(group)
            if abs(node["y"] - gy) <= tol:
                group.append(node)
                placed = True
                break
        if not placed:
            rows.append([node])
    for group in rows:
        group.sort(key=lambda n: n["x0"])
    return rows


def _hit_from_button(node: Dict[str, Any], source: str, width: int, height: int,
                     label: str) -> Dict[str, Any]:
    return {
        "source": source, "x_pm": _pm(node["x"], width), "y_pm": _pm(node["y"], height),
        "score": 1.0, "label": label, "full": True, "x": node["x"], "y": node["y"],
        "bounds": node["bounds"], "by": node.get("by") or "",
        "content_desc": node.get("content_desc") or "", "text": node.get("text") or "",
        "resource_id": node.get("resource_id") or "", "class": node.get("class") or "",
    }


def label_hits(hierarchy: Any, width: int, height: int) -> List[Dict[str, Any]]:
    """Like controls named by the accessibility hierarchy. Empty on a bad dump.

    Bounds use huoke's button box (at least 20px, not a banner). On a
    phone-width frame the cap follows the screen so an xxhdpi action button
    wider than 300px still counts. The hit is not a tap by itself.
    """
    hits = []
    for node in _hierarchy_buttons(hierarchy, width, height):
        if not node.get("label"):
            continue
        hits.append(_hit_from_button(node, "label", width, height, str(node["label"])))
    hits.sort(key=lambda h: (h["y_pm"], h["x_pm"]))
    return hits


def _position_row(group: Sequence[Dict[str, Any]], width: int) -> bool:
    """True when this row is an action bar and the leftmost slot can be Like."""
    if not 2 <= len(group) <= 4:
        return False
    heights = [n["y1"] - n["y0"] for n in group]
    if min(heights) <= 0 or max(heights) / min(heights) > 1.85:
        return False
    centers = [n["x"] for n in group]
    if centers[-1] - centers[0] < width * 0.15:
        return False
    for prev, nxt in zip(centers, centers[1:]):
        if nxt - prev < 8:
            return False
    if not any(n.get("role") in ("comment", "share") for n in group):
        return False
    left = group[0].get("role") or ""
    return left in ("", "like")


def position_hits(hierarchy: Any, width: int, height: int) -> List[Dict[str, Any]]:
    """Leftmost button of a Comment/Share action bar. Not a tap by itself.

    The Like glyph on an icon-only bar often has no word. The row still has
    a comment button and a share button. The leftmost slot of that row is
    the candidate. A reaction picker (Love / Haha / Wow) is not a row.
    """
    hits = []
    buttons = _drop_containers(_hierarchy_buttons(hierarchy, width, height))
    for group in _button_rows(buttons, height):
        if not _position_row(group, width):
            continue
        left = group[0]
        label = str(left.get("label") or "icon")
        hits.append(_hit_from_button(left, "position", width, height, label))
    hits.sort(key=lambda h: (h["y_pm"], h["x_pm"]))
    return hits


def _diag_node(node: Dict[str, Any]) -> Dict[str, str]:
    return {
        "bounds": _clip_diag(node.get("bounds") or "", 40),
        "class": _clip_diag(node.get("class") or ""),
        "content_desc": _clip_diag(node.get("content_desc") or ""),
        "resource_id": _clip_diag(node.get("resource_id") or ""),
        "text": _clip_diag(node.get("text") or ""),
    }


def _unique_attrs(nodes: Sequence[Dict[str, Any]], key: str) -> List[str]:
    out: List[str] = []
    for node in nodes:
        text = _clip_diag(node.get(key) or "")
        if text and text not in out:
            out.append(text)
        if len(out) >= 12:
            break
    return out


def _candidate_row(buttons: Sequence[Dict[str, Any]], height: int,
                   like: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [group for group in _button_rows(buttons, height) if 1 <= len(group) <= 8]
    if like is not None:
        for group in rows:
            if any(n["bounds"] == like.get("bounds") for n in group):
                return list(group)[:_DIAG_NODES]
    for group in rows:
        # A comment/share sibling is enough to show the operator this row,
        # even when the span is too short to count as a position hit.
        if 2 <= len(group) <= 4 and any(n.get("role") in ("comment", "share") for n in group):
            return list(group)[:_DIAG_NODES]
    ranked = sorted(rows, key=lambda group: sum(1 for n in group if n.get("content_desc") or n.get("text") or n.get("resource_id")), reverse=True)
    for group in ranked:
        if 2 <= len(group) <= 4:
            return list(group)[:_DIAG_NODES]
    if like is not None:
        return [like]
    return []


def _like_match_record(node: Dict[str, Any]) -> Dict[str, str]:
    return {
        "label": _clip_diag(node.get("label") or "", 32),
        "by": str(node.get("by") or ""),
        "content_desc": _clip_diag(node.get("content_desc") or ""),
        "text": _clip_diag(node.get("text") or ""),
        "resource_id": _clip_diag(node.get("resource_id") or ""),
        "bounds": _clip_diag(node.get("bounds") or "", 40),
    }


def hierarchy_diag(hierarchy: Any, width: int, height: int) -> Dict[str, Any]:
    """Compact action-bar attributes. No pixels, no device identity."""
    status = _dump_status(hierarchy)
    buttons = _hierarchy_buttons(hierarchy, width, height) if status == "ok" else []
    likes = [n for n in buttons if n.get("label")]
    likes.sort(key=lambda n: (n["y"], n["x"]))
    like = likes[0] if likes else None
    row = _candidate_row(_drop_containers(buttons), height, like) if buttons else []
    uia: Dict[str, Any] = {
        "dump": status, "like_found": like is not None,
        "attempts": 0, "via": "", "compressed": False, "error": "",
    }
    if like is not None:
        uia["match"] = _like_match_record(like)
    return {
        "uiautomator": uia,
        "action_bar": {
            "content_descs": _unique_attrs(row, "content_desc"),
            "texts": _unique_attrs(row, "text"),
            "resource_ids": _unique_attrs(row, "resource_id"),
        },
        "nodes": [_diag_node(n) for n in row[:_DIAG_NODES]],
    }


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
    blobs = _suppress_nested(blobs)
    if len(blobs) > 80:
        target = max(min_side * min_side, int((width * 0.045) ** 2))
        blobs.sort(key=lambda b: abs((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) - target))
        blobs = blobs[:80]
    return blobs


def _suppress_nested(blobs: Sequence[Dict[str, int]]) -> List[Dict[str, int]]:
    """Drop an edge ring that sits inside a larger icon blob.

    An outline comment bubble often yields the outer stroke and a second
    component on the inner stroke. The inner one is not its own action.
    """
    kept: List[Dict[str, int]] = []
    for blob in blobs:
        bw = blob["x1"] - blob["x0"]
        bh = blob["y1"] - blob["y0"]
        cx = (blob["x0"] + blob["x1"]) / 2.0
        cy = (blob["y0"] + blob["y1"]) / 2.0
        nested = False
        for other in blobs:
            if other is blob:
                continue
            ow = other["x1"] - other["x0"]
            oh = other["y1"] - other["y0"]
            if ow < bw * 1.15 or oh < bh * 1.15:
                continue
            if other["x0"] <= cx <= other["x1"] and other["y0"] <= cy <= other["y1"]:
                nested = True
                break
        if not nested:
            kept.append(blob)
    return kept


def _prune_icon_row(group: Sequence[Dict[str, int]]) -> List[Dict[str, int]]:
    """Drop a speck that joined an icon row and would break even spacing."""
    if len(group) < 2:
        return list(group)
    areas = sorted((b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) for b in group)
    med = areas[len(areas) // 2]
    if med <= 0:
        return list(group)
    return [b for b in group if (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]) >= med * 0.35]


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


def composer_centers(raw: bytes) -> List[float]:
    """Y centers of wide, short pills — the comment composer under an action bar.

    The composer is a rounded bar spanning most of the width. Icon blobs never
    include it (it is far wider than an icon), so this scan is separate. A
    single hairline divider is not a pill: the top and bottom edges have to
    sit a short distance apart.
    """
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
        for x in range(sw):
            sx = min(width - 1, x * step)
            gray[y * sw + x] = _luma_at(raw, hdr, width, order, sx, sy)
    # 8 luma catches a white pill on the near-white feed (about 9 apart). A
    # 1px border on a flat fill is the same kind of full-width edge.
    delta = 8
    need = int(sw * 0.50)
    strong: List[int] = []
    for y in range(1, sh - 1):
        count = 0
        row = y * sw
        prev = (y - 1) * sw
        for x in range(sw):
            if abs(gray[row + x] - gray[prev + x]) >= delta:
                count += 1
        if count >= need:
            strong.append(y)
    bands: List[List[int]] = []
    for y in strong:
        if bands and y - bands[-1][-1] <= 2:
            bands[-1].append(y)
        else:
            bands.append([y])
    ys = [sum(band) / len(band) for band in bands]
    strong_set = set(strong)
    lo = max(3.0, sh * 0.010)
    hi = max(lo + 1.0, sh * 0.075)
    centers: List[float] = []
    for i, top in enumerate(ys):
        for bot in ys[i + 1:]:
            gap = bot - top
            if gap < lo:
                continue
            if gap > hi:
                break
            interior = range(int(top) + 1, int(bot))
            if not interior:
                continue
            noisy = sum(1 for y in interior if y in strong_set)
            # The fill between the two edges is flat. A photo or a stack of
            # dividers is not a composer.
            if noisy > max(1, int(0.35 * len(list(interior)))):
                continue
            centers.append((top + bot) / 2.0 * step)
            break
    return centers


def structure_hits(blobs: Sequence[Dict[str, int]], boxes: Sequence[Dict[str, Any]],
                   width: int, height: int, composer_ys: Optional[Sequence[float]] = None) -> List[Dict[str, Any]]:
    """Leftmost slot of each action bar.

    The bar is a row of 2–4 even icons under a reaction-count line, or a row
    of 3–4 even icons sitting just above the comment composer. The leftmost
    slot is the Like / reaction control.
    """
    count_ys = _count_row_ys(boxes, height)
    rows = _rows_of(blobs, height)
    icon_rows = []
    for group in rows:
        pruned = _prune_icon_row(group)
        if _even_icon_row(pruned):
            icon_rows.append(pruned)
    hits = []
    lo = max(8.0, height * 0.012)
    hi = height * 0.14
    comp_lo = max(4.0, height * 0.004)
    comp_hi = height * 0.14
    composers = list(composer_ys or ())

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
        count_ok = bool(above) and any(lo <= (cy - ay) <= hi for ay in above)
        composer_ok = False
        if composers and 3 <= len(group) <= 4:
            centers = [(b["x0"] + b["x1"]) / 2.0 for b in group]
            span = centers[-1] - centers[0]
            if span >= width * 0.18:
                composer_ok = any(comp_lo <= (ay - cy) <= comp_hi for ay in composers)
        if not count_ok and not composer_ok:
            continue
        if not all(_fully_inside(b["x0"], b["y0"], b["x1"], b["y1"], width, height) for b in group):
            continue
        left = group[0]
        cx, cyy = _center(left["x0"], left["y0"], left["x1"], left["y1"])
        row_bounds = "[{},{}][{},{}]".format(
            min(int(b["x0"]) for b in group), min(int(b["y0"]) for b in group),
            max(int(b["x1"]) for b in group), max(int(b["y1"]) for b in group),
        )
        hits.append({
            "source": "structure", "x_pm": _pm(cx, width), "y_pm": _pm(cyy, height),
            "score": 1.0, "label": "icon", "full": True, "x": cx, "y": cyy,
            "row_bounds": row_bounds,
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


def _thumb_silhouette(width: int, height: int) -> List[List[bool]]:
    """Facebook-style thumbs-up. The stem sits left of the palm."""
    grid = [[False] * width for _ in range(height)]
    for y in range(height):
        ny = (y + 0.5) / height
        row = grid[y]
        for x in range(width):
            nx = (x + 0.5) / width
            on = False
            if 0.18 <= nx <= 0.42 and 0.05 <= ny <= 0.48:
                on = True
            if 0.16 <= nx <= 0.88 and 0.38 <= ny <= 0.74:
                on = True
            if 0.28 <= nx <= 0.72 and 0.66 <= ny <= 0.95:
                on = True
            row[x] = on
    return grid


def render_thumb_icon(width: int, height: int, *, dark: bool, filled: bool) -> bytes:
    """Outline or filled thumbs-up for the icon-only action bar.

    Light: dark glyph on a light pad. Dark: the inverse. The outline stroke
    is thick enough to stay one connected edge blob after a 4-neighbor scan.
    """
    bg = (28, 30, 34) if dark else (246, 246, 248)
    ink = (232, 234, 238) if dark else (55, 58, 64)
    grid = _thumb_silhouette(width, height)
    stroke = max(2, int(round(min(width, height) * 0.075)))
    if filled:
        mask = grid
    else:
        bound = [[False] * width for _ in range(height)]
        for y in range(height):
            for x in range(width):
                if not grid[y][x]:
                    continue
                edge = False
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    yy, xx = y + dy, x + dx
                    if yy < 0 or xx < 0 or yy >= height or xx >= width or not grid[yy][xx]:
                        edge = True
                        break
                bound[y][x] = edge
        mask = [row[:] for row in bound]
        radius = stroke - 1
        if radius > 0:
            for y in range(height):
                for x in range(width):
                    if not bound[y][x]:
                        continue
                    for dy in range(-radius, radius + 1):
                        for dx in range(-radius, radius + 1):
                            if max(abs(dx), abs(dy)) > radius:
                                continue
                            yy, xx = y + dy, x + dx
                            if 0 <= yy < height and 0 <= xx < width and grid[yy][xx]:
                                mask[yy][xx] = True
    out = bytearray()
    for y in range(height):
        for x in range(width):
            out += bytes(ink if mask[y][x] else bg)
    return bytes(out)


def write_default_templates(dest: Optional[Path] = None) -> Path:
    folder = dest or _TEMPLATE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        dark = theme == "dark"
        for name, w, h in _SCALES:
            rgb = render_thumb(w, h, dark=dark)
            _write_png(folder / f"like_{theme}_{name}.png", w, h, rgb)
        for name, w, h in _ICON_SCALES:
            _write_png(folder / f"like_{theme}_outline_{name}.png", w, h,
                       render_thumb_icon(w, h, dark=dark, filled=False))
            _write_png(folder / f"like_{theme}_filled_{name}.png", w, h,
                       render_thumb_icon(w, h, dark=dark, filled=True))
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
    for style in ("outline", "filled"):
        for theme in ("light", "dark"):
            for name, _w, _h in _ICON_SCALES:
                need.add(f"like_{theme}_{style}_{name}")
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


def _template_scan(raw: bytes, blobs: Sequence[Dict[str, int]],
                   templates: Optional[Sequence[Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], float]:
    """Template hits plus the best score, including scores under TEMPLATE_MED.

    Scores under the bar are not hits. The best score is still returned so a
    probe can show a near miss.
    """
    decoded = _gray(raw)
    if decoded is None:
        return [], 0.0
    width, height, _hdr, _order, luma = decoded
    pack = list(templates) if templates is not None else load_templates()
    if not pack:
        return [], 0.0
    hits = []
    best_any = 0.0
    for blob in blobs:
        x0 = max(0, int(blob["x0"]))
        y0 = max(0, int(blob["y0"]))
        x1 = min(width - 1, int(blob["x1"]))
        y1 = min(height - 1, int(blob["y1"]))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        cx, cy = _center(x0, y0, x1, y1)
        bw, bh = x1 - x0, y1 - y0
        best = 0.0
        best_name = ""
        best_xy = (cx, cy)
        for tmpl in pack:
            tw, th = int(tmpl["w"]), int(tmpl["h"])
            if bw < 4 or bh < 4:
                continue
            if not (_TEMPLATE_SIZE_LO <= (tw / bw) <= _TEMPLATE_SIZE_HI
                    and _TEMPLATE_SIZE_LO <= (th / bh) <= _TEMPLATE_SIZE_HI):
                continue
            for dx, dy in _TEMPLATE_OFFSETS:
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
                if best >= 0.97:
                    break
            if best >= 0.97:
                break
        if best > best_any:
            best_any = best
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
    return hits, best_any


def template_hits(raw: bytes, blobs: Sequence[Dict[str, int]],
                  templates: Optional[Sequence[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    hits, _best = _template_scan(raw, blobs, templates)
    return hits


def _looks_like_thumb(luma: Sequence[int], width: int, height: int) -> bool:
    """True when the crop is a left-stem thumbs-up, not a square, bubble, or arrow.

    Spans come from the edge pixels, so a filled glyph and an outline glyph
    share the same outer silhouette. Gates, measured on the bundled glyphs:
    stem/palm width about 0.33, wrist/palm about 0.61, stem center left of the
    palm. A square's rows are the same width. A circle's top is centered.
    An arrow's head sits on the right, so its top center is not left of the palm.
    """
    if width < 8 or height < 10 or not 0.45 <= (width / float(height)) <= 1.35:
        return False
    edge = [False] * (width * height)
    ink_edges = 0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            c = int(luma[y * width + x])
            diff = max(
                abs(c - int(luma[y * width + x - 1])),
                abs(c - int(luma[y * width + x + 1])),
                abs(c - int(luma[(y - 1) * width + x])),
                abs(c - int(luma[(y + 1) * width + x])),
            )
            if diff >= 26:
                edge[y * width + x] = True
                ink_edges += 1
    if ink_edges < max(12, (width + height) // 2):
        return False
    spans = [0] * height
    centers: List[Optional[float]] = [None] * height
    for y in range(height):
        xs = [x for x in range(width) if edge[y * width + x]]
        if len(xs) < 2:
            continue
        spans[y] = xs[-1] - xs[0] + 1
        centers[y] = (xs[0] + xs[-1]) / 2.0
    if sum(1 for s in spans if s > 0) < int(height * 0.55):
        return False

    def _med_span(a: int, b: int) -> int:
        vals = sorted(spans[i] for i in range(a, b) if spans[i] > 0)
        if not vals:
            return 0
        return vals[len(vals) // 2]

    def _med_center(a: int, b: int) -> Optional[float]:
        vals = sorted(centers[i] for i in range(a, b) if centers[i] is not None and spans[i] > 0)
        if not vals:
            return None
        return float(vals[len(vals) // 2])

    t1 = max(1, height // 3)
    m0, m1 = height // 3, (2 * height) // 3
    if m1 <= m0:
        return False
    top_w, mid_w, bot_w = _med_span(0, t1), _med_span(m0, m1), _med_span(m1, height)
    if mid_w < max(4, int(width * 0.35)) or top_w <= 0 or bot_w <= 0:
        return False
    top_ratio, bot_ratio = top_w / float(mid_w), bot_w / float(mid_w)
    if not 0.22 <= top_ratio <= 0.55 or not 0.48 <= bot_ratio <= 0.92:
        return False
    tc, mc = _med_center(0, t1), _med_center(m0, m1)
    if tc is None or mc is None:
        return False
    return (mc - tc) / float(width) >= 0.10


def _window_fit(value: float, lo: float, hi: float) -> float:
    span = max(hi - lo, 1e-6)
    if lo <= value <= hi:
        return 1.0
    if value < lo:
        return max(0.0, 1.0 - (lo - value) / span)
    return max(0.0, 1.0 - (value - hi) / span)


def _thumb_partial(luma: Sequence[int], width: int, height: int) -> float:
    """How close a miss is to the thumb silhouette. Never 1.0.

    The tap gate stays ``_looks_like_thumb``. This score is diagnostic only.
    """
    if width < 8 or height < 10 or not 0.45 <= (width / float(height)) <= 1.35:
        return 0.0
    edge = [False] * (width * height)
    ink_edges = 0
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            c = int(luma[y * width + x])
            diff = max(
                abs(c - int(luma[y * width + x - 1])),
                abs(c - int(luma[y * width + x + 1])),
                abs(c - int(luma[(y - 1) * width + x])),
                abs(c - int(luma[(y + 1) * width + x])),
            )
            if diff >= 26:
                edge[y * width + x] = True
                ink_edges += 1
    need = max(12, (width + height) // 2)
    if ink_edges < need:
        return min(0.20, 0.20 * ink_edges / float(need))
    spans = [0] * height
    centers: List[Optional[float]] = [None] * height
    for y in range(height):
        xs = [x for x in range(width) if edge[y * width + x]]
        if len(xs) < 2:
            continue
        spans[y] = xs[-1] - xs[0] + 1
        centers[y] = (xs[0] + xs[-1]) / 2.0
    if sum(1 for s in spans if s > 0) < int(height * 0.55):
        return 0.30

    def _med_span(a: int, b: int) -> int:
        vals = sorted(spans[i] for i in range(a, b) if spans[i] > 0)
        if not vals:
            return 0
        return vals[len(vals) // 2]

    def _med_center(a: int, b: int) -> Optional[float]:
        vals = sorted(centers[i] for i in range(a, b) if centers[i] is not None and spans[i] > 0)
        if not vals:
            return None
        return float(vals[len(vals) // 2])

    t1 = max(1, height // 3)
    m0, m1 = height // 3, (2 * height) // 3
    if m1 <= m0:
        return 0.35
    top_w, mid_w, bot_w = _med_span(0, t1), _med_span(m0, m1), _med_span(m1, height)
    if mid_w < max(4, int(width * 0.35)) or top_w <= 0 or bot_w <= 0:
        return 0.45
    top_fit = _window_fit(top_w / float(mid_w), 0.22, 0.55)
    bot_fit = _window_fit(bot_w / float(mid_w), 0.48, 0.92)
    tc, mc = _med_center(0, t1), _med_center(m0, m1)
    if tc is None or mc is None:
        return min(0.90, 0.50 + 0.40 * (top_fit + bot_fit) / 2.0)
    stem = max(0.0, min(1.0, ((mc - tc) / float(width)) / 0.10))
    return min(0.99, 0.55 + 0.20 * (top_fit + bot_fit) / 2.0 + 0.24 * stem)


def shape_report(raw: bytes, blobs: Sequence[Dict[str, int]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Thumb hits plus a score record for every icon-sized blob.

    A hit is exactly what ``_looks_like_thumb`` accepts. A miss keeps a
    partial score under 1.0 so a probe can show a near miss.
    """
    decoded = _gray(raw)
    if decoded is None:
        return [], []
    width, height, _hdr, _order, luma = decoded
    hits = []
    records = []
    for blob in blobs:
        x0 = max(0, int(blob["x0"]))
        y0 = max(0, int(blob["y0"]))
        x1 = min(width - 1, int(blob["x1"]))
        y1 = min(height - 1, int(blob["y1"]))
        bw, bh = x1 - x0, y1 - y0
        if bw < 8 or bh < 10:
            continue
        patch = [luma[(y0 + yy) * width + (x0 + xx)] for yy in range(bh) for xx in range(bw)]
        passed = _looks_like_thumb(patch, bw, bh)
        score = 1.0 if passed else min(0.99, _thumb_partial(patch, bw, bh))
        cx, cy = _center(x0, y0, x1, y1)
        records.append({"x": cx, "y": cy, "score": score})
        if not passed:
            continue
        hits.append({
            "source": "shape", "x_pm": _pm(cx, width), "y_pm": _pm(cy, height),
            "score": 1.0, "label": "thumb",
            "full": _fully_inside(x0, y0, x1, y1, width, height),
            "x": cx, "y": cy,
        })
    hits.sort(key=lambda h: (h["y_pm"], h["x_pm"]))
    return hits, records


def shape_hits(raw: bytes, blobs: Sequence[Dict[str, int]]) -> List[Dict[str, Any]]:
    """Thumb-shaped icon blobs. Not a tap by themselves."""
    hits, _records = shape_report(raw, blobs)
    return hits


def _agree(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return abs(int(a["x_pm"]) - int(b["x_pm"])) <= AGREE_X_PM and abs(int(a["y_pm"]) - int(b["y_pm"])) <= AGREE_Y_PM


def _tap_point(group: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    prefer = [h for h in group if h["source"] == "template"] or [h for h in group if h["source"] == "structure"] or list(group)
    anchor = prefer[0]
    sources = sorted({str(h["source"]) for h in group})
    labels = [str(h.get("label") or "") for h in group if h.get("source") in ("text", "label") and h.get("label")]
    return {
        "x": int(anchor["x_pm"]), "y": int(anchor["y_pm"]),
        "signals": "+".join(sources),
        "label": labels[0] if labels else str(anchor.get("label") or sources[0]),
        "score": max(float(h["score"]) for h in group),
        "full": all(bool(h.get("full")) for h in group),
    }


def _signals_confirm(sources: set) -> bool:
    """Two different readings.

    Shape and template describe the same glyph. A hierarchy label agrees only
    with a template, so a label without one is dropped before the count and
    cannot veto a position-plus-screenshot pair. Position (first button of a
    comment/share row) agrees only with a screenshot signal, not with a label
    from the same dump.
    """
    got = set(sources)
    if "label" in got and "template" not in got:
        got.discard("label")
    if "position" in got and not (got & {"template", "structure", "shape"}):
        got.discard("position")
    if len(got) < 2 or got <= {"shape", "template"}:
        return False
    return True


def _confirming_hits(group: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Hits that actually counted. A lone label must not appear in the tap."""
    sources = {str(h["source"]) for h in group}
    drop = set()
    if "label" in sources and "template" not in sources:
        drop.add("label")
    if "position" in sources and not ((sources - drop) & {"template", "structure", "shape"}):
        drop.add("position")
    return [h for h in group if h["source"] not in drop]


def fuse_signals(text: Sequence[Dict[str, Any]], templates: Sequence[Dict[str, Any]],
                 structure: Sequence[Dict[str, Any]], shapes: Sequence[Dict[str, Any]] = (),
                 labels: Sequence[Dict[str, Any]] = (),
                 positions: Sequence[Dict[str, Any]] = ()) -> Optional[Dict[str, Any]]:
    """Topmost fully visible target. High template, or two agreeing signals.

    Shape and template describe the same glyph, so those two alone do not
    agree. A hierarchy label agrees only with a thumbs-up template. The
    action bar or the Like word can still confirm a template or a silhouette
    when the dump is missing. The leftmost button of a comment/share row
    agrees with a screenshot signal.
    """
    pool = [h for h in (
        list(text) + list(templates) + list(structure) + list(shapes) + list(labels) + list(positions)
    ) if h.get("full")]
    candidates = []
    for hit in pool:
        if hit["source"] == "template" and float(hit["score"]) >= TEMPLATE_HIGH:
            candidates.append(_tap_point([hit]))
    for i, a in enumerate(pool):
        group = [a]
        sources = {a["source"]}
        for b in pool[i + 1:]:
            if b["source"] in sources:
                continue
            if _agree(a, b):
                group.append(b)
                sources.add(b["source"])
        kept = _confirming_hits(group)
        if _signals_confirm({str(h["source"]) for h in kept}):
            candidates.append(_tap_point(kept))
    if not candidates:
        return None
    full = [c for c in candidates if c.get("full")]
    if not full:
        return None
    full.sort(key=lambda c: (c["y"], c["x"], -str(c.get("signals") or "").count("+"), -c["score"]))
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
    for group in _rows_of(blobs, height):
        if _even_icon_row(_prune_icon_row(group)):
            return True
    return False


def _like_slot_blobs(blobs: Sequence[Dict[str, int]], height: int) -> List[Dict[str, int]]:
    """Blobs worth a template read.

    On an even action bar only the leftmost slot can be Like, so the comment
    and share icons are not scored. A lone icon is still scored: a high
    template match does not need the bar.
    """
    chosen: List[Dict[str, int]] = []
    seen = set()
    for group in _rows_of(blobs, height):
        pruned = _prune_icon_row(group)
        pick = [pruned[0]] if _even_icon_row(pruned) else pruned
        for blob in pick:
            key = (blob["x0"], blob["y0"], blob["x1"], blob["y1"])
            if key in seen:
                continue
            seen.add(key)
            chosen.append(blob)
            if len(chosen) >= 16:
                return chosen
    return chosen


def _blank_diag(dump: str) -> Dict[str, Any]:
    return {
        "uiautomator": {
            "dump": dump, "like_found": False,
            "attempts": 0, "via": "", "compressed": False, "error": "",
        },
        "action_bar": {"content_descs": [], "texts": [], "resource_ids": []},
        "template_score": 0.0,
        "template_matched": False,
        "structure_matched": False,
        "structure_bounds": "",
        "shape_matched": False,
        "shape_score": 0.0,
        "position_matched": False,
        "nodes": [],
    }


def locate_like_row(raw: bytes, boxes: Any = (), hierarchy: Any = None) -> Dict[str, Any]:
    """Locate a Like target. ``target`` is None when the bar is not confident.

    ``hierarchy`` is a ``uiautomator dump`` document. A missing or broken dump
    leaves the label signal empty and the screenshot signals still run.

    Return keys: target ({x, y, signals, label, score} permille), post (bool),
    empty_phrase (bool), diag (per-signal report for ``like_probe``). x/y are
    the icon center when a template or the action bar took part, otherwise
    the Like-word center. ``diag`` does not decide the tap.
    """
    norm = normalize_ocr_boxes(boxes)
    try:
        width, height, _hdr, _order = frame_size(raw)
    except ValueError:
        return {
            "target": None, "post": False, "empty_phrase": empty_phrase(norm),
            "diag": _blank_diag(_dump_status(hierarchy) if hierarchy else "empty"),
        }
    blobs = extract_blobs(raw)
    text = text_hits(norm, width, height)
    structure = structure_hits(blobs, norm, width, height, composer_centers(raw))
    templ, template_score = _template_scan(raw, _like_slot_blobs(blobs, height))
    shapes, shape_records = shape_report(raw, blobs)
    labels = label_hits(hierarchy, width, height)
    positions = position_hits(hierarchy, width, height)
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
        shapes = [h for h in shapes if not _blue_hit(h)]
        shape_records = [h for h in shape_records if not _blue_hit(h)]
        labels = [h for h in labels if not _blue_hit(h)]
        positions = [h for h in positions if not _blue_hit(h)]
    target = fuse_signals(text, templ, structure, shapes, labels, positions)
    if target is not None:
        target = {k: target[k] for k in ("x", "y", "signals", "label", "score")}
    diag = hierarchy_diag(hierarchy, width, height)
    diag["template_score"] = round(float(template_score), 3)
    diag["template_matched"] = bool(templ)
    diag["structure_matched"] = bool(structure)
    diag["structure_bounds"] = str(structure[0].get("row_bounds") or "") if structure else ""
    diag["shape_matched"] = bool(shapes)
    if shapes:
        diag["shape_score"] = 1.0
    elif shape_records:
        diag["shape_score"] = round(min(0.99, max(float(item["score"]) for item in shape_records)), 3)
    else:
        diag["shape_score"] = 0.0
    diag["position_matched"] = bool(positions)
    return {
        "target": target,
        "post": post_evidence(norm, blobs, width, height),
        "empty_phrase": empty_phrase(norm),
        "diag": diag,
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
    "TEMPLATE_HIGH", "TEMPLATE_MED", "composer_centers", "empty_phrase", "extract_blobs",
    "fuse_signals", "hierarchy_diag", "label_hits", "like_state_changed", "load_rapidocr", "load_templates",
    "locate_like_row", "normalize_ocr_boxes", "position_hits", "post_evidence", "rapidocr_available",
    "read_text_boxes", "render_thumb", "render_thumb_icon", "shape_hits", "structure_hits", "template_hits",
    "templates_available", "text_hits", "vision_stack_ready", "write_default_templates",
]
