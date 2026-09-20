"""账号删除/登出护栏（2026-07-21 生产实测修复）。

背景：连接中心抽屉里 config/适配器来源的条目（如 A 线 telegram default）不在
账号注册表里，旧版 /remove /logout 对它们是**静默空操作**（仍回 ok:true）——
前端刷新后账号原样回来，表现为「点删除没反应」。现在：
- 注册表没有的账号 → 404 + 本地化 detail（前端 verbatim 直显）；
- /api/unified-inbox/chats 的 platform_status 条目带 removable 标记，
  前端据此隐藏不可删条目的删除/登出按钮。
"""

from __future__ import annotations


def test_remove_unknown_account_404(auth_client):
    """注册表没有的账号删除 → 明确 404，不再静默 ok。"""
    r = auth_client.post("/api/accounts/telegram/default/remove")
    assert r.status_code == 404
    assert r.json().get("detail")


def test_logout_unknown_account_404(auth_client):
    r = auth_client.post("/api/accounts/telegram/default/logout")
    assert r.status_code == 404
    assert r.json().get("detail")


def test_remove_registry_account_soft_deletes(auth_client):
    """注册表在册账号删除 → ok:true 且状态置 removed（旧行为不变）。"""
    from src.integrations.account_registry import get_account_registry

    reg = get_account_registry()
    reg.upsert("telegram", "acct_rm_guard", mode="protocol", status="offline")
    r = auth_client.post("/api/accounts/telegram/acct_rm_guard/remove")
    assert r.status_code == 200
    assert r.json().get("ok") is True
    row = reg.get("telegram", "acct_rm_guard")
    assert row is not None and row["status"] == "removed"


def test_remove_already_removed_account_idempotent(auth_client):
    """已软删的账号再删一次 → 幂等 ok（行还在注册表，不 404）。"""
    from src.integrations.account_registry import get_account_registry

    reg = get_account_registry()
    reg.upsert("telegram", "acct_rm_twice", mode="protocol", status="removed")
    r = auth_client.post("/api/accounts/telegram/acct_rm_twice/remove")
    assert r.status_code == 200 and r.json().get("ok") is True


def test_platform_status_marks_removable():
    """_merge_orchestrator_status 给注册表在册条目 removable=True，
    适配器/config 来源条目（telegram default、web）removable=False。"""
    from src.integrations.account_registry import get_account_registry
    from src.web.routes.unified_inbox_read_routes import (
        _merge_orchestrator_status,
    )

    get_account_registry().upsert(
        "whatsapp", "wa_rm_flag", mode="protocol", status="online")
    platform_status = {
        "telegram": {"platform": "telegram", "account_id": "default",
                     "label": "Telegram", "running": False},
        "web_web": {"platform": "web", "account_id": "web",
                    "label": "网页客服", "running": True},
        "whatsapp:wa_rm_flag": {"platform": "whatsapp",
                                "account_id": "wa_rm_flag", "running": True},
    }
    _merge_orchestrator_status(platform_status, None)
    assert platform_status["whatsapp:wa_rm_flag"]["removable"] is True
    assert platform_status["telegram"]["removable"] is False
    assert platform_status["web_web"]["removable"] is False
