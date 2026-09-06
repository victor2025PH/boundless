"""M-2 A（#232）通道真相契约：发送 ✓ 以回执为准 / 通道状态单一出口 / 未连接禁投递。

1.0.75 首日实录（CW6RP4 / ZGKVQB）：Messenger 边车重启 ``restored 0``，界面「已登录」
两小时、收得到发不出、每条 500 静默；手动发表情界面打 ✓ 客户没收到。本文件钉住：

- ``channel_connection_state``：注册表 offline / 健康表不健康 / 编排器 worker 放弃 →
  disconnected；worker error 退避中 → reconnecting；running → connected；无登记 → unknown；
- ``send_via_adapters``：disconnected 时**自动与手动同一闸** 503 + reason_code；
- RPA 入队不再自称 delivered=True（入队 ≠ 回执）；
- ``MessengerWebWorker.healthy()``：边车列出该账号 → 上报 authorized（带 login_id）；
  边车在线但没有该账号会话、超过宽限 → 上报 needs_login；边车不可达 → 不上报。
"""
from __future__ import annotations

import asyncio
import time

import pytest


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    import src.integrations.account_registry as ar
    import src.inbox.account_channel_gate as gate
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    monkeypatch.setattr(ar, "_registry", None, raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    gate._reset_for_tests()
    yield
    gate._reset_for_tests()


class _FakeOrch:
    def __init__(self, state=None):
        self._state = state

    def managed_state(self, platform, account_id):
        return self._state

    def owns(self, platform, account_id):
        return False


def _patch_orch(monkeypatch, state):
    import src.integrations.account_orchestrator as ao
    monkeypatch.setattr(ao, "get_orchestrator_if_running",
                        lambda: _FakeOrch(state), raising=False)


# ── channel_connection_state ────────────────────────────────────────────────

def test_state_unknown_when_nothing_known(monkeypatch):
    from src.integrations.platform_session_health import channel_connection_state
    _patch_orch(monkeypatch, None)
    cs = channel_connection_state("messenger", "6158")
    assert cs["state"] == "unknown"


def test_state_disconnected_when_health_unhealthy(monkeypatch):
    from src.integrations.platform_session_health import (
        channel_connection_state, channel_send_block_reason,
        get_platform_session_health,
    )
    _patch_orch(monkeypatch, {"state": "running", "restarts": 0, "gave_up": False})
    get_platform_session_health().record("messenger", "6158", "needs_login",
                                         detail="sidecar has no session")
    cs = channel_connection_state("messenger", "6158")
    assert cs["state"] == "disconnected" and cs["reason"] == "needs_login"
    assert channel_send_block_reason("messenger", "6158")["reason"] == "channel_disconnected"


def test_state_reconnecting_then_connected(monkeypatch):
    from src.integrations.platform_session_health import (
        channel_connection_state, channel_send_block_reason,
    )
    _patch_orch(monkeypatch, {"state": "error", "restarts": 2, "gave_up": False,
                              "last_error": "unhealthy", "updated_at": time.time()})
    cs = channel_connection_state("messenger", "6158")
    assert cs["state"] == "reconnecting"
    # 重连中不拦（发出去会诚实 5xx 留痕，比一律拒发更少误伤）
    assert channel_send_block_reason("messenger", "6158")["reason"] == ""
    _patch_orch(monkeypatch, {"state": "running", "restarts": 0, "gave_up": False,
                              "updated_at": time.time()})
    assert channel_connection_state("messenger", "6158")["state"] == "connected"


def test_state_disconnected_when_worker_gave_up(monkeypatch):
    from src.integrations.platform_session_health import channel_connection_state
    _patch_orch(monkeypatch, {"state": "error", "restarts": 99, "gave_up": True,
                              "last_error": "unhealthy", "updated_at": time.time()})
    assert channel_connection_state("messenger", "6158")["state"] == "disconnected"


def test_state_disconnected_when_registry_offline(monkeypatch, tmp_path):
    from src.integrations.account_registry import get_account_registry
    from src.integrations.platform_session_health import channel_connection_state
    reg = get_account_registry(tmp_path / "reg.db")
    reg.upsert("messenger", "6158", status="offline",
               meta={"offline_reason": "worker:needs_login"}, merge_meta=True)
    _patch_orch(monkeypatch, None)
    cs = channel_connection_state("messenger", "6158")
    assert cs["state"] == "disconnected" and cs["reason"] == "registry:offline"


# ── 发送闸：自动与手动同一判定 ─────────────────────────────────────────────

def test_send_via_adapters_blocks_when_disconnected(monkeypatch):
    from src.inbox.channel_adapters import ChannelSendError, send_via_adapters
    from src.integrations.platform_session_health import get_platform_session_health
    _patch_orch(monkeypatch, None)
    get_platform_session_health().record("messenger", "6158", "needs_login")

    class _Adapter:
        platform = "messenger"

        async def send(self, request, account_id, chat_key, text):
            raise AssertionError("must not reach adapter when channel disconnected")

    for origin in ("auto", "manual"):
        with pytest.raises(ChannelSendError) as ei:
            asyncio.run(send_via_adapters(
                None, "messenger", "6158", "jid1", "hi", [_Adapter()], origin=origin))
        assert ei.value.status_code == 503
        assert ei.value.reason_code == "channel_disconnected"


def test_send_via_adapters_passes_when_unknown(monkeypatch):
    from src.inbox.channel_adapters import send_via_adapters
    _patch_orch(monkeypatch, None)

    class _Adapter:
        platform = "messenger"

        async def send(self, request, account_id, chat_key, text):
            return {"delivered": True, "message_id": "m1"}

    res = asyncio.run(send_via_adapters(
        None, "messenger", "6158", "jid1", "hi", [_Adapter()]))
    assert res["delivered"] is True


def test_rpa_queue_is_not_a_receipt():
    from src.inbox.channel_adapters import _send_via_rpa_queue

    class _Svc:
        def enqueue_send(self, chat_key, peer_name, text):
            return 7

    class _Req:
        class app:
            class state:
                inbox_store = None

    res = asyncio.run(_send_via_rpa_queue(
        _Svc(), "line", "acct", "U1", "hi", request=_Req()))
    assert res["queued"] is True and res["delivered"] is None


# ── 编排器探测 → 健康表 ─────────────────────────────────────────────────────

def _worker(monkeypatch, accounts_payload, *, unreachable=False):
    from src.integrations.account_orchestrator import MessengerWebWorker
    import src.integrations.messenger_web_login as mwl

    async def _fake_get_json(url, **kw):
        if unreachable:
            raise RuntimeError("connect refused")
        return accounts_payload

    monkeypatch.setattr(mwl, "_get_json", _fake_get_json)
    monkeypatch.setattr(mwl, "service_base_url", lambda cfg: "http://127.0.0.1:8791")
    return MessengerWebWorker({"account_id": "6158"}, {})


def test_healthy_probe_reports_authorized_with_login_id(monkeypatch):
    from src.integrations.platform_session_health import get_platform_session_health
    w = _worker(monkeypatch, {"accounts": [
        {"account_id": "6158", "login_id": "msg_abc", "logged_in": True}]})
    assert asyncio.run(w.healthy()) is True
    sess = get_platform_session_health().session_status("messenger", "6158")
    assert sess.get("status") == "authorized" and sess.get("login_id") == "msg_abc"


def test_healthy_probe_absent_reports_needs_login_after_grace(monkeypatch):
    from src.integrations.platform_session_health import (
        channel_connection_state, get_platform_session_health,
    )
    w = _worker(monkeypatch, {"accounts": [], "worker": {"restoring": 0}})
    # 首次未列出：宽限期内不上报（开机 restore 还在拉浏览器）
    assert asyncio.run(w.healthy()) is False
    assert get_platform_session_health().session_status("messenger", "6158") == {}
    # 宽限过后仍未列出 → needs_login → 通道真相 disconnected（账号栏「需重新登录」）
    w._absent_since = time.time() - w.ABSENT_GRACE_SEC - 1
    assert asyncio.run(w.healthy()) is False
    sess = get_platform_session_health().session_status("messenger", "6158")
    assert sess.get("status") == "needs_login"
    _patch_orch(monkeypatch, None)
    assert channel_connection_state("messenger", "6158")["state"] == "disconnected"


def test_healthy_probe_restoring_extends_grace(monkeypatch):
    from src.integrations.platform_session_health import get_platform_session_health
    w = _worker(monkeypatch, {"accounts": [], "worker": {"restoring": 1}})
    w._absent_since = time.time() - w.ABSENT_GRACE_SEC - 1
    assert asyncio.run(w.healthy()) is False
    assert get_platform_session_health().session_status("messenger", "6158") == {}


def test_healthy_probe_unreachable_does_not_report(monkeypatch):
    from src.integrations.platform_session_health import get_platform_session_health
    w = _worker(monkeypatch, {}, unreachable=True)
    assert asyncio.run(w.healthy()) is False
    assert get_platform_session_health().session_status("messenger", "6158") == {}
    assert "unreachable" in w.detail


def test_healthy_probe_logged_in_false_reports_needs_login(monkeypatch):
    from src.integrations.platform_session_health import get_platform_session_health
    w = _worker(monkeypatch, {"accounts": [
        {"account_id": "6158", "login_id": "msg_abc", "logged_in": False}]})
    assert asyncio.run(w.healthy()) is False
    assert get_platform_session_health().session_status(
        "messenger", "6158").get("status") == "needs_login"
