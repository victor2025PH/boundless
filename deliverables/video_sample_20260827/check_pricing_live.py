#!/usr/bin/env python3
"""真浏览器验证线上 /pricing：客户端水合后有没有错误边界卡 + console 错误。"""
import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "https://bd2026.cc/pricing"
errors: list[str] = []
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(URL, wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(3000)
    for _ in range(5):
        pg.evaluate("window.scrollBy({top: 1000})")
        pg.wait_for_timeout(800)
    body = pg.inner_text("body")
    has_err_card = ("页面加载遇到点小问题" in body) or ("Something went wrong" in body)
    has_faq = ("常见问题" in body) or ("Pricing FAQ" in body) or ("关于新价格" in body)
    pg.screenshot(path="pricing_live_check.png", full_page=False)
    b.close()

print(f"error_card={has_err_card} visible_faq={has_faq} console_errors={len(errors)}")
for e in errors[:8]:
    print("  CONSOLE:", e[:200])
sys.exit(1 if has_err_card else 0)
