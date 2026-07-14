# -*- coding: utf-8 -*-
"""P15 `scripts/rebase_assistant.py` 单元测试。

只测纯逻辑 (collect_plans 拓扑排序 / render), git 和 GitHub API 用 mock。
do_rebase / apply_rebase 不测 — 会动真分支, 由手动 --apply 验证。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# P2-⑫: spawn 子 Python 进程时强制 UTF-8 防 Windows cp936 emoji 解码挂.
_UTF8_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "rebase_assistant.py"


# ─── CLI 参数保护 ────────────────────────────────────────────────────────────

class TestCliArgs:
    def test_push_requires_apply(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--push", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 2
        assert "--push" in (r.stderr + r.stdout)

    def test_test_requires_apply(self):
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--test", "--no-color"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=_UTF8_ENV, timeout=30,
        )
        assert r.returncode == 2


# ─── RebasePlan dataclass ────────────────────────────────────────────────────

class TestRebasePlan:
    def test_default_pending(self):
        from scripts.rebase_assistant import RebasePlan
        p = RebasePlan(branch="x", base="main")
        assert p.status == "pending"
        assert p.predicted_conflicts == []


# ─── collect_plans 拓扑排序 ──────────────────────────────────────────────────

class TestCollectPlans:
    def _pr(self, num, head, base):
        return {"number": num,
                "head": {"ref": head},
                "base": {"ref": base}}

    def test_empty(self):
        from scripts.rebase_assistant import collect_plans
        with patch("scripts.rebase_assistant.github_api",
                   return_value=[]):
            plans = collect_plans(token="fake")
        assert plans == []

    def test_single_base_main(self):
        from scripts.rebase_assistant import collect_plans
        prs = [self._pr(1, "feat-b-a", "main")]
        with patch("scripts.rebase_assistant.github_api", return_value=prs):
            plans = collect_plans(token="fake")
        assert len(plans) == 1
        assert plans[0].branch == "feat-b-a"
        assert plans[0].base == "main"

    def test_stack_ordered_correctly(self):
        """p3 (base=main) ← p4 (base=p3) ← p5 (base=p4) 应按 p3 → p4 → p5 排。"""
        from scripts.rebase_assistant import collect_plans
        prs = [
            self._pr(5, "feat-b-chat-p5", "feat-b-chat-p4"),
            self._pr(3, "feat-b-chat-p3", "main"),
            self._pr(4, "feat-b-chat-p4", "feat-b-chat-p3"),
        ]
        with patch("scripts.rebase_assistant.github_api", return_value=prs):
            plans = collect_plans(token="fake")
        branches = [p.branch for p in plans]
        # p3 必须在 p4 之前,p4 在 p5 之前
        assert branches.index("feat-b-chat-p3") < branches.index("feat-b-chat-p4")
        assert branches.index("feat-b-chat-p4") < branches.index("feat-b-chat-p5")

    def test_non_feat_b_filtered(self):
        from scripts.rebase_assistant import collect_plans
        prs = [
            self._pr(1, "feat-b-chat-p1", "main"),
            self._pr(2, "feat-a-phase3", "main"),  # 过滤掉
            self._pr(3, "random-branch", "main"),  # 过滤掉
        ]
        with patch("scripts.rebase_assistant.github_api", return_value=prs):
            plans = collect_plans(token="fake", head_prefix="feat-b-")
        assert len(plans) == 1
        assert plans[0].branch == "feat-b-chat-p1"

    def test_only_filter(self):
        from scripts.rebase_assistant import collect_plans
        prs = [
            self._pr(1, "feat-b-a", "main"),
            self._pr(2, "feat-b-b", "main"),
            self._pr(3, "feat-b-c", "main"),
        ]
        with patch("scripts.rebase_assistant.github_api", return_value=prs):
            plans = collect_plans(token="fake", only_branch="feat-b-b")
        assert len(plans) == 1
        assert plans[0].branch == "feat-b-b"

    def test_orphan_base_placed_at_end(self):
        """base 不在 open PR 集合中 (base 已合并), 应放末尾不失序。"""
        from scripts.rebase_assistant import collect_plans
        prs = [
            self._pr(1, "feat-b-a", "main"),
            self._pr(2, "feat-b-b", "feat-b-merged-base"),  # base 已合并
        ]
        with patch("scripts.rebase_assistant.github_api", return_value=prs):
            plans = collect_plans(token="fake")
        # feat-b-a 先, feat-b-b 后 (orphan)
        assert plans[0].branch == "feat-b-a"
        assert plans[1].branch == "feat-b-b"


# ─── _classify_rebase_output (纯解析, 可测) ─────────────────────────────────

class TestClassifyRebaseOutput:
    def test_empty(self):
        from scripts.rebase_assistant import _classify_rebase_output
        r = _classify_rebase_output("", "")
        assert r["skipped_commits"] == []
        assert r["conflict_files"] == []
        assert r["has_real_conflict"] is False

    def test_skipped_only_no_conflict(self):
        """只有 skipped previously applied warnings — 不是失败原因。"""
        from scripts.rebase_assistant import _classify_rebase_output
        stderr = (
            "warning: skipped previously applied commit a83c7ef\n"
            "warning: skipped previously applied commit 746bd26\n"
        )
        r = _classify_rebase_output("", stderr)
        assert len(r["skipped_commits"]) == 2
        assert r["skipped_commits"][0] == "a83c7ef"
        assert r["conflict_files"] == []
        assert r["has_real_conflict"] is False

    def test_real_conflict_detected(self):
        from scripts.rebase_assistant import _classify_rebase_output
        out = (
            "Auto-merging src/foo.py\n"
            "CONFLICT (content): Merge conflict in src/foo.py\n"
            "error: could not apply abc1234... feat: foo\n"
        )
        r = _classify_rebase_output(out, "")
        assert r["has_real_conflict"] is True
        assert "src/foo.py" in r["conflict_files"]

    def test_mixed_skipped_and_conflict_triggers_recovery_path(self):
        """栈式常见: 先 skipped 几个已应用 commits, 然后下游某 commit 冲突。"""
        from scripts.rebase_assistant import _classify_rebase_output
        out = (
            "warning: skipped previously applied commit a83c7ef\n"
            "warning: skipped previously applied commit 746bd26\n"
            "Auto-merging src/bar.py\n"
            "CONFLICT (content): Merge conflict in src/bar.py\n"
            "error: could not apply xyz9012... feat: bar\n"
        )
        r = _classify_rebase_output(out, "")
        assert len(r["skipped_commits"]) == 2
        assert "src/bar.py" in r["conflict_files"]
        assert r["has_real_conflict"] is True

    def test_could_not_apply_without_file_still_real(self):
        """某些 rebase 失败只输出 could not apply 不列具体文件, 也应判定为真。"""
        from scripts.rebase_assistant import _classify_rebase_output
        out = "error: could not apply abc1234\n"
        r = _classify_rebase_output(out, "")
        assert r["has_real_conflict"] is True

    def test_conflict_files_deduplicated(self):
        from scripts.rebase_assistant import _classify_rebase_output
        out = (
            "CONFLICT (content): Merge conflict in src/x.py\n"
            "Auto-merging src/x.py\n"
            "Merge conflict in src/x.py\n"
        )
        r = _classify_rebase_output(out, "")
        assert r["conflict_files"].count("src/x.py") == 1


# ─── predict_rebase_conflict ────────────────────────────────────────────────

class TestPredictRebaseConflict:
    def test_same_branch_no_conflict(self):
        """main rebased onto main 无冲突。"""
        from scripts.rebase_assistant import predict_rebase_conflict
        conflicts = predict_rebase_conflict("main", "main")
        # git merge-tree 对同一 ref 返空
        assert conflicts == []

    def test_nonexistent_branch_returns_some(self):
        """不存在的分支应 graceful 返 error 字符串而非崩溃。"""
        from scripts.rebase_assistant import predict_rebase_conflict
        # 不崩即可
        r = predict_rebase_conflict("nonexistent-xyz", "main")
        # 可能空可能含错误,不崩即通过
        assert isinstance(r, list)


# ─── dry_run ─────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_updates_status_to_dry_ok_or_conflict(self):
        from scripts.rebase_assistant import RebasePlan, dry_run
        plans = [RebasePlan(branch="main", base="main")]
        with patch("scripts.rebase_assistant.predict_rebase_conflict",
                   return_value=[]):
            r = dry_run(plans)
        assert r[0].status == "dry_ok"

    def test_conflict_sets_status(self):
        from scripts.rebase_assistant import RebasePlan, dry_run
        plans = [RebasePlan(branch="x", base="main")]
        with patch("scripts.rebase_assistant.predict_rebase_conflict",
                   return_value=["file1.py", "file2.py"]):
            r = dry_run(plans)
        assert r[0].status == "dry_conflict"
        assert r[0].predicted_conflicts == ["file1.py", "file2.py"]


# ─── render ──────────────────────────────────────────────────────────────────

class TestRender:
    def test_render_dry_ok(self):
        from scripts.rebase_assistant import RebasePlan, render
        plans = [RebasePlan(branch="feat-b-x", base="main",
                             pr_number=42, status="dry_ok")]
        txt = render(plans, applied=False)
        assert "Dry Run" in txt
        assert "dry_ok" in txt
        assert "PR#42" in txt
        assert "feat-b-x" in txt

    def test_render_conflict_shows_files(self):
        from scripts.rebase_assistant import RebasePlan, render
        plans = [RebasePlan(branch="x", base="main", pr_number=1,
                             status="dry_conflict",
                             predicted_conflicts=["src/a.py", "src/b.py"])]
        txt = render(plans, applied=False)
        assert "dry_conflict" in txt
        assert "src/a.py" in txt
        assert "src/b.py" in txt

    def test_render_applied_skips_predicted_section(self):
        """applied 模式下不再显示预测冲突 (实际结果会显示 error)。"""
        from scripts.rebase_assistant import RebasePlan, render
        plans = [RebasePlan(branch="x", base="main",
                             status="applied",
                             predicted_conflicts=["src/a.py"],
                             applied_backup="backup-x-20260424000000")]
        txt = render(plans, applied=True)
        assert "Apply" in txt
        # predicted conflicts 应该不在 apply 模式显示
        assert "src/a.py" not in txt
        # 备份信息应显示
        assert "backup" in txt.lower() or "备份" in txt

    def test_render_failed_shows_error(self):
        from scripts.rebase_assistant import RebasePlan, render
        plans = [RebasePlan(branch="x", base="main",
                             status="failed",
                             error="merge conflict")]
        txt = render(plans, applied=True)
        assert "failed" in txt
        assert "merge conflict" in txt

    def test_render_empty_plans(self):
        from scripts.rebase_assistant import render
        txt = render([], applied=False)
        assert "无匹配" in txt or "B PR" in txt

    def test_render_summary_counts(self):
        from scripts.rebase_assistant import RebasePlan, render
        plans = [
            RebasePlan(branch="a", base="main", status="dry_ok"),
            RebasePlan(branch="b", base="main", status="dry_ok"),
            RebasePlan(branch="c", base="main",
                        status="dry_conflict",
                        predicted_conflicts=["x.py"]),
        ]
        txt = render(plans, applied=False)
        assert "dry_ok" in txt
        assert "dry_conflict" in txt


# ─── cleanup_old_backups ────────────────────────────────────────────────────

class TestCleanupOldBackups:
    def test_no_matching_branches_returns_empty(self):
        from scripts.rebase_assistant import cleanup_old_backups
        fake = MagicMock()
        fake.stdout = "main\nfeat-b-chat-p3\nother-branch\n"
        with patch("scripts.rebase_assistant.git", return_value=fake):
            r = cleanup_old_backups(prefix="rebase-backup", keep_days=7)
        assert r == []

    def test_recent_backup_kept(self):
        """刚创建的备份 (时间戳在 keep_days 内) 不删。"""
        import datetime as dt
        from scripts.rebase_assistant import cleanup_old_backups
        now = dt.datetime.now(dt.timezone.utc)
        recent_ts = now.strftime("%Y%m%d%H%M%S")
        fake = MagicMock()
        fake.stdout = f"rebase-backup-feat-b-x-{recent_ts}\n"
        with patch("scripts.rebase_assistant.git", return_value=fake):
            r = cleanup_old_backups(prefix="rebase-backup", keep_days=7)
        assert r == []

    def test_old_backup_deleted(self):
        """14 天前的备份应删。"""
        import datetime as dt
        from scripts.rebase_assistant import cleanup_old_backups
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)
        old_ts = old.strftime("%Y%m%d%H%M%S")
        fake = MagicMock()
        fake.stdout = f"rebase-backup-feat-b-x-{old_ts}\n"
        with patch("scripts.rebase_assistant.git", return_value=fake) as mg:
            r = cleanup_old_backups(prefix="rebase-backup", keep_days=7)
        assert len(r) == 1
        assert r[0] == f"rebase-backup-feat-b-x-{old_ts}"
        # 第二次调用应该是 branch -D
        calls = [c.args for c in mg.call_args_list]
        assert any("branch" in c and "-D" in c for c in calls)

    def test_malformed_timestamp_ignored(self):
        """名字像 backup 但时间戳不合法的 (手工创建) 不删。"""
        from scripts.rebase_assistant import cleanup_old_backups
        fake = MagicMock()
        fake.stdout = "rebase-backup-feat-b-x-abcdefghijklmn\n"
        with patch("scripts.rebase_assistant.git", return_value=fake):
            r = cleanup_old_backups(prefix="rebase-backup", keep_days=7)
        assert r == []


# ─── 实仓拓扑排序验证 (不跑 git 命令, 只拉 PR 结构) ─────────────────────────

class TestIntegrationOrderOnRealRepo:
    """用实仓 PR 图验证拓扑排序不错位。不跑 git, 只调 API。"""
    def test_real_repo_pr_topo_order(self):
        from scripts.rebase_assistant import collect_plans, get_token
        token = get_token()
        try:
            plans = collect_plans(token)
        except Exception:
            pytest.skip("GitHub API 不可用或无 B PR")

        if not plans:
            pytest.skip("repo 里无 feat-b- PR")

        branch_set = {p.branch for p in plans}
        placed: set = set()
        for p in plans:
            # base 要么是 main, 要么是已 placed 的 B 分支, 要么是 orphan
            assert p.base == "main" or p.base in placed \
                or p.base not in branch_set, \
                f"拓扑顺序错误: {p.branch} base={p.base} 但未 placed"
            placed.add(p.branch)
