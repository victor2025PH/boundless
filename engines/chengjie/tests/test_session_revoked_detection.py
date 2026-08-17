"""Telegram「手机端退出」检测闭环契约（2026-08-14）。

事故：用户在手机官方客户端点「退出登录」→ 该 session 被吊销，本系统这边：
- A 线 ``TelegramClient.start`` 把 SESSION_REVOKED 吞成一行日志（不 raise）；
- companion worker 只看到 running=False，编排器只拿到泛化 "unhealthy"；
- 注册表停留 online、健康表零事件 → 坐席账号抽屉**毫无提示**，只有点「同步记录」
  时 sync 链路才偶然报出来。

修复链（本文件钉住每一环）：
1. ``TelegramClient.start`` 失败时把原始异常存 ``last_start_error``；
2. ``TelegramCompanionWorker.start`` 复核 running，未起来即带根因 raise；
3. 编排器 ``_start_account`` 失败经 ``tg_error_kind`` 分类，``session_revoked``
   → ``report_session_transition("logged_out")``；成功（telegram）→ authorized；
4. ``report_session_transition``：健康表 record + 注册表 online→offline（authorized
   反向归位）+ ``platform_session_alert`` 告警——坐席账号抽屉离线提示/ops 卡/
   watchdog 升级提醒全部同源点亮。
"""

from __future__ import annotations

import pytest

from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator
from src.integrations.account_registry import AccountRegistry


@pytest.fixture(autouse=True)
def _fresh_singletons(monkeypatch, tmp_path):
    """每例独立的健康单例 / EventBus / 全局注册表。"""
    import src.integrations.account_registry as ar
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    monkeypatch.setattr(
        ar, "_registry",
        AccountRegistry(tmp_path / "_revoked_test_registry.db"),
        raising=False)
    yield


def _registry():
    from src.integrations.account_registry import get_account_registry
    return get_account_registry()


def _health():
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    return get_platform_session_health()


def _alerts():
    from src.integrations.shared.event_bus import get_event_bus
    return [e for e in get_event_bus().recent_events(50)
            if e["type"] == "platform_session_alert"]


# ── report_session_transition（库入口，与 /session-status 路由同语义） ──────


def test_logged_out_flips_registry_offline_and_alerts():
    from src.integrations.platform_session_health import (
        report_session_transition,
    )
    _registry().upsert("telegram", "800", mode="protocol", status="online")
    trans = report_session_transition(
        "telegram", "800", "logged_out", detail="[401 SESSION_REVOKED] ...")
    assert trans["went_unhealthy"] is True
    assert _health().is_unhealthy("telegram", "800") is True
    row = _registry().get("telegram", "800")
    assert row["status"] == "offline"      # 持久化：重启后种子接续真相
    evs = _alerts()
    assert len(evs) == 1
    assert evs[0]["data"]["status"] == "logged_out"
    assert evs[0]["data"]["recovered"] is False
    assert evs[0]["data"]["rate_key"] == "telegram:800"


def test_authorized_restores_registry_and_sends_recovery():
    from src.integrations.platform_session_health import (
        report_session_transition,
    )
    _registry().upsert("telegram", "800", mode="protocol", status="online")
    report_session_transition("telegram", "800", "logged_out")
    trans = report_session_transition(
        "telegram", "800", "authorized", detail="re-login ok")
    assert trans["recovered"] is True
    assert _health().is_unhealthy("telegram", "800") is False
    assert _registry().get("telegram", "800")["status"] == "online"
    evs = _alerts()
    assert len(evs) == 2 and evs[-1]["data"]["recovered"] is True


def test_failed_is_selfhealable_and_does_not_flip_registry():
    """``failed``＝可自愈故障：健康表如实记，但注册表不落 offline
    （标「已退出」会误导运营去手动重登）。"""
    from src.integrations.platform_session_health import (
        report_session_transition,
    )
    _registry().upsert("telegram", "800", mode="protocol", status="online")
    report_session_transition("telegram", "800", "failed", detail="crash loop")
    assert _registry().get("telegram", "800")["status"] == "online"
    assert _health().is_unhealthy("telegram", "800") is True


def test_authorized_never_resurrects_removed():
    from src.integrations.platform_session_health import (
        report_session_transition,
    )
    _registry().upsert("telegram", "801", mode="protocol", status="removed")
    report_session_transition("telegram", "801", "authorized")
    assert _registry().get("telegram", "801")["status"] == "removed"


# ── 编排器接线：启动失败分类上报 / 启动成功回报 authorized ──────────────────


