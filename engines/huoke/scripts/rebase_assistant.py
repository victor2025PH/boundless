# -*- coding: utf-8 -*-
"""栈式 PR rebase 辅助工具 — A 合入 main 后 B 批量 rebase 所有栈。

动机: B 的 20+ 分支是栈式 (feat-b-chat-p3 ← p4 ← p5 ← p6 ← ... ← 工具层),
A 合入 main 新代码后,B 栈每一层都要 rebase。手动逐个 git rebase 容易
出错:
  * 栈下层未 rebase 时, 栈上层 rebase 到 main 会丢中间层 commits
  * 冲突时忘记 abort, 分支状态悬空
  * force-push 混合 rebase 容易误操作

本工具:
  1. 拉 GitHub API 查 B 分支的 base 依赖 (head → base 图)
  2. 拓扑排序: base=main 的分支先 rebase, 栈上层依赖已 rebased 的下层
  3. 每个 rebase 前 `git merge-tree` 预测冲突 — dry-run 默认
  4. `--apply` 才实际本地 rebase
  5. `--push` 才 `git push --force-with-lease` 推远端 (B PR 会自动更新)
  6. `--test` 每层 rebase 后跑 pytest 快速验收

关键安全:
  * 默认 dry-run 不写
  * Apply 前 `git branch backup-<name>-<ts>` 备份
  * 冲突 auto-abort 不留 dirty state
  * Force push 用 lease 防覆盖他人 push

用法:
    # 预测 (最常用)
    python scripts/rebase_assistant.py

    # 只 rebase 单个分支
    python scripts/rebase_assistant.py --only feat-b-chat-p3

    # 本地实际跑
    python scripts/rebase_assistant.py --apply

    # Apply + 跑测试 + 推远端
    python scripts/rebase_assistant.py --apply --test --push

    # JSON 输出
    python scripts/rebase_assistant.py --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
CYAN = "\033[36m"
RESET = "\033[0m"
BOLD = "\033[1m"

if os.environ.get("NO_COLOR") or (sys.platform == "win32" and
                                    not os.environ.get("ANSICON")):
    try:
        os.system("")
    except Exception:
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""


REPO = "victor2025PH/mobile-auto0423"
DEFAULT_HEAD_PREFIX = "feat-b-"


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API
# ─────────────────────────────────────────────────────────────────────────────

def get_token() -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=10, check=True,
        )
    except Exception:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def github_api(path: str, token: Optional[str]) -> Any:
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "rebase-assistant"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(
        f"https://api.github.com{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ─────────────────────────────────────────────────────────────────────────────
# Git 辅助
# ─────────────────────────────────────────────────────────────────────────────

def git(*args: str, check: bool = True, capture: bool = True
         ) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=capture, text=True,
        check=check, timeout=60,
    )


def git_fetch_all() -> bool:
    try:
        git("fetch", "origin", "--quiet")
        return True
    except Exception as e:
        print(f"{RED}git fetch 失败: {e}{RESET}", file=sys.stderr)
        return False


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def branch_exists_local(name: str) -> bool:
    try:
        git("rev-parse", "--verify", f"refs/heads/{name}", check=True)
        return True
    except Exception:
        return False


def branch_exists_remote(name: str) -> bool:
    try:
        git("rev-parse", "--verify", f"refs/remotes/origin/{name}", check=True)
        return True
    except Exception:
        return False


def predict_rebase_conflict(branch: str, onto: str) -> List[str]:
    """用 git merge-tree 预测 rebase branch onto <onto> 是否冲突。

    返回冲突文件列表; 空 = 无冲突。不实际动分支。
    """
    try:
        # git merge-tree --write-tree --name-only onto branch
        # 输出冲突文件,空则无冲突
        r = git("merge-tree", "--write-tree", "--name-only",
                 f"origin/{onto}", f"origin/{branch}",
                 check=False)
        if r.returncode == 0:
            # 干净, 只输出 tree SHA
            return []
        # 冲突输出: 第一行 tree SHA, 后面是冲突文件
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        # 去掉第一行 tree SHA
        return lines[1:] if len(lines) > 1 else []
    except Exception as e:
        return [f"(merge-tree 检查失败: {e})"]


def _classify_rebase_output(stdout: str, stderr: str) -> Dict[str, Any]:
    """解析 git rebase 的 stdout/stderr, 区分三类信号:

      * skipped_commits: `warning: skipped previously applied commit <sha>`
        — 栈式 rebase 很常见, **不是失败原因**
      * conflict_files: `CONFLICT (...): Merge conflict in <path>` 或
        `Merge conflict in <path>` — 真文件级冲突
      * has_real_conflict: 有冲突文件或输出里含 `could not apply`

    只有 (returncode != 0 且 has_real_conflict) 才是真正的失败。
    单纯 skipped 常和 returncode==0 一起出现 — 老工具把 skipped 误当失败正是
    因为没区分这三者。
    """
    combined = ((stdout or "") + "\n" + (stderr or "")).strip()
    skipped: List[str] = []
    conflict_files: List[str] = []
    for raw_line in combined.splitlines():
        line = raw_line.strip()
        if "skipped previously applied commit" in line:
            parts = line.split()
            if parts:
                skipped.append(parts[-1][:12])
            continue
        if "Merge conflict in " in line:
            fpart = line.split("Merge conflict in ", 1)[1].strip()
            if fpart:
                conflict_files.append(fpart)
            continue
        if line.startswith("CONFLICT") and " in " in line:
            fpart = line.split(" in ", 1)[1].strip().rstrip(":")
            if fpart:
                conflict_files.append(fpart)
    # 去重保序
    conflict_files = list(dict.fromkeys(conflict_files))
    has_real_conflict = bool(conflict_files) or "could not apply" in combined
    return {
        "skipped_commits": skipped,
        "conflict_files": conflict_files,
        "has_real_conflict": has_real_conflict,
        "raw": combined[-400:],
    }


def do_rebase(branch: str, onto: str,
              backup_prefix: str = "rebase-backup") -> Tuple[bool, str]:
    """实际 rebase branch onto origin/<onto>。成功 return (True, backup_note),
    失败 auto-abort + return (False, reason)。

    Self-healing: 若首次 rebase 失败且输出含 `skipped previously applied` +
    真冲突 (多半是栈式 rebase 的 upstream 算错), 自动用 `--fork-point` 重试一次。
    这复现了手解 #13/#14/#15 时用的 recovery 手法。
    """
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = f"{backup_prefix}-{branch}-{ts}"
    try:
        # 拉远端到本地
        if not branch_exists_local(branch):
            git("checkout", "-B", branch, f"origin/{branch}")
        else:
            git("checkout", branch)
            # 若本地已存在, 对齐到远端 (避免本地有未推的 commits 干扰)
            git("reset", "--hard", f"origin/{branch}")
        # backup
        git("branch", backup)

        # 第一次尝试: 普通 rebase
        r = git("rebase", f"origin/{onto}", check=False)
        cls = _classify_rebase_output(r.stdout, r.stderr)
        if r.returncode == 0:
            if cls["skipped_commits"]:
                return True, (f"{backup} "
                               f"(skipped {len(cls['skipped_commits'])} 已应用 commits)")
            return True, backup

        # rebase 状态机半成品, 先 abort
        git("rebase", "--abort", check=False)

        # 自动恢复: skipped + 真冲突 → 多半是栈式 rebase 的 upstream 算错,
        # --fork-point 让 git 靠 reflog 找真正的分叉点
        if cls["skipped_commits"] and cls["has_real_conflict"]:
            r2 = git("rebase", "--fork-point", f"origin/{onto}",
                      check=False)
            cls2 = _classify_rebase_output(r2.stdout, r2.stderr)
            if r2.returncode == 0:
                note = " (--fork-point 自动恢复"
                if cls2["skipped_commits"]:
                    note += f", skipped {len(cls2['skipped_commits'])}"
                note += ")"
                return True, f"{backup}{note}"
            git("rebase", "--abort", check=False)
            flist = cls2["conflict_files"][:3] or cls["conflict_files"][:3]
            if flist:
                return False, (f"真冲突 (--fork-point 恢复后仍冲突): "
                                f"{', '.join(flist)}")
            return False, f"rebase 失败: {cls2['raw'][-150:]}"

        # 纯粹真冲突, 无 skipped — 直接报告冲突文件
        flist = cls["conflict_files"][:3]
        if flist:
            return False, f"冲突: {', '.join(flist)}"
        return False, (cls["raw"][-150:] or "unknown")
    except Exception as e:
        try:
            git("rebase", "--abort", check=False)
        except Exception:
            pass
        return False, str(e)[:200]


def cleanup_old_backups(prefix: str = "rebase-backup",
                         keep_days: int = 7) -> List[str]:
    """删 `<prefix>-*-YYYYMMDDHHMMSS` 形式的老备份分支 (> keep_days 天)。

    Return 删掉的分支名列表。保留最近 keep_days 天的便于回滚。
    """
    try:
        r = git("for-each-ref", "--format=%(refname:short)",
                 "refs/heads/", check=False)
    except Exception:
        return []
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=keep_days)
    removed: List[str] = []
    for name in r.stdout.splitlines():
        name = name.strip()
        if not name.startswith(f"{prefix}-"):
            continue
        # 尾部 14 位数字 = 时间戳
        ts = name[-14:]
        if len(ts) != 14 or not ts.isdigit():
            continue
        try:
            t = _dt.datetime.strptime(ts, "%Y%m%d%H%M%S").replace(
                tzinfo=_dt.timezone.utc)
        except ValueError:
            continue
        if t >= cutoff:
            continue
        try:
            git("branch", "-D", name, check=False)
            removed.append(name)
        except Exception:
            pass
    return removed


def run_tests_quick() -> Tuple[bool, str]:
    """跑 pytest -q 快速测试, 返回 (ok, summary)。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q",
             "--tb=line", "-p", "no:warnings",
             "--ignore=tests/e2e",
             "-k", "not real",
             "-x",
             ],
            capture_output=True, text=True,
            timeout=300,
        )
        last_line = ""
        for line in reversed(r.stdout.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                last_line = line.strip()
                break
        return (r.returncode == 0, last_line or "(no summary)")
    except Exception as e:
        return (False, f"run 异常: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# 规划
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RebasePlan:
    branch: str
    base: str           # 新 base (rebase onto 的目标)
    pr_number: Optional[int] = None
    status: str = "pending"   # pending/dry_ok/dry_conflict/applied/failed/skipped
    predicted_conflicts: List[str] = field(default_factory=list)
    applied_backup: str = ""
    test_result: str = ""
    error: str = ""


def collect_plans(token: Optional[str],
                   head_prefix: str = DEFAULT_HEAD_PREFIX,
                   only_branch: Optional[str] = None) -> List[RebasePlan]:
    """调 GitHub API 拿所有 B open PR, 按栈依赖排序生成 RebasePlan 列表。

    排序策略:
      * base=main 的先
      * base=其他 B 分支的后 (其 base 的 PR 已在前)
    """
    prs = github_api(
        f"/repos/{REPO}/pulls?state=open&per_page=100", token)
    # 只保留 head 以指定前缀开头
    b_prs = [p for p in prs if p["head"]["ref"].startswith(head_prefix)]
    if only_branch:
        b_prs = [p for p in b_prs if p["head"]["ref"] == only_branch]

    # 构建 head → pr_dict 索引
    by_head = {p["head"]["ref"]: p for p in b_prs}

    # 拓扑排序
    ordered: List[Dict] = []
    placed: Set[str] = set()

    # 第一轮: base=main
    for p in b_prs:
        if p["base"]["ref"] == "main":
            ordered.append(p)
            placed.add(p["head"]["ref"])

    # 递推: base 已 placed 的加入
    branch_set = {p["head"]["ref"] for p in b_prs}

    def _recurse():
        """循环加入 base=main 或 base∈placed 的 PR, 直到无变化。"""
        changed_any = True
        while changed_any:
            changed_any = False
            for _p in b_prs:
                h = _p["head"]["ref"]
                b = _p["base"]["ref"]
                if h in placed:
                    continue
                if b == "main" or b in placed:
                    ordered.append(_p)
                    placed.add(h)
                    changed_any = True

    _recurse()

    # 剩下未 placed 的分 orphan 和依赖 orphan 的两种:
    # orphan = base 不在 open PR 集合中 (base 已被合并); 先加 orphan,
    # 然后再递推一次让依赖 orphan 的 PR 也被 placed。
    # 若仍有剩余 (环? 理论不存在), 按 b_prs 原序兜底。
    while True:
        progress = False
        for p in b_prs:
            head = p["head"]["ref"]
            base = p["base"]["ref"]
            if head in placed:
                continue
            if base not in branch_set:
                # orphan — base 已合并或从未在本批, 先放进来
                ordered.append(p)
                placed.add(head)
                progress = True
        if not progress:
            break
        _recurse()  # 依赖新 placed 的 orphan 的 PR 也能进

    # 兜底: 剩余 (极少见, 通常是循环依赖)
    for p in b_prs:
        if p["head"]["ref"] not in placed:
            ordered.append(p)
            placed.add(p["head"]["ref"])

    plans: List[RebasePlan] = []
    for p in ordered:
        plans.append(RebasePlan(
            branch=p["head"]["ref"],
            base=p["base"]["ref"],
            pr_number=p["number"],
        ))
    return plans


# ─────────────────────────────────────────────────────────────────────────────
# Dry-run + Apply
# ─────────────────────────────────────────────────────────────────────────────

def dry_run(plans: List[RebasePlan]) -> List[RebasePlan]:
    """对每个 plan 跑 git merge-tree 预测冲突, 更新 plan.status + predicted_conflicts。"""
    for p in plans:
        # base=main → 预测 rebase onto main
        conflicts = predict_rebase_conflict(p.branch, p.base)
        p.predicted_conflicts = conflicts
        p.status = "dry_conflict" if conflicts else "dry_ok"
    return plans


def apply_rebase(plans: List[RebasePlan], run_test: bool,
                 push: bool) -> List[RebasePlan]:
    """按栈顺序实际 rebase。每层冲突就 skip 后续依赖它的, 但继续跑兄弟分支。"""
    original_branch = current_branch()
    failed_branches: Set[str] = set()
    try:
        for p in plans:
            # 如果此分支的 base 在失败列表 → 链条断, skip
            if p.base != "main" and p.base in failed_branches:
                p.status = "skipped"
                p.error = f"base {p.base} 失败, 链条中断"
                failed_branches.add(p.branch)
                continue

            ok, info = do_rebase(p.branch, p.base)
            if not ok:
                p.status = "failed"
                p.error = info
                failed_branches.add(p.branch)
                continue
            p.status = "applied"
            p.applied_backup = info

            if run_test:
                test_ok, test_sum = run_tests_quick()
                p.test_result = test_sum
                if not test_ok:
                    p.status = "failed"
                    p.error = f"test failed: {test_sum}"
                    failed_branches.add(p.branch)
                    continue

            if push:
                try:
                    git("push", "--force-with-lease", "origin",
                         p.branch)
                except Exception as e:
                    p.status = "failed"
                    p.error = f"push failed: {str(e)[:80]}"
                    failed_branches.add(p.branch)
    finally:
        # 回到原分支
        if original_branch:
            try:
                git("checkout", original_branch, check=False)
            except Exception:
                pass
    return plans


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def render(plans: List[RebasePlan], applied: bool) -> str:
    lines: List[str] = []
    title = "Rebase Assistant — Apply Results" if applied \
        else "Rebase Assistant — Dry Run"
    lines.append(f"\n{BOLD}=== {title} ==={RESET}\n")

    if not plans:
        lines.append(f"{YELLOW}(无匹配的 B PR){RESET}")
        return "\n".join(lines)

    for p in plans:
        status_color = {
            "dry_ok":       GREEN,
            "applied":      GREEN,
            "dry_conflict": YELLOW,
            "skipped":      YELLOW,
            "failed":       RED,
            "pending":      "",
        }.get(p.status, "")
        tag = f"{status_color}[{p.status:13}]{RESET}"
        pr_tag = f"PR#{p.pr_number}" if p.pr_number else "    "
        lines.append(f"  {tag} {pr_tag}  {p.branch:35s} → {p.base}")
        if p.predicted_conflicts and applied is False:
            lines.append(f"      {YELLOW}预测冲突文件:{RESET}")
            for f in p.predicted_conflicts[:8]:
                lines.append(f"        • {f}")
            if len(p.predicted_conflicts) > 8:
                lines.append(f"        ... 省略 {len(p.predicted_conflicts) - 8} 个")
        if p.applied_backup:
            lines.append(f"      {BLUE}备份: {p.applied_backup}{RESET}")
        if p.test_result:
            lines.append(f"      test: {p.test_result}")
        if p.error:
            lines.append(f"      {RED}error: {p.error}{RESET}")

    # 汇总
    count = {}
    for p in plans:
        count[p.status] = count.get(p.status, 0) + 1
    lines.append("")
    lines.append(f"{BOLD}汇总:{RESET}")
    for status in ("dry_ok", "applied", "dry_conflict",
                    "skipped", "failed"):
        n = count.get(status, 0)
        if n == 0:
            continue
        color = {"dry_ok": GREEN, "applied": GREEN,
                 "dry_conflict": YELLOW, "skipped": YELLOW,
                 "failed": RED}.get(status, "")
        lines.append(f"  {color}{status}{RESET}: {n}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="栈式 PR rebase 辅助 — dry-run 预测冲突,显式 --apply 才动分支")
    parser.add_argument("--head-prefix", default=DEFAULT_HEAD_PREFIX,
                        help="过滤 head 前缀 (默认 feat-b-)")
    parser.add_argument("--only", default=None,
                        help="只 rebase 指定分支")
    parser.add_argument("--apply", action="store_true",
                        help="实际本地 rebase (默认 dry-run)")
    parser.add_argument("--test", action="store_true",
                        help="--apply 时每层 rebase 后跑 pytest")
    parser.add_argument("--push", action="store_true",
                        help="--apply 时 force-with-lease 推远端")
    parser.add_argument("--no-fetch", action="store_true",
                        help="跳过 git fetch (调试用)")
    parser.add_argument("--cleanup-backups", action="store_true",
                        help="删 >7 天的 rebase-backup-* 本地分支后直接退出")
    parser.add_argument("--cleanup-keep-days", type=int, default=7,
                        help="--cleanup-backups 保留天数 (默认 7)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, CYAN, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""

    if args.push and not args.apply:
        parser.error("--push 需要 --apply")
    if args.test and not args.apply:
        parser.error("--test 需要 --apply")

    if args.cleanup_backups:
        removed = cleanup_old_backups(keep_days=args.cleanup_keep_days)
        if removed:
            print(f"{GREEN}清理 {len(removed)} 个老备份分支:{RESET}")
            for n in removed:
                print(f"  - {n}")
        else:
            print(f"{YELLOW}(没有老于 {args.cleanup_keep_days} 天的备份分支){RESET}")
        return 0

    if not args.no_fetch:
        git_fetch_all()

    token = get_token()
    try:
        plans = collect_plans(token, args.head_prefix, args.only)
    except Exception as e:
        print(f"{RED}ERROR:{RESET} 拉 PR 列表失败: {e}", file=sys.stderr)
        return 1

    if not plans:
        print(f"{YELLOW}(没有匹配的 B PR){RESET}")
        return 0

    # dry run 先 (永远跑, apply 前看预测)
    plans = dry_run(plans)

    if args.apply:
        plans = apply_rebase(plans, run_test=args.test, push=args.push)

    if args.json:
        out = [{
            "branch": p.branch, "base": p.base, "pr_number": p.pr_number,
            "status": p.status,
            "predicted_conflicts": p.predicted_conflicts,
            "applied_backup": p.applied_backup,
            "test_result": p.test_result,
            "error": p.error,
        } for p in plans]
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(plans, applied=args.apply))

    # exit code: failed 有值返 1
    any_failed = any(p.status == "failed" for p in plans)
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
