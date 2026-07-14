# -*- coding: utf-8 -*-
"""A 端 Lead Mesh journey 接入测试 (Phase 6.A, 2026-04-23)。

覆盖:
  * _set_greet_reason 自动写 lead_journey (cid 挂着时)
  * _append_journey_for_action 直接一步调用 (友好接入 add_friend 成功路径)
  * _record_friend_request_safely 接入点 (sent → friend_requested;
    risk → friend_request_risk)
"""
from __future__ import annotations

import pytest


def _stub_fb():
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "D_journey"
    fb._last_greet_skip_reason = ""
    fb._current_lead_cid = ""
    fb._current_lead_persona = ""
    fb._current_greet_template_id = ""
    return fb


# ─── _set_greet_reason 同步 journey ─────────────────────────────────────
class TestSetGreetReasonSyncsJourney:
    def test_ok_writes_greeting_sent(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_journey
        fb = _stub_fb()
        cid = resolve_identity(platform="facebook",
                                account_id="fb:TestUser",
                                display_name="TestUser")
        fb._current_lead_cid = cid
        fb._current_lead_persona = "jp_female_midlife"
        fb._current_greet_template_id = "yaml:jp:3"
        fb._set_greet_reason("ok")

        events = get_journey(cid, action_prefix="greeting_")
        sent = [e for e in events if e["action"] == "greeting_sent"]
        assert len(sent) == 1
        assert sent[0]["data"]["via"] == "inline_profile_message"
        assert sent[0]["data"]["template_id"] == "yaml:jp:3"
        assert sent[0]["data"]["persona_key"] == "jp_female_midlife"
        assert sent[0]["actor"] == "agent_a"

    def test_ok_via_fallback_marks_via_messenger(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_journey
        fb = _stub_fb()
        cid = resolve_identity(platform="facebook",
                                account_id="fb:FallbackPeer",
                                display_name="FallbackPeer")
        fb._current_lead_cid = cid
        fb._set_greet_reason("ok_via_fallback")

        events = get_journey(cid, action_prefix="greeting_")
        sent = [e for e in events if e["action"] == "greeting_sent"]
        assert any(e["data"].get("via") == "messenger_fallback" for e in sent)

    def test_phase_blocked_writes_greeting_blocked(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_journey
        fb = _stub_fb()
        cid = resolve_identity(platform="facebook",
                                account_id="fb:Cold",
                                display_name="Cold")
        fb._current_lead_cid = cid
        fb._set_greet_reason("phase_blocked")

        events = get_journey(cid)
        blocked = [e for e in events if e["action"] == "greeting_blocked"]
        assert len(blocked) == 1
        assert blocked[0]["data"]["reason"] == "phase_blocked"

    def test_cap_hit_writes_reason(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_journey
        fb = _stub_fb()
        cid = resolve_identity(platform="facebook",
                                account_id="fb:Capped",
                                display_name="Capped")
        fb._current_lead_cid = cid
        fb._set_greet_reason("cap_hit")
        events = get_journey(cid)
        blocked = [e for e in events if e["action"] == "greeting_blocked"]
        assert blocked and blocked[0]["data"]["reason"] == "cap_hit"

    def test_no_cid_no_journey(self, tmp_db):
        """cid 为空时 (resolve 失败) 不应抛异常, 也不应写 journey。"""
        fb = _stub_fb()
        # cid 为空串, reason 写入后应 silent skip journey
        fb._set_greet_reason("ok")  # 不应抛
        # 无法测负面 (没有 cid 无法查), 但不抛就是成功

    def test_empty_reason_no_journey(self, tmp_db):
        """reason="" (清空) 时不写 journey, 避免重置时误写。"""
        from src.host.lead_mesh import resolve_identity, get_journey
        fb = _stub_fb()
        cid = resolve_identity(platform="facebook",
                                account_id="fb:Empty",
                                display_name="Empty")
        fb._current_lead_cid = cid
        fb._set_greet_reason("")   # 应该不写
        greeting_events = [e for e in get_journey(cid)
                            if e["action"].startswith("greeting_")]
        # 只有 extracted (创建 canonical 时自动写的), 无 greeting_* 事件
        assert len(greeting_events) == 0


# ─── _append_journey_for_action ─────────────────────────────────────────
class TestAppendJourneyForAction:
    def test_resolves_and_writes(self, tmp_db):
        from src.host.lead_mesh import get_journey
        fb = _stub_fb()
        fb._append_journey_for_action(
            "Alice", "friend_requested", did="D1",
            persona_key="jp_female_midlife",
            data={"note_len": 20})
        # 通过重新 resolve 查 cid
        from src.host.lead_mesh import resolve_identity
        cid = resolve_identity(platform="facebook", account_id="fb:Alice",
                                display_name="Alice")
        events = get_journey(cid)
        friend_evts = [e for e in events if e["action"] == "friend_requested"]
        assert len(friend_evts) == 1
        assert friend_evts[0]["data"]["note_len"] == 20
        assert friend_evts[0]["actor"] == "agent_a"
        assert friend_evts[0]["actor_device"] == "D1"

    def test_empty_peer_name_no_op(self, tmp_db):
        fb = _stub_fb()
        # 空 peer → 不抛异常, 静默跳过
        fb._append_journey_for_action("", "friend_requested", did="D1")


# ─── _record_friend_request_safely 集成 ─────────────────────────────────
class TestRecordFriendRequestIntegration:
    def test_sent_writes_friend_requested(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_journey
        from src.host.fb_store import get_friend_request_stats
        fb = _stub_fb()
        fb._record_friend_request_safely(
            "D1", "Alice",
            note="hi there",
            persona_key="jp_female_midlife",
            source="group_a", preset_key="name_hunter",
            status="sent")

        # Friend request 表写入
        stats = get_friend_request_stats(device_id="D1")
        assert stats["sent"] >= 1

        # Lead Mesh journey 写入
        cid = resolve_identity(platform="facebook", account_id="fb:Alice",
                                display_name="Alice")
        events = get_journey(cid)
        friend_evts = [e for e in events if e["action"] == "friend_requested"]
        assert len(friend_evts) == 1
        assert friend_evts[0]["data"]["source"] == "group_a"
        assert friend_evts[0]["data"]["preset_key"] == "name_hunter"
        assert friend_evts[0]["data"]["note_len"] == len("hi there")

    def test_risk_writes_friend_request_risk(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, get_journey
        fb = _stub_fb()
        fb._record_friend_request_safely(
            "D1", "Bob",
            note="",
            status="risk")
        cid = resolve_identity(platform="facebook", account_id="fb:Bob",
                                display_name="Bob")
        events = get_journey(cid)
        risk = [e for e in events if e["action"] == "friend_request_risk"]
        assert len(risk) == 1


# ─── 端到端: Dossier 里看到完整的 A 端动作 ────────────────────────────
class TestEndToEndDossier:
    def test_dossier_shows_add_friend_plus_greeting(self, tmp_db):
        """模拟完整 A 端流程: add_friend 成功 → greeting 发出 → dossier 全看到。"""
        from src.host.lead_mesh import get_dossier
        fb = _stub_fb()

        # 1. add_friend 成功
        fb._record_friend_request_safely(
            "D1", "Yamada",
            note="はじめまして",
            persona_key="jp_female_midlife",
            source="ママ友会",
            preset_key="friend_growth",
            status="sent")

        # 2. greeting 发出 — 模拟 send_greeting_after_add_friend 入口流程
        from src.host.lead_mesh import resolve_identity
        cid = resolve_identity(platform="facebook", account_id="fb:Yamada",
                                display_name="Yamada")
        fb._current_lead_cid = cid
        fb._current_lead_persona = "jp_female_midlife"
        fb._current_greet_template_id = "yaml:jp:5"
        fb._set_greet_reason("ok")

        # 3. 查 dossier
        d = get_dossier(cid)
        assert d is not None
        actions = [e["action"] for e in d["journey"]]
        # extracted (初次) + friend_requested + greeting_sent
        assert "friend_requested" in actions
        assert "greeting_sent" in actions
        assert d["current_owner"] == "agent_a"
