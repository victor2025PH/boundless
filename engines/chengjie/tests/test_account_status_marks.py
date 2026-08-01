"""账号状态真相化门禁（P0 → 产品修订 2026-08-01）。

事故背景：Messenger 账号已登出（注册表 status=offline），但工作台仍渲染成在线。
产品口径修订后：**全平台退出 = 聊天页不显示**（会话/chip/未读全隐），消息留本机
store；同号 ``status→online``（重登）后同一 conversation_id 自然回显。账号管理
``/api/accounts`` 仍列 offline 供重登。

门禁钉死：

1. ``_account_status_map`` include_removed 死代码 bug 回归。
2. store 列表：**跳过** offline 会话（不进聊天页）；removed 仍只读打标。
3. ``_merge_orchestrator_status``：offline **不得**进聊天页 platform_status。
4. 发送闸门 / session-status 回写 / 草稿 account_offline 护栏仍在。
5. protocol 适配器 active 桶不含 offline。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations.account_registry import get_account_registry


def _fake_request(inbox_store=None, config=None):
    """最小 request 替身：只带 _account_status_map/_collect_chats_from_store 消费的字段。"""
    state = SimpleNamespace(
        config_manager=SimpleNamespace(config=config) if config is not None else None,
        inbox_store=inbox_store,
    )
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _seed_registry():
    reg = get_account_registry()
    reg.upsert("messenger", "m_out", mode="web", status="offline")
    reg.upsert("whatsapp", "w_removed", mode="protocol", status="removed")
    reg.upsert("telegram", "t_on", mode="protocol", status="online")
    reg.upsert("line", "l_pending", mode="protocol", status="pending")
    return reg


# ── 1. 状态映射（含 include_removed bug 回归钉） ─────────────────────────────

def test_status_map_includes_removed_and_offline():
    _seed_registry()
    from src.web.routes.unified_inbox_aggregate import _account_status_map
    m = _account_status_map(_fake_request())
    assert m[("messenger", "m_out")] == "offline"
    # bug 回归钉：removed 行必须可见（旧实现 list() 默认排除 → 恒空集）
    assert m[("whatsapp", "w_removed")] == "removed"
    assert ("telegram", "t_on") not in m
    assert ("line", "l_pending") not in m


def test_status_map_respects_show_removed_history_flag():
    _seed_registry()
    from src.web.routes.unified_inbox_aggregate import _account_status_map
    cfg = {"inbox": {"show_removed_history": False}}
    m = _account_status_map(_fake_request(config=cfg))
    # 开关只隐藏 removed 的只读历史；offline 是可恢复态，不受该开关影响
    assert ("whatsapp", "w_removed") not in m
    assert m[("messenger", "m_out")] == "offline"


def test_protocol_adapter_removed_bucket_not_empty():
    """channel_adapters._protocol_ids 同款 include_removed bug 的回归钉。"""
    _seed_registry()
    from src.inbox.channel_adapters import ProtocolInboxAdapter
    active, removed = ProtocolInboxAdapter()._protocol_ids()
    assert "w_removed" in removed.get("whatsapp", set())
    assert "t_on" in active.get("telegram", set())


# ── 2. store 读路径行打标 ────────────────────────────────────────────────────

def test_store_row_marks_removed_readonly():
    from src.inbox.normalizer import store_row_to_chat
    base = {"platform": "messenger", "account_id": "m_out", "chat_key": "u1",
            "conversation_id": "messenger:m_out:u1", "last_text": "hi",
            "last_ts": 1000.0}
    # removed：只读历史（旧语义不变）
    row2 = store_row_to_chat(base, read_only=True, account_status="removed")
    assert row2["read_only"] is True and row2["can_send"] is False
    # 正常账号：缺省语义不变
    row3 = store_row_to_chat(base)
    assert row3["can_send"] is True and row3["read_only"] is False
    assert row3["account_status"] == ""


def test_collect_chats_hides_offline_keeps_history_in_store(monkeypatch):
    """已登出会话不进聊天列表；重登(online)后同路径自动回显（库未删）。"""
    _seed_registry()

    class _FakeStore:
        def list_conversations(self, **kw):
            mk = lambda p, a, k: {  # noqa: E731
                "platform": p, "account_id": a, "chat_key": k,
                "conversation_id": f"{p}:{a}:{k}", "last_text": "x",
                "last_ts": 1.0, "unread": 0,
            }
            return [mk("messenger", "m_out", "c1"),
                    mk("whatsapp", "w_removed", "c2"),
                    mk("telegram", "t_on", "c3")]

        def count_messages(self, cid):
            return 1

        def get_conversation(self, cid):
            return {"platform": "messenger", "account_id": "m_out",
                    "chat_key": "c1", "conversation_id": cid,
                    "last_text": "x", "last_ts": 1.0}

    from src.web.routes import unified_inbox_aggregate as agg
    req = _fake_request(inbox_store=_FakeStore(), config={})
    rows = agg._collect_chats_from_store(req, limit=10)
    by_acct = {r["account_id"]: r for r in rows}
    assert "m_out" not in by_acct, "offline 不得进聊天列表"
    assert by_acct["w_removed"]["account_status"] == "removed"
    assert by_acct["w_removed"]["read_only"] is True
    assert by_acct["t_on"]["can_send"] is True
    # 会话头兜底：offline → None（线程接口也不会放行）
    assert agg._store_conv_as_chat(req, "messenger:m_out:c1") is None
    # 同号重登 → 历史回显
    get_account_registry().upsert("messenger", "m_out", mode="web",
                                  status="online")
    rows2 = agg._collect_chats_from_store(req, limit=10)
    assert any(r["account_id"] == "m_out" for r in rows2)


def test_protocol_active_excludes_offline():
    """protocol 适配器 active 桶不含 offline（全平台聊天页同口径）。"""
    _seed_registry()
    # protocol 模式号才能进 ProtocolInboxAdapter 分桶
    reg = get_account_registry()
    reg.upsert("messenger", "m_out", mode="protocol", status="offline")
    reg.upsert("telegram", "t_on", mode="protocol", status="online")
    from src.inbox.channel_adapters import ProtocolInboxAdapter
    active, removed = ProtocolInboxAdapter()._protocol_ids()
    assert "m_out" not in active.get("messenger", set())
    assert "t_on" in active.get("telegram", set())


# ── 3. platform_status 不展示 offline（聊天页） ──────────────────────────────

def _merge(platform_status, orch_accounts=()):
    import src.integrations.account_orchestrator as ao
    from src.web.routes.unified_inbox_read_routes import (
        _merge_orchestrator_status,
    )

    class _FakeOrch:
        def status(self):
            return {"accounts": list(orch_accounts)}

    orig = ao.get_orchestrator
    ao.get_orchestrator = lambda cfg=None: _FakeOrch()   # noqa: E731
    try:
        _merge_orchestrator_status(platform_status, None)
    finally:
        ao.get_orchestrator = orig
    return platform_status


def test_merge_excludes_offline_from_chat_chips():
    """已登出号不得进聊天页 platform_status（无 chip）；账号管理另路可见。"""
    _seed_registry()
    ps = _merge({})
    assert "messenger:m_out" not in ps
    assert "whatsapp:w_removed" not in ps


def test_merge_strips_orch_residue_of_logged_out():
    """编排器残留的已登出条目也要从聊天页 platform_status 摘掉。"""
    _seed_registry()
    ps = {"messenger:m_out": {"platform": "messenger", "account_id": "m_out",
                              "running": False, "label": "", "state": "error"}}
    out = _merge(ps)
    assert "messenger:m_out" not in out


# ── 4. 发送闸门 ──────────────────────────────────────────────────────────────

def test_send_block_semantics(monkeypatch):
    _seed_registry()
    import src.integrations.account_orchestrator as ao
    from src.web.routes.unified_inbox_send_routes import _account_send_block

    class _Orch:
        def __init__(self, owns):
            self._o = owns

        def owns(self, p, a):
            return self._o

    monkeypatch.setattr(ao, "get_orchestrator", lambda cfg=None: _Orch(False))
    assert _account_send_block("whatsapp", "w_removed") == "removed"
    assert _account_send_block("messenger", "m_out") == "offline"
    assert _account_send_block("telegram", "t_on") == ""
    assert _account_send_block("telegram", "default") == ""   # A 线不误拦
    assert _account_send_block("messenger", "ghost_unknown") == ""
    # 注册表说 offline 但编排器有在跑 worker（状态陈旧）→ 以运行时为准放行
    monkeypatch.setattr(ao, "get_orchestrator", lambda cfg=None: _Orch(True))
    assert _account_send_block("messenger", "m_out") == ""


# ── 5. session-status 回写注册表（被动退出闭环） ─────────────────────────────

@pytest.fixture()
def _session_client(monkeypatch):
    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    from src.web.routes.unified_inbox_account_routes import (
        register_account_routes,
    )
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    return TestClient(app)


def _post_status(client, status, acct="m_out", plat="messenger"):
    return client.post("/api/internal/protocol/session-status", json={
        "platform": plat, "account_id": acct, "status": status,
        "login_id": "lg1", "detail": "t",
    })


def test_session_status_marks_registry_offline(_session_client):
    reg = get_account_registry()
    reg.upsert("messenger", "m_out", mode="web", status="online")
    r = _post_status(_session_client, "needs_login")
    assert r.status_code == 200
    assert reg.get("messenger", "m_out")["status"] == "offline"
    # authorized 恢复 online（重登成功自动归位）
    _post_status(_session_client, "authorized")
    assert reg.get("messenger", "m_out")["status"] == "online"


def test_session_status_never_resurrects_removed(_session_client):
    reg = get_account_registry()
    reg.upsert("whatsapp", "w_removed", mode="protocol", status="removed")
    _post_status(_session_client, "authorized", acct="w_removed", plat="whatsapp")
    assert reg.get("whatsapp", "w_removed")["status"] == "removed"
    _post_status(_session_client, "logged_out", acct="w_removed", plat="whatsapp")
    assert reg.get("whatsapp", "w_removed")["status"] == "removed"


def test_session_status_failed_does_not_mark_offline(_session_client):
    """failed=崩溃循环放弃（可自愈故障）≠ 已退出；标 offline 会误导运营去手动重登。"""
    reg = get_account_registry()
    reg.upsert("messenger", "m_out", mode="web", status="online")
    _post_status(_session_client, "failed")
    assert reg.get("messenger", "m_out")["status"] == "online"


def test_session_status_unknown_account_no_row_created(_session_client):
    reg = get_account_registry()
    _post_status(_session_client, "needs_login", acct="never_seen")
    assert reg.get("messenger", "never_seen") is None


# ── 6. 草稿「通过」预判/护栏第三档：account_offline ──────────────────────────

def test_draft_account_offline_block(monkeypatch):
    """已退出账号的待审草稿：徽标与护栏同口径拦（通过了也发不出去）。"""
    _seed_registry()
    import src.integrations.account_orchestrator as ao
    from src.inbox.drafts import DraftService

    class _Orch:
        def owns(self, p, a):
            return False

    monkeypatch.setattr(ao, "get_orchestrator", lambda cfg=None: _Orch())
    svc = DraftService.__new__(DraftService)   # 不建 DB：只测判定入口
    svc._inbox_deliver_cb = lambda *a, **k: None
    svc._stale_approve_hours = 24.0
    draft_out = {"draft_id": "inbox:1", "created_ts": __import__("time").time(),
                 "conversation_id": "messenger:m_out:peer1"}
    draft_on = {"draft_id": "inbox:2", "created_ts": __import__("time").time(),
                "conversation_id": "telegram:t_on:peer2"}
    # 徽标（公开入口）
    assert svc.approve_block_reason(draft_out) == "account_offline"
    assert svc.approve_block_reason(draft_on) == ""
    # 护栏（_stale_check）与徽标一致，且给专属 409 语义
    blk = svc._stale_check(draft_out, "approve")
    assert blk is not None and blk["code"] == 409
    assert blk.get("account_offline") is True
    assert blk["stale_reason"] == "account_offline"
    # 稿龄护栏关闭（max_h=0）时 account_offline 仍然生效（与稿龄正交）
    svc._stale_approve_hours = 0.0
    assert svc.approve_block_reason(draft_out) == "account_offline"
    assert svc._stale_check(draft_out, "approve") is not None
    # 无时间戳的草稿：age/replied 不判，但 account_offline 照拦（徽标=护栏）
    draft_nots = {"draft_id": "inbox:3",
                  "conversation_id": "messenger:m_out:peer3"}
    assert svc.approve_block_reason(draft_nots) == "account_offline"
    assert svc._stale_check(draft_nots, "approve") is not None
    # 编排器实际有在跑 worker（状态陈旧）→ 放行
    monkeypatch.setattr(ao, "get_orchestrator",
                        lambda cfg=None: type("O", (), {"owns": lambda s, p, a: True})())
    assert svc.approve_block_reason(draft_out) == ""
    # force_override（主管逃生门）放行
    monkeypatch.setattr(ao, "get_orchestrator", lambda cfg=None: _Orch())
    assert svc._stale_check(draft_out, "approve", force_override=True) is None
    # 未接线（不会真发）→ 不拦
    svc._inbox_deliver_cb = None
    assert svc.approve_block_reason(draft_out) == ""
    assert svc._stale_check(draft_out, "approve") is None


# ── 7. SLA 对死号静音 + 健康表跨重启种子化 ─────────────────────────────────

def test_enrich_sla_muted_for_offline_accounts(monkeypatch):
    """已退出账号会话：保留 unanswered_sec，但 sla_level/sla_breach 必须静音。"""
    _seed_registry()
    from src.web.routes import unified_inbox_read_routes as rr

    class _FakeIbx:
        def last_message_dirs(self, cids):
            return {cid: {"direction": "in", "ts": __import__("time").time() - 7200}
                    for cid in cids}

    req = _fake_request()
    req.app.state.inbox_store = _FakeIbx()
    monkeypatch.setattr(rr, "_inbox_store", lambda r: r.app.state.inbox_store)
    monkeypatch.setattr(rr, "_sla_cfg",
                        lambda r: {"warn": 1800, "crit": 7200})
    monkeypatch.setattr(rr, "_contacts_store", lambda r: None)
    chats = [
        {"platform": "messenger", "account_id": "m_out",
         "conversation_id": "messenger:m_out:p1", "account_status": "offline"},
        {"platform": "telegram", "account_id": "t_on",
         "conversation_id": "telegram:t_on:p2", "account_status": ""},
    ]
    rr._enrich_chat_list(req, chats, config_manager=None)
    out, on = chats[0], chats[1]
    assert out["unanswered_sec"] >= 7000
    assert out["sla_breach"] is False and out["sla_level"] == ""
    assert on["sla_breach"] is True and on["sla_level"] in ("warn", "crit")


def test_health_seed_from_registry_offline(monkeypatch):
    """重启后 ensure_seeded 必须把注册表 offline 号灌回健康表（掉线时长不归零）。"""
    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    _seed_registry()
    n = psh.ensure_seeded_from_registry()
    assert n >= 1
    hp = psh.get_platform_session_health()
    dump = hp.dump()
    key = hp._key("messenger", "m_out")
    assert key in dump["sessions"]
    sess = dump["sessions"][key]
    assert sess["status"] == "logged_out"
    assert float(sess["unhealthy_since"]) > 0
    assert sess.get("seeded") is True
    # 幂等：再调一次不重复灌
    assert psh.ensure_seeded_from_registry() == 0
    # 已有真事件时种子不覆盖
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    hp.record("messenger", "m_out", "needs_login", detail="live")
    # 重新种子（模拟进程未重启但函数被再调）——key 已在，跳过
    assert hp.seed_offline_from_registry([
        {"platform": "messenger", "account_id": "m_out", "status": "offline",
         "updated_at": 1.0},
    ]) == 0
    assert hp.dump()["sessions"][key]["status"] == "needs_login"


def test_channel_banner_excludes_intentional_logout(monkeypatch):
    """顶栏「通道离线」横幅不把运营主动登出当故障催修。"""
    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    hp = psh.get_platform_session_health()
    hp.record("messenger", "a", "logged_out", detail="logout")
    hp.record("whatsapp", "b", "needs_login", detail="cookie")
    from src.web.routes.unified_inbox_setup_routes import _channel_health_snapshot
    snap = _channel_health_snapshot()
    plats = {it["platform"] for it in snap["unhealthy"]}
    assert "whatsapp" in plats
    assert "messenger" not in plats
