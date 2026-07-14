"""
Tests for the Vision module: backends, screen_parser, auto_selector.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Test imports
# ---------------------------------------------------------------------------

from src.vision.backends import (
    DetectedElement,
    LLMVisionBackend,
    OmniParserBackend,
    HybridBackend,
    get_vision_backend,
    _fuzzy_label_match,
)
from src.vision.screen_parser import (
    XMLElement,
    XMLParser,
    ParsedElement,
    ScreenParser,
)
from src.vision.auto_selector import (
    SelectorEntry,
    SelectorStore,
    AutoSelector,
)
from src.app_automation.generic_plugin import (
    ActionStep,
    ActionFlow,
    AppDefinition,
    FlowExecutor,
    GenericAppPlugin,
    FlowResult,
)
from src.app_automation.app_registry import AppRegistry


# ===========================================================================
# Fixtures
# ===========================================================================

SAMPLE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.test.app" content-desc="" checkable="false" checked="false"
        clickable="false" enabled="true" focusable="false" focused="false"
        scrollable="false" long-clickable="false" password="false" selected="false"
        bounds="[0,0][1080,1920]">
    <node index="0" text="" resource-id="com.test.app:id/toolbar"
          class="android.widget.LinearLayout" package="com.test.app"
          content-desc="" checkable="false" checked="false" clickable="false"
          enabled="true" focusable="false" focused="false" scrollable="false"
          long-clickable="false" password="false" selected="false"
          bounds="[0,0][1080,120]">
      <node index="0" text="Search" resource-id="com.test.app:id/search_btn"
            class="android.widget.Button" package="com.test.app"
            content-desc="Search button" checkable="false" checked="false"
            clickable="true" enabled="true" focusable="true" focused="false"
            scrollable="false" long-clickable="false" password="false" selected="false"
            bounds="[900,20][1060,100]" />
    </node>
    <node index="1" text="Hello World" resource-id="com.test.app:id/message_input"
          class="android.widget.EditText" package="com.test.app"
          content-desc="" checkable="false" checked="false"
          clickable="true" enabled="true" focusable="true" focused="false"
          scrollable="false" long-clickable="false" password="false" selected="false"
          bounds="[50,1600][950,1700]" />
    <node index="2" text="" resource-id="com.test.app:id/send_btn"
          class="android.widget.ImageButton" package="com.test.app"
          content-desc="Send" checkable="false" checked="false"
          clickable="true" enabled="true" focusable="true" focused="false"
          scrollable="false" long-clickable="false" password="false" selected="false"
          bounds="[960,1600][1060,1700]" />
    <node index="3" text="Not clickable" resource-id=""
          class="android.widget.TextView" package="com.test.app"
          content-desc="" checkable="false" checked="false"
          clickable="false" enabled="true" focusable="false" focused="false"
          scrollable="false" long-clickable="false" password="false" selected="false"
          bounds="[100,200][500,250]" />
  </node>
</hierarchy>
"""


@pytest.fixture
def xml_elements():
    return XMLParser.parse(SAMPLE_XML)


@pytest.fixture
def tmp_selectors_dir():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_apps_dir():
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


class MockLLMClient:
    """Deterministic mock for LLMClient.chat_vision."""

    def __init__(self, responses=None):
        self._responses = responses or {}

    def chat_vision(self, prompt, img_b64, max_tokens=256):
        for key, response in self._responses.items():
            if key in prompt:
                return response
        return json.dumps([
            {"label": "Send", "type": "button", "center_x": 1010, "center_y": 1650, "interactable": True},
            {"label": "Search", "type": "button", "center_x": 980, "center_y": 60, "interactable": True},
        ])


class MockDevice:
    """Mocks a u2 device."""

    def __init__(self, xml=SAMPLE_XML):
        self._xml = xml
        self._clicks = []
        self._texts = []

    def dump_hierarchy(self):
        return self._xml

    def screenshot(self, format="raw"):
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # fake PNG

    def click(self, x, y):
        self._clicks.append((x, y))

    def long_click(self, x, y):
        self._clicks.append(("long", x, y))

    def send_keys(self, text, clear=False):
        self._texts.append(text)

    def swipe(self, x1, y1, x2, y2, duration=0.5):
        pass

    def app_current(self):
        return {"package": "com.test.app", "activity": ".MainActivity"}

    def app_start(self, package):
        pass

    def app_stop(self, package):
        pass

    def __call__(self, **kwargs):
        return MockElement(kwargs, found=True)

    @property
    def info(self):
        return {"currentPackageName": "com.test.app"}


