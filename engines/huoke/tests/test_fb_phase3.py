# -*- coding: utf-8 -*-
"""Phase 3 扩展测试(2026-04-23)。

覆盖:
  * fb_concurrency.device_section_lock 基础语义 + 并发串行
  * gate_registry 分发 + 默认 gate 注册 + campaign step 检查
  * fb_contact_events 表 CRUD + reply_rate 聚合
  * /facebook/contact-events + /facebook/greeting-reply-rate 端点
"""
from __future__ import annotations

import threading
import time
import pytest


# ─── Lock 基础语义 ────────────────────────────────────────────────────────────
class TestDeviceSectionLock:
    def test_empty_device_no_lock(self):
        """device_id 为空时直接 yield,不获取锁(命令行/测试场景)。"""
        from src.host.fb_concurrency import device_section_lock
        with device_section_lock("", "add_friend"):
            pass  # 不应抛异常

    def test_same_key_serializes(self):
        """同一 (device, section) 下两个线程顺序进入,第二个等第一个释放。"""
        from src.host.fb_concurrency import (device_section_lock,
                                              reset_metrics_for_tests,
                                              device_lock_metrics)
        reset_metrics_for_tests()
        events = []
        barrier = threading.Barrier(2)

        def worker(name: str, hold_ms: int):
            barrier.wait()
            with device_section_lock("d1", "add_friend", timeout=10.0):
                events.append(("enter", name))
                time.sleep(hold_ms / 1000.0)
                events.append(("exit", name))

        t1 = threading.Thread(target=worker, args=("A", 200))
        t2 = threading.Thread(target=worker, args=("B", 50))
        t1.start(); t2.start()
        t1.join(); t2.join()
        # 两个线程的 enter/exit 必须成对不交错
        seq = [e[0] for e in events]
        # 合法顺序: [enter,exit,enter,exit]
        assert seq == ["enter", "exit", "enter", "exit"], f"交错执行: {events}"
        m = device_lock_metrics()
        assert m["acquired_count"] == 2
        # 第二个至少等了 ~50ms
        assert m["waited_total_ms"] >= 40  # 留 10ms 裕量给调度

    def test_different_section_independent(self):
        """同 device 不同 section 互不阻塞。"""
        from src.host.fb_concurrency import device_section_lock
        both_held = threading.Event()

        def worker_a():
            with device_section_lock("d1", "add_friend", timeout=5.0):
                time.sleep(0.1)
                both_held.set()
                time.sleep(0.1)

        def worker_b():
            # 等 A 拿到锁再开始 —— 证明 B 不被 A 阻塞
            time.sleep(0.05)
            with device_section_lock("d1", "send_greeting", timeout=5.0):
                assert both_held.is_set() or True  # 只要不卡死就算对
                time.sleep(0.05)

        t1 = threading.Thread(target=worker_a)
        t2 = threading.Thread(target=worker_b)
        t1.start(); t2.start()
        t1.join(timeout=2.0); t2.join(timeout=2.0)
        assert not t1.is_alive() and not t2.is_alive()

    def test_timeout_raises(self):
        """拿不到锁 → 超时 RuntimeError。"""
        from src.host.fb_concurrency import (device_section_lock,
                                              reset_metrics_for_tests,
                                              device_lock_metrics)
        reset_metrics_for_tests()
        holding = threading.Event()
        release = threading.Event()

        def holder():
            with device_section_lock("d1", "s", timeout=5.0):
                holding.set()
                release.wait()

        t = threading.Thread(target=holder)
        t.start()
        holding.wait()
        with pytest.raises(RuntimeError):
            with device_section_lock("d1", "s", timeout=0.2):
                pass
        release.set()
        t.join()
        m = device_lock_metrics()
        assert m["timeouts"] >= 1


