# -*- coding: utf-8 -*-
"""Phase 2 截图: L3 SLA 看板 + 客服工作台 modal."""
from playwright.sync_api import sync_playwright
import time
from pathlib import Path

OUT = Path("logs/smoke_screenshots")


def shoot():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # 1) L3 看板 (含 SLA panel)
        ctx = browser.new_context(viewport={"width": 1440, "height": 2200})
        page = ctx.new_page()
        page.goto("http://192.168.0.118:8000/static/l2-dashboard.html?v=" + str(int(time.time())),
                  wait_until="domcontentloaded")
        time.sleep(4)
        page.screenshot(path=str(OUT / "50_l3_with_sla.png"), full_page=True)
        print("saved 50_l3_with_sla.png")
        ctx.close()

        # 2) 主后台 admin 登录 → 我的工作台 modal
        ctx2 = browser.new_context(viewport={"width": 1440, "height": 1800})
        page2 = ctx2.new_page()
        page2.goto("http://192.168.0.118:8000/auth/me")
        time.sleep(1)
        login = page2.evaluate("""
            async () => (await fetch('/auth/login', {method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({username:'admin',password:'admin'})})).json()
        """)
        token = login.get('token', '')
        if token:
            page2.evaluate(f"""
                localStorage.setItem('oc_token', '{token}');
                localStorage.setItem('oc_user', 'agent_002');
                localStorage.setItem('oc_cs_id', 'agent_002');
                localStorage.setItem('oc_role', 'admin');
            """)
        page2.goto("http://192.168.0.118:8000/dashboard?v=" + str(int(time.time())),
                   wait_until="domcontentloaded")
        time.sleep(4)

        # 调出我的工作台
        page2.evaluate("if(window.lmOpenMyDesk)lmOpenMyDesk()")
        time.sleep(4)
        page2.screenshot(path=str(OUT / "51_mydesk_modal.png"), full_page=False)
        print("saved 51_mydesk_modal.png")

        ctx2.close()
        browser.close()


if __name__ == "__main__":
    shoot()
