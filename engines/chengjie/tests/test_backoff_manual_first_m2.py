# -*- coding: utf-8 -*-
"""M-2 C（#233）退避锁手动优先契约。

K9CY6R 实录：边车 ``send_backoff`` 熔断后 AutosendWorker 按 13s/23s 改期重投 → 一到点就撞
仍在退避的边车（或再 500）→ 边车 streak 续命 → 坐席手动发送也吃 429。本文件钉住：

- 改期到点的重投先过账号门禁：退避水位未过 → 再排、不重投、不计失败（``total_retry_gated``）；
- 真实成功 / 登录成功（authorized 转移）→ 退避解锁，重投才放行；
- 人工路径在同一 await 链内带 ``manual`` 标：``send_via_adapters(origin="manual")`` →
  边车载荷 ``manual: true``；自动链不带。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.store import InboxStore


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    import src.integrations.platform_session_health as psh
    from src.integrations.shared import event_bus as eb
    import src.integrations.account_registry as ar
    import src.inbox.account_channel_gate as gate
    import src.integrations.account_orchestrator as ao
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    monkeypatch.setattr(psh, "_SEEDED", False, raising=False)
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    monkeypatch.setattr(ar, "_registry", None, raising=False)
    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: None, raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    gate._reset_for_tests()
    yield
    gate._reset_for_tests()


PLAT, ACCT = "messenger", "6158"


class _Svc:
    def __init__(self, store):
        self.queue = []
        self._store = store

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        return {"ok": True}


def _item(n):
    return {"draft_id": f"d{n}", "autopilot_level": "L2", "final_text": f"t{n}",
            "platform": PLAT, "account_id": ACCT, "chat_key": f"c{n}",
            "conversation_id": f"{PLAT}:{ACCT}:c{n}", "source_id": f"{PLAT}:{ACCT}:c{n}"}


@pytest.mark.asyncio
async def test_deferred_retry_waits_for_backoff_not_hammering(tmp_path, monkeypatch):
    """边车 429 send_backoff → 改期项到点时账号仍在退避 → 再排不重投；水位清了才投。"""
    from src.inbox.account_channel_gate import backoff_remaining, reset_backoff
    store = InboxStore(tmp_path / "bo.db")
    svc = _Svc(store)
    calls = []
    state = {"backoff": True}

    async def _cb(platform, account_id, chat_key, text):
        calls.append(text)
        if state["backoff"]:
            return {"ok": False, "delivered": False, "error": "429 send_backoff",
                    "error_kind": "send_backoff", "retry_after_ms": 13000}
        return {"ok": True, "delivered": True}

    async def _sleep(d):
        return None

    w = AutosendWorker(draft_service=svc, send_callback=_cb, sleep=_sleep)
    svc.queue = [_item(1)]
    await w._tick()
    assert calls == ["t1"]
    assert w.total_deferred == 1 and len(w._retry_queue) == 1
    assert backoff_remaining(PLAT, ACCT) > 0
    # 让改期项「到点」但账号退避水位（13s）未过 → 不重投、再排
    w._retry_queue[0]["next_ts"] = time.time() - 1
    await w._tick()
    assert calls == ["t1"]                 # 没有第二次撞边车
    assert w.total_retry_gated == 1 and len(w._retry_queue) == 1
    assert w._retry_queue[0]["next_ts"] > time.time()
    # 登录成功 / 健康探测通过 → 退避解锁（#233「登录成功即解锁」）→ 到点重投放行
    assert reset_backoff(PLAT, ACCT, why="test") is True
    state["backoff"] = False
    w._retry_queue[0]["next_ts"] = time.time() - 1
    await w._tick()
    assert calls == ["t1", "t1"] and len(w._retry_queue) == 0
    assert w.status_snapshot()["total_retry_gated"] == 1


@pytest.mark.asyncio
async def test_authorized_transition_unlocks_backoff():
    from src.inbox.account_channel_gate import backoff_remaining, note_send_fail
    from src.integrations.platform_session_health import report_session_transition
    note_send_fail(PLAT, ACCT, error_kind="send_backoff", retry_after_ms=60000)
    assert backoff_remaining(PLAT, ACCT) > 0
    report_session_transition(PLAT, ACCT, "authorized", login_id="msg_x")
    assert backoff_remaining(PLAT, ACCT) == 0


def test_manual_flag_reaches_sidecar_payload_only_for_manual(monkeypatch):
    """人工路径 manual=true 进边车载荷；自动链不带（手动独立于自动退避锁）。"""
    from src.inbox.channel_adapters import MessengerInboxAdapter, send_via_adapters
    import src.integrations.messenger_web_login as mwl
    seen = []

    async def _fake_post(url, payload, **kw):
        seen.append(dict(payload))
        return {"ok": True, "delivered": True, "message_id": "m1"}

    monkeypatch.setattr(mwl, "_post_json", _fake_post)
    monkeypatch.setattr(mwl, "service_base_url", lambda cfg: "http://127.0.0.1:8791")
    monkeypatch.setattr(mwl, "web_enabled", lambda cfg: True)
    monkeypatch.setattr(MessengerInboxAdapter, "_is_web_mode", staticmethod(lambda aid: True))
    monkeypatch.setattr(MessengerInboxAdapter, "_config", staticmethod(lambda req: {}))
    import src.inbox.channel_adapters as ca
    monkeypatch.setattr(ca, "_writeback_outbound", lambda *a, **k: None)

    class _Req:
        class app:
            class state:
                messenger_rpa_service = None
                config_manager = None

    adapters = [MessengerInboxAdapter()]
    asyncio.run(send_via_adapters(_Req(), PLAT, ACCT, "jid1", "hi", adapters, origin="manual"))
    asyncio.run(send_via_adapters(_Req(), PLAT, ACCT, "jid1", "hi", adapters, origin="auto"))
    assert seen[0].get("manual") is True
    assert "manual" not in seen[1]


def test_worker_send_manual_flag(monkeypatch):
    """编排器 MessengerWebWorker.send 同样只在人工上下文带 manual。"""
    from src.inbox.send_context import manual_send_scope
    from src.integrations.account_orchestrator import MessengerWebWorker
    import src.integrations.messenger_web_login as mwl
    seen = []

    async def _fake_post(url, payload, **kw):
        seen.append(dict(payload))
        return {"ok": True, "delivered": True}

    monkeypatch.setattr(mwl, "_post_json", _fake_post)
    monkeypatch.setattr(mwl, "service_base_url", lambda cfg: "http://127.0.0.1:8791")
    w = MessengerWebWorker({"account_id": ACCT}, {})

    async def _run():
        with manual_send_scope(True):
            await w.send("jid1", "hi")
        await w.send("jid1", "hi")

    asyncio.run(_run())
    assert seen[0].get("manual") is True and "manual" not in seen[1]
