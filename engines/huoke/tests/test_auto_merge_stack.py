# -*- coding: utf-8 -*-
"""`scripts/auto_merge_stack.py` 单元测试 — 纯逻辑 + API mock。

真的 apply_merges 会动 GitHub 分支, 不测, 用 `--apply` 手动验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# P2-⑫: spawn 子 Python 进程时强制 UTF-8 防 Windows cp936 emoji 解码挂.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "auto_merge_stack.py"


# ─── CLI 参数保护 ──────────────────────────────────────────────────────────

class TestCli:
    def test_help_exits_0(self):
        """--help 不打网络, 秒退, 能打印 description。"""
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 0
        assert "自动合并" in r.stdout or "auto" in r.stdout.lower()


# ─── _parse_ids ──────────────────────────────────────────────────────────────

class TestParseIds:
    def test_basic(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids("1,2,3") == {1, 2, 3}

    def test_with_spaces(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids(" 10 , 20 ") == {10, 20}

    def test_skips_non_int(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids("1,foo,3") == {1, 3}

    def test_empty_tokens_ignored(self):
        from scripts.auto_merge_stack import _parse_ids
        assert _parse_ids(",,1,") == {1}


# ─── MergePlan dataclass ───────────────────────────────────────────────────

class TestMergePlan:
    def test_default_state(self):
        from scripts.auto_merge_stack import MergePlan
        p = MergePlan(pr_number=1, branch="feat-b-x", base="main")
        assert p.status == "pending"
        assert p.approved_by_a is False
        assert p.has_changes_requested is False

    def test_to_dict_roundtrip(self):
        from scripts.auto_merge_stack import MergePlan
        p = MergePlan(pr_number=42, branch="b", base="main",
                       latest_review_state="APPROVED", merge_sha="abc123")
        d = p.to_dict()
        assert d["pr_number"] == 42
        assert d["merge_sha"] == "abc123"


# ─── collect_plans 拓扑排序 ──────────────────────────────────────────────────

class TestCollectPlans:
    def _pr(self, num, head, base, title="t"):
        return {"number": num, "title": title,
                "head": {"ref": head}, "base": {"ref": base}}

    def test_empty(self):
        from scripts.auto_merge_stack import collect_plans
        with patch("scripts.auto_merge_stack.github_api", return_value=[]):
            assert collect_plans("fake") == []

    def test_base_main_first(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(3, "feat-b-p4", "feat-b-p3"),
            self._pr(1, "feat-b-p3", "main"),
            self._pr(2, "feat-b-other", "main"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t")
        # p3 和 p-other 都 base=main → 在前 (顺序任意), p4 在后
        idx = {p.branch: i for i, p in enumerate(plans)}
        assert idx["feat-b-p3"] < idx["feat-b-p4"]

    def test_only_filter(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(1, "feat-b-a", "main"),
            self._pr(2, "feat-b-b", "main"),
            self._pr(3, "feat-b-c", "main"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t", only_prs={1, 3})
        assert {p.pr_number for p in plans} == {1, 3}

    def test_orphan_base_placed_end(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(1, "feat-b-a", "main"),
            self._pr(2, "feat-b-b", "feat-b-already-merged"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t")
        assert plans[0].branch == "feat-b-a"
        assert plans[1].branch == "feat-b-b"

    def test_non_b_filtered(self):
        from scripts.auto_merge_stack import collect_plans
        prs = [
            self._pr(1, "feat-b-x", "main"),
            self._pr(2, "feat-a-phase3", "main"),
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   return_value=prs):
            plans = collect_plans("t", head_prefix="feat-b-")
        assert len(plans) == 1
        assert plans[0].branch == "feat-b-x"


# ─── check_readiness ────────────────────────────────────────────────────────

class TestCheckReadiness:
    def _pr(self, state="open", mergeable=True,
             mergeable_state="clean", base="main"):
        return {"number": 10, "title": "x", "state": state,
                "mergeable": mergeable, "mergeable_state": mergeable_state,
                "head": {"ref": "feat-b-x"}, "base": {"ref": base}}

    def _api_factory(self, pr_meta, reviews):
        def fake_api(path, token, method="GET", body=None):
            if path.endswith("/reviews"):
                return reviews
            return pr_meta
        return fake_api

    def test_ready_when_approved_and_clean(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "ready"
        assert plan.approved_by_a is True

    def test_blocked_when_changes_requested(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [
            {"state": "APPROVED",
              "submitted_at": "2026-04-23T10:00:00Z",
              "user": {"login": "a"}},
            {"state": "CHANGES_REQUESTED",
              "submitted_at": "2026-04-24T12:00:00Z",  # 更新
              "user": {"login": "a"}},
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert plan.has_changes_requested is True
        assert "CHANGES_REQUESTED" in plan.error

    def test_blocked_when_not_approved(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), [])):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert "APPROVED" in plan.error

    def test_skipped_when_pr_closed(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(state="closed"), [])):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "skipped"

    def test_blocked_when_dirty(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(mergeable_state="dirty"), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert "dirty" in plan.error

    def test_retarget_set_when_parent_merged(self):
        """base 在 merged_branches → 标 retarget_base_to=main。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-p4",
                          base="feat-b-p3")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(base="feat-b-p3"), reviews)):
            check_readiness(plan, "t",
                             merged_branches={"feat-b-p3"})
        assert plan.retarget_base_to == "main"
        assert plan.status == "ready"

    def test_blocked_when_base_not_main_and_parent_unmerged(self):
        """base 指向栈上层分支但 parent PR 还没合 → 保守 blocked, 防止
        直接 PUT /merge 合进栈分支 (2026-04-24 PR #10 踩过的坑: #10 approved
        + mergeable=clean, base=feat-b-followup-a-review, #9 仍 blocked;
        旧版工具直接 merge 把 #10 合进了 #9 分支而不是 main)。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-p7",
                          base="feat-b-followup-a-review")
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       self._pr(base="feat-b-followup-a-review"),
                       reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.status == "blocked"
        assert "base" in plan.error
        assert "feat-b-followup-a-review" in plan.error

    def test_commented_with_approve_marker_is_approved(self):
        """A 的 approve-equivalent COMMENTED 识别为 APPROVED (GitHub 不让自审)。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{
            "state": "COMMENTED",
            "submitted_at": "2026-04-23T21:17:24Z",
            "user": {"login": "victor2025PH"},
            "body": "## ✅ A 侧 review 通过 (approve-equivalent)\n\n..."
        }]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.approved_by_a is True
        assert plan.status == "ready"

    def test_commented_without_marker_blocks(self):
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [{
            "state": "COMMENTED",
            "submitted_at": "2026-04-23T21:17:24Z",
            "user": {"login": "other"},
            "body": "looks interesting"
        }]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        assert plan.approved_by_a is False
        assert plan.status == "blocked"

    def test_is_approve_equivalent_pure(self):
        from scripts.auto_merge_stack import _is_approve_equivalent
        assert _is_approve_equivalent({"state": "APPROVED"}) is True
        assert _is_approve_equivalent({
            "state": "COMMENTED",
            "body": "## ✅ A 侧 review 通过 (approve-equivalent)",
        }) is True
        assert _is_approve_equivalent({
            "state": "COMMENTED", "body": "nope"}) is False
        assert _is_approve_equivalent({
            "state": "CHANGES_REQUESTED",
            "body": "approve-equivalent jk"}) is False

    def test_dismissed_review_not_counted_as_approved(self):
        """DISMISSED review 不当 APPROVED。"""
        from scripts.auto_merge_stack import MergePlan, check_readiness
        plan = MergePlan(pr_number=10, branch="feat-b-x", base="main")
        reviews = [
            {"state": "APPROVED",
              "submitted_at": "2026-04-23T10:00:00Z",
              "user": {"login": "a"}},
            {"state": "DISMISSED",
              "submitted_at": "2026-04-24T12:00:00Z",
              "user": {"login": "a"}},
        ]
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(self._pr(), reviews)):
            check_readiness(plan, "t", merged_branches=set())
        # latest 是 DISMISSED → approved_users 空
        assert plan.approved_by_a is False
        assert plan.status == "blocked"


