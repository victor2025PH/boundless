"""#154（2026-09-04）会话「发→X」铆定被程序清空——store 审计 + 端点清除语义门禁。

事故复盘（backend.log YQJMZ7，会话 telegram:7331682688:8852939166）：
13:11:09 sendpoint 还在按 ``pin=en`` 改写日语草稿；14:50:35/14:50:45/14:58:15/
14:58:24 四条日语原文直接出门，sendpoint 零日志＝``outbound_lang_pin()`` 读空。
报障人确认没动过选择器——铆定是被**程序**清的：前端 ``_persistXlatePrefs``
在任何翻译偏好变动时把当时可能为空的 ``_xlateOut`` POST 到
``/api/unified-inbox/conv-xlate-out``，服务端 ``lang=""`` 直接 DELETE，无审计。

本门禁钉三条：
1. POST 空串 = **no-op**（不删行），如实回当前值；
2. 删除必须显式 ``clear:true``，且落审计行（source=clear）；
3. store 层每次真实跃迁都进 ``conversation_outbound_lang_log``（prev/new/source）。
"""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


_CONV = {"platform": "telegram", "account_id": "7331682688",
         "chat_key": "8852939166"}
_ROOT = Path(__file__).resolve().parent.parent
_INBOX_HTML = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"


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


def _get(c):
    return c.get("/api/unified-inbox/conv-xlate-out", params=_CONV).json()


def _post(c, **extra):
    return c.post("/api/unified-inbox/conv-xlate-out",
                  json=dict(_CONV, **extra)).json()


# ── 端点：空串 no-op / 显式 clear ────────────────────────────────────────────

def test_empty_lang_is_noop_and_returns_current(tmp_path):
    """#154 根因三：空串曾直接 DELETE。现在不动库，如实回当前值。"""
    c = _client(tmp_path)
    assert _post(c, lang="en", source="ui_select")["lang"] == "en"
    d = _post(c, lang="")
    assert d["ok"] is True and d["lang"] == "en" and d["noop"] is True
    assert _get(c)["lang"] == "en"     # 铆定还在——事故的直接止血点


def test_explicit_clear_deletes_row(tmp_path):
    c = _client(tmp_path)
    _post(c, lang="en", source="ui_select")
    d = _post(c, lang="", clear=True)
    assert d["ok"] is True and d["lang"] == ""
    assert _get(c)["lang"] == ""


def test_clear_flag_wins_over_stale_lang(tmp_path):
    """clear:true 是明确意图：即便 body 还带着旧语种也照删（前端去抖竞态）。"""
    c = _client(tmp_path)
    _post(c, lang="en", source="ui_select")
    assert _post(c, lang="en", clear=True)["lang"] == ""
    assert _get(c)["lang"] == ""


def test_noop_on_unset_conversation(tmp_path):
    """从没设过的会话收到空串：不建行、不报错，回空。"""
    c = _client(tmp_path)
    d = _post(c, lang="")
    assert d["ok"] is True and d["lang"] == "" and d["noop"] is True
    store = c.app.state.inbox_store
    assert store.list_outbound_lang_log("telegram:7331682688:8852939166") == []


def test_auto_still_writable(tmp_path):
    """'auto'（显式跟客户语言）不是清除——必须照常落库。"""
    c = _client(tmp_path)
    assert _post(c, lang="auto", source="ui_select")["lang"] == "auto"
    assert _get(c)["lang"] == "auto"


def test_bad_lang_rejected_without_touching_row(tmp_path):
    c = _client(tmp_path)
    _post(c, lang="en", source="ui_select")
    assert _post(c, lang="tlh")["error"] == "bad_lang"
    assert _get(c)["lang"] == "en"


# ── 审计表（store 层） ───────────────────────────────────────────────────────

