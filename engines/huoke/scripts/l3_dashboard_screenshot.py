# -*- coding: utf-8 -*-
"""L3 dashboard headless 截图 — 给 victor 看部署后真实数据."""
from playwright.sync_api import sync_playwright
import time
from pathlib import Path

OUT_DIR = Path("logs/smoke_screenshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def shoot(url: str, filename: str, viewport=(1280, 1600), wait_sec: float = 3.0):
    out = OUT_DIR / filename
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        time.sleep(wait_sec)  # 让 JS fetch 完
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    print(f"saved: {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    # 桌面视图
    shoot("http://192.168.0.118:8000/static/l2-dashboard.html",
          "l3_desktop.png", viewport=(1280, 1600))
    # 移动端视图 (iPhone-like)
    shoot("http://192.168.0.118:8000/static/l2-dashboard.html",
          "l3_mobile.png", viewport=(390, 1900))
    print("done")
