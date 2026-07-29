"""Phase 4 坐席工作台：快捷回复 / KB 检索 / Contacts 档案富化。"""

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.models import CHANNEL_LINE
from src.contacts.store import ContactStore
from src.web.routes.unified_inbox_routes import register_unified_inbox_routes


class _Templates:
    def TemplateResponse(self, request, name, context):
        raise AssertionError("page rendering is not used in API tests")


class _CfgMgr:
    def __init__(self, cfg):
        self.config = cfg

    def get_dynamic_templates_config(self):
        return self.config.get("templates_dyn") or {}


class LineSvc:
    account_id = "line-a"
    _merged_cfg = {"label": "LINE-A"}

    def list_chats(self, limit):
        return [{
            "chat_key": "u1", "name": "Line User",
            "last_peer_text": "hello", "last_ts": 100, "unread_count": 1,
        }]

    def status(self):
        return {"running": True, "serial": "s1"}


def _client(*, config_manager=None, contacts=None, kb_store=None):
    app = FastAPI()

    def page_auth(request: Request):
        return True

    def api_auth(request: Request):
        return True

    register_unified_inbox_routes(
        app,
        page_auth=page_auth,
        api_auth=api_auth,
        templates=_Templates(),
        config_manager=config_manager,
    )
    app.state.line_rpa_services = [LineSvc()]
    if contacts is not None:
        app.state.contacts = contacts
    if kb_store is not None:
        app.state.kb_store = kb_store
    return TestClient(app)


def test_templates_merges_workspace_and_messenger():
    cfg = _CfgMgr({
        "workspace": {
            "quick_templates": [{"label": "WS", "text": "工作台话术"}],
        },
        "messenger_rpa": {
            "approval_templates": [{"label": "MS", "text": "Messenger 话术"}],
        },
        "templates_dyn": {
            "greeting": ["动态问候"],
        },
    })
    c = _client(config_manager=cfg)
    resp = c.get("/api/unified-inbox/templates")
    assert resp.status_code == 200
    data = resp.json()
    labels = {t["label"] for t in data["templates"]}
    assert "WS" in labels
    assert "MS" in labels
    assert "greeting" in labels


def test_kb_search_returns_entries():
    class _Kb:
        def search(self, query, top_k=5, lang="zh", query_vec=None):
            return {
                "entries": [{
                    "id": "e1",
                    "title": "退款政策",
                    "example_reply_zh": "7 天内可退",
                    "category": "售后",
                    "_score": 0.9,
                    "_mode": "bm25",
                }],
                "search_mode": "bm25",
            }

    c = _client(kb_store=_Kb())
    resp = c.get("/api/unified-inbox/kb-search?q=退款")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["entries"][0]["title"] == "退款政策"
    assert "7 天内" in data["entries"][0]["answer"]


def test_kb_search_unavailable():
    c = _client(kb_store=None)
    resp = c.get("/api/unified-inbox/kb-search?q=test")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ── P0 推荐降噪：auto 置信闸 + confidence 标签（unified_inbox_aux_read_routes）──


def _kb_two_scores():
    class _Kb:
        def search(self, query, top_k=5, lang="zh", query_vec=None):
            return {
                "entries": [
                    {"id": "hi", "title": "高分条目", "example_reply_zh": "答A",
                     "_score": 20.0, "_mode": "bm25"},
                    {"id": "lo", "title": "低分条目", "example_reply_zh": "答B",
                     "_score": 2.0, "_mode": "bm25"},
                ],
                "search_mode": "bm25",
            }
    return _Kb()


def test_kb_search_auto_gates_low_confidence():
    """auto=1（会话打开自动推送）：低于噪声闸的条目不返回，高分条目带 high 置信。"""
    c = _client(kb_store=_kb_two_scores())
    data = c.get("/api/unified-inbox/kb-search?q=test&auto=1").json()
    ids = [e["entry_id"] for e in data["entries"]]
    assert "hi" in ids and "lo" not in ids
    assert data["entries"][0]["confidence"] == "high"


def test_kb_search_manual_keeps_low_scores():
    """手动检索（无 auto）不做噪声过滤——坐席主动搜就该什么都能看到（回归钉）。"""
    c = _client(kb_store=_kb_two_scores())
    data = c.get("/api/unified-inbox/kb-search?q=test").json()
    by_id = {e["entry_id"]: e for e in data["entries"]}
    assert set(by_id) == {"hi", "lo"}
    assert by_id["hi"]["confidence"] == "high"
    assert by_id["lo"]["confidence"] == "mid"