class MockElement:
    def __init__(self, selector, found=True):
        self._selector = selector
        self._found = found

    def exists(self, timeout=5):
        return self._found

    def wait(self, timeout=5):
        return self._found

    @property
    def info(self):
        return {
            "bounds": {"left": 960, "top": 1600, "right": 1060, "bottom": 1700},
            "text": "",
            "contentDescription": "Send",
            "className": "android.widget.ImageButton",
            "clickable": True,
            "enabled": True,
        }

    def click(self):
        pass


# ===========================================================================
# Tests: XMLParser
# ===========================================================================

class TestXMLParser:

    def test_parse_elements(self, xml_elements):
        assert len(xml_elements) >= 4  # root + toolbar + search + input + send + text

    def test_parse_search_button(self, xml_elements):
        search = [e for e in xml_elements if e.short_id == "search_btn"]
        assert len(search) == 1
        assert search[0].clickable is True
        assert search[0].content_desc == "Search button"
        assert search[0].bounds == (900, 20, 1060, 100)

    def test_parse_send_button(self, xml_elements):
        send = [e for e in xml_elements if e.short_id == "send_btn"]
        assert len(send) == 1
        assert send[0].content_desc == "Send"
        assert send[0].center == (1010, 1650)

    def test_find_at_coordinate(self, xml_elements):
        el = XMLParser.find_at_coordinate(xml_elements, 1010, 1650)
        assert el is not None
        assert el.short_id == "send_btn"

    def test_find_at_coordinate_smallest(self, xml_elements):
        """Coordinate inside search button (which is inside toolbar)."""
        el = XMLParser.find_at_coordinate(xml_elements, 980, 60)
        assert el is not None
        assert el.short_id == "search_btn"

    def test_find_at_coordinate_miss(self, xml_elements):
        el = XMLParser.find_at_coordinate(xml_elements, 5000, 5000)
        assert el is None

    def test_find_interactive(self, xml_elements):
        interactive = XMLParser.find_interactive(xml_elements)
        ids = [e.short_id for e in interactive]
        assert "search_btn" in ids
        assert "send_btn" in ids
        assert "message_input" in ids

    def test_empty_xml(self):
        assert XMLParser.parse("") == []
        assert XMLParser.parse("not xml") == []

    def test_best_selector(self, xml_elements):
        search = [e for e in xml_elements if e.short_id == "search_btn"][0]
        sel = search.best_selector()
        assert sel == {"resourceId": "com.test.app:id/search_btn"}

    def test_all_selectors(self, xml_elements):
        search = [e for e in xml_elements if e.short_id == "search_btn"][0]
        sels = search.all_selectors()
        assert len(sels) >= 2
        assert {"resourceId": "com.test.app:id/search_btn"} in sels
        assert {"description": "Search button"} in sels

    def test_area(self, xml_elements):
        search = [e for e in xml_elements if e.short_id == "search_btn"][0]
        assert search.area == (1060 - 900) * (100 - 20)


# ===========================================================================
# Tests: VisionBackends
# ===========================================================================

class TestLLMVisionBackend:

    def test_parse_screen(self):
        mock_client = MockLLMClient()
        backend = LLMVisionBackend(client=mock_client)
        elements = backend.parse_screen(b"fake_png")
        assert len(elements) == 2
        assert elements[0].label == "Send"
        assert elements[0].center == (1010, 1650)

    def test_find_element(self):
        response = json.dumps({"label": "Send", "center_x": 1010, "center_y": 1650, "confidence": 0.9})
        mock_client = MockLLMClient(responses={"Send button": response})
        backend = LLMVisionBackend(client=mock_client)
        result = backend.find_element(b"fake_png", "Send button")
        assert result is not None
        assert result.center == (1010, 1650)
        assert result.confidence == 0.9

    def test_find_element_not_found(self):
        response = json.dumps({"label": "Send", "not_found": True})
        mock_client = MockLLMClient(responses={"Missing": response})
        backend = LLMVisionBackend(client=mock_client)
        result = backend.find_element(b"fake_png", "Missing")
        assert result is None

    def test_parse_markdown_json(self):
        raw = "```json\n[{\"label\":\"OK\",\"type\":\"button\",\"center_x\":540,\"center_y\":960,\"interactable\":true}]\n```"
        elements = LLMVisionBackend._parse_array_response(raw)
        assert len(elements) == 1
        assert elements[0].label == "OK"

    def test_parse_garbage(self):
        assert LLMVisionBackend._parse_array_response("I don't know") == []

    def test_find_element_fallback_coordinates(self):
        response = "The send button is at 1010, 1650 on screen"
        mock_client = MockLLMClient(responses={"Send": response})
        backend = LLMVisionBackend(client=mock_client)
        result = backend.find_element(b"fake_png", "Send")
        assert result is not None
        assert result.center == (1010, 1650)
        assert result.confidence == 0.5


