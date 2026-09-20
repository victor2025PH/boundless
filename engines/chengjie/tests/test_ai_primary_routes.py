"""主对话模式运营面（``ai.primary`` setup API + usage 分组，P1 闭环）。

守：
1. GET /api/setup/ai 带回 primary 快照（声明 / 生效 / 本地就绪）；
2. POST /api/setup/ai-primary 写 overlay + 热重建；非法值 / 缺本地端点拒写；
3. ``local_primary`` 出话分布独立成组（不并进 primary）；
4. ai-runtime-status 暴露 primary（坐席条可读，无密钥）。
"""
from __future__ import annotations

import json

import pytest

import src.web.routes.unified_inbox_setup_routes as setup_mod
from src.web.routes.unified_inbox_setup_routes import (
    _ai_primary_snapshot,
    _local_endpoint_ready,
    _usage_tier_group,
    register_setup_routes,
)


# ── 纯函数 ───────────────────────────────────────────────────────


def test_local_endpoint_ready_requires_triplet():
    assert _local_endpoint_ready({}) is False
    assert _local_endpoint_ready({"fallback": {"enabled": True}}) is False
    assert _local_endpoint_ready({
        "fallback": {"enabled": True, "base_url": "http://x:11434", "model": "q"},
    }) is True


def test_usage_tier_group_keeps_local_primary_separate():
    assert _usage_tier_group("local_primary") == "local_primary"
    assert _usage_tier_group("local_fallback") == "local_fallback"
    assert _usage_tier_group("key_pool") == "key_pool"
    assert _usage_tier_group("default") == "primary"
    assert _usage_tier_group("cloud") == "primary"


def test_snapshot_marks_divergent_when_runtime_differs():
    class _Mgr:
        config = {"ai": {"primary": "local_only",
                         "fallback": {"enabled": True, "base_url": "http://x",
                                      "model": "q"}}}

    class _Cli:
        _primary_mode = "cloud"   # 端点缺时 initialize 会退回

    snap = _ai_primary_snapshot(_Mgr(), _Cli())
    assert snap["configured"] == "local_only"
    assert snap["effective"] == "cloud"
    assert snap["divergent"] is True
    assert snap["local_ready"] is True


# ── 路由 ─────────────────────────────────────────────────────────


def _build_app(config_manager):
    from fastapi import FastAPI
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None,
                          config_manager=config_manager)
    return app


def _client_mgr(tmp_path, *, primary="cloud", with_fallback=False):
    from fastapi.testclient import TestClient
    from src.utils.config_manager import ConfigManager
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  api_key: \"\"\n", encoding="utf-8")
    m = ConfigManager(str(cfg))
    ai = {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-primary", "model": "deepseek-chat",
        "primary": primary,
    }
    if with_fallback:
        ai["fallback"] = {
            "enabled": True, "base_url": "http://192.168.0.176:11434",
            "model": "qwen3:30b",
        }
    m.config = {"ai": ai}
    return TestClient(_build_app(m)), m


def test_ai_primary_route_registered():
    app = _build_app(None)
    live = {(getattr(r, "path", ""), meth)
            for r in app.routes
            for meth in (getattr(r, "methods", None) or set())
            if meth not in {"HEAD", "OPTIONS"}}
    assert ("/api/setup/ai-primary", "POST") in live
    assert ("/api/setup/ai-primary/summary", "GET") in live


def test_get_setup_ai_includes_primary(tmp_path):
    client, _ = _client_mgr(tmp_path, primary="cloud", with_fallback=True)
    r = client.get("/api/setup/ai").json()
    assert r["ok"] is True
    assert r["primary"]["configured"] == "cloud"
    assert r["primary"]["local_ready"] is True
    assert r["primary"]["local_model"] == "qwen3:30b"


def test_get_ai_primary_summary_is_single_source(tmp_path):
    """2026-09-17：所有表达层共用这一份，零密钥、带降级链。"""
    client, m = _client_mgr(tmp_path, primary="local", with_fallback=True)
    m.config["ai"]["primary_lock"] = "local"
    m.config["ai"]["model"] = "deepseek-chat"
    r = client.get("/api/setup/ai-primary/summary").json()
    assert r["ok"] is True
    assert r["effective"] == "local" and r["lock"] == "local"
    assert r["order"][0] == "local"
    assert "本地 vLLM" in r["primary_text"]
    assert "api_key" not in json.dumps(r)
    assert "sk-" not in json.dumps(r)


def test_post_rejects_invalid_and_unready(tmp_path, monkeypatch):
    async def _fake_reload(app, cm):
        return True
    monkeypatch.setattr(setup_mod, "reload_ai_runtime", _fake_reload)

    client, m = _client_mgr(tmp_path, with_fallback=False)
    bad = client.post("/api/setup/ai-primary", json={"primary": "on-prem"}).json()
    assert bad["ok"] is False and bad.get("detail")

    no_fb = client.post("/api/setup/ai-primary", json={"primary": "local_only"}).json()
    assert no_fb["ok"] is False and no_fb.get("detail")


