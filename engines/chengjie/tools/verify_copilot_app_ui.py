# -*- coding: utf-8 -*-
"""统一 App 副驾（/copilot/app.html）真浏览器只读验证（P2.5，2026-08-12）。

**为什么需要它**：统一 App 是桌面壳右栏的默认形态 + 网页 App 模式灰度的目标形态，
纯前端且热更新直上生产。本系列三个「静态门禁抓不到、只有真浏览器能抓」的实锤：
  - hero 卡被 flex 压成 2px（.cp-card overflow:hidden 把 min-height:auto 算 0，
    截图冒烟才发现——装配门禁只看 DOM 存在性，不看几何）；
  - 账号图标 hidden 属性被 .cp-tabs-tool 的 display:inline-flex 盖掉（属性在、
    显示也在）；
  - applyI18n 按 textContent 覆盖会吃掉 button 内图标（结构性回归）。

与 ``tools/verify_care_ui.py`` 同族（Playwright 只读）；app.html 鉴权走 #token=
hash（桌面 iframe 同款），不经 /login。**只读**：不点生成/发送/启停/归档，
只验渲染几何、层开合、tab 切换与 i18n 无裸键。

用法::

    python tools/verify_copilot_app_ui.py            # 打默认实例
    python tools/verify_copilot_app_ui.py --headed   # 肉眼看一遍

覆盖的不变量：
  1. 空态（无 cid）：三 tab 常显 + 每 tab 有 SVG 图标（applyI18n 没吃图标）+
     空态卡可见 + 账号 CTA 存在 + hero 隐藏（no-ctx）。
  2. 账号图标默认可见；点开 → 覆盖层可见（body.acct-open）+ 返回按钮；
     点返回 → 关闭。层内 cp-accounts 组件已渲染（shadowRoot 有内容）。
  3. ?hostAccounts=1：账号图标真隐藏（几何级判定，钉 display 盖 hidden 缺陷类）。
  4. 会话态（?cid=）：hero 卡保持移除态（2026-08-14「AI 下一步」下线决策；app 端
     复现可见 hero=单边装配漂移）；切 customer/tools tab → 对应 sec 激活；
     账号层打开时点 tab 自动关层。
  5. i18n 无裸键：页面可见文本不含 "cp.app." / "cp.convops." / "cp.kb."。

缺 playwright / 实例不可达 → SKIP exit 0（不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改回 localhost：::1 回退每连接 ~2s
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
        print(f"\n== 统一 App 副驾验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_STATE_JS = """() => {
  const q = (s) => document.querySelector(s);
  const vis = (el) => !!el && el.getBoundingClientRect().height > 0
                      && getComputedStyle(el).display !== 'none';
  const hero = q('.cp-hero');
  const acct = document.getElementById('cp-acct-open');
  const layer = document.getElementById('cp-acct-layer');
  const accEl = document.getElementById('accounts');
  return {
    tabs: document.querySelectorAll('.cp-tab').length,
    tabIcons: document.querySelectorAll('.cp-tab .cp-tab-ic svg').length,
    noCtx: (document.getElementById('cp-app') || {}).className.includes('no-ctx'),
    emptyCardVisible: vis(q('.cp-sec.active .cp-empty-card')),
    ctaCount: document.querySelectorAll('[data-acct-cta]').length,
    heroVisible: vis(hero),
    heroHeight: hero ? Math.round(hero.getBoundingClientRect().height) : -1,
    acctIconVisible: vis(acct),
    acctLayerOpen: document.body.classList.contains('acct-open') && vis(layer),
    acctShadowLen: accEl && accEl.shadowRoot ? accEl.shadowRoot.innerHTML.length : 0,
    activeSec: (q('.cp-sec.active') || {}).dataset ? q('.cp-sec.active').dataset.tab : '',
    bodyText: document.body.innerText || '',
  };
}"""


def run(base: str, token: str, *, headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 420, "height": 900})
        page = ctx.new_page()

        def goto(query: str) -> None:
            page.goto(f"{base}/copilot/app.html?theme=dark{query}#token={token}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(900)   # applyI18n + 组件 upgrade + 首轮取数

        print("== 1. 空态（无 cid）==")
        goto("")
        st = page.evaluate(_STATE_JS)
        ck.check("三 tab 常显", st["tabs"] == 3, f"n={st['tabs']}")
        ck.check("每 tab 有 SVG 图标（applyI18n 未吃图标）", st["tabIcons"] == 3,
                 f"icons={st['tabIcons']}")
        ck.check("no-ctx 空态卡可见", st["noCtx"] and st["emptyCardVisible"])
        ck.check("账号 CTA 存在（三 tab 各一）", st["ctaCount"] == 3, f"n={st['ctaCount']}")
        ck.check("hero 在空态隐藏", not st["heroVisible"])
        ck.check("账号图标默认可见", st["acctIconVisible"])

        print("== 2. 账号覆盖层开合 ==")
        page.click("#cp-acct-open")
        page.wait_for_timeout(600)
        st = page.evaluate(_STATE_JS)
        ck.check("点图标 → 覆盖层打开", st["acctLayerOpen"])
        ck.check("cp-accounts 组件已渲染", st["acctShadowLen"] > 100,
                 f"shadow={st['acctShadowLen']}")
        page.click("#cp-acct-back")
        page.wait_for_timeout(300)
        st = page.evaluate(_STATE_JS)
        ck.check("点返回 → 覆盖层关闭", not st["acctLayerOpen"])

        print("== 3. 宿主声明 hostAccounts=1 ==")
        goto("&hostAccounts=1")
        st = page.evaluate(_STATE_JS)
        ck.check("账号图标真隐藏（几何级，钉 display 盖 hidden）", not st["acctIconVisible"])
        ck.check("空态 CTA 仍在（永不做死路）", st["ctaCount"] == 3)

        print("== 4. 会话态（假 cid，组件按 err/empty 态渲染骨架）==")
        goto("&cid=telegram:default:__gate_probe__&ck=__gate_probe__")
        page.wait_for_timeout(1200)
        st = page.evaluate(_STATE_JS)
        # 2026-08-14「AI 下一步」面板整体下线：app 端英雄卡随之移除（panel-manifest
        # hero 条目注：hero 仅 web surface）。断言随决策翻转为「不复活」——app 端
        # 出现可见 hero=单边加卡的装配漂移（web 端 hero 归 panel-manifest 门禁守）。
        ck.check("hero 已按 2026-08-14 决策移除（app 端不复活）",
                 not st["heroVisible"], f"h={st['heroHeight']}")
        page.click('.cp-tab[data-tab="customer"]')
        page.wait_for_timeout(300)
        st = page.evaluate(_STATE_JS)
        ck.check("切 customer tab 生效", st["activeSec"] == "customer")
        # 账号层打开时点 tab 必须自动关层（tabs 常显=天然退出口）
        page.click("#cp-acct-open")
        page.wait_for_timeout(400)
        page.click('.cp-tab[data-tab="tools"]')
        page.wait_for_timeout(300)
        st = page.evaluate(_STATE_JS)
        ck.check("账号层打开时点 tab → 自动关层", not st["acctLayerOpen"])
        ck.check("切 tools tab 生效", st["activeSec"] == "tools")

        print("== 5. i18n 无裸键 ==")
        leaked = [k for k in ("cp.app.", "cp.convops.", "cp.kb.", "cp.collab.")
                  if k in st["bodyText"]]
        ck.check("可见文本无裸键", not leaked, f"leaked={leaked}")

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        print("SKIP: playwright 未安装")
        return 0
    token = read_token(args.data_root)
    if not token:
        print("SKIP: 读不到实例 auth_token")
        return 0
    try:
        import urllib.request
        urllib.request.urlopen(args.base + "/login", timeout=5)
    except Exception:
        print("SKIP: 实例不可达")
        return 0
    try:
        return run(args.base, token, headed=args.headed)
    except Exception as e:  # 门禁自身崩溃如实红（与 SKIP 语义区分）
        print(f"FAIL: 验证过程异常 {e!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
