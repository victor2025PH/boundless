"""Facebook post safety: caption before publish, and fail closed when asked to confirm.

This module does not touch the device. Callers drop a blind ``media_next`` tap.
A live tap is allowed only when a window dump shows a control that is Next and
not Post, Share, or Publish. ``assess_publish`` reports ``confirmed`` only when
a success phrase is visible and a publish control is not.

Caption text stays the ASCII subset used by ``phone_text`` (1–200 characters).
Non-ASCII, including Chinese, is rejected upstream as ``text_non_ascii_unsupported``.
Typing through an IME is out of scope here.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree

MIN_SEPARATION_PERMILLE = 80
_OVERLAP_PX = 24
_OVERLAP_IOU = 0.25

PUBLISH_LABELS = frozenset({
    "post", "share", "publish", "share now", "post now",
    "发布", "发帖", "分享", "投稿", "發佈", "發布",
})
NEXT_LABELS = frozenset({
    "next", "continue",
    "下一步", "继续", "繼續", "次へ", "siguiente",
})
# Substring phrases. The word "post" alone is not success.
SUCCESS_PHRASES = (
    "post shared", "your post is now", "posted to",
    "发布成功", "已发布", "已發佈", "已分享", "分享成功",
)

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def min_sep_px(width: int, height: int) -> int:
    """Pixel gap that counts as the same control. At least 24px, or 80 permille of the short side."""
    side = min(int(width), int(height))
    if side < 1:
        return 24
    return max(24, MIN_SEPARATION_PERMILLE * side // 1000)


def anchors_separated(a: Sequence[int], b: Sequence[int], minimum: int = MIN_SEPARATION_PERMILLE) -> bool:
    """True when two permille anchors are at least ``minimum`` apart."""
    dx = int(a[0]) - int(b[0])
    dy = int(a[1]) - int(b[1])
    limit = int(minimum)
    return dx * dx + dy * dy >= limit * limit


def points_collide(x1: int, y1: int, x2: int, y2: int, width: int, height: int) -> bool:
    """True when two pixels are inside the separation radius (a blind tap could hit both)."""
    limit = min_sep_px(width, height)
    dx = int(x1) - int(x2)
    dy = int(y1) - int(y2)
    return dx * dx + dy * dy < limit * limit


def _norm(label: str) -> str:
    return " ".join(str(label or "").split()).casefold()


def _role_one(label: str) -> str:
    text = _norm(label)
    if not text:
        return ""
    if text in PUBLISH_LABELS:
        return "publish"
    if text in NEXT_LABELS:
        return "next"
    return "other"


def _role(text: str, desc: str) -> str:
    roles = {_role_one(text), _role_one(desc)} - {""}
    if "publish" in roles and "next" in roles:
        return "ambiguous"
    if "publish" in roles:
        return "publish"
    if "next" in roles:
        return "next"
    return "other"


def _bounds(raw: str) -> Optional[Tuple[int, int, int, int]]:
    match = _BOUNDS_RE.search(str(raw or ""))
    if not match:
        return None
    x1, y1, x2, y2 = (int(match.group(i)) for i in range(1, 5))
    if x2 < x1 or y2 < y1:
        return None
    return x1, y1, x2, y2


def _center(bounds: Tuple[int, int, int, int]) -> Tuple[int, int]:
    return (bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / union


def _overlaps(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> bool:
    if _iou(a, b) >= _OVERLAP_IOU:
        return True
    acx, acy = _center(a)
    bcx, bcy = _center(b)
    dx, dy = acx - bcx, acy - bcy
    if dx * dx + dy * dy <= _OVERLAP_PX * _OVERLAP_PX:
        return True
    return b[0] <= acx <= b[2] and b[1] <= acy <= b[3]


def _xml_body(xml: str) -> str:
    text = xml if isinstance(xml, str) else ""
    start = text.find("<")
    if start < 0:
        return ""
    return text[start:]


def parse_nodes(xml: str) -> List[Dict[str, Any]]:
    """Clickable-ish nodes with a role of next, publish, ambiguous, or other.

    A node is tappable when it is clickable, its class name contains Button,
    or its immediate parent is clickable. Parse errors and empty dumps yield
    an empty list.
    """
    body = _xml_body(xml)
    if "<node" not in body and "<hierarchy" not in body:
        return []
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return []
    found: List[Dict[str, Any]] = []

    def walk(el: ElementTree.Element, parent_clickable: bool) -> None:
        own = str(el.attrib.get("clickable") or "").lower() == "true"
        if el.tag == "node":
            klass = str(el.attrib.get("class") or "")
            bounds = _bounds(str(el.attrib.get("bounds") or ""))
            tappable = own or parent_clickable or "button" in klass.casefold()
            text = str(el.attrib.get("text") or "")
            desc = str(el.attrib.get("content-desc") or "")
            item: Dict[str, Any] = {
                "role": _role(text, desc),
                "tappable": tappable,
                "bounds": bounds,
                "text": text,
                "desc": desc,
            }
            if bounds is not None:
                item["x"], item["y"] = _center(bounds)
            found.append(item)
        child_parent = own if el.tag == "node" else parent_clickable
        for child in list(el):
            walk(child, child_parent)

    walk(root, False)
    return found


def distinct_next_point(xml: str) -> Optional[Tuple[int, int]]:
    """Center of a Next control that is not also Post/Share/Publish.

    Empty, Post, Share, Publish, or a Next overlapped by a publish control
    returns None. Several Next controls: the top-right one (largest x, then
    smallest y). A real Next button may sit on the same corner as Publish;
    the label is what makes the tap legal, not the distance from post_submit.
    """
    candidates = []
    blockers = []
    for node in parse_nodes(xml):
        bounds = node.get("bounds")
        if bounds is None:
            continue
        role = node.get("role")
        if role in ("publish", "ambiguous"):
            blockers.append(bounds)
            continue
        if role == "next" and node.get("tappable"):
            candidates.append(node)
    if not candidates:
        return None
    candidates.sort(key=lambda node: (-int(node["x"]), int(node["y"])))
    for node in candidates:
        bounds = node["bounds"]
        if any(_overlaps(bounds, block) for block in blockers):
            continue
        return int(node["x"]), int(node["y"])
    return None


def ocr_vetoes_next(lines: Sequence[str]) -> bool:
    """An exact publish label in optional OCR text cancels a Next tap."""
    for line in lines:
        if _role_one(str(line)) == "publish":
            return True
    return False


def assess_publish(xml: str, extra_lines: Optional[Sequence[str]] = None) -> str:
    """``confirmed`` or ``unproven``.

    Confirmed only when the dump parsed to at least one node, a success phrase
    is present, and no tappable Post/Share/Publish control remains. An empty
    dump is unproven even if ``extra_lines`` contains a success phrase: the
    publish button might still be there. A screenshot change is not success.
    """
    if not isinstance(xml, str) or "<node" not in xml:
        return "unproven"
    nodes = parse_nodes(xml)
    if not nodes:
        return "unproven"
    for node in nodes:
        if node.get("tappable") and node.get("role") in ("publish", "ambiguous"):
            return "unproven"
    parts = [xml]
    if extra_lines:
        parts.extend(str(line) for line in extra_lines if line)
    blob = "\n".join(parts).casefold()
    if any(phrase.casefold() in blob for phrase in SUCCESS_PHRASES):
        return "confirmed"
    return "unproven"


__all__ = [
    "MIN_SEPARATION_PERMILLE", "PUBLISH_LABELS", "NEXT_LABELS", "SUCCESS_PHRASES",
    "min_sep_px", "anchors_separated", "points_collide", "parse_nodes",
    "distinct_next_point", "ocr_vetoes_next", "assess_publish",
]