def test_post_writes_overlay_and_reloads(tmp_path, monkeypatch):
    reloaded = []

    async def _fake_reload(app, cm):
        reloaded.append(1)
        return True
    monkeypatch.setattr(setup_mod, "reload_ai_runtime", _fake_reload)

    client, m = _client_mgr(tmp_path, with_fallback=True)
    r = client.post("/api/setup/ai-primary", json={"primary": "local_only"}).json()
    assert r["ok"] is True
    assert r["primary"]["configured"] == "local_only"
    assert reloaded == [1]
    assert (m.config.get("ai") or {}).get("primary") == "local_only"


def test_cloud_credentials_usage_splits_local_primary(tmp_path, monkeypatch):
    class _Cost:
        def dump(self):
            return {"rows": [
                {"tier": "local_primary", "calls": 3, "prompt_tokens": 10,
                 "completion_tokens": 5, "cost_usd": 0.0,
                 "latency_ms_sum": 168000},
                {"tier": "default", "calls": 2, "prompt_tokens": 4,
                 "completion_tokens": 2, "cost_usd": 0.01,
                 "latency_ms_sum": 3000},
            ]}

    monkeypatch.setattr("src.ai.llm_cost.get_llm_cost", lambda: _Cost())
    client, _ = _client_mgr(tmp_path, with_fallback=True)
    r = client.get("/api/setup/cloud-credentials").json()
    assert r["usage"]["local_primary"]["calls"] == 3
    assert r["usage"]["primary"]["calls"] == 2
    # P3：每组平均延迟（56s 实证的可见化——分层换模型的读数依据）
    assert r["usage"]["local_primary"]["latency_avg_ms"] == 56000
    assert r["usage"]["primary"]["latency_avg_ms"] == 1500
    assert "latency_ms_sum" not in r["usage"]["local_primary"]
    assert "primary" in r


def test_ai_runtime_status_exposes_primary(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None, config_manager=None)

    class _Cli:
        _primary_mode = "local_only"

        def degradation_snapshot(self):
            return {"degraded": False, "mode": "primary", "primary": "local_only"}

    app.state.ai_client = _Cli()
    r = TestClient(app).get("/api/workspace/ai-runtime-status").json()
    assert r["ok"] is True
    assert r["primary"] == "local_only"
    assert r["degraded"] is False


def test_get_stats_includes_primary_mode():
    from src.ai.ai_client import AIClient
    from tests.test_ai_client_chat_fallback import _Cfg

    c = AIClient(_Cfg())
    c._primary_mode = "local"
    assert c.get_stats()["primary_mode"] == "local"
    snap = c.degradation_snapshot()
    assert snap["primary"] == "local"
    assert snap["degraded"] is False


# ── 启动探针按模式分流（纯本地部署 initialize 必须能成功）─────────────────


def _probe_cfg(primary: str):
    from tests.test_ai_client_chat_fallback import _Cfg

    class _C(_Cfg):
        def get_ai_config(self):
            return {
                "provider": "openai_compatible",
                "api_key": "YOUR_AI_API_KEY",     # 纯本地部署：云 key 占位
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "primary": primary,
                "fallback": {"enabled": True, "model": "qwen3:30b",
                             "base_url": "http://192.168.0.176:11434"},
            }
    return _C()


def _patch_probes(monkeypatch, *, local_ok: bool, cloud_ok: bool):
    from src.ai.ai_client import AIClient
    calls: list = []

    class _FakeOpenAI:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr("src.ai.ai_client.AsyncOpenAI", _FakeOpenAI)

    async def _local(self):
        calls.append("local")
        return local_ok

    async def _cloud(self):
        calls.append("cloud")
        return cloud_ok

    monkeypatch.setattr(AIClient, "_test_local_connection", _local)
    monkeypatch.setattr(AIClient, "_test_openai_connection", _cloud)
    return calls


async def test_local_only_initializes_without_cloud(monkeypatch):
    """纯本地部署（云 key 占位）+ 本地探针通 → initialize 成功且不探云端。

    旧逻辑只探云端 → False → reload_ai_runtime 拒换绑，模式切换永远「未生效」。
    """
    from src.ai.ai_client import AIClient
    calls = _patch_probes(monkeypatch, local_ok=True, cloud_ok=False)
    c = AIClient(_probe_cfg("local_only"))
    assert await c.initialize() is True
    assert calls == ["local"], "local_only 不该探云端"
    assert c._primary_mode == "local_only"


async def test_local_only_init_fails_when_local_down(monkeypatch):
    from src.ai.ai_client import AIClient
    calls = _patch_probes(monkeypatch, local_ok=False, cloud_ok=True)
    c = AIClient(_probe_cfg("local_only"))
    assert await c.initialize() is False, "local_only 本地挂了无链可用，如实失败"
    assert "cloud" not in calls


async def test_local_mode_falls_back_to_cloud_probe(monkeypatch):
    from src.ai.ai_client import AIClient
    calls = _patch_probes(monkeypatch, local_ok=False, cloud_ok=True)
    c = AIClient(_probe_cfg("local"))
    assert await c.initialize() is True, "local 模式云端可回落，探针同语义"
    assert calls == ["local", "cloud"]


async def test_cloud_mode_probe_unchanged(monkeypatch):
    from src.ai.ai_client import AIClient
    calls = _patch_probes(monkeypatch, local_ok=True, cloud_ok=False)
    c = AIClient(_probe_cfg("cloud"))
    assert await c.initialize() is False, "cloud 模式旧语义不变：云探针失败即失败"
    assert calls == ["cloud"]