# ─── render ────────────────────────────────────────────────────────────────

class TestRender:
    def test_empty(self):
        from scripts.auto_merge_stack import render
        txt = render([], applied=False)
        assert "无匹配" in txt

    def test_ready_row(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-x", base="main",
                       title="feat x", status="ready",
                       approved_by_a=True, mergeable_state="clean")
        txt = render([p], applied=False)
        assert "PR#10" in txt
        assert "ready" in txt
        assert "feat-b-x" in txt
        assert "APPROVED" in txt

    def test_blocked_shows_error(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-x", base="main",
                       status="blocked", error="未 APPROVED")
        txt = render([p], applied=False)
        assert "blocked" in txt
        assert "APPROVED" in txt

    def test_merged_shows_sha(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-x", base="main",
                       status="merged", merge_sha="abc12345ef")
        txt = render([p], applied=True)
        assert "merged" in txt
        assert "abc12345" in txt

    def test_retarget_indicator_shown(self):
        from scripts.auto_merge_stack import MergePlan, render
        p = MergePlan(pr_number=10, branch="feat-b-p4",
                       base="feat-b-p3", status="ready",
                       approved_by_a=True, mergeable_state="clean",
                       retarget_base_to="main")
        txt = render([p], applied=False)
        assert "base→main" in txt or "base" in txt

    def test_summary_counts(self):
        from scripts.auto_merge_stack import MergePlan, render
        plans = [
            MergePlan(pr_number=1, branch="a", base="main", status="ready",
                       approved_by_a=True, mergeable_state="clean"),
            MergePlan(pr_number=2, branch="b", base="main", status="ready",
                       approved_by_a=True, mergeable_state="clean"),
            MergePlan(pr_number=3, branch="c", base="main", status="blocked",
                       error="x"),
        ]
        txt = render(plans, applied=False)
        assert "ready" in txt
        assert "blocked" in txt