def test_audit_records_prev_new_source(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:7331682688:8852939166"
    store.set_outbound_lang(cid, "en", source="ui_select")
    store.set_outbound_lang(cid, "ja", source="api")
    store.set_outbound_lang(cid, "", source="clear")
    rows = store.list_outbound_lang_log(cid)
    assert [(r["prev"], r["new"], r["source"]) for r in rows] == [
        ("ja", "", "clear"), ("en", "ja", "api"), ("", "en", "ui_select"),
    ]


def test_audit_skips_no_change(tmp_path):
    """同值重写不落行——审计轨只记真实跃迁（与 automation_mode_log 同纪律）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:1"
    store.set_outbound_lang(cid, "en", source="ui_select")
    store.set_outbound_lang(cid, "en", source="ui_prefsync")
    assert len(store.list_outbound_lang_log(cid)) == 1


def test_audit_unknown_source_normalized(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:2"
    store.set_outbound_lang(cid, "en", source="whatever")
    assert store.list_outbound_lang_log(cid)[0]["source"] == "other"


def test_set_outbound_lang_returns_prev(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:3"
    assert store.set_outbound_lang(cid, "en", source="api") == ""
    assert store.set_outbound_lang(cid, "ja", source="api") == "en"


def test_clear_logs_warning(tmp_path, caplog):
    """清除必须留 WARNING——「铆定突然没了」要能在日志里当场看见（#154 排障成本）。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:4"
    store.set_outbound_lang(cid, "en", source="ui_select")
    with caplog.at_level("WARNING"):
        store.set_outbound_lang(cid, "", source="clear")
    assert any("#154" in r.message or "铆定被清除" in r.getMessage()
               for r in caplog.records)


def test_route_records_source_in_audit(tmp_path):
    """端点必须把 source 透传进审计（区分「坐席真选」与「偏好同步」）。"""
    c = _client(tmp_path)
    _post(c, lang="en", source="ui_select")
    _post(c, lang="", clear=True)
    rows = c.app.state.inbox_store.list_outbound_lang_log(
        "telegram:7331682688:8852939166")
    assert [r["source"] for r in rows] == ["clear", "ui_select"]


# ── 前端接线钉（源码扫描，风格同 test_voice_outbound_xlate） ─────────────────

def test_frontend_prefsync_no_longer_writes_server():
    """``_persistXlatePrefs`` 绝不能再同步服务端——它被语气/预览/反译等一切
    偏好变动调用，而彼时 ``_xlateOut`` 可能只是未 hydrate 的空值（#154 根因二）。"""
    src = _INBOX_HTML.read_text(encoding="utf-8")
    body = src.split("function _persistXlatePrefs()", 1)[1].split(
        "function _syncConvXlateOutSrv", 1)[0]
    assert "_syncConvXlateOutSrv(" not in body, (
        "_persistXlatePrefs 又在同步「发→X」到服务端（#154 会再次清空铆定）")


def test_frontend_syncs_only_when_out_changed():
    """只有 ``#xlate-out`` 真被改动才写服务端事实源（source=ui_select）。"""
    src = _INBOX_HTML.read_text(encoding="utf-8")
    body = src.split("function _onXlateChange()", 1)[1].split(
        "function _persistXlatePrefs", 1)[0]
    assert "prevOut" in body and "_xlateOut!==prevOut" in body
    assert "_syncConvXlateOutSrv('ui_select')" in body


def test_frontend_clear_is_explicit():
    """前端清除走 ``clear:true``——裸空串在服务端已是 no-op，不带旗标＝静默失效。"""
    src = _INBOX_HTML.read_text(encoding="utf-8")
    body = src.split("function _syncConvXlateOutSrv", 1)[1].split(
        "async function _hydrateConvXlateOut", 1)[0]
    assert "body.clear=true" in body


def test_frontend_hydrates_from_server_on_conv_switch():
    """``_loadXlateForConv`` 必须拉服务端「发→X」（清 localStorage/换机仍是 en）。"""
    src = _INBOX_HTML.read_text(encoding="utf-8")
    body = src.split("function _loadXlateForConv(c)", 1)[1].split(
        "\nasync function _loadServerDefaultLang", 1)[0]
    assert "_hydrateConvXlateOut(c)" in body
    hyd = src.split("async function _hydrateConvXlateOut(c)", 1)[1][:1600]
    assert "/api/unified-inbox/conv-xlate-out?" in hyd
    assert "selectedChat!==c" in hyd, "过期响应必须丢弃（切会话竞态）"


def test_frontend_seat_target_uses_inbound_setting():
    """#154(d)：坐席侧译文目标语＝会话「收→」，不再拿出站铆定当阅读语言。"""
    src = _INBOX_HTML.read_text(encoding="utf-8")
    assert "function _seatXlTarget()" in src
    assert "_xlateOut||_xlateIn||_agentLang()" not in src, (
        "坐席侧译文仍在用出站铆定当目标语（铆英的会话里日文文档译成英文）")
