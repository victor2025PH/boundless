# -*- coding: utf-8 -*-
"""P12 `scripts/check_merge_readiness.py` 的单元测试。

只测 classify_prs / PrInfo.is_ready / PrInfo.review_verdict 的纯业务逻辑 —
GitHub API 访问层由手动运行 (已在实仓 15 个 PR 上验证输出正确)。
"""
from __future__ import annotations

import pytest


def _make_pr(number=1, head="branch-a", base="main",
             mergeable=True, mergeable_state="clean",
             draft=False, reviews=None, checks_state="passing",
             title="test pr"):
    from scripts.check_merge_readiness import PrInfo
    return PrInfo(
        number=number, title=title, head=head, base=base,
        user="x", state="open",
        mergeable=mergeable, mergeable_state=mergeable_state,
        draft=draft, html_url=f"https://x/{number}",
        reviews=reviews or [], checks_state=checks_state,
    )


# ─── PrInfo.is_ready ─────────────────────────────────────────────────────────

class TestIsReady:
    def test_clean_base_main_is_ready(self):
        pr = _make_pr(mergeable_state="clean")
        assert pr.is_ready is True

    def test_unstable_base_main_is_ready(self):
        """unstable = 可合但无 required check 绿 (仓库无 CI 时常态)。"""
        pr = _make_pr(mergeable_state="unstable")
        assert pr.is_ready is True

    def test_draft_not_ready(self):
        pr = _make_pr(mergeable_state="clean", draft=True)
        assert pr.is_ready is False

    def test_dirty_not_ready(self):
        pr = _make_pr(mergeable_state="dirty")
        assert pr.is_ready is False

    def test_behind_not_ready(self):
        pr = _make_pr(mergeable_state="behind")
        assert pr.is_ready is False

    def test_failing_checks_not_ready(self):
        pr = _make_pr(mergeable_state="clean", checks_state="failing")
        assert pr.is_ready is False

    def test_base_not_main_not_ready(self):
        pr = _make_pr(mergeable_state="clean", base="feat-x")
        assert pr.is_ready is False

    def test_mergeable_false_not_ready(self):
        pr = _make_pr(mergeable=False, mergeable_state="clean")
        assert pr.is_ready is False


# ─── PrInfo.review_verdict ───────────────────────────────────────────────────