# ─── wait_for_mergeable_settled (GitHub recompute 窗口重试) ─────────────────

class TestWaitForMergeableSettled:
    def _api_factory(self, states_sequence):
        """每次调 GET /pulls/:n 返序列里下一个 mergeable_state。"""
        idx = [0]
        def fake_api(path, token, method="GET", body=None):
            if path.endswith("/reviews"):
                return [{
                    "state": "APPROVED",
                    "submitted_at": "2026-04-24T12:00:00Z",
                    "user": {"login": "a"},
                }]
            if "/pulls/" in path:
                state = states_sequence[
                    min(idx[0], len(states_sequence) - 1)]
                idx[0] += 1
                return {"number": 10, "title": "t", "state": "open",
                        "mergeable": True, "mergeable_state": state,
                        "head": {"ref": "x"}, "base": {"ref": "main"}}
            return []
        return fake_api

    def test_unknown_then_clean_settles(self):
        from scripts.auto_merge_stack import (
            MergePlan, wait_for_mergeable_settled,
        )
        plan = MergePlan(pr_number=10, branch="x", base="main")
        sleep_calls = []
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(["unknown", "clean"])):
            wait_for_mergeable_settled(
                plan, "t", merged_branches=set(),
                max_attempts=4, wait_seconds=0.01,
                sleep_fn=lambda s: sleep_calls.append(s))
        assert plan.mergeable_state == "clean"
        assert plan.status == "ready"
        assert len(sleep_calls) == 1

    def test_clean_first_call_no_sleep(self):
        from scripts.auto_merge_stack import (
            MergePlan, wait_for_mergeable_settled,
        )
        plan = MergePlan(pr_number=10, branch="x", base="main")
        sleep_calls = []
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(["clean"])):
            wait_for_mergeable_settled(
                plan, "t", merged_branches=set(),
                max_attempts=4, wait_seconds=0.01,
                sleep_fn=lambda s: sleep_calls.append(s))
        assert plan.mergeable_state == "clean"
        assert sleep_calls == []

    def test_all_unknown_exits_after_max(self):
        from scripts.auto_merge_stack import (
            MergePlan, wait_for_mergeable_settled,
        )
        plan = MergePlan(pr_number=10, branch="x", base="main")
        sleep_calls = []
        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=self._api_factory(
                       ["unknown", "unknown", "unknown", "unknown"])):
            wait_for_mergeable_settled(
                plan, "t", merged_branches=set(),
                max_attempts=4, wait_seconds=0.01,
                sleep_fn=lambda s: sleep_calls.append(s))
        assert plan.mergeable_state == "unknown"
        assert len(sleep_calls) == 3