class TestOmniParserBackend:

    def test_unavailable(self):
        backend = OmniParserBackend("http://localhost:99999")
        backend._available = False
        assert backend.parse_screen(b"fake") == []

    def test_convert(self):
        raw = [
            {"label": "Send", "type": "button", "bbox": [960, 1600, 1060, 1700],
             "confidence": 0.95, "interactable": True},
        ]
        result = OmniParserBackend._convert(raw)
        assert len(result) == 1
        assert result[0].center == (1010, 1650)
        assert result[0].bounds == (960, 1600, 1060, 1700)


class TestHybridBackend:

    def test_fallback_to_llm(self):
        omni = OmniParserBackend()
        omni._available = False
        mock_client = MockLLMClient()
        llm = LLMVisionBackend(client=mock_client)
        hybrid = HybridBackend(omni=omni, llm=llm)
        elements = hybrid.parse_screen(b"fake_png")
        assert len(elements) == 2


class TestFuzzyMatch:

    def test_exact(self):
        assert _fuzzy_label_match("Send button", "send button") == 1.0

    def test_partial(self):
        assert _fuzzy_label_match("Send", "send button") == 0.5

    def test_no_match(self):
        assert _fuzzy_label_match("Cancel", "send button") == 0.0

    def test_empty(self):
        assert _fuzzy_label_match("anything", "") == 0.0


class TestGetVisionBackend:

    def test_factory_llm(self):
        b = get_vision_backend("llm")
        assert isinstance(b, LLMVisionBackend)

    def test_factory_omni(self):
        b = get_vision_backend("omniparser")
        assert isinstance(b, OmniParserBackend)

    def test_factory_auto(self):
        b = get_vision_backend("auto")
        assert isinstance(b, HybridBackend)


# ===========================================================================
# Tests: ScreenParser
# ===========================================================================

class TestScreenParser:

    def test_parse_xml_only(self):
        device = MockDevice()
        parser = ScreenParser()
        result = parser.parse(device, use_vision=False)
        assert len(result) > 0
        labels = [p.label for p in result]
        assert "Search button" in labels or "search_btn" in labels

    def test_parse_with_vision(self):
        mock_client = MockLLMClient()
        backend = LLMVisionBackend(client=mock_client)
        device = MockDevice()
        parser = ScreenParser(backend)
        result = parser.parse(device, use_vision=True)
        assert len(result) > 0
        # should have fused elements
        has_send = any("Send" in p.label or "send" in p.label for p in result)
        assert has_send

    def test_find_by_text(self):
        device = MockDevice()
        parser = ScreenParser()
        result = parser.find(device, "Search button")
        assert result is not None
        assert result.xml is not None
        assert result.xml.short_id == "search_btn"

    def test_find_by_text_no_match(self):
        device = MockDevice()
        parser = ScreenParser()
        result = parser.find(device, "Nonexistent element xyz123")
        assert result is None  # no backend available

    def test_find_with_vision_fallback(self):
        response = json.dumps({"label": "Profile", "center_x": 540, "center_y": 960, "confidence": 0.8})
        mock_client = MockLLMClient(responses={"Profile": response})
        backend = LLMVisionBackend(client=mock_client)
        device = MockDevice()
        parser = ScreenParser(backend)
        result = parser.find(device, "Profile button")
        # "Profile button" won't match any XML text, so it falls to Vision
        # Vision returns center (540, 960) which maps to the root FrameLayout
        assert result is not None

    def test_xml_text_search_exact(self):
        elements = XMLParser.parse(SAMPLE_XML)
        result = ScreenParser._xml_text_search(elements, "Send")
        assert result is not None
        assert result.content_desc == "Send"


# ===========================================================================
# Tests: SelectorStore
# ===========================================================================