class _RevokedWorker:
    def __init__(self, account, config):
        pass

    async def start(self):
        raise RuntimeError(
            "A 线 TelegramClient 启动失败：[401 SESSION_REVOKED] - "
            "The session is revoked (caused by \"InvokeWithTakeout\")")

    async def stop(self):
        pass


class _BoomWorker:
    def __init__(self, account, config):
        pass

    async def start(self):
        raise RuntimeError("connection timeout")

    async def stop(self):
        pass


class _OkWorker:
    def __init__(self, account, config):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def healthy(self):
        return True


@pytest.fixture()
def _factory_slot():
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    yield
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def test_orchestrator_reports_logged_out_on_session_revoked(_factory_slot):
    reg = _registry()
    reg.upsert("telegram", "900", mode="protocol", status="online")
    orch.register_worker("telegram", "protocol",
                         lambda a, c: _RevokedWorker(a, c))
    o = AccountOrchestrator(registry=reg)
    await o.sync()
    # 分类命中 session_revoked → 注册表落 offline + 健康表 logged_out + 告警
    assert reg.get("telegram", "900")["status"] == "offline"
    assert _health().is_unhealthy("telegram", "900") is True
    evs = _alerts()
    assert len(evs) == 1 and evs[0]["data"]["status"] == "logged_out"
    # 下一轮 sync：offline 已出期望集 → 停止空转重试
    await o.sync()
    assert not o.status()["accounts"] or all(
        a["key"] != "telegram:900" or a["state"] == "stopped"
        for a in o.status()["accounts"])


async def test_orchestrator_generic_failure_does_not_mark_logged_out(_factory_slot):
    """普通启动失败（网络抖动等）绝不误标「已退出」——那会让运营白跑重登。"""
    reg = _registry()
    reg.upsert("telegram", "901", mode="protocol", status="online")
    orch.register_worker("telegram", "protocol", lambda a, c: _BoomWorker(a, c))
    o = AccountOrchestrator(registry=reg)
    await o.sync()
    assert reg.get("telegram", "901")["status"] == "online"
    assert _health().is_unhealthy("telegram", "901") is False
    assert _alerts() == []


async def test_orchestrator_success_reports_authorized_and_recovers(_factory_slot):
    """重扫成功 → 登录路由已把注册表翻回 online → 编排器拉起成功即回报
    authorized：健康表恢复 + 发恢复通知，坐席离线提示自动消失。"""
    from src.integrations.platform_session_health import (
        report_session_transition,
    )
    reg = _registry()
    reg.upsert("telegram", "902", mode="protocol", status="online")
    report_session_transition("telegram", "902", "logged_out")   # 先前被吊销
    assert reg.get("telegram", "902")["status"] == "offline"
    reg.upsert("telegram", "902", status="online")               # 重扫落库
    orch.register_worker("telegram", "protocol", lambda a, c: _OkWorker(a, c))
    o = AccountOrchestrator(registry=reg)
    await o.sync()
    assert _health().is_unhealthy("telegram", "902") is False
    evs = _alerts()
    assert evs and evs[-1]["data"]["recovered"] is True


# ── companion worker：start 后复核 running，带根因 raise ────────────────────


async def test_companion_worker_raises_with_root_cause(monkeypatch):
    from src.integrations import telegram_companion_worker as tcw

    class _SwallowingClient:
        """模拟 A 线 TelegramClient：start 吞异常只留 last_start_error。"""
        running = False
        last_start_error = "[401 SESSION_REVOKED] - The session is revoked"

        def __init__(self, **kwargs):
            pass

        async def initialize(self):
            return True

        async def start(self, block=True):
            return None

        async def stop(self):
            pass

    import src.client.telegram_client as tc
    monkeypatch.setattr(tc, "TelegramClient", _SwallowingClient)
    monkeypatch.setattr(
        tcw, "get_companion_context",
        lambda: {"config_manager": object(), "skill_manager": object(),
                 "ai_client": None})
    w = tcw.TelegramCompanionWorker(
        {"account_id": "903", "meta": {"session_name": "s903"}}, {})
    monkeypatch.setattr(w, "_account_cfg", lambda: {})

    async def _no_overlay():
        return {}
    monkeypatch.setattr(w, "_isolation_overlay", _no_overlay)
    with pytest.raises(RuntimeError) as ei:
        await w.start()
    assert "SESSION_REVOKED" in str(ei.value)
    assert w.client is None


# ── 分类器：编排器包装文案仍可被识别 ────────────────────────────────────────


def test_error_kind_matches_wrapped_worker_message():
    from src.integrations.protocol_bridge import tg_error_kind
    assert tg_error_kind(
        "A 线 TelegramClient 启动失败：[401 SESSION_REVOKED] - ...",
    ) == "session_revoked"
    assert tg_error_kind("connection timeout") == ""
