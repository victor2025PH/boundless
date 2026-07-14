# -*- coding: utf-8 -*-
"""Phase 8 funnel_report 单测 (2026-04-24).

构造 fake journey 数据 → 调 compute_funnel → 断言聚合结果正确.
"""
from __future__ import annotations

import pytest


class TestFunnelAggregation:
    def test_empty_db_returns_zeros(self, tmp_db):
        from src.host.lead_mesh.funnel_report import compute_funnel
        stats = compute_funnel(days=7)
        assert stats.total_friend_requested == 0
        assert stats.total_greeting_sent == 0
        assert stats.rate_greet_after_friend == 0.0
        assert stats.top_blocked_reason == ""

    def test_basic_funnel(self, tmp_db):
        """3 个 lead: 全都 extract, 2 个 friend_requested, 1 个 greeting_sent(inline).
        预期: extracted=3 / friend_req=2 / greeting_sent=1 / rate=50%."""
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel

        # 3 人 (resolve_identity 自动 append extracted 事件)
        cid_a = resolve_identity(platform="facebook",
                                   account_id="fb:Alice", display_name="Alice")
        cid_b = resolve_identity(platform="facebook",
                                   account_id="fb:Bob", display_name="Bob")
        cid_c = resolve_identity(platform="facebook",
                                   account_id="fb:Carol", display_name="Carol")

        # A 和 B 加了好友
        append_journey(cid_a, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp_female_midlife"})
        append_journey(cid_b, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp_female_midlife"})
        # 只 A 打了招呼 (inline)
        append_journey(cid_a, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message",
                               "template_id": "yaml:jp:0"})

        stats = compute_funnel(days=7)
        assert stats.total_extracted == 3
        assert stats.total_friend_requested == 2
        assert stats.total_greeting_sent == 1
        assert stats.greeting_via_inline == 1
        assert stats.greeting_via_fallback == 0
        # 转化率 = 1/2
        assert abs(stats.rate_greet_after_friend - 0.5) < 1e-6
        # 100% inline
        assert abs(stats.rate_inline_vs_fallback - 1.0) < 1e-6

    def test_via_distribution(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:V", display_name="V")
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message"})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "messenger_fallback"})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": ""})  # unknown

        stats = compute_funnel(days=7)
        assert stats.greeting_via_inline == 1
        assert stats.greeting_via_fallback == 1
        assert stats.greeting_via_unknown == 1
        # rate_inline = 1/3
        assert abs(stats.rate_inline_vs_fallback - 1.0 / 3) < 1e-6

    def test_top_blocked_reason(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:B", display_name="B")
        # 5 个 no_message_button, 2 个 phase_blocked, 1 个 template_empty
        for _ in range(5):
            append_journey(cid, actor="agent_a", action="greeting_blocked",
                             data={"reason": "no_message_button"})
        for _ in range(2):
            append_journey(cid, actor="agent_a", action="greeting_blocked",
                             data={"reason": "phase_blocked"})
        append_journey(cid, actor="agent_a", action="greeting_blocked",
                         data={"reason": "template_empty"})

        stats = compute_funnel(days=7)
        assert stats.total_greeting_blocked == 8
        assert stats.blocked_reasons["no_message_button"] == 5
        assert stats.blocked_reasons["phase_blocked"] == 2
        assert stats.blocked_reasons["template_empty"] == 1
        assert stats.top_blocked_reason == "no_message_button"

    def test_per_persona_grouping(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel

        cid1 = resolve_identity(platform="facebook",
                                  account_id="fb:P1", display_name="P1")
        cid2 = resolve_identity(platform="facebook",
                                  account_id="fb:P2", display_name="P2")
        cid3 = resolve_identity(platform="facebook",
                                  account_id="fb:P3", display_name="P3")

        for cid in (cid1, cid2, cid3):
            append_journey(cid, actor="agent_a", action="friend_requested",
                             data={"persona_key": "jp_female_midlife"})
        # 再加 1 个其他 persona
        cid4 = resolve_identity(platform="facebook",
                                  account_id="fb:P4", display_name="P4")
        append_journey(cid4, actor="agent_a", action="friend_requested",
                         data={"persona_key": "tw_male_young"})

        stats = compute_funnel(days=7)
        assert stats.per_persona_friend_requested["jp_female_midlife"] == 3
        assert stats.per_persona_friend_requested["tw_male_young"] == 1

    def test_actor_filter(self, tmp_db):
        """actor=agent_a 过滤: 不看 B 机事件."""
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:F", display_name="F")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp"})
        append_journey(cid, actor="agent_b", action="friend_requested",
                         data={"persona_key": "jp"})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message"})

        stats_all = compute_funnel(days=7)
        assert stats_all.total_friend_requested == 2

        stats_a = compute_funnel(days=7, actor="agent_a")
        assert stats_a.total_friend_requested == 1
        assert stats_a.total_greeting_sent == 1


