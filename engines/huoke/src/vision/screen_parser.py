"""
ScreenParser — fuses XML hierarchy with Vision-detected elements.

Core algorithm:
  1. Dump XML hierarchy via u2 → structured UIElement list
  2. (Optional) Send screenshot to Vision backend → DetectedElement list
  3. Match Vision coordinates to XML elements (smallest containing element wins)
  4. Produce enriched ParsedElement list with both structural + semantic data

The fusion enables AutoSelector to learn selectors from Vision results:
  Vision says "Send button at (650, 1350)"  →  XML says that coordinate
  falls inside an element with resourceId="send_btn"  →  save the selector.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .backends import DetectedElement, VisionBackend

log = logging.getLogger(__name__)

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class XMLElement:
    """Raw element parsed from Android XML hierarchy."""

    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    class_name: str = ""
    package: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)  # l, t, r, b
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    scrollable: bool = False
    checkable: bool = False
    long_clickable: bool = False
    index: int = 0
    depth: int = 0
    # Phase 17 (2026-04-25): 父节点引用 (在 list 中的索引 + class). 默认 -1
    # 表示无父 (root). 用于 ListView 行级匹配等结构敏感场景.
    parent_index: int = -1
    parent_class: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        return (
            (self.bounds[0] + self.bounds[2]) // 2,
            (self.bounds[1] + self.bounds[3]) // 2,
        )

    @property
    def area(self) -> int:
        return max(0, self.bounds[2] - self.bounds[0]) * max(0, self.bounds[3] - self.bounds[1])

    @property
    def is_interactive(self) -> bool:
        return self.clickable or self.focusable or self.checkable or self.long_clickable

    @property
    def short_id(self) -> str:
        if self.resource_id and "/" in self.resource_id:
            return self.resource_id.split("/", 1)[1]
        return self.resource_id

    def best_selector(self) -> Optional[Dict[str, str]]:
        """Return the single best u2 selector dict for this element."""
        if self.resource_id:
            return {"resourceId": self.resource_id}
        if self.content_desc:
            return {"description": self.content_desc}
        if self.text and len(self.text) < 50:
            return {"text": self.text}
        return None

    def all_selectors(self) -> List[Dict[str, str]]:
        """Return all viable u2 selectors, ordered by reliability."""
        sels: List[Dict[str, str]] = []
        if self.resource_id:
            sels.append({"resourceId": self.resource_id})
        if self.content_desc:
            sels.append({"description": self.content_desc})
        if self.text and len(self.text) < 50:
            sels.append({"text": self.text})
        if self.class_name and self.resource_id:
            sels.append({"className": self.class_name, "resourceId": self.resource_id})
        return sels


@dataclass
class ParsedElement:
    """Enriched element: XML structure + Vision semantics."""

    xml: Optional[XMLElement] = None
    vision: Optional[DetectedElement] = None
    semantic_label: str = ""
    match_confidence: float = 0.0
    selectors: List[Dict[str, str]] = field(default_factory=list)

    @property
    def center(self) -> Tuple[int, int]:
        if self.xml:
            return self.xml.center
        if self.vision:
            return self.vision.center
        return (0, 0)

    @property
    def label(self) -> str:
        if self.semantic_label:
            return self.semantic_label
        if self.vision:
            return self.vision.label
        if self.xml:
            return self.xml.content_desc or self.xml.text or self.xml.short_id
        return ""


# ---------------------------------------------------------------------------
# XML hierarchy parser
# ---------------------------------------------------------------------------

class XMLParser:
    """Parse u2 XML hierarchy into XMLElement list."""

    @staticmethod
    def parse(xml_content: str) -> List[XMLElement]:
        if not xml_content or not xml_content.strip():
            return []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            log.warning("XML parse failed: %s", e)
            return []
        elements: List[XMLElement] = []
        XMLParser._walk(root, elements, depth=0)
        return elements

    @staticmethod
    def _walk(node: ET.Element, out: List[XMLElement], depth: int,
              parent_idx: int = -1, parent_cls: str = ""):
        bs = node.get("bounds", "")
        m = _BOUNDS_RE.match(bs)
        my_idx_in_out = -1
        if m:
            out.append(XMLElement(
                resource_id=node.get("resource-id", ""),
                text=node.get("text", ""),
                content_desc=node.get("content-desc", ""),
                class_name=node.get("class", ""),
                package=node.get("package", ""),
                bounds=(int(m.group(1)), int(m.group(2)),
                        int(m.group(3)), int(m.group(4))),
                clickable=node.get("clickable", "false") == "true",
                enabled=node.get("enabled", "true") == "true",
                focusable=node.get("focusable", "false") == "true",
                scrollable=node.get("scrollable", "false") == "true",
                checkable=node.get("checkable", "false") == "true",
                long_clickable=node.get("long-clickable", "false") == "true",
                index=int(node.get("index", 0)),
                depth=depth,
                # Phase 17 父节点引用
                parent_index=parent_idx,
                parent_class=parent_cls,
            ))
            my_idx_in_out = len(out) - 1
        # 给 children 传当前节点 (有 bounds) 或祖父节点 (本 node 没 bounds 跳过)
        next_parent_idx = my_idx_in_out if my_idx_in_out >= 0 else parent_idx
        next_parent_cls = (node.get("class", "")
                            if my_idx_in_out >= 0 else parent_cls)
        for child in node:
            XMLParser._walk(child, out, depth + 1,
                            parent_idx=next_parent_idx,
                            parent_cls=next_parent_cls)

    @staticmethod
    def find_at_coordinate(elements: List[XMLElement],
                           cx: int, cy: int) -> Optional[XMLElement]:
        """Find the smallest XML element whose bounds contain (cx, cy)."""
        best: Optional[XMLElement] = None
        best_area = float("inf")
        for el in elements:
            x1, y1, x2, y2 = el.bounds
            if x1 <= cx <= x2 and y1 <= cy <= y2:
                a = el.area
                if a < best_area:
                    best = el
                    best_area = a
        return best

    @staticmethod
    def find_interactive(elements: List[XMLElement]) -> List[XMLElement]:
        return [e for e in elements if e.is_interactive and e.enabled]


# ---------------------------------------------------------------------------
# Screen parser (fusion engine)
# ---------------------------------------------------------------------------

class ScreenParser:
    """
    The main entry point for screen understanding.

    Usage:
        parser = ScreenParser(vision_backend)
        result = parser.parse(device)                # XML only (fast)
        result = parser.parse(device, use_vision=True) # XML + Vision (smart)
        element = parser.find(device, "Send button")  # targeted search
    """

    def __init__(self, backend: Optional[VisionBackend] = None):
        self._backend = backend

    def parse_xml(self, device) -> List[XMLElement]:
        """Parse the current screen's XML hierarchy."""
        try:
            xml_content = device.dump_hierarchy()
        except Exception as e:
            log.warning("XML dump failed: %s", e)
            return []
        return XMLParser.parse(xml_content)

    def parse(self, device, use_vision: bool = False,
              context: str = "") -> List[ParsedElement]:
        """
        Full screen parse.
        Returns ParsedElement list with XML structure and optional Vision semantics.
        """
        xml_elements = self.parse_xml(device)

        if not use_vision or not self._backend:
            return [
                ParsedElement(
                    xml=el,
                    semantic_label=el.content_desc or el.text or el.short_id,
                    selectors=el.all_selectors(),
                )
                for el in xml_elements
                if el.is_interactive and el.enabled
            ]

        screenshot_bytes = self._take_screenshot(device)
        if not screenshot_bytes:
            return [
                ParsedElement(xml=el, selectors=el.all_selectors())
                for el in xml_elements if el.is_interactive
            ]

        vision_elements = self._backend.parse_screen(screenshot_bytes, context)
        return self._fuse(xml_elements, vision_elements)

    def find(self, device, target: str,
             context: str = "") -> Optional[ParsedElement]:
        """
        Find a specific element by description.

        Strategy:
          1. Parse XML and look for obvious matches by text/desc
          2. If not found, use Vision backend to locate by screenshot
          3. Match Vision result back to XML for reliable selectors
        """
        xml_elements = self.parse_xml(device)

        # fast path: text/desc match in XML
        xml_match = self._xml_text_search(xml_elements, target)
        if xml_match:
            return ParsedElement(
                xml=xml_match,
                semantic_label=target,
                match_confidence=0.9,
                selectors=xml_match.all_selectors(),
            )

        if not self._backend:
            return None

        screenshot_bytes = self._take_screenshot(device)
        if not screenshot_bytes:
            return None

        vision_result = self._backend.find_element(screenshot_bytes, target, context)
        if not vision_result:
            return None

        # match Vision coordinates back to XML
        xml_at_coord = XMLParser.find_at_coordinate(
            xml_elements, vision_result.center[0], vision_result.center[1]
        )

        return ParsedElement(
            xml=xml_at_coord,
            vision=vision_result,
            semantic_label=target,
            match_confidence=vision_result.confidence,
            selectors=xml_at_coord.all_selectors() if xml_at_coord else [],
        )

    # -- internal -----------------------------------------------------------

    def _fuse(self, xml_elements: List[XMLElement],
              vision_elements: List[DetectedElement]) -> List[ParsedElement]:
        """Match Vision elements to XML elements by coordinate containment."""
        result: List[ParsedElement] = []
        matched_xml_indices: set = set()

        for v_el in vision_elements:
            cx, cy = v_el.center
            xml_match = XMLParser.find_at_coordinate(xml_elements, cx, cy)
            if xml_match:
                idx = id(xml_match)
                matched_xml_indices.add(idx)
                result.append(ParsedElement(
                    xml=xml_match,
                    vision=v_el,
                    semantic_label=v_el.label,
                    match_confidence=v_el.confidence,
                    selectors=xml_match.all_selectors(),
                ))
            else:
                result.append(ParsedElement(
                    vision=v_el,
                    semantic_label=v_el.label,
                    match_confidence=v_el.confidence * 0.7,
                ))

        # add XML-only interactive elements not matched by Vision
        for el in xml_elements:
            if el.is_interactive and el.enabled and id(el) not in matched_xml_indices:
                result.append(ParsedElement(
                    xml=el,
                    semantic_label=el.content_desc or el.text or el.short_id,
                    selectors=el.all_selectors(),
                ))

        return result

    # 仅作用于 target side,避免误伤 element side(很多按钮 desc 就叫 "Settings")
    _TARGET_NOISE_WORDS = {
        # English
        "the", "a", "an", "of", "in", "on", "at", "to", "for", "with",
        "or", "and", "by", "as", "is", "this", "that", "any", "from",
        "into", "main", "bottom", "top", "left", "right", "below", "above",
        "menu", "bar", "icon", "link", "header", "page", "screen",
        "navigation", "section", "first", "second", "third",
        "current", "visible", "matching",
        # Chinese
        "的", "上", "下", "里", "内", "或", "和",
    }

    # 仅作用于双方,标点/小词
    _COMMON_NOISE = {"a", "an", "the", "of", "to"}

    @staticmethod
    def _tokenize(text: str, drop_target_noise: bool = False) -> set:
        """归一化 + 去停用词 — 让 'Groups, tab' 与 'Groups tab in bottom menu'
        都得到 {'groups', 'tab'} 这一核心 token 集。

        drop_target_noise=True 时(给 target 用)再去掉位置词等噪声,
        让 token 真正集中到 noun。
        """
        if not text:
            return set()
        import re
        tokens = set(re.findall(r"[A-Za-z\u4e00-\u9fff]+", text.lower()))
        tokens -= ScreenParser._COMMON_NOISE
        if drop_target_noise:
            tokens -= ScreenParser._TARGET_NOISE_WORDS
        return tokens

    @staticmethod
    def _xml_text_search(elements: List[XMLElement],
                         target: str) -> Optional[XMLElement]:
        """Heuristic: find an interactive XML element whose text/desc matches target.

        Sprint 3 P3 真机加固:
          1. 标点 / 停用词 全部归一化,让 'Groups, tab' 命中 'Groups tab in...'
          2. clickable / clickable-ancestor 元素优先于纯 label
          3. 字段权重 description > text > resource-id 末段
          4. 同 score 取尺寸最大且坐标偏中部(更可能是真按钮)
        """
        target_lower = target.lower().strip()
        target_tokens = ScreenParser._tokenize(target, drop_target_noise=True)
        if not target_tokens:
            return None

        best: Optional[XMLElement] = None
        best_score = 0.0

        for el in elements:
            if not el.enabled:
                continue
            interactive = bool(getattr(el, "clickable", False))
            score = 0.0
            for field_val, field_w in [
                (getattr(el, "content_desc", "") or "", 1.10),
                (getattr(el, "text", "") or "", 1.00),
                (getattr(el, "short_id", "") or "", 0.85),
            ]:
                if not field_val:
                    continue
                fl = field_val.lower()
                if fl == target_lower:
                    return el  # 完美命中,优先返回
                tokens = ScreenParser._tokenize(field_val)
                if not tokens:
                    continue
                overlap = target_tokens & tokens
                if not overlap:
                    continue
                base = len(overlap) / max(len(target_tokens), 1)
                s = base * field_w * (1.20 if interactive else 1.00)
                score = max(score, s)

            if score > best_score and score >= 0.45:
                best = el
                best_score = score

        return best

    @staticmethod
    def _take_screenshot(device) -> Optional[bytes]:
        try:
            img = device.screenshot(format="raw")
            if isinstance(img, bytes):
                return img
            from io import BytesIO
            buf = BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception as e:
            log.error("Screenshot failed: %s", e)
            return None
