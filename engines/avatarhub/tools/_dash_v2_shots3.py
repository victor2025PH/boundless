# -*- coding: utf-8 -*-
"""看板 v2 视证 3：/ui 内嵌 iframe 真实渲染（嵌入自动识别）+ 修饰后首屏复检。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\logs")


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(HUB + "/ui#dashboard", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(6000)
        pg.screenshot(path=str(OUT / "dash_v2_in_ui.png"))
        frames = [f.url for f in pg.frames]
        print("frames:", frames)
        fr = next((f for f in pg.frames if f.url.endswith("/dashboard")), None)
        if fr:
            hidden = fr.evaluate(
                "getComputedStyle(document.querySelector('.brandline')).display")
            print("iframe embed brandline display=%s (期望 none)" % hidden)
        pg2 = b.new_page(viewport={"width": 1440, "height": 1050})
        pg2.goto(HUB + "/dashboard", wait_until="networkidle", timeout=30000)
        pg2.wait_for_timeout(2200)
        pg2.screenshot(path=str(OUT / "dash_v2_top2.png"))
        b.close()
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
