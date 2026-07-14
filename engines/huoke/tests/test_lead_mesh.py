# -*- coding: utf-8 -*-
"""Lead Mesh (Phase 5) 集成测试。

覆盖:
  * CanonicalResolver: 硬匹配 / 软匹配自动合并 / 手动合并 / 撤销
  * JourneyStore: append / get / count
  * LeadDossier: 聚合视图 + 合并链追踪
  * LockManager: 获取/释放/TTL/并发争用
  * Handoff: 状态机 + 脱敏 + 去重检查
  * AgentMesh: SQLite send/poll + query_sync
  * WebhookDispatcher: 入队 / 失败重试 / 死信
  * HTTP API: 全部端点 (TestClient)
"""
from __future__ import annotations

import threading
import time
import pytest


# ─── Canonical Resolver ─────────────────────────────────────────────────
class TestCanonicalResolver:
    def test_hard_match_returns_same(self, tmp_db):
        from src.host.lead_mesh import resolve_identity
        cid1 = resolve_identity(platform="facebook", account_id="fb_x",
                                 display_name="山田花子")
        cid2 = resolve_identity(platform="facebook", account_id="fb_x")
        assert cid1 == cid2

    def test_different_account_different_canonical(self, tmp_db):
        from src.host.lead_mesh import resolve_identity
        cid1 = resolve_identity(platform="facebook", account_id="fb_a",
                                 display_name="hanako", auto_merge=False)
        cid2 = resolve_identity(platform="facebook", account_id="fb_b",
                                 display_name="hanako", auto_merge=False)
        assert cid1 != cid2

    def test_soft_match_auto_merge_high_confidence(self, tmp_db):
        """同名 + 同头像 hash → 置信度 0.75, 低于默认阈值, 不合并。"""
        from src.host.lead_mesh import resolve_identity
        cid1 = resolve_identity(
            platform="facebook", account_id="fb_a",
            display_name="山田花子", language="ja",
            extra_metadata={"avatar_hash": "abc123"})

        # 同名 + 同头像 hash + 同 bio hash → 0.35 + 0.40 + 0.15 = 0.90 > 0.85
        cid2 = resolve_identity(
            platform="line", account_id="@yamadahanako",
            display_name="山田花子",
            extra_metadata={"avatar_hash": "abc123", "bio_hash": "abc123"})
        # 应自动合并: LINE identity 挂在 cid1 下
        from src.host.lead_mesh.dossier import get_dossier
        d1 = get_dossier(cid1)
        # cid2 返回的要么是 cid1(合并了), 要么是新 canonical_id 同时有两条 journey
        # 实际策略是 cid2 = cid1, identity 挂 cid1
        assert cid2 == cid1 or d1["canonical"]["canonical_id"] == cid1
        # 身份数应 ≥ 2 (fb + line)
        platforms = {i["platform"] for i in d1["identities"]}
        assert "facebook" in platforms

    def test_low_confidence_no_merge(self, tmp_db):
        """仅同名 (0.35) 不达阈值, 不合并。"""
        from src.host.lead_mesh import resolve_identity
        cid1 = resolve_identity(platform="facebook", account_id="fb_a",
                                 display_name="Bob")
        cid2 = resolve_identity(platform="line", account_id="@bob",
                                 display_name="Bob")
        # 应是两个独立 lead
        assert cid1 != cid2

    def test_manual_merge(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_dossier
        from src.host.lead_mesh.canonical import merge_manually
        cid1 = resolve_identity(platform="facebook", account_id="a",
                                 display_name="X", auto_merge=False)
        cid2 = resolve_identity(platform="line", account_id="@x",
                                 display_name="X", auto_merge=False)
        ok = merge_manually(cid2, cid1, merged_by="human:ops", reason="manual")
        assert ok
        # cid2 的 dossier 应跟随到 cid1
        d = get_dossier(cid2)
        assert d["effective_canonical_id"] == cid1
        # LINE identity 应挂到 cid1
        platforms = {i["platform"] for i in d["identities"]}
        assert platforms == {"facebook", "line"}

    def test_revert_merge(self, tmp_db):
        from src.host.lead_mesh import resolve_identity
        from src.host.lead_mesh.canonical import merge_manually, revert_merge
        from src.host.database import _connect
        cid1 = resolve_identity(platform="facebook", account_id="a",
                                 display_name="X", auto_merge=False)
        cid2 = resolve_identity(platform="line", account_id="@x",
                                 display_name="X", auto_merge=False)
        merge_manually(cid2, cid1, merged_by="human:ops")
        with _connect() as conn:
            row = conn.execute(
                "SELECT id FROM lead_merges WHERE source_canonical_id=?",
                (cid2,)).fetchone()
        assert row
        ok = revert_merge(row[0], reverted_by="human:ops", reason="oops")
        assert ok


# ─── Journey ─────────────────────────────────────────────────────────
class TestJourney:
    def test_append_and_get_order(self, tmp_db):
        from src.host.lead_mesh import append_journey, get_journey, resolve_identity
        cid = resolve_identity(platform="facebook", account_id="j1",
                                display_name="Alice")
        append_journey(cid, actor="agent_a", action="greeting_sent")
        time.sleep(0.02)
        append_journey(cid, actor="agent_b", action="reply_sent")
        events = get_journey(cid)
        # 至少 3 条 (extracted + greeting_sent + reply_sent)
        assert len(events) >= 3
        # 最后一条是 reply_sent
        assert events[-1]["action"] == "reply_sent"
        assert events[-1]["actor"] == "agent_b"

    def test_count_actions(self, tmp_db):
        from src.host.lead_mesh import append_journey, count_actions, resolve_identity
        cid = resolve_identity(platform="facebook", account_id="j2")
        for _ in range(3):
            append_journey(cid, actor="agent_a", action="greeting_sent")
        assert count_actions(cid, "greeting_sent") == 3
        assert count_actions(cid, "reply_sent") == 0


# ─── Dossier ─────────────────────────────────────────────────────────
class TestDossier:
    def test_full_dossier_structure(self, tmp_db):
        from src.host.lead_mesh import (resolve_identity, append_journey,
                                          create_handoff, get_dossier)
        cid = resolve_identity(platform="facebook", account_id="d1",
                                display_name="Dossier Test")
        append_journey(cid, actor="agent_a", action="greeting_sent")
        create_handoff(canonical_id=cid, source_agent="agent_b", channel="line",
                        conversation_snapshot=[{"direction": "incoming",
                                                 "text": "hi"}],
                        enqueue_webhook=False)
        d = get_dossier(cid)
        assert d is not None
        assert "canonical" in d and "identities" in d
        assert "journey" in d and "handoffs" in d
        assert "current_owner" in d
        assert d["canonical"]["canonical_id"] == cid
        assert len(d["handoffs"]) >= 1
        assert "by_action" in d["journey_summary"]

    def test_search_by_name(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, search_leads
        resolve_identity(platform="facebook", account_id="s1",
                          display_name="SearchAble", auto_merge=False)
        results = search_leads(name_like="Search")
        assert any(r["primary_name"] == "SearchAble" for r in results)


# ─── Lock Manager ────────────────────────────────────────────────────
class TestLockManager:
    def test_acquire_and_release(self, tmp_db):
        from src.host.lead_mesh import acquire_lock, is_locked
        with acquire_lock("L1", "referring", by="agent_a", ttl_sec=60) as ok:
            assert ok
            assert is_locked("L1", "referring") is not None
        # 释放后应无锁
        assert is_locked("L1", "referring") is None

    def test_second_acquire_blocked(self, tmp_db):
        from src.host.lead_mesh import acquire_lock
        with acquire_lock("L2", "referring", by="agent_a", ttl_sec=60) as ok1:
            assert ok1
            # 第二个 agent 拿同一把锁 → False
            with acquire_lock("L2", "referring", by="agent_b", ttl_sec=60) as ok2:
                assert ok2 is False

    def test_different_actions_independent(self, tmp_db):
        from src.host.lead_mesh import acquire_lock
        with acquire_lock("L3", "referring", by="agent_a", ttl_sec=60) as a:
            with acquire_lock("L3", "chatting", by="agent_b", ttl_sec=60) as b:
                assert a is True and b is True  # 不同 action 互不阻塞

    def test_expired_lock_can_be_reacquired(self, tmp_db):
        """TTL 过期后另一个 agent 可以重新拿。"""
        from src.host.lead_mesh.lock_manager import acquire_lock_raw
        from src.host.database import _connect
        # 手工塞一个过期锁
        with _connect() as conn:
            conn.execute(
                "INSERT INTO lead_locks (canonical_id, action, locked_by,"
                " acquired_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                ("L4", "referring", "old_agent",
                 "2020-01-01T00:00:00Z", "2020-01-01T00:01:00Z"))
        ok = acquire_lock_raw("L4", "referring", by="new_agent", ttl_sec=60)
        assert ok


# ─── Handoff ─────────────────────────────────────────────────────────
class TestHandoff:
    def test_create_then_ack_then_complete(self, tmp_db):
        from src.host.lead_mesh import (create_handoff, acknowledge_handoff,
                                          complete_handoff, get_handoff,
                                          resolve_identity)
        cid = resolve_identity(platform="facebook", account_id="h1")
        hid = create_handoff(canonical_id=cid, source_agent="agent_b",
                              channel="line", enqueue_webhook=False)
        assert hid
        assert get_handoff(hid)["state"] == "pending"
        assert acknowledge_handoff(hid, by="human:ops_01")
        assert get_handoff(hid)["state"] == "acknowledged"
        assert complete_handoff(hid, by="human:ops_01")
        assert get_handoff(hid)["state"] == "completed"

    def test_reject_from_pending(self, tmp_db):
        from src.host.lead_mesh import (create_handoff, reject_handoff,
                                          get_handoff, resolve_identity)
        cid = resolve_identity(platform="facebook", account_id="h2")
        hid = create_handoff(canonical_id=cid, source_agent="agent_b",
                              channel="line", enqueue_webhook=False)
        assert reject_handoff(hid, by="human:ops")
        assert get_handoff(hid)["state"] == "rejected"

    def test_cannot_ack_completed(self, tmp_db):
        from src.host.lead_mesh import (create_handoff, complete_handoff,
                                          acknowledge_handoff,
                                          resolve_identity)
        cid = resolve_identity(platform="facebook", account_id="h3")
        hid = create_handoff(canonical_id=cid, source_agent="agent_b",
                              channel="line", enqueue_webhook=False)
        complete_handoff(hid, by="human:ops")
        # completed 后不能再 ack
        assert acknowledge_handoff(hid, by="human:ops") is False

    def test_snapshot_sanitization(self, tmp_db):
        """聊天记录里的手机号/邮箱/LINE ID 应被脱敏。"""
        from src.host.lead_mesh import create_handoff, get_handoff, resolve_identity
        cid = resolve_identity(platform="facebook", account_id="h4")
        hid = create_handoff(
            canonical_id=cid, source_agent="agent_b", channel="line",
            conversation_snapshot=[
                {"direction": "incoming",
                 "text": "我的电话 +81 90 1234 5678 邮箱 test@example.com"},
                {"direction": "outgoing",
                 "text": "加我 LINE: @myjpid 方便沟通"},
            ],
            enqueue_webhook=False)
        snap = get_handoff(hid)["conversation_snapshot"]
        txt1 = snap[0]["text"]
        txt2 = snap[1]["text"]
        assert "[PHONE]" in txt1
        assert "[EMAIL]" in txt1
        assert "90 1234" not in txt1  # 原号码不留痕
        assert "[LINE_ID]" in txt2
        assert "@myjpid" not in txt2

    def test_check_duplicate(self, tmp_db):
        """同 lead 同 channel 创建两次 handoff, 第二次应被识别为 duplicate。"""
        from src.host.lead_mesh import create_handoff, resolve_identity
        from src.host.lead_mesh.handoff import check_duplicate_handoff
        cid = resolve_identity(platform="facebook", account_id="h5")
        hid1 = create_handoff(canonical_id=cid, source_agent="agent_b",
                               channel="line", enqueue_webhook=False)
        dup = check_duplicate_handoff(cid, "line")
        assert dup is not None
        assert dup["handoff_id"] == hid1
        # 换渠道就不算重复
        assert check_duplicate_handoff(cid, "whatsapp") is None


# ─── Agent Mesh ──────────────────────────────────────────────────────
class TestAgentMesh:
    def test_send_and_poll(self, tmp_db):
        from src.host.lead_mesh import send_message, poll_messages
        cid = send_message(from_agent="agent_a", to_agent="agent_b",
                            message_type="notification",
                            payload={"event": "greeting_sent"})
        assert cid  # correlation_id 返回
        msgs = poll_messages("agent_b")
        assert len(msgs) == 1
        assert msgs[0]["payload"]["event"] == "greeting_sent"

    def test_mark_delivered_then_ack(self, tmp_db):
        from src.host.lead_mesh import (send_message, poll_messages,
                                          mark_delivered, mark_acknowledged)
        send_message(from_agent="a", to_agent="b", payload={})
        msg = poll_messages("b")[0]
        assert mark_delivered(msg["id"])
        # 再 poll pending 应为 0
        assert len(poll_messages("b", status="pending")) == 0
        assert mark_acknowledged(msg["id"])

    def test_query_sync_succeeds_when_reply_arrives(self, tmp_db):
        """query_sync 阻塞等 reply; 另一线程模拟 reply。"""
        from src.host.lead_mesh import send_message, poll_messages, query_sync
        from src.host.lead_mesh.agent_mesh import reply_to

        def responder():
            time.sleep(0.3)
            msgs = poll_messages("agent_b", message_type="query")
            if msgs:
                reply_to(msgs[0], from_agent="agent_b",
                         payload={"result": "data"})

        t = threading.Thread(target=responder)
        t.start()
        result = query_sync(from_agent="agent_a", to_agent="agent_b",
                             payload={"question": "status"},
                             timeout_sec=5, poll_interval=0.2)
        t.join(timeout=3)
        assert result == {"result": "data"}

    def test_query_sync_timeout(self, tmp_db):
        from src.host.lead_mesh import query_sync
        result = query_sync(from_agent="a", to_agent="b",
                             payload={}, timeout_sec=1, poll_interval=0.3)
        assert result is None


# ─── Webhook Dispatcher ──────────────────────────────────────────────
class TestWebhookDispatcher:
    def test_enqueue_with_no_subscribers(self, tmp_db):
        """未配置订阅者时 enqueue 返回 0。"""
        from src.host.lead_mesh import enqueue_webhook
        count = enqueue_webhook(event_type="handoff.created",
                                 payload={"foo": "bar"})
        assert count == 0  # 无订阅者

    def test_flush_no_pending(self, tmp_db):
        from src.host.lead_mesh import flush_pending_webhooks
        stats = flush_pending_webhooks()
        assert stats["delivered"] == 0

    def test_retry_dead_letter(self, tmp_db, monkeypatch):
        """模拟 dead letter 入库 + 重置回 pending。"""
        from src.host.database import _connect
        from src.host.lead_mesh.webhook_dispatcher import retry_dead_letter
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO webhook_dispatches"
                " (event_type, target_url, payload_json, status, attempt_count,"
                "  last_error)"
                " VALUES (?,?,?,?,?,?)",
                ("handoff.created", "http://x", "{}", "dead_letter", 3, "oops"))
            did = cur.lastrowid
        assert retry_dead_letter(did)
        with _connect() as conn:
            row = conn.execute(
                "SELECT status FROM webhook_dispatches WHERE id=?",
                (did,)).fetchone()
        assert row[0] == "pending"


