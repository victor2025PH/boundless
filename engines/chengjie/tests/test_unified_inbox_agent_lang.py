"""P1（2026-08-16）：坐席级「我的语言」偏好——GET/POST /api/unified-inbox/agent-lang。

背景：翻译弹层的坐席语言此前只靠浏览器推导（localStorage 持久），换机/清缓存即丢。
本端点把偏好落服务端 KV（键 ``inbox.agent_lang.{agent_id}``，身份取会话 session，
无 SessionMiddleware 回落共享 ``agent``）。前端 ``_agentLang()`` 解析序：
本偏好 > UI 语言 en > 浏览器语言 > html lang > zh。

覆盖：读默认空 / 写读回环 / 归一化（zh-cn→zh）/ 空串清除 / 语种白名单拒绝 /
KV 键命名（per-agent 前缀）与 updated_by 留痕。
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


def _client(tmp_path):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app, page_auth=page_auth, api_auth=api_auth, templates=_Templates(),
    )
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    return TestClient(app)


def test_agent_lang_default_empty(tmp_path):
    c = _client(tmp_path)
    d = c.get("/api/unified-inbox/agent-lang").json()
    assert d["ok"] is True
    assert d["lang"] == ""
    # 无 SessionMiddleware → 身份回落共享 "agent"（单坐席部署语义）
    assert d["agent_id"] == "agent"


def test_agent_lang_roundtrip_and_normalize(tmp_path):
    c = _client(tmp_path)
    assert c.post("/api/unified-inbox/agent-lang", json={"lang": "vi"}).json()["ok"]
    assert c.get("/api/unified-inbox/agent-lang").json()["lang"] == "vi"
    # 归一化：zh-cn → zh
    assert c.post("/api/unified-inbox/agent-lang", json={"lang": "zh-cn"}).json()["lang"] == "zh"
    assert c.get("/api/unified-inbox/agent-lang").json()["lang"] == "zh"


def test_agent_lang_clear_with_empty(tmp_path):
    c = _client(tmp_path)
    c.post("/api/unified-inbox/agent-lang", json={"lang": "th"})
    d = c.post("/api/unified-inbox/agent-lang", json={"lang": ""}).json()
    assert d["ok"] is True and d["lang"] == ""
    assert c.get("/api/unified-inbox/agent-lang").json()["lang"] == ""


def test_agent_lang_rejects_non_whitelist(tmp_path):
    c = _client(tmp_path)
    d = c.post("/api/unified-inbox/agent-lang", json={"lang": "tlh"}).json()
    assert d["ok"] is False and d["error"] == "bad_lang"
    # 拒绝时不落库
    assert c.get("/api/unified-inbox/agent-lang").json()["lang"] == ""


def test_agent_lang_accepts_hymt_variants_and_european(tmp_path):
    """UI 目录扩到 HY-MT 34 码后，繁体/粤语/法语必须能落库（否则芯片能选、保存 400）。"""
    c = _client(tmp_path)
    for lang in ("zh-tw", "yue", "fr"):
        d = c.post("/api/unified-inbox/agent-lang", json={"lang": lang}).json()
        assert d["ok"] is True and d["lang"] == lang, lang
        assert c.get("/api/unified-inbox/agent-lang").json()["lang"] == lang


def test_agent_lang_key_naming_and_audit(tmp_path):
    """KV 键必须是 per-agent 前缀（inbox.agent_lang.{agent}），updated_by 留痕。"""
    c = _client(tmp_path)
    c.post("/api/unified-inbox/agent-lang", json={"lang": "es"})
    store = c.app.state.inbox_store
    rows = store.list_app_settings("inbox.agent_lang")
    assert len(rows) == 1
    assert rows[0]["key"] == "inbox.agent_lang.agent"
    assert rows[0]["value"] == "es"
    assert rows[0]["updated_by"] == "agent"


def test_agent_lang_isolated_from_default_lang(tmp_path):
    """与「默认译文语言」轴键空间不串（LIKE 前缀互不误匹配）。"""
    c = _client(tmp_path)
    c.post("/api/unified-inbox/agent-lang", json={"lang": "ko"})
    c.post("/api/unified-inbox/default-lang", json={"scope": "global", "lang": "en"})
    assert c.get("/api/unified-inbox/agent-lang").json()["lang"] == "ko"
    d = c.get("/api/unified-inbox/default-lang?platform=telegram&account_id=a").json()
    assert d["resolved"] == "en"
    disp = c.get("/api/unified-inbox/default-lang/all").json()["items"]
    assert all(it["lang"] == "en" for it in disp) and len(disp) == 1
