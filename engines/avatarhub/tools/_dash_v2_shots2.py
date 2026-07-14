# -*- coding: utf-8 -*-
"""看板 v2 视证 2：嵌入模式(embed) + 演示模式(hub_demo) 两张。"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\logs")


def main():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 760})
        pg.goto(HUB + "/dashboard?embed=1", wait_until="networkidle", timeout=30000)
        pg.wait_for_timeout(2000)
        pg.screenshot(path=str(OUT / "dash_v2_embed.png"))

        pg2 = b.new_page(viewport={"width": 1280, "height": 900})
        pg2.goto(HUB + "/dashboard", wait_until="domcontentloaded", timeout=30000)
        pg2.evaluate("localStorage.setItem('hub_demo','1')")
        pg2.reload(wait_until="networkidle")
        pg2.wait_for_timeout(2000)
        pg2.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        pg2.wait_for_timeout(500)
        shot = OUT / "dash_v2_demo_bottom.png"
        pg2.screenshot(path=str(shot))
        sysHidden = pg2.evaluate(
            "getComputedStyle(document.getElementById('z-system')).display")
        navHidden = pg2.evaluate(
            "getComputedStyle(document.getElementById('navSystem')).display")
        dangerHidden = pg2.evaluate(
            "[...document.querySelectorAll('[data-danger]')].every(e=>getComputedStyle(e).display==='none')")
        pg2.evaluate("localStorage.removeItem('hub_demo')")
        print("demo: z-system display=%s navSystem=%s dangerAllHidden=%s"
              % (sysHidden, navHidden, dangerHidden))
        embedHidden = pg.evaluate(
            "getComputedStyle(document.querySelector('.brandline')).display")
        print("embed: brandline display=%s" % embedHidden)
        b.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
