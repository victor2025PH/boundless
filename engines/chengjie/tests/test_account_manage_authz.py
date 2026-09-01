# -*- coding: utf-8 -*-
"""账号管理写口授权门禁（P1 2026-08-12，副驾账号面板治理第二批）。

背景：账号 启停/重启/登出/删除/改名/官方资料推送/批量对齐/自动回复开关与参数/
全局设置/编排器同步 全是「运营动作」——误触即「账号下线漏接客户 / 官方身份被
改写 / AI 开始代发」。此前这些 POST 只有 ``api_auth``（任意登录）；P0 只做了
**前端**收起按钮（cp-accounts 按 viewer_can_manage 隐藏），服务端边界缺失。

授权模型（沿用 test_alert_channel_authz 实测厘清的认知）：
- ``api_auth._agent_guard`` 只特判 **agent**——白名单外的 ``/api/accounts/*`` 对
  agent 一律 403（前置层）；
- **viewer 是漏网**：能过 api_auth 直呼写口——本批 ``_require_account_manager``
  补上（拒 agent∪viewer；agent 属纵深冗余）；
- **排除法而非白名单**：桌面壳纯 Bearer、session 无 role ＝装机主人，绝不误伤。

读口刻意不闸：viewer 保持「可观察不可操作」（清单/审计/体检/编排器状态照读），
且 ``GET /api/accounts`` 的 ``viewer_can_manage`` 字段必须如实反映本闸（前端预判
与服务端行为一致——预判另算一套比没预判更糟，工作台徽标同一哲学）。
"""

import pytest
from starlette.testclient import TestClient

from src.utils.web_user_store import (
    ROLE_AGENT, ROLE_MASTER, ROLE_VIEWER, WebUserStore,
)

_REFERER = {"Referer": "http://testserver/workspace"}

# 全部被闸写口（method, path, body）——新增账号管理写口必须同步登记，
# 否则「忘了闸」不会被任何测试点名。
_GATED_WRITES = [
    ("/api/accounts/orchestrator/sync", {}),
    ("/api/accounts/telegram/acc1/start", {}),
    ("/api/accounts/telegram/acc1/stop", {}),
    ("/api/accounts/telegram/acc1/restart", {}),
    ("/api/accounts/telegram/acc1/logout", {}),
    ("/api/accounts/telegram/acc1/remove", {}),
    ("/api/accounts/telegram/acc1/label", {"label": "x"}),
    ("/api/accounts/telegram/acc1/profile", {"name": "x"}),
    ("/api/accounts/persona-align", {"persona_id": "p1"}),
    ("/api/accounts/telegram/acc1/auto-reply", {"enabled": True}),
    ("/api/accounts/telegram/acc1/auto-reply/override", {"rate": {"hourly": 1}}),
    ("/api/accounts/auto-reply/config", {}),
]


def _login(app, config_dir, role):
    """建一个该角色的用户并登录（session 带 role）。首个用户必须是 master（首装约定）。"""
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("boss", "pw-master-123", ROLE_MASTER)
    uname = f"u_{role}"
    store.create_user(uname, "pw-123456", role)
    c = TestClient(app)
    c.get("/login")
    c.post("/login", data={"username": uname, "password": "pw-123456"},
           follow_redirects=True)
    return c


# ── viewer：api_auth 放行但写口必须 403（本闸存在的理由） ──────────────────────

@pytest.mark.parametrize("path,body", _GATED_WRITES, ids=lambda v: v if isinstance(v, str) else "")
def test_viewer_blocked_on_every_manage_write(app, config_dir, path, body):
    c = _login(app, config_dir, ROLE_VIEWER)
    r = c.post(path, json=body, headers=_REFERER)
    assert r.status_code == 403, f"观察员 POST {path} 应 403，得 {r.status_code}"


