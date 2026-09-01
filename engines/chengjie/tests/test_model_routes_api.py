"""多模型路由管理路由（/api/setup/model-routes）门禁。

覆盖：GET 掩码回显 + 任务映射 + 已认任务名；POST overlay 落盘 + 掩码回传保留旧真值 +
校验（base 无效/model 空/重名/路由指向空档）+ 清空。不触网：reload_ai_runtime monkeypatch。
"""
import yaml


def _build_app(config_manager):
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None,
                          config_manager=config_manager)
    return app


def _client_mgr(tmp_path, models=None, task_routes=None):
    from fastapi.testclient import TestClient
    from src.utils.config_manager import ConfigManager
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  api_key: \"\"\n", encoding="utf-8")
    m = ConfigManager(str(cfg))
    m.config = {"ai": {"provider": "openai_compatible",
                       "base_url": "https://api.deepseek.com/v1",
                       "api_key": "sk-primary", "model": "deepseek-v4-flash"}}
    if models is not None:
        m.config["ai"]["models"] = models
    if task_routes is not None:
        m.config["ai"]["task_routes"] = task_routes
    return TestClient(_build_app(m)), m


def test_model_routes_registered():
    app = _build_app(None)
    live = set()
    for r in app.routes:
        for meth in (getattr(r, "methods", None) or set()):
            if meth in {"HEAD", "OPTIONS"}:
                continue
            live.add((getattr(r, "path", ""), meth))
    assert ("/api/setup/model-routes", "GET") in live
    assert ("/api/setup/model-routes", "POST") in live


class TestGet:
    def test_masks_and_lists(self, tmp_path):
        client, m = _client_mgr(
            tmp_path,
            models={"planner_local": {"base_url": "http://192.168.0.173:8001",
                                      "model": "chatx",
                                      "api_key": "sk-secret-0123456789"}},
            task_routes={"assistant_planner": "planner_local"})
        r = client.get("/api/setup/model-routes").json()
        assert r["ok"] is True
        assert r["models"][0]["name"] == "planner_local"
        assert r["models"][0]["model"] == "chatx"
        assert "sk-secret-0123456789" not in str(r), "完整 key 不得回显"
        assert r["task_routes"] == {"assistant_planner": "planner_local"}
        assert "assistant_planner" in r["known_tasks"]

    def test_empty_default(self, tmp_path):
        client, m = _client_mgr(tmp_path)
        r = client.get("/api/setup/model-routes").json()
        assert r["ok"] is True and r["models"] == [] and r["task_routes"] == {}