# ─── apply_merges (核心合并逻辑, 全部 mock API) ──────────────────────────────

class TestApplyMerges:
    def test_happy_path_two_prs(self):
        """两个 PR base=main 全绿 → 都 merged。"""
        from scripts.auto_merge_stack import MergePlan, apply_merges

        plan1 = MergePlan(pr_number=1, branch="feat-b-a", base="main")
        plan2 = MergePlan(pr_number=2, branch="feat-b-b", base="main")

        pr_meta_template = {
            "state": "open", "mergeable": True,
            "mergeable_state": "clean",
            "head": {"ref": "x"}, "base": {"ref": "main"},
            "title": "t",
        }
        reviews = [{"state": "APPROVED",
                     "submitted_at": "2026-04-24T12:00:00Z",
                     "user": {"login": "a"}}]

        def fake_api(path, token, method="GET", body=None):
            if "/merge" in path and method == "PUT":
                return {"merged": True, "sha": f"merged-{path}"}
            if path.endswith("/reviews"):
                return reviews
            if "/pulls/" in path:
                return pr_meta_template
            return []

        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=fake_api), \
             patch("scripts.auto_merge_stack.git_fetch_main",
                   return_value=True):
            r = apply_merges([plan1, plan2], "t",
                              merge_method="merge",
                              continue_on_error=False)
        assert r[0].status == "merged"
        assert r[1].status == "merged"

    def test_halts_on_blocked(self):
        """第一个 blocked, 第二个应 skipped。"""
        from scripts.auto_merge_stack import MergePlan, apply_merges

        plan1 = MergePlan(pr_number=1, branch="feat-b-a", base="main")
        plan2 = MergePlan(pr_number=2, branch="feat-b-b", base="main")

        def fake_api(path, token, method="GET", body=None):
            if path.endswith("/reviews"):
                return []  # 没 approval
            if "/pulls/" in path:
                return {"state": "open", "mergeable": True,
                        "mergeable_state": "clean",
                        "head": {"ref": "x"}, "base": {"ref": "main"},
                        "title": "t"}
            return []

        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=fake_api), \
             patch("scripts.auto_merge_stack.git_fetch_main",
                   return_value=True):
            r = apply_merges([plan1, plan2], "t",
                              merge_method="merge",
                              continue_on_error=False)
        assert r[0].status == "blocked"
        assert r[1].status == "skipped"
        assert "halted" in r[1].error

    def test_continue_on_error_keeps_trying(self):
        from scripts.auto_merge_stack import MergePlan, apply_merges

        plan1 = MergePlan(pr_number=1, branch="feat-b-a", base="main")
        plan2 = MergePlan(pr_number=2, branch="feat-b-b", base="main")

        # plan1 blocked (no approval), plan2 approved clean → merged
        reviews_by_pr = {1: [], 2: [{
            "state": "APPROVED",
            "submitted_at": "2026-04-24T12:00:00Z",
            "user": {"login": "a"},
        }]}

        def fake_api(path, token, method="GET", body=None):
            if "/merge" in path and method == "PUT":
                return {"merged": True, "sha": "ok"}
            if path.endswith("/reviews"):
                pr_num = int(path.split("/pulls/")[1].split("/")[0])
                return reviews_by_pr.get(pr_num, [])
            if "/pulls/" in path:
                return {"state": "open", "mergeable": True,
                        "mergeable_state": "clean",
                        "head": {"ref": "x"}, "base": {"ref": "main"},
                        "title": "t"}
            return []

        with patch("scripts.auto_merge_stack.github_api",
                   side_effect=fake_api), \
             patch("scripts.auto_merge_stack.git_fetch_main",
                   return_value=True):
            r = apply_merges([plan1, plan2], "t",
                              merge_method="merge",
                              continue_on_error=True)
        assert r[0].status == "blocked"
        assert r[1].status == "merged"


