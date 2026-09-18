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
        assert r["models"][0]["has_key"] is True
        assert r["task_routes"] == {"assistant_planner": "planner_local"}
        assert "assistant_planner" in r["known_tasks"]
        # 2026-09-18：用途注册表只列有消费方的任务；computer_use / chat 下线
        assert [t["key"] for t in r["tasks"]] == ["assistant_planner", "assistant_qa", "memory_extract"]
        assert all(t["label"] and t["desc"] for t in r["tasks"])
        assert r["main_chain"]["model"] == "deepseek-v4-flash"
        assert r["main_chain"]["public_host"] == "api.deepseek.com"
        assert r["unrestricted_profile"] == "unrestricted"
        # 私网档：展示事实与 composer 目录同源（厂商 local / 私有 / 不亮 RFC1918）
        row = r["models"][0]
        assert row["vendor"] == "local" and row["private"] is True
        assert "192.168" not in row["public_host"]

    def test_custom_task_in_yaml_survives_as_custom_row(self, tmp_path):
        """YAML 里配着注册表之外的任务名（旧 computer_use）→ 以 custom=True 带出，UI 不会静默丢掉。"""
        client, m = _client_mgr(
            tmp_path, models={"vlm": {"base_url": "http://h:1", "model": "x"}},
            task_routes={"computer_use": "vlm"})
        r = client.get("/api/setup/model-routes").json()
        custom = [t for t in r["tasks"] if t["custom"]]
        assert [t["key"] for t in custom] == ["computer_use"]

    def test_extras_reported_only_when_configured(self, tmp_path):
        client, m = _client_mgr(
            tmp_path, models={"gpt": {"base_url": "https://api.openai.com/v1", "model": "gpt-a",
                                      "label": "GPT", "max_ctx": 200000, "cost_hint": "¥2/M"},
                              "bare": {"base_url": "https://api.x.ai/v1", "model": "grok-a"}})
        r = client.get("/api/setup/model-routes").json()
        by = {x["name"]: x for x in r["models"]}
        assert by["gpt"]["label"] == "GPT" and by["gpt"]["max_ctx"] == 200000 and by["gpt"]["cost_hint"] == "¥2/M"
        assert by["gpt"]["vendor"] == "openai" and by["gpt"]["display_label"] == "GPT"
        # 未配的不回填默认值（免得页面一保存把默认值固化进 YAML）
        assert by["bare"]["label"] == "" and by["bare"]["max_ctx"] is None and by["bare"]["cost_hint"] == ""
        assert by["bare"]["display_label"] == "Grok" and by["bare"]["max_ctx_default"] == 128000

    def test_local_endpoint_from_private_fallback(self, tmp_path):
        client, m = _client_mgr(tmp_path)
        assert client.get("/api/setup/model-routes").json()["local_endpoint"] is None
        m.config["ai"]["fallback"] = {"base_url": "http://192.168.0.173:8001/v1", "model": "chatx"}
        le = client.get("/api/setup/model-routes").json()["local_endpoint"]
        assert le == {"base_url": "http://192.168.0.173:8001/v1", "model": "chatx"}
        m.config["ai"]["fallback"] = {"base_url": "https://api.deepseek.com/v1", "model": "x"}
        assert client.get("/api/setup/model-routes").json()["local_endpoint"] is None  # 公网不算「自有算力」

    def test_empty_default(self, tmp_path):
        client, m = _client_mgr(tmp_path)
        r = client.get("/api/setup/model-routes").json()
        assert r["ok"] is True and r["models"] == [] and r["task_routes"] == {}
        assert "by_model" in r["usage"] and "model_convs" in r["usage"]
        assert "unrestricted_convs" in r["usage"]


