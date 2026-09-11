# -*- coding: utf-8 -*-
"""prompt-inspect 留痕 / 缓存命中统计 / over 软放行告警 门禁（2026-09-11）。"""
from __future__ import annotations

import types

import pytest

from src.ai import prompt_trace


@pytest.fixture(autouse=True)
def _reset():
    prompt_trace.reset()
    yield
    prompt_trace.reset()


def _usage(pt=1000, hit=800, ct=50, reasoning=0, style="deepseek"):
    if style == "deepseek":
        return types.SimpleNamespace(
            prompt_tokens=pt, completion_tokens=ct,
            prompt_cache_hit_tokens=hit, prompt_cache_miss_tokens=pt - hit,
            completion_tokens_details=types.SimpleNamespace(reasoning_tokens=reasoning),
            model_extra={})
    if style == "openai":
        return types.SimpleNamespace(
            prompt_tokens=pt, completion_tokens=ct,
            prompt_tokens_details=types.SimpleNamespace(cached_tokens=hit),
            completion_tokens_details=None, model_extra={})
    if style == "extra":  # SDK 未建模字段落在 model_extra
        return types.SimpleNamespace(
            prompt_tokens=pt, completion_tokens=ct, completion_tokens_details=None,
            model_extra={"prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": pt - hit})
    return {"prompt_tokens": pt, "completion_tokens": ct, "prompt_cache_hit_tokens": hit}


@pytest.mark.parametrize("style", ["deepseek", "openai", "extra", "dict"])
def test_usage_fields_reads_cache_hit_from_all_shapes(style):
    uf = prompt_trace.usage_fields(_usage(style=style))
    assert uf["prompt_tokens"] == 1000 and uf["cache_hit_tokens"] == 800
    assert uf["cache_miss_tokens"] == 200 and uf["completion_tokens"] == 50


def test_usage_fields_never_raises():
    assert prompt_trace.usage_fields(None)["prompt_tokens"] == 0
    assert prompt_trace.usage_fields(object())["cache_hit_tokens"] == 0


def test_record_list_get_and_cache_ratio():
    msgs = [{"role": "system", "content": "【后台人设定位 · 须遵守】\n苏婉\n\n【用户长期记忆要点】\n- 叫阿明"},
            {"role": "user", "content": "在吗"}, {"role": "assistant", "content": "在"},
            {"role": "user", "content": "你记得我叫啥"}]
    seq = prompt_trace.record(messages=msgs, model="deepseek-flash", host="api.deepseek.com",
                              conv="telegram:1:990001099", request_id="r1",
                              usage=_usage(), budget_stats={"before": 900, "after": 900, "over": 0},
                              latency_ms=812)
    assert seq == 1
    items = prompt_trace.list_entries(conv="990001099")
    assert len(items) == 1
    it = items[0]
    assert "system" not in it and "messages" not in it            # 摘要不带正文
    assert it["system_sections"] == ["【后台人设定位 · 须遵守】", "【用户长期记忆要点】"]
    assert it["history_msgs"] == 2 and it["usage"]["cache_hit_tokens"] == 800
    full = prompt_trace.get_entry(1)
    assert "苏婉" in full["system"] and len(full["messages"]) == 3
    cs = prompt_trace.cache_stats()
    assert cs["calls"] == 1 and cs["hit_ratio"] == 0.8
    assert prompt_trace.get_entry(99) is None
    assert prompt_trace.list_entries(conv="nope") == []


def test_ring_buffer_bounded_and_long_system_clipped():
    big = "人" * (prompt_trace.MAX_TEXT_CHARS + 500)
    for _ in range(prompt_trace.MAX_ENTRIES + 5):
        prompt_trace.record(messages=[{"role": "system", "content": big}], model="m")
    assert len(prompt_trace.list_entries(limit=1000)) == prompt_trace.MAX_ENTRIES
    e = prompt_trace.get_entry(prompt_trace.MAX_ENTRIES + 5)
    assert e["system_chars"] == len(big) and "已截断" in e["system"]


# ── AIClient 挂点 ────────────────────────────────────────────────────────

