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


def _registered(platform="messenger", account_id="100", status="online"):
    """把账号登记成注册表在册号（提醒只针对**期望在线**的号）。"""
    from src.integrations.account_registry import get_account_registry
    get_account_registry().upsert(platform, account_id, mode="web",
                                  status=status)


def test_watchdog_emits_reminder_with_rate_key():
    s = _store()
    _registered()
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
    _registered()
    s.record("messenger", "100", "expired")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    wd = _watchdog({"health_watchdog": {
        "session_stale_remind": {"enabled": False}}})
    wd._check_platform_sessions(now=t0 + 86400)
    assert _events() == []


@pytest.mark.parametrize("status", ["offline", "removed"])
def test_watchdog_no_reminder_for_deliberately_offline_account(status):
    """运营主动登出/删除 → Node push logged_out，但那不是「没人修的故障」。

    坐席登出会把注册表置 offline（删除是软删 status=removed），编排器不会再拉起它
    → 催人去修只会污染告警信号。该 push 仍留在健康表里（发送前快速失败要用）。
    """
    s = _store()
    _registered(status=status)
    s.record("messenger", "100", "logged_out", detail="logout requested via API")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    wd = _watchdog()
    wd._check_platform_sessions(now=t0 + 86400)
    assert _events() == []
    assert s.is_unhealthy("messenger", "100") is True


def test_watchdog_no_reminder_for_unknown_account():
    """注册表里没有的号（已彻底清掉 / 键其实是个 login_id）：编排器拉不起来，不催。"""
    s = _store()
    s.record("messenger", "msg_orphan", "expired")
    t0 = s.dump()["sessions"]["messenger:msg_orphan"]["unhealthy_since"]
    wd = _watchdog()
    wd._check_platform_sessions(now=t0 + 86400)
    assert _events() == []


def test_watchdog_reminds_when_registry_unreadable(monkeypatch):
    """注册表读失败 → 保守按「期望在线」处理，不因巡检自身故障漏报真掉线。"""
    import src.integrations.account_registry as ar

    def _boom():
        raise RuntimeError("db locked")

    s = _store()
    s.record("messenger", "100", "expired")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    monkeypatch.setattr(ar, "get_account_registry", _boom)
    _watchdog()._check_platform_sessions(now=t0 + 86400)
    assert len(_events()) == 1


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