class TestSave:
    def _post(self, client, models, task_routes=None):
        body = {"models": models}
        if task_routes is not None:
            body["task_routes"] = task_routes
        return client.post("/api/setup/model-routes", json=body).json()

    def _patch_reload(self, monkeypatch):
        import src.web.routes.unified_inbox_setup_routes as mod

        async def _fake(app, cm):
            return True
        monkeypatch.setattr(mod, "reload_ai_runtime", _fake)

    def test_save_writes_overlay(self, tmp_path, monkeypatch):
        self._patch_reload(monkeypatch)
        client, m = _client_mgr(tmp_path)
        r = self._post(
            client,
            [{"name": "planner_local", "base_url": "http://192.168.0.173:8001", "model": "chatx"},
             {"name": "cu_vlm", "base_url": "http://192.168.0.176:11434", "model": "qwen3-vl:8b-instruct"}],
            {"assistant_planner": "planner_local", "computer_use": "cu_vlm"})
        assert r["ok"] is True and r["models"] == 2 and r["routes"] == 2 and r["ai_ready"] is True
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["models"]["planner_local"]["model"] == "chatx"
        assert overlay["ai"]["task_routes"]["assistant_planner"] == "planner_local"
        assert m.config["ai"]["models"]["cu_vlm"]["base_url"].startswith("http://192.168.0.176")

    def test_masked_key_kept(self, tmp_path, monkeypatch):
        self._patch_reload(monkeypatch)
        client, m = _client_mgr(
            tmp_path,
            models={"a": {"base_url": "http://h:1", "model": "x", "api_key": "sk-real-old-abcdef"}})
        r = self._post(client, [{"name": "a", "base_url": "http://h:1", "model": "x",
                                 "api_key": "sk-r…cdef"}])  # 掩码回传
        assert r["ok"] is True
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["models"]["a"]["api_key"] == "sk-real-old-abcdef"

    def test_validation(self, tmp_path, monkeypatch):
        self._patch_reload(monkeypatch)
        client, m = _client_mgr(tmp_path)
        assert client.post("/api/setup/model-routes", json={}).json()["ok"] is False
        assert self._post(client, [{"name": "a", "base_url": "nope", "model": "x"}])["ok"] is False
        assert self._post(client, [{"name": "a", "base_url": "http://h:1", "model": ""}])["ok"] is False
        assert self._post(client, [{"name": "a", "base_url": "http://h:1", "model": "x"},
                                   {"name": "a", "base_url": "http://h:2", "model": "y"}])["ok"] is False
        assert self._post(client, [{"name": "a", "base_url": "http://h:1", "model": "x"}],
                          {"assistant_planner": "ghost"})["ok"] is False

    def test_empty_clears(self, tmp_path, monkeypatch):
        self._patch_reload(monkeypatch)
        client, m = _client_mgr(tmp_path,
                                models={"a": {"base_url": "http://h:1", "model": "x"}})
        r = self._post(client, [], {})
        assert r["ok"] is True and r["models"] == 0
        overlay = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["ai"]["models"] == {}


# ── 端点健康探活（picker 🟢/🔴）──────────────────────────────────
class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestProbeModelEndpoint:
    def test_reachable_200(self, monkeypatch):
        from src.web.routes import unified_inbox_setup_routes as mod
        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _Resp(200))
        h = mod._probe_model_endpoint("http://192.168.0.173:8001", "k")
        assert h["reachable"] is True and h["status"] == 200

    def test_http_error_still_reachable(self, monkeypatch):
        import urllib.error
        from src.web.routes import unified_inbox_setup_routes as mod

        def _raise(req, timeout=0):
            raise urllib.error.HTTPError("http://h/v1/models", 401, "unauth", {}, None)
        monkeypatch.setattr("urllib.request.urlopen", _raise)
        h = mod._probe_model_endpoint("http://h:1", "k")
        assert h["reachable"] is True and h["status"] == 401  # 主机应答=在线

    def test_conn_error_unreachable(self, monkeypatch):
        import urllib.error
        from src.web.routes import unified_inbox_setup_routes as mod

        def _raise(req, timeout=0):
            raise urllib.error.URLError("refused")
        monkeypatch.setattr("urllib.request.urlopen", _raise)
        h = mod._probe_model_endpoint("http://h:1", "k")
        assert h["reachable"] is False

    def test_bad_url(self):
        from src.web.routes import unified_inbox_setup_routes as mod
        h = mod._probe_model_endpoint("notaurl")
        assert h["reachable"] is False and h["error"] == "bad_url"


def test_get_probe_attaches_health(tmp_path, monkeypatch):
    import src.web.routes.unified_inbox_setup_routes as mod
    monkeypatch.setattr(mod, "_probe_model_endpoint",
                        lambda base, key="", timeout=3.0: {"reachable": True, "status": 200,
                                                           "latency_ms": 7})
    client, m = _client_mgr(
        tmp_path,
        models={"planner_local": {"base_url": "http://192.168.0.173:8001", "model": "chatx"}})
    r = client.get("/api/setup/model-routes?probe=1").json()
    assert r["ok"] and r["models"][0]["health"]["reachable"] is True
    # 不带 probe → 无 health（快路径，不阻塞加载）
    r2 = client.get("/api/setup/model-routes").json()
    assert "health" not in r2["models"][0]
