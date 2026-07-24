"""平台会话自愈闭环二期契约（P1-P4 稳定性延伸）。

一期（P0-2）钉死了「上报→登记→告警→观测」；本文件钉死二期四件事：

1. **持续掉线提醒**（P4）：掉线转移告警只发一次，若长时间没人修，
   ``PlatformSessionHealth.due_reminders`` + ``HealthWatchdog._check_platform_sessions``
   周期补提醒（升级式：after_min 首提 → 每 interval_min 一条；恢复自动清零）。
2. **一键重登**（P2）：``POST /api/admin/platform-sessions/relogin`` 转发给
   messenger-web 的 ``/accounts/:id/relogin``（同 profile 重启 + 交互登录窗口）。
3. **WhatsApp 快速失败闸**（P3）：baileys push 的会话健康登记显示被登出/重连放弃
   → ``WhatsAppProtocolWorker.send/send_media`` 快速失败（与 messenger 同口径）。
4. **限流判别符**（rate_key）：多账号同小时先后掉线各自成键，不再共挤
   「每小时一条」的窗口；提醒与转移告警键分离。
"""

from __future__ import annotations

import asyncio
import time
import types
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch):
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


def _events():
    from src.integrations.shared.event_bus import get_event_bus
    return [e for e in get_event_bus().recent_events(50)
            if e["type"] == "platform_session_alert"]


# ── 1) store：持续不健康跟踪 + 提醒节流 ─────────────────────────────────────

def test_unhealthy_since_survives_same_status_repush():
    """放弃自愈的周期重报（同态 re-record）不能刷新掉线起点，「已掉线多久」才可信。"""
    s = _store()
    s.record("messenger", "100", "expired")
    since1 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    time.sleep(0.01)
    s.record("messenger", "100", "expired", detail="slow-retry gave up again")
    assert s.dump()["sessions"]["messenger:100"]["unhealthy_since"] == since1
    # 不健康态之间迁移（expired → needs_login）也不重置起点
    s.record("messenger", "100", "needs_login")
    assert s.dump()["sessions"]["messenger:100"]["unhealthy_since"] == since1


def test_recovery_clears_unhealthy_since_and_remind_ts():
    s = _store()
    s.record("messenger", "100", "expired")
    now = time.time()
    assert s.due_reminders(min_age_sec=0, interval_sec=600, now=now + 1)
    s.record("messenger", "100", "authorized")
    sess = s.dump()["sessions"]["messenger:100"]
    assert sess["unhealthy_since"] == 0.0
    assert sess["last_remind_ts"] == 0.0
    # 再次掉线 → 新一轮起点，从头计时
    s.record("messenger", "100", "expired")
    assert s.dump()["sessions"]["messenger:100"]["unhealthy_since"] > 0


def test_due_reminders_escalation_semantics():
    """after 前不提；after 到 → 首提；interval 内不重复；interval 到 → 再提。"""
    s = _store()
    s.record("messenger", "100", "expired")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]

    # 掉线 10 分钟 < after 30 分钟 → 不提醒
    assert s.due_reminders(min_age_sec=1800, interval_sec=14400,
                           now=t0 + 600) == {}
    # 掉线 31 分钟 → 首提（带 down_sec）
    due = s.due_reminders(min_age_sec=1800, interval_sec=14400, now=t0 + 1860)
    assert "messenger:100" in due
    assert due["messenger:100"]["down_sec"] == pytest.approx(1860, abs=2)
    # 刚提过 → interval 内不重复
    assert s.due_reminders(min_age_sec=1800, interval_sec=14400,
                           now=t0 + 1920) == {}
    # interval（4h）后 → 再提
    due2 = s.due_reminders(min_age_sec=1800, interval_sec=14400,
                           now=t0 + 1860 + 14460)
    assert "messenger:100" in due2