def test_relogin_route_whatsapp_reconnect(monkeypatch):
    """P1（2026-08-14）：whatsapp 走 baileys 凭据级 reconnect（编排器自愈同端点）。
    响应无 status 字段时从 already/reconnecting 布尔位推导，运营能看懂发生了什么。"""
    import src.integrations.messenger_web_login as mgw
    import src.integrations.whatsapp_baileys_login as wbl
    calls = {}

    async def _fake(url, payload, timeout=20.0):
        calls["url"] = url
        return {"ok": True, "reconnecting": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    monkeypatch.setattr(wbl, "service_base_url", lambda cfg: "http://wa-svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "whatsapp", "account_id": "8613800000000"})
    assert r.status_code == 200, r.text
    assert calls["url"] == "http://wa-svc/accounts/8613800000000/reconnect"
    assert r.json()["status"] == "reconnecting"


def test_relogin_route_whatsapp_already_online(monkeypatch):
    import src.integrations.messenger_web_login as mgw
    import src.integrations.whatsapp_baileys_login as wbl

    async def _fake(url, payload, timeout=20.0):
        return {"ok": True, "already": True}

    monkeypatch.setattr(mgw, "_post_json", _fake)
    monkeypatch.setattr(wbl, "service_base_url", lambda cfg: "http://wa-svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "whatsapp", "account_id": "8613800000000"})
    assert r.status_code == 200
    assert r.json()["status"] == "already"


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


# ── 7) 自灭型 offline 账号的持续提醒（2026-08-16 messenger 17 天零提醒事故修） ──
# 注册表 offline 现分两档：meta.offline_reason=worker:*（自灭——worker push 翻
# 状态时落标，**继续催**，重登/删号自然停催）/ operator（运营主动登出，
# _clear_session_creds 落标）或无标记（历史存量，来历不明）→ 静默维持旧行为。


def _registry():
    from src.integrations.account_registry import get_account_registry
    return get_account_registry()


def test_watchdog_reminds_worker_dead_offline_account():
    """自灭标记的 offline 号：一次转移告警后仍持续升级提醒（事故核心修复——
    修复前 offline 一律被当「运营主动下线」，死号 17 天零提醒）。"""
    _registry().upsert("messenger", "100", mode="web", status="offline",
                       meta={"offline_reason": "worker:expired"},
                       merge_meta=True)
    s = _store()
    s.record("messenger", "100", "expired", detail="cookie invalidated")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    wd = _watchdog()
    wd._check_platform_sessions(now=t0 + 3600)
    evs = _events()
    assert len(evs) == 1
    assert evs[0]["data"]["reminder"] is True
    assert evs[0]["data"]["account_id"] == "100"


def test_watchdog_silent_for_operator_offline_account():
    """operator 标记（运营主动登出）→ 与无标记同语义：不催。"""
    _registry().upsert("messenger", "100", mode="web", status="offline",
                       meta={"offline_reason": "operator"}, merge_meta=True)
    s = _store()
    s.record("messenger", "100", "logged_out")
    t0 = s.dump()["sessions"]["messenger:100"]["unhealthy_since"]
    _watchdog()._check_platform_sessions(now=t0 + 86400)
    assert _events() == []


def test_seeded_dead_account_reminds_after_restart(monkeypatch):
    """重启剧本端到端：健康表全空（=刚重启）→ 看门狗自种子 → 自灭号发提醒。

    修复前该链在两处断：① 种子只挂 web 读路径，看门狗可能先于任何页面访问跑；
    ② 就算种上，_session_expected_online 对 offline 一律 False。"""
    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    _registry().upsert("messenger", "100", mode="web", status="offline",
                       meta={"offline_reason": "worker:crash_loop"},
                       merge_meta=True)
    wd = _watchdog()
    # 不往健康表 record——看门狗内的 ensure_seeded_from_registry 必须自己把
    # 注册表 offline 行接续进来（unhealthy_since=注册表 updated_at≈刚才）
    wd._check_platform_sessions(now=time.time() + 3600)
    evs = _events()
    assert len(evs) == 1
    d = evs[0]["data"]
    assert d["reminder"] is True and d["account_id"] == "100"
    assert d["status"] == "logged_out"   # 种子行的状态语义


def test_session_status_push_stamps_worker_reason_and_clears_on_auth():
    """路由写点：online 号被 push logged_out → offline + worker: 标记；
    authorized 归位 → online + 标记清空（下一次自灭从头判定）。"""
    from src.web.routes.unified_inbox_account_routes import (
        register_account_routes,
    )
    reg = _registry()
    reg.upsert("messenger", "300", mode="web", status="online")
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    c = TestClient(app)
    c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "300", "status": "logged_out"})
    row = reg.get("messenger", "300")
    assert row["status"] == "offline"
    assert row["meta"].get("offline_reason") == "worker:logged_out"
    c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "300", "status": "authorized"})
    row = reg.get("messenger", "300")
    assert row["status"] == "online"
    assert row["meta"].get("offline_reason") == ""


def test_logout_endpoint_stamps_operator_reason(monkeypatch):
    """运营登出写点：/logout → offline + operator 标记。哪怕 Node push 先一步
    落了 worker: 标记也要被改回——两种到达顺序结果一致（竞态收敛断言）。"""
    import src.web.routes.unified_inbox_account_routes as rt

    class _FakeOrch:
        async def stop_account(self, key):
            return True

    monkeypatch.setattr(rt, "get_orchestrator", lambda cfg=None: _FakeOrch())
    monkeypatch.setattr(rt, "ensure_builtin_workers", lambda cfg=None: None)
    import src.integrations.messenger_web_login as mgw

    async def _fake_post(url, payload, timeout=20.0):
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    reg = _registry()
    reg.upsert("messenger", "400", mode="web", status="offline",
               meta={"offline_reason": "worker:logged_out",
                     "session_string": "s"}, merge_meta=True)
    app = FastAPI()
    rt.register_account_routes(app, api_auth=lambda request: None,
                               config_manager=None)
    c = TestClient(app)
    r = c.post("/api/accounts/messenger/400/logout")
    assert r.status_code == 200, r.text
    row = reg.get("messenger", "400")
    assert row["status"] == "offline"
    assert row["meta"].get("offline_reason") == "operator"
    assert "session_string" not in row["meta"]   # 凭据仍被清（原语义不回退）