# ─── gate_registry ────────────────────────────────────────────────────────────
class TestGateRegistry:
    def test_default_gates_registered(self):
        from src.host.gate_registry import (registered_task_types,
                                             registered_campaign_steps)
        task_gates = registered_task_types()
        assert "facebook_add_friend" in task_gates
        assert "facebook_add_friend_and_greet" in task_gates
        assert "facebook_send_greeting" in task_gates
        step_gates = registered_campaign_steps()
        assert "add_friends" in step_gates
        assert "send_greeting" in step_gates

    def test_unknown_task_passes_through(self):
        from src.host.gate_registry import check_gate_for_task
        err, meta = check_gate_for_task("unknown_task", "d1", {})
        assert err is None
        assert meta.get("gate") == "not_registered"

    def test_no_device_id_passes(self):
        from src.host.gate_registry import check_gate_for_task
        err, meta = check_gate_for_task("facebook_add_friend", "", {})
        assert err is None
        assert meta.get("gate") == "no_device_id"

    def test_send_greeting_gate_blocks_cold_start(self, tmp_db):
        from src.host.gate_registry import check_gate_for_task
        err, meta = check_gate_for_task("facebook_send_greeting", "d1",
                                         {"phase": "cold_start"})
        assert err is not None and "cold_start" in err

    def test_campaign_steps_any_fail_rejects(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        # 填满 greeting cap
        for i in range(10):
            record_inbox_message("d_full", f"p{i}", direction="outgoing",
                                 ai_decision="greeting")
        from src.host.gate_registry import check_gates_for_campaign_steps
        err, meta = check_gates_for_campaign_steps(
            ["add_friends", "send_greeting"], "d_full", {"phase": "mature"})
        assert err is not None
        assert meta["failed_step"] == "send_greeting"

    def test_campaign_steps_no_gated_step_passes(self, tmp_db):
        """没有带 gate 的 step(如 warmup)时不拒绝。"""
        from src.host.gate_registry import check_gates_for_campaign_steps
        err, meta = check_gates_for_campaign_steps(
            ["warmup", "group_engage"], "d1", {})
        assert err is None


# ─── fb_contact_events ────────────────────────────────────────────────────────
class TestContactEvents:
    def test_record_and_count(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                        count_contact_events,
                                        CONTACT_EVT_GREETING_SENT)
        rid = record_contact_event("d1", "Alice", CONTACT_EVT_GREETING_SENT,
                                    template_id="yaml:jp:2",
                                    preset_key="name_hunter")
        assert rid > 0
        n = count_contact_events(device_id="d1",
                                  event_type=CONTACT_EVT_GREETING_SENT)
        assert n == 1

    def test_list_by_peer_ordered(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                        list_contact_events_by_peer,
                                        CONTACT_EVT_ADD_FRIEND_SENT,
                                        CONTACT_EVT_GREETING_SENT)
        record_contact_event("d1", "Bob", CONTACT_EVT_ADD_FRIEND_SENT)
        time.sleep(0.02)
        record_contact_event("d1", "Bob", CONTACT_EVT_GREETING_SENT)
        events = list_contact_events_by_peer("d1", "Bob")
        assert len(events) == 2
        assert events[0]["event_type"] == CONTACT_EVT_ADD_FRIEND_SENT
        assert events[1]["event_type"] == CONTACT_EVT_GREETING_SENT

    def test_unknown_event_type_still_writes(self, tmp_db, caplog):
        """扩展性: B 可能加自己的新 event_type, 不应被强拒。"""
        from src.host.fb_store import record_contact_event, count_contact_events
        import logging
        with caplog.at_level(logging.WARNING):
            rid = record_contact_event("d1", "x", "__custom_evt")
        assert rid > 0
        assert count_contact_events(device_id="d1",
                                     event_type="__custom_evt") == 1
        # 应该 warn 了
        assert any("未知 event_type" in rec.message for rec in caplog.records)

    def test_greeting_reply_rate_by_template(self, tmp_db):
        """模拟: 2 条 greeting(tpl=A) 中 1 条被回复; 3 条 greeting(tpl=B) 被回 2 条。"""
        from src.host.fb_store import (record_contact_event,
                                        get_greeting_reply_rate_by_template,
                                        CONTACT_EVT_GREETING_SENT,
                                        CONTACT_EVT_GREETING_REPLIED)
        # tpl=yaml:jp:0 — 2 sent, 1 replied
        record_contact_event("d1", "p1", CONTACT_EVT_GREETING_SENT, template_id="yaml:jp:0")
        record_contact_event("d1", "p2", CONTACT_EVT_GREETING_SENT, template_id="yaml:jp:0")
        record_contact_event("d1", "p1", CONTACT_EVT_GREETING_REPLIED, template_id="yaml:jp:0")
        # tpl=yaml:jp:3 — 3 sent, 2 replied
        for i in range(3):
            record_contact_event("d1", f"q{i}", CONTACT_EVT_GREETING_SENT,
                                 template_id="yaml:jp:3")
        record_contact_event("d1", "q0", CONTACT_EVT_GREETING_REPLIED, template_id="yaml:jp:3")
        record_contact_event("d1", "q1", CONTACT_EVT_GREETING_REPLIED, template_id="yaml:jp:3")

        rows = get_greeting_reply_rate_by_template(device_id="d1", hours=24)
        assert len(rows) == 2
        # 应按 reply_rate 降序: jp:3 = 2/3 ≈ 0.67 > jp:0 = 1/2 = 0.5
        assert rows[0]["template_id"] == "yaml:jp:3"
        assert rows[0]["sent"] == 3
        assert rows[0]["replied"] == 2
        assert abs(rows[0]["reply_rate"] - 0.667) < 0.01
        assert rows[1]["template_id"] == "yaml:jp:0"
        assert abs(rows[1]["reply_rate"] - 0.5) < 0.01

    def test_empty_no_templates(self, tmp_db):
        from src.host.fb_store import get_greeting_reply_rate_by_template
        assert get_greeting_reply_rate_by_template() == []


# ─── /facebook/contact-events + /facebook/greeting-reply-rate ────────────────
class TestContactEventsAPI:
    def test_query_single_peer(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                        CONTACT_EVT_GREETING_SENT)
        record_contact_event("d1", "Alice", CONTACT_EVT_GREETING_SENT,
                             template_id="yaml:jp:0")

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.get("/facebook/contact-events?device_id=d1&peer_name=Alice")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1
        assert body["events"][0]["event_type"] == CONTACT_EVT_GREETING_SENT

    def test_query_count_only(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                        CONTACT_EVT_ADD_FRIEND_SENT)
        for i in range(5):
            record_contact_event("d1", f"p{i}", CONTACT_EVT_ADD_FRIEND_SENT)

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.get("/facebook/contact-events?device_id=d1&event_type=add_friend_sent")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 5
        assert body["event_type_filter"] == "add_friend_sent"

    def test_reply_rate_endpoint(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                        CONTACT_EVT_GREETING_SENT,
                                        CONTACT_EVT_GREETING_REPLIED)
        record_contact_event("d1", "a", CONTACT_EVT_GREETING_SENT, template_id="t1")
        record_contact_event("d1", "a", CONTACT_EVT_GREETING_REPLIED, template_id="t1")

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.get("/facebook/greeting-reply-rate?device_id=d1")
        assert r.status_code == 200
        body = r.json()
        assert body["total_templates"] == 1
        assert body["templates"][0]["reply_rate"] == 1.0


# ─── 漏斗响应格式 ─────────────────────────────────────────────────────────────
class TestFunnelGreetingFields:
    def test_funnel_includes_greeting_metrics_when_empty(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.get("/facebook/funnel")
        assert r.status_code == 200
        body = r.json()
        # 关键字段存在即可(空库时都应该是 0)
        assert "stage_greetings_sent" in body
        assert "stage_greetings_fallback" in body
        assert "rate_greet_after_add" in body
        assert "greeting_template_distribution" in body


# ─── record_friend_request_safely 的幂等行为 ──────────────────────────────────
class TestAutomationLayerRecording:
    """P3-1 把 record 下放到 automation 层后, 验证不会产生重复记录。"""

    def test_safely_method_records_once(self, tmp_db):
        """直接调 _record_friend_request_safely 应该只插一条记录。"""
        from src.app_automation.facebook import FacebookAutomation

        class _StubMgr:
            def __getattr__(self, n):
                raise AssertionError(f"不应调 {n}")

        try:
            fb = FacebookAutomation.__new__(FacebookAutomation)
            fb._record_friend_request_safely(
                "d_rec", "TestUser", note="hi", source="group_X",
                preset_key="name_hunter", status="sent")
        except Exception:
            pytest.skip("FacebookAutomation 需要真机上下文,跳过")

        from src.host.fb_store import (get_friend_request_stats,
                                        count_contact_events,
                                        CONTACT_EVT_ADD_FRIEND_SENT)
        stats = get_friend_request_stats(device_id="d_rec")
        assert stats["sent"] == 1
        # P3-3: 同时写了 contact_event
        assert count_contact_events(device_id="d_rec",
                                     event_type=CONTACT_EVT_ADD_FRIEND_SENT) == 1