class TestReviewVerdict:
    def test_no_reviews_returns_none(self):
        pr = _make_pr()
        assert pr.review_verdict == "NONE"

    def test_approved_wins(self):
        pr = _make_pr(reviews=[
            {"user": {"login": "u1"}, "state": "APPROVED",
             "submitted_at": "2026-04-20"},
        ])
        assert pr.review_verdict == "APPROVED"

    def test_changes_requested_overrides_approved_from_same_user(self):
        pr = _make_pr(reviews=[
            {"user": {"login": "u1"}, "state": "APPROVED",
             "submitted_at": "2026-04-20"},
            {"user": {"login": "u1"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-04-21"},
        ])
        assert pr.review_verdict == "CHANGES_REQUESTED"

    def test_approved_from_second_user_keeps_approved(self):
        pr = _make_pr(reviews=[
            {"user": {"login": "u1"}, "state": "APPROVED",
             "submitted_at": "2026-04-20"},
            {"user": {"login": "u2"}, "state": "APPROVED",
             "submitted_at": "2026-04-21"},
        ])
        assert pr.review_verdict == "APPROVED"

    def test_one_changes_requested_blocks(self):
        pr = _make_pr(reviews=[
            {"user": {"login": "u1"}, "state": "APPROVED",
             "submitted_at": "2026-04-20"},
            {"user": {"login": "u2"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-04-21"},
        ])
        assert pr.review_verdict == "CHANGES_REQUESTED"


# ─── classify_prs ────────────────────────────────────────────────────────────

class TestClassifyPrs:
    def test_empty_list(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([])
        assert r.ready == []
        assert r.waiting == []
        assert r.blocked == []
        assert r.recommended_order == []

    def test_single_clean_main_pr_ready(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, mergeable_state="clean")])
        assert len(r.ready) == 1
        assert r.waiting == []
        assert r.blocked == []
        assert r.recommended_order == [1]

    def test_dirty_goes_to_blocked(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, mergeable_state="dirty")])
        assert len(r.blocked) == 1
        assert "冲突" in r.blocked[0][1]

    def test_draft_goes_to_blocked(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, draft=True)])
        assert len(r.blocked) == 1
        assert "草稿" in r.blocked[0][1]

    def test_failing_ci_goes_to_blocked(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, checks_state="failing")])
        assert len(r.blocked) == 1

    def test_changes_requested_goes_to_blocked(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, reviews=[
            {"user": {"login": "u1"}, "state": "CHANGES_REQUESTED",
             "submitted_at": "2026-04-21"},
        ])])
        assert len(r.blocked) == 1

    def test_stack_dep_goes_to_waiting(self):
        """PR #2 base=feat-x, PR #1 head=feat-x → PR #2 waiting on PR #1。"""
        from scripts.check_merge_readiness import classify_prs
        pr1 = _make_pr(number=1, head="feat-x", base="main",
                       mergeable_state="clean")
        pr2 = _make_pr(number=2, head="feat-y", base="feat-x",
                       mergeable_state="clean")
        r = classify_prs([pr1, pr2])
        assert len(r.ready) == 1
        assert r.ready[0].number == 1
        assert len(r.waiting) == 1
        assert r.waiting[0][0].number == 2
        assert "PR #1" in r.waiting[0][1]

    def test_recommended_order_topological(self):
        """三层栈 PR1(main) ← PR2(feat-a) ← PR3(feat-b) 应按依赖序输出。"""
        from scripts.check_merge_readiness import classify_prs
        pr1 = _make_pr(number=1, head="feat-a", base="main",
                       mergeable_state="clean")
        pr2 = _make_pr(number=2, head="feat-b", base="feat-a",
                       mergeable_state="clean")
        pr3 = _make_pr(number=3, head="feat-c", base="feat-b",
                       mergeable_state="clean")
        r = classify_prs([pr3, pr1, pr2])  # 乱序输入
        assert r.recommended_order == [1, 2, 3]

    def test_mixed_ready_and_stack(self):
        """独立 main PR + 栈 PR 混合场景。"""
        from scripts.check_merge_readiness import classify_prs
        indep = _make_pr(number=10, head="solo", base="main",
                          mergeable_state="clean")
        stack_base = _make_pr(number=20, head="stack-a", base="main",
                               mergeable_state="clean")
        stack_top = _make_pr(number=21, head="stack-b", base="stack-a",
                              mergeable_state="clean")
        r = classify_prs([indep, stack_base, stack_top])
        assert len(r.ready) == 2  # 10 和 20 都能 main 直接合
        assert 10 in r.recommended_order
        assert 20 in r.recommended_order
        # 21 必须在 20 之后
        assert r.recommended_order.index(21) > r.recommended_order.index(20)

    def test_behind_is_waiting(self):
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, mergeable_state="behind")])
        assert len(r.waiting) == 1
        assert "rebase" in r.waiting[0][1]

    def test_blocked_mergeable_state_is_waiting(self):
        """mergeable_state=blocked 通常是 required reviews 不够, 归 waiting。"""
        from scripts.check_merge_readiness import classify_prs
        r = classify_prs([_make_pr(number=1, mergeable_state="blocked")])
        assert len(r.waiting) == 1
        assert "reviews" in r.waiting[0][1] or "blocked" in r.waiting[0][1]

    def test_non_main_base_no_matching_open_pr_allows_ready(self):
        """如果 PR base 是某个分支但没对应 open PR (已 merged), 仍可 ready。"""
        from scripts.check_merge_readiness import classify_prs
        orphan = _make_pr(number=5, head="child", base="already-merged",
                           mergeable_state="clean")
        r = classify_prs([orphan])
        assert len(r.ready) == 1
