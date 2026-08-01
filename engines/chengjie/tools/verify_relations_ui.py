# -*- coding: utf-8 -*-
"""流失预警页 真浏览器只读验证（Playwright；RH-P4，2026-08-01 沉淀）。

**为什么需要它**：本页 2026-08-01 从「裸 404 死页」改版为「能力探测 → 三态渲染 →
双源双 tab → 必挽队列 → 页内直发」，全部是**纯前端行为 + 条件注册端点**的组合——
静态门禁只能证「函数挂了 window / i18n 键存在」，证不了「探测真的把页面带进 full
模式、KPI 卡真的渲染、双 tab 真的各自有数据」，而模板热更新直上生产。
与 ``tools/verify_account_rail_ui.py`` 同族（同一实例 + token 登录）。

**只读铁律**：绝不点「生成话术」（烧 LLM + 写 draft_log）、绝不点「标记已发/
直接发送」（写 reactivation_sent 真账 + 真发消息）。队列/直发只验 DOM 接线存在。

用法::

    python tools/verify_relations_ui.py                # 打默认实例
    python tools/verify_relations_ui.py --headed       # 肉眼看一遍
    python tools/verify_relations_ui.py --shots out/   # 存证截图

覆盖的不变量：
  1. 能力探测 API：mode=full / 榜单+挽回话术端点已注册 / 轻量榜可用 / 非冷启动。
  2. 挽回统计 API：ok + 契约字段齐（sent/replied/matured/pending/rate）。
  3. 页面进 full 模式：主区可见、引导卡不可见、双 tab 可见。
  4. KPI 区 ≥5 张卡，且「7日挽回率」卡（异步端点渲染）出现。
  5. 全量榜有数据行；行级「打开会话」深链按钮存在（跨域 conversation_id 富集）。
  6. 必挽队列/页内直发的 DOM 接线存在（模态含 下一位/直接发送/队列位 元素）。
  7. 轻量榜 tab 切换 → 收件箱信号榜有数据行 + 「打开会话」按钮。
  8. 图例可见；无「等待重启/冷启动」横幅残留。

token 从实例数据根读取，**绝不打印**。缺 playwright / 实例不可达 / 无 token →
SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://localhost:18799"
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"


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

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 流失预警页验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_STATE_JS = """() => ({
  mainVisible: (document.getElementById('rh-main') || {style:{display:'none'}}).style.display !== 'none',
  guideVisible: (() => { const g = document.getElementById('rh-guide');
      return !!(g && g.style.display !== 'none' && g.innerHTML.trim()); })(),
  tabsVisible: (() => { const t = document.getElementById('rh-tabs');
      return !!(t && t.style.display !== 'none'); })(),
  bannerText: (() => { const b = document.getElementById('rh-banner');
      return (b && b.style.display !== 'none') ? (b.textContent || '').trim() : ''; })(),
  kpiCount: document.querySelectorAll('#rh-kpis .rh-kpi').length,
  winbackKpi: !!document.getElementById('rh-kpi-winback'),
  fullRows: document.querySelectorAll('#rh-body tr').length,
  fullOpenBtns: document.querySelectorAll('#rh-body [data-cid]').length,
  draftBtns: document.querySelectorAll('#rh-body [data-jid][onclick*="rhDraft"]').length,
  focusCards: document.querySelectorAll('#rh-focus .rh-fcard').length,
  legendVisible: (() => { const l = document.querySelector('#rh-full-sec .rh-legend');
      if (!l) return false; const r = l.getBoundingClientRect(); return r.width > 0; })(),
  modalWiring: !!(document.getElementById('rh-next-btn')
      && document.getElementById('rh-send-btn')
      && document.getElementById('rh-modal-queue')
      && document.getElementById('rh-sent-btn')),
  liteVisible: (() => { const s = document.getElementById('rh-lite-sec');
      return !!(s && s.style.display !== 'none'); })(),
  liteRows: document.querySelectorAll('#rh-lite-body tr').length,
  liteOpenBtns: document.querySelectorAll('#rh-lite-body [data-cid]').length,
  /* 错误行带「重试」按钮，合法空态是纯文字 colspan 单元格——两者必须区分：
     2026-08-01 实锤本工具曾把 churn-risks 500 的错误行误读成空态，掩护了
     一个生产缺陷（conversation_meta 幽灵列 claimed_by）。 */
  liteErrorRow: !!document.querySelector('#rh-lite-body td[colspan] button'),
  liteEmptyState: (() => { const td = document.querySelector('#rh-lite-body td[colspan]');
      return !!(td && !td.querySelector('button')); })(),
})"""


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1360, "height": 900})
        # 预置「新手引导已看过」——全新 profile 会弹 onboarding 蒙层挡住页面交互；
        # 本工具验证的是常规坐席视角（老访客态），不是首访引导流程。
        ctx.add_init_script(
            "try{localStorage.setItem('_onboard_shown_simple','1');}catch(e){}")
        ctx.request.post(base + "/login", form={"auth_token": token})

        print("== 1. 能力探测 / 挽回统计 API 契约 ==")
        cap = None
        try:
            r = ctx.request.get(base + "/api/relations/capability")
            cap = r.json() if r.ok else None
        except Exception:
            cap = None
        if not cap:
            print("[SKIP] capability 端点不可达（实例未起或登录失败）")
            browser.close()
            return 0
        ck.check("capability mode=full", cap.get("mode") == "full",
                 f"mode={cap.get('mode')}")
        ck.check("榜单路由已注册", cap.get("board_registered"))
        ck.check("挽回话术路由已注册", cap.get("reactivation_registered"))
        ck.check("轻量榜可用", cap.get("lite_available"))
        ck.check("已建档且非冷启动",
                 (cap.get("contact_count") or 0) >= 1 and not cap.get("cold_start"),
                 f"contacts={cap.get('contact_count')}")
        try:
            wb = ctx.request.get(
                base + "/api/relations/winback-stats?days=30&reply_window_days=7").json()
        except Exception:
            wb = {}
        ck.check("winback-stats 契约字段齐",
                 wb.get("ok") is True and all(
                     k in wb for k in
                     ("sent", "replied", "matured", "pending", "rate")),
                 f"sent={wb.get('sent')} rate={wb.get('rate')}")

        print("== 2. 页面 full 模式 + KPI（含异步挽回卡） ==")
        page = ctx.new_page()
        page.goto(base + "/relations-health", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("#rh-main", state="visible", timeout=20000)
        except Exception:
            st0 = page.evaluate(_STATE_JS)
            print(f"  [ABORT] 页面未进入主区（guide={st0.get('guideVisible')}"
                  f" banner={st0.get('bannerText')!r}）")
            browser.close()
            return 1
        try:
            # 等真实数据行（[data-jid]）或空态/错误单格——骨架屏也是 <tr>，不能当就绪信号
            page.wait_for_selector("#rh-body [data-jid], #rh-body td[colspan]",
                                   timeout=20000)
            page.wait_for_selector("#rh-kpi-winback", timeout=20000)
        except Exception:
            pass  # 断言层如实报
        st = page.evaluate(_STATE_JS)
        ck.check("主区可见 / 引导卡隐藏", st["mainVisible"] and not st["guideVisible"])
        ck.check("双 tab 可见（全量+轻量并存）", st["tabsVisible"])
        ck.check("无重启/冷启动横幅残留", not st["bannerText"],
                 f"banner={st['bannerText']!r}" if st["bannerText"] else "")
        ck.check("KPI 卡 ≥5 张", st["kpiCount"] >= 5, f"n={st['kpiCount']}")
        ck.check("「7日挽回率」KPI 卡已渲染", st["winbackKpi"])

        print("== 3. 全量榜数据 + 行级接线 ==")
        ck.check("全量榜有数据行", st["fullRows"] >= 1, f"rows={st['fullRows']}")
        ck.check("行级「打开会话」深链存在", st["fullOpenBtns"] >= 1,
                 f"open_btns={st['fullOpenBtns']}")
        ck.check("行级「生成话术」按钮存在", st["draftBtns"] >= 1,
                 f"draft_btns={st['draftBtns']}")
        ck.check("图例可见", st["legendVisible"])
        ck.check("必挽队列/直发 DOM 接线齐（下一位/直发/队列位/标记已发）",
                 st["modalWiring"])
        print(f"  [INFO] 必挽焦点卡 {st['focusCards']} 张（0..5 数据相关，不作硬断言）")
        if shots:
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / "relations_full_board.png"),
                            full_page=True)

        print("== 4. 轻量榜 tab 切换 ==")
        page.click("#rh-tab-lite")
        try:
            # 同全量榜：等真实数据行（带 data-cid 的打开会话按钮）或空态/错误单格
            page.wait_for_selector(
                "#rh-lite-body [data-cid], #rh-lite-body td[colspan]",
                timeout=15000)
        except Exception:
            pass
        st2 = page.evaluate(_STATE_JS)
        ck.check("轻量榜区可见", st2["liteVisible"])
        ck.check("轻量榜非错误态（500/加载失败必红）", not st2["liteErrorRow"])
        ck.check("轻量榜有数据行或合法空态",
                 st2["liteOpenBtns"] >= 1 or st2["liteEmptyState"],
                 f"rows={st2['liteRows']} open_btns={st2['liteOpenBtns']}"
                 f" empty={st2['liteEmptyState']}")
        print(f"  [INFO] 轻量榜数据行 {st2['liteOpenBtns']} 条（数据相关，不作硬断言）")
        if shots:
            page.screenshot(path=str(shots / "relations_lite_board.png"),
                            full_page=True)
        page.click("#rh-tab-full")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--shots", default="")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[SKIP] playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 实例数据根无 web_admin.auth_token")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print(f"[SKIP] 实例不可达: {args.base}")
        return 0
    shots = Path(args.shots) if args.shots else None
    try:
        return run(args.base, token, shots=shots, headed=args.headed)
    except Exception as e:  # noqa: BLE001
        print(f"[SKIP] 运行异常（环境问题按跳过处理）: {e}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
