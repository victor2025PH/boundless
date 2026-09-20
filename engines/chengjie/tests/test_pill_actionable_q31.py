# -*- coding: utf-8 -*-
"""Q-31 D（#317 PUUWJB）：顶栏「发前确认」只计可行动稿。

门禁：超龄稿不计 / 已回过不计 / 新鲜稿计 / auto_expire 默认值与 levels /
边车推送体含 backfill 字段（node --test）/ 批量清空只动超龄。
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.drafts import DraftService
from src.inbox.models import InboxMessage
from src.inbox.sla_watcher import SLAWatcher
from src.inbox.store import InboxStore
from src.web.routes.drafts_routes import register_drafts_routes

ENGINE = Path(__file__).resolve().parents[1]
_TPL_BASE = ENGINE / "src" / "web" / "templates" / "workspace_base.html"
_TPL_REVIEW = ENGINE / "src" / "web" / "templates" / "draft_review.html"
_SIDECAR = ENGINE / "services" / "messenger-web" / "server.js"
_SIDECAR_TEST = ENGINE / "services" / "messenger-web" / "backfill_flag.test.js"


def _wire(svc: DraftService, stale_h: float = 24.0) -> DraftService:
    async def _cb(_row):
        return {"ok": True}

    svc.set_inbox_deliver_callback(_cb, stale_approve_hours=stale_h)
    return svc


def _put_draft(store: InboxStore, *, sid: str, cid: str, age_h: float,
               level: str = "L1") -> str:
    created = time.time() - age_h * 3600.0
    store.upsert_draft({
        "source_kind": "inbox",
        "source_id": sid,
        "draft_id": f"inbox:{sid}",
        "conversation_id": cid,
        "platform": "messenger",
        "account_id": "acct",
        "chat_key": cid.rsplit(":", 1)[-1],
        "autopilot_level": level,
        "risk_level": "low",
        "status": "pending",
        "draft_text": "hey",
        "created_at": created,
    })
    return f"inbox:{sid}"


def _svc(store: InboxStore) -> DraftService:
    return _wire(DraftService(inbox_store=store, line_services=[], wa_services=[],
                              messenger_service=None))


def _client(svc: DraftService, role: str = "agent") -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request: Request, call_next):
        request.scope["session"] = {
            "role": role, "user_id": "skuio", "username": "skuio",
        }
        return await call_next(request)

    def api_auth(request: Request):
        return True

    register_drafts_routes(app, api_auth=api_auth)
    app.state.draft_service = svc
    return TestClient(app, raise_server_exceptions=True)


def test_stale_draft_not_in_actionable(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = _svc(store)
    _put_draft(store, sid="stale", cid="messenger:acct:c1", age_h=30.0)
    s = svc.risk_summary()
    assert s["by_level"]["L1"] == 1
    assert s["actionable"]["L1"] == 0
    assert s["stale_count"] == 1
    assert svc.approve_block_reason(svc.get_draft("inbox:stale") or {}) == "age"


def test_replied_draft_not_in_actionable(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = _svc(store)
    cid = "messenger:acct:c2"
    did = _put_draft(store, sid="replied", cid=cid, age_h=3.0)
    row = svc.get_draft(did) or {}
    created = float(row.get("created_at") or row.get("created_ts") or 0)
    store.ingest_message(InboxMessage(
        conversation_id=cid, platform_msg_id="out1", direction="out",
        text="already sent", ts=created + 30,
    ))
    s = svc.risk_summary()
    assert s["by_level"]["L1"] == 1
    assert s["actionable"]["L1"] == 0
    assert s["stale_count"] == 0
    assert svc.approve_block_reason(svc.get_draft(did) or {}) == "replied"


def test_fresh_draft_is_actionable(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = _svc(store)
    _put_draft(store, sid="fresh", cid="messenger:acct:c3", age_h=1.0)
    s = svc.risk_summary()
    assert s["by_level"]["L1"] == 1
    assert s["actionable"]["L1"] == 1
    assert s["stale_count"] == 0
    assert svc.approve_block_reason(svc.get_draft("inbox:fresh") or {}) == ""


def test_auto_expire_factory_default_48h_l1_only():
    w = SLAWatcher(draft_service=None, inbox_store=None)
    assert w._auto_expire_hours == 48.0
    assert w._auto_expire_levels == ["L1"]
    w2 = SLAWatcher(draft_service=None, inbox_store=None, config={})
    assert w2._auto_expire_hours == 48.0
    assert w2._auto_expire_levels == ["L1"]
    src = (ENGINE / "src" / "inbox" / "sla_watcher.py").read_text(encoding="utf-8")
    assert "_DEFAULT_AUTO_EXPIRE_HOURS: float = 48.0" in src
    assert '_DEFAULT_AUTO_EXPIRE_LEVELS: List[str] = ["L1"]' in src


def test_sidecar_backfill_body_has_flag_node_test():
    src = _SIDECAR.read_text(encoding="utf-8")
    i = src.index("async function backfillStep")
    chunk = src[i:i + 2800]
    assert "backfill: true" in chunk or "backfill:true" in chunk
    assert 'backfill_source: "msg_backfill"' in chunk or "backfill_source: 'msg_backfill'" in chunk
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    r = subprocess.run(
        [node, "--test", str(_SIDECAR_TEST)],
        cwd=str(_SIDECAR_TEST.parent), capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_bulk_clear_only_touches_stale(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = _svc(store)
    stale_id = _put_draft(store, sid="old", cid="messenger:acct:c4", age_h=40.0)
    fresh_id = _put_draft(store, sid="new", cid="messenger:acct:c5", age_h=1.0)
    replied_id = _put_draft(store, sid="rep", cid="messenger:acct:c6", age_h=3.0)
    row = svc.get_draft(replied_id) or {}
    created = float(row.get("created_at") or row.get("created_ts") or 0)
    store.ingest_message(InboxMessage(
        conversation_id="messenger:acct:c6", platform_msg_id="out2",
        direction="out", text="done", ts=created + 20,
    ))
    c = _client(svc, role="agent")
    r = c.post("/api/drafts/bulk-resolve",
               json={"action": "reject", "reason": "bulk_stale",
                     "draft_ids": [stale_id, fresh_id, replied_id]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["succeeded"] == 1
    assert store.get_draft(stale_id)["status"] == "rejected"
    assert store.get_draft(fresh_id)["status"] == "pending"
    assert store.get_draft(replied_id)["status"] == "pending"
    audits = store.list_draft_audit(draft_id=stale_id) if hasattr(
        store, "list_draft_audit") else []
    if audits:
        assert any(a.get("reason") == "bulk_stale" for a in audits)


def test_risk_summary_route_exposes_actionable(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = _svc(store)
    _put_draft(store, sid="a", cid="messenger:acct:c7", age_h=1.0)
    _put_draft(store, sid="b", cid="messenger:acct:c8", age_h=36.0)
    j = _client(svc).get("/api/drafts/risk-summary").json()
    assert j["ok"] is True
    assert j["actionable"]["L1"] == 1
    assert j["by_level"]["L1"] == 2
    assert j["stale_count"] == 1


def test_refresh_l4_reads_actionable_and_hides_on_zero():
    src = _TPL_BASE.read_text(encoding="utf-8")
    i = src.index("function refreshL4(")
    chunk = src[i:i + 1800]
    assert "d.actionable" in chunk
    assert "_setPill(_l4Badge, '')" in chunk or '_setPill(_l4Badge, "")' in chunk
    assert "base.pill.l4_clear_stale" in chunk
    drafts = (ENGINE / "src" / "inbox" / "drafts.py").read_text(encoding="utf-8")
    assert "self.approve_block_reason(d)" in drafts
    review = _TPL_REVIEW.read_text(encoding="utf-8")
    assert "reason:'bulk_stale'" in review or 'reason:"bulk_stale"' in review
    assert "btn-clear-stale" in review
    assert "function clearStale(" in review
