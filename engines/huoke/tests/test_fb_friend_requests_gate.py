# -*- coding: utf-8 -*-
"""P1 `check_friend_requests_inbox` 双维度 gate 单元测试 (机器 B)。

覆盖:
  * `_lookup_lead_score` 的 3 种输出 (匹配/未匹配/异常降级)
  * mutual_friends gate 单维度行为 (向后兼容, min_lead_score=0)
  * mutual + lead_score 双维度 `and` / `or` policy
  * `accept_all=True` / `safe_accept=False` 绕过 gate
  * quota 限制 (max_requests // 2)
  * accepted_reasons / skipped_reasons 计数正确性
  * lead_score_checked / lead_score_hits 遥测

不测设备交互: 通过 MagicMock patch 掉 _u2 / smart_tap / _tap_accept_button_for
/ _list_friend_requests / _resolve_phase_and_cfg / time.sleep /
update_friend_request_status。
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest


def _make_fb():
    """Instantiate FacebookAutomation bypassing __init__ (需要 device_manager)."""
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb.hb = MagicMock()
    fb.hb.wait_think = MagicMock()
    return fb


@pytest.fixture
def fb_env():
    """Yield (fb, set_requests) with all device/IO deps patched out."""
    fb = _make_fb()
    stack = ExitStack()
    # 设备 / UI 层全部 no-op
    stack.enter_context(patch.object(fb, "_did", return_value="devA"))
    stack.enter_context(patch.object(fb, "_u2", return_value=MagicMock()))
    stack.enter_context(patch.object(fb, "_dismiss_dialogs"))
    stack.enter_context(patch.object(fb, "_detect_risk_dialog",
                                     return_value=(False, "")))
    stack.enter_context(patch.object(fb, "smart_tap", return_value=True))
    stack.enter_context(patch.object(fb, "_tap_accept_button_for",
                                     return_value=True))
    # 模块级依赖
    stack.enter_context(patch("src.app_automation.facebook._resolve_phase_and_cfg",
                              return_value=("growth", {})))
    stack.enter_context(patch("src.app_automation.facebook.time.sleep"))
    stack.enter_context(patch("src.app_automation.facebook.random.uniform",
                              return_value=0.0))
    stack.enter_context(patch("src.host.fb_store.update_friend_request_status"))

    requests_holder = {"list": []}

    def _set_requests(lst):
        requests_holder["list"] = lst

    stack.enter_context(patch.object(
        fb, "_list_friend_requests",
        side_effect=lambda d, n: list(requests_holder["list"][:n])
    ))

    try:
        yield fb, _set_requests
    finally:
        stack.close()


# ─── _lookup_lead_score 直测 ──────────────────────────────────────────────────

class TestLookupLeadScore:
    def test_empty_name_returns_zero(self):
        from src.app_automation.facebook import FacebookAutomation
        assert FacebookAutomation._lookup_lead_score("") == (None, 0)
        assert FacebookAutomation._lookup_lead_score("   ") == (None, 0)

    def test_store_miss_returns_zero(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = None
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Alice") == (None, 0)
        store.find_match.assert_called_once_with(name="Alice")

    def test_store_hit_returns_score(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = 42
        store.get_lead.return_value = {"id": 42, "score": 73}
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Bob") == (42, 73)

    def test_store_hit_with_float_score_coerced(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = 7
        store.get_lead.return_value = {"id": 7, "score": 68.9}
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Carol") == (7, 68)

    def test_store_hit_with_none_score(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = 9
        store.get_lead.return_value = {"id": 9, "score": None}
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Dan") == (9, 0)

    def test_store_hit_score_clamped_to_100(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = 1
        store.get_lead.return_value = {"id": 1, "score": 999}
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Eve") == (1, 100)

    def test_store_hit_negative_score_clamped(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = 1
        store.get_lead.return_value = {"id": 1, "score": -5}
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Fred") == (1, 0)

    def test_exception_downgrades_to_zero(self):
        from src.app_automation.facebook import FacebookAutomation
        with patch("src.leads.store.get_leads_store",
                   side_effect=RuntimeError("db offline")):
            assert FacebookAutomation._lookup_lead_score("Greg") == (None, 0)

    def test_bad_score_type_downgrades(self):
        from src.app_automation.facebook import FacebookAutomation
        store = MagicMock()
        store.find_match.return_value = 5
        store.get_lead.return_value = {"id": 5, "score": "not-a-number"}
        with patch("src.leads.store.get_leads_store", return_value=store):
            assert FacebookAutomation._lookup_lead_score("Hank") == (5, 0)


# ─── F5: Levenshtein 兜底测试 ────────────────────────────────────────────────

class TestLevenshteinLe1:
    def test_identical(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("alice", "alice") is True

    def test_single_substitution(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("alice", "alise") is True  # c→s

    def test_single_insertion(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("alice", "alices") is True
        assert _levenshtein_le1("alice", "aalice") is True

    def test_single_deletion(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("alice", "alic") is True
        assert _levenshtein_le1("alice", "lice") is True

    def test_two_substitutions_rejected(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("alice", "bloce") is False

    def test_length_diff_2_rejected(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("alice", "alices!") is False

    def test_prefix_of_other_with_diff_1(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("bob", "bobb") is True

    def test_empty_strings(self):
        from src.app_automation.facebook import _levenshtein_le1
        assert _levenshtein_le1("", "") is True
        assert _levenshtein_le1("", "a") is True
        assert _levenshtein_le1("", "ab") is False


class TestFuzzyLookupLeadScore:
    """F5: 硬 find_match miss 时 Levenshtein 兜底匹配。"""

    def test_fuzzy_matches_fullwidth_space(self, tmp_db):
        """全角空格 vs 半角空格 normalize 差异 1,Levenshtein 应命中。"""
        from src.app_automation.facebook import FacebookAutomation
        from src.leads.store import get_leads_store
        store = get_leads_store()
        # 先用半角空格存入 A 的 normalize 结果 (e.g. "山田 花子")
        lid = store.add_lead(name="yamada hanako",  # 模拟 normalize 后
                             source_platform="facebook")
        # B 扫到 UI 文本是带全角空格或拼写略差的 "yamada hanakoo"
        # find_match 硬匹配会 miss, fuzzy 兜底命中
        result = FacebookAutomation._lookup_lead_score("yamada hanakoo")
        assert result[0] == lid
        assert result[1] == 0  # 默认 score=0

    def test_fuzzy_rejects_too_different(self, tmp_db):
        """距离 >1 应 miss 而非错匹。"""
        from src.app_automation.facebook import FacebookAutomation
        from src.leads.store import get_leads_store
        store = get_leads_store()
        store.add_lead(name="yamada hanako", source_platform="facebook")
        result = FacebookAutomation._lookup_lead_score("yamasuki nanoko")
        assert result == (None, 0)

    def test_fuzzy_skips_too_short(self, tmp_db):
        """目标名太短 (<4 chars) fuzzy 不启用,避免 "bo"→"bob" 之类误匹。"""
        from src.app_automation.facebook import FacebookAutomation
        from src.leads.store import get_leads_store
        store = get_leads_store()
        store.add_lead(name="bob", source_platform="facebook")
        # normalized = "bob", 长度 3 < 4
        result = FacebookAutomation._lookup_lead_score("box")
        assert result == (None, 0)

    def test_exact_match_preferred_over_fuzzy(self, tmp_db):
        """有硬匹配时不走 fuzzy (find_match 结果优先)。"""
        from src.app_automation.facebook import FacebookAutomation
        from src.leads.store import get_leads_store
        store = get_leads_store()
        exact_lid = store.add_lead(name="charles dupont",
                                   source_platform="facebook")
        near_lid = store.add_lead(name="charles dupond",
                                  source_platform="facebook")
        # 期望硬命中 exact_lid
        result = FacebookAutomation._lookup_lead_score("charles dupont")
        assert result[0] == exact_lid
        assert result[0] != near_lid


# ─── mutual_friends 单维度 (向后兼容) ─────────────────────────────────────────

class TestMutualOnlyGate:
    def test_min_lead_score_zero_does_not_query_leads_store(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "_lookup_lead_score") as mocked_lookup:
            stats = fb.check_friend_requests_inbox(
                min_lead_score=0, max_requests=4)
        mocked_lookup.assert_not_called()
        assert stats["score_enabled"] is False
        assert stats["score_policy"] == ""
        assert stats["accepted"] == 1
        assert stats["accepted_reasons"]["mutual_only"] == 1

    def test_mutual_low_skipped(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        stats = fb.check_friend_requests_inbox(
            min_mutual_friends=1, max_requests=4)
        assert stats["accepted"] == 0
        assert stats["skipped"] == 1
        assert stats["skipped_reasons"]["mutual_low"] == 1
        assert stats["skipped_reasons"]["score_low"] == 0
        assert stats["skipped_reasons"]["both_low"] == 0

    def test_mixed_seen_count_correct(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([
            {"name": "Alice", "mutual_friends": 3},
            {"name": "Bob", "mutual_friends": 0},
            {"name": "Carol", "mutual_friends": 5},
        ])
        stats = fb.check_friend_requests_inbox(
            min_mutual_friends=1, max_requests=6)
        # quota = max(1, 6//2) = 3 → 允许三人全过
        assert stats["requests_seen"] == 3
        assert stats["accepted"] == 2
        assert stats["skipped"] == 1
        assert stats["accepted_reasons"]["mutual_only"] == 2
        assert stats["skipped_reasons"]["mutual_low"] == 1


# ─── 双维度 and policy ────────────────────────────────────────────────────────

class TestAndPolicy:
    def test_both_ok_accepted_as_both(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 70)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="and", max_requests=4)
        assert stats["accepted"] == 1
        assert stats["accepted_reasons"]["both"] == 1
        assert stats["lead_score_checked"] == 1
        assert stats["lead_score_hits"] == 1

    def test_mutual_ok_score_low_skipped_as_score_low(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 30)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="and", max_requests=4)
        assert stats["accepted"] == 0
        assert stats["skipped_reasons"]["score_low"] == 1

    def test_mutual_low_score_ok_skipped_as_mutual_low(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 80)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="and", max_requests=4)
        assert stats["accepted"] == 0
        assert stats["skipped_reasons"]["mutual_low"] == 1

    def test_both_low_skipped_as_both_low(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        with patch.object(fb, "_lookup_lead_score", return_value=(None, 0)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="and", max_requests=4)
        assert stats["accepted"] == 0
        assert stats["skipped_reasons"]["both_low"] == 1
        assert stats["lead_score_hits"] == 0


# ─── 双维度 or policy ─────────────────────────────────────────────────────────

class TestOrPolicy:
    def test_mutual_ok_score_low_accepted_as_mutual_only(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 20)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="or", max_requests=4)
        assert stats["accepted"] == 1
        assert stats["accepted_reasons"]["mutual_only"] == 1

    def test_mutual_low_score_ok_accepted_as_score_only(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 80)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="or", max_requests=4)
        assert stats["accepted"] == 1
        assert stats["accepted_reasons"]["score_only"] == 1

    def test_both_low_still_skipped_under_or(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        with patch.object(fb, "_lookup_lead_score", return_value=(None, 0)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="or", max_requests=4)
        assert stats["accepted"] == 0
        assert stats["skipped_reasons"]["both_low"] == 1


# ─── 绕过 gate ────────────────────────────────────────────────────────────────

class TestBypassGate:
    def test_accept_all_bypasses_both_checks(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([
            {"name": "Alice", "mutual_friends": 0},
            {"name": "Bob", "mutual_friends": 0},
        ])
        with patch.object(fb, "_lookup_lead_score") as mocked_lookup:
            stats = fb.check_friend_requests_inbox(
                accept_all=True, min_mutual_friends=10,
                min_lead_score=99, max_requests=10)
        mocked_lookup.assert_not_called()
        assert stats["accepted"] == 2
        assert stats["accepted_reasons"]["quota"] == 2

    def test_safe_accept_false_bypasses(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        with patch.object(fb, "_lookup_lead_score") as mocked_lookup:
            stats = fb.check_friend_requests_inbox(
                safe_accept=False, min_mutual_friends=10,
                min_lead_score=99, max_requests=4)
        mocked_lookup.assert_not_called()
        assert stats["accepted"] == 1
        assert stats["accepted_reasons"]["quota"] == 1


# ─── quota / 错误路径 ─────────────────────────────────────────────────────────

class TestQuotaAndErrors:
    def test_quota_limits_accepts_to_half(self, fb_env):
        fb, set_reqs = fb_env
        # 6 人全符合条件,但 quota = max(1, 6//2) = 3
        set_reqs([
            {"name": f"p{i}", "mutual_friends": 5} for i in range(6)
        ])
        stats = fb.check_friend_requests_inbox(
            min_mutual_friends=1, max_requests=6)
        assert stats["accepted"] == 3
        assert stats["requests_seen"] == 6

    def test_tap_accept_failure_counts_as_error(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "_tap_accept_button_for", return_value=False):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, max_requests=4)
        assert stats["accepted"] == 0
        assert stats["errors"] == 1

    def test_friends_tab_missing_returns_early(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "smart_tap", return_value=False):
            stats = fb.check_friend_requests_inbox(max_requests=4)
        assert stats["opened"] is False
        assert "error" in stats
        assert stats["accepted"] == 0

    def test_risk_dialog_aborts(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        with patch.object(fb, "_detect_risk_dialog",
                          return_value=(True, "login challenge")):
            stats = fb.check_friend_requests_inbox(max_requests=4)
        assert stats["opened"] is True
        assert stats["risk_detected"] == "login challenge"
        assert stats["accepted"] == 0


# ─── telemetry ────────────────────────────────────────────────────────────────

class TestTelemetry:
    def test_lead_score_checked_counts_only_enabled_path(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([
            {"name": "Alice", "mutual_friends": 3},
            {"name": "Bob", "mutual_friends": 5},
        ])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 80)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50, max_requests=4)
        assert stats["lead_score_checked"] == 2
        assert stats["lead_score_hits"] == 2

    def test_lead_score_hit_miss_mix(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([
            {"name": "A", "mutual_friends": 3},
            {"name": "B", "mutual_friends": 3},
            {"name": "C", "mutual_friends": 3},
        ])
        calls = {"n": 0}

        def _scorer(name):
            calls["n"] += 1
            return ((99, 80), (None, 0), (5, 60))[calls["n"] - 1]

        with patch.object(fb, "_lookup_lead_score", side_effect=_scorer):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="or", max_requests=6)
        assert stats["lead_score_checked"] == 3
        assert stats["lead_score_hits"] == 2  # A + C,B 未命中

    def test_policy_echoed_only_when_enabled(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([])
        stats_disabled = fb.check_friend_requests_inbox(
            min_lead_score=0, max_requests=4)
        assert stats_disabled["score_policy"] == ""
        stats_enabled = fb.check_friend_requests_inbox(
            min_lead_score=50, score_policy="or", max_requests=4)
        assert stats_enabled["score_policy"] == "or"

    def test_invalid_policy_falls_back_to_and(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        with patch.object(fb, "_lookup_lead_score", return_value=(10, 80)):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                score_policy="xor", max_requests=4)
        # xor 被归一到 and; mutual 不够 + score ok → skip
        assert stats["score_policy"] == "and"
        assert stats["accepted"] == 0
        assert stats["skipped_reasons"]["mutual_low"] == 1

    def test_meta_annotated_with_lead_info(self, fb_env):
        fb, set_reqs = fb_env
        metas = [{"name": "Alice", "mutual_friends": 3}]
        set_reqs(metas)
        with patch.object(fb, "_lookup_lead_score", return_value=(77, 66)):
            fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50, max_requests=4)
        # 原 meta dict 被写入 lead_id / lead_score(供调用方回看)
        # 注:_list_friend_requests 返回的是拷贝列表但元素是同一 dict
        assert metas[0].get("lead_id") == 77
        assert metas[0].get("lead_score") == 66


# ─── P7 §7.1: add_friend_accepted contact_event ─────────────────────────────

class TestAddFriendAcceptedEvent:
    """§7.1 B 机在接受好友请求后应写 record_contact_event(add_friend_accepted)
    给 A 的 Lead Mesh Dashboard,Phase 5 未 merge 时静默 skip。"""

    def test_accepted_writes_contact_event(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        events = []
        fake_fb_store_mod = MagicMock()
        fake_fb_store_mod.record_contact_event = lambda *a, **kw: (
            events.append({"args": a, "kw": kw}) or 1)
        fake_fb_store_mod.update_friend_request_status = MagicMock()
        with patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store_mod}):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, max_requests=4)
        assert stats["accepted"] == 1
        accepted_events = [e for e in events
                           if e["args"][2] == "add_friend_accepted"]
        assert len(accepted_events) == 1
        e = accepted_events[0]
        assert e["args"][0] == "devA"
        assert e["args"][1] == "Alice"
        meta = e["kw"]["meta"]
        assert meta["mutual_friends"] == 3
        assert meta["accept_key"] == "mutual_only"

    def test_accepted_meta_has_lead_info(self, fb_env):
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        events = []
        fake_fb_store_mod = MagicMock()
        fake_fb_store_mod.record_contact_event = lambda *a, **kw: (
            events.append(kw) or 1)
        fake_fb_store_mod.update_friend_request_status = MagicMock()
        with patch.object(fb, "_lookup_lead_score",
                          return_value=(77, 88)), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store_mod}):
            fb.check_friend_requests_inbox(
                min_mutual_friends=1, min_lead_score=50,
                max_requests=4)
        assert events[0]["meta"]["lead_id"] == 77
        assert events[0]["meta"]["lead_score"] == 88
        assert events[0]["meta"]["accept_key"] == "both"

    def test_skipped_does_not_emit_event(self, fb_env):
        """skipped 场景不发 add_friend_accepted 事件。"""
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        events = []
        fake_fb_store_mod = MagicMock()
        fake_fb_store_mod.record_contact_event = lambda *a, **kw: (
            events.append(a) or 1)
        fake_fb_store_mod.update_friend_request_status = MagicMock()
        with patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store_mod}):
            fb.check_friend_requests_inbox(
                min_mutual_friends=1, max_requests=4)
        assert events == []

    def test_tap_accept_fails_no_event(self, fb_env):
        """_tap_accept_button_for 返回 False 时不发事件。"""
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        events = []
        fake_fb_store_mod = MagicMock()
        fake_fb_store_mod.record_contact_event = lambda *a, **kw: (
            events.append(a) or 1)
        fake_fb_store_mod.update_friend_request_status = MagicMock()
        with patch.object(fb, "_tap_accept_button_for", return_value=False), \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store_mod}):
            fb.check_friend_requests_inbox(
                min_mutual_friends=1, max_requests=4)
        assert events == []

    def test_accepted_writes_contact_event_quota_path(self, fb_env):
        """M4.2 (A Round 3 review): accept_all=True 走 quota 路径时,
        add_friend_accepted 事件 meta.accept_key='quota' (对齐 P1 gate 4 档)。"""
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 0}])
        events = []
        fake_fb_store_mod = MagicMock()
        fake_fb_store_mod.record_contact_event = lambda *a, **kw: (
            events.append({"args": a, "kw": kw}) or 1)
        fake_fb_store_mod.update_friend_request_status = MagicMock()
        with patch.object(fb, "_lookup_lead_score") as mocked_lookup, \
             patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store_mod}):
            stats = fb.check_friend_requests_inbox(
                accept_all=True, min_mutual_friends=10,
                min_lead_score=99, max_requests=4)
        mocked_lookup.assert_not_called()
        assert stats["accepted"] == 1
        accepted = [e for e in events
                     if e["args"][2] == "add_friend_accepted"]
        assert len(accepted) == 1
        assert accepted[0]["kw"]["meta"]["accept_key"] == "quota"

    def test_phase5_not_merged_graceful(self, fb_env):
        """record_contact_event 不存在 → 不抛异常, stats.accepted 仍正常。"""
        fb, set_reqs = fb_env
        set_reqs([{"name": "Alice", "mutual_friends": 3}])
        # fb_store 没有 record_contact_event
        fake_fb_store_mod = MagicMock(
            spec=["update_friend_request_status"])
        fake_fb_store_mod.update_friend_request_status = MagicMock()
        with patch.dict("sys.modules",
                        {"src.host.fb_store": fake_fb_store_mod}):
            stats = fb.check_friend_requests_inbox(
                min_mutual_friends=1, max_requests=4)
        assert stats["accepted"] == 1