def test_due_reminders_only_unhealthy():
    s = _store()
    s.record("messenger", "100", "authorized")
    assert s.due_reminders(min_age_sec=0, interval_sec=1,
                           now=time.time() + 3600) == {}


# ── 2) watchdog：周期复查 → 发提醒事件 ──────────────────────────────────────

def _watchdog(config=None):
    from src.inbox.health_watchdog import HealthWatchdog
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    return HealthWatchdog(app=app,
                          config_manager=types.SimpleNamespace(config=config or {}),
                          interval_sec=60)


def test_watchdog_emits_reminder_with_rate_key():
    s = _store()
    s.record("messenger", "100", "expired", detail="crash-loop give-up",
             login_id="msg_x")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    wd = _watchdog()
    wd._check_platform_sessions(now=t0 + 3600)  # 掉线 1h > 默认 after 30min
    evs = _events()
    assert len(evs) == 1
    d = evs[0]["data"]
    assert d["reminder"] is True
    assert d["platform"] == "messenger" and d["account_id"] == "100"
    assert d["down_minutes"] == 60
    assert d["rate_key"] == "messenger:100:remind"  # 与转移告警键分离
    assert wd.total_platform_session_reminders == 1
    # 同一轮已标记 → 紧接着复查不重发
    wd._check_platform_sessions(now=t0 + 3660)
    assert len(_events()) == 1


def test_watchdog_reminder_respects_disable_flag():
    s = _store()
    s.record("messenger", "100", "expired")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    wd = _watchdog({"health_watchdog": {
        "session_stale_remind": {"enabled": False}}})
    wd._check_platform_sessions(now=t0 + 86400)
    assert _events() == []


def test_watchdog_no_reminder_when_all_healthy():
    _store().record("messenger", "100", "authorized")
    wd = _watchdog()
    wd._check_platform_sessions(now=time.time() + 86400)
    assert _events() == []


# ── 3) 告警文案与限流判别符 ─────────────────────────────────────────────────

def test_notifier_reminder_message_branch():
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("platform_session_alert", {
        "platform": "messenger", "account_id": "100", "status": "expired",
        "detail": "crash-loop", "reminder": True, "down_minutes": 150,
    })
    assert "持续掉线" in title
    assert "2 小时 30 分钟" in title
    assert "100" in text
    # 转移告警（非提醒）文案不受影响
    t2, _ = _build_message("platform_session_alert", {
        "platform": "messenger", "account_id": "100", "status": "expired",
    })
    assert "持续" not in t2


def test_session_status_endpoint_publishes_rate_key():
    """多账号同小时先后掉线：事件各带 platform:account 判别符，互不挤限流窗。"""
    from src.web.routes.unified_inbox_account_routes import (
        register_account_routes,
    )
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    c = TestClient(app)
    for acct in ("100", "200"):
        c.post("/api/internal/protocol/session-status", json={
            "platform": "messenger", "account_id": acct, "status": "expired",
        })
    evs = _events()
    assert {e["data"]["rate_key"] for e in evs} == {
        "messenger:100", "messenger:200"}


def test_notifier_rate_key_fallback_expression():
    """判别符优先级：draft_id > rate_key > 空（旧事件零行为变化）。"""
    data_draft = {"draft_id": "d1", "rate_key": "x"}
    data_rk = {"rate_key": "messenger:100"}
    data_none: dict = {}
    pick = lambda d: d.get("draft_id") or d.get("rate_key") or ""  # noqa: E731
    assert pick(data_draft) == "d1"
    assert pick(data_rk) == "messenger:100"
    assert pick(data_none) == ""


# ── 4) 一键重登路由 ─────────────────────────────────────────────────────────

def _ops_app(audit=None):
    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    class _Ctx:
        def api_auth(self, request):
            return True

        def api_write(self, perm):
            def _dep():
                return True
            return _dep

        def page_auth(self, request):
            return True

        templates = None
        config_manager = types.SimpleNamespace(config={})
        audit_store = audit
        user_store = None
        token = None

    app = FastAPI()
    register_ops_overview_routes(app, _Ctx())
    return app


