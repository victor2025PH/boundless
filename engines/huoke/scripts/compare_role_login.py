# -*- coding: utf-8 -*-
"""对比 admin vs agent_001 登录看到的 dashboard."""
from playwright.sync_api import sync_playwright
import time
from pathlib import Path

OUT = Path("logs/smoke_screenshots")


def login_and_shoot(username: str, password: str, label: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 1800})
        page = ctx.new_page()

        page.goto("http://192.168.0.118:8000/auth/me")
        time.sleep(1)
        login = page.evaluate(f"""
            async () => {{
                const r = await fetch('/auth/login', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{username: '{username}', password: '{password}'}})
                }});
                return await r.json();
            }}
        """)
        token = login.get('token', '')
        if not token:
            print(f"{label} login FAILED: {login}")
            browser.close()
            return
        page.evaluate(f"""
            localStorage.setItem('oc_token', '{token}');
            localStorage.setItem('oc_user', '{login.get('user', '')}');
            localStorage.setItem('oc_role', '{login.get('role', '')}');
        """)
        page.goto("http://192.168.0.118:8000/dashboard", wait_until="networkidle")
        time.sleep(4)
        page.screenshot(path=str(OUT / f"30_{label}_login.png"), full_page=False)
        print(f"saved 30_{label}_login.png  user={login.get('user')} role={login.get('role')}")
        browser.close()


if __name__ == "__main__":
    login_and_shoot("admin", "admin", "admin")
    login_and_shoot("agent_001", "agent001@openclaw", "customer_service")