def test_viewer_can_still_read_list_and_flag_is_honest(app, config_dir):
    """viewer 读清单照常（可观察），且 viewer_can_manage 必须为 False——
    前端按它收按钮，预判必须与服务端 403 行为完全一致。"""
    c = _login(app, config_dir, ROLE_VIEWER)
    r = c.get("/api/accounts")
    assert r.status_code == 200, f"观察员读账号清单应 200，得 {r.status_code}"
    d = r.json()
    assert d.get("ok") is True
    assert d.get("viewer_can_manage") is False, \
        "viewer_can_manage 对观察员必须 False（前端预判=服务端边界）"


# ── agent：api_auth 前置层已全拦（纵深冗余，钉住既有行为不被放松） ────────────

def test_agent_blocked_upstream_by_api_auth(app, config_dir):
    c = _login(app, config_dir, ROLE_AGENT)
    assert c.get("/api/accounts").status_code == 403
    assert c.post("/api/accounts/telegram/acc1/stop", json={},
                  headers=_REFERER).status_code == 403


# ── 运营角色 / 主人：放行（绝不误伤） ─────────────────────────────────────────

def test_master_flag_true_and_writes_pass_authz(auth_client):
    """master：flag 如实 True；写口不被本闸拦（业务层可 404/400，但绝不 403）。"""
    r = auth_client.get("/api/accounts")
    assert r.status_code == 200
    assert r.json().get("viewer_can_manage") is True
    r2 = auth_client.post("/api/accounts/telegram/acc1/stop", json={})
    assert r2.status_code != 403, f"master 停止账号不应 403，得 {r2.status_code}"
    r3 = auth_client.post("/api/accounts/auto-reply/config", json={})
    assert r3.status_code != 403, f"master 写全局设置不应 403，得 {r3.status_code}"


def test_bearer_desktop_shell_not_blocked(app, config_dir):
    """桌面壳＝纯 Bearer、session 无 role：核心不变量——排除法绝不误伤主人。"""
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("boss", "pw-master-123", ROLE_MASTER)
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token-123"})
    r = c.get("/api/accounts")
    assert r.status_code == 200 and r.json().get("viewer_can_manage") is True
    r2 = c.post("/api/accounts/telegram/acc1/stop", json={})
    assert r2.status_code != 403, f"Bearer 桌面壳停止账号不应 403，得 {r2.status_code}"


# ── fields=basic 轻量档（2026-08-30）：首屏「先显示账号」骨架用，个位数 ms ──────────

def test_accounts_basic_fields_lightweight(auth_client):
    """fields=basic：200 + basic:True + 每行带 platform/account_id + running(bool)；
    只回 registry+config 基础态（跳过运行时/编排/健康/配额聚合），完整态由普通档补。"""
    r = auth_client.get("/api/accounts?fields=basic")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert d.get("basic") is True
    assert isinstance(d.get("accounts"), list)
    for a in d["accounts"]:
        assert a.get("platform") and "account_id" in a
        assert isinstance(a.get("running"), bool)


def test_accounts_basic_respects_viewer_flag(app, config_dir):
    """轻量档同样如实下发 viewer_can_manage（前端预判与服务端边界一致）。"""
    c = _login(app, config_dir, ROLE_VIEWER)
    r = c.get("/api/accounts?fields=basic")
    assert r.status_code == 200
    assert r.json().get("viewer_can_manage") is False


# ── 契约自守：写口清单不許悄悄缩水（防「删了闸没人知道」） ────────────────────

def test_gated_write_registry_matches_source():
    """路由源码里 _require_account_manager 的调用次数必须 ≥ 登记清单长度——
    有人删掉某个闸（或新增写口忘闸忘登记）时此处先红。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    calls = src.count("_require_account_manager(request)")
    assert calls >= len(_GATED_WRITES), (
        f"路由里只有 {calls} 处 _require_account_manager，登记清单有 "
        f"{len(_GATED_WRITES)} 条——删闸/忘闸了？")