def test_relogin_route_happy_path(monkeypatch):
    import src.integrations.messenger_web_login as mgw
    calls = {}

    async def _fake(url, payload, timeout=20.0):
        calls["url"] = url
        return {"ok": True, "login_id": "msg_x", "status": "pending"}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "messenger", "login_id": "msg_x"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "login_id": "msg_x", "status": "pending"}
    assert calls["url"] == "http://svc/accounts/msg_x/relogin"


def test_relogin_route_account_id_fallback(monkeypatch):
    import src.integrations.messenger_web_login as mgw
    calls = {}

    async def _fake(url, payload, timeout=20.0):
        calls["url"] = url
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "messenger", "account_id": "100"})
    assert r.status_code == 200
    assert calls["url"] == "http://svc/accounts/100/relogin"


def test_relogin_route_validations():
    c = TestClient(_ops_app())
    assert c.post("/api/admin/platform-sessions/relogin",
                  json={"login_id": "x"}).status_code == 400  # 缺 platform
    assert c.post("/api/admin/platform-sessions/relogin",
                  json={"platform": "messenger"}).status_code == 400  # 缺 id
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "line", "login_id": "x"})
    assert r.status_code == 400  # 暂不支持的平台如实拒绝


def test_relogin_route_worker_unreachable_502(monkeypatch):
    import src.integrations.messenger_web_login as mgw

    async def _boom(url, payload, timeout=20.0):
        raise RuntimeError("connect refused")

    monkeypatch.setattr(mgw, "_post_json", _boom)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "messenger", "login_id": "msg_x"})
    assert r.status_code == 502


def test_relogin_route_writes_audit(monkeypatch):
    import src.integrations.messenger_web_login as mgw

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    logged = []
    audit = types.SimpleNamespace(
        log=lambda *a, **k: logged.append((a, k)))
    c = TestClient(_ops_app(audit=audit))
    c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "messenger", "login_id": "msg_x"})
    assert len(logged) == 1
    assert logged[0][0][1] == "platform_session_relogin"


# ── 5) WhatsApp 快速失败闸（与 messenger 同口径） ───────────────────────────

def _wa_worker():
    from src.integrations.account_orchestrator import WhatsAppProtocolWorker
    return WhatsAppProtocolWorker({"account_id": "wa100"}, {})


def test_wa_worker_send_fails_fast_when_logged_out(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wab
    _store().record("whatsapp", "wa100", "logged_out",
                    detail="device unlinked")
    called = {"n": 0}

    async def _fake(url, payload, timeout=20.0):
        called["n"] += 1
        return {"ok": True}

    monkeypatch.setattr(wab, "_post_json", _fake)
    res = asyncio.run(_wa_worker().send("555@s.whatsapp.net", "hi"))
    assert res["delivered"] is False
    assert res["blocked"] == "session_unhealthy"
    assert called["n"] == 0

    # 重新配对成功（Node push authorized）→ 自动放行
    _store().record("whatsapp", "wa100", "authorized")
    res2 = asyncio.run(_wa_worker().send("555@s.whatsapp.net", "hi"))
    assert res2["delivered"] is True and called["n"] == 1


def test_wa_worker_send_media_also_gated(monkeypatch):
    import src.integrations.whatsapp_baileys_login as wab
    _store().record("whatsapp", "wa100", "expired",
                    detail="reconnect gave up")

    async def _fake(url, payload, timeout=20.0):  # pragma: no cover
        raise AssertionError("掉线会话不应尝试发媒体")

    monkeypatch.setattr(wab, "_post_json", _fake)
    res = asyncio.run(_wa_worker().send_media(
        "555@s.whatsapp.net", media_path="x.jpg", media_type="image"))
    assert res["delivered"] is False and res["blocked"] == "session_unhealthy"


def test_wa_worker_unreported_session_not_gated(monkeypatch):
    """从未上报过健康状态的账号不拦（渐进接入，零误伤）。"""
    import src.integrations.whatsapp_baileys_login as wab

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "message_id": "m1"}

    monkeypatch.setattr(wab, "_post_json", _fake)
    res = asyncio.run(_wa_worker().send("555@s.whatsapp.net", "hi"))
    assert res["delivered"] is True


