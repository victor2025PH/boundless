"""
Vision Backend — provider-agnostic screen understanding.

Backends:
  LLMVisionBackend  — uses LLMClient.chat_vision (works immediately, no extra infra)
  OmniParserBackend — connects to OmniParser V2 HTTP API (Docker, optional GPU)

All backends return the same DetectedElement list so downstream code
(ScreenParser, AutoSelector) never cares about the provider.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class DetectedElement:
    """A UI element detected by a vision backend."""

    label: str
    element_type: str  # button, input, text, icon, image, toggle, list_item, tab
    center: Tuple[int, int] = (0, 0)
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    confidence: float = 0.0
    interactable: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class VisionBackend(ABC):
    """Interface every vision provider must implement."""

    @abstractmethod
    def parse_screen(self, screenshot_bytes: bytes,
                     context: str = "") -> List[DetectedElement]:
        """Detect ALL interactive UI elements in a screenshot."""

    @abstractmethod
    def find_element(self, screenshot_bytes: bytes,
                     target: str,
                     context: str = "") -> Optional[DetectedElement]:
        """Find ONE specific element by natural-language description."""


# ---------------------------------------------------------------------------
# LLM-based backend (works with DeepSeek, GPT-4o, Qwen-VL, etc.)
# ---------------------------------------------------------------------------

_PARSE_SCREEN_PROMPT = """\
Analyze this mobile app screenshot. List ALL interactive UI elements.

For each element return a JSON object:
  label       — what the element is ("Send button", "Search field", "Home tab")
  type        — button | input | text | icon | image | toggle | list_item | tab
  center_x    — horizontal pixel coordinate of the element center
  center_y    — vertical pixel coordinate of the element center
  interactable — true if the user can tap/type on it

Return ONLY a JSON array.  Example:
[{"label":"Send","type":"button","center_x":650,"center_y":1350,"interactable":true}]
"""

_FIND_ELEMENT_PROMPT = """\
Find the UI element: "{target}"

Return ONLY a JSON object:
{{"label":"{target}","center_x":N,"center_y":N,"confidence":0.0-1.0}}