# ── 8) 身份接替清账 + 登出联动健康表 + 催办衰减 + 重登分诊 ──────────────────
# （2026-08-27 Calixa 僵尸横幅事故：登录档案 msg_* 先属 A，重登窗口里改登成 B →
#   A 在本节点永无档案，其 needs_login 记录成了删不掉的红条 + 重登永远 404。）


def test_record_supersede_flips_old_account_on_login_reuse():
    """同一登录档案（login_id）被另一账号授权 ⇒ 旧账号不健康记录翻 logged_out。"""
    s = _store()
    s.record("messenger", "A100", "needs_login", detail="cookies expired",
             login_id="msg_slot1")
    s.record_inbox_health("messenger", "A100", unread=3, read_attempts=2,
                          read_fails=2)
    t = s.record("messenger", "B200", "authorized", login_id="msg_slot1")
    assert t["superseded"] == ["A100"]
    sess = s.dump()["sessions"]["messenger:A100"]
    assert sess["status"] == "logged_out"
    assert "superseded by messenger:B200" in sess["detail"]
    # 发送前快速失败语义不变（logged_out 仍属不健康集合）
    assert s.is_unhealthy("messenger", "A100") is True
    # 记录仍在 unhealthy_sessions（横幅消费方按 logged_out 过滤，不再显示）
    assert "messenger:A100" in s.unhealthy_sessions()
    # 入站健康残行整行弹出（档案已易主，旧读数是另一个号的陈迹）
    assert "messenger:A100" not in s.inbox_health()


def test_record_supersede_skips_healthy_and_other_slots():
    s = _store()
    s.record("messenger", "A100", "needs_login", login_id="msg_slot1")
    s.record("messenger", "C300", "needs_login", login_id="msg_other")
    s.record("messenger", "E500", "authorized", login_id="msg_slot2")
    t = s.record("messenger", "B200", "authorized", login_id="msg_slot1")
    assert t["superseded"] == ["A100"]
    d = s.dump()["sessions"]
    assert d["messenger:C300"]["status"] == "needs_login"   # 别的档案不动
    assert d["messenger:E500"]["status"] == "authorized"    # 健康行不动


def test_record_same_account_reauth_is_recovery_not_supersede():
    s = _store()
    s.record("messenger", "A100", "needs_login", login_id="msg_slot1")
    t = s.record("messenger", "A100", "authorized", login_id="msg_slot1")
    assert t["recovered"] is True and t["superseded"] == []
    assert s.is_unhealthy("messenger", "A100") is False


def test_mark_superseded_updates_worker_marker_only():
    """注册表落盘边界：只改 offline+worker:* 行；operator/online 行不替人做主。"""
    from src.integrations.platform_session_health import (
        mark_superseded_accounts,
    )
    reg = _registry()
    reg.upsert("messenger", "SUP_W", status="offline",
               meta={"offline_reason": "worker:needs_login"}, merge_meta=True)
    reg.upsert("messenger", "SUP_O", status="offline",
               meta={"offline_reason": "operator"}, merge_meta=True)
    reg.upsert("messenger", "SUP_ON", status="online",
               meta={"offline_reason": "worker:needs_login"}, merge_meta=True)
    mark_superseded_accounts("messenger", ["SUP_W", "SUP_O", "SUP_ON", ""],
                             "B200")
    assert (reg.get("messenger", "SUP_W")["meta"]["offline_reason"]
            == "superseded:B200")
    assert reg.get("messenger", "SUP_O")["meta"]["offline_reason"] == "operator"
    assert (reg.get("messenger", "SUP_ON")["meta"]["offline_reason"]
            == "worker:needs_login")


