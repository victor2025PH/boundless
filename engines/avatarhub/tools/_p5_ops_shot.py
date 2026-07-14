# -*- coding: utf-8 -*-
"""P5-1 视证：临时播种漏斗样例数据 → 无头截图 ops「设备自愈漏斗」卡 → 还原 devflow 文件。"""
import sys
import time
from pathlib import Path

import requests

HUB = "http://127.0.0.1:9000"
FLOW = Path(r"C:\模仿音色\data\devflow_stats.json")
OUT = Path(r"C:\模仿音色\logs\p5_ops_devflow_card.png")


def main():
    flow_orig = FLOW.read_bytes() if FLOW.exists() else None
    try:
        seed = [("expose", "strip")] * 5 + [("click", "strip")] * 3 + [("ok", "strip")] * 2 \
             + [("expose", "diag")] * 2 + [("click", "diag"), ("fail", "diag")]
        for ev, src in seed:
            requests.post(HUB + "/api/metrics/devflow",
                          json={"ev": ev, "kind": "in", "src": src}, timeout=5)
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            pg.goto(HUB + "/ops", wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(1500)
            card = pg.locator("#devflowCard")
            card.scroll_into_view_if_needed()
            card.screenshot(path=str(OUT))
            b.close()
        print("shot ->", OUT)
        return 0
    finally:
        if flow_orig is None:
            FLOW.unlink(missing_ok=True)
        else:
            FLOW.write_bytes(flow_orig)
        print("devflow restored:",
              (FLOW.read_bytes() if FLOW.exists() else None) == flow_orig)


if __name__ == "__main__":
    sys.exit(main())