# ── 5b) P1 身份化：健康轮询机会式回填自身昵称/头像（覆盖 restore 存量号） ────

def test_wa_worker_healthy_backfills_self_profile(monkeypatch):
    """/accounts 带回 pushname/avatar_url → 回填一次；身份未变不重复打扰。"""
    import src.integrations.account_self_profile as sp
    import src.integrations.whatsapp_baileys_login as wab

    async def _fake_get(url, timeout=20.0):
        return {"accounts": [
            {"login_id": "wa_x", "account_id": "wa100",
             "pushname": "雷人1", "avatar_url": "https://pps/x.jpg"},
            {"login_id": "wa_y", "account_id": "other", "pushname": "别家"},
        ]}

    enriched = []

    async def _fake_enrich(platform, account_id, **kw):
        enriched.append((platform, account_id, kw.get("name"),
                         kw.get("avatar_url")))
        return {"self_name": kw.get("name", "")}

    monkeypatch.setattr(wab, "_get_json", _fake_get)
    monkeypatch.setattr(sp, "enrich_from_fields", _fake_enrich)
    w = _wa_worker()
    assert asyncio.run(w.healthy()) is True
    assert asyncio.run(w.healthy()) is True   # 第二轮：身份未变 → 不再 enrich
    assert enriched == [("whatsapp", "wa100", "雷人1", "https://pps/x.jpg")]


def test_wa_worker_healthy_no_identity_fields_noop(monkeypatch):
    """旧版 Node（/accounts 不带身份字段）→ 健康判定照旧、不触发富集。"""
    import src.integrations.account_self_profile as sp
    import src.integrations.whatsapp_baileys_login as wab

    async def _fake_get(url, timeout=20.0):
        return {"accounts": [{"login_id": "wa_x", "account_id": "wa100"}]}

    async def _boom(*a, **k):  # pragma: no cover - 不应被调用
        raise AssertionError("无身份字段不应 enrich")

    monkeypatch.setattr(wab, "_get_json", _fake_get)
    monkeypatch.setattr(sp, "enrich_from_fields", _boom)
    assert asyncio.run(_wa_worker().healthy()) is True


# ── 5c) 编排器真自愈：restore 后账号仍缺席 → 追加账号级 reconnect ────────────
# 事故：Node restoreAll 对内存里已存在（哪怕 expired 假死）的 session 直接 skip，
# 编排器 error→退避→start() 只打 restore → 自愈永远空转。新契约：restore 后核对
# /accounts，自己不在 → POST /accounts/{id}/reconnect（best-effort，失败不抛——
# start() 抛异常会被编排器计为启动失败进退避，反拖慢自愈）。

_WA_BASE = "http://127.0.0.1:8790"  # 空 config 的默认 base（service_base_url 兜底值）


def test_wa_worker_start_reconnects_missing_account(monkeypatch):
    """/accounts 缺自己 → 调用序列：restore → GET /accounts → 账号级 reconnect。"""
    import src.integrations.whatsapp_baileys_login as wab
    calls = []

    async def _fake_post(url, payload, timeout=20.0):
        calls.append(("post", url))
        return {"ok": True, "reconnecting": True}

    async def _fake_get(url, timeout=20.0):
        calls.append(("get", url))
        return {"accounts": [{"account_id": "other"}]}  # 假死账号不在列表

    monkeypatch.setattr(wab, "_post_json", _fake_post)
    monkeypatch.setattr(wab, "_get_json", _fake_get)
    w = _wa_worker()
    asyncio.run(w.start())
    assert w.state == "running"
    assert calls == [
        ("post", f"{_WA_BASE}/accounts/restore"),
        ("get", f"{_WA_BASE}/accounts"),
        ("post", f"{_WA_BASE}/accounts/wa100/reconnect"),
    ]


