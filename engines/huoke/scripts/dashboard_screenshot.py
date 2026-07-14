# -*- coding: utf-8 -*-
"""引流后台 SPA 截图 - 干净流程."""
from playwright.sync_api import sync_playwright
import time
from pathlib import Path

OUT_DIR = Path("logs/smoke_screenshots")


def shoot():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1800})
        page = ctx.new_page()

        # 1) 先访问任意 page 设 cookie domain
        page.goto("http://192.168.0.118:8000/auth/me", wait_until="domcontentloaded")
        time.sleep(1)

        # 2) API 登录拿 token + 设 cookie
        login_resp = page.evaluate("""
            async () => {
                const r = await fetch('/auth/login', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({username: 'admin', password: 'admin'})
                });
                return await r.json();
            }
        """)
        print(f"login: {login_resp.get('user')} / role={login_resp.get('role')}")

        token = login_resp.get('token', '')
        user = login_resp.get('user', '')
        role = login_resp.get('role', '')
        if token:
            # SPA 用 localStorage 而不是 cookie
            page.evaluate(f"""
                localStorage.setItem('oc_token', '{token}');
                localStorage.setItem('oc_user', '{user}');
                localStorage.setItem('oc_role', '{role}');
                document.cookie = 'oc_token={token}; path=/; max-age=28800';
            """)

        # 3) goto dashboard, 等所有脚本 load
        page.goto("http://192.168.0.118:8000/dashboard", wait_until="networkidle")
        time.sleep(5)  # 让 SPA fully boot

        diag = page.evaluate("""
            () => ({
                title: document.title,
                scripts_count: document.scripts.length,
                lm_funcs: Object.keys(window).filter(k => k.startsWith('lm') || k.startsWith('Plat')),
                has_lmOpen: typeof window.lmOpenHandoffInbox,
                has_lmCsAssign: typeof window.lmCsAssign,
                body_len: document.body.innerHTML.length,
            })
        """)
        print(f"diag: {diag}")

        page.screenshot(path=str(OUT_DIR / "20_dashboard_logged_in.png"), full_page=False)

        # 4) call lmOpenHandoffInbox
        if diag.get('has_lmOpen') == 'function':
            page.evaluate("lmOpenHandoffInbox('')")
            time.sleep(5)
            page.screenshot(path=str(OUT_DIR / "21_inbox_pr6_buttons.png"), full_page=True)
            card_html = page.evaluate("""
                () => {
                    const c = document.querySelector('[id^="lm-card-"]');
                    return c ? c.outerHTML.length : 'no card';
                }
            """)
            print(f"card html len: {card_html}")
            # 单 card zoom screenshot
            try:
                first = page.locator('[id^="lm-card-"]').first
                first.screenshot(path=str(OUT_DIR / "22_card_zoom.png"))
                print("saved: 22_card_zoom.png")
            except Exception as e:
                print(f"card zoom err: {e}")
            print("saved: 21_inbox_pr6_buttons.png")
        else:
            print("lmOpenHandoffInbox not on window — JS load issue")

        browser.close()


if __name__ == "__main__":
    shoot()
