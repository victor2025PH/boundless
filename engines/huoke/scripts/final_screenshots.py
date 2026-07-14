# -*- coding: utf-8 -*-
"""Phase 1 全部完成后的最终截图."""
from playwright.sync_api import sync_playwright
import time
from pathlib import Path

OUT = Path("logs/smoke_screenshots")


def shoot():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # 1) L3 看板新版 (含我接手按钮)
        ctx1 = browser.new_context(viewport={"width": 1440, "height": 1800})
        page = ctx1.new_page()
        page.goto("http://192.168.0.118:8000/static/l2-dashboard.html?v=" + str(int(time.time())),
                  wait_until="networkidle")
        time.sleep(4)
        page.screenshot(path=str(OUT / "40_l3_dashboard_new.png"), full_page=False)
        print("saved 40_l3_dashboard_new.png")

        # 2) 客户详情 modal (点第一个客户行)
        try:
            page.locator('#customers-tbody tr').first.click()
            time.sleep(2)
            page.screenshot(path=str(OUT / "41_customer_detail_modal.png"), full_page=False)
            print("saved 41_customer_detail_modal.png")
            # 关 modal
            page.keyboard.press("Escape")
            time.sleep(1)
        except Exception as e:
            print(f"customer modal err: {e}")
        ctx1.close()

        # 3) 主后台 (admin 登录, 显示紫色客服中心置顶)
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
                localStorage.setItem('oc_user', '{login.get('user', '')}');
                localStorage.setItem('oc_role', '{login.get('role', '')}');
            """)
        page2.goto("http://192.168.0.118:8000/dashboard?v=" + str(int(time.time())),
                   wait_until="networkidle")
        time.sleep(5)
        page2.screenshot(path=str(OUT / "42_main_dashboard_with_cs.png"), full_page=False)
        print("saved 42_main_dashboard_with_cs.png")

        # 4) 打开客服 inbox + 点开 lmCsAssign modal 看真 UI
        page2.evaluate("if(window.lmOpenHandoffInbox)lmOpenHandoffInbox('')")
        time.sleep(4)
        page2.screenshot(path=str(OUT / "43_handoff_inbox_pretty.png"), full_page=False)
        print("saved 43_handoff_inbox_pretty.png")

        # 5) 找一个 handoff card 点 "我接手" 按钮
        try:
            assign_btns = page2.locator('button:has-text("我接手")').all()
            if assign_btns:
                assign_btns[0].click()
                time.sleep(2)
                page2.screenshot(path=str(OUT / "44_assign_modal_real_ui.png"), full_page=False)
                print("saved 44_assign_modal_real_ui.png")
        except Exception as e:
            print(f"assign modal err: {e}")

        ctx2.close()
        browser.close()


if __name__ == "__main__":
    shoot()