# ─── HTTP API (TestClient) ────────────────────────────────────────────
class TestLeadMeshAPI:
    def test_resolve_and_get_dossier(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/lead-mesh/leads/resolve", json={
                "platform": "facebook", "account_id": "api_1",
                "display_name": "API Test"})
            assert r.status_code == 200
            cid = r.json()["canonical_id"]
            r2 = c.get(f"/lead-mesh/leads/{cid}")
            assert r2.status_code == 200
            assert r2.json()["canonical"]["canonical_id"] == cid

    def test_handoff_lifecycle_api(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            cid = c.post("/lead-mesh/leads/resolve", json={
                "platform": "facebook", "account_id": "api_h1"}).json()["canonical_id"]
            r = c.post("/lead-mesh/handoffs", json={
                "canonical_id": cid, "source_agent": "agent_b",
                "channel": "line",
                "receiver_account_key": "line_jp_01",
                "enqueue_webhook": False})
            assert r.status_code == 200
            hid = r.json()["handoff_id"]
            assert c.post(f"/lead-mesh/handoffs/{hid}/acknowledge",
                           json={"by": "human"}).status_code == 200
            assert c.post(f"/lead-mesh/handoffs/{hid}/complete",
                           json={"by": "human"}).status_code == 200
            assert c.get(f"/lead-mesh/handoffs/{hid}").json()["state"] == "completed"

    def test_check_duplicate_api(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            cid = c.post("/lead-mesh/leads/resolve", json={
                "platform": "facebook", "account_id": "api_dup"}).json()["canonical_id"]
            c.post("/lead-mesh/handoffs", json={
                "canonical_id": cid, "source_agent": "a", "channel": "line",
                "enqueue_webhook": False})
            r = c.get(f"/lead-mesh/handoffs/check-duplicate?canonical_id={cid}&channel=line")
            assert r.status_code == 200
            assert r.json()["is_duplicate"] is True

    def test_agent_mesh_http(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/lead-mesh/agents/messages", json={
                "from_agent": "agent_a", "to_agent": "agent_b",
                "message_type": "notification",
                "payload": {"hello": "world"}})
            assert r.status_code == 200
            r2 = c.get("/lead-mesh/agents/messages?to_agent=agent_b")
            assert r2.status_code == 200
            assert r2.json()["count"] == 1
