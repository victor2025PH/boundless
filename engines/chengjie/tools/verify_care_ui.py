# -*- coding: utf-8 -*-
"""主动关怀页 真浏览器只读验证（Playwright；P3 2026-08-01 沉淀，P7 2026-08-03 随
「AI 关怀管家」改版重写不变量）。

**为什么需要它**：care_schedule.html 的形态切换（管家条/hero、方案卡、折叠补卡、
引擎室懒加载、plan 接口降级回落）全部是纯前端行为且模板热更新直上生产。静态门禁
只能证「函数挂了 window / id 不重复」，证不了「选人真的选得中、引擎室展开真的
渲染四灯、plan 404 时降级横幅真的出现」。

与 ``tools/verify_account_rail_ui.py`` 同族（同一实例 + token 登录 + Playwright），
**只读**：不点「排上 / 立即发 / 跳过 / 改时间 / 看 AI 会说什么」（动状态或烧 LLM），
只做渲染与筛选层交互。

用法::

    python tools/verify_care_ui.py                # 打默认实例
    python tools/verify_care_ui.py --headed       # 肉眼看一遍

覆盖的不变量：
  1. 页面加载 + 主渲染完成（管家条或 hero 二选一可见）。
  2. 按引擎态正确切形态：开 → 管家条（状态 chip + 一句话方案）；关 → hero 空态 + CTA。
  3. 方案区渲染（分组卡片 或 正向空态文案），效果条至少出「待关怀」一格。
  4. 「补一条关怀」默认折叠；展开后齐件：搜索框 + ≥5 主题快捷片 + ≥4 时间快捷片。
  5. 选择器可用：聚焦加载最近会话 → 输入过滤 → 点第一行 → 选中态出现、搜索隐藏
     →「重新选人」恢复（全程不提交）。
  6. 引擎室默认折叠；展开 → 四盏灯渲染 + 历史表格懒加载出行/空态。
  7. 降级一致性：/api/care/plan 可用 ↔ 降级横幅隐藏；404（旧实例）↔ 横幅可见
     （方案接口未装载时页面必须仍以基础模式完整可用）。
  8. i18n 无裸键：页面可见文本不含 "cs2_" / "cs3_"。
  9. 深链 ?contact= 自动展开补卡面板并选中该联系人。

缺 playwright / 实例不可达 → SKIP exit 0（不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s（见 verify_inbox_density.py 同行注释）
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
        print(f"\n== 主动关怀页验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_STATE_JS = """() => {
  const vis = (id) => {
    const el = document.getElementById(id);
    return !!el && el.offsetParent !== null;
  };
  const txt = (id) => ((document.getElementById(id) || {}).textContent || '');
  return {
    steward: vis('cs-steward'),
    hero: vis('cs-hero'),
    run: vis('cs-run'),
    chip: txt('cs-st-chip-tx'),
    brief: txt('cs-st-brief'),
    planHtml: ((document.getElementById('cs-plan') || {}).innerHTML || ''),
    planCards: document.querySelectorAll('#cs-plan .cs3-card').length,
    statCells: document.querySelectorAll('#cs-stats .cs3-stat').length,
    degraded: vis('cs-degraded'),
    addOpen: !!(document.getElementById('cs-add-details') || {}).open,
    engineOpen: !!(document.getElementById('cs-engine-room') || {}).open,
    lights: document.querySelectorAll('#cs-health-lights .cs-light').length,
    tableVisible: vis('cs-table-wrap'),
    bodyRows: document.querySelectorAll('#cs-body tr').length,
    topicChips: document.querySelectorAll('#cs-p-topics .cs-chip').length,
    whenChips: document.querySelectorAll('#cs-p-whens .cs-chip').length,
    pickVisible: vis('cs-p-pick'),
    selVisible: vis('cs-p-sel'),
    listRows: document.querySelectorAll('#cs-p-list .cs-p-item').length,
    listEmpty: document.querySelectorAll('#cs-p-list .cs-p-empty').length,
    selName: txt('cs-p-sel-name'),
  };
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page.goto(base + "/care-schedule", wait_until="domcontentloaded")
        try:
            page.wait_for_function("() => typeof window.csPlanLoad === 'function'",
                                   timeout=15000)
        except Exception:
            print("  [ABORT] care 页 JS 未就绪（登录失败或模板异常）")
            browser.close()
            return 1
        page.wait_for_timeout(2500)   # 等 csPlanLoad 首轮完成（含降级回落路径）
        # 新会话首访的「简洁/完整模式」引导弹窗会拦截 pointer events——移除后再交互
        page.evaluate("() => { const m = document.getElementById('onboard-modal');"
                      " if (m) m.remove(); }")
        page.wait_for_timeout(200)

        auth_hdr = {"Authorization": f"Bearer {token}"}
        health = ctx.request.get(base + "/api/care/health", headers=auth_hdr).json()
        plan_resp = ctx.request.get(base + "/api/care/plan", headers=auth_hdr)
        plan_ok = plan_resp.ok

        print("== 1. 引擎态 → 页面形态 ==")
        st = page.evaluate(_STATE_JS)
        if health.get("enabled"):
            ck.check("引擎开 → 管家条可见、hero 隐藏", st["steward"] and not st["hero"])
            ck.check("状态 chip 有文字", len(st["chip"]) > 1, f"chip={st['chip']!r}")
            ck.check("一句话方案非空", len(st["brief"]) > 4)
        else:
            ck.check("引擎关 → hero 空态可见", st["hero"])

        if st["run"]:
            print("== 2. 方案区 + 效果条 ==")
            ck.check("方案区渲染（卡片或空态）", len(st["planHtml"]) > 10,
                     f"cards={st['planCards']}")
            ck.check("效果条至少 1 格", st["statCells"] >= 1, f"n={st['statCells']}")

            print("== 3. 降级一致性（plan 接口 <-> 横幅） ==")
            if plan_ok:
                ck.check("plan 可用 → 降级横幅隐藏", not st["degraded"])
            else:
                ck.check("plan 未装载 → 降级横幅可见（基础模式兜底）", st["degraded"])

            print("== 4. 补一条关怀（默认折叠 → 展开齐件） ==")
            ck.check("补卡面板默认折叠", not st["addOpen"])
            page.evaluate("() => { document.getElementById('cs-add-details').open = true; }")
            page.wait_for_timeout(200)
            st2 = page.evaluate(_STATE_JS)
            ck.check("主题快捷片 ≥5", st2["topicChips"] >= 5, f"n={st2['topicChips']}")
            ck.check("时间快捷片 ≥4", st2["whenChips"] >= 4, f"n={st2['whenChips']}")

            print("== 5. 联系人选择器（只选不提交） ==")
            page.focus("#cs-p-search")
            page.wait_for_timeout(1500)   # 触发 _csEnsureChats 拉取
            st3 = page.evaluate(_STATE_JS)
            ck.check("聚焦后列表渲染（行或空态提示）",
                     st3["listRows"] > 0 or st3["listEmpty"] > 0,
                     f"rows={st3['listRows']}")
            if st3["listRows"] > 0:
                page.click("#cs-p-list .cs-p-item")
                page.wait_for_timeout(200)
                st4 = page.evaluate(_STATE_JS)
                ck.check("点行 → 选中态出现、搜索隐藏",
                         st4["selVisible"] and not st4["pickVisible"]
                         and len(st4["selName"]) > 0, f"sel={st4['selName']!r}")
                page.click("#cs-p-sel .cs-act")   # 重新选人
                page.wait_for_timeout(200)
                st5 = page.evaluate(_STATE_JS)
                ck.check("重新选人 → 搜索恢复", st5["pickVisible"] and not st5["selVisible"])

            print("== 6. 引擎室（默认折叠 → 展开出四灯 + 历史表） ==")
            ck.check("引擎室默认折叠", not st["engineOpen"])
            page.evaluate("() => { document.getElementById('cs-engine-room').open = true; }")
            page.wait_for_timeout(1200)   # ontoggle → 懒加载历史
            st6 = page.evaluate(_STATE_JS)
            ck.check("四盏灯渲染", st6["lights"] == 4, f"lights={st6['lights']}")
            ck.check("历史表可见且有行/空态", st6["tableVisible"] and st6["bodyRows"] >= 1,
                     f"rows={st6['bodyRows']}")

            print("== 6b. 历史搜索 + 联系人筛选件（P2） ==")
            has_q = page.evaluate("() => !!document.getElementById('cs-hist-q')")
            ck.check("历史搜索框存在", bool(has_q))
            ck.check("联系人筛选 chip 容器存在",
                     bool(page.evaluate("() => !!document.getElementById('cs-hist-flt')")))
            if has_q:
                page.fill("#cs-hist-q", "zzz__no_match__zzz")
                page.wait_for_timeout(200)
                n_filtered = page.evaluate(
                    "() => document.querySelectorAll('#cs-body tr').length")
                ck.check("无命中搜索 → 单空态行", n_filtered == 1, f"rows={n_filtered}")
                page.fill("#cs-hist-q", "")
                page.wait_for_timeout(200)
                n_back = page.evaluate(
                    "() => document.querySelectorAll('#cs-body tr').length")
                ck.check("清空搜索 → 行恢复", n_back >= st6["bodyRows"],
                         f"rows={n_back}")

        print("== 7. i18n 无裸键 ==")
        body_text = page.evaluate("() => document.body.innerText")
        ck.check("可见文本不含 cs2_/cs3_/cs4_ 裸键",
                 "cs2_" not in body_text and "cs3_" not in body_text
                 and "cs4_" not in body_text)

        if st["run"]:
            print("== 8. 深链 ?contact= 自动展开并选中 ==")
            cid = page.evaluate(
                "() => { const r = document.querySelector('#cs-p-list .cs-p-item');"
                " return r ? r.getAttribute('data-cid') : ''; }")
            if cid:
                from urllib.parse import quote
                page.goto(base + "/care-schedule?contact=" + quote(cid, safe=""),
                          wait_until="domcontentloaded")
                page.wait_for_timeout(4000)
                page.evaluate("() => { const m = document.getElementById('onboard-modal');"
                              " if (m) m.remove(); }")
                std = page.evaluate(_STATE_JS)
                ck.check("深链 → 补卡面板展开", std["addOpen"])
                ck.check("深链 → 自动选中该联系人",
                         std["selVisible"] and len(std["selName"]) > 0,
                         f"sel={std['selName']!r}")
            else:
                print("  [SKIP] 无最近会话可作深链目标")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("[SKIP] 读不到实例 auth_token")
        return 0
    import urllib.request
    try:
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print("[SKIP] 实例不可达: " + args.base)
        return 0
    return run(args.base, token, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