class TestBlockedPeers:
    """list_blocked_peers 按 reason 过滤 + 去重聚合 + 按最近倒序."""

    def test_empty_returns_empty(self, tmp_db):
        from src.host.lead_mesh.funnel_report import list_blocked_peers
        assert list_blocked_peers("no_message_button", days=7) == []

    def test_filters_by_reason(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import list_blocked_peers

        cid_a = resolve_identity(platform="facebook",
                                   account_id="fb:BA", display_name="BA")
        cid_b = resolve_identity(platform="facebook",
                                   account_id="fb:BB", display_name="BB")
        # cid_a: 2 次 no_message_button
        for _ in range(2):
            append_journey(cid_a, actor="agent_a", action="greeting_blocked",
                             data={"reason": "no_message_button"})
        # cid_b: 1 次 phase_blocked (不同 reason, 不该匹)
        append_journey(cid_b, actor="agent_a", action="greeting_blocked",
                         data={"reason": "phase_blocked"})

        peers = list_blocked_peers("no_message_button", days=7)
        assert len(peers) == 1
        assert peers[0]["canonical_id"] == cid_a
        assert peers[0]["n_blocked"] == 2

    def test_multiple_peers_sorted_by_last_at(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import list_blocked_peers
        import time as _t

        cid_old = resolve_identity(platform="facebook",
                                     account_id="fb:Old", display_name="Old")
        cid_new = resolve_identity(platform="facebook",
                                     account_id="fb:New", display_name="New")

        append_journey(cid_old, actor="agent_a", action="greeting_blocked",
                         data={"reason": "cap_hit"})
        _t.sleep(1.1)  # 确保秒级区别
        append_journey(cid_new, actor="agent_a", action="greeting_blocked",
                         data={"reason": "cap_hit"})

        peers = list_blocked_peers("cap_hit", days=7)
        # 最近的排前
        assert peers[0]["canonical_id"] == cid_new
        assert peers[1]["canonical_id"] == cid_old

    def test_persona_key_in_output(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import list_blocked_peers

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:P", display_name="P")
        append_journey(cid, actor="agent_a", action="greeting_blocked",
                         data={"reason": "template_empty",
                               "persona_key": "jp_female_midlife"})
        peers = list_blocked_peers("template_empty", days=7)
        assert len(peers) == 1
        assert peers[0]["persona_key"] == "jp_female_midlife"

    def test_empty_reason_returns_empty(self, tmp_db):
        from src.host.lead_mesh.funnel_report import list_blocked_peers
        assert list_blocked_peers("", days=7) == []


class TestDateFilter:
    """Phase 8g: date 参数 (单日下钻) 优先于 days."""

    def test_compute_funnel_with_valid_date_filters_single_day(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel
        import datetime as dt

        cid = resolve_identity(platform="facebook", account_id="fb:D1",
                                 display_name="D1")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={})

        today = dt.datetime.utcnow().date().isoformat()
        # 单日 date=today 应有 1 个 friend_req
        stats = compute_funnel(days=30, date=today)
        assert stats.window_days == 1   # date 生效时强制 1
        assert stats.total_friend_requested == 1

        # 另一天 date (前 10 天) 应 0
        other = (dt.datetime.utcnow() - dt.timedelta(days=10)).date().isoformat()
        stats_other = compute_funnel(days=30, date=other)
        assert stats_other.total_friend_requested == 0

    def test_compute_funnel_invalid_date_falls_back_to_days(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel

        cid = resolve_identity(platform="facebook", account_id="fb:D2",
                                 display_name="D2")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={})
        # "invalid-date" 不合法 → 降级 days=7 窗口 → 应看到该 friend_req
        stats = compute_funnel(days=7, date="invalid-date")
        assert stats.window_days == 7
        assert stats.total_friend_requested == 1

    def test_list_blocked_peers_with_date(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import list_blocked_peers
        import datetime as dt

        cid = resolve_identity(platform="facebook", account_id="fb:D3",
                                 display_name="D3")
        append_journey(cid, actor="agent_a", action="greeting_blocked",
                         data={"reason": "no_message_button"})

        today = dt.datetime.utcnow().date().isoformat()
        peers = list_blocked_peers("no_message_button", days=30, date=today)
        assert len(peers) == 1
        # 他日 date 不含此事件
        other = (dt.datetime.utcnow() - dt.timedelta(days=5)).date().isoformat()
        peers_other = list_blocked_peers("no_message_button", days=30, date=other)
        assert len(peers_other) == 0


class TestTimeseries:
    """Phase 8e: 近 N 天按日分桶的漏斗时序."""

    def test_empty_db_returns_zeros_all_days(self, tmp_db):
        from src.host.lead_mesh.funnel_report import compute_funnel_timeseries
        series = compute_funnel_timeseries(days=7)
        assert len(series) == 7
        for p in series:
            assert p["friend_req"] == 0
            assert p["greeting_sent"] == 0
            assert p["blocked"] == 0
            # date 格式 YYYY-MM-DD
            assert len(p["date"]) == 10 and p["date"][4] == "-"

    def test_dates_are_consecutive_ascending(self, tmp_db):
        from src.host.lead_mesh.funnel_report import compute_funnel_timeseries
        import datetime as dt
        series = compute_funnel_timeseries(days=5)
        assert len(series) == 5
        # 升序连续, 最后一天是 today (UTC)
        today = dt.datetime.utcnow().date().isoformat()
        assert series[-1]["date"] == today
        # 相邻差 1 天
        for i in range(1, len(series)):
            a = dt.date.fromisoformat(series[i - 1]["date"])
            b = dt.date.fromisoformat(series[i]["date"])
            assert (b - a).days == 1

    def test_counts_bucket_today(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel_timeseries

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:TS", display_name="TS")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp"})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message"})
        append_journey(cid, actor="agent_a", action="greeting_blocked",
                         data={"reason": "no_message_button"})

        series = compute_funnel_timeseries(days=3)
        # 今天的 bucket 应有 1/1/1
        today_pt = series[-1]
        assert today_pt["friend_req"] == 1
        assert today_pt["greeting_sent"] == 1
        assert today_pt["blocked"] == 1
        # 前几天都是 0
        for p in series[:-1]:
            assert p["friend_req"] == 0
            assert p["greeting_sent"] == 0
            assert p["blocked"] == 0

    def test_actor_filter(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import compute_funnel_timeseries

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:AF", display_name="AF")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={})
        append_journey(cid, actor="agent_b", action="friend_requested",
                         data={})

        s_all = compute_funnel_timeseries(days=1)
        s_a = compute_funnel_timeseries(days=1, actor="agent_a")
        assert s_all[0]["friend_req"] == 2
        assert s_a[0]["friend_req"] == 1

    def test_days_clamped(self, tmp_db):
        from src.host.lead_mesh.funnel_report import compute_funnel_timeseries
        # days=0 → 1 天; days=1000 → 90 天
        assert len(compute_funnel_timeseries(days=0)) == 1
        assert len(compute_funnel_timeseries(days=1000)) == 90


class TestTextReport:
    def test_format_text_empty(self, tmp_db):
        from src.host.lead_mesh.funnel_report import (compute_funnel,
                                                        format_text_report)
        stats = compute_funnel(days=7)
        txt = format_text_report(stats)
        assert "A 端漏斗报告" in txt
        assert "近 7 天" in txt
        assert "rate_greet_after_friend: 0.0%" in txt

    def test_format_text_with_data(self, tmp_db):
        from src.host.lead_mesh import resolve_identity, append_journey
        from src.host.lead_mesh.funnel_report import (compute_funnel,
                                                        format_text_report)
        cid = resolve_identity(platform="facebook",
                                 account_id="fb:T", display_name="T")
        append_journey(cid, actor="agent_a", action="friend_requested",
                         data={"persona_key": "jp_female_midlife"})
        append_journey(cid, actor="agent_a", action="greeting_sent",
                         data={"via": "inline_profile_message"})

        txt = format_text_report(compute_funnel(days=7))
        assert "friend_requested: 1" in txt
        assert "greeting_sent: 1" in txt
        assert "jp_female_midlife: 1" in txt
        assert "100.0%" in txt   # rate_greet_after_friend
