"""平台会话健康闭环契约（P0-2）：store 迁移语义 + push 端点 + 告警发布。

链路：messenger-web(Node) 在登录/掉线/放弃自愈时 POST
``/api/internal/protocol/session-status`` → ``PlatformSessionHealth`` 登记 →
「进入不健康/恢复」经 EventBus 发 ``platform_session_alert``（告警渠道订阅别名
``platform_session``）→ ops 看板卡片读 ``/api/workspace/metrics.platform_sessions``。

之前 Node 掉线只写自己的日志，Python/运营两眼一抹黑（「会话死了还在装在线」），
本文件把「上报→登记→告警→观测」四步的契约钉死。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_account_routes import register_account_routes


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
    """每例独立的健康单例 + EventBus（防跨测试污染）。"""
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    yield


def _store():
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    return get_platform_session_health()


def _client():
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    return TestClient(app)


def _events():
    from src.integrations.shared.event_bus import get_event_bus
    return [e for e in get_event_bus().recent_events(50)
            if e["type"] == "platform_session_alert"]


# ── store 纯语义 ─────────────────────────────────────────────────────────────

def test_store_transition_semantics():
    s = _store()
    t1 = s.record("messenger", "100", "authorized")
    assert t1["changed"] and not t1["went_unhealthy"] and not t1["recovered"]

    t2 = s.record("messenger", "100", "needs_login", detail="cookies expired")
    assert t2["went_unhealthy"] and not t2["recovered"]
    assert s.is_unhealthy("messenger", "100") is True

    # 重复同态：不再触发 went_unhealthy（防告警风暴）
    t3 = s.record("messenger", "100", "needs_login")
    assert not t3["changed"] and not t3["went_unhealthy"]

    # needs_login → expired：仍不健康，但不算「新进入不健康」
    t4 = s.record("messenger", "100", "expired")
    assert t4["changed"] and not t4["went_unhealthy"] and not t4["recovered"]

    t5 = s.record("messenger", "100", "authorized")
    assert t5["recovered"] and s.is_unhealthy("messenger", "100") is False


def test_store_unknown_session_is_healthy():
    assert _store().is_unhealthy("messenger", "nobody") is False


# ── B63-②（实施64 P1-4）：发送失败会话性败因 → 健康登记 ─────────────────────

def test_note_send_auth_failure_maps_session_codes(monkeypatch):
    import src.integrations.platform_session_health as psh
    seen = []
    monkeypatch.setattr(
        psh, "report_session_transition",
        lambda p, a, st, detail="", login_id="": seen.append((p, a, st, detail)) or {})
    # PIN 浮层 → failed + 精确 hint 码（坐席该做的是填 PIN，不是完整重登）
    assert psh.note_send_auth_failure(
        "messenger", "42",
        "Server error '500' — composer not found [e2ee_pin_prompt]") == "failed"
    assert seen[-1][2] == "failed" and seen[-1][3] == "e2ee_pin_required"
    # 接受浮层 → failed + needs_accept
    assert psh.note_send_auth_failure(
        "messenger", "42", "blocked [needs_accept]") == "failed"
    assert seen[-1][3] == "needs_accept"
    # 登出态 → needs_login（连带注册表 offline 由 report_session_transition 语义承担）
    assert psh.note_send_auth_failure(
        "messenger", "42", "send failed: not_logged_in") == "needs_login"
    # 普通发送失败（渲染超时等）不登记——不是账号级处境
    assert psh.note_send_auth_failure(
        "messenger", "42", "render_timeout after 8s") == ""
    assert psh.note_send_auth_failure("messenger", "42", "") == ""
    assert len(seen) == 3


def test_note_send_auth_failure_never_raises(monkeypatch):
    import src.integrations.platform_session_health as psh

    def _boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(psh, "report_session_transition", _boom)
    assert psh.note_send_auth_failure("messenger", "42", "e2ee_pin_prompt") == ""


def test_send_fail_wiring_present_in_both_send_paths():
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    ca = (repo / "src" / "inbox" / "channel_adapters.py").read_text(encoding="utf-8")
    ao = (repo / "src" / "integrations" / "account_orchestrator.py"
          ).read_text(encoding="utf-8")
    assert "note_send_auth_failure" in ca, "统一收件箱 messenger 发送路径未接会话登记"
    assert "note_send_auth_failure" in ao, "编排器 messenger 发送路径未接会话登记"


def test_inbox_health_blindspot_observability_fields():
    """P1 2026-08-15 盲区修复观测：unread_forced / worker_code_* /
    requests_suspect 随心跳落行；**缺省（老 worker 未上报）不写键**——
    dump 消费面据缺键区分「没报」与「报了 0」（与 pin_heal 同约定）。"""
    s = _store()
    # 老 worker：不带新字段 → 行里不出现新键
    s.record_inbox_health("messenger", "acc1", unread=0)
    h1 = s.inbox_health()["messenger:acc1"]
    assert "unread_forced" not in h1
    assert "worker_code_stale" not in h1
    assert "worker_code_fp" not in h1
    assert "requests_suspect" not in h1
    # 新 worker：上报即落行（含 0 值——「报了 0」是有效读数）
    s.record_inbox_health(
        "messenger", "acc1", unread=0,
        unread_forced_picked=0, worker_code_stale=False,
        worker_code_fp="2fe7f8944723", requests_suspect_streak=0)
    h2 = s.inbox_health()["messenger:acc1"]
    assert h2["unread_forced"] == 0
    assert h2["worker_code_stale"] is False
    assert h2["worker_code_fp"] == "2fe7f8944723"
    assert h2["requests_suspect"] == 0
    # 值更新 + 半部署位翻转 + 指纹消毒（非法字符剥离）
    s.record_inbox_health(
        "messenger", "acc1", unread=1,
        unread_forced_picked=7, worker_code_stale=True,
        worker_code_fp="ab<script>12", requests_suspect_streak=9)
    h3 = s.inbox_health()["messenger:acc1"]
    assert h3["unread_forced"] == 7
    assert h3["worker_code_stale"] is True
    assert h3["worker_code_fp"] == "abscript12"
    assert h3["requests_suspect"] == 9
    # dump 透传（ops 卡消费口径）
    d = s.dump()["inbox_health"]["messenger:acc1"]
    assert d["unread_forced"] == 7 and d["worker_code_stale"] is True


def test_endpoint_passes_blindspot_fields():
    """路由透传契约：worker 心跳带新键 → 落行；缺键 → 不写（老 worker 兼容）。"""
    c = _client()
    r = c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "777",
        "unread": 2, "read_attempts": 1, "read_fails": 0,
        "unread_forced_picked": 3, "worker_code_stale": True,
        "worker_code_fp": "deadbeef1234", "requests_suspect_streak": 5,
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    h = _store().inbox_health()["messenger:777"]
    assert h["unread_forced"] == 3
    assert h["worker_code_stale"] is True
    assert h["worker_code_fp"] == "deadbeef1234"
    assert h["requests_suspect"] == 5
    # 老 worker 心跳（无新键）不清已有值、也不误写 False/0
    c.post("/api/internal/protocol/inbox-health", json={
        "platform": "messenger", "account_id": "888", "unread": 0,
    })
    h2 = _store().inbox_health()["messenger:888"]
    assert "worker_code_stale" not in h2 and "unread_forced" not in h2


def test_login_promotion_clears_placeholder_row():
    """晋级清占位（P0 2026-08-14）：登录早期以 login_id 顶位 key 挂的占位行，在
    同一 login_id 携真实 account_id 报 authorized 时立即清除——不清会以「另一个号」
    的身份在横幅/看门狗常亮，此前只有 30 分钟 TTL 兜底。"""
    s = _store()
    # 登录初期：account_id 未知，worker 以 login_id 顶位上报（路由 acct or login_id）
    s.record("messenger", "msg_abc123", "expired",
             detail="login pending", login_id="msg_abc123")
    assert s.is_unhealthy("messenger", "msg_abc123") is True
    s.record_inbox_health("messenger", "msg_abc123", unread=3,
                          read_attempts=3, read_fails=3)
    # 授权成功：同 login_id 晋级为真实账号 → 占位行（会话+入站健康）当场清除
    s.record("messenger", "100", "authorized", login_id="msg_abc123")
    d = s.dump()
    assert "messenger:msg_abc123" not in d["sessions"]
    assert "messenger:msg_abc123" not in s.inbox_health()
    assert d["sessions"]["messenger:100"]["status"] == "authorized"
    # 无 login_id / login_id==key 的常规上报不受影响
    s.record("messenger", "200", "authorized")
    assert s.dump()["sessions"]["messenger:200"]["status"] == "authorized"


def test_store_dump_and_prom():
    s = _store()
    s.record("messenger", "100", "expired", detail="crash-loop")
    s.record("whatsapp", "wa1", "authorized")
    d = s.dump()
    assert d["total_events"] == 2
    assert d["unhealthy_count"] == 1
    assert "messenger:100" in d["unhealthy"]
    assert d["sessions"]["whatsapp:wa1"]["status"] == "authorized"
    prom = s.dump_prom()
    assert 'platform_session_unhealthy{session="messenger:100"} 1' in prom
    assert 'platform_session_unhealthy{session="whatsapp:wa1"} 0' in prom
    assert 'platform_session_events_total{status="expired"} 1' in prom


def test_store_distinct_key_cap():
    s = _store()
    for i in range(200):
        s.record("messenger", f"acct{i}", "expired")
    assert len(s.dump()["sessions"]) <= 64  # 上限防脏数据撑爆
    assert s.dump()["total_events"] == 200  # 事件计数仍如实累计


# ── push 端点 + 告警发布 ─────────────────────────────────────────────────────

def test_endpoint_records_and_alerts_on_unhealthy():
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100", "login_id": "msg_x",
        "status": "needs_login", "detail": "cookies expired", "ts": 1780000000,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["went_unhealthy"] is True
    assert _store().is_unhealthy("messenger", "100") is True
    evs = _events()
    assert len(evs) == 1
    assert evs[0]["data"]["status"] == "needs_login"
    assert evs[0]["data"]["recovered"] is False


def test_endpoint_repeat_unhealthy_no_alert_storm():
    c = _client()
    for _ in range(3):
        c.post("/api/internal/protocol/session-status", json={
            "platform": "messenger", "account_id": "100",
            "status": "needs_login",
        })
    assert len(_events()) == 1  # 只在「进入不健康」时发一次


def test_endpoint_recovery_alert():
    c = _client()
    c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100", "status": "expired",
    })
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "100", "status": "authorized",
        "detail": "connected",
    })
    assert r.json()["recovered"] is True
    evs = _events()
    assert len(evs) == 2
    assert evs[-1]["data"]["recovered"] is True
    assert _store().is_unhealthy("messenger", "100") is False


def test_endpoint_missing_fields_rejected_softly():
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={"status": "x"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    r2 = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger"})
    assert r2.json()["ok"] is False


def test_endpoint_keys_on_login_id_when_account_unknown():
    """restore 早期 accountId 可能还读不到 → 以 login_id 兜底登记，事件不丢。"""
    c = _client()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "login_id": "msg_abc",
        "status": "needs_login",
    })
    assert r.json()["ok"] is True
    assert _store().is_unhealthy("messenger", "msg_abc") is True


# ── P1 身份化：authorized 推送携带 pushname/avatar_url → 富集 self_* ─────────

def _client_with_cfg():
    from types import SimpleNamespace
    app = FastAPI()
    register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={
            "accounts": {"self_profile": {"enabled": True}}}))
    return TestClient(app)


def test_endpoint_enriches_self_profile_on_authorized(monkeypatch):
    """重启后 Node restoreAll 重连（不经登录轮询）→ authorized push 即回填身份。"""
    import src.integrations.account_self_profile as sp
    calls = []

    async def fake_enrich(platform, account_id, **kw):
        calls.append((platform, account_id, kw.get("name"),
                      kw.get("avatar_url")))
        return {"self_name": kw.get("name", "")}

    monkeypatch.setattr(sp, "enrich_from_fields", fake_enrich)
    c = _client_with_cfg()
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "whatsapp", "account_id": "639555000111",
        "login_id": "wa_x", "status": "authorized", "detail": "connected",
        "pushname": "雷人1", "avatar_url": "https://pps.whatsapp.net/p.jpg",
    })
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == [("whatsapp", "639555000111", "雷人1",
                      "https://pps.whatsapp.net/p.jpg")]


def test_endpoint_no_enrich_without_identity_or_when_unhealthy(monkeypatch):
    """字段缺失 / 非 authorized 状态 → 不触发富集（向后兼容旧 Node）。"""
    import src.integrations.account_self_profile as sp
    calls = []

    async def fake_enrich(*a, **k):  # pragma: no cover - 不应被调用
        calls.append((a, k))
        return {}

    monkeypatch.setattr(sp, "enrich_from_fields", fake_enrich)
    c = _client_with_cfg()
    c.post("/api/internal/protocol/session-status", json={
        "platform": "whatsapp", "account_id": "639555000111",
        "status": "authorized",   # 无 pushname/avatar_url（旧版 Node）
    })
    c.post("/api/internal/protocol/session-status", json={
        "platform": "whatsapp", "account_id": "639555000111",
        "status": "logged_out", "pushname": "雷人1",   # 非 authorized
    })
    assert calls == []


# ── 告警文案：platform_session_alert 有专属 _build_message 分支 ──────────────

def test_notifier_message_branch():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert "platform_session" in _EVENT_ALIASES
    title, text = _build_message("platform_session_alert", {
        "platform": "messenger", "account_id": "100",
        "status": "needs_login", "detail": "cookies expired",
        "recovered": False,
    })
    assert "messenger" in title and title != "[platform_session_alert] 事件"
    assert "100" in text
    t2, _ = _build_message("platform_session_alert", {
        "platform": "messenger", "account_id": "100", "recovered": True,
    })
    assert "恢复" in t2
