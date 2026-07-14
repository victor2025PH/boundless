"""备用 Key 池管理路由 + 主动探活（P-KeyPool 2026-07-12 下午）门禁。

覆盖：
- GET /api/setup/cloud-credentials：余额/探活/池运行态汇总，密钥全程掩码不外泄；
- POST /api/setup/key-pool：overlay 落盘 + 掩码回传保留旧真值 + 校验 + 热生效调用；
- chat ping 纯函数：targets 继承/去重/占位跳过、run_chat_pings 节流/强制/状态快照。

不触网：探针/热重建全部 monkeypatch。
"""

import pytest
import yaml

from src.utils import cloud_credentials as cc


# ── chat ping 纯函数 ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_ping_state():
    cc._PING_STATE.clear()
    cc._PING_LAST_RUN["ts"] = 0.0
    cc._BALANCE_CACHE.update({"ts": 0.0, "sig": "", "result": None})
    yield
    cc._PING_STATE.clear()
    cc._PING_LAST_RUN["ts"] = 0.0
    cc._BALANCE_CACHE.update({"ts": 0.0, "sig": "", "result": None})


def _pool_cfg(keys, *, cc_enabled=True, ping_enabled=True):
    return {
        "ai": {"provider": "openai_compatible",
               "base_url": "https://api.deepseek.com/v1", "api_key": "sk-main",
               "model": "deepseek-chat",
               "key_pool": {"enabled": True, "keys": keys}},
        "ops": {"cloud_credentials": {"enabled": cc_enabled,
                                      "chat_ping": {"enabled": ping_enabled}}},
    }


class TestChatPingTargets:
    def test_inherits_base_and_model(self):
        out = cc.chat_ping_targets(_pool_cfg([{"name": "b1", "api_key": "sk-b"}]))
        assert len(out) == 1
        assert out[0]["base_url"] == "https://api.deepseek.com/v1"
        assert out[0]["model"] == "deepseek-chat"

    def test_skips_placeholder_and_dupes(self):
        out = cc.chat_ping_targets(_pool_cfg([
            {"name": "ph", "api_key": "YOUR_X"},
            {"name": "same-as-primary", "api_key": "sk-main"},
            {"name": "b", "api_key": "sk-b"},
            {"name": "b2", "api_key": "sk-b"},   # 同 key 同 base → 去重
        ]))
        assert [t["name"] for t in out] == ["b"]

    def test_cross_vendor_kept(self):
        out = cc.chat_ping_targets(_pool_cfg([
            {"name": "zhipu", "api_key": "sk-z",
             "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
        ]))
        assert out and out[0]["base_url"].startswith("https://open.bigmodel.cn")

    def test_pool_disabled_empty(self):
        cfg = _pool_cfg([{"name": "b", "api_key": "sk-b"}])
        cfg["ai"]["key_pool"]["enabled"] = False
        assert cc.chat_ping_targets(cfg) == []


class TestRunChatPings:
    def test_runs_and_snapshots(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cc, "probe_chat_key",
                            lambda base, key, model, **kw: calls.append(key) or
                            {"ok": True, "reachable": True, "http_status": 200,
                             "latency_ms": 5})
        out = cc.run_chat_pings(_pool_cfg([{"name": "b", "api_key": "sk-b"}]), now=1000.0)
        assert calls == ["sk-b"]
        assert out and out[0]["name"] == "b" and out[0]["ok"] is True
        snap = cc.ping_state_snapshot()
        assert snap["b"]["ok"] is True

    def test_throttled_within_interval(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cc, "probe_chat_key",
                            lambda *a, **kw: calls.append(1) or
                            {"ok": True, "reachable": True, "http_status": 200})
        cfg = _pool_cfg([{"name": "b", "api_key": "sk-b"}])
        cc.run_chat_pings(cfg, now=1000.0)
        out2 = cc.run_chat_pings(cfg, now=2000.0)   # 间隔 < 86400 → 吃缓存
        assert len(calls) == 1
        assert out2 and out2[0]["name"] == "b"      # 返回缓存态而非空

    def test_force_reruns(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cc, "probe_chat_key",
                            lambda *a, **kw: calls.append(1) or
                            {"ok": True, "reachable": True, "http_status": 200})
        cfg = _pool_cfg([{"name": "b", "api_key": "sk-b"}])
        cc.run_chat_pings(cfg, now=1000.0)
        cc.run_chat_pings(cfg, force=True, now=2000.0)
        assert len(calls) == 2

    def test_disabled_noop(self, monkeypatch):
        called = []
        monkeypatch.setattr(cc, "probe_chat_key",
                            lambda *a, **kw: called.append(1) or {})
        assert cc.run_chat_pings(
            _pool_cfg([{"name": "b", "api_key": "sk-b"}], cc_enabled=False)) == []
        assert cc.run_chat_pings(
            _pool_cfg([{"name": "b", "api_key": "sk-b"}], ping_enabled=False)) == []
        assert not called


# ── 路由 ─────────────────────────────────────────────────────────


def _build_app(config_manager):
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None,
                          config_manager=config_manager)
    return app


