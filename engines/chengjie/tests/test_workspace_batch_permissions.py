# -*- coding: utf-8 -*-
"""十二期：批量操作权限与转派审计。

- viewer（只读账号）对 batch/archive|tags|assign 一律 403；
- agent/master 正常放行；
- assign 成功后落 record_draft_audit(action="assign") 留痕。
"""
import pytest

from src.utils.web_user_store import ROLE_AGENT, ROLE_VIEWER, WebUserStore


def _login_as(client, config_dir, username, role):
    store = WebUserStore(config_dir / "web_users.db")
    if not store.get_user(username):
        store.create_user(username, "pw-123456", role)
    client.get("/login")
    r = client.post(
        "/login",
        data={"username": username, "password": "pw-123456"},
        follow_redirects=True,
    )
    assert "/login" not in str(getattr(r, "url", "")), f"{username} 登录失败"
    # JSON POST 过 CSRF：双提交 cookie ↔ 头（不用 Bearer——那会绕过 session 角色）
    tok = client.cookies.get("csrf_token", "")
    assert tok, "缺 csrf_token cookie"
    client.headers.update({"X-CSRF-Token": tok})
    return client


@pytest.mark.parametrize("path,body", [
    ("/api/workspace/batch/archive", {"conversation_ids": ["c1"], "archived": True}),
    ("/api/workspace/batch/tags", {"conversation_ids": ["c1"], "tags": ["vip"]}),
    ("/api/workspace/batch/assign", {"conversation_ids": ["c1"], "agent_id": "a1"}),
])
def test_viewer_denied_on_batch_writes(client, config_dir, path, body):
    _login_as(client, config_dir, "ro_viewer", ROLE_VIEWER)
    r = client.post(path, json=body)
    assert r.status_code == 403, (path, r.status_code, r.text[:200])
    # 必须是角色拒绝而非 CSRF 拒绝（防假通过）
    assert "CSRF" not in r.text, (path, r.text[:200])


def test_agent_allowed_on_batch_assign(client, config_dir):
    _login_as(client, config_dir, "agent_amy", ROLE_AGENT)
    r = client.post("/api/workspace/batch/assign",
                    json={"conversation_ids": ["c1"], "agent_id": "a2"})
    # 不因权限拒绝；store 未挂载时返回 ok:false 的业务错误（非 403）
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_assign_writes_audit_event(app, client, config_dir, tmp_path):
    from src.inbox.models import InboxConversation
    from src.inbox.store import InboxStore

    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(InboxConversation(
        conversation_id="tg:a:1", platform="telegram",
        account_id="a", chat_key="1", display_name="客户",
        last_ts=1000.0,
    ))
    app.state.inbox_store = store

    _login_as(client, config_dir, "agent_bob", ROLE_AGENT)
    r = client.post("/api/workspace/batch/assign",
                    json={"conversation_ids": ["tg:a:1"], "agent_id": "agent_amy"})
    d = r.json()
    assert r.status_code == 200 and d.get("ok") is True, r.text[:200]
    # 十二期修复回归锚：原实现静默空转恒 updated=0，修复后必须真落 1 条
    assert d.get("updated") == 1, d

    # 转派落 claims 事实源（行徽章/「我的」筛选同源）
    claim = store.get_conversation_claim("tg:a:1")
    assert claim and claim.get("agent_id") == "agent_amy", claim

    rows = store.list_draft_audit(limit=10)
    hit = [x for x in rows if x.get("action") == "assign"]
    assert hit, f"缺 assign 审计事件: {rows}"
    # 操作者身份沿用 _session_agent 约定（user_id 主键优先），断言"有身份且非目标坐席"
    op = str(hit[0].get("agent_id") or "")
    assert op and op != "agent_amy", hit[0]
    assert "agent_amy" in str(hit[0].get("reason") or "")
    assert hit[0].get("conversation_id") == "tg:a:1"