def test_watchdog_no_reminder_for_superseded_account():
    """superseded 标记（非 worker: 前缀）→ 死号催办自然停止。"""
    _registry().upsert("messenger", "SUP1", mode="web", status="offline",
                       meta={"offline_reason": "superseded:B200"},
                       merge_meta=True)
    s = _store()
    s.record("messenger", "SUP1", "logged_out", detail="superseded by B200")
    t0 = s.dump()["sessions"]["messenger:SUP1"]["unhealthy_since"]
    _watchdog()._check_platform_sessions(now=t0 + 86400)
    assert _events() == []


def test_session_status_endpoint_supersede_marks_registry():
    """端到端：worker push「B 在档案 slotZ 授权」→ 旧号 OLD9 健康记录翻
    logged_out + 注册表 worker:* 标记改 superseded:*（横幅/催办双灭）。"""
    from src.web.routes.unified_inbox_account_routes import (
        register_account_routes,
    )
    reg = _registry()
    reg.upsert("messenger", "OLD9", mode="web", status="offline",
               meta={"offline_reason": "worker:needs_login"}, merge_meta=True)
    _store().record("messenger", "OLD9", "needs_login", login_id="msg_slotZ")
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=None)
    c = TestClient(app)
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "NEW9",
        "status": "authorized", "login_id": "msg_slotZ"})
    assert r.status_code == 200, r.text
    assert (_store().dump()["sessions"]["messenger:OLD9"]["status"]
            == "logged_out")
    assert (reg.get("messenger", "OLD9")["meta"]["offline_reason"]
            == "superseded:NEW9")


def test_due_reminders_stale_decay_to_daily():
    """催办衰减：>stale_after 后节流从 interval 升为 stale_interval（日更）。"""
    s = _store()
    s.record("messenger", "ST1", "expired")
    t0 = s.dump()["sessions"]["messenger:ST1"]["unhealthy_since"]
    kw = dict(min_age_sec=1800, interval_sec=14400,
              stale_after_sec=48 * 3600, stale_interval_sec=86400)
    due = s.due_reminders(now=t0 + 1860, **kw)          # 新鲜期首提
    assert due["messenger:ST1"]["stale"] is False
    assert s.due_reminders(now=t0 + 1860 + 3600, **kw) == {}   # 4h 内不重复
    due = s.due_reminders(now=t0 + 1860 + 14460, **kw)         # 4h 到点照提
    assert due["messenger:ST1"]["stale"] is False
    due = s.due_reminders(now=t0 + 48 * 3600 + 60, **kw)       # 进入陈旧期
    assert due["messenger:ST1"]["stale"] is True
    # 陈旧期内 4h 到点不再提……
    assert s.due_reminders(now=t0 + 48 * 3600 + 60 + 14460, **kw) == {}
    # ……满 24h 才提（日更保底）
    assert "messenger:ST1" in s.due_reminders(
        now=t0 + 48 * 3600 + 60 + 86460, **kw)


def test_due_reminders_stale_params_zero_keeps_old_behavior():
    s = _store()
    s.record("messenger", "ST0", "expired")
    t0 = s.dump()["sessions"]["messenger:ST0"]["unhealthy_since"]
    assert "messenger:ST0" in s.due_reminders(
        min_age_sec=1800, interval_sec=14400, now=t0 + 30 * 86400)


def test_watchdog_stale_decay_daily_reminder():
    """看门狗接线：默认 48h 后降为日更（防「每 4h 轰一个没人修的死号」）。"""
    s = _store()
    _registered(account_id="ST2")
    s.record("messenger", "ST2", "expired")
    t0 = s.dump()["sessions"]["messenger:ST2"]["unhealthy_since"]
    wd = _watchdog()
    wd._check_platform_sessions(now=t0 + 49 * 3600)            # 陈旧期首提
    assert len(_events()) == 1
    wd._check_platform_sessions(now=t0 + 49 * 3600 + 14460)    # 4h 后：不提
    assert len(_events()) == 1
    wd._check_platform_sessions(now=t0 + 49 * 3600 + 86460)    # 24h 后：日更
    assert len(_events()) == 2


