# -*- coding: utf-8 -*-
"""看板 v3 视证：SSE 状态灯 + 角色下钻浮层（点击/深链）截图与断言。"""
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
        # 注意：SSE 长连接会让 networkidle 永不满足（这正是推流在工作的证据）
        pg.goto(HUB + "/dashboard", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(5000)          # 等 SSE 建连 + 首帧
        live = pg.text_content("#liveTxt") or ""
        print("liveTxt:", live)
        # 点角色行开下钻
        row = pg.locator("[data-prof]").first
        prof = row.get_attribute("data-prof")
        row.click()
        pg.wait_for_timeout(1800)
        shown = pg.evaluate("document.getElementById('drillMask').style.display !== 'none'")
        print("drill opened for %r: %s, hash=%s" % (prof, shown, pg.evaluate("location.hash")))
        pg.screenshot(path=str(OUT / "dash_v3_drill.png"))
        # Esc 关闭
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)
        closed = pg.evaluate("document.getElementById('drillMask').style.display === 'none'")
        print("drill esc-closed:", closed, "hash=", pg.evaluate("location.hash"))
        # 深链直开
        pg2 = b.new_page(viewport={"width": 900, "height": 1000})
        pg2.on("pageerror", lambda e: errors.append(str(e)))
        pg2.goto(HUB + "/dashboard#p=%E9%98%BF%E4%B9%A0%E8%AE%B2%E8%AF%9D",
                 wait_until="domcontentloaded", timeout=30000)
        pg2.wait_for_timeout(3500)
        deep = pg2.evaluate("document.getElementById('drillMask').style.display !== 'none'")
        print("deeplink drill opened:", deep)
        pg2.screenshot(path=str(OUT / "dash_v3_deeplink.png"))
        b.close()
    print("pageerrors:", errors or "none")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