def _client_mgr(tmp_path, pool_keys=None):
    from fastapi.testclient import TestClient
    from src.utils.config_manager import ConfigManager
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  api_key: \"\"\n", encoding="utf-8")
    m = ConfigManager(str(cfg))
    m.config = {"ai": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-primary", "model": "deepseek-chat",
    }}
    if pool_keys is not None:
        m.config["ai"]["key_pool"] = {"enabled": True, "keys": pool_keys}
    return TestClient(_build_app(m)), m


def test_new_setup_routes_registered():
    app = _build_app(None)
    live = set()
    for r in app.routes:
        for meth in (getattr(r, "methods", None) or set()):
            if meth in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), meth))
    assert ("/api/setup/cloud-credentials", "GET") in live
    assert ("/api/setup/key-pool", "POST") in live


class TestCloudCredentialsGet:
    def test_masks_keys_and_reports_pool(self, tmp_path):
        client, m = _client_mgr(
            tmp_path, pool_keys=[{"name": "b1", "api_key": "sk-backup-0123456789"}])
        r = client.get("/api/setup/cloud-credentials").json()
        assert r["ok"] is True
        assert r["pool"]["enabled"] is True
        assert r["pool"]["keys"][0]["name"] == "b1"
        assert "sk-backup-0123456789" not in str(r), "完整 key 不得回显"
        assert r["pool"]["keys"][0]["api_key_masked"].startswith("sk-b")
        assert "…" in r["pool"]["keys"][0]["api_key_masked"]

    def test_balances_gated_by_ops_flag(self, tmp_path):
        client, m = _client_mgr(tmp_path)
        r = client.get("/api/setup/cloud-credentials").json()
        assert r["enabled"] is False and r["balances"] == []

    def test_probe_forces_ping(self, tmp_path, monkeypatch):
        seen = {"force": None}

        def _fake_run(cfg, *, force=False, now=None):
            seen["force"] = force
            return []
        monkeypatch.setattr(cc, "run_chat_pings", _fake_run)
        client, m = _client_mgr(tmp_path)
        m.config["ops"] = {"cloud_credentials": {"enabled": True}}
        client.get("/api/setup/cloud-credentials?probe=1")
        assert seen["force"] is True


