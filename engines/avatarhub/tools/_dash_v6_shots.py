# -*- coding: utf-8 -*-
"""看板 v6 视证：菜单新项(推送日报/外链地址) + 外链设置弹窗 + 推送回执 toast。"""
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\logs")


def main():
    errors = []
    checks = {}
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1440, "height": 1050})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(HUB + "/dashboard", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(4500)

        pg.click("#menuBtn")
        pg.wait_for_timeout(300)
        checks["menu_push_btn"] = pg.is_visible("text=推送日报到群")
        checks["menu_base_btn"] = pg.is_visible("text=外链地址…")
        pg.screenshot(path=str(OUT / "dash_v6_menu.png"))

        # 外链地址弹窗：出现输入框 + 显示当前生效地址
        pg.click("text=外链地址…")
        pg.wait_for_timeout(800)
        checks["base_modal_input"] = pg.is_visible("#pbInput")
        checks["base_modal_effective"] = pg.evaluate(
            "document.querySelector('.modal') && document.querySelector('.modal').innerText.includes('当前生效')")
        pg.screenshot(path=str(OUT / "dash_v6_basemodal.png"))
        pg.click("#mCancel")
        pg.wait_for_timeout(300)

        # 推送日报：无 webhook 环境应 toast「未配 webhook」
        pg.click("#menuBtn")
        pg.wait_for_timeout(300)
        pg.click("text=推送日报到群")
        pg.wait_for_timeout(2500)
        checks["push_toast"] = pg.evaluate(
            "document.getElementById('toastWrap').innerText.includes('webhook')")
        pg.screenshot(path=str(OUT / "dash_v6_push.png"))
        br.close()
    print(json.dumps(checks, ensure_ascii=False, indent=1))
    print("pageerrors:", errors or "none")
    return 1 if (errors or [k for k, v in checks.items() if not v]) else 0


if __name__ == "__main__":
    sys.exit(main())
