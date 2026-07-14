"""阶段 2B：VLM 空答换端点重试（empty failover）门禁。

覆盖 ``_describe_openai_sync`` 的空答语义分叉：
- 默认关（allow_empty_failover=False）＝旧语义（空答直接放弃，不烧第二块 GPU）；
- 开启后空答最多换 1 个端点再试；两连空按放弃；单端点无处可换不重试；
- 空答不进端点冷却；transport 异常切换不受开关影响；
- 观测标签 empty_failover_try / empty_failover_rescued；
- 链层按 ``vision.empty_retry.enabled`` 传旗标（UI 直调路径恒为 False）。

复用 test_vision_fallback.py 的 _FakeOpenAI 桩模式（无网络）。
"""
from __future__ import annotations

import pytest

import src.vision_client as vc_mod
from src.ai.provider_stats import get_provider_stats
from src.vision_client import VisionClient, _empty_retry_enabled


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.message = _FakeMsg(content)


class _FakeResp:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeOpenAI:
    """按 base_url 决定行为：behaviors[url] = Exception(抛) / str(返回) / None(空答)。"""

    behaviors: dict = {}
    calls: list = []

    def __init__(self, api_key=None, base_url=None, timeout=None, **kw):
        self._url = base_url
        outer = self

        class _Completions:
            def create(self, **kw):
                _FakeOpenAI.calls.append(outer._url)
                b = _FakeOpenAI.behaviors.get(outer._url)
                if isinstance(b, Exception):
                    raise b
                return _FakeResp(b)

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def _fake_openai(monkeypatch):
    monkeypatch.setattr(vc_mod, "OpenAI", _FakeOpenAI)
    monkeypatch.setattr(vc_mod, "OPENAI_SYNC_AVAILABLE", True)
    monkeypatch.setattr(
        vc_mod, "_image_to_data_url", lambda *a, **k: "data:image/jpeg;base64,x"
    )
    _FakeOpenAI.behaviors = {}
    _FakeOpenAI.calls = []
    vc_mod._URL_BAD_UNTIL.clear()
    get_provider_stats("vision").reset()
    yield
    vc_mod._URL_BAD_UNTIL.clear()
    get_provider_stats("vision").reset()


def _mk_client(urls):
    c = VisionClient(
        {"provider": "openai_compatible", "base_urls": list(urls), "model": "vlm"}
    )
    assert c.initialize()
    return c


# ── 配置解析 ─────────────────────────────────────────────────────────

def test_empty_retry_enabled_parser():
    assert _empty_retry_enabled({"empty_retry": {"enabled": True}}) is True
    assert _empty_retry_enabled({"empty_retry": {"enabled": False}}) is False
    assert _empty_retry_enabled({"empty_retry": {}}) is False
    assert _empty_retry_enabled({"empty_retry": True}) is True   # 简写 bool
    assert _empty_retry_enabled({}) is False                     # 缺省=旧行为


# ── 空答语义 ─────────────────────────────────────────────────────────

def test_disabled_keeps_old_semantics(_fake_openai):
    """开关关（默认）：空答直接 None，第二端点不被触碰。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": None, "http://b:2/v1": "alive"}
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg", allow_empty_failover=False) is None
    assert _FakeOpenAI.calls == ["http://a:1/v1"]
    d = get_provider_stats("vision").dump()
    assert "empty_failover_try" not in d["labels"]


def test_rescues_on_second_endpoint(_fake_openai):
    """开关开：a 空答 → 换 b 成功 → 返回 b 的描述；记 try+rescued；空答不进冷却。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": None, "http://b:2/v1": "b 的描述"}
    c = _mk_client(["http://a:1", "http://b:2"])
    out = c._describe_openai_sync("f.jpg", allow_empty_failover=True)
    assert out == "b 的描述"
    assert _FakeOpenAI.calls == ["http://a:1/v1", "http://b:2/v1"]
    assert not vc_mod._URL_BAD_UNTIL  # 空答不算端点故障
    labels = get_provider_stats("vision").dump()["labels"]
    assert labels.get("empty_failover_try") == 1
    assert labels.get("empty_failover_rescued") == 1


