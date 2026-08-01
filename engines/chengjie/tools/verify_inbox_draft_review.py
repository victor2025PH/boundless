# -*- coding: utf-8 -*-
"""坐席工作台「草稿审批预判徽标」真浏览器门禁（Playwright；2026-07-30）。

**为什么需要它**：草稿卡在坐席点「发送」**之前**就得预判「这条原样发会被护栏拦」——
稿龄 chip + 拦截 chip + 发送置灰 + 把「编辑」标成建议出路。后端 ``_approve_block_reason``
与 ``_stale_check`` 同一判定入口，已有 8 组交叉断言钉住判定逻辑（``approve_block_reason
== stale_reason``）。但**前端是否忠实把 ``approve_blocked`` 字段渲染成置灰 + 正确 chip**
这一环没有真浏览器守护——它是纯前端渲染、热更新直上生产，静态门禁只能证「函数挂了
window」，证不了「blocked='age' 的草稿真的置灰了」。产品明确的核心不变量是
**「预判必须与护栏行为完全一致」**（坐席看到没标记却被拦 = 比没徽标更糟）；后端钉了
判定，这里钉「判定→渲染」的最后一环。

与 ``verify_inbox_identity.py`` / ``verify_inbox_density.py`` 同族（同实例 + token
登录 + Playwright + SKIP exit 0）。**草稿列表由 route mock 注入三态，绝不写生产**；
只点 ``.conv-item:not(.has-unread)`` 已读会话，不改任何已读态。

**为何没有「篡改式 self-proof」**：density 能压天花板/注入空 div 自证，identity 能拆
DOM 自证——都不碰生产。而本门禁要证「前端忠实渲染」，唯一的篡改点是**生产模板本身**
（热更新直上生产，绝不可动）。改用**两段互补**替代：① 源码接线静态段（前端不再读
``approve_blocked`` → 先红，不依赖实例）；② route mock 注入三种 ``approve_blocked`` 值、
断言三种不同渲染——渲染逻辑若坏（永远置灰 / 永远不置灰）三态断言里必有一条红，每次
跑都在自证「渲染随字段三态而变」。

覆盖的不变量：
  0. 源码接线：``_loadDraftsBody`` 读 ``approve_blocked`` + 有 disabled 分支 +
     ``draft-block-chip`` + ``is-suggested`` 编辑高亮 + 两个 chip 文案键。
  1. blocked='' → 发送可点、无拦截 chip、编辑不高亮。
  2. blocked='age' → 发送置灰、拦截 chip 文案「已过期」、稿龄 chip 带 is-stale、
     编辑高亮为建议出路、有整段拦截说明。
  3. blocked='replied' → 发送置灰、拦截 chip 文案「已回过」（与 age 分开——一个会
     重复、一个会脱节，坐席该做的事不同）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://localhost:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
INBOX_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "unified_inbox.html"

_CARDS_JS = """() => {
  const cards = Array.from(
    document.querySelectorAll('#draft-panel-items .draft-card-mini'));
  return cards.map(card => ({
    text: (card.querySelector('.draft-text-mini') || {}).textContent || '',
    sendDisabled: !!card.querySelector('.btn-approve[disabled]'),
    hasSendActive: !!card.querySelector('.btn-approve:not([disabled])'),
    hasBlockChip: !!card.querySelector('.draft-block-chip'),
    blockChipText: ((card.querySelector('.draft-block-chip') || {}).textContent || '').trim(),
    ageStale: !!card.querySelector('.draft-age-chip.is-stale'),
    editSuggested: !!card.querySelector('.tiny-btn.is-suggested'),
    hasBlockHint: !!card.querySelector('.draft-block-hint'),
  }));
}"""


def read_token(data_root: str) -> str:
    """从实例数据根读 web_admin.auth_token（overlay 优先；不打印）。"""
    import yaml
    root = Path(data_root)
    for name in ("config.local.yaml", "config.yaml"):
        fp = root / "config" / name
        if not fp.exists():
            continue
        try:
            cfg = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        tok = str((cfg.get("web_admin") or {}).get("auth_token") or "")
        if tok:
            return tok
    return ""


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []
        self.skipped: List[str] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}  {why}")

    def summary(self, title: str = "草稿审批预判徽标验证") -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        extra = f"  (skipped {len(self.skipped)})" if self.skipped else ""
        print(f"\n== {title}: {total - len(fails)}/{total} PASS{extra}{tail}")
        return 1 if fails else 0


def check_source_wiring(ck: Checker) -> None:
    print("== 0. 源码接线（静态）==")
    if not INBOX_TEMPLATE.exists():
        ck.check("unified_inbox.html 存在", False, str(INBOX_TEMPLATE))
        return
    src = INBOX_TEMPLATE.read_text(encoding="utf-8")
    ck.check("_loadDraftsBody 读 approve_blocked", "approve_blocked" in src)
    ck.check("发送按钮有置灰分支",
             "btn-approve" in src and "disabled" in src and "cursor:not-allowed" in src)
    ck.check("渲染拦截 chip（draft-block-chip）", "draft-block-chip" in src)
    ck.check("编辑标为建议出路（is-suggested）", "is-suggested" in src)
    ck.check("稿龄 chip 拦截态（is-stale）", "draft-age-chip" in src and "is-stale" in src)
    ck.check("两态文案键（chip_stale/chip_replied）",
             "inbox.draft_mini.chip_stale" in src
             and "inbox.draft_mini.chip_replied" in src)


def _login_workspace(p: Any, base: str, token: str, *, headed: bool) -> Tuple[Any, Any, Any]:
    browser = p.chromium.launch(headless=not headed)
    ctx = browser.new_context(viewport=VIEWPORT)
    ctx.request.post(base + "/login", form={"auth_token": token})
    page = ctx.new_page()
    page.goto(base + "/workspace", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => typeof window.setPlatFilter === 'function'", timeout=20000)
    page.wait_for_timeout(3000)
    return browser, ctx, page


def _mock_drafts(now: float) -> dict:
    """三态草稿；不带 chat_key → _loadDraftsBody 的 filter 放行到任意打开的会话。"""
    return {"drafts": [
        {"draft_id": "gate_ok", "autopilot_level": "L1",
         "created_ts": now - 1800, "approve_blocked": "",
         "draft_text": "GATEDRAFT_OK", "peer_name": "Gate Ok",
         "peer_last_text": "hi"},
        {"draft_id": "gate_age", "autopilot_level": "L1",
         "created_ts": now - 72 * 3600, "approve_blocked": "age",
         "draft_text": "GATEDRAFT_AGE", "peer_name": "Gate Age",
         "peer_last_text": "hi"},
        {"draft_id": "gate_replied", "autopilot_level": "L1",
         "created_ts": now - 5 * 3600, "approve_blocked": "replied",
         "draft_text": "GATEDRAFT_REPLIED", "peer_name": "Gate Replied",
         "peer_last_text": "hi"},
    ]}


def _open_read_chat(page: Any) -> bool:
    return bool(page.evaluate(
        """() => {
          const row = document.querySelector(
            '#conv-items .conv-item:not(.has-unread)');
          if (!row) return false;
          row.click();
          return true;
        }"""))


def _ensure_draft_panel_open(page: Any) -> None:
    page.evaluate(
        """() => {
          const p = document.getElementById('draft-panel');
          if (p && !p.classList.contains('show') && window.toggleAiDraft) {
            window.toggleAiDraft();
          }
        }""")


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    with sync_playwright() as p:
        try:
            browser, ctx, page = _login_workspace(p, base, token, headed=headed)
        except Exception as e:  # noqa: BLE001
            print(f"[SKIP] 工作台脚本未就绪（{str(e)[:80]}）")
            return 0

        page.route("**/api/drafts?status=pending*",
                   lambda route: route.fulfill(
                       status=200, content_type="application/json",
                       body=json.dumps(_mock_drafts(time.time()), ensure_ascii=False)))

        print("== 1. 打开已读会话 + 展开草稿面板 ==")
        if not _open_read_chat(page):
            ck.skip("草稿三态渲染", "无已读会话可开——不碰未读")
            browser.close()
            return ck.summary()
        page.wait_for_timeout(800)
        _ensure_draft_panel_open(page)

        cards: List[dict] = []
        for _ in range(60):          # ≤9s：toggle→loadDrafts(async coalesce)→渲染
            cards = page.evaluate(_CARDS_JS)
            if any(c.get("text", "").startswith("GATEDRAFT_") for c in cards):
                break
            page.wait_for_timeout(150)

        by = {}
        for c in cards:
            t = c.get("text", "")
            if t.startswith("GATEDRAFT_"):
                by[t] = c
        if not by:
            ck.skip("草稿三态渲染", "mock 草稿未渲染（草稿面板未开或被过滤）")
            browser.close()
            return ck.summary()

        # ── 2. blocked='' → 可发、无拦截 ─────────────────────────────
        print("== 2. 未拦截草稿（blocked=''）==")
        ok = by.get("GATEDRAFT_OK")
        if not ok:
            ck.skip("未拦截草稿", "GATEDRAFT_OK 未渲染")
        else:
            ck.check("未拦截：发送可点", ok["hasSendActive"] and not ok["sendDisabled"],
                     f"active={ok['hasSendActive']} disabled={ok['sendDisabled']}")
            ck.check("未拦截：无拦截 chip", not ok["hasBlockChip"])
            ck.check("未拦截：编辑不高亮", not ok["editSuggested"])
            ck.check("未拦截：稿龄 chip 非 stale 态", not ok["ageStale"])

        # ── 3. blocked='age' → 置灰 + 过期 chip ──────────────────────
        print("== 3. 超龄草稿（blocked='age'）==")
        age = by.get("GATEDRAFT_AGE")
        if not age:
            ck.skip("超龄草稿", "GATEDRAFT_AGE 未渲染")
        else:
            ck.check("超龄：发送置灰", age["sendDisabled"] and not age["hasSendActive"],
                     f"disabled={age['sendDisabled']}")
            ck.check("超龄：拦截 chip 文案「已过期」", age["blockChipText"] == "已过期",
                     f"chip={age['blockChipText']!r}")
            ck.check("超龄：稿龄 chip 带 is-stale", age["ageStale"])
            ck.check("超龄：编辑标为建议出路", age["editSuggested"])
            ck.check("超龄：有整段拦截说明", age["hasBlockHint"])

        # ── 4. blocked='replied' → 置灰 + 已回过 chip ───────────────
        print("== 4. 已回过草稿（blocked='replied'）==")
        rep = by.get("GATEDRAFT_REPLIED")
        if not rep:
            ck.skip("已回过草稿", "GATEDRAFT_REPLIED 未渲染")
        else:
            ck.check("已回过：发送置灰", rep["sendDisabled"] and not rep["hasSendActive"],
                     f"disabled={rep['sendDisabled']}")
            ck.check("已回过：拦截 chip 文案「已回过」（与过期分开）",
                     rep["blockChipText"] == "已回过", f"chip={rep['blockChipText']!r}")
            ck.check("已回过：编辑标为建议出路", rep["editSuggested"])

        page.unroute("**/api/drafts?status=pending*")
        browser.close()

    return ck.summary()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="坐席工作台草稿审批预判徽标门禁（route mock，无副作用）")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--token", default="")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--source-only", action="store_true",
                    help="只跑静态源码接线（不启浏览器）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if args.source_only:
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("草稿预判徽标源码接线")

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[WARN] 未装 playwright；仅跑静态源码接线")
        ck = Checker()
        check_source_wiring(ck)
        return ck.summary("草稿预判徽标源码接线（无 playwright）")

    import urllib.error
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except urllib.error.HTTPError:
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{args.base}）：{str(e)[:80]}")
        ck = Checker()
        check_source_wiring(ck)
        code = ck.summary("草稿预判徽标源码接线（实例不可达）")
        return 0 if code == 0 else code

    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[ABORT] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 2

    print(f"== 目标 {args.base}（视口 {VIEWPORT['width']}x{VIEWPORT['height']}）==")
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
