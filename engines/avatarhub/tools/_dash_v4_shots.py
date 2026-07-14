# -*- coding: utf-8 -*-
"""看板 v4 视证：A/B 对比浮层 + 历史战报菜单截图。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\logs")


def main():
    errors = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1050})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(HUB + "/dashboard", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(5000)
        # A/B compare via hash
        pg.goto(HUB + "/dashboard#compare=%E9%98%BF%E4%B9%A0%E8%AE%B2%E8%AF%9D,%E5%88%98%E4%BA%A6%E8%8F%B2",
                wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(6500)
        shown = pg.evaluate("document.getElementById('compareMask').style.display !== 'none'")
        print("compare deeplink:", shown)
        pg.screenshot(path=str(OUT / "dash_v4_compare.png"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)
        # history modal
        pg.click("#menuBtn")
        pg.wait_for_timeout(300)
        pg.click("text=历史战报")
        pg.wait_for_timeout(1200)
        pg.screenshot(path=str(OUT / "dash_v4_history.png"))
        b.close()
    print("pageerrors:", errors or "none")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