class TestSelectorStore:

    def test_put_and_get(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        entry = SelectorEntry(
            target="Send button",
            best={"resourceId": "com.test:id/send"},
            alts=[{"description": "Send"}],
            hits=5,
            learned_at="2026-03-12",
        )
        store.put("com.test.app", entry)
        loaded = store.get("com.test.app", "Send button")
        assert loaded is not None
        assert loaded.best == {"resourceId": "com.test:id/send"}
        assert loaded.hits == 5

    def test_persistence(self, tmp_selectors_dir):
        store1 = SelectorStore(tmp_selectors_dir)
        store1.put("com.test.app", SelectorEntry(
            target="btn", best={"text": "OK"}, hits=3,
        ))

        store2 = SelectorStore(tmp_selectors_dir)
        loaded = store2.get("com.test.app", "btn")
        assert loaded is not None
        assert loaded.best == {"text": "OK"}

    def test_record_hit_miss(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        store.put("pkg", SelectorEntry(target="t", best={"text": "X"}, hits=0, misses=0))
        store.record_hit("pkg", "t")
        store.record_hit("pkg", "t")
        store.record_miss("pkg", "t")

        entry = store.get("pkg", "t")
        assert entry.hits == 2
        assert entry.misses == 1
        assert entry.confidence == pytest.approx(2 / 3, abs=0.01)

    def test_list_packages(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        store.put("com.a.b", SelectorEntry(target="x", best={}))
        store.put("org.c.d", SelectorEntry(target="y", best={}))
        pkgs = store.list_packages()
        assert "com.a.b" in pkgs
        assert "org.c.d" in pkgs

    def test_stats(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        store.put("pkg", SelectorEntry(target="a", best={}, hits=10, misses=2))
        store.put("pkg", SelectorEntry(target="b", best={}, hits=5, misses=0))
        s = store.stats("pkg")
        assert s["total_selectors"] == 2

    def test_empty_get(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        assert store.get("nonexistent", "target") is None


# ===========================================================================
# Tests: AutoSelector
# ===========================================================================

class TestAutoSelector:

    def test_learn_and_cache(self, tmp_selectors_dir):
        """First call uses Vision to find, second call uses cache."""
        mock_client = MockLLMClient()
        backend = LLMVisionBackend(client=mock_client)
        store = SelectorStore(tmp_selectors_dir)
        auto = AutoSelector(backend=backend, store=store)
        device = MockDevice()

        # first: learns from XML text match (no Vision needed for "Search button")
        result = auto.find(device, "com.test.app", "Search button")
        assert result is not None

        # verify it was learned
        entry = store.get("com.test.app", "Search button")
        assert entry is not None
        assert entry.best.get("resourceId") == "com.test.app:id/search_btn"

    def test_cached_selector_hit(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        store.put("com.test.app", SelectorEntry(
            target="Send button",
            best={"description": "Send"},
            hits=10,
        ))
        auto = AutoSelector(store=store)
        device = MockDevice()

        result = auto.find(device, "com.test.app", "Send button")
        assert result is not None

        entry = store.get("com.test.app", "Send button")
        assert entry.hits == 11  # incremented

    def test_invalidate(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        store.put("pkg", SelectorEntry(target="a", best={"text": "A"}))
        store.put("pkg", SelectorEntry(target="b", best={"text": "B"}))

        auto = AutoSelector(store=store)
        auto.invalidate("pkg", "a")
        assert store.get("pkg", "a") is None
        assert store.get("pkg", "b") is not None

    def test_invalidate_all(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        store.put("pkg", SelectorEntry(target="a", best={}))
        store.put("pkg", SelectorEntry(target="b", best={}))

        auto = AutoSelector(store=store)
        auto.invalidate("pkg")
        assert store.get("pkg", "a") is None
        assert store.get("pkg", "b") is None

    def test_find_all(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        auto = AutoSelector(store=store)
        device = MockDevice()
        elements = auto.find_all(device, "com.test.app")
        assert len(elements) > 0


# ===========================================================================
# Tests: GenericAppPlugin & FlowExecutor
# ===========================================================================

class TestFlowExecutor:

    def test_simple_flow(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        auto = AutoSelector(store=store)
        executor = FlowExecutor(auto)

        flow = ActionFlow(
            name="test_tap",
            steps=[
                ActionStep(find="Search button", action="tap"),
            ],
        )

        device = MockDevice()
        result = executor.execute(device, "com.test.app", flow)
        assert result.success
        assert result.steps_completed == 1

    def test_type_flow(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        auto = AutoSelector(store=store)
        executor = FlowExecutor(auto)

        flow = ActionFlow(
            name="test_type",
            params=["query"],
            steps=[
                ActionStep(find="Hello World", action="type", param="query"),
            ],
        )

        device = MockDevice()
        result = executor.execute(device, "com.test.app", flow, {"query": "test"})
        assert result.success
        assert "test" in device._texts

    def test_optional_step_skip(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        auto = AutoSelector(store=store)
        executor = FlowExecutor(auto)

        flow = ActionFlow(
            name="test_optional",
            steps=[
                ActionStep(find="Search button", action="tap"),
                ActionStep(find="Nonexistent xyz", action="tap", optional=True),
                ActionStep(find="Send", action="tap"),
            ],
        )

        device = MockDevice()
        result = executor.execute(device, "com.test.app", flow)
        assert result.success

    def test_required_step_fail(self, tmp_selectors_dir):
        store = SelectorStore(tmp_selectors_dir)
        auto = AutoSelector(store=store)
        executor = FlowExecutor(auto)

        flow = ActionFlow(
            name="fail_flow",
            steps=[
                ActionStep(find="Nonexistent element abc123"),
            ],
        )

        device = MockDevice()
        result = executor.execute(device, "com.test.app", flow)
        assert not result.success
        assert "failed" in result.error.lower() or "Step" in result.error


# ===========================================================================
# Tests: AppRegistry
# ===========================================================================

class TestAppRegistry:

    def test_load_yaml(self, tmp_apps_dir):
        config = {
            "package": "com.example.app",
            "name": "TestApp",
            "actions": {
                "search": {
                    "description": "Search",
                    "params": ["query"],
                    "steps": [
                        {"find": "Search bar", "action": "tap"},
                        {"find": "Input", "action": "type", "param": "query"},
                    ],
                },
            },
        }
        (tmp_apps_dir / "test_app.yaml").write_text(
            __import__("yaml").dump(config), encoding="utf-8"
        )

        registry = AppRegistry(config_dir=tmp_apps_dir)
        apps = registry.list_apps()
        assert len(apps) == 1
        assert apps[0]["name"] == "TestApp"
        assert "search" in apps[0]["actions"]

    def test_get_definition(self, tmp_apps_dir):
        config = {
            "package": "com.ex.app",
            "name": "MyApp",
            "actions": {},
        }
        (tmp_apps_dir / "myapp.yaml").write_text(
            __import__("yaml").dump(config), encoding="utf-8"
        )

        registry = AppRegistry(config_dir=tmp_apps_dir)
        d = registry.get_definition("myapp")
        assert d is not None
        assert d.package == "com.ex.app"

        d2 = registry.get_definition("com.ex.app")
        assert d2 is not None
        assert d2.name == "MyApp"

    def test_register_programmatic(self, tmp_apps_dir):
        registry = AppRegistry(config_dir=tmp_apps_dir)
        app_def = AppDefinition(
            package="com.dynamic.app",
            name="DynamicApp",
        )
        registry.register_app(app_def)
        assert registry.get_definition("dynamicapp") is not None

    def test_save_app(self, tmp_apps_dir):
        registry = AppRegistry(config_dir=tmp_apps_dir)
        app_def = AppDefinition(
            package="com.saved.app",
            name="SavedApp",
            actions={
                "tap_btn": ActionFlow(
                    name="tap_btn",
                    description="Tap a button",
                    steps=[ActionStep(find="OK button", action="tap")],
                ),
            },
        )
        registry.register_app(app_def)
        registry.save_app("savedapp")

        # reload and verify
        registry2 = AppRegistry(config_dir=tmp_apps_dir)
        d = registry2.get_definition("savedapp")
        assert d is not None
        assert "tap_btn" in d.actions
        assert d.actions["tap_btn"].steps[0].find == "OK button"

    def test_reload(self, tmp_apps_dir):
        registry = AppRegistry(config_dir=tmp_apps_dir)
        assert len(registry.list_apps()) == 0

        config = {"package": "com.new", "name": "NewApp", "actions": {}}
        (tmp_apps_dir / "new.yaml").write_text(
            __import__("yaml").dump(config), encoding="utf-8"
        )
        registry.reload()
        assert len(registry.list_apps()) == 1


# ===========================================================================
# Tests: SelectorEntry
# ===========================================================================

class TestSelectorEntry:

    def test_confidence(self):
        e = SelectorEntry(target="t", hits=9, misses=1)
        assert e.confidence == pytest.approx(0.9)

    def test_confidence_zero(self):
        e = SelectorEntry(target="t", hits=0, misses=0)
        assert e.confidence == 0.5

    def test_all_selectors(self):
        e = SelectorEntry(
            target="t",
            best={"resourceId": "x"},
            alts=[{"text": "Y"}, {"description": "Z"}],
        )
        sels = e.all_selectors()
        assert len(sels) == 3
        assert sels[0] == {"resourceId": "x"}
