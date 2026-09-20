# -*- coding: utf-8 -*-
"""渠道中心四页对齐不变量——真浏览器只读巡检（P1-P3 对齐工程的运行时门禁）。

静态门禁（tests/test_channel_alignment_vocab.py 等）只能证「模板源码里键/顺序
对」，证不了「渲染出来的页面真的长这样、外迁 JS 真的加载执行了」；而模板热
更新直上生产。本工具把对齐工程的用户级不变量在真浏览器里钉住：

  1. 四页 hero 同用共享 .rpa-hero + theme-<plat> 头像；
  2. 主 Tab 集合与顺序（按 id/data-pane/data-tab 判定，语言无关）；
  3. 共享 KPI 元素在位（AI 已发 / 待处理 的四页填充位）；
  4. 控制条暂停时长三页同档（300/900/1800/3600/10800）；
  5. 渲染文本零 mustache 泄漏（{{runs_count}} 类事故回归网）；
  6. 零 pageerror（外迁 JS 加载失败/语法错在这里现形）；
  7. Messenger：外迁主脚本真的执行（window.saveConfig 可达）、共享漏斗
     pane 在位、rpa-card 家族保有量、mr-panel 零残留；
  8. 平台能力速览条（软检查：ctx 未注入时仅提示不判红——重启前属正常态）。

与 ``tools/verify_account_rail_ui.py`` 同族（同实例 + token 登录 + Playwright，
token 绝不打印）。只读：不点写操作、不发消息。缺 playwright / 实例不可达
=> SKIP exit 0（不污染回归信号）。

用法：
    python tools/verify_channel_alignment.py [--base URL] [--data-root DIR]
                                             [--token TOK] [--shots DIR] [--headed]
"""
from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path
from typing import Any, List, Optional, Tuple

DEFAULT_BASE = "http://127.0.0.1:18799"  # 勿改 localhost：::1 回退每连接 ~2s
DEFAULT_DATA_ROOT = "D:/chengjie-instances/zhiliao/data"

CHANNELS = ("telegram", "line", "messenger", "whatsapp")

# 主 Tab 顺序（语言无关判据）：五段式定稿 + 平台特有缀尾
_TAB_EXPECT = {
    "telegram": ("id", ["tab-reply", "tab-voice", "tab-account", "tab-funnel"]),
    "line": ("data-pane", ["pane-monitor", "pane-pending", "pane-settings",
                           "pane-ops", "pane-funnel"]),
    "whatsapp": ("data-pane", ["pane-wa-monitor", "pane-wa-pending",
                               "pane-wa-settings", "pane-wa-ops",
                               "pane-wa-funnel", "pane-wa-templates"]),
    "messenger": ("data-tab", ["overview", "approvals", "settings", "runs",
                               "funnel", "leads", "personas", "accounts"]),
}
_KPI_EXPECT = {
    "telegram": ["st-replies"],
    "line": ["lr-kpi-sent", "lr-kpi-pending"],
    "whatsapp": ["wa-kpi-sent", "wa-kpi-pending"],
    "messenger": ["kpi-sent", "kpi-pending"],
}
_PAUSE_SELECT = {"line": "lr-pause-select", "whatsapp": "wa-pause-select",
                 "messenger": "mr-pause-select"}
_PAUSE_TIERS = ["300", "900", "1800", "3600", "10800"]


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

    def info(self, name: str, detail: str = "") -> None:
        print(f"  [info] {name}" + (f"  {detail}" if detail else ""))

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== 渠道对齐巡检: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def run(base: str, token: str, *, shots: Optional[Path] = None,
        headed: bool = False) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        ctx.request.post(base + "/login", form={"auth_token": token})
        page = ctx.new_page()
        page_errors: List[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        for ch in CHANNELS:
            print(f"\n-- {ch}")
            page_errors.clear()
            page.goto(f"{base}/workspace/channels/{ch}",
                      wait_until="domcontentloaded")
            page.wait_for_timeout(4500)

            # 1) hero 骨架
            ck.check(f"{ch}: rpa-hero 在位",
                     page.locator(".rpa-hero").count() >= 1)
            ck.check(f"{ch}: theme-{ch} 头像",
                     page.locator(f".rpa-hero-avatar.theme-{ch}").count() == 1)

            # 2) 主 Tab 集合与顺序（语言无关）
            attr, expect = _TAB_EXPECT[ch]
            sel = ".mr-tabs .mr-tab" if ch == "messenger" else ".st-bar .st-tab"
            got = page.eval_on_selector_all(
                sel, f"els => els.map(e => e.getAttribute('{attr}'))")
            got = [g for g in (got or []) if g]
            ck.check(f"{ch}: 主 Tab 顺序符合五段式定稿", got == expect,
                     f"got={got}")

            # 3) 共享 KPI 元素在位
            for el_id in _KPI_EXPECT[ch]:
                ck.check(f"{ch}: KPI #{el_id} 在位",
                         page.locator(f"#{el_id}").count() == 1)

            # 4) 暂停时长档位（LINE/WA/MSG）
            if ch in _PAUSE_SELECT:
                tiers = page.eval_on_selector_all(
                    f"#{_PAUSE_SELECT[ch]} option", "os => os.map(o => o.value)")
                ck.check(f"{ch}: 暂停时长五档一致", tiers == _PAUSE_TIERS,
                         f"got={tiers}")

            # 5) mustache 泄漏（渲染文本里不得出现字面 {{ ）
            body_text = page.evaluate("document.body.innerText") or ""
            ck.check(f"{ch}: 无 mustache 字面泄漏", "{{" not in body_text)

            # 8) 能力速览条（软检查：ctx 缺席=重启前正常态，不判红）
            strip = page.locator(".chc-cap-strip .chc-cap-chip").count()
            if strip:
                ck.check(f"{ch}: 能力速览条 4 chips", strip == 4, f"got={strip}")
            else:
                ck.info(f"{ch}: 能力速览条未渲染（ctx 未注入：新后端未装载前属正常）")

            # 7) Messenger 专项
            if ch == "messenger":
                ck.check("messenger: 外迁主脚本已执行（window.saveConfig 可达）",
                         page.evaluate("typeof window.saveConfig === 'function'"))
                ck.check("messenger: 共享漏斗 pane 在位",
                         page.locator("#view-funnel").count() == 1)
                ck.check("messenger: rpa-card 家族保有量 ≥ 20",
                         page.locator(".rpa-card").count() >= 20)
                ck.check("messenger: mr-panel 零残留",
                         page.locator(".mr-panel").count() == 0)

            # 6) 零 pageerror（外迁 JS 加载失败/语法错在此现形）
            ck.check(f"{ch}: 零 pageerror", not page_errors,
                     f"errors={page_errors[:3]}")

            if shots is not None:
                shots.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shots / f"align_{ch}.png"))

        browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--data-root", default=DEFAULT_DATA_ROOT,
                    help="实例数据根（读 web_admin.auth_token）")
    ap.add_argument("--token", default="", help="直接给 token（优先于 --data-root）")
    ap.add_argument("--shots", default="", help="截图输出目录（可选）")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 不可用（pip install playwright && playwright install chromium）")
        return 0
    try:
        urllib.request.urlopen(args.base + "/login", timeout=4)
    except Exception:
        print(f"[SKIP] 实例不可达: {args.base}")
        return 0
    token = args.token or read_token(args.data_root)
    if not token:
        print(f"[SKIP] 未能从 {args.data_root} 读到 web_admin.auth_token")
        return 0

    shots = Path(args.shots) if args.shots else None
    return run(args.base, token, shots=shots, headed=args.headed)


if __name__ == "__main__":
    sys.exit(main())
