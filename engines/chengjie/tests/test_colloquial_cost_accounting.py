"""口语化改写 llm_cost 记账门禁（2026-08-13 内测读数盲区补全）。

`_rewrite_via_endpoint` httpx 直连不经 ai_client → 出话分布看板此前看不见
这条链（「vLLM 收到 N 次请求 vs 看板只记 1 次」缺口的主要成分）。本门禁钉住：
- OpenAI 兼容口（/v1）成功改写 → llm_cost 记 tier="colloquial" + usage token
- Ollama 原生口成功改写 → 同上（token 取顶层 *_eval_count）
- 空回复不记账（miss 不该污染出话分布）
- 记账层异常绝不影响改写主流程（best-effort 契约）
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai import voice_colloquial_llm as vcl  # noqa: E402


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeAsyncClient:
    """httpx.AsyncClient 替身：记录请求并返回预置 JSON。"""

    last_url = ""
    next_data: dict = {}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        _FakeAsyncClient.last_url = url
        return _FakeResp(_FakeAsyncClient.next_data)


class _CostRecorder:
    def __init__(self):
        self.rows = []

    def record(self, **kw):
        self.rows.append(kw)


def _patch_httpx(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


def _patch_cost(monkeypatch):
    rec = _CostRecorder()
    fake_mod = types.SimpleNamespace(get_llm_cost=lambda: rec)
    monkeypatch.setitem(sys.modules, "src.ai.llm_cost", fake_mod)
    return rec


def _ep(base_url):
    return {
        "base_url": base_url, "model": "chatx", "api_key": "vllm",
        "timeout_sec": 5.0, "num_ctx": 0, "keep_alive": "",
    }


def test_openai_branch_records_colloquial_tier(monkeypatch):
    _patch_httpx(monkeypatch)
    rec = _patch_cost(monkeypatch)
    _FakeAsyncClient.next_data = {
        "choices": [{"message": {"content": "改好的口语稿"}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }
    out = asyncio.run(vcl._rewrite_via_endpoint(
        _ep("http://192.168.0.173:8001/v1"), "sys", "user", temperature=0.3))
    assert out == "改好的口语稿"
    assert _FakeAsyncClient.last_url.endswith("/v1/chat/completions")
    assert len(rec.rows) == 1
    row = rec.rows[0]
    assert row["tier"] == "colloquial"
    assert row["model"] == "chatx"
    assert row["prompt_tokens"] == 120 and row["completion_tokens"] == 30
    assert row["latency_ms"] >= 0


def test_ollama_branch_records_native_counts(monkeypatch):
    _patch_httpx(monkeypatch)
    rec = _patch_cost(monkeypatch)
    _FakeAsyncClient.next_data = {
        "message": {"content": "口语稿"},
        "prompt_eval_count": 88, "eval_count": 21,
    }
    out = asyncio.run(vcl._rewrite_via_endpoint(
        _ep("http://192.168.0.198:11434"), "sys", "user", temperature=0.3))
    assert out == "口语稿"
    assert _FakeAsyncClient.last_url.endswith("/api/chat")
    assert len(rec.rows) == 1
    assert rec.rows[0]["prompt_tokens"] == 88
    assert rec.rows[0]["completion_tokens"] == 21


def test_empty_reply_not_recorded(monkeypatch):
    _patch_httpx(monkeypatch)
    rec = _patch_cost(monkeypatch)
    _FakeAsyncClient.next_data = {
        "choices": [{"message": {"content": "  "}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 0},
    }
    out = asyncio.run(vcl._rewrite_via_endpoint(
        _ep("http://192.168.0.173:8001/v1"), "sys", "user", temperature=0.3))
    assert out is None
    assert rec.rows == []


def test_cost_layer_failure_never_breaks_rewrite(monkeypatch):
    _patch_httpx(monkeypatch)

    class _Boom:
        def record(self, **kw):
            raise RuntimeError("cost store down")

    fake_mod = types.SimpleNamespace(get_llm_cost=lambda: _Boom())
    monkeypatch.setitem(sys.modules, "src.ai.llm_cost", fake_mod)
    _FakeAsyncClient.next_data = {
        "choices": [{"message": {"content": "稿子"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5},
    }
    out = asyncio.run(vcl._rewrite_via_endpoint(
        _ep("http://192.168.0.173:8001/v1"), "sys", "user", temperature=0.3))
    assert out == "稿子"
