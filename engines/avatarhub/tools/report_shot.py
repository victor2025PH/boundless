# -*- coding: utf-8 -*-
"""战报长图子进程：无头 Chromium 整页截图 /api/dashboard/report → PNG。
由 Hub 的 ?fmt=png 分支以子进程调用（浏览器不进 Hub 事件环，崩溃也不连坐）。"""
import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出 PNG 路径")
    ap.add_argument("--hub", default=os.environ.get("HUB_URL", "http://127.0.0.1:9000"))
    ap.add_argument("--width", type=int, default=820)
    args = ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"playwright 不可用: {e}")
        return 2
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": args.width, "height": 1000},
                        device_scale_factor=2)          # 2x：微信里放大看数字不糊
        pg.goto(args.hub.rstrip("/") + "/api/dashboard/report",
                wait_until="networkidle", timeout=30000)
        pg.wait_for_timeout(900)
        pg.evaluate("document.querySelectorAll('.no-print').forEach(e=>e.remove())")
        pg.screenshot(path=args.out, full_page=True)
        b.close()
    print("ok", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
