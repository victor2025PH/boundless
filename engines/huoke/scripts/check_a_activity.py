# -*- coding: utf-8 -*-
"""A 机活动检查 — 替代"人工转发+等回复"低效沟通。

核心洞察: 两台 Claude 通过同一 GitHub repo 同步, A 推送 commit 后 B 只需
``git fetch`` 就能读到。问题是没有**通知机制**。本工具自动化这个 poll
过程, 让 B 不再需要 user 人工转发 "A 已完成" 这类消息。

两种模式:

**活动流模式** (默认): 汇总最近 N 小时内 A 的所有动作 (commit/review/comment/merge)
    python scripts/check_a_activity.py                  # 默认 24h
    python scripts/check_a_activity.py --hours 72       # 3 天
    python scripts/check_a_activity.py --watch-pr 17    # 聚焦单 PR
    python scripts/check_a_activity.py --json           # 程序消费

**Review 看板模式** (``--reviews``): 只盘指定 PR 当前的 review 状态, 用于
"等 A 几个 PR review" 的场景, 输出一行一 PR 的就绪/待办判定。
    python scripts/check_a_activity.py --reviews 10,6,7,1
    python scripts/check_a_activity.py --reviews 10,6,7,1 \
        --expect-file docs/A_TO_B_ROUND3_REVIEW_RESULTS.md

退出码:
  * 活动流模式: 恒 0
  * Review 看板: 全 APPROVED 且 expect-file 已落地 → 0, 否则 1
  * 便于 cron / CI 当作 "A 动作就绪" 的门控信号。
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
from typing import Any, Dict, List, Optional


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
A_BRANCH_PREFIXES = ("feat-a-",)
A_USERS = ("victor2025PH",)  # A 账号

# A 在 PR #26 反馈: GitHub 不允许 author 自审 (A/B 共用 victor2025PH token),
# APPROVE state 走不通; 约定 COMMENTED body 含下列任一 marker 视同 approved。
# A 承诺每次 approve-equivalent 评论必带 "✅ A 侧 review 通过 (approve-equivalent)"。
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


# 重要共享契约文件 (若 A 改动应提醒 B)
SHARED_FILES_TO_WATCH = [
    "docs/INTEGRATION_CONTRACT.md",
    "src/host/database.py",
    "src/host/fb_store.py",
    "src/host/fb_concurrency.py",
]


# ─────────────────────────────────────────────────────────────────────────────
# Git 本地层
# ─────────────────────────────────────────────────────────────────────────────

def git_fetch() -> bool:
    try:
        subprocess.run(["git", "fetch", "origin", "--quiet"],
                        check=True, timeout=30)
        return True
    except Exception as e:
        print(f"{YELLOW}[WARN]{RESET} git fetch 失败: {e}", file=sys.stderr)
        return False


def git_branches_with_prefix(prefix: str) -> List[str]:
    try:
        r = subprocess.run(
            ["git", "branch", "-r", "--list", f"origin/{prefix}*"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=15,
        )
        lines = [l.strip().replace("origin/", "") for l in r.stdout.splitlines()]
        return [l for l in lines if l and "HEAD" not in l]
    except Exception:
        return []


def git_recent_commits_on_branch(branch: str, hours: int,
                                   main_ref: str = "origin/main"
                                   ) -> List[Dict[str, str]]:
    """返回该分支相对于 main 独有的最近 N 小时 commits。

    用 ``origin/main..branch`` 过滤掉已在 main 的 commits, 避免栈式 PR
    多分支重复报同一 commit (例如 feat-a-phase3 的 commits 也在
    feat-a-phase5-mesh 和 feat-a-reply-to-b 里)。
    """
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+0000")
    try:
        r = subprocess.run(
            ["git", "log", f"{main_ref}..origin/{branch}",
             f"--since={since_iso}",
             "--pretty=format:%H|%an|%ar|%s"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=15,
        )
    except Exception:
        return []
    commits = []
    for line in r.stdout.splitlines():
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, author, when, subject = parts
        commits.append({"sha": sha[:7], "full_sha": sha, "author": author,
                        "when": when, "subject": subject[:80]})
    return commits


def git_main_recent_commits(hours: int) -> List[Dict[str, str]]:
    return git_recent_commits_on_branch("main", hours)


def git_file_changed_recently(path: str, hours: int,
                               branch: str = "main"
                               ) -> List[Dict[str, str]]:
    """某路径在 N 小时内的新 commits (排除 initial commit 无 parent 的)。

    返回 [{sha, when, subject}, ...], 空列表表示无实际变化。
    """
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S+0000")
    try:
        r = subprocess.run(
            ["git", "log", f"origin/{branch}",
             f"--since={since_iso}",
             # %P parent SHAs (空 = initial commit)
             "--pretty=format:%H|%P|%ar|%s",
             "--", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=15,
        )
    except Exception:
        return []
    commits: List[Dict[str, str]] = []
    for line in r.stdout.splitlines():
        if not line or "|" not in line:
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            continue
        sha, parents, when, subject = parts
        if not parents.strip():
            continue  # initial commit, 跳过 (不算"变化")
        commits.append({"sha": sha[:7], "when": when,
                        "subject": subject[:80]})
    return commits


# ─────────────────────────────────────────────────────────────────────────────
# GitHub API 层
# ─────────────────────────────────────────────────────────────────────────────

def get_token() -> Optional[str]:
    try:
        r = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, check=True,
        )
    except Exception:
        return None
    for line in r.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None


def github_api_get(path: str, token: Optional[str],
                    *, max_retries: int = 3,
                    backoff_base: float = 2.0) -> Any:
    """GitHub API GET, HTTP 429 / 5xx 指数退避 retry (cron 自动化避免裸失败).

    Retry-After header 优先, 否则 backoff_base * 2^attempt, 单次 cap 30s。
    max_retries=3 总 wait ≤ ~14s。非重试错误或用尽次数 → raise RuntimeError。
    """
    import time
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "check-a-activity"}
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(
        f"https://api.github.com{path}", headers=headers)
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            status = e.code
            retriable = status == 429 or 500 <= status < 600
            if not retriable or attempt == max_retries:
                raise RuntimeError(
                    f"{path}: {status} {e.read().decode()[:200]}")
            retry_after_hdr = (e.headers.get("Retry-After")
                                if e.headers else None)
            try:
                wait = (float(retry_after_hdr) if retry_after_hdr
                        else backoff_base * (2 ** attempt))
            except (TypeError, ValueError):
                wait = backoff_base * (2 ** attempt)
            time.sleep(min(wait, 30.0))


# ─────────────────────────────────────────────────────────────────────────────
# Activity items
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ActivityItem:
    kind: str  # branch_commit / pr_comment / pr_review / pr_merged / shared_file_change
    summary: str
    detail: str = ""
    urgency: str = "info"  # info / attention / urgent

    def render(self) -> str:
        color = {"info": "", "attention": YELLOW,
                 "urgent": RED}.get(self.urgency, "")
        tag = f"[{self.kind:20}]"
        return f"  {color}{tag}{RESET} {self.summary}" + (
            f"\n      {BLUE}→ {self.detail}{RESET}" if self.detail else "")


def collect_activity(hours: int, token: Optional[str],
                     watch_pr: Optional[int] = None) -> List[ActivityItem]:
    items: List[ActivityItem] = []

    # 1. A 的分支最近 commits (去重 SHA, 避免栈式分支重复报同一 commit)
    seen_shas: set = set()
    branch_commits: List[tuple] = []  # (branch, commit dict)
    for prefix in A_BRANCH_PREFIXES:
        for br in git_branches_with_prefix(prefix):
            commits = git_recent_commits_on_branch(br, hours)
            for c in commits:
                if c["full_sha"] in seen_shas:
                    continue
                seen_shas.add(c["full_sha"])
                branch_commits.append((br, c))
    # 按时间倒序 (git log 默认已经是)
    for br, c in branch_commits:
        items.append(ActivityItem(
            kind="branch_commit",
            summary=f"{br}: {c['subject']}",
            detail=f"{c['sha']} by {c['author']} {c['when']}",
            urgency="attention",
        ))

    # 2. main 分支最近 A 的 commits
    for c in git_main_recent_commits(hours):
        if any(u in c["author"] for u in A_USERS):
            items.append(ActivityItem(
                kind="main_commit_by_a",
                summary=f"main: {c['subject']}",
                detail=f"{c['sha']} by {c['author']} {c['when']}",
                urgency="urgent",  # A 合进 main 是大事
            ))

    # 3. 重点共享契约文件在 main 分支上的新变化 (排除 initial commit)
    for f in SHARED_FILES_TO_WATCH:
        changes = git_file_changed_recently(f, hours, "main")
        for c in changes:
            items.append(ActivityItem(
                kind="shared_file_change",
                summary=f"main 的 {f}: {c['subject']}",
                detail=f"{c['sha']} {c['when']} — B 可能需要 git rebase origin/main",
                urgency="urgent",  # 共享文件进入 main 真值得 urgent
            ))

    # 4. GitHub API: B→A 的 PR 有没有 A 的互动
    if token:
        # B 自己账号是 claude-messenger-bot 本地 name, GitHub API 里 PR author
        # 实际可能是 victor2025PH 因为 git credential 是 victor2025PH 的 token。
        # 所以 "B's PR" 实际通过 head 分支前缀 feat-b- 识别, review/comment
        # 的 reviewer 通过 login 非 B 过滤。这里简化: 扫所有 feat-b- 开头的
        # PR, 看最近 N 小时有没有 review/comment 活动。
        try:
            prs = github_api_get(
                f"/repos/{REPO}/pulls?state=all&per_page=50", token)
        except Exception as e:
            items.append(ActivityItem(
                kind="github_api_error",
                summary="GitHub API 调用失败",
                detail=str(e)[:100],
                urgency="attention",
            ))
            prs = []

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
        for p in prs:
            head_ref = (p.get("head") or {}).get("ref", "")
            if not head_ref.startswith("feat-b-"):
                continue
            num = p["number"]

            if watch_pr and num != watch_pr:
                continue

            # reviews
            try:
                reviews = github_api_get(
                    f"/repos/{REPO}/pulls/{num}/reviews", token) or []
            except Exception:
                reviews = []
            for r in reviews:
                sub = r.get("submitted_at", "")
                if not sub:
                    continue
                try:
                    ts = _dt.datetime.strptime(
                        sub, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=_dt.timezone.utc)
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                user = (r.get("user") or {}).get("login", "")
                state = r.get("state", "")
                items.append(ActivityItem(
                    kind="pr_review",
                    summary=f"PR #{num} {state} by {user}",
                    detail=f"{sub} — {p['title'][:60]}",
                    urgency="urgent" if state == "CHANGES_REQUESTED" else "attention",
                ))

            # comments (issue_comments API, PR comments 走同一端点)
            try:
                comments = github_api_get(
                    f"/repos/{REPO}/issues/{num}/comments", token) or []
            except Exception:
                comments = []
            for c in comments:
                created = c.get("created_at", "")
                if not created:
                    continue
                try:
                    ts = _dt.datetime.strptime(
                        created, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=_dt.timezone.utc)
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                user = (c.get("user") or {}).get("login", "")
                body = c.get("body", "")[:80]
                items.append(ActivityItem(
                    kind="pr_comment",
                    summary=f"PR #{num} comment by {user}",
                    detail=f"{created} — {body}",
                    urgency="urgent",
                ))

            # merged state
            if p.get("merged_at"):
                try:
                    merged = _dt.datetime.strptime(
                        p["merged_at"], "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=_dt.timezone.utc)
                except ValueError:
                    merged = None
                if merged and merged >= cutoff:
                    items.append(ActivityItem(
                        kind="pr_merged",
                        summary=f"PR #{num} MERGED 🎉",
                        detail=f"{p['merged_at']} — {p['title'][:60]}",
                        urgency="urgent",
                    ))
    else:
        items.append(ActivityItem(
            kind="no_token",
            summary="GitHub token 不可用 (git credential fill 没给), PR 活动未查",
            detail="装 GitHub CLI 或配 git credential helper",
            urgency="attention",
        ))

    return items


# ─────────────────────────────────────────────────────────────────────────────
# Review dashboard (--reviews 模式: 专门盘指定 PR 的 review 状态)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReviewSummary:
    pr_number: int
    title: str = ""
    state: str = "unknown"               # open / closed / merged
    head_ref: str = ""
    latest_review_state: Optional[str] = None   # APPROVED / CHANGES_REQUESTED / COMMENTED / DISMISSED
    latest_review_user: Optional[str] = None
    latest_review_at: Optional[str] = None
    latest_review_is_approve_equivalent: bool = False
    review_count: int = 0
    error: str = ""

    @property
    def ready(self) -> bool:
        """True = 可直接合,无需额外动作。"""
        if self.latest_review_state == "APPROVED":
            return True
        # A 的 approve-equivalent COMMENTED (见 APPROVE_EQUIVALENT_MARKERS)
        if (self.latest_review_state == "COMMENTED"
                and self.latest_review_is_approve_equivalent):
            return True
        return False

    @property
    def blocked(self) -> bool:
        """True = A 标了 CHANGES_REQUESTED, 等 B 改。"""
        return self.latest_review_state == "CHANGES_REQUESTED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pr_number": self.pr_number,
            "title": self.title,
            "state": self.state,
            "head_ref": self.head_ref,
            "latest_review_state": self.latest_review_state,
            "latest_review_user": self.latest_review_user,
            "latest_review_at": self.latest_review_at,
            "latest_review_is_approve_equivalent":
                self.latest_review_is_approve_equivalent,
            "review_count": self.review_count,
            "ready": self.ready,
            "blocked": self.blocked,
            "error": self.error,
        }


def _latest_review(reviews: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按 submitted_at 选最新一条, GitHub 返顺序是时间升序但以防万一显式排。

    忽略 submitted_at 为空/非法的条目。
    """
    valid = []
    for r in reviews or []:
        sub = r.get("submitted_at") or ""
        if not sub:
            continue
        try:
            ts = _dt.datetime.strptime(sub, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
        valid.append((ts, r))
    if not valid:
        return None
    valid.sort(key=lambda x: x[0])
    return valid[-1][1]


def fetch_pr_review_summary(pr_num: int, token: Optional[str]
                             ) -> ReviewSummary:
    """拉一个 PR 的 review 概要, 单次调用封死 ≤2 API 请求 (PR meta + reviews)。"""
    s = ReviewSummary(pr_number=pr_num)
    if not token:
        s.error = "no github token"
        return s
    try:
        pr = github_api_get(f"/repos/{REPO}/pulls/{pr_num}", token)
    except Exception as e:
        s.error = f"pr meta: {str(e)[:80]}"
        return s
    s.title = (pr.get("title") or "")[:80]
    s.head_ref = (pr.get("head") or {}).get("ref", "")
    if pr.get("merged_at"):
        s.state = "merged"
    else:
        s.state = pr.get("state", "unknown")
    try:
        reviews = github_api_get(
            f"/repos/{REPO}/pulls/{pr_num}/reviews", token) or []
    except Exception as e:
        s.error = f"reviews: {str(e)[:80]}"
        return s
    s.review_count = len(reviews)
    latest = _latest_review(reviews)
    if latest:
        s.latest_review_state = latest.get("state")
        s.latest_review_user = (latest.get("user") or {}).get("login")
        s.latest_review_at = latest.get("submitted_at")
        s.latest_review_is_approve_equivalent = _is_approve_equivalent(latest)
    return s


def check_main_has_file(path: str) -> bool:
    """origin/main 里是否有该路径 (文件级, 非目录)。"""
    try:
        r = subprocess.run(
            ["git", "cat-file", "-e", f"origin/main:{path}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def render_review_dashboard(summaries: List[ReviewSummary],
                             expect_file: Optional[str],
                             expect_file_present: Optional[bool]) -> str:
    lines: List[str] = []
    lines.append(f"\n{BOLD}=== A Review 看板 ({len(summaries)} PR){RESET}")
    for s in summaries:
        if s.error:
            lines.append(f"  {RED}PR#{s.pr_number} ERROR{RESET}: {s.error}")
            continue
        if s.state == "merged":
            status = f"{GREEN}✅ MERGED{RESET}"
        elif s.ready and s.latest_review_state == "COMMENTED":
            status = f"{GREEN}✅ APPROVE≡ (COMMENTED+marker){RESET}"
        elif s.ready:
            status = f"{GREEN}✅ APPROVED{RESET}"
        elif s.blocked:
            status = f"{RED}🔴 CHANGES_REQUESTED{RESET}"
        elif s.latest_review_state == "COMMENTED":
            status = f"{YELLOW}💬 COMMENTED (无 approve marker){RESET}"
        elif s.latest_review_state == "DISMISSED":
            status = f"{YELLOW}⊘ DISMISSED{RESET}"
        elif s.review_count == 0:
            status = f"{CYAN}⏳ 未 review{RESET}"
        else:
            status = f"{CYAN}? {s.latest_review_state}{RESET}"
        by = f" by {s.latest_review_user}" if s.latest_review_user else ""
        when = f" at {s.latest_review_at}" if s.latest_review_at else ""
        lines.append(
            f"  PR#{s.pr_number} {status}  "
            f"{s.head_ref:32s} — {s.title}")
        if s.latest_review_user or s.latest_review_at:
            lines.append(f"         {BLUE}{by}{when}  (共 {s.review_count} 条 review){RESET}")

    if expect_file is not None:
        lines.append("")
        if expect_file_present:
            lines.append(
                f"  {GREEN}✅ expect-file 已在 main: {expect_file}{RESET}")
        else:
            lines.append(
                f"  {YELLOW}⏳ expect-file 未落地 main: {expect_file}{RESET}")

    # 汇总
    approved = sum(1 for s in summaries if s.ready or s.state == "merged")
    blocked_n = sum(1 for s in summaries if s.blocked)
    # COMMENTED 但不 approve-equivalent 的才需手读
    commented = sum(1 for s in summaries
                     if s.latest_review_state == "COMMENTED"
                     and not s.latest_review_is_approve_equivalent)
    unreviewed = sum(1 for s in summaries
                      if s.review_count == 0 and not s.error)
    lines.append("")
    lines.append(f"{BOLD}=== 汇总{RESET}")
    lines.append(f"  APPROVED/MERGED: {GREEN}{approved}{RESET} / {len(summaries)}")
    if blocked_n:
        lines.append(f"  CHANGES_REQUESTED: {RED}{blocked_n}{RESET}")
    if commented:
        lines.append(f"  COMMENTED (需手读): {YELLOW}{commented}{RESET}")
    if unreviewed:
        lines.append(f"  未 review: {CYAN}{unreviewed}{RESET}")
    return "\n".join(lines)


def is_review_dashboard_ready(summaries: List[ReviewSummary],
                                expect_file_present: Optional[bool]) -> bool:
    """所有 PR 都 ready (approved/merged) 且 (若要求) expect-file 已落地 → True。"""
    if not summaries:
        return False
    for s in summaries:
        if s.error:
            return False
        if not (s.ready or s.state == "merged"):
            return False
    if expect_file_present is False:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

def render_report(items: List[ActivityItem], hours: int,
                   watch_pr: Optional[int]) -> str:
    lines: List[str] = []
    lines.append(f"\n{BOLD}=== A 机活动报告 (最近 {hours}h){RESET}")
    if watch_pr:
        lines.append(f"{BOLD}重点关注 PR #{watch_pr}{RESET}")

    if not items:
        lines.append(f"\n  {YELLOW}(本窗口内无 A 的活动){RESET}")
        lines.append(f"\n  建议: 增大 --hours 或发消息提醒 A, 或执行:")
        lines.append(f"    git fetch origin")
        lines.append(f"    git log origin/main --since='{hours} hours ago' --pretty=oneline")
        return "\n".join(lines)

    # 分组
    by_kind: Dict[str, List[ActivityItem]] = {}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    for kind, group in by_kind.items():
        header_color = CYAN
        if any(it.urgency == "urgent" for it in group):
            header_color = RED
        lines.append(f"\n{BOLD}{header_color}## {kind} ({len(group)}){RESET}")
        for it in group:
            lines.append(it.render())

    # 汇总
    urgent_n = sum(1 for it in items if it.urgency == "urgent")
    attn_n = sum(1 for it in items if it.urgency == "attention")
    lines.append(f"\n{BOLD}=== 汇总{RESET}")
    lines.append(f"  总事件: {len(items)}")
    if urgent_n:
        lines.append(f"  {RED}urgent: {urgent_n}{RESET} (A 合入 main / review / comment, 需关注)")
    if attn_n:
        lines.append(f"  {YELLOW}attention: {attn_n}{RESET}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_pr_list(raw: str) -> List[int]:
    out: List[int] = []
    for tok in raw.replace(" ", "").split(","):
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="A 机活动检查 (替代人工转发)")
    parser.add_argument("--hours", type=int, default=24,
                        help="查最近 N 小时 (默认 24)")
    parser.add_argument("--watch-pr", type=int, default=None,
                        help="只关注特定 PR 编号")
    parser.add_argument("--reviews", default=None,
                        help="Review 看板模式: --reviews 10,6,7,1")
    parser.add_argument("--expect-file", default=None,
                        help="配合 --reviews: 期待 A 写到 main 的文件路径, "
                             "就绪判定会包含该文件落地")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 供程序消费")
    parser.add_argument("--no-fetch", action="store_true",
                        help="跳过 git fetch (如果刚 fetch 过)")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    if args.no_color:
        global GREEN, YELLOW, RED, BLUE, CYAN, RESET, BOLD
        GREEN = YELLOW = RED = BLUE = CYAN = RESET = BOLD = ""

    if not args.no_fetch:
        git_fetch()

    token = get_token()

    # Review 看板模式
    if args.reviews:
        pr_nums = _parse_pr_list(args.reviews)
        if not pr_nums:
            print(f"{RED}--reviews 解析空{RESET}", file=sys.stderr)
            return 2
        summaries = [fetch_pr_review_summary(n, token) for n in pr_nums]
        expect_present: Optional[bool] = None
        if args.expect_file:
            expect_present = check_main_has_file(args.expect_file)
        if args.json:
            out = {
                "reviews": [s.to_dict() for s in summaries],
                "expect_file": args.expect_file,
                "expect_file_present": expect_present,
                "all_ready": is_review_dashboard_ready(
                    summaries, expect_present),
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(render_review_dashboard(
                summaries, args.expect_file, expect_present))
        return 0 if is_review_dashboard_ready(
            summaries, expect_present) else 1

    # 活动流模式 (默认)
    items = collect_activity(args.hours, token, watch_pr=args.watch_pr)

    if args.json:
        out = [{"kind": i.kind, "summary": i.summary,
                "detail": i.detail, "urgency": i.urgency}
               for i in items]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(render_report(items, args.hours, args.watch_pr))
    return 0


if __name__ == "__main__":
    sys.exit(main())