def test_both_empty_caps_at_one_extra(_fake_openai):
    """三端点全备但 a、b 连空 → 只多试 1 个（不试 c），返回 None；rescued 不记。"""
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": None, "http://b:2/v1": None, "http://c:3/v1": "never",
    }
    c = _mk_client(["http://a:1", "http://b:2", "http://c:3"])
    assert c._describe_openai_sync("f.jpg", allow_empty_failover=True) is None
    assert _FakeOpenAI.calls == ["http://a:1/v1", "http://b:2/v1"]
    labels = get_provider_stats("vision").dump()["labels"]
    assert labels.get("empty_failover_try") == 1
    assert "empty_failover_rescued" not in labels


def test_single_endpoint_no_retry_no_label(_fake_openai):
    """单端点无处可换：空答直接 None，不记 try（防指标虚增）。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": None}
    c = _mk_client(["http://a:1"])
    assert c._describe_openai_sync("f.jpg", allow_empty_failover=True) is None
    assert _FakeOpenAI.calls == ["http://a:1/v1"]
    d = get_provider_stats("vision").dump()
    assert "empty_failover_try" not in d["labels"]


def test_transport_failover_unaffected_by_flag(_fake_openai):
    """transport 异常切换是既有语义，与本开关无关（关着也切）。"""
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": RuntimeError("conn refused"), "http://b:2/v1": "ok",
    }
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg", allow_empty_failover=False) == "ok"
    assert "http://a:1/v1" in vc_mod._URL_BAD_UNTIL  # transport 失败才进冷却


def test_transport_fail_then_empty_no_third(_fake_openai):
    """a transport 挂 + b 空答（已是末端点）→ None；不记 try（无处可换）。"""
    _FakeOpenAI.behaviors = {
        "http://a:1/v1": RuntimeError("down"), "http://b:2/v1": None,
    }
    c = _mk_client(["http://a:1", "http://b:2"])
    assert c._describe_openai_sync("f.jpg", allow_empty_failover=True) is None
    d = get_provider_stats("vision").dump()
    assert "empty_failover_try" not in d["labels"]


# ── 链层旗标传递 ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_passes_flag_from_config(monkeypatch):
    """_describe_fallback_chain 按 vision.empty_retry.enabled 传 allow_empty_failover。"""
    captured = {}

    def fake_init(self):
        self._backend = "openai"
        return True

    async def fake_describe(self, image_path, prompt=None, *, allow_empty_failover=False):
        captured["allow"] = allow_empty_failover
        return "desc"

    monkeypatch.setattr(VisionClient, "initialize", fake_init)
    monkeypatch.setattr(VisionClient, "describe_image", fake_describe)

    base = {"provider": "openai_compatible", "base_url": "http://x:11434/v1", "model": "m"}

    txt, tag = await VisionClient._describe_fallback_chain(
        {**base, "empty_retry": {"enabled": True}}, {}, "f.jpg", "p")
    assert (txt, tag) == ("desc", "ollama_ok")
    assert captured["allow"] is True

    await VisionClient._describe_fallback_chain(dict(base), {}, "f.jpg", "p")
    assert captured["allow"] is False


@pytest.mark.asyncio
async def test_direct_describe_image_defaults_off(monkeypatch, _fake_openai):
    """UI 直调路径（describe_image 不带旗标）恒旧语义：空答不换端点。"""
    _FakeOpenAI.behaviors = {"http://a:1/v1": None, "http://b:2/v1": "alive"}
    c = _mk_client(["http://a:1", "http://b:2"])
    out = await c.describe_image("f.jpg")
    assert out is None
    assert _FakeOpenAI.calls == ["http://a:1/v1"]
