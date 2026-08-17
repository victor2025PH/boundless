"""platform/leadbus 契约承接端点测试：/api/leadbus/ingest、/api/leadbus/status。

裸 FastAPI() + register_leadbus_routes + 真 InboxStore(tmp_path)（不启 create_app/main.py），
仿 test_care_routes.py 的模式。覆盖：合法信封 ingest 成功落库 + 字段映射取舍；缺必填字段
返回 400 且不崩；store 未挂载时软降级 {"ready": false}（不 500）；status 端点；鉴权确实生效。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from src.inbox.store import InboxStore
from src.web.routes.leadbus_routes import register_leadbus_routes


def _client(tmp_path, *, with_store=True, auth_ok=True):
    app = FastAPI()

    def _auth(request: Request):
        if not auth_ok:
            raise HTTPException(status_code=401, detail="Unauthorized")
        return True

    register_leadbus_routes(app, api_auth=_auth, config_manager=None)
    store = None
    if with_store:
        store = InboxStore(tmp_path / "leadbus_inbox.db")
        app.state.inbox_store = store
    return TestClient(app), store


def _envelope(**overrides):
    env = {
        "lead_id": "lead_abc123",
        "ts": "2026-07-19T10:00:00Z",
        "source": {"product": "zhituo", "platform": "telegram", "campaign": "utm_x"},
        "lead": {
            "external_id": "tg:123",
            "handle": "@who",
            "profile": {"lang": "en", "funnel_stage": "new", "intent_score": 0.72},
        },
        "assign_hint": {"domain": "ecommerce", "persona": "sales"},
    }
    env.update(overrides)
    return {"lead": env}


# ── ① 合法信封 ingest 成功 ──────────────────────────────────────────────
def test_ingest_valid_envelope_succeeds(tmp_path):
    client, store = _client(tmp_path)
    r = client.post("/api/leadbus/ingest", json=_envelope())
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["assigned"] is True
    assert body["lead_id"] == "lead_abc123"
    assert body["session_id"] == "telegram:zhituo:tg:123"

    convs = store.list_conversations(limit=10, platform="telegram")
    assert any(c["conversation_id"] == body["session_id"] for c in convs)


def test_ingest_maps_handle_to_name_and_synthesizes_placeholder_text(tmp_path):
    """handle → name；无真实聊天正文 → text 合成「[线索捕获] handle」占位（非真实消息）。"""
    client, store = _client(tmp_path)
    r = client.post("/api/leadbus/ingest", json=_envelope())
    cid = r.json()["session_id"]
    row = store.get_conversation(cid)
    assert row["display_name"] == "@who"
    assert row["last_text"] == "[线索捕获] @who"


def test_ingest_falls_back_to_external_id_when_handle_missing(tmp_path):
    client, store = _client(tmp_path)
    env = _envelope()
    env["lead"]["lead"] = dict(env["lead"]["lead"])
    env["lead"]["lead"].pop("handle")
    r = client.post("/api/leadbus/ingest", json=env)
    cid = r.json()["session_id"]
    row = store.get_conversation(cid)
    assert row["last_text"] == "[线索捕获] tg:123"


def test_ingest_external_id_used_as_chat_key_without_splitting_prefix(tmp_path):
    """external_id 形如 "tg:123" 原样当 chat_key，不拆分平台前缀；account_id 取 source.product 顶替。"""
    client, store = _client(tmp_path)
    r = client.post("/api/leadbus/ingest", json=_envelope())
    cid = r.json()["session_id"]
    row = store.get_conversation(cid)
    assert row["chat_key"] == "tg:123"
    assert row["account_id"] == "zhituo"
    assert row["platform"] == "telegram"


def test_ingest_repeated_same_lead_reuses_same_conversation(tmp_path):
    """同一 external_id 重投（补投/重试）应落同一条会话，而不是每次新开一条。"""
    client, store = _client(tmp_path)
    r1 = client.post("/api/leadbus/ingest", json=_envelope())
    r2 = client.post("/api/leadbus/ingest", json=_envelope())
    assert r1.json()["session_id"] == r2.json()["session_id"]
    convs = store.list_conversations(limit=50, platform="telegram")
    assert len(convs) == 1


# ── ② 缺字段返回错误不崩 ───────────────────────────────────────────────
def test_ingest_missing_lead_key_returns_400(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/leadbus/ingest", json={})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_ingest_missing_source_product_returns_400(tmp_path):
    client, _ = _client(tmp_path)
    env = _envelope()
    del env["lead"]["source"]["product"]
    r = client.post("/api/leadbus/ingest", json=env)
    assert r.status_code == 400
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "missing_required_field"


def test_ingest_missing_source_platform_returns_400(tmp_path):
    client, _ = _client(tmp_path)
    env = _envelope()
    del env["lead"]["source"]["platform"]
    r = client.post("/api/leadbus/ingest", json=env)
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_ingest_missing_external_id_returns_400(tmp_path):
    client, _ = _client(tmp_path)
    env = _envelope()
    env["lead"]["lead"] = dict(env["lead"]["lead"])
    del env["lead"]["lead"]["external_id"]
    r = client.post("/api/leadbus/ingest", json=env)
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_ingest_malformed_json_body_returns_400_not_500(tmp_path):
    """完全不是信封形状的 body（如空对象/非 dict lead）也走 400，不 500。"""
    client, _ = _client(tmp_path)
    r = client.post("/api/leadbus/ingest", json={"lead": "not-a-dict"})
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_ingest_without_store_soft_degrades_not_500(tmp_path):
    """store 未挂载：合法信封也不应 500，而是 {"ready": false} 软降级（类 deferred_outbox 风格）。"""
    client, _ = _client(tmp_path, with_store=False)
    r = client.post("/api/leadbus/ingest", json=_envelope())
    assert r.status_code == 200
    assert r.json() == {"ready": False}


# ── ③ status 端点 ──────────────────────────────────────────────────────
def test_status_ready_true_when_store_mounted(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/api/leadbus/status")
    assert r.status_code == 200
    assert r.json() == {"ready": True}


def test_status_ready_false_without_store(tmp_path):
    client, _ = _client(tmp_path, with_store=False)
    r = client.get("/api/leadbus/status")
    assert r.status_code == 200
    assert r.json() == {"ready": False}


# ── 鉴权确实生效（Depends(api_auth) 全程保护）──────────────────────────
def test_ingest_requires_auth(tmp_path):
    client, _ = _client(tmp_path, auth_ok=False)
    r = client.post("/api/leadbus/ingest", json=_envelope())
    assert r.status_code == 401


def test_status_requires_auth(tmp_path):
    client, _ = _client(tmp_path, auth_ok=False)
    r = client.get("/api/leadbus/status")
    assert r.status_code == 401