def test_wa_worker_start_no_reconnect_when_account_present(monkeypatch):
    """/accounts 已含自己（restore 生效/本就在线）→ 不打 reconnect。"""
    import src.integrations.whatsapp_baileys_login as wab
    posts = []

    async def _fake_post(url, payload, timeout=20.0):
        posts.append(url)
        return {"ok": True}

    async def _fake_get(url, timeout=20.0):
        return {"accounts": [{"account_id": "wa100"}]}

    monkeypatch.setattr(wab, "_post_json", _fake_post)
    monkeypatch.setattr(wab, "_get_json", _fake_get)
    w = _wa_worker()
    asyncio.run(w.start())
    assert w.state == "running"
    assert posts == [f"{_WA_BASE}/accounts/restore"]  # 只有 restore，无 reconnect


def test_wa_worker_start_reconnect_failure_swallowed(monkeypatch):
    """reconnect 打不通（Node 正在重启/旧版无端点 404 抛错）→ start() 不抛、照常 running。"""
    import src.integrations.whatsapp_baileys_login as wab

    async def _fake_post(url, payload, timeout=20.0):
        if url.endswith("/reconnect"):
            raise RuntimeError("connect refused")
        return {"ok": True}

    async def _fake_get(url, timeout=20.0):
        return {"accounts": []}

    monkeypatch.setattr(wab, "_post_json", _fake_post)
    monkeypatch.setattr(wab, "_get_json", _fake_get)
    w = _wa_worker()
    asyncio.run(w.start())  # 不得抛出
    assert w.state == "running"


def test_wa_worker_start_accounts_probe_failure_swallowed(monkeypatch):
    """restore 成功但 GET /accounts 挂了 → 核对步骤整体吞掉，start() 仍成功（原语义）。"""
    import src.integrations.whatsapp_baileys_login as wab

    async def _fake_post(url, payload, timeout=20.0):
        if url.endswith("/reconnect"):  # pragma: no cover - 不应走到
            raise AssertionError("核对失败时不应盲打 reconnect")
        return {"ok": True}

    async def _fake_get(url, timeout=20.0):
        raise RuntimeError("node restarting")

    monkeypatch.setattr(wab, "_post_json", _fake_post)
    monkeypatch.setattr(wab, "_get_json", _fake_get)
    w = _wa_worker()
    asyncio.run(w.start())
    assert w.state == "running"


def test_wa_worker_start_restore_failure_still_raises(monkeypatch):
    """回归钉：restore 本身失败仍按旧语义抛出（编排器计启动失败→退避，行为不变）。"""
    import src.integrations.whatsapp_baileys_login as wab

    async def _boom_post(url, payload, timeout=20.0):
        raise RuntimeError("service down")

    async def _fake_get(url, timeout=20.0):  # pragma: no cover - 不应走到
        raise AssertionError("restore 失败后不应继续核对")

    monkeypatch.setattr(wab, "_post_json", _boom_post)
    monkeypatch.setattr(wab, "_get_json", _fake_get)
    with pytest.raises(RuntimeError):
        asyncio.run(_wa_worker().start())


# ── 6) messenger verified 观测位（回读二次确认）不改送达语义 ─────────────────

def test_worker_send_verified_false_still_delivered(monkeypatch):
    """回读没锚到气泡（不定态）→ 仍按已送达（防重发刷屏），仅观测。"""
    import src.integrations.messenger_web_login as mgw
    from src.integrations.account_orchestrator import MessengerWebWorker

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "sent": True, "verified": False}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    res = asyncio.run(
        MessengerWebWorker({"account_id": "100"}, {}).send("555", "hi"))
    assert res["delivered"] is True
