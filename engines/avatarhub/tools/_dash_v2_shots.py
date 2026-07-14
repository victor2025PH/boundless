# -*- coding: utf-8 -*-
"""看板 v2 视证：无头截图 桌面/系统区展开/手机/战报页 四张，供人工目检。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\logs")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1440, "height": 1050})
        pg.goto(HUB + "/dashboard", wait_until="networkidle", timeout=30000)
        pg.wait_for_timeout(2500)
        pg.screenshot(path=str(OUT / "dash_v2_top.png"))
        pg.evaluate("toggleSystem(true)")
        pg.wait_for_timeout(400)
        pg.evaluate("document.getElementById('z-system').scrollIntoView()")
        pg.wait_for_timeout(700)
        pg.screenshot(path=str(OUT / "dash_v2_system.png"))

        pg2 = b.new_page(viewport={"width": 390, "height": 844})
        pg2.goto(HUB + "/dashboard", wait_until="networkidle", timeout=30000)
        pg2.wait_for_timeout(2000)
        pg2.screenshot(path=str(OUT / "dash_v2_mobile.png"))

        pg3 = b.new_page(viewport={"width": 860, "height": 1100})
        pg3.goto(HUB + "/api/dashboard/report", wait_until="networkidle", timeout=30000)
        pg3.wait_for_timeout(1200)
        pg3.screenshot(path=str(OUT / "dash_v2_report.png"), full_page=True)
        b.close()
    print("shots done ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
