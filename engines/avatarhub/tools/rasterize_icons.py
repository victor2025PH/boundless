# -*- coding: utf-8 -*-
"""PWA 图标重栅格化：icon.svg → icon-192/512.png（2026-07-30 品牌回填收尾）。

强调色回填（tools/backfill_brand_accent.py）更新了 icon.svg 的渐变
（#1e6bf0→#7a3bf5），但 manifest 引用的 PNG 是历史栅格（像素仍是旧
#4f7aff/#a855f7）——PWA 安装图标与品牌脱节。本脚本用 playwright 的
chromium 当栅格化器（本机无 inkscape/rsvg；chromium 渲染 SVG 保真且
`omit_background` 保留圆角外透明区）。

用法：python tools/rasterize_icons.py   （幂等；改 icon.svg 后重跑）
"""
from __future__ import annotations

import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "static"
SVG = STATIC / "icon.svg"
SIZES = (192, 512)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[SKIP] 未安装 playwright，无法栅格化")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for size in SIZES:
            page = browser.new_page(
                viewport={"width": size, "height": size},
                device_scale_factor=1,
            )
            page.goto(SVG.as_uri())
            out = STATIC / f"icon-{size}.png"
            page.screenshot(path=str(out), omit_background=True)
            page.close()
            print(f"[ok] {out.name} ({size}x{size})")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
