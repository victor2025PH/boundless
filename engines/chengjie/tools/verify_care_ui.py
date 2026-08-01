# -*- coding: utf-8 -*-
"""主动关怀页 真浏览器只读验证（Playwright；P3 2026-08-01 沉淀）。

**为什么需要它**：care_schedule.html 在 P0-P3 三轮里从 176 行长到 700+ 行
（链路四灯 / hero 空态 / 联系人选择器 / 主题·时间快捷片 / 待关怀卡片 / 样本审核 /
草稿预览），全部是纯前端行为且模板热更新直上生产。静态门禁只能证「函数挂了
window / id 不重复」，证不了「搜索选人真的选得中、卡片视图真的切得过去」。

与 ``tools/verify_account_rail_ui.py`` 同族（同一实例 + token 登录 + Playwright），
**只读**：不点「排上 / 立即发 / 取消 / 看 AI 会说什么」（后两者动状态或烧 LLM），
只做渲染与筛选层交互。

用法::

    python tools/verify_care_ui.py                # 打默认实例
    python tools/verify_care_ui.py --headed       # 肉眼看一遍

覆盖的不变量：
  1. 页面加载 + 链路自检灯条渲染出 4 盏灯。
  2. 按健康态正确切形态：引擎开 → 运行区 + 试运行/真发横幅；关 → hero 空态 + CTA。
  3. 「排一条关怀」面板齐件：搜索框 + ≥5 个主题快捷片 + ≥4 个时间快捷片。
  4. 选择器可用：聚焦加载最近会话 → 输入过滤 → 点第一行 → 选中态出现、搜索隐藏
     →「重新选人」恢复（全程不提交）。
  5. 状态筛选 pending → 卡片容器可见、表格隐藏；切 sent → 反转。
  6. i18n 无裸键：页面可见文本不含 "cs2_"。

缺 playwright / 实例不可达 → SKIP exit 0（不污染回归信号）。
gate_sweep.ps1 挂接待其空闲后补（该脚本当前由另一条线活跃编辑）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

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
        print(f"\n== 主动关怀页验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_STATE_JS = """() => {
  const vis = (id) => {
    const el = document.getElementById(id);
    return !!el && el.offsetParent !== null;
  };
  return {
    lights: document.querySelectorAll('#cs-health-lights .cs-light').length,
    hero: vis('cs-hero'),
    run: vis('cs-run'),
    banner: (document.getElementById('cs-banner') || {}).textContent || '',
    addPanel: vis('cs-add-panel'),
    topicChips: document.querySelectorAll('#cs-p-topics .cs-chip').length,
    whenChips: document.querySelectorAll('#cs-p-whens .cs-chip').length,
    cardsVisible: vis('cs-cards'),
    tableVisible: vis('cs-table-wrap'),
    pickVisible: vis('cs-p-pick'),
    selVisible: vis('cs-p-sel'),
    listRows: document.querySelectorAll('#cs-p-list .cs-p-item').length,
    listEmpty: document.querySelectorAll('#cs-p-list .cs-p-empty').length,
    selName: (document.getElementById('cs-p-sel-name') || {}).textContent || '',
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
            page.wait_for_function("() => typeof window.csHealth === 'function'",
                                   timeout=15000)
        except Exception:
            print("  [ABORT] care 页 JS 未就绪（登录失败或模板异常）")
            browser.close()
            return 1
        page.wait_for_timeout(2500)   # 等 csHealth/csLoad/csLoadSamples 首轮完成
        # 新会话首访的「简洁/完整模式」引导弹窗会拦截 pointer events——移除后再交互
        # （只动客户端 DOM，零服务端状态；真实坐席会点掉它，这里等价）。
        page.evaluate("() => { const m = document.getElementById('onboard-modal');"
                      " if (m) m.remove(); }")
        page.wait_for_timeout(200)

        print("== 1. 链路自检灯条 ==")
        st = page.evaluate(_STATE_JS)
        ck.check("四盏灯渲染", st["lights"] == 4, f"lights={st['lights']}")

        print("== 2. 健康态 → 页面形态 ==")
        health = ctx.request.get(base + "/api/care/health",
                                 headers={"Authorization": f"Bearer {token}"}).json()
        if health.get("enabled"):
            ck.check("引擎开 → 运行区可见", st["run"] and not st["hero"])
            ck.check("横幅有内容（试运行/真发）", len(st["banner"]) > 4)
        else:
            ck.check("引擎关 → hero 空态可见", st["hero"])

        if st["run"]:
            print("== 3. 排一条关怀 面板齐件 ==")
            ck.check("添加面板可见", st["addPanel"])
            ck.check("主题快捷片 ≥5", st["topicChips"] >= 5, f"n={st['topicChips']}")
            ck.check("时间快捷片 ≥4", st["whenChips"] >= 4, f"n={st['whenChips']}")

            print("== 4. 联系人选择器（只选不提交） ==")
            page.focus("#cs-p-search")
            page.wait_for_timeout(1500)   # 触发 _csEnsureChats 拉取
            st2 = page.evaluate(_STATE_JS)
            ck.check("聚焦后列表渲染（行或空态提示）",
                     st2["listRows"] > 0 or st2["listEmpty"] > 0,
                     f"rows={st2['listRows']}")
            if st2["listRows"] > 0:
                page.click("#cs-p-list .cs-p-item")
                page.wait_for_timeout(200)
                st3 = page.evaluate(_STATE_JS)
                ck.check("点行 → 选中态出现、搜索隐藏",
                         st3["selVisible"] and not st3["pickVisible"]
                         and len(st3["selName"]) > 0, f"sel={st3['selName']!r}")
                page.click("#cs-p-sel .cs-act")   # 重新选人
                page.wait_for_timeout(200)
                st4 = page.evaluate(_STATE_JS)
                ck.check("重新选人 → 搜索恢复", st4["pickVisible"] and not st4["selVisible"])

            print("== 5. 状态筛选切换视图 ==")
            page.select_option("#cs-status", "pending")
            page.wait_for_timeout(800)
            stp = page.evaluate(_STATE_JS)
            ck.check("pending → 卡片视图", stp["cardsVisible"] and not stp["tableVisible"])
            page.select_option("#cs-status", "sent")
            page.wait_for_timeout(800)
            sts = page.evaluate(_STATE_JS)
            ck.check("sent → 表格视图", sts["tableVisible"] and not sts["cardsVisible"])

        print("== 6. i18n 无裸键 ==")
        body_text = page.evaluate("() => document.body.innerText")
        ck.check("可见文本不含 cs2_ 裸键", "cs2_" not in body_text)

        if st["run"]:
            print("== 7. 深链 ?contact= 自动选中（工作台入口的另一半） ==")
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
                ck.check("深链自动选中该联系人",
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
