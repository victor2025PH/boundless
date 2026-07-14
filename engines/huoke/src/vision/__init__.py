"""
Vision module — self-learning UI understanding engine.

Components:
  backends       — VisionBackend abstraction (LLM, OmniParser, Hybrid)
  screen_parser  — XML + Vision fusion parser
  auto_selector  — Self-learning selector cache (Vision → XML → YAML)
"""

from .backends import (
    DetectedElement,
    VisionBackend,
    LLMVisionBackend,
    OmniParserBackend,
    HybridBackend,
    get_vision_backend,
)
from .screen_parser import (
    XMLElement,
    ParsedElement,
    XMLParser,
    ScreenParser,
)
from .auto_selector import (
    SelectorEntry,
    SelectorStore,
    AutoSelector,
    get_auto_selector,
)

__all__ = [
    "DetectedElement", "VisionBackend", "LLMVisionBackend",
    "OmniParserBackend", "HybridBackend", "get_vision_backend",
    "XMLElement", "ParsedElement", "XMLParser", "ScreenParser",
    "SelectorEntry", "SelectorStore", "AutoSelector", "get_auto_selector",
]
