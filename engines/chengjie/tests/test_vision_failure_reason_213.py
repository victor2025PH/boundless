"""#213（L-6 A）：识图失败原因人话 + 智谱兜底不再拿设备令牌当 key。

钧机 1.0.74 诊断包 MG65CT 实锤的两段链：
  ① 网关 400「request (4170 tokens) exceeds the available context size (4096)」
  ② 回落智谱 → 401「令牌已过期或验证不正确」——其实是 hosted_gateway 写进 vision.api_key
     的设备令牌 ``cx.…`` 被 _zhipu_credentials 当成智谱 key 送了出去。
工作台两段都只显示「识别翻译不可用」。本文件钉：
  - _zhipu_credentials：cx. 令牌 / OpenAI 兼容段的 api_key 不算智谱 key；zhipu_api_key 与
    provider=zhipu 的 api_key 照旧；
  - classify_vision_error / vision_failure_reason：异常 → kind → 三档原因码；
  - _openai_vision_request：全端点抛异常 → last_fail；有端点空答 → 不算失败；
  - _describe_fallback_chain 的 tag：ollama_failed:<kind> 与 ollama_empty 分开，_backend_from_tag 归因不变；
  - 路由 _attach_vision_failure_message：按 ocr_tag 补 message / vision_reason。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from src import vision_client as vc_mod
from src.vision_client import (
    VisionClient,
    _backend_from_tag,
    _zhipu_credentials,
    classify_vision_error,
    vision_failure_reason,
)


# ── 智谱凭据判定 ──────────────────────────────────────────────────────

def test_device_token_in_api_key_is_not_zhipu_key():
    hosted = {"provider": "openai_compatible", "base_url": "https://bd2026.cc/api/ai/v1",
              "api_key": "cx.eyJ2IjoxfQ.sig", "model": "qwen3-vl:8b-instruct"}
    assert _zhipu_credentials(hosted, hosted) is None


def test_openai_compat_api_key_is_endpoint_key_not_zhipu():
    lan = {"provider": "openai_compatible", "base_url": "http://192.168.0.176:11434",
           "api_key": "lmstudio", "model": "qwen3-vl:8b-instruct"}
    assert _zhipu_credentials(lan, lan) is None


def test_zhipu_api_key_still_used_for_fallback():
    lan = {"provider": "openai_compatible", "base_url": "http://192.168.0.176:11434",
           "api_key": "ollama", "model": "qwen3-vl:8b-instruct",
           "zhipu_api_key": "real.zhipu.key", "zhipu_model": "glm-4v-plus"}
    creds = _zhipu_credentials(lan, lan)
    assert creds == {"api_key": "real.zhipu.key", "model": "glm-4v-plus"}


def test_zhipu_provider_api_key_still_accepted():
    zh = {"provider": "zhipu", "api_key": "real.zhipu.key", "model": "glm-4v-flash"}
    assert _zhipu_credentials(zh, {}) == {"api_key": "real.zhipu.key", "model": "glm-4v-flash"}


def test_zhipu_api_key_that_is_device_token_rejected():
    cfg = {"provider": "zhipu", "zhipu_api_key": "cx.abc.def"}
    assert _zhipu_credentials(cfg, cfg) is None


# ── 异常归类 / 原因码 ────────────────────────────────────────────────

def test_classify_context_overflow_from_gateway_400():
    exc = RuntimeError(
        "Error code: 400 - {'error': {'message': '{\"error\":{\"code\":400,\"message\":"
        "\"request (4170 tokens) exceeds the available context size (4096 tokens), try "
        "increasing it\",\"type\":\"exceed_context_size_error\"}}'}}")
    assert classify_vision_error(exc) == "context_overflow"


def test_classify_other_kinds():
    assert classify_vision_error(RuntimeError("Error code: 401 令牌已过期")) == "auth"
    assert classify_vision_error(TimeoutError("read timed out")) == "timeout"
    assert classify_vision_error(ConnectionError("connection refused")) == "unreachable"
    assert classify_vision_error(RuntimeError("Error code: 502 upstream_error")) == "upstream"
    assert classify_vision_error(RuntimeError("Error code: 429 quota_exceeded")) == "rate_limited"
    assert classify_vision_error(ValueError("weird")) == "error"


@pytest.mark.parametrize("tag,expected", [
    ("ollama_failed:context_overflow", "busy"),
    ("ollama_failed:timeout|no_cloud_fallback", "busy"),
    ("ollama_empty|zhipu_failed:auth", "busy"),
    ("zhipu_failed:auth", "busy"),
    ("ollama_unavailable", "unconfigured"),
    ("vision_client_init_fail", "unconfigured"),
    ("ollama_unavailable|zhipu_init_fail", "unconfigured"),
    ("ollama_empty", "no_text"),
    ("ollama_empty_no_zhipu_key", "no_text"),
    ("ollama_empty|zhipu_empty", "no_text"),
    ("ollama_empty|no_cloud_fallback", "no_text"),
    ("", "busy"),
])
def test_vision_failure_reason_mapping(tag, expected):
    assert vision_failure_reason(tag) == expected


def test_backend_attribution_unchanged_for_failed_tags():
    assert _backend_from_tag("ollama_failed:context_overflow") == "ollama"
    assert _backend_from_tag("ollama_failed:timeout|no_cloud_fallback") == "none"  # 末段规则照旧
    assert _backend_from_tag("ollama_empty|zhipu_failed:auth") == "zhipu"


# ── 端点循环：失败 vs 空答 ───────────────────────────────────────────

class _Resp:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content))]


class _Cli:
    def __init__(self, behaviour):
        self._b = behaviour
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        if isinstance(self._b, BaseException):
            raise self._b
        return _Resp(self._b)


def _client_with(endpoints):
    vc = VisionClient({"provider": "openai_compatible", "model": "qwen3-vl:8b-instruct"})
    vc._backend = "openai"
    vc._oa_endpoints = [(u, _Cli(b)) for u, b in endpoints]
    return vc


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    monkeypatch.setattr(vc_mod, "_URL_BAD_UNTIL", {})


def test_all_endpoints_raise_sets_last_fail():
    err = RuntimeError("Error code: 400 exceeds the available context size (4096 tokens)")
    vc = _client_with([("http://a/v1", err), ("http://b/v1", err)])
    assert vc._openai_vision_request([{"type": "text", "text": "x"}]) is None
    assert vc.last_fail == "context_overflow"


def test_one_endpoint_answers_empty_is_not_failure():
    err = RuntimeError("timed out")
    vc = _client_with([("http://a/v1", err), ("http://b/v1", "")])
    assert vc._openai_vision_request([{"type": "text", "text": "x"}]) is None
    assert vc.last_fail == ""


def test_success_clears_last_fail():
    vc = _client_with([("http://a/v1", RuntimeError("timed out")), ("http://b/v1", "看到一只猫")])
    assert vc._openai_vision_request([{"type": "text", "text": "x"}]) == "看到一只猫"
    assert vc.last_fail == ""


# ── fallback 链 tag ─────────────────────────────────────────────────

def _mk_img() -> str:
    fd, path = tempfile.mkstemp(prefix="v213_", suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(b"not-really-a-jpeg")
    return path


def _run_chain(monkeypatch, *, last_fail: str, no_cloud_fallback: bool = False):
    def fake_init(self):
        self._backend = "openai"
        self._oa_endpoints = [("https://bd2026.cc/api/ai/v1", object())]
        return True

    async def fake_describe(self, image_path, prompt=None, *, allow_empty_failover=False):
        self.last_fail = last_fail
        return None

    monkeypatch.setattr(VisionClient, "initialize", fake_init)
    monkeypatch.setattr(VisionClient, "describe_image", fake_describe)
    merged = {"provider": "openai_compatible", "base_url": "https://bd2026.cc/api/ai/v1",
              "api_key": "cx.token.sig", "model": "qwen3-vl:8b-instruct"}
    if no_cloud_fallback:
        merged["no_cloud_fallback"] = True
    path = _mk_img()
    try:
        return asyncio.run(VisionClient._describe_fallback_chain(merged, merged, path, "p"))
    finally:
        os.remove(path)


def test_chain_tags_failed_and_does_not_call_zhipu_with_device_token(monkeypatch):
    calls = {"zhipu": 0}

    def boom(self):
        calls["zhipu"] += 1
        return False

    monkeypatch.setattr(VisionClient, "_initialize_zhipu", boom)
    txt, tag = _run_chain(monkeypatch, last_fail="context_overflow")
    assert txt is None
    assert tag == "ollama_failed:context_overflow"
    assert calls["zhipu"] == 0, "设备令牌不是智谱 key，不该再去敲智谱的门"
    assert vision_failure_reason(tag) == "busy"


def test_chain_tags_empty_when_model_answered_nothing(monkeypatch):
    txt, tag = _run_chain(monkeypatch, last_fail="")
    assert txt is None
    assert tag == "ollama_empty_no_zhipu_key"
    assert vision_failure_reason(tag) == "no_text"


def test_chain_no_cloud_fallback_keeps_failed_kind(monkeypatch):
    _, tag = _run_chain(monkeypatch, last_fail="timeout", no_cloud_fallback=True)
    assert tag == "ollama_failed:timeout|no_cloud_fallback"
    assert vision_failure_reason(tag) == "busy"


# ── 路由：补 message ────────────────────────────────────────────────

def test_route_attaches_human_message():
    from src.web.routes.unified_inbox_translate_routes import _attach_vision_failure_message

    req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"))
    out = _attach_vision_failure_message(
        req, {"ok": False, "reason": "no_text", "ocr_tag": "ollama_failed:context_overflow"})
    assert out["vision_reason"] == "busy"
    assert out["message"] == "识图服务忙，稍后重试"

    out = _attach_vision_failure_message(
        req, {"ok": False, "reason": "no_text", "ocr_tag": "ollama_unavailable"})
    assert out["message"] == "识图服务未配置"

    out = _attach_vision_failure_message(
        req, {"ok": False, "reason": "no_text", "ocr_tag": "ollama_empty_no_zhipu_key"})
    assert out["message"] == "图中未识别到文字"

    out = _attach_vision_failure_message(
        req, {"ok": False, "reason": "ocr_error", "ocr_tag": "error:ReadTimeout"})
    assert out["vision_reason"] == "busy"

    en = SimpleNamespace(state=SimpleNamespace(ui_lang="en"))
    out = _attach_vision_failure_message(
        en, {"ok": False, "reason": "no_text", "ocr_tag": "ollama_failed:timeout"})
    assert out["message"] == "Image recognition is busy, please retry shortly"


def test_route_leaves_success_and_existing_message_alone():
    from src.web.routes.unified_inbox_translate_routes import _attach_vision_failure_message

    req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"))
    ok = {"ok": True, "ocr_text": "hi"}
    assert _attach_vision_failure_message(req, ok) is ok and "message" not in ok
    had = {"ok": False, "reason": "no_text", "message": "自定义"}
    assert _attach_vision_failure_message(req, had)["message"] == "自定义"
    other = {"ok": False, "reason": "outside_base_dirs"}
    assert "message" not in _attach_vision_failure_message(req, other)
