# -*- coding: utf-8 -*-
"""告警渠道配置授权门禁（2026-07-31，产品化收尾：权限收紧）。

背景：告警渠道是**整个实例的运营配置**——配错/删渠道 → 全实例告警瞎，渠道明细还含
明文群 webhook 地址。此前读明细/写/测试全是 `api_auth`（任意登录），普通坐席能改甚至
删掉主管配的渠道。收紧到运营角色（拒 agent/viewer，放行 master/admin 与 Bearer 桌面壳
＝装机主人），而**纯告警类型目录 alert-catalog 放开**（坐席能看有哪些告警类型无妨）。

关键设计——**排除法而非严格白名单**：绝不误伤「主人」。桌面壳走纯 Bearer、session 无
role，若用 `_require_supervisor`（白名单 master/admin）会把桌面壳一并拦掉，违背「每个
安装的都能各绑各的」诉求。本门禁同时钉住「viewer 被拒」与「Bearer 放行」两侧。

**授权模型认知（本轮实测厘清）**：admin.py 的 `_api_auth._agent_guard` **只特判 agent**
——白名单（unified-inbox/workspace/drafts/voice-tts）外的 `/api/accounts/*` 对 agent 一律
403。但它**漏了 viewer**：观察员能过 api_auth、进而能读/改/删告警渠道。真正的授权洞是
**viewer 能写运营配置**，正是本 `_require_alert_channel_admin` 补上的（拒 agent∪viewer，
其中 agent 是纵深冗余、viewer 是 api_auth 漏网）。故 catalog 对 agent 放不开（api_auth
先拦），但对 viewer 能放开（无 _require）。
"""

from starlette.testclient import TestClient

from src.utils.web_user_store import (
    ROLE_AGENT, ROLE_MASTER, ROLE_VIEWER, WebUserStore,
)

_WEBHOOKS = "/api/accounts/auto-reply/webhooks"
_CATALOG = "/api/accounts/auto-reply/alert-catalog"


def _login(app, config_dir, role):
    """建一个该角色的用户并用 username/password 登录（session 带 role）。返回其 client。

    首个用户必须是 master（首装约定）；非 master 角色额外建一个再登录。
    """
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("boss", "pw-master-123", ROLE_MASTER)  # 首装须先有 master
    uname = f"u_{role}"
    store.create_user(uname, "pw-123456", role)   # function-scope tmp 库，不重名
    c = TestClient(app)
    c.get("/login")
    c.post("/login", data={"username": uname, "password": "pw-123456"},
           follow_redirects=True)
    return c


# ── viewer（api_auth 漏网、本 _require 补拦的关键角色）：读明细/写/测试全拒 ──────

def test_viewer_cannot_read_channel_details(app, config_dir):
    """观察员本应只读，却能过 api_auth 读到含明文群地址的渠道明细 → 本 _require 拦。"""
    c = _login(app, config_dir, ROLE_VIEWER)
    r = c.get(_WEBHOOKS)
    assert r.status_code == 403, f"观察员读渠道明细应 403，得 {r.status_code}"


def test_viewer_cannot_save_channels(app, config_dir):
    c = _login(app, config_dir, ROLE_VIEWER)
    # 带同源 Referer 过 CSRF 闸 → 命中授权检查（否则先撞 CSRF 403，测不到授权层）
    r = c.post(_WEBHOOKS, json={"webhooks": []},
               headers={"Referer": "http://testserver/workspace"})
    assert r.status_code == 403, f"观察员保存渠道应 403，得 {r.status_code}"


def test_viewer_cannot_test_channel(app, config_dir):
    c = _login(app, config_dir, ROLE_VIEWER)
    r = c.post(_WEBHOOKS + "/test", json={"index": 0},
               headers={"Referer": "http://testserver/workspace"})
    assert r.status_code == 403, f"观察员测试发送应 403，得 {r.status_code}"


# ── agent：纵深冗余（api_auth._agent_guard 早已把 /api/accounts/* 全拦） ──────────

def test_agent_fully_blocked_by_api_auth(app, config_dir):
    """坐席访问 /api/accounts/* 一律 403（api_auth 层白名单）——本 _require 之前就拦了，
    纵深防御双保险。catalog 同前缀，agent 同样拦（放不开是既有行为，非本轮目标）。"""
    c = _login(app, config_dir, ROLE_AGENT)
    assert c.get(_WEBHOOKS).status_code == 403
    assert c.post(_WEBHOOKS, json={"webhooks": []},
                  headers={"Referer": "http://testserver/workspace"}).status_code == 403


# ── 运营角色 / 主人：放行 ────────────────────────────────────────────────────

def test_master_can_read_and_save(auth_client):
    """auth_client = master + Bearer（装机主人）：读明细 + 保存都放行（非 403）。"""
    r = auth_client.get(_WEBHOOKS)
    assert r.status_code == 200, f"主管读渠道应 200，得 {r.status_code}"
    r2 = auth_client.post(_WEBHOOKS, json={"webhooks": []})
    assert r2.status_code != 403, f"主管保存渠道不应 403，得 {r2.status_code}"


def test_bearer_desktop_shell_not_blocked(app, config_dir):
    """桌面壳＝纯 Bearer、session 无 role：绝不能被授权拦（核心不变量：不误伤主人）。"""
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("boss", "pw-master-123", ROLE_MASTER)
    c = TestClient(app)  # 不登录、不建 session，仅带 Bearer（与 auth_token 一致）
    c.headers.update({"Authorization": "Bearer test-token-123"})
    r = c.get(_WEBHOOKS)
    assert r.status_code != 403, f"Bearer 桌面壳读渠道不应 403，得 {r.status_code}"
    r2 = c.post(_WEBHOOKS, json={"webhooks": []})
    assert r2.status_code != 403, f"Bearer 桌面壳保存不应 403，得 {r2.status_code}"


# ── 纯类型目录：放开（无实例敏感信息）——对 viewer 放行（agent 被 api_auth 前置拦） ──

def test_catalog_open_to_viewer(app, config_dir):
    """告警类型目录（纯 alias+大白话，无群地址/token）对观察员放开：无 _require，
    viewer 过 api_auth → 200。这样 viewer 能看有哪些告警类型，但配不了渠道。"""
    c = _login(app, config_dir, ROLE_VIEWER)
    r = c.get(_CATALOG)
    assert r.status_code == 200, f"告警类型目录应对观察员放开，得 {r.status_code}"
    d = r.json()
    assert d.get("ok") and "business" in d and "technical" in d
