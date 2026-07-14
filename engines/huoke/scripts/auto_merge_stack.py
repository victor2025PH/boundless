# -*- coding: utf-8 -*-
"""栈式 PR 自动合并工具 — A review 通过后按拓扑序批量合 B 栈。

配套 `rebase_assistant.py` (拓扑排序 + rebase) 和 `check_a_activity.py`
(review 就绪判定) 的最后一步: 合并。

工作流:
  1. 拉 GitHub API 列 feat-b-* open PRs
  2. 拓扑排序 (base=main 先, 栈上层后)
  3. 按顺序: 检查就绪 (open / APPROVED / mergeable) → 若父 PR 已合就 re-target
     base 到 main → `PUT /pulls/:n/merge` 合入
  4. 每合 1 个本地 git fetch 更新 origin/main 供下一个用
  5. 任一 PR 失败即停 (避免级联坏合)

安全:
  * 默认 dry-run 只列计划不真合
  * `--apply` 才实际调 merge API
  * `--only N1,N2` 限定特定 PR
  * `--merge-method merge|squash|rebase` 默认 merge (匹配此 repo 既有风格)
  * `--continue-on-error` 允许失败不终止 (默认严格停)

用法:
    # 预演
    python scripts/auto_merge_stack.py

    # 只看特定 PR
    python scripts/auto_merge_stack.py --only 6,7,1

    # 真合
    python scripts/auto_merge_stack.py --apply

    # JSON
    python scripts/auto_merge_stack.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


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

# A 在 PR #26 反馈: GitHub 不允许 author 自审 (A/B 共用 victor2025PH token),
# APPROVED state 走不通。A 承诺每次 approve-equivalent 评论必带 marker。
APPROVE_EQUIVALENT_MARKERS = (
    "✅ A 侧 review 通过",
    "approve-equivalent",
)


def _is_approve_equivalent(review: Dict[str, Any]) -> bool:
    """state=APPROVED 或 state=COMMENTED 且 body 含 approve marker。"""
    state = review.get("state", "")
    if state == "APPROVED":
        return True
    if state == "COMMENTED":
        body = review.get("body") or ""
        return any(m in body for m in APPROVE_EQUIVALENT_MARKERS)
    return False


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


def github_api(path: str, token: Optional[str],
                method: str = "GET",
                body: Optional[Dict[str, Any]] = None,
                *, max_retries: int = 3,
                backoff_base: float = 2.0) -> Any:
    """GitHub API 调用, HTTP 429 / 5xx 指数退避 retry (cron 自动化避免裸失败).

    Retry-After header 优先, 否则用 backoff_base * 2^attempt, 单次 cap 30s。
    max_retries=3 总 wait ≤ ~14s。非重试错误或用尽次数 → raise RuntimeError。
    """
    import time
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "auto-merge-stack"}
    if token:
        headers["Authorization"] = f"token {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://api.github.com{path}", headers=headers,
        method=method, data=data)
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            status = e.code
            retriable = status == 429 or 500 <= status < 600
            if not retriable or attempt == max_retries:
                raise RuntimeError(
                    f"{method} {path}: {status} {e.read().decode()[:200]}")
            retry_after_hdr = (e.headers.get("Retry-After")
                                if e.headers else None)
            try:
                wait = (float(retry_after_hdr) if retry_after_hdr
                        else backoff_base * (2 ** attempt))
            except (TypeError, ValueError):
                wait = backoff_base * (2 ** attempt)
            time.sleep(min(wait, 30.0))


# ─────────────────────────────────────────────────────────────────────────────
# Git 辅助
# ─────────────────────────────────────────────────────────────────────────────

def git_fetch_main() -> bool:
    try:
        subprocess.run(
            ["git", "fetch", "origin", "main", "--quiet"],
            check=True, timeout=30,
        )
        return True
    except Exception as e:
        print(f"{YELLOW}[WARN]{RESET} git fetch main 失败: {e}",
              file=sys.stderr)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 拓扑 + Plan
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MergePlan:
    pr_number: int
    branch: str
    base: str
    title: str = ""
    mergeable: Optional[bool] = None
    mergeable_state: str = "unknown"
    approved_by_a: bool = False
    has_changes_requested: bool = False
    latest_review_state: Optional[str] = None
    retarget_base_to: Optional[str] = None  # 若需改 base, 目标 ref
    status: str = "pending"
    # pending / ready / blocked / merged / retargeted / failed / skipped
    error: str = ""
    merge_sha: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "branch": self.branch,
            "base": self.base,
            "title": self.title,
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "approved_by_a": self.approved_by_a,
            "has_changes_requested": self.has_changes_requested,
            "latest_review_state": self.latest_review_state,
            "retarget_base_to": self.retarget_base_to,
            "status": self.status,
            "error": self.error,
            "merge_sha": self.merge_sha,
        }


def collect_plans(token: Optional[str],
                   head_prefix: str = DEFAULT_HEAD_PREFIX,
                   only_prs: Optional[Set[int]] = None
                   ) -> List[MergePlan]:
    """拓扑排序 B open PRs, base=main 先, 栈上层依赖其 base 已 placed。"""
    prs = github_api(
        f"/repos/{REPO}/pulls?state=open&per_page=100", token)
    b_prs = [p for p in prs if p["head"]["ref"].startswith(head_prefix)]
    if only_prs:
        b_prs = [p for p in b_prs if p["number"] in only_prs]

    by_head = {p["head"]["ref"]: p for p in b_prs}
    branch_set = set(by_head)
    ordered: List[Dict] = []
    placed: Set[str] = set()

    # 第一轮: base=main
    for p in b_prs:
        if p["base"]["ref"] == "main":
            ordered.append(p)
            placed.add(p["head"]["ref"])

    def _recurse():
        changed = True
        while changed:
            changed = False
            for _p in b_prs:
                h = _p["head"]["ref"]
                b = _p["base"]["ref"]
                if h in placed:
                    continue
                if b == "main" or b in placed:
                    ordered.append(_p)
                    placed.add(h)
                    changed = True

    _recurse()

    # orphan 兜底 (base 已合/不存在)
    while True:
        progress = False
        for p in b_prs:
            if p["head"]["ref"] in placed:
                continue
            if p["base"]["ref"] not in branch_set:
                ordered.append(p)
                placed.add(p["head"]["ref"])
                progress = True
        if not progress:
            break
        _recurse()

    for p in b_prs:
        if p["head"]["ref"] not in placed:
            ordered.append(p)
            placed.add(p["head"]["ref"])

    return [MergePlan(pr_number=p["number"],
                       branch=p["head"]["ref"],
                       base=p["base"]["ref"],
                       title=(p.get("title") or "")[:80])
            for p in ordered]


# ─────────────────────────────────────────────────────────────────────────────
# Readiness 检查
# ─────────────────────────────────────────────────────────────────────────────

def check_readiness(plan: MergePlan, token: Optional[str],
                     merged_branches: Set[str]) -> None:
    """写 plan.mergeable / approved_by_a / retarget_base_to / status / error。"""
    try:
        pr = github_api(
            f"/repos/{REPO}/pulls/{plan.pr_number}", token)
    except Exception as e:
        plan.status = "failed"
        plan.error = f"pr meta: {str(e)[:80]}"
        return
    if pr.get("state") != "open":
        plan.status = "skipped"
        plan.error = f"state={pr.get('state')}"
        return
    plan.mergeable = pr.get("mergeable")
    plan.mergeable_state = pr.get("mergeable_state") or "unknown"

    # reviews: APPROVED 和 CHANGES_REQUESTED (按 submitted_at 最新)
    try:
        reviews = github_api(
            f"/repos/{REPO}/pulls/{plan.pr_number}/reviews", token) or []
    except Exception as e:
        plan.status = "failed"
        plan.error = f"reviews: {str(e)[:80]}"
        return

    latest_by_user: Dict[str, Dict[str, Any]] = {}
    for r in reviews:
        user = (r.get("user") or {}).get("login", "")
        state = r.get("state", "")
        # COMMENTED + approve-equivalent marker → 视同 APPROVED (GitHub 不让
        # author 自审, A/B 共用同 token 时唯一出路)
        if state == "COMMENTED" and _is_approve_equivalent(r):
            r = dict(r)
            r["_effective_state"] = "APPROVED"
        elif state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        else:
            r = dict(r)
            r["_effective_state"] = state
        prev = latest_by_user.get(user)
        if prev is None or (r.get("submitted_at", "") >
                             prev.get("submitted_at", "")):
            latest_by_user[user] = r
    # 按 _effective_state (考虑 approve-equivalent) 分类
    approved_users = [u for u, r in latest_by_user.items()
                       if r["_effective_state"] == "APPROVED"]
    changes_users = [u for u, r in latest_by_user.items()
                      if r["_effective_state"] == "CHANGES_REQUESTED"]
    plan.approved_by_a = bool(approved_users)
    plan.has_changes_requested = bool(changes_users)
    if latest_by_user:
        latest_overall = max(latest_by_user.values(),
                              key=lambda r: r.get("submitted_at", ""))
        plan.latest_review_state = latest_overall.get("state")

    # re-target 需要? base 在 merged_branches → 改 base=main
    if plan.base != "main" and plan.base in merged_branches:
        plan.retarget_base_to = "main"

    # 综合 status
    if plan.has_changes_requested:
        plan.status = "blocked"
        plan.error = f"CHANGES_REQUESTED by {','.join(changes_users)}"
        return
    if not plan.approved_by_a:
        plan.status = "blocked"
        plan.error = "未 APPROVED (或未带 approve-equivalent marker)"
        return
    # base 安全性: base 非 main 且不在本 run 已合 head 里 → 对应 PR 仍 open,
    # 直接 PUT /merge 会合进栈分支而不是 main (2026-04-24 PR #10 踩坑)。
    # 等 base PR 合了再回头处理。
    if plan.base != "main" and plan.base not in merged_branches:
        plan.status = "blocked"
        plan.error = f"等 base PR 先合 (base={plan.base})"
        return
    # mergeable_state: clean / stable = 绿灯; blocked / dirty / behind = 问题
    if plan.mergeable_state in ("dirty", "blocked"):
        plan.status = "blocked"
        plan.error = f"mergeable_state={plan.mergeable_state}"
        return
    plan.status = "ready"


# ─────────────────────────────────────────────────────────────────────────────
# Apply 动作
# ─────────────────────────────────────────────────────────────────────────────

def retarget_base(pr_number: int, new_base: str,
                   token: Optional[str]) -> None:
    github_api(f"/repos/{REPO}/pulls/{pr_number}", token,
                method="PATCH", body={"base": new_base})


def merge_pr(pr_number: int, method: str,
              token: Optional[str]) -> str:
    """PUT /pulls/:n/merge → return merge_commit_sha。"""
    r = github_api(f"/repos/{REPO}/pulls/{pr_number}/merge", token,
                    method="PUT", body={"merge_method": method})
    if not r.get("merged"):
        raise RuntimeError(f"merge returned merged=false: {r}")
    return r.get("sha", "")


def wait_for_mergeable_settled(plan: MergePlan, token: Optional[str],
                                 merged_branches: Set[str],
                                 max_attempts: int = 4,
                                 wait_seconds: float = 3.0,
                                 sleep_fn=None) -> None:
    """Github 对 main 推进后 mergeable 要算几秒。连串合并时第一个合完第二个
    mergeable=unknown, 直接 PUT /merge 会 405。这里轮询直到 mergeable 落定
    到明确状态 (clean/stable/dirty/blocked/...) 或 max_attempts 用完。
    """
    import time
    sleep = sleep_fn or time.sleep
    for i in range(max_attempts):
        check_readiness(plan, token, merged_branches)
        if plan.mergeable_state != "unknown":
            return
        if i < max_attempts - 1:
            sleep(wait_seconds)
    # mergeable_state 还 unknown 就 out, 由 apply_merges 按正常分支处理


def apply_merges(plans: List[MergePlan], token: Optional[str],
                  merge_method: str, continue_on_error: bool
                  ) -> List[MergePlan]:
    merged_branches: Set[str] = set()
    halted = False
    for p in plans:
        if halted:
            p.status = "skipped"
            p.error = "halted by prior failure"
            continue
        # 先 refresh readiness (上一个 merge 可能改了本 PR 的 base 或 mergeable);
        # wait_for_mergeable_settled 含 check_readiness + 重试 unknown 状态。
        wait_for_mergeable_settled(p, token, merged_branches)
        if p.status == "blocked":
            if not continue_on_error:
                halted = True
            continue
        if p.status in ("failed", "skipped"):
            if not continue_on_error:
                halted = True
            continue
        # re-target (若需)
        if p.retarget_base_to:
            try:
                retarget_base(p.pr_number, p.retarget_base_to, token)
            except Exception as e:
                p.status = "failed"
                p.error = f"retarget: {str(e)[:100]}"
                if not continue_on_error:
                    halted = True
                continue
            # re-check readiness: retarget 后 GitHub 重算 mergeable (也可能 unknown)
            wait_for_mergeable_settled(p, token, merged_branches)
            if p.status != "ready":
                if not continue_on_error:
                    halted = True
                continue
        # merge
        try:
            sha = merge_pr(p.pr_number, merge_method, token)
        except Exception as e:
            p.status = "failed"
            p.error = f"merge: {str(e)[:100]}"
            if not continue_on_error:
                halted = True
            continue
        p.status = "merged"
        p.merge_sha = sha
        merged_branches.add(p.branch)
        # 本地 fetch 更新 origin/main
        git_fetch_main()
    return plans


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def render(plans: List[MergePlan], applied: bool) -> str:
    title = "Auto Merge Stack — Apply" if applied else "Auto Merge Stack — Dry Run"
    lines = [f"\n{BOLD}=== {title} ==={RESET}"]
    if not plans:
        lines.append(f"  {YELLOW}(无匹配的 open B PR){RESET}")
        return "\n".join(lines)
    for p in plans:
        color = {"ready": GREEN, "merged": GREEN,
                 "blocked": YELLOW, "skipped": YELLOW,
                 "failed": RED, "retargeted": CYAN,
                 "pending": ""}.get(p.status, "")
        tag = f"{color}[{p.status:10}]{RESET}"
        approve = GREEN + "✓APPROVED" + RESET if p.approved_by_a else \
            (RED + "✗" + RESET if p.has_changes_requested else
             CYAN + "⏳" + RESET)
        m = p.mergeable_state or "?"
        retarget = ""
        if p.retarget_base_to:
            retarget = f"  {CYAN}[base→{p.retarget_base_to}]{RESET}"
        lines.append(
            f"  {tag} PR#{p.pr_number}  {approve}  "
            f"mergeable={m:8s}  "
            f"{p.branch:35s} → {p.base}{retarget}")
        if p.title:
            lines.append(f"              {BLUE}{p.title}{RESET}")
        if p.merge_sha:
            lines.append(f"              ✓ merged sha={p.merge_sha[:8]}")
        if p.error:
            lines.append(f"              {RED}error: {p.error}{RESET}")

    counts: Dict[str, int] = {}
    for p in plans:
        counts[p.status] = counts.get(p.status, 0) + 1
    lines.append(f"\n{BOLD}汇总:{RESET}")
    for k in ("ready", "merged", "blocked", "skipped", "failed"):
        n = counts.get(k, 0)
        if n == 0:
            continue
        c = {"ready": GREEN, "merged": GREEN,
             "blocked": YELLOW, "skipped": YELLOW,
             "failed": RED}[k]
        lines.append(f"  {c}{k}{RESET}: {n}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_ids(raw: str) -> Set[int]:
    out: Set[int] = set()
    for tok in raw.replace(" ", "").split(","):
        if not tok:
            continue
        try:
            out.add(int(tok))
        except ValueError:
            pass
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="栈式 PR 自动合并 — 默认 dry-run, --apply 才真合")
    parser.add_argument("--head-prefix", default=DEFAULT_HEAD_PREFIX)
    parser.add_argument("--only", default=None,
                        help="只合指定 PR 号, e.g. --only 6,7,1")
    parser.add_argument("--apply", action="store_true",
                        help="真的调用 merge API (默认 dry-run)")
    parser.add_argument("--merge-method",
                        choices=("merge", "squash", "rebase"),
                        default="merge",
                        help="匹配 repo 既有风格默认 merge")
    parser.add_argument("--continue-on-error", action="store_true",
                        help="任一 PR 失败仍继续后面 (默认严格停)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, CYAN, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""

    token = get_token()
    if not token:
        print(f"{RED}ERROR:{RESET} 无 GitHub token (git credential fill 没给)",
              file=sys.stderr)
        return 1

    only = _parse_ids(args.only) if args.only else None
    try:
        plans = collect_plans(token, args.head_prefix, only_prs=only)
    except Exception as e:
        print(f"{RED}ERROR:{RESET} 拉 PR 列表失败: {e}", file=sys.stderr)
        return 1

    if not plans:
        print(f"{YELLOW}(没有匹配的 open B PR){RESET}")
        return 0

    # dry-run: 先对每个 PR 检查 readiness (不真合, 不 retarget)
    merged_set: Set[str] = set()
    for p in plans:
        check_readiness(p, token, merged_set)
        # 模拟 dry-run 下 "父已合" 的传递: 若父 plan 是 ready, 认为它会被合,
        # 让子 plan 的 retarget 检查触发
        if p.status == "ready":
            merged_set.add(p.branch)

    if args.apply:
        # 重置 merged_set, 真正 apply 从空开始
        for p in plans:
            p.retarget_base_to = None
            p.status = "pending"
        plans = apply_merges(plans, token, args.merge_method,
                              args.continue_on_error)

    if args.json:
        print(json.dumps([p.to_dict() for p in plans],
                          ensure_ascii=False, indent=2))
    else:
        print(render(plans, applied=args.apply))

    any_failed = any(p.status == "failed" for p in plans)
    any_blocked = any(p.status == "blocked" for p in plans)
    if args.apply:
        return 1 if any_failed else 0
    # dry-run: 有 blocked 也报 1 便于 cron 门控
    return 1 if (any_failed or any_blocked) else 0


if __name__ == "__main__":
    sys.exit(main())
