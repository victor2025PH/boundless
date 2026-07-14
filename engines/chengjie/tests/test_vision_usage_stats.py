"""阶段 2A：入站识图统一观测门禁。

覆盖 VisionClient 单一瓶颈处的 provider_stats(namespace=vision) 接线：
- _backend_from_tag 纯函数语义（tag → 后端名）；
- 成功/失败/缓存命中/云兜底 各路径计数；
- 端到端：/api/workspace/metrics.providers.vision（JSON）+ vision_attempts_total（Prometheus）。

不依赖真实 VLM：monkeypatch _describe_fallback_chain。
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.media_text_cache import get_vision_desc_cache
from src.ai.provider_stats import get_provider_stats
from src.vision_client import VisionClient, _backend_from_tag


# ── 纯函数：tag → 后端名 ─────────────────────────────────────────────

def test_backend_from_tag_semantics():
    assert _backend_from_tag("ollama_ok") == "ollama"
    assert _backend_from_tag("ollama_empty_no_zhipu_key") == "ollama"
    assert _backend_from_tag("ollama_unavailable") == "ollama"
    assert _backend_from_tag("vision_ok") == "ollama"          # 遗留 tag
    assert _backend_from_tag("zhipu_only") == "zhipu"
    assert _backend_from_tag("ollama_empty|zhipu_fallback") == "zhipu"
    assert _backend_from_tag("ollama_unavailable|zhipu_empty") == "zhipu"
    assert _backend_from_tag("vision_client_init_fail") == "none"
    assert _backend_from_tag("") == "none"


# ── 记录路径 ─────────────────────────────────────────────────────────

def _mk_img(data: bytes = b"vision-stats-img-bytes") -> str:
    fd, path = tempfile.mkstemp(prefix="vs_test_", suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _patch_chain(monkeypatch, result, counter: dict) -> None:
    async def fake_chain(cls, merged, gv, image_path, prompt=None):
        counter["n"] += 1
        return result

    monkeypatch.setattr(VisionClient, "_describe_fallback_chain", classmethod(fake_chain))


@pytest.fixture(autouse=True)
def _reset():
    get_vision_desc_cache().reset()
    get_provider_stats("vision").reset()
    yield
    get_vision_desc_cache().reset()
    get_provider_stats("vision").reset()


@pytest.mark.asyncio
async def test_success_and_cache_hit_recorded(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, ("一张海边照片", "ollama_ok"), counter)
    path = _mk_img()
    try:
        merged = {"model": "qwen2.5vl"}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        d = get_provider_stats("vision").dump()
        assert d["total_attempts"] == 1          # 第二次是缓存命中，不算 provider 调用
        assert d["cache_hits"] == 1
        assert d["cache_hit_rate"] == 0.5
        row = {r["provider"]: r for r in d["rows"]}["ollama"]
        assert row["calls"] == 1 and row["ok"] == 1
        assert d["labels"].get("ollama_ok") == 1
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_zhipu_fallback_counted(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, ("desc", "ollama_empty|zhipu_fallback"), counter)
    path = _mk_img(b"zf-bytes")
    try:
        await VisionClient.describe_image_with_ollama_zhipu_fallback({"model": "m"}, {}, path, "p")
        d = get_provider_stats("vision").dump()
        assert d["fallbacks"] == 1
        row = {r["provider"]: r for r in d["rows"]}["zhipu"]
        assert row["calls"] == 1 and row["ok"] == 1
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_failure_recorded_as_fail(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, (None, "ollama_empty_no_zhipu_key"), counter)
    path = _mk_img(b"fail-bytes")
    try:
        await VisionClient.describe_image_with_ollama_zhipu_fallback({"model": "m"}, {}, path, "p")
        d = get_provider_stats("vision").dump()
        row = {r["provider"]: r for r in d["rows"]}["ollama"]
        assert row["fail"] == 1 and row["ok"] == 0
        assert d["labels"].get("ollama_empty_no_zhipu_key") == 1
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_stats_never_blocks_recognition(monkeypatch):
    """观测层异常绝不影响识图返回。"""
    counter = {"n": 0}
    _patch_chain(monkeypatch, ("desc", "ollama_ok"), counter)

    def boom(*a, **k):
        raise RuntimeError("stats down")

    import src.ai.provider_stats as ps
    monkeypatch.setattr(ps, "get_provider_stats", boom)
    path = _mk_img(b"noblock-bytes")
    try:
        txt, tag = await VisionClient.describe_image_with_ollama_zhipu_fallback(
            {"model": "m"}, {}, path, "p")
        assert txt == "desc" and tag == "ollama_ok"
    finally:
        os.remove(path)


# ── 端到端：metrics 路由 ─────────────────────────────────────────────

def _make_app():
    from src.web.routes.drafts_routes import register_metrics_route

    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=api_auth)
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.asyncio
async def test_metrics_route_exposes_vision(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, ("desc", "ollama_ok"), counter)
    path = _mk_img(b"route-bytes")
    try:
        merged = {"model": "m"}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
    finally:
        os.remove(path)

    c = _make_app()
    m = c.get("/api/workspace/metrics").json()
    vz = (m.get("providers") or {}).get("vision")
    assert vz is not None
    assert vz["total_attempts"] == 1 and vz["cache_hits"] == 1

    r = c.get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert 'vision_attempts_total{provider="ollama"} 1' in r.text
    assert "vision_cache_hits_total 1" in r.text