def test_get_usage_from_conv_route_snapshot(tmp_path, monkeypatch):
    """用量只转发 conv_route.stats_snapshot 已有字段：去掉 latency、丢掉 0 计数。"""
    from src.ai import conv_route
    monkeypatch.setattr(conv_route, "stats_snapshot", lambda store=None: {
        "by_model": {"chatgpt": {"calls": 4, "ok": 3, "fail": 1, "latency_ms_total": 999},
                     "idle": {"calls": 0, "ok": 0, "fail": 0, "latency_ms_total": 0}},
        "model_convs": {"chatgpt": 2, "zero": 0},
        "unrestricted_convs": 3,
    })
    client, _ = _client_mgr(tmp_path)
    u = client.get("/api/setup/model-routes").json()["usage"]
    assert u["by_model"] == {"chatgpt": {"calls": 4, "ok": 3, "fail": 1}}
    assert u["model_convs"] == {"chatgpt": 2}
    assert u["unrestricted_convs"] == 3


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
        assert (m.config.get("ai") or {}).get("models") == {}
        assert client.get("/api/setup/model-routes").json()["models"] == []

    def test_delete_replaces_base_catalog(self, tmp_path, monkeypatch):
        """主 config.yaml 里的档必须能被页面删掉：ai.models / ai.task_routes 整树替换，
        不能被 overlay 深合并把旧键合回来（浏览器实测 Gemini 删了保存后又出现）。"""
        import asyncio
        from src.utils.config_manager import ConfigManager
        self._patch_reload(monkeypatch)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(yaml.dump({
            "ai": {
                "api_key": "sk-primary",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-v4-flash",
                "models": {
                    "keep": {"base_url": "http://h:1", "model": "a"},
                    "gone": {"base_url": "http://h:2", "model": "b"},
                },
                "task_routes": {"assistant_planner": "keep", "computer_use": "gone"},
            }
        }, allow_unicode=True), encoding="utf-8")
        m = ConfigManager(str(cfg))
        asyncio.run(m.load())
        from fastapi.testclient import TestClient
        client = TestClient(_build_app(m))
        r = self._post(client,
                       [{"name": "keep", "base_url": "http://h:1", "model": "a"}],
                       {"assistant_planner": "keep"})
        assert r["ok"] is True
        names = [x["name"] for x in client.get("/api/setup/model-routes").json()["models"]]
        assert names == ["keep"]
        assert client.get("/api/setup/model-routes").json()["task_routes"] == {"assistant_planner": "keep"}
        m2 = ConfigManager(str(cfg))
        asyncio.run(m2.load())
        assert set((m2.config.get("ai") or {}).get("models") or {}) == {"keep"}
        assert (m2.config.get("ai") or {}).get("task_routes") == {"assistant_planner": "keep"}

    def test_extras_preserved_and_editable(self, tmp_path, monkeypatch):
        """2026-09-18 数据丢失修复：页面保存不再抹掉 YAML 手配的 label/max_ctx/cost_hint/
        supports_thinking/reasoning 与未知键；body 显式带出的元数据才覆盖，空值＝删。"""
        self._patch_reload(monkeypatch)
        client, m = _client_mgr(
            tmp_path,
            models={"gpt": {"base_url": "https://api.openai.com/v1", "model": "gpt-a",
                            "api_key": "sk-real-old-abcdef", "label": "GPT", "max_ctx": 200000,
                            "cost_hint": "¥2/M", "supports_thinking": False, "reasoning": True,
                            "num_ctx": 4096}})
        # ① 只回传三件套 + 掩码 key → 其余键原样保留
        r = self._post(client, [{"name": "gpt", "base_url": "https://api.openai.com/v1",
                                 "model": "gpt-b", "api_key": "sk-r…cdef"}])
        assert r["ok"] is True
        ov = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))["ai"]["models"]["gpt"]
        assert ov["model"] == "gpt-b" and ov["api_key"] == "sk-real-old-abcdef"
        assert ov["label"] == "GPT" and ov["max_ctx"] == 200000 and ov["cost_hint"] == "¥2/M"
        assert ov["supports_thinking"] is False and ov["reasoning"] is True and ov["num_ctx"] == 4096
        # ② 显式改 / 清：label 清空、max_ctx 改数、cost_hint 换
        r = self._post(client, [{"name": "gpt", "base_url": "https://api.openai.com/v1", "model": "gpt-b",
                                 "api_key": "sk-r…cdef", "label": "", "max_ctx": "300000",
                                 "cost_hint": "¥3/M"}])
        assert r["ok"] is True
        ov = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))["ai"]["models"]["gpt"]
        assert "label" not in ov and ov["max_ctx"] == 300000 and ov["cost_hint"] == "¥3/M"
        assert ov["supports_thinking"] is False and ov["num_ctx"] == 4096
        # ③ 坏值拒绝
        assert self._post(client, [{"name": "gpt", "base_url": "https://api.openai.com/v1",
                                    "model": "gpt-b", "max_ctx": "abc"}])["ok"] is False
        assert self._post(client, [{"name": "gpt", "base_url": "https://api.openai.com/v1",
                                    "model": "gpt-b", "max_ctx": 0}])["ok"] is False
        # ④ 新档带元数据直接落盘
        r = self._post(client, [{"name": "new", "base_url": "https://api.x.ai/v1", "model": "grok-a",
                                 "api_key": "sk-new", "label": "Grok 主力", "max_ctx": 128000}])
        assert r["ok"] is True
        ov = yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))["ai"]["models"]
        assert set(ov) == {"new"} and ov["new"]["label"] == "Grok 主力" and ov["new"]["max_ctx"] == 128000