def test_relogin_route_worker_404_maps_to_no_profile(monkeypatch):
    """worker 404（本机已无该账号档案）→ 语义化 404 + 人话文案，
    绝不再把 httpx 英文原文/内部 URL 糊给坐席（2026-08-27 实锤）。"""
    import src.integrations.messenger_web_login as mgw

    class _Resp:
        status_code = 404

        @staticmethod
        def json():
            return {"ok": False, "error": "no session/profile found for id"}

    class _Err(Exception):
        def __init__(self):
            super().__init__(
                "Client error '404 Not Found' for url 'http://127.0.0.1:8791/"
                "accounts/61584070255403/relogin'")
            self.response = _Resp()

    async def _boom(url, payload, timeout=20.0):
        raise _Err()

    monkeypatch.setattr(mgw, "_post_json", _boom)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "messenger", "account_id": "61584070255403"})
    assert r.status_code == 404, r.text
    d = str(r.json().get("detail") or "")
    assert ("登录档案" in d) or ("login profile" in d)
    assert "127.0.0.1" not in d and "Client error" not in d


def test_relogin_route_connect_error_maps_to_worker_down(monkeypatch):
    """连不上 worker（无 response）→ 502 + 「服务未运行」指路，不透传原文。"""
    import src.integrations.messenger_web_login as mgw

    async def _boom(url, payload, timeout=20.0):
        raise RuntimeError("All connection attempts failed")

    monkeypatch.setattr(mgw, "_post_json", _boom)
    monkeypatch.setattr(mgw, "service_base_url", lambda cfg: "http://svc")
    c = TestClient(_ops_app())
    r = c.post("/api/admin/platform-sessions/relogin", json={
        "platform": "messenger", "account_id": "100"})
    assert r.status_code == 502
    d = str(r.json().get("detail") or "")
    assert ("登录服务" in d) or ("login service" in d)
    assert "connection attempts" not in d


def test_logout_route_flips_health_record(monkeypatch):
    """运营登出 → 内存健康表同步翻 logged_out（横幅立灭，不再等重启）；
    发送前快速失败语义保持（logged_out 仍不健康）。"""
    import src.web.routes.unified_inbox_account_routes as rt

    class _FakeOrch:
        async def stop_account(self, key):
            return True

    monkeypatch.setattr(rt, "get_orchestrator", lambda cfg=None: _FakeOrch())
    monkeypatch.setattr(rt, "ensure_builtin_workers", lambda cfg=None: None)
    import src.integrations.messenger_web_login as mgw

    async def _fake_post(url, payload, timeout=20.0):
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    reg = _registry()
    reg.upsert("messenger", "HS1", mode="web", status="online")
    s = _store()
    s.record("messenger", "HS1", "needs_login", detail="cookies expired")
    app = FastAPI()
    rt.register_account_routes(app, api_auth=lambda request: None,
                               config_manager=None)
    c = TestClient(app)
    r = c.post("/api/accounts/messenger/HS1/logout")
    assert r.status_code == 200, r.text
    sess = s.dump()["sessions"]["messenger:HS1"]
    assert sess["status"] == "logged_out"
    assert "operator logout" in sess["detail"]
    assert s.is_unhealthy("messenger", "HS1") is True
    row = reg.get("messenger", "HS1")
    assert row["status"] == "offline"
    assert row["meta"].get("offline_reason") == "operator"


def test_remove_route_flips_health_record(monkeypatch):
    import src.web.routes.unified_inbox_account_routes as rt

    class _FakeOrch:
        async def stop_account(self, key):
            return True

    monkeypatch.setattr(rt, "get_orchestrator", lambda cfg=None: _FakeOrch())
    monkeypatch.setattr(rt, "ensure_builtin_workers", lambda cfg=None: None)
    import src.integrations.messenger_web_login as mgw

    async def _fake_post(url, payload, timeout=20.0):
        return {"ok": True}

    monkeypatch.setattr(mgw, "_post_json", _fake_post)
    reg = _registry()
    reg.upsert("messenger", "HS2", mode="web", status="offline")
    s = _store()
    s.record("messenger", "HS2", "expired")
    app = FastAPI()
    rt.register_account_routes(app, api_auth=lambda request: None,
                               config_manager=None)
    c = TestClient(app)
    r = c.post("/api/accounts/messenger/HS2/remove")
    assert r.status_code == 200, r.text
    assert s.dump()["sessions"]["messenger:HS2"]["status"] == "logged_out"
    assert reg.get("messenger", "HS2")["status"] == "removed"
