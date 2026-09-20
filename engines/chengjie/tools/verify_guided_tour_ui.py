# -*- coding: utf-8 -*-
"""新手交互引导（_guided_tour.html blTour 引擎）真浏览器验证（P1 2026-08-07）。

**为什么需要它**：引导引擎是纯前端行为（聚光灯定位/自动跳步/键盘出口/点击穿透），
模板热更新直上生产；静态门禁只能证「模板能编译、i18n 键存在」，证不了
「目标丢失时真的跳步而不是卡住」「遮罩真的不拦截点击」——而『绝不卡住用户』
正是这个组件的核心承诺（同日实锤：一个 CSS `{#` 就让全部工作台页 500，
静态扫描与真渲染各守一半）。

fixture 模式（照 verify_goal_form_ui.py 先例）：零实例依赖——用真 Jinja +
真 web_i18n 词条把 `_guided_tour.html` 渲成 HTML，拼进带锚点的夹具页，
Playwright 驱动断言。不打生产、零遥测污染。

用法::

    python tools/verify_guided_tour_ui.py            # 无头跑全部场景
    python tools/verify_guided_tour_ui.py --headed   # 肉眼看一遍

覆盖的不变量：
  S1  引擎挂载：window.blTour = {start, stop, seen}。
  S2  start 后聚光灯罩住目标元素、气泡可见、计数 1/N。
  S3  **遮罩不拦截点击**：高亮中的按钮本人可直接点（pointer-events:none）。
  S4  下一步/上一步换目标；**目标缺失自动跳步**（不是卡住）。
  S5  末步「完成」→ 全部隐藏 + localStorage bl_tour_done + seen()==true。
  S6  Esc 随时退出。
  S7  sel:null 的步居中显示（无聚光灯）。
  S8  **全部目标缺失 → 整体优雅退出**（气泡绝不悬空卡住）。
  S9  EN 渲染零 CJK 泄漏（与 sealed-pages 门禁同口径的局部回归）。

缺 playwright → SKIP exit 0（不污染回归信号）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.stdout.reconfigure(encoding="utf-8")


def render_partial(lang: str) -> str:
    """真 Jinja + 真词条渲染引导 partial（与生产同源，词条缺键会当场暴露）。"""
    from jinja2 import Environment, FileSystemLoader

    from src.web.web_i18n import get_translations

    env = Environment(loader=FileSystemLoader(str(ENGINE / "src/web/templates")))
    return env.get_template("_guided_tour.html").render(i18n=get_translations(lang))


_FIXTURE = """<!doctype html><html><head><meta charset="utf-8"><title>gtour probe</title>
<style>
  body{margin:0;font-family:system-ui;background:#f4f6fb;min-height:2000px}
  .anchor{width:240px;padding:14px;margin:24px;background:#fff;border:1px solid #cbd5e1;border-radius:10px}
  #spacer{height:600px}
</style></head><body>
<div id="ob-card" class="anchor">onboard card</div>
<div class="anchor"><button id="btn-a" type="button" onclick="window.__clicked=(window.__clicked||0)+1">configure AI</button></div>
<div id="spacer"></div>
<div id="row-b" class="anchor" data-ob-id="channel">channel row</div>
@@PARTIAL@@
</body></html>
"""


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
        print(f"\n== 新手引导引擎验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def cjk_leaks(text: str) -> list:
    """用户可见面的 CJK 泄漏（对齐 sealed-pages 门禁口径：JS 注释不算——
    随脚本下发但永不显示；词条串走 |tojson 属字符串字面量，照常受查）。"""
    import re
    text = re.sub(r"(?m)^\s*//.*$", "", text)          # JS 行注释
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # JS 块注释
    return re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright 不可用（pip install playwright && playwright install chromium）")
        return 0

    ck = Checker()

    # S9 先做纯文本断言（不需要浏览器）：EN 渲染零 CJK。
    en_html = render_partial("en")
    # <script> 里的 |tojson 词条已是英文；正则直接扫全文即可（Jinja 注释不进产物）。
    ck.check("S9 EN 渲染零 CJK 泄漏", not cjk_leaks(en_html), str(cjk_leaks(en_html)[:3]))

    zh_html = render_partial("zh")
    fixture = _FIXTURE.replace("@@PARTIAL@@", zh_html)
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "gtour_probe.html"
        fp.write_text(fixture, encoding="utf-8")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(fp.as_uri())
            page.wait_for_timeout(120)

            api = page.evaluate(
                "() => window.blTour && ['start','stop','seen'].every(k => typeof window.blTour[k] === 'function')")
            ck.check("S1 引擎挂载 blTour{start,stop,seen}", api)

            steps = [
                {"sel": "#ob-card", "title": "t1", "body": "b1"},
                {"sel": "#btn-a", "title": "t2", "body": "b2"},
                {"sel": "#nope-missing", "title": "t3", "body": "b3"},   # 缺失 → 应自动跳过
                {"sel": "#row-b", "title": "t4", "body": "b4"},
            ]
            page.evaluate("(s) => { localStorage.removeItem('bl_tour_done'); window.blTour.start(s); }",
                          steps)
            page.wait_for_timeout(400)

            tip_vis = page.is_visible("#blTourTip")
            n_txt = page.text_content("#blTourN") or ""
            ck.check("S2 气泡可见 + 计数 1/4", tip_vis and n_txt.strip() == "1 / 4", n_txt)

            overlap = page.evaluate("""() => {
                const a = document.getElementById('ob-card').getBoundingClientRect();
                const h = document.getElementById('blTourHole').getBoundingClientRect();
                return h.width > 0 && h.left <= a.left && h.right >= a.right
                       && h.top <= a.top && h.bottom >= a.bottom;
            }""")
            ck.check("S2 聚光灯罩住目标", overlap)

            # S4 前半：下一步 → 高亮 #btn-a
            page.click("#blTourNext")
            page.wait_for_timeout(400)
            on_btn = page.evaluate("""() => {
                const a = document.getElementById('btn-a').getBoundingClientRect();
                const h = document.getElementById('blTourHole').getBoundingClientRect();
                return Math.abs(h.left + 6 - a.left) < 2 && Math.abs(h.top + 6 - a.top) < 2;
            }""")
            ck.check("S4 下一步换目标", on_btn, page.text_content("#blTourN") or "")

            # S3 点击穿透：直接点高亮中的按钮本体
            before = page.evaluate("() => window.__clicked || 0")
            page.click("#btn-a")
            after = page.evaluate("() => window.__clicked || 0")
            ck.check("S3 遮罩不拦截点击（高亮按钮可直接点）", after == before + 1, f"{before}->{after}")

            # S4 后半：下一步跨过缺失的 #nope-missing 直达第 4 步
            page.click("#blTourNext")
            page.wait_for_timeout(400)
            n_txt = (page.text_content("#blTourN") or "").strip()
            ck.check("S4 缺失目标自动跳步（2→4）", n_txt == "4 / 4", n_txt)

            # 末步按钮应显示「完成」词条（zh 词条同源）
            done_label = (page.text_content("#blTourNext") or "").strip()
            ck.check("S5 末步按钮=完成词条", done_label == "完成", done_label)
            page.click("#blTourNext")
            page.wait_for_timeout(150)
            closed = page.evaluate(
                "() => !document.getElementById('blTourTip').style.display.includes('block')"
                " && localStorage.getItem('bl_tour_done') === '1' && window.blTour.seen()")
            ck.check("S5 完成后关闭 + 落 bl_tour_done + seen()", closed)

            # S6 Esc 出口
            page.evaluate("(s) => window.blTour.start(s)", steps[:2])
            page.wait_for_timeout(200)
            page.keyboard.press("Escape")
            page.wait_for_timeout(150)
            ck.check("S6 Esc 随时退出", not page.is_visible("#blTourTip"))

            # S7 sel:null 居中步
            page.evaluate(
                "() => window.blTour.start([{sel: null, title: 'center', body: 'c'}])")
            page.wait_for_timeout(200)
            centered = page.evaluate("""() => {
                const tip = document.getElementById('blTourTip');
                const hole = document.getElementById('blTourHole');
                if (tip.style.display !== 'block' || hole.style.display === 'block') return false;
                const r = tip.getBoundingClientRect();
                return Math.abs((r.left + r.width / 2) - window.innerWidth / 2) < 40;
            }""")
            ck.check("S7 无目标步居中显示（无聚光灯）", centered)
            page.keyboard.press("Escape")

            # S8 全部目标缺失 → 优雅退出，绝不悬空
            page.evaluate("""() => window.blTour.start([
                {sel: '#gone1', title: 'x', body: 'x'},
                {sel: '#gone2', title: 'y', body: 'y'}
            ])""")
            page.wait_for_timeout(300)
            ck.check("S8 全部目标缺失 → 整体退出不卡住", not page.is_visible("#blTourTip"))

            browser.close()

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