# ── 端点健康探活（与 composer「模型」面板共用 conv_route.probe_spec 一套判定）────────
class _ProbeHx:
    """httpx.AsyncClient 假件：GET /models 按 status 应答；conn=True 时抛连接错。"""
    status = 200
    conn = False
    calls: list = []

    class _R:
        def __init__(self, sc):
            self.status_code = sc

        def json(self):
            return {"data": [{"id": "chatx"}, {"id": "gpt-a"}]}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        _ProbeHx.calls.append(("GET", url, headers))
        if _ProbeHx.conn:
            raise ConnectionError("refused")
        return self._R(_ProbeHx.status)

    async def post(self, url, json=None, headers=None):
        _ProbeHx.calls.append(("POST", url, headers))
        if _ProbeHx.conn:
            raise ConnectionError("refused")
        return self._R(_ProbeHx.status)


class TestProbeModelEndpoint:
    def _run(self, *a, **k):
        import asyncio
        from src.web.routes import unified_inbox_setup_routes as mod
        return asyncio.run(mod._probe_model_endpoint(*a, **k))

    def test_online_200_cloud_uses_models_list(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _ProbeHx)
        _ProbeHx.calls = []; _ProbeHx.status = 200; _ProbeHx.conn = False
        h = self._run("https://api.openai.com", "gpt-a", "k")
        assert h["online"] is True and h["status"] == 200 and h["probe"] == "models"
        assert _ProbeHx.calls[-1][1] == "https://api.openai.com/v1/models"   # 自动补 /v1（与运行时同口径）
        assert _ProbeHx.calls[-1][2]["Authorization"] == "Bearer k"
        assert "warn" not in h

    def test_401_is_offline_not_green(self, monkeypatch):
        """密钥错必须报离线（2026-09-18 前把 401 也画 🟢）。"""
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _ProbeHx)
        _ProbeHx.status = 401; _ProbeHx.conn = False
        h = self._run("https://api.openai.com/v1", "gpt-a", "bad")
        assert h["online"] is False and h["status"] == 401 and h["error"] == "http_401"

    def test_model_not_listed_warns(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _ProbeHx)
        _ProbeHx.status = 200; _ProbeHx.conn = False
        h = self._run("https://api.openai.com/v1", "gpt-zzz", "k")
        assert h["online"] is True and h["warn"] == "model_not_listed"

    def test_private_uses_ping(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _ProbeHx)
        _ProbeHx.calls = []; _ProbeHx.status = 200; _ProbeHx.conn = False
        h = self._run("http://192.168.0.173:8001", "chatx", "")
        assert h["online"] is True and h["probe"] == "chat"
        assert [c[0] for c in _ProbeHx.calls] == ["POST"]

    def test_conn_error_offline(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _ProbeHx)
        _ProbeHx.conn = True
        h = self._run("http://h:1", "x", "k")
        _ProbeHx.conn = False
        assert h["online"] is False and h["error"] == "connectionerror"

    def test_bad_url(self):
        h = self._run("notaurl", "x")
        assert h["online"] is False and h["error"] == "bad_url"


def _fake_probe_factory(seen: list, online: bool = True):
    async def _fake(base, model="", key="", timeout=5.0):
        seen.append((base, model, key))
        return {"online": online, "status": 200 if online else 401, "latency_ms": 7,
                "error": "" if online else "http_401"}
    return _fake


def test_get_probe_attaches_health(tmp_path, monkeypatch):
    import src.web.routes.unified_inbox_setup_routes as mod
    seen: list = []
    monkeypatch.setattr(mod, "_probe_model_endpoint", _fake_probe_factory(seen))
    client, m = _client_mgr(
        tmp_path,
        models={"planner_local": {"base_url": "http://192.168.0.173:8001", "model": "chatx"},
                "gpt": {"base_url": "https://api.openai.com/v1", "model": "gpt-a", "api_key": "sk-own"}})
    r = client.get("/api/setup/model-routes?probe=1").json()
    assert r["ok"] and all(x["health"]["online"] is True for x in r["models"])
    keys = {b: k for b, _m, k in seen}
    assert keys["http://192.168.0.173:8001"] == "sk-primary"     # 无自有 key → 主链 key
    assert keys["https://api.openai.com/v1"] == "sk-own"
    # 不带 probe → 无 health（快路径，不阻塞加载）
    r2 = client.get("/api/setup/model-routes").json()
    assert "health" not in r2["models"][0]


