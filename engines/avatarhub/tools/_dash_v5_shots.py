# -*- coding: utf-8 -*-
"""看板 v5 视证：①hero 较昨日差分 ②A/B 对比赢家👑(含深链补验) ③菜单告警自检按钮 ④战报头条较昨日。"""
import json
import sys
import urllib.request
from urllib.parse import quote
from pathlib import Path

from playwright.sync_api import sync_playwright

HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\logs")


def main():
    snap = json.loads(urllib.request.urlopen(HUB + "/api/dashboard/snapshot", timeout=30).read())
    names = [p["name"] for p in (snap.get("profiles", {}) or {}).get("profiles", [])]
    if len(names) < 2:
        print("profiles <2, skip compare"); names += names
    a, b = names[0], names[1]
    errors = []
    checks = {}
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1440, "height": 1050})
        pg.on("pageerror", lambda e: errors.append(str(e)))

        # ① 看板 hero：较昨日
        pg.goto(HUB + "/dashboard", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(5000)
        checks["hero_daydiff"] = pg.evaluate(
            "document.getElementById('kpis').innerText.includes('较昨日')")
        pg.screenshot(path=str(OUT / "dash_v5_hero.png"))

        # ② A/B 对比深链 + 赢家高亮（补验上轮深链）
        pg.goto(HUB + f"/dashboard#compare={quote(a)},{quote(b)}",
                wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(6500)
        checks["compare_deeplink"] = pg.evaluate(
            "document.getElementById('compareMask').style.display !== 'none'")
        checks["compare_win_kpi"] = pg.evaluate(
            "document.querySelectorAll('.compare-col .kpi.win').length") 
        pg.screenshot(path=str(OUT / "dash_v5_compare.png"))
        pg.keyboard.press("Escape")
        pg.wait_for_timeout(400)

        # ③ 菜单里的告警自检按钮（只验证存在与点击回执，不重复点扰民）
        pg.click("#menuBtn")
        pg.wait_for_timeout(300)
        checks["alert_test_btn"] = pg.is_visible("text=测试告警通路")
        pg.screenshot(path=str(OUT / "dash_v5_menu.png"))
        pg.click("text=测试告警通路")
        pg.wait_for_timeout(2500)
        checks["alert_test_toast"] = pg.evaluate(
            "document.getElementById('toastWrap').innerText.length > 0")
        pg.screenshot(path=str(OUT / "dash_v5_alerttest.png"))

        # ④ 战报页头条较昨日
        pg.goto(HUB + "/api/dashboard/report", wait_until="domcontentloaded", timeout=30000)
        pg.wait_for_timeout(2500)
        checks["report_daydiff"] = pg.evaluate(
            "!!document.querySelector('.daydiff')")
        checks["report_daydiff_text"] = pg.evaluate(
            "(document.querySelector('.daydiff')||{}).innerText||''")
        pg.screenshot(path=str(OUT / "dash_v5_report.png"))
        br.close()
    print(json.dumps(checks, ensure_ascii=False, indent=1))
    print("pageerrors:", errors or "none")
    bad = [k for k, v in checks.items()
           if k != "report_daydiff_text" and not v]
    return 1 if (errors or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