def test_kb_search_auto_all_low_returns_empty():
    class _Kb:
        def search(self, query, top_k=5, lang="zh", query_vec=None):
            return {"entries": [
                {"id": "j1", "title": "杂讯", "example_reply_zh": "x",
                 "_score": 1.2, "_mode": "bm25"},
            ], "search_mode": "bm25"}
    c = _client(kb_store=_Kb())
    data = c.get("/api/unified-inbox/kb-search?q=test&auto=1").json()
    assert data["ok"] is True
    assert data["entries"] == []


def test_kb_search_hybrid_scale_skips_min_gate():
    """hybrid(RRF) 分数是排名共识刻度（~0.016-0.033），不套 BM25 噪声闸；0.03+ 记 high。"""
    class _Kb:
        def search(self, query, top_k=5, lang="zh", query_vec=None):
            return {"entries": [
                {"id": "h1", "title": "双路共识", "example_reply_zh": "a",
                 "_score": 0.032, "_mode": "hybrid"},
                {"id": "h2", "title": "单路", "example_reply_zh": "b",
                 "_score": 0.016, "_mode": "hybrid"},
            ], "search_mode": "hybrid"}
    c = _client(kb_store=_Kb())
    data = c.get("/api/unified-inbox/kb-search?q=test&auto=1").json()
    by_id = {e["entry_id"]: e for e in data["entries"]}
    assert set(by_id) == {"h1", "h2"}
    assert by_id["h1"]["confidence"] == "high"
    assert by_id["h2"]["confidence"] == "mid"


def test_kb_search_thresholds_configurable():
    """阈值经 config inbox.kb_suggest 覆写（与 config 热更新同一读取路径）。"""
    cfg = _CfgMgr({"inbox": {"kb_suggest": {"auto_min_score": 25, "auto_high_score": 30}}})
    c = _client(config_manager=cfg, kb_store=_kb_two_scores())
    data = c.get("/api/unified-inbox/kb-search?q=test&auto=1").json()
    assert data["entries"] == []


class _FunnelStore:
    def __init__(self):
        self.recorded = []

    def record_kb_recommendation(self, **kw):
        self.recorded.append(kw)


def test_kb_search_auto_logs_funnel_and_returns_rec_id():
    """P3 漏斗：auto 模式把推荐落 kb_recommendation_log 并给每条带 rec_id
    （前端引用时回调既有 /api/workspace/kb-click 完成 推荐→引用 转化归因）。"""
    st = _FunnelStore()
    c = _client(kb_store=_kb_two_scores())
    c.app.state.inbox_store = st
    d = c.get(
        "/api/unified-inbox/kb-search?q=test&auto=1&conv=telegram:default:u1").json()
    assert d["ok"] is True and d["entries"]
    assert all(e.get("rec_id") for e in d["entries"])
    assert len(st.recorded) == len(d["entries"])
    assert st.recorded[0]["conversation_id"] == "telegram:default:u1"
    assert st.recorded[0]["entry_id"] == d["entries"][0]["entry_id"]


def test_kb_search_manual_does_not_log_funnel():
    """手动检索不落漏斗（坐席主动搜不是"系统推荐"，混进来会稀释命中率口径）。"""
    st = _FunnelStore()
    c = _client(kb_store=_kb_two_scores())
    c.app.state.inbox_store = st
    d = c.get("/api/unified-inbox/kb-search?q=test").json()
    assert d["entries"]
    assert st.recorded == []
    assert all("rec_id" not in e for e in d["entries"])


def test_profile_enriched_with_contacts_journey(tmp_path):
    from src.contacts import ContactGateway, GatewayContactHooks, HandoffTokenService, MergeService

    store = ContactStore(tmp_path / "contacts.db")
    gw = ContactGateway(store, HandoffTokenService(store, ttl_seconds=3600), MergeService(store))
    hooks = GatewayContactHooks(gw)
    ctx = hooks.on_message(
        channel=CHANNEL_LINE, account_id="line-a", external_id="u1",
        direction="in", text_preview="你好",
    )
    assert ctx is not None
    store.update_journey(ctx.journey.journey_id, intimacy_score=72.5, _touch=False)
    store.update_contact(ctx.contact.contact_id, primary_name="张三")

    contacts = SimpleNamespace(store=store)
    c = _client(contacts=contacts)
    resp = c.get("/api/unified-inbox/profile?platform=line&account_id=line-a&chat_key=u1")
    assert resp.status_code == 200
    prof = resp.json()["profile"]
    assert prof["display_name"] == "张三"
    assert prof["contacts"]["funnel_stage"] == "ENGAGED"
    assert prof["contacts"]["intimacy_score"] == 72.5
    assert "深入互动" in prof["relationship"]["stage"]
    assert any("高亲密" in t for t in prof["tags"])
    store.close()