class TestKeyPoolSave:
    def _post(self, client, keys):
        return client.post("/api/setup/key-pool", json={"keys": keys}).json()

    def test_save_writes_overlay_and_reloads(self, tmp_path, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod
        reloaded = []

        async def _fake_reload(app, cm):
            reloaded.append(1)
            return True
        monkeypatch.setattr(mod, "reload_ai_runtime", _fake_reload)
        client, m = _client_mgr(tmp_path)
        r = self._post(client, [
            {"name": "b1", "api_key": "sk-backup-1"},
            {"name": "glm", "api_key": "sk-z",
             "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
        ])
        assert r["ok"] is True and r["count"] == 2 and r["ai_ready"] is True
        assert reloaded == [1]
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        kp = overlay["ai"]["key_pool"]
        assert kp["enabled"] is True
        assert kp["keys"][0] == {"name": "b1", "api_key": "sk-backup-1"}
        assert kp["keys"][1]["base_url"].startswith("https://open.bigmodel.cn")
        # 进程内配置同步生效
        assert m.config["ai"]["key_pool"]["keys"][0]["api_key"] == "sk-backup-1"

    def test_masked_key_keeps_old_value(self, tmp_path, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod

        async def _fake_reload(app, cm):
            return True
        monkeypatch.setattr(mod, "reload_ai_runtime", _fake_reload)
        client, m = _client_mgr(
            tmp_path, pool_keys=[{"name": "b1", "api_key": "sk-real-old-abcdef"}])
        r = self._post(client, [{"name": "b1", "api_key": "sk-r…cdef"}])  # 掩码回传
        assert r["ok"] is True
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["key_pool"]["keys"][0]["api_key"] == "sk-real-old-abcdef"

    def test_validation_errors(self, tmp_path, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod

        async def _fake_reload(app, cm):
            return True
        monkeypatch.setattr(mod, "reload_ai_runtime", _fake_reload)
        client, m = _client_mgr(tmp_path)
        assert client.post("/api/setup/key-pool", json={}).json()["ok"] is False
        r = self._post(client, [{"name": "a", "api_key": "k1"},
                                {"name": "a", "api_key": "k2"}])
        assert r["ok"] is False   # 重名
        r = self._post(client, [{"name": "nokey", "api_key": ""}])
        assert r["ok"] is False   # 新条目空 key（无旧值可沿用）
        r = self._post(client, [{"name": f"k{i}", "api_key": f"sk-{i}"} for i in range(11)])
        assert r["ok"] is False   # 超上限

    def test_empty_list_clears_pool(self, tmp_path, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod

        async def _fake_reload(app, cm):
            return True
        monkeypatch.setattr(mod, "reload_ai_runtime", _fake_reload)
        client, m = _client_mgr(
            tmp_path, pool_keys=[{"name": "b1", "api_key": "sk-x"}])
        r = self._post(client, [])
        assert r["ok"] is True and r["count"] == 0
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["key_pool"]["keys"] == []


# ── /api/workspace/ai-runtime-status（坐席状态条数据源）──────────


class TestAiRuntimeStatus:
    def test_registered(self):
        app = _build_app(None)
        live = {(getattr(r, "path", ""), m) for r in app.routes
                for m in (getattr(r, "methods", None) or set()) if m == "GET"}
        assert ("/api/workspace/ai-runtime-status", "GET") in live

    def test_without_ai_client_reports_normal(self, tmp_path):
        client, m = _client_mgr(tmp_path)
        r = client.get("/api/workspace/ai-runtime-status").json()
        assert r == {"ok": True, "degraded": False, "mode": "primary"}

    def test_with_ai_client_snapshot(self, tmp_path):
        import types
        client, m = _client_mgr(tmp_path)
        # TestClient 的 app.state 可直接挂假 ai_client
        client.app.state.ai_client = types.SimpleNamespace(
            degradation_snapshot=lambda: {"degraded": True, "mode": "pool",
                                          "circuit_open": True})
        r = client.get("/api/workspace/ai-runtime-status").json()
        assert r["ok"] is True and r["degraded"] is True and r["mode"] == "pool"

    def test_snapshot_error_fails_open(self, tmp_path):
        import types
        client, m = _client_mgr(tmp_path)

        def _boom():
            raise RuntimeError("x")
        client.app.state.ai_client = types.SimpleNamespace(degradation_snapshot=_boom)
        r = client.get("/api/workspace/ai-runtime-status").json()
        assert r["degraded"] is False


class TestUsageDistribution:
    def test_cloud_credentials_aggregates_by_tier(self, tmp_path, monkeypatch):
        import src.ai.llm_cost as lc

        class _FakeCost:
            def dump(self):
                return {"rows": [
                    {"tier": "default", "calls": 90, "prompt_tokens": 900,
                     "completion_tokens": 100, "cost_usd": 0.9},
                    {"tier": "premium", "calls": 10, "prompt_tokens": 100,
                     "completion_tokens": 10, "cost_usd": 0.3},
                    {"tier": "key_pool", "calls": 5, "prompt_tokens": 50,
                     "completion_tokens": 5, "cost_usd": 0.05},
                    {"tier": "local_fallback", "calls": 3, "prompt_tokens": 30,
                     "completion_tokens": 3, "cost_usd": 0.0},
                ]}
        monkeypatch.setattr(lc, "get_llm_cost", lambda: _FakeCost())
        client, m = _client_mgr(tmp_path)
        r = client.get("/api/setup/cloud-credentials").json()
        u = r["usage"]
        assert u["primary"]["calls"] == 100      # default+premium 归并主链
        assert u["key_pool"]["calls"] == 5
        assert u["local_fallback"]["calls"] == 3
        assert abs(u["primary"]["cost_usd"] - 1.2) < 1e-9


def test_workspace_base_wires_degrade_bar():
    """坐席端降级状态条 wiring（静态契约）：条 + 轮询端点 + i18n 键可达。"""
    import pathlib
    repo = pathlib.Path(__file__).resolve().parent.parent
    src = (repo / "src" / "web" / "templates" / "workspace_base.html").read_text(encoding="utf-8")
    assert 'id="ws-aidegrade"' in src
    assert "/api/workspace/ai-runtime-status" in src
    assert "ws.aidegrade.mode_pool" in src and "ws.aidegrade.mode_local" in src


# ── AIClient.pool_status ─────────────────────────────────────────


def test_pool_status_snapshot():
    import time as _t
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"ai": {}}

        def get_ai_config(self):
            return {}

    c = AIClient(_Cfg())
    c._pool_entries = [
        {"name": "a", "client": object(), "model": "m1",
         "label": "m1 @ x (a)", "bad_until": 0.0},
        {"name": "b", "client": object(), "model": "m2",
         "label": "m2 @ y (b)", "bad_until": _t.time() + 60},
    ]
    st = c.pool_status()
    assert st[0]["cooling"] is False and st[0]["cooldown_remaining_sec"] == 0
    assert st[1]["cooling"] is True and st[1]["cooldown_remaining_sec"] > 0
    assert "api_key" not in str(st)