def test_probe_endpoint_probes_local_list_and_resolves_keys(tmp_path, monkeypatch):
    """POST /probe 体检**页面当前列表**（含未保存新档）：明文 key 直接用、掩码/空 → 存档真值 →
    主链 key；顺带带回展示事实（厂商/去向），密钥不回显。"""
    import src.web.routes.unified_inbox_setup_routes as mod
    seen: list = []
    monkeypatch.setattr(mod, "_probe_model_endpoint", _fake_probe_factory(seen))
    client, m = _client_mgr(
        tmp_path, models={"gpt": {"base_url": "https://api.openai.com/v1", "model": "gpt-a",
                                  "api_key": "sk-stored"}})
    r = client.post("/api/setup/model-routes/probe", json={"models": [
        {"name": "gpt", "base_url": "https://api.openai.com/v1", "model": "gpt-a", "api_key": "sk-s…ored"},
        {"name": "grok_new", "base_url": "https://api.x.ai/v1", "model": "grok-4", "api_key": "sk-new"},
        {"name": "lan", "base_url": "http://192.168.0.173:8001/v1", "model": "chatx"},
    ]}).json()
    assert r["ok"] and r["n"] == 3
    keys = {b: k for b, _m, k in seen}
    assert keys["https://api.openai.com/v1"] == "sk-stored"      # 掩码 → 同名存档真值
    assert keys["https://api.x.ai/v1"] == "sk-new"                # 新档明文
    assert keys["http://192.168.0.173:8001/v1"] == "sk-primary"   # 空 → 主链
    assert r["health"]["grok_new"]["online"] is True
    assert r["health"]["grok_new"]["facts"]["vendor"] == "xai"
    assert r["health"]["grok_new"]["facts"]["public_host"] == "api.x.ai"
    assert r["health"]["lan"]["facts"]["private"] is True and "192.168" not in r["health"]["lan"]["facts"]["public_host"]
    assert "sk-stored" not in str(r) and "sk-new" not in str(r) and "sk-primary" not in str(r)
    # 坏 body
    assert client.post("/api/setup/model-routes/probe", json={}).json()["ok"] is False
    # name:"" ＝ 主链行：端点/密钥服务端自填（客户端不持有主链 base_url），重复标记只探一次
    seen.clear()
    r = client.post("/api/setup/model-routes/probe", json={"models": [{"name": ""}, {"name": ""}]}).json()
    assert r["ok"] and r["n"] == 1 and seen == [("https://api.deepseek.com/v1", "deepseek-v4-flash", "sk-primary")]
    assert r["health"][""]["online"] is True and r["health"][""]["facts"]["vendor"] == "deepseek"
    # 内部档（_ 前缀，目录不列）GET 也带厂商/去向事实
    m.config["ai"]["models"]["_lan_tool"] = {"base_url": "http://192.168.0.173:8001/v1", "model": "chatx"}
    row = [x for x in client.get("/api/setup/model-routes").json()["models"] if x["name"] == "_lan_tool"][0]
    assert row["internal"] is True and row["vendor"] == "local" and row["private"] is True


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
    """模型与密钥页（2026-09-18 重排）：#dvmr 锚仍在（composer「管理模型与密钥 →」跳转目标）、
    厂商预设只填端点不写死模型版本号、「拉取模型列表」接 list-models、体检打 /probe（不重载整页）、
    内网地址不进模板、密钥绝不以明文渲染、删除必过 confirmAction、离开拦 beforeunload。
    开发者页只留跳转，旧锚 #dvmr 仍在。"""
    import pathlib
    import re
    card = pathlib.Path("src/web/templates/_model_routes_card.html").read_text(encoding="utf-8")
    page = pathlib.Path("src/web/templates/model_keys.html").read_text(encoding="utf-8")
    dev = pathlib.Path("src/web/templates/developer.html").read_text(encoding="utf-8")
    assert 'id="dvmr"' in card and "{% include \"_model_routes_card.html\" %}" in page
    assert "/api/setup/model-routes/list-models" in card
    assert "/api/setup/model-routes/probe" in card and "?probe=1" not in card
    assert "function mrApplyPreset(" in card and 'data-preset="openai"' in card
    i = card.index("const _DVMR_PRESETS")
    block = card[i:card.index("};", i)]
    for base in ("https://api.openai.com/v1", "https://generativelanguage.googleapis.com/v1beta/openai",
                 "https://api.x.ai/v1", "https://api.deepseek.com/v1"):
        assert base in block
    # 云厂商预设不写死会过期的模型版本号（DeepSeek 官方唯一现役 deepseek-flash 例外）
    assert not re.search(r"model:\s*'(gpt|gemini|grok|claude)-", block)
    # 内网地址不写死在模板：「自有算力」预设由服务端 local_endpoint 下发
    assert not re.search(r"192\.168\.|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.", card)
    assert "local_endpoint" in card
    # 密钥：输入框 password 型 + 眼睛切换；行内只渲染 has_key/掩码，明文字段不进 innerHTML
    assert 'type="password" id="dvmr-new-key"' in card and "function mrToggleKey(" in card
    assert "_dvmrEsc(m.api_key)" not in card and "_dvmrEsc(m.api_key ||" not in card
    # 删除确认 + 撤销；未保存离开拦截；403 整卡锁定
    assert "confirmAction(" in card and "function mrUndoDelete(" in card
    assert "beforeunload" in card and "function _dvmrLock(" in card and "fieldset" in card
    assert "filter(m => !m.internal)" in card
    assert "function _dvmrLocalizeTask(" in card
    assert "d.usage" in card and "function mrToggleInternal(" in card
    assert 'data-dvmr-act="internal" aria-expanded="' in card
    assert 'class="mk-seat"' in page and 'href="/workspace"' in page
    assert "function mrToggleRoutes(" in card
    # 撤销条给足阅读时间（删档会自动展开用途分配）
    # 页面样式：文字不以 opacity 调灰（admin-theme 禁则 1），说明字号 ≥ .75rem
    style = page[page.index("<style>"):page.index("</style>")]
    assert "opacity:." not in style.replace("opacity:1", "").replace("opacity:.35;transform", "")
    for cls in (".mk-sub", ".mk-seat", ".mk-hint", ".mk-hd-sub", ".mk-row-s", ".mk-field-hint", ".mk-route-d", ".mk-privacy",
                ".mk-ed-msg", ".save-result", ".mk-savebar-txt", ".mk-more"):
        m = re.search(re.escape(cls) + r"\{[^}]*font-size:(\.\d+)rem", style)
        assert m and float(m.group(1)) >= 0.75, f"{cls} 说明字号须 ≥ .75rem（admin-theme 禁则 2）"
    # 类名不得与 base.html `.main`（min-height:100vh / flex:1 主栏）撞车，否则主链行被撑成整屏高
    assert ".mk-row.is-main{" in style and ".mk-tag.is-main{" in style
    assert ".mk-row.main{" not in style and ".mk-tag.main{" not in style
    assert "(main ? ' is-main'" in card and "_dvmrTag(window.T('mk_js_main_tag'), 'is-main'" in card
    # [hidden] 必须压过 .banner/.mk-undo 的 display:flex，否则 403 横幅和撤销条会一直露出来
    assert re.search(r"\.mk-root\s+\[hidden\]\s*\{\s*display\s*:\s*none", style)
    assert 'id="dvmr"' in dev and 'href="/model-keys"' in dev
    assert "function mrApplyPreset(" not in dev
    assert "location.hash==='#dvmr'" in dev
    from src.web.i18n_packs import developer_page as dp
    from src.web.web_i18n import get_translations
    hant = get_translations("zh_hant")
    for k in ("dv_mr_list_btn", "nav_model_keys", "dv_mr_moved", "dv_mr_moved_btn", "mk_need_admin",
              "mk_sub", "mk_seat", "mk_seat_go", "mk_btn_add", "mk_routes_title", "mk_js_del_confirm", "mk_js_leave",
              "mk_js_show_internal", "mk_js_usage_convs", "mk_js_internal_d", "mk_js_unr_row_d",
              "dv_mr_task_planner", "dv_mr_task_qa", "dv_mr_task_memory", "dv_mr_task_custom_d"):
        assert k in dp.ZH and k in dp.EN and hant.get(k), k
    # 本批新键（mk_* / dv_mr_task_*）繁体手写在本包，不等 regen
    new_keys = [k for k in dp.ZH if k.startswith("mk_") or k.startswith("dv_mr_task_")]
    assert new_keys and all(k in dp.ZH_HANT for k in new_keys), [k for k in new_keys if k not in dp.ZH_HANT]
    # 已下线的旧键不许残留（借用键 / 内网泄漏文案 / 无消费方任务）
    for k in ("dv_mr_preset_local", "dv_mr_js_none", "dv_mr_js_task_default", "dv_mr_sub"):
        assert k not in dp.ZH and k not in dp.EN, k
    assert "dv_cc_js_inherit" not in card and "msg_s416" not in card and "tg_js_011" not in card