class _CM:
    def __init__(self, ai=None):
        self.config = {"ai": dict(ai or {})}


def _client(ai=None):
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)
    c.config = _CM(ai)
    c._prompt_budget_tokens = 12000
    return c


def test_apply_budget_stashes_stats_and_alerts_on_over(monkeypatch):
    c = _client()
    calls = []
    import src.ops.ops_alert as oa
    monkeypatch.setattr(oa, "notify", lambda kind, text, **kw: calls.append((kind, text, kw)) or True)
    c._trim_prompt_to_budget = lambda msgs, budget: (msgs, {
        "before": 15000, "after": 14000, "hist": 0, "fewshot": 0, "inject_chars": 0, "over": 2000})
    c._apply_prompt_budget([{"role": "user", "content": "hi"}], {"account_id": "acc1", "chat_id": "c1"})
    assert c._last_budget_stats["over"] == 2000 and c._last_budget_stats["budget"] == 12000
    assert len(calls) == 1
    kind, text, kw = calls[0]
    assert kind == "prompt_over_budget" and "软放行" in text and "2000" in text
    assert kw["account_id"] == "acc1" and kw["debounce_sec"] == 1800

    # over=0 不告警
    c._trim_prompt_to_budget = lambda msgs, budget: (msgs, {
        "before": 100, "after": 100, "hist": 0, "fewshot": 0, "inject_chars": 0, "over": 0})
    c._apply_prompt_budget([{"role": "user", "content": "hi"}], {})
    assert len(calls) == 1


def test_trace_prompt_records_with_host_and_budget():
    c = _client()
    c._last_budget_stats = {"before": 900, "after": 900, "over": 0, "budget": 12000}
    client = types.SimpleNamespace(base_url="https://api.deepseek.com/v1/")
    c._trace_prompt([{"role": "system", "content": "【后台人设定位】x"}, {"role": "user", "content": "hi"}],
                    model="deepseek-flash", client=client, context={"chat_id": "c9", "request_id": "rq"},
                    usage=_usage(pt=2000, hit=1500), latency_ms=700)
    it = prompt_trace.list_entries()[0]
    assert it["host"] == "api.deepseek.com" and it["model"] == "deepseek-flash"
    assert it["budget"]["budget"] == 12000 and it["usage"]["cache_hit_tokens"] == 1500
    assert it["request_id"] == "rq" and it["latency_ms"] == 700


def test_main_chain_calls_trace_hook():
    """主链源码里 usage 抽取后紧跟留痕调用（防重构丢失）。"""
    import inspect
    from src.ai.ai_client import AIClient
    src = inspect.getsource(AIClient)
    i_usage = src.find('_rtoks = getattr(')
    i_trace = src.find('self._trace_prompt(', i_usage)
    assert i_usage > 0 and 0 < i_trace - i_usage < 800


# ── 路由 ────────────────────────────────────────────────────────────────

def test_inspect_routes_json():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.web.routes.ai_inspect_routes import register_ai_inspect_routes

    app = FastAPI()
    register_ai_inspect_routes(app, api_auth=lambda: None,
                               config_manager=types.SimpleNamespace(config={
                                   "ai": {"context_depth": "deep", "usage_mode": "economy"}}))
    prompt_trace.record(messages=[{"role": "system", "content": "【后台人设定位】p"},
                                  {"role": "user", "content": "q"}],
                        model="deepseek-flash", conv="tg:1:77", usage=_usage())
    cl = TestClient(app)
    r = cl.get("/api/ai/prompt-inspect?conv=77").json()
    assert r["ok"] and r["count"] == 1 and r["depth"]["current"] == "deep"
    assert r["depth"]["economy"]["on"] is True and r["depth"]["economy"]["history_cap"] == 4
    assert r["cache"]["hit_ratio"] == 0.8 and "system" not in r["items"][0]
    one = cl.get("/api/ai/prompt-inspect/1").json()
    assert one["ok"] and one["item"]["system"].startswith("【后台人设定位】")
    assert cl.get("/api/ai/prompt-inspect/999").json()["ok"] is False