# ─── github_api 429 / 5xx retry ──────────────────────────────────────────────

class TestGithubApiRetry:
    """`github_api` 429/5xx 指数退避 — 配合 cron autonomous loop 避免裸失败。"""

    def _mock_response(self, body: dict):
        import json as _j
        from unittest.mock import MagicMock
        m = MagicMock()
        m.__enter__ = lambda self_: m
        m.__exit__ = lambda *a: None
        m.read.return_value = _j.dumps(body).encode("utf-8")
        return m

    def _http_error(self, code: int, body: str = "", headers=None):
        import urllib.error as _ue
        e = _ue.HTTPError(
            "https://api.github.com/test", code,
            f"HTTP {code}", headers or {}, None)
        e.read = lambda: body.encode("utf-8")
        return e

    def test_success_no_retry(self):
        from scripts.auto_merge_stack import github_api
        with patch("scripts.auto_merge_stack.urllib.request.urlopen") as m:
            m.return_value = self._mock_response({"ok": True})
            assert github_api("/t", "tok") == {"ok": True}
            assert m.call_count == 1

    def test_429_then_success_retries(self):
        from scripts.auto_merge_stack import github_api
        waits = []
        with patch("scripts.auto_merge_stack.urllib.request.urlopen") as m, \
             patch("time.sleep", side_effect=waits.append):
            m.side_effect = [
                self._http_error(429, "rate limit", {"Retry-After": "0"}),
                self._mock_response({"ok": True}),
            ]
            assert github_api("/t", "tok",
                              max_retries=3, backoff_base=0.01) == {"ok": True}
        assert m.call_count == 2
        assert waits == [0.0]

    def test_5xx_retries_then_success(self):
        from scripts.auto_merge_stack import github_api
        with patch("scripts.auto_merge_stack.urllib.request.urlopen") as m, \
             patch("time.sleep"):
            m.side_effect = [
                self._http_error(503, "bad gateway"),
                self._mock_response({"ok": True}),
            ]
            assert github_api("/t", "tok",
                              max_retries=3, backoff_base=0.01) == {"ok": True}

    def test_429_all_fails_raises(self):
        from scripts.auto_merge_stack import github_api
        with patch("scripts.auto_merge_stack.urllib.request.urlopen") as m, \
             patch("time.sleep"):
            m.side_effect = [self._http_error(429, "limit") for _ in range(4)]
            with pytest.raises(RuntimeError, match="429"):
                github_api("/t", "tok",
                            max_retries=3, backoff_base=0.01)

    def test_4xx_not_retriable(self):
        """404 非 retriable: 直接 raise, 不 sleep, 不重试。"""
        from scripts.auto_merge_stack import github_api
        with patch("scripts.auto_merge_stack.urllib.request.urlopen") as m, \
             patch("time.sleep") as sleep_m:
            m.side_effect = self._http_error(404, "not found")
            with pytest.raises(RuntimeError, match="404"):
                github_api("/t", "tok",
                            max_retries=3, backoff_base=0.01)
            sleep_m.assert_not_called()
