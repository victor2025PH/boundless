# -*- coding: utf-8 -*-
"""目标达成报表页 /workspace/goal-report 真浏览器只读门禁（Playwright；
P2 2026-08-09 随报表页 P0-P2 三批前端落地）。

**为什么需要它**：这页承载了「报表 → 动作」闭环的全部前端状态机——勾选批量、
两段式触达（预检→确认）、单发弹层、后续链一键启动、URL 深链预填、趋势/热力
条件渲染、403 未启用引导。全是运行时行为，哑按钮/孤儿引用等静态门禁只能证
「函数挂了 window」，证不了「点了以后对不对」；而模板热更新**直接上生产**，
坏一次坐席立刻踩。

**模式**：与 tools/verify_inbox_identity.py 同族——live 实例 + token 登录 +
Playwright ``page.route`` 把 /api/goals/report/* 等**全部数据接口换成合成响应**：
断言的是模板+JS 对固定数据的渲染与交互，不依赖生产库里有没有目标数据，
**零生产写入**（出站类接口只 mock 预检，绝不点「确认发送」真发路径）。

覆盖的不变量（编号对应 run() 断言）：
  0   源码接线（静态，零实例依赖）：行动作走事件委托 data-gr-* 而非内联拼接、
      预检/执行分离、深链预填与趋势/热力渲染函数在位
  S1  KPI 摘要条按 accounts 响应渲染（done/won_amount 等非 '—'）
  S2  账号视角表出行 + 完成率条形
  S3  趋势卡与热力卡按数据显隐（有数据=显示，空容器=隐藏）
  S4  客户明细：已跟进/待跟进徽章、🔥 完成 48h 内高亮、金额列
  S5  深链预填：?status=ended&days=7 → 两个筛选控件被预置
  S6  单发弹层：行「发消息」→ 收件人正确、模板 chip 填入文案（不发送）
  S7  勾选 → 底部批量条出现且计数正确；清空选择 → 条消失
  S8  批量触达弹层：预检 → 可发/跳过（原因）/语言分布渲染、确认按钮带人数
      （**到此为止**，不点确认——真发路径由服务端门禁与 e2e 另测）
  S9  后续链按钮：完成行带 🔁（链清单 mock 在位）
  S10 403 模式：accounts 接口回 403 → 未启用引导可见、正文隐藏

用法::

    python tools/verify_goal_report_ui.py             # 门禁模式
    python tools/verify_goal_report_ui.py --headed    # 肉眼看一遍

缺 playwright / 实例不可达 → SKIP exit 0；**页面路由 404（后端批次还没随
重启装载）→ SKIP exit 0**（重启后本门禁自动转实跑）；无 token → ABORT exit 2。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"
VIEWPORT = {"width": 1440, "height": 900}
ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPORT_TEMPLATE = ENGINE_ROOT / "src" / "web" / "templates" / "goal_report.html"

_NOW = time.time()

# ── 合成响应（与 matrix_report / contacts_report / outreach preview 契约对齐）──

FX_ACCOUNTS = {
    "ok": True, "window_days": 7,
    "totals": {"active": 3, "done": 5, "failed": 1, "expired": 1,
               "cancelled": 0, "won": 3, "won_amount": 597.0,
               "done_rate": 0.714, "avg_days_to_done": 3.2},
    "accounts": [
        {"platform": "telegram", "account_id": "8244899900", "active": 2,
         "done": 4, "failed": 1, "expired": 0, "cancelled": 0, "won": 2,
         "won_amount": 398.0, "done_rate": 0.8, "avg_days_to_done": 2.9,
         "top_template": "conversion_unlock"},
        {"platform": "telegram", "account_id": "8438080491", "active": 1,
         "done": 1, "failed": 0, "expired": 1, "cancelled": 0, "won": 1,
         "won_amount": 199.0, "done_rate": 0.5, "avg_days_to_done": 4.5,
         "top_template": "conversion_subscribe"},
    ],
    "trend": [{"day": f"08-{i:02d}", "done": (i % 3), "won": (i % 2)}
              for i in range(1, 15)],
    "heat": {
        "accounts": ["telegram:8244899900", "telegram:8438080491"],
        "templates": [{"id": "conversion_unlock", "name": "付费解锁"},
                      {"id": "conversion_subscribe", "name": "会员订阅"}],
        "cells": {"telegram:8244899900": {"conversion_unlock": 3,
                                          "conversion_subscribe": 1},
                  "telegram:8438080491": {"conversion_subscribe": 1}},
    },
}

FX_CONTACTS = {
    "ok": True, "window_days": 7, "status": "done", "page": 1,
    "page_size": 25, "total": 3,
    "rows": [
        {"goal_id": "g1", "conversation_id": "telegram:8244899900:111",
         "platform": "telegram", "account_id": "8244899900",
         "chat_key": "111", "contact_name": "小美",
         "template": "conversion_unlock", "template_name": "付费解锁",
         "title": "付费解锁", "status": "done", "progress": 1.0,
         "milestone_idx": 3, "created_by": "agent", "result_kind": "order",
         "amount": 199, "product": "pro", "created_at": _NOW - 5 * 86400,
         "done_at": _NOW - 3600, "days_to_done": 4.9,
         "followed_up": False, "rec_chain": "starter_reactivate"},
        {"goal_id": "g2", "conversation_id": "telegram:8438080491:222",
         "platform": "telegram", "account_id": "8438080491",
         "chat_key": "222", "contact_name": "老王",
         "template": "conversion_subscribe", "template_name": "会员订阅",
         "title": "会员订阅", "status": "done", "progress": 1.0,
         "milestone_idx": 3, "created_by": "auto_create",
         "result_kind": "manual", "amount": 398, "product": "",
         "created_at": _NOW - 9 * 86400, "done_at": _NOW - 5 * 86400,
         "days_to_done": 4.0, "followed_up": True, "rec_chain": ""},
        # P6：失守三分法行——expired + offered（琥珀徽章「已开价未成交」）
        {"goal_id": "g3", "conversation_id": "telegram:8244899900:333",
         "platform": "telegram", "account_id": "8244899900",
         "chat_key": "333", "contact_name": "阿strong",
         "template": "acquire_and_convert", "template_name": "获客转化",
         "title": "获客转化", "status": "expired", "progress": 0.9,
         "milestone_idx": 4, "created_by": "auto_create",
         "result_kind": "", "amount": None, "product": "",
         "created_at": _NOW - 12 * 86400, "done_at": _NOW - 2 * 86400,
         "days_to_done": 10.0, "miss_kind": "offered"},
    ],
}

FX_TEMPLATES = {
    "templates": [
        {"id": "conversion_unlock", "name_zh": "付费解锁", "name_en": "Unlock"},
        {"id": "conversion_subscribe", "name_zh": "会员订阅",
         "name_en": "Subscribe"},
    ],
    "autonomy_levels": ["observe", "suggest", "auto"],
    "statuses": ["active", "paused", "done", "failed", "expired", "cancelled"],
    "caps": {}, "pickers": {},
}

FX_CHAINS = {"ok": True, "chains": [
    {"chain_id": "starter_reactivate", "name": "沉默唤回跟进"}]}

FX_OB_PREVIEW = {
    "ok": True, "generated_at": int(_NOW), "total_matched": 2,
    "eligible_count": 1, "skipped_count": 1,
    "eligible": [{"conversation_id": "telegram:8244899900:111",
                  "platform": "telegram", "account_id": "8244899900",
                  "chat_key": "111", "display_name": "小美", "last_ts": _NOW,
                  "silent_days": 0.1, "tags": [], "rel_stage": "",
                  "language": "en"}],
    "skipped": [{"conversation_id": "telegram:8438080491:222",
                 "reason": "cooldown"}],
    "per_account": {"8244899900": {"assigned": 1, "remaining_before": 30,
                                   "cap": 30}},
    "estimated_seconds": 8.0,
}


def read_token(data_root: str) -> str:
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

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
              + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        tail = f"  FAILED: {fails}" if fails else " =="
        print(f"\n== 目标达成报表页验证: {total - len(fails)}/{total} PASS{tail}")
        return 1 if fails else 0


def check_source_wiring(ck: Checker) -> None:
    """零实例依赖的静态接线（防「删一段 JS 页面照样打得开」的静默回归）。"""
    print("== 0. 源码接线（静态）==")
    if not REPORT_TEMPLATE.exists():
        ck.check("goal_report.html 存在", False, str(REPORT_TEMPLATE))
        return
    src = REPORT_TEMPLATE.read_text(encoding="utf-8")
    ck.check("行动作走事件委托（data-gr-send）", "data-gr-send" in src)
    ck.check("后续链按钮走事件委托（data-gr-chain）", "data-gr-chain" in src)
    ck.check("批量预检与执行分离",
             "grObPreview" in src and "grObExecute" in src)
    ck.check("执行需显式 confirm", '"confirm": true' in src.replace("'", '"')
             or "confirm: true" in src)
    ck.check("深链预填在位（URLSearchParams）", "URLSearchParams" in src)
    ck.check("趋势/热力渲染在位",
             "gr-trend-card" in src and "gr-heat-card" in src)
    ck.check("跟进推导消费 followed_up", "followed_up" in src)
    ck.check("日期格式走 wsFmt*（不裸 toLocale）",
             "wsFmtDate" in src and "toLocaleTimeString" not in src)


def _route_mocks(ctx: Any, *, accounts_status: int = 200) -> None:
    def _json(route: Any, data: Any, status: int = 200) -> None:
        route.fulfill(status=status, content_type="application/json",
                      body=json.dumps(data, ensure_ascii=False))

    ctx.route("**/api/goals/report/accounts*",
              lambda r: _json(r, FX_ACCOUNTS if accounts_status == 200
                              else {"detail": "disabled"}, accounts_status))
    ctx.route("**/api/goals/report/contacts*", lambda r: _json(r, FX_CONTACTS))
    ctx.route("**/api/goals/templates*", lambda r: _json(r, FX_TEMPLATES))
    ctx.route("**/api/workspace/workflow-chains*", lambda r: _json(r, FX_CHAINS))
    ctx.route("**/api/unified-inbox/outreach/preview*",
              lambda r: _json(r, FX_OB_PREVIEW))
    # 兜底：execute/send 若被误触 → 507 哨兵状态（断言里没有点它们，撞上=红）
    ctx.route("**/api/unified-inbox/outreach/execute*",
              lambda r: _json(r, {"ok": False, "reason": "gate_probe"}, 507))
    ctx.route("**/api/unified-inbox/send*",
              lambda r: _json(r, {"ok": False}, 507))


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    check_source_wiring(ck)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport=VIEWPORT)
        ctx.request.post(base + "/login", form={"auth_token": token})
        _route_mocks(ctx)
        page = ctx.new_page()
        resp = page.goto(base + "/workspace/goal-report",
                         wait_until="domcontentloaded")
        if resp is not None and resp.status == 404:
            print("[SKIP-live] /workspace/goal-report 路由未装载"
                  "（后端批次待重启）——重启后本门禁自动转实跑；静态接线仍生效")
            browser.close()
            return ck.summary()
        if "/workspace/goal-report" not in page.url:
            print(f"[SKIP-live] 被重定向（{page.url}）——token 非主管或页面被闸；"
                  "静态接线仍生效")
            browser.close()
            return ck.summary()
        page.wait_for_function(
            "() => typeof window.grLoad === 'function'", timeout=15000)
        page.wait_for_timeout(1200)

        print("== 1. KPI / 账号表 / 趋势热力（合成数据渲染）==")
        ck.check("S1 KPI done=5",
                 page.locator("#kpi-done").inner_text().strip() == "5")
        ck.check("S1 KPI 赢单金额 $597",
                 "$597" in page.locator("#kpi-amount").inner_text())
        rows = page.locator("#gr-acct-rows tr").count()
        ck.check("S2 账号表两行", rows == 2, f"rows={rows}")
        ck.check("S2 完成率条形在位",
                 page.locator("#gr-acct-rows .gr-bar").count() >= 2)
        ck.check("S3 趋势卡可见",
                 page.locator("#gr-trend-card").is_visible())
        ck.check("S3 热力卡可见",
                 page.locator("#gr-heat-card").is_visible())

        print("== 2. 客户明细（徽章/热度/金额/链按钮/三分法）==")
        ct = page.locator("#gr-ct-rows tr")
        ck.check("S4 明细三行", ct.count() == 3, f"rows={ct.count()}")
        body_txt = page.locator("#gr-ct-rows").inner_text()
        ck.check("S4 待跟进徽章在（g1）", "待跟进" in body_txt)
        ck.check("S4 已跟进徽章在（g2）", "已跟进" in body_txt)
        ck.check("S4 🔥 48h 热度标记在", "🔥" in body_txt)
        ck.check("S4 金额列 $199", "$199" in body_txt)
        ck.check("S9 完成行带后续链按钮",
                 page.locator("#gr-ct-rows [data-gr-chain]").count() == 1)
        ck.check("S11 失守三分法徽章（已开价未成交）",
                 "已开价未成交" in body_txt or "Offered" in body_txt)

        print("== 3. 单发弹层（不发送）==")
        page.locator("#gr-ct-rows [data-gr-send]").first.click()
        page.wait_for_timeout(200)
        ck.check("S6 弹层打开、收件人正确",
                 page.locator("#gr-modal-bg").is_visible()
                 and "小美" in page.locator("#gr-send-to").inner_text())
        page.evaluate("grFillTpl('thanks')")
        ck.check("S6 模板 chip 填入文案",
                 len(page.locator("#gr-send-text").input_value()) > 4)
        page.evaluate("grCloseModal()")

        print("== 4. 勾选与批量触达（到预检为止）==")
        page.locator("#gr-ct-rows input[data-gr-sel]").first.check()
        page.wait_for_timeout(150)
        ck.check("S7 批量条出现且计数=1",
                 page.locator("#gr-batchbar").is_visible()
                 and "1" in page.locator("#gr-bb-count").inner_text())
        page.evaluate("grOpenOutreach()")
        page.wait_for_timeout(150)
        page.locator("#gr-ob-text").fill("谢谢支持，{name}！")
        page.evaluate("grObPreview()")
        page.wait_for_timeout(400)
        ob_txt = page.locator("#gr-ob-preview").inner_text()
        ck.check("S8 预检出可发人数", "1" in ob_txt)
        ck.check("S8 预检出跳过原因（冷却）", "冷却" in ob_txt or "cooldown" in ob_txt)
        ck.check("S8 预检出语言分布", "en" in ob_txt)
        exec_btn = page.locator("#gr-ob-exec-btn")
        ck.check("S8 确认按钮点亮且带人数",
                 exec_btn.is_enabled() and "1" in exec_btn.inner_text())
        page.evaluate("grCloseOutreach()")
        page.evaluate("grClearSel()")
        page.wait_for_timeout(150)
        ck.check("S7 清空后批量条消失",
                 not page.locator("#gr-batchbar").is_visible())

        print("== 5. 深链预填 ==")
        page.goto(base + "/workspace/goal-report?status=ended&days=7",
                  wait_until="domcontentloaded")
        page.wait_for_function(
            "() => typeof window.grLoad === 'function'", timeout=15000)
        page.wait_for_timeout(800)
        ck.check("S5 status 预填 ended",
                 page.locator("#f-status").input_value() == "ended")
        ck.check("S5 days 预填 7",
                 page.locator("#f-days").input_value() == "7")

        print("== 6. 未启用引导（403）==")
        ctx2 = browser.new_context(viewport=VIEWPORT)
        ctx2.request.post(base + "/login", form={"auth_token": token})
        _route_mocks(ctx2, accounts_status=403)
        page2 = ctx2.new_page()
        page2.goto(base + "/workspace/goal-report",
                   wait_until="domcontentloaded")
        page2.wait_for_function(
            "() => typeof window.grLoad === 'function'", timeout=15000)
        page2.wait_for_timeout(800)
        ck.check("S10 未启用引导可见",
                 page2.locator("#gr-disabled").is_visible())
        ck.check("S10 正文隐藏",
                 not page2.locator("#gr-body").is_visible())
        ctx2.close()
        browser.close()

    return ck.summary()


def main() -> int:
    # Windows 控制台默认 GBK——断言名里的 emoji/中文一律按 UTF-8 出，
    # 编不过去也只降级替换字符，绝不让「打印」本身把门禁打红。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] 未安装 playwright——跳过（exit 0）")
        return 0

    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 实例不可达（{str(e)[:60]}）——跳过（exit 0）")
        return 0

    token = read_token(args.data_root)
    if not token:
        print("[ABORT] 读不到 web_admin.auth_token（exit 2）")
        return 2
    try:
        return run(args.base, token, headed=args.headed)
    except Exception as e:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        print(f"[FAIL] 门禁异常：{e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
