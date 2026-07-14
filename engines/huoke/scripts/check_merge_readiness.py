# -*- coding: utf-8 -*-
"""PR 合并就绪检查 (P12)。

遍历仓库所有 open PR, 判断每个 PR 的:
  * mergeable 状态 (clean/dirty/blocked/unstable)
  * CI checks 状态 (passing/failing/pending)
  * review 状态 (approved/changes_requested/review_required)
  * base 栈依赖 (是否被其他未合 PR base)

按结果分 3 类输出 + 给出推荐合并顺序。读取 GitHub API 只读,不改任何
PR 状态。

认证: 从 ``git credential fill`` 提取 token (和 scripts/_open_*_pr_tmp.py
同机制), 无需手动配 env。

用法:
    python scripts/check_merge_readiness.py
    python scripts/check_merge_readiness.py --repo victor2025PH/mobile-auto0423
    python scripts/check_merge_readiness.py --json          # JSON 输出供程序消费
    python scripts/check_merge_readiness.py --author-filter victor2025PH
    python scripts/check_merge_readiness.py --head-prefix feat-b-   # 只看 B 的
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BLUE = "\033[34m"
RESET = "\033[0m"
BOLD = "\033[1m"

if os.environ.get("NO_COLOR") or (sys.platform == "win32" and
                                    not os.environ.get("ANSICON")):
    try:
        os.system("")
    except Exception:
        GREEN = YELLOW = RED = BLUE = RESET = BOLD = ""


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API
# ─────────────────────────────────────────────────────────────────────────────

def get_token_from_git_credential() -> Optional[str]:
    """从 git credential helper 取 GitHub token。"""
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


def github_api_get(path: str, token: Optional[str],
                   accept: str = "application/vnd.github+json") -> Any:
    """GET GitHub API。"""
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": accept,
        "User-Agent": "merge-readiness-check",
    }
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {path} failed: {e.code} {body[:200]}")


# ─────────────────────────────────────────────────────────────────────────────
# PR 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PrInfo:
    number: int
    title: str
    head: str              # head.ref
    base: str              # base.ref
    user: str              # author login
    state: str             # open
    mergeable: Optional[bool]          # null 时 GitHub 还没算完
    mergeable_state: str               # clean/dirty/blocked/behind/unstable/unknown
    draft: bool
    html_url: str
    reviews: List[Dict[str, Any]] = field(default_factory=list)
    checks_state: str = "unknown"  # passing/failing/pending/none
    labels: List[str] = field(default_factory=list)

    @property
    def is_ready(self) -> bool:
        """只针对 base=main 的独立 PR 判断可立即合。

        mergeable_state 取值 (GitHub API):
          * clean    — 可合, 所有 check 绿
          * unstable — 可合, 但有 non-required check 未通过 (仓库无 required
                       checks 设定时常见, 实际可手动合)
          * blocked  — 被 branch protection 阻塞 (通常是 required reviews 不够)
          * dirty    — 有冲突需 rebase
          * behind   — 落后 base 需同步
          * unknown  — GitHub 还在算

        这里把 clean + unstable 都算 ready (no required checks 时合法)。
        blocked 不归 ready (需 approve)。dirty/behind 归 blocked 桶。
        """
        if self.draft:
            return False
        if self.mergeable is False:
            return False
        if self.mergeable_state in ("dirty", "behind"):
            return False
        if self.checks_state == "failing":
            return False
        return self.base == "main" and self.mergeable_state in ("clean",
                                                                  "unstable")

    @property
    def review_verdict(self) -> str:
        """从 reviews 列表推断最终 verdict: APPROVED/CHANGES_REQUESTED/NONE。"""
        if not self.reviews:
            return "NONE"
        # 以每个 user 的最新 review 为准
        latest: Dict[str, str] = {}
        for r in sorted(self.reviews,
                         key=lambda x: x.get("submitted_at", "") or ""):
            user = (r.get("user") or {}).get("login", "")
            state = r.get("state", "")
            if user and state in ("APPROVED", "CHANGES_REQUESTED"):
                latest[user] = state
        if any(v == "CHANGES_REQUESTED" for v in latest.values()):
            return "CHANGES_REQUESTED"
        if any(v == "APPROVED" for v in latest.values()):
            return "APPROVED"
        return "NONE"


def fetch_prs(repo: str, token: Optional[str]) -> List[PrInfo]:
    """列所有 open PR。对每个 PR 补充 reviews + checks。"""
    raw_list = github_api_get(
        f"/repos/{repo}/pulls?state=open&per_page=100", token)
    prs: List[PrInfo] = []
    for p in raw_list:
        # mergeable 可能 null, GitHub 后台算; 查单个 PR 强制计算
        detail = github_api_get(f"/repos/{repo}/pulls/{p['number']}", token)
        reviews = github_api_get(
            f"/repos/{repo}/pulls/{p['number']}/reviews", token) or []
        # checks: 用 statuses API 代替 check-runs 简化
        checks_state = "none"
        try:
            sha = detail["head"]["sha"]
            status = github_api_get(
                f"/repos/{repo}/commits/{sha}/status", token)
            if status.get("total_count", 0) > 0:
                checks_state = status.get("state", "none")
                if checks_state == "success":
                    checks_state = "passing"
                elif checks_state == "failure":
                    checks_state = "failing"
                else:
                    checks_state = "pending"
        except Exception:
            checks_state = "none"

        prs.append(PrInfo(
            number=p["number"],
            title=p["title"],
            head=p["head"]["ref"],
            base=p["base"]["ref"],
            user=p["user"]["login"],
            state=p["state"],
            mergeable=detail.get("mergeable"),
            mergeable_state=detail.get("mergeable_state", "unknown"),
            draft=p.get("draft", False),
            html_url=p["html_url"],
            reviews=reviews,
            checks_state=checks_state,
            labels=[l["name"] for l in p.get("labels", [])],
        ))
    return prs


# ─────────────────────────────────────────────────────────────────────────────
# 分类 + 推荐顺序
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReadinessReport:
    ready: List[PrInfo] = field(default_factory=list)          # 立即可合
    waiting: List[Tuple[PrInfo, str]] = field(default_factory=list)  # (pr, reason)
    blocked: List[Tuple[PrInfo, str]] = field(default_factory=list)  # (pr, reason)
    recommended_order: List[int] = field(default_factory=list)


def classify_prs(prs: List[PrInfo]) -> ReadinessReport:
    r = ReadinessReport()
    pr_by_head: Dict[str, PrInfo] = {p.head: p for p in prs}

    for pr in prs:
        if pr.draft:
            r.blocked.append((pr, "草稿状态"))
            continue
        if pr.checks_state == "failing":
            r.blocked.append((pr, "CI 失败"))
            continue
        if pr.mergeable is False or pr.mergeable_state == "dirty":
            r.blocked.append((pr, "存在冲突,需 rebase"))
            continue
        if pr.review_verdict == "CHANGES_REQUESTED":
            r.blocked.append((pr, "有 review 要求修改"))
            continue

        if pr.base == "main":
            if pr.mergeable_state in ("clean", "unstable"):
                # unstable = 可合, 无 required checks 时常见 (实际跑 gh pr
                # merge --squash 会成功)
                r.ready.append(pr)
            elif pr.mergeable_state == "blocked":
                r.waiting.append((pr, "base=main 但 blocked (通常 required reviews 不够)"))
            elif pr.mergeable_state == "behind":
                r.waiting.append((pr, "落后 main, 需 git rebase origin/main"))
            else:
                r.waiting.append((pr, f"mergeable_state={pr.mergeable_state}"))
        else:
            # base 是另一个分支 — 查该分支是否有 open PR
            base_pr = pr_by_head.get(pr.base)
            if base_pr:
                r.waiting.append((pr, f"栈依赖: 先合 PR #{base_pr.number} ({pr.base})"))
            else:
                # base 不对应任何 open PR, 可能已合或从未开
                if pr.mergeable_state in ("clean", "unstable"):
                    r.ready.append(pr)
                else:
                    r.waiting.append((pr, f"base={pr.base} 非 main, 且无对应 open PR"))

    # 推荐合并顺序:
    # 1. base=main + ready 的先合
    # 2. 按 base→head 依赖关系做拓扑排序
    # 3. blocked 的不进顺序
    order: List[int] = []
    placed: Set[int] = set()

    # 第一轮: base=main 的 ready
    for pr in r.ready:
        if pr.base == "main" and pr.number not in placed:
            order.append(pr.number)
            placed.add(pr.number)

    # 递推: 依赖 base 已在 order 的, 逐次加入
    pr_by_num: Dict[int, PrInfo] = {p.number: p for p in prs}
    changed = True
    while changed:
        changed = False
        for pr in prs:
            if pr.number in placed:
                continue
            if (pr, ) in ():  # placeholder
                continue
            # 跳过 blocked
            if any(pr.number == b.number for b, _ in r.blocked):
                continue
            # 查 base 是否已 placed (通过 head 反查)
            base_pr = pr_by_head.get(pr.base)
            if pr.base == "main" or (base_pr is not None
                                      and base_pr.number in placed):
                order.append(pr.number)
                placed.add(pr.number)
                changed = True

    r.recommended_order = order
    return r


# ─────────────────────────────────────────────────────────────────────────────
# 渲染
# ─────────────────────────────────────────────────────────────────────────────

def _tag(status: str) -> str:
    colors = {
        "READY":   (GREEN, "READY"),
        "WAIT":    (YELLOW, "WAIT "),
        "BLOCK":   (RED, "BLOCK"),
    }
    c, txt = colors.get(status, ("", status))
    return f"{c}[{txt}]{RESET}"


def _pr_row(pr: PrInfo, extra: str = "") -> str:
    verdict = pr.review_verdict
    v_color = {"APPROVED": GREEN, "CHANGES_REQUESTED": RED,
               "NONE": YELLOW}.get(verdict, "")
    ms = pr.mergeable_state
    ms_color = {"clean": GREEN, "dirty": RED, "blocked": YELLOW,
                "behind": YELLOW, "unstable": YELLOW}.get(ms, "")
    checks = pr.checks_state
    c_color = {"passing": GREEN, "failing": RED,
               "pending": YELLOW}.get(checks, "")

    title = pr.title[:60]
    line = (f"  PR #{pr.number:<3} "
            f"{pr.head[:32]:<32} → {pr.base:<24} "
            f"[{v_color}{verdict:<10}{RESET}] "
            f"[{ms_color}{ms:<8}{RESET}] "
            f"[{c_color}{checks:<7}{RESET}] "
            f"{title}")
    if extra:
        line += f"\n      {BLUE}→ {extra}{RESET}"
    return line


def render_report(report: ReadinessReport) -> str:
    lines: List[str] = []
    lines.append(f"\n{BOLD}=== PR Merge Readiness ==={RESET}\n")

    lines.append(f"{BOLD}{GREEN}## 可立即合入 ({len(report.ready)}){RESET}")
    if not report.ready:
        lines.append(f"  {YELLOW}(无){RESET}")
    for pr in report.ready:
        lines.append(_pr_row(pr))
    lines.append("")

    lines.append(f"{BOLD}{YELLOW}## 等待依赖 ({len(report.waiting)}){RESET}")
    if not report.waiting:
        lines.append(f"  {GREEN}(无){RESET}")
    for pr, reason in report.waiting:
        lines.append(_pr_row(pr, reason))
    lines.append("")

    lines.append(f"{BOLD}{RED}## 需处理 ({len(report.blocked)}){RESET}")
    if not report.blocked:
        lines.append(f"  {GREEN}(无){RESET}")
    for pr, reason in report.blocked:
        lines.append(_pr_row(pr, reason))
    lines.append("")

    lines.append(f"{BOLD}## 推荐合并顺序{RESET}")
    if report.recommended_order:
        for i, num in enumerate(report.recommended_order, 1):
            lines.append(f"  {i:2}. PR #{num}")
    else:
        lines.append(f"  {YELLOW}(无可立即推进的 PR){RESET}")

    total = len(report.ready) + len(report.waiting) + len(report.blocked)
    lines.append(f"\n{BOLD}=== Summary ==={RESET}")
    lines.append(f"  总 open PR: {total}")
    lines.append(f"  {GREEN}可合: {len(report.ready)}{RESET}"
                  f"  {YELLOW}等待: {len(report.waiting)}{RESET}"
                  f"  {RED}阻塞: {len(report.blocked)}{RESET}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="PR 合并就绪检查 (只读, 不改任何 PR)")
    parser.add_argument("--repo", default="victor2025PH/mobile-auto0423",
                        help="GitHub repo (owner/name)")
    parser.add_argument("--token", default=None,
                        help="GitHub token (默认从 git credential 取)")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 供程序消费")
    parser.add_argument("--author-filter", default=None,
                        help="只看指定 author 的 PR")
    parser.add_argument("--head-prefix", default=None,
                        help="只看 head ref 以指定前缀开头的 PR")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = RESET = BOLD = ""

    token = args.token or get_token_from_git_credential()
    if not token:
        print(f"{YELLOW}[WARN]{RESET} 没有 GitHub token, API 会走 anonymous "
              "rate limit (60/hr)", file=sys.stderr)

    try:
        prs = fetch_prs(args.repo, token)
    except Exception as e:
        print(f"{RED}ERROR:{RESET} fetch_prs 失败: {e}", file=sys.stderr)
        return 1

    # 过滤
    if args.author_filter:
        prs = [p for p in prs if p.user == args.author_filter]
    if args.head_prefix:
        prs = [p for p in prs if p.head.startswith(args.head_prefix)]

    if not prs:
        print(f"{YELLOW}(没有 open PR 匹配过滤条件){RESET}")
        return 0

    report = classify_prs(prs)

    if args.json:
        out = {
            "ready": [_pr_to_dict(p) for p in report.ready],
            "waiting": [{"pr": _pr_to_dict(p), "reason": r}
                        for p, r in report.waiting],
            "blocked": [{"pr": _pr_to_dict(p), "reason": r}
                        for p, r in report.blocked],
            "recommended_order": report.recommended_order,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(render_report(report))
    # exit code: 有 blocked 返 1 (CI/test pipeline 可据此判断)
    return 1 if report.blocked else 0


def _pr_to_dict(pr: PrInfo) -> Dict[str, Any]:
    return {
        "number": pr.number,
        "title": pr.title,
        "head": pr.head,
        "base": pr.base,
        "user": pr.user,
        "mergeable": pr.mergeable,
        "mergeable_state": pr.mergeable_state,
        "draft": pr.draft,
        "checks_state": pr.checks_state,
        "review_verdict": pr.review_verdict,
        "url": pr.html_url,
    }


if __name__ == "__main__":
    sys.exit(main())
