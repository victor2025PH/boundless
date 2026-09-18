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


# ── 拉模型列表（2026-09-12 厂商预设配套：/v1/models → 候选 id）───────────────────
class _HxResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload

    def json(self):
        return self._p


class _HxClient:
    calls = []
    status = 200
    payload = {"data": [{"id": "gpt-b"}, {"id": "gpt-a"}, {"id": "gpt-a"}]}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _HxClient.calls.append((url, headers))
        return _HxResp(_HxClient.status, _HxClient.payload)


def test_list_models_uses_stored_or_primary_key_and_dedups(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _HxClient)
    client, m = _client_mgr(
        tmp_path, models={"gpt": {"base_url": "https://api.openai.com", "model": "gpt-a",
                                  "api_key": "sk-stored"}})
    _HxClient.calls.clear(); _HxClient.status = 200
    r = client.post("/api/setup/model-routes/list-models",
                    json={"base_url": "https://api.openai.com", "api_key": "sk-s…ored", "name": "gpt"}).json()
    assert r["ok"] and r["ids"] == ["gpt-a", "gpt-b"] and r["n"] == 2
    url, headers = _HxClient.calls[-1]
    assert url == "https://api.openai.com/v1/models"            # 自动补 /v1
    assert headers["Authorization"] == "Bearer sk-stored"       # 掩码回传 → 同名存档真值
    # 无存档、无显式 key → 主链 key
    client.post("/api/setup/model-routes/list-models", json={"base_url": "https://api.x.ai/v1"})
    assert _HxClient.calls[-1][1]["Authorization"] == "Bearer sk-primary"
    # 401 → ok=False 带 status；坏 URL → ok=False
    _HxClient.status = 401
    r = client.post("/api/setup/model-routes/list-models", json={"base_url": "https://api.x.ai/v1"}).json()
    assert r["ok"] is False and r["status"] == 401 and r["ids"] == []
    assert client.post("/api/setup/model-routes/list-models", json={"base_url": "nope"}).json()["ok"] is False
    assert "sk-stored" not in str(r) and "sk-primary" not in str(r)


def test_developer_page_presets_and_anchor():
    """模型与密钥页：多模型路由卡有 #dvmr 锚（composer「管理模型与密钥 →」跳转目标）、厂商预设
    只填端点不写死模型版本号、「拉取模型列表」接 list-models。开发者页只留跳转，旧锚 #dvmr 仍在。"""
    import pathlib
    import re
    card = pathlib.Path("src/web/templates/_model_routes_card.html").read_text(encoding="utf-8")
    page = pathlib.Path("src/web/templates/model_keys.html").read_text(encoding="utf-8")
    dev = pathlib.Path("src/web/templates/developer.html").read_text(encoding="utf-8")
    assert 'id="dvmr"' in card and "{% include \"_model_routes_card.html\" %}" in page
    assert "/api/setup/model-routes/list-models" in card
    assert 'onchange="mrApplyPreset(this.value)"' in card and "function mrApplyPreset(" in card
    i = card.index("const _DVMR_PRESETS")
    block = card[i:card.index("};", i)]
    for base in ("https://api.openai.com/v1", "https://generativelanguage.googleapis.com/v1beta/openai",
                 "https://api.x.ai/v1", "https://api.deepseek.com/v1"):
        assert base in block
    # 云厂商预设不写死会过期的模型版本号（DeepSeek 官方唯一现役 deepseek-flash 例外）
    assert not re.search(r"model:\s*'(gpt|gemini|grok|claude)-", block)
    assert 'id="dvmr"' in dev and 'href="/model-keys"' in dev
    assert "function mrApplyPreset(" not in dev
    assert "location.hash==='#dvmr'" in dev
    from src.web.i18n_packs import developer_page as dp
    for k in ("dv_mr_f_preset", "dv_mr_list_btn", "dv_mr_js_list_ok", "dv_mr_js_list_fail", "dv_mr_sub_conv",
              "nav_model_keys", "dv_mr_moved", "dv_mr_moved_btn", "mk_need_admin"):
        assert k in dp.ZH and k in dp.EN, k