If the element is NOT visible, return:
{{"label":"{target}","not_found":true}}
"""


class LLMVisionBackend(VisionBackend):
    """Multimodal LLM as the vision engine."""

    def __init__(self, client=None):
        if client is None:
            from ..ai.llm_client import get_llm_client
            client = get_llm_client()
        self._client = client

    # -- full screen parse --------------------------------------------------

    def parse_screen(self, screenshot_bytes: bytes,
                     context: str = "") -> List[DetectedElement]:
        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        prompt = _PARSE_SCREEN_PROMPT
        if context:
            prompt += f"\nContext: {context}"

        raw = self._client.chat_vision(prompt, img_b64, max_tokens=2000)
        return self._parse_array_response(raw)

    # -- single element find ------------------------------------------------

    def find_element(self, screenshot_bytes: bytes,
                     target: str,
                     context: str = "") -> Optional[DetectedElement]:
        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
        prompt = _FIND_ELEMENT_PROMPT.format(target=target)
        if context:
            prompt += f"\nContext: {context}"

        raw = self._client.chat_vision(prompt, img_b64, max_tokens=300)
        return self._parse_single_response(raw, target)

    # -- JSON parsing helpers -----------------------------------------------

    @staticmethod
    def _extract_json(text: str) -> str:
        """Strip markdown fences and surrounding prose to isolate JSON."""
        text = text.strip()
        fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
        if fence:
            return fence.group(1).strip()
        # try to find array or object
        for ch, end in [("[", "]"), ("{", "}")]:
            start_i = text.find(ch)
            end_i = text.rfind(end)
            if start_i != -1 and end_i > start_i:
                return text[start_i : end_i + 1]
        return text

    @classmethod
    def _parse_array_response(cls, raw: str) -> List[DetectedElement]:
        if not raw:
            return []
        try:
            data = json.loads(cls._extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            log.warning("Vision backend returned non-JSON: %.200s", raw)
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        elements: List[DetectedElement] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cx = int(item.get("center_x", 0))
            cy = int(item.get("center_y", 0))
            if cx <= 0 or cy <= 0:
                continue
            elements.append(DetectedElement(
                label=str(item.get("label", "")),
                element_type=str(item.get("type", "unknown")),
                center=(cx, cy),
                confidence=float(item.get("confidence", 0.7)),
                interactable=bool(item.get("interactable", True)),
            ))
        return elements

    @classmethod
    def _parse_single_response(cls, raw: str,
                                target: str) -> Optional[DetectedElement]:
        if not raw:
            return None
        try:
            data = json.loads(cls._extract_json(raw))
        except (json.JSONDecodeError, ValueError):
            # fallback: look for "COORDINATES: x, y"
            m = re.search(r"(\d{2,4})\s*,\s*(\d{2,4})", raw)
            if m:
                cx, cy = int(m.group(1)), int(m.group(2))
                if 0 < cx < 3000 and 0 < cy < 5000:
                    return DetectedElement(
                        label=target, element_type="unknown",
                        center=(cx, cy), confidence=0.5,
                    )
            return None

        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return None
        if data.get("not_found"):
            return None
        cx = int(data.get("center_x", 0))
        cy = int(data.get("center_y", 0))
        if cx <= 0 or cy <= 0:
            return None
        return DetectedElement(
            label=str(data.get("label", target)),
            element_type=str(data.get("type", "unknown")),
            center=(cx, cy),
            confidence=float(data.get("confidence", 0.7)),
        )


# ---------------------------------------------------------------------------
# OmniParser V2 backend (HTTP API to Docker-deployed model)
# ---------------------------------------------------------------------------

class OmniParserBackend(VisionBackend):
    """
    Connects to a locally deployed OmniParser V2 instance.

    Expected API:
      POST /parse  body: {"image": "<base64>"}
      → {"elements": [{"label": ..., "bbox": [x1,y1,x2,y2], ...}]}
    """

    def __init__(self, api_url: str = "http://localhost:8080"):
        self._url = api_url.rstrip("/")
        self._available: Optional[bool] = None

    def _is_available(self) -> bool:
        if self._available is not None:
            return self._available
        try:
            import httpx
            resp = httpx.get(f"{self._url}/health", timeout=3)
            self._available = resp.status_code == 200
        except Exception:
            self._available = False
        return self._available

    def parse_screen(self, screenshot_bytes: bytes,
                     context: str = "") -> List[DetectedElement]:
        if not self._is_available():
            return []
        try:
            import httpx
            img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            resp = httpx.post(
                f"{self._url}/parse",
                json={"image": img_b64, "context": context},
                timeout=15,
            )
            data = resp.json()
            return self._convert(data.get("elements", []))
        except Exception as e:
            log.warning("OmniParser parse_screen failed: %s", e)
            return []

    def find_element(self, screenshot_bytes: bytes,
                     target: str,
                     context: str = "") -> Optional[DetectedElement]:
        elements = self.parse_screen(screenshot_bytes, context)
        target_lower = target.lower()
        best = None
        best_score = 0.0
        for el in elements:
            score = _fuzzy_label_match(el.label, target_lower)
            if score > best_score:
                best_score = score
                best = el
        if best and best_score > 0.3:
            return best
        return None

    @staticmethod
    def _convert(raw_elements: list) -> List[DetectedElement]:
        out: List[DetectedElement] = []
        for item in raw_elements:
            bbox = item.get("bbox", [0, 0, 0, 0])
            if len(bbox) < 4:
                continue
            x1, y1, x2, y2 = [int(v) for v in bbox[:4]]
            out.append(DetectedElement(
                label=str(item.get("label", item.get("text", ""))),
                element_type=str(item.get("type", "unknown")),
                center=((x1 + x2) // 2, (y1 + y2) // 2),
                bounds=(x1, y1, x2, y2),
                confidence=float(item.get("confidence", 0.8)),
                interactable=bool(item.get("interactable", True)),
            ))
        return out


# ---------------------------------------------------------------------------
# Hybrid backend: OmniParser for detection, LLM for understanding
# ---------------------------------------------------------------------------

class HybridBackend(VisionBackend):
    """
    Best of both worlds:
    - Try OmniParser first (fast, accurate element detection)
    - Fall back to LLM if OmniParser unavailable or returns empty
    """

    def __init__(self, omni: Optional[OmniParserBackend] = None,
                 llm: Optional[LLMVisionBackend] = None):
        self._omni = omni or OmniParserBackend()
        self._llm = llm or LLMVisionBackend()

    def parse_screen(self, screenshot_bytes, context=""):
        result = self._omni.parse_screen(screenshot_bytes, context)
        if result:
            return result
        return self._llm.parse_screen(screenshot_bytes, context)

    def find_element(self, screenshot_bytes, target, context=""):
        result = self._omni.find_element(screenshot_bytes, target, context)
        if result:
            return result
        return self._llm.find_element(screenshot_bytes, target, context)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fuzzy_label_match(label: str, target: str) -> float:
    """Simple word-overlap similarity for matching Vision labels to targets."""
    label_words = set(label.lower().split())
    target_words = set(target.split())
    if not target_words:
        return 0.0
    overlap = label_words & target_words
    return len(overlap) / len(target_words)


def get_vision_backend(backend_type: str = "auto",
                       **kwargs) -> VisionBackend:
    """Factory: create the appropriate vision backend."""
    if backend_type == "omniparser":
        return OmniParserBackend(**kwargs)
    if backend_type == "llm":
        return LLMVisionBackend(**kwargs)
    if backend_type == "hybrid":
        return HybridBackend(**kwargs)
    # auto: try hybrid (OmniParser + LLM fallback)
    return HybridBackend()
