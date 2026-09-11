# -*- coding: utf-8 -*-
"""用户管理页 P0（2026-09-11）：「子帐号登不上 / admin 退出无效」两起实录的服务端契约。

根因：/logout → 303 /login → 桌面壳（renderer.js 凭据链 / main.js 弹窗代登）拿
backend.token 秒级重登为 master → 退出形同虚设，子帐号永远见不到登录表单。
修复三端契约（壳侧静态契约见 desktop/test/backend-popup-login.test.js）：
  · /logout 落地 /login?manual=1（「人主动退出」信号）；
  · 登录页在 manual 态渲染提示 + 表单透传 manual=1（密码错重渲染不丢态）；
  · 重置密码入口（/users/update password）与创建同一 6 位下限；
  · 删除失败文案走 tr()（此前硬编码中文）。
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_logout_lands_on_manual_login(auth_client):
    r = auth_client.get("/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?manual=1"


def test_login_page_manual_mode_renders_note_and_passthrough(client, config_dir):
    from src.utils.web_user_store import ROLE_MASTER, WebUserStore
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("admin", "test-token-123", ROLE_MASTER)
    r = client.get("/login?manual=1", headers={"user-agent": "Mozilla/5.0 Electron/30 ChatX"})
    assert r.status_code == 200
    assert 'id="manual-note"' in r.text
    assert 'name="manual" value="1"' in r.text
    # 普通 /login 不出该提示（会话过期回跳不该被当成主动退出）
    r2 = client.get("/login")
    assert 'id="manual-note"' not in r2.text
    # 密码错 → 重渲染仍保持 manual 态（壳侧靠此 + 粘性标记不重登）
    r3 = client.post("/login", data={"username": "admin", "password": "wrong-pw", "manual": "1"})
    assert r3.status_code == 200
    assert 'id="manual-note"' in r3.text
    assert 'name="manual" value="1"' in r3.text


def test_reset_password_enforces_min_length(auth_client, config_dir):
    from src.utils.web_user_store import WebUserStore
    store = WebUserStore(config_dir / "web_users.db")
    u = store.create_user("agent_p0", "pass123456", "agent", "")
    r = auth_client.post(f"/users/update/{u['id']}", data={"password": "123"},
                         headers={"Accept": "application/json"})
    assert r.status_code == 400
    r = auth_client.post(f"/users/update/{u['id']}", data={"password": "newpass789"},
                         headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["ok"]
    assert store.verify("agent_p0", "newpass789")
    assert not store.verify("agent_p0", "pass123456")


def test_users_page_surfaces_backend_host_and_reset_entry(auth_client, config_dir):
    from src.utils.web_user_store import WebUserStore
    store = WebUserStore(config_dir / "web_users.db")
    store.create_user("agent_ui", "pass123456", "agent", "")
    r = auth_client.get("/users")
    assert r.status_code == 200
    # 重置密码入口：必须带 reset- 前缀——base.html 顶栏「修改密码」已占用 openPwdModal()/#pwd-new，
    # 同名会互相覆盖（本页点「修改密码」会打开 @undefined 的重置弹窗；生成密码填进错的输入框）
    assert "openResetPwdModal(" in r.text
    assert 'id="reset-pwd-new"' in r.text
    assert r.text.count("function openPwdModal(") == 1, "openPwdModal 只能由 base.html 定义一次"
    assert r.text.count('id="pwd-new"') == 1, "#pwd-new 只能是 base.html 修改密码框"
    assert "copyLoginInfo(" in r.text           # 复制登录信息
    assert 'class="u-chip"' in r.text           # 帐号所在后端 chip
    assert "window.uiPrompt(" in r.text         # 删除逐字确认不再走原生 prompt()
    assert not re.search(r"(?<![\w.$])prompt\s*\(\s*[^)\s]", r.text)


def test_desktop_shell_honors_manual_and_auto_login_flag():
    renderer = (_ROOT / "desktop" / "renderer" / "renderer.js").read_text(encoding="utf-8")
    main = (_ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    assert "_manualLogin" in renderer and 'searchParams.get("manual") === "1"' in renderer
    assert "backend.auto_login !== false" in renderer
    assert 'searchParams.get("manual") === "1"' in main
    assert "auto_login === false" in main
