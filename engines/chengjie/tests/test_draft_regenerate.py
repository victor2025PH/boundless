# -*- coding: utf-8 -*-
"""陈旧稿一键重生成端点门禁（P1 2026-08-09）。

stale_approve_hours 护栏（2026-07-29）把老稿拦下（409 too_stale）后，坐席的
出路此前只有手工两条。POST /api/drafts/{id}/regenerate ＝按**当前**会话上下文
重走人设产线 → 原子作废旧稿 → 铸新 pending 稿。守四条硬语义：

1. 生成成功才作废旧稿（生成失败 → 502，旧稿原样保留，不会「作废了却没新稿」）；
2. 竞态窗内旧稿被同事处置 → 409 already_resolved，**不铸新稿**（防双活）；
3. 新稿 autopilot 永远按 review 口径（只产 L1/L3/L4）——重生成是人工审阅流，
   绝不产 L2 落进自动投递批次；
4. 终态旧稿（approved/rejected/cancelled）→ 409；不存在 → 404。
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.drafts import DraftService
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.web.routes.drafts_routes import register_drafts_routes


def _api_auth(request: Request):
    return True


def _client(tmp_path):
    app = FastAPI()
    register_drafts_routes(app, api_auth=_api_auth)
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    app.state.draft_service = DraftService(inbox_store=store)
    return TestClient(app), store


def _seed(store, *, cid="telegram:a:100", status="pending"):
    store.upsert_conversation(InboxConversation(
        conversation_id=cid, platform="telegram", account_id="a",
        chat_key=cid.rsplit(":", 1)[-1], chat_type="private",
        display_name="N"))
    did = store.upsert_draft({
        "source_kind": "inbox", "source_id": "seed_1",
        "conversation_id": cid, "platform": "telegram", "account_id": "a",
        "chat_key": cid.rsplit(":", 1)[-1], "peer_text": "早上那条老消息",
        "draft_text": "我刚到家，娃正在客厅拼乐高", "status": status,
    })
    return did


def _patch_gen(monkeypatch, *, ok=True, reply="现在这个时间点的新回复",
               calls=None):
    async def _fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return {"ok": ok, "reply": reply if ok else "", "reply_lang": "zh"}

    import src.inbox.persona_reply as pr
    monkeypatch.setattr(pr, "generate_persona_reply", _fake)


def test_regenerate_happy_path(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    old_id = _seed(store)
    calls = []
    _patch_gen(monkeypatch, calls=calls)
    r = c.post(f"/api/drafts/{old_id}/regenerate")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["cancelled"] == old_id
    assert body["draft_text"] == "现在这个时间点的新回复"
    # 旧稿终态 cancelled + 处置人打 regen 标
    old = store.get_draft(old_id)
    assert old["status"] == "cancelled"
    assert str(old["decided_by"]).startswith("regen:")
    # 新稿 pending + 溯源 + review 口径定级（低风险 → L1，绝不 L2）
    new = store.get_draft(body["draft_id"])
    assert new["status"] == "pending"
    assert new["trace_id"] == f"regen:{old_id}"
    assert new["autopilot_level"] == "L1"
    assert body["autopilot_level"] != "L2"
    # 生成器吃的是会话标识（当前上下文），不是旧稿正文
    assert calls and calls[0]["conversation_id"] == "telegram:a:100"
    store.close()


def test_regenerate_generation_failure_keeps_old_draft(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    old_id = _seed(store)
    _patch_gen(monkeypatch, ok=False)
    r = c.post(f"/api/drafts/{old_id}/regenerate")
    assert r.status_code == 502
    assert store.get_draft(old_id)["status"] == "pending"   # 旧稿原样保留
    # 没有铸出任何新稿
    pend = store.list_drafts(status="pending", limit=10)
    assert len(pend) == 1
    store.close()


def test_regenerate_resolved_draft_conflicts(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    old_id = _seed(store, status="approved")
    calls = []
    _patch_gen(monkeypatch, calls=calls)
    r = c.post(f"/api/drafts/{old_id}/regenerate")
    assert r.status_code == 409
    assert not calls, "终态稿不应触发生成（省 LLM）"
    store.close()


def test_regenerate_missing_draft_404(tmp_path):
    c, store = _client(tmp_path)
    r = c.post("/api/drafts/no-such-draft/regenerate")
    assert r.status_code == 404
    store.close()


def test_regenerate_race_lost_does_not_mint(tmp_path, monkeypatch):
    """生成期间旧稿被同事处置 → 409 且不铸新稿（原子闸门语义）。"""
    c, store = _client(tmp_path)
    old_id = _seed(store)

    async def _fake(**kwargs):
        # 模拟：LLM 生成期间另一窗口把旧稿通过了
        store.update_draft_status(old_id, status="approved", decided_by="peer")
        return {"ok": True, "reply": "迟到的生成结果", "reply_lang": "zh"}

    import src.inbox.persona_reply as pr
    monkeypatch.setattr(pr, "generate_persona_reply", _fake)
    r = c.post(f"/api/drafts/{old_id}/regenerate")
    assert r.status_code == 409
    assert store.get_draft(old_id)["status"] == "approved"   # 同事的处置保留
    assert len(store.list_drafts(status="pending", limit=10)) == 0
    store.close()
