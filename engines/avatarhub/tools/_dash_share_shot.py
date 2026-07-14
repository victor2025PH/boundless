# -*- coding: utf-8 -*-
"""P11 传播看板实拍（人工 QA 辅助）：截「传播转化」卡验证扫码进站归因列。需 Hub 在线。"""
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:9000"
out = Path(tempfile.gettempdir()) / "stream_states" / "dash_share.png"
out.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 1600})
    pg.goto(BASE + "/dashboard", wait_until="domcontentloaded")
    pg.wait_for_timeout(3500)
    el = pg.locator("#shareStats")
    if not el.count():
        print("NG: #shareStats 不存在")
        sys.exit(1)
    el.scroll_into_view_if_needed()
    el.screenshot(path=str(out))
    txt = el.inner_text()
    print(("OK" if "扫码进站" in txt else "NG(未见扫码进站行)") + f" -> {out}")
    b.close()
