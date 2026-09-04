# -*- coding: utf-8 -*-
"""#180（J-3 B，2026-09-05）：LINE 号发语音/媒体 501 分因 + receiver 连败跨重启退避。

钧机 3PZ95W：同一 LINE 账号 12:06 ``send_media type=voice delivered=True`` 成功过两次，
00:48 却 501「该账号不支持从收件箱发送语音（需 protocol 多开且在线）」——能力存在，
只是 worker 那一刻不在 running；同日志 19:55–20:02 receiver 对 gw operation/receive
连败 6 次 → 放弃 → 编排器重启 → 立刻又连败……多轮循环，重启期能力全灰。

本文件钉：① ``media_capability`` 分因（no_worker / no_send_media / worker_not_running）；
② 两套 501 文案 + 返回体 reason/state；③ send-caps 带 caps_reason/worker_state；
④ receiver 放弃后**跨重启**退避（延后拉起、状态 reconnecting、稳跑清零、计数进
line_media_stats.receiver）。
"""
from __future__ import annotations

import asyncio
import pathlib
import threading
import time
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def _req(lang="zh"):
    return types.SimpleNamespace(state=types.SimpleNamespace(ui_lang=lang))


# ─────────────────── ① 编排器分因 ───────────────────

def _orch():
    from src.integrations.account_orchestrator import AccountOrchestrator
    return AccountOrchestrator(config={})


def _managed(orch, platform, acct, *, state, worker, restarts=0, backoff=0.0, last_error=""):
    from src.integrations.account_orchestrator import _Managed, account_key
    key = account_key(platform, acct)
    m = _Managed(key=key, platform=platform, account_id=acct, mode="protocol",
                 worker=worker, state=state, restarts=restarts, last_error=last_error)
    m.backoff_until = orch._now() + backoff
    orch._managed[key] = m
    return m


def test_capability_no_worker_when_unmanaged():
    cap = _orch().media_capability("line", "ghost")
    assert cap["owns"] is False and cap["reason"] == "no_worker" and cap["state"] == ""


def test_capability_worker_not_running_carries_state_and_backoff():
    orch = _orch()
    w = types.SimpleNamespace(send_media=lambda *a, **k: None)
    _managed(orch, "line", "U1", state="error", worker=w, restarts=3, backoff=42.0,
             last_error="receiver gave up")
    cap = orch.media_capability("line", "U1")
    assert cap["owns"] is False and cap["reason"] == "worker_not_running"
    assert cap["state"] == "error" and cap["restarts"] == 3
    assert 40 <= cap["backoff_sec"] <= 42 and "receiver" in cap["last_error"]
    assert orch.owns_media("line", "U1") is False   # 旧判据与分因一致


def test_capability_no_send_media_when_switch_off():
    orch = _orch()
    _managed(orch, "line", "U1", state="running", worker=types.SimpleNamespace())
    cap = orch.media_capability("line", "U1")
    assert cap["owns"] is False and cap["reason"] == "no_send_media" and cap["state"] == "running"


def test_capability_owns_when_running_with_send_media():
    orch = _orch()
    _managed(orch, "line", "U1", state="running",
             worker=types.SimpleNamespace(send_media=lambda *a, **k: None))
    cap = orch.media_capability("line", "U1")
    assert cap["owns"] is True and cap["reason"] == "" and orch.owns_media("line", "U1") is True


# ─────────────────── ② 501 两套文案 + 返回体 ───────────────────

def test_501_wording_splits_platform_vs_reconnecting():
    from src.web.routes.unified_inbox_send_routes import _media_unsupported_exc
    e1 = _media_unsupported_exc(_req(), {"reason": "no_worker", "state": ""}, platform="line", kind="voice")
    assert e1.status_code == 501
    d1 = e1.detail
    assert d1["code"] == "voice_unsupported" and d1["reason"] == "no_worker"
    assert "不支持" in d1["message"] and "多开" not in d1["message"], d1
    e2 = _media_unsupported_exc(_req(), {"reason": "worker_not_running", "state": "error",
                                         "backoff_sec": 37}, platform="line", kind="voice")
    d2 = e2.detail
    assert d2["reason"] == "worker_not_running" and d2["state"] == "error"
    assert d2["backoff_sec"] == 37 and d2["platform"] == "line"
    assert "LINE" in d2["message"] and "重连" in d2["message"] and "error" in d2["message"]
    e3 = _media_unsupported_exc(_req(), {"reason": "no_send_media", "state": "running"},
                                platform="line", kind="media")
    assert e3.detail["code"] == "media_unsupported" and "媒体" in e3.detail["message"]


def test_501_wording_english():
    from src.web.routes.unified_inbox_send_routes import _media_unsupported_exc
    e = _media_unsupported_exc(_req("en"), {"reason": "worker_not_running", "state": "starting"},
                               platform="whatsapp", kind="media")
    assert "WhatsApp" in e.detail["message"] and "reconnecting" in e.detail["message"]
    e2 = _media_unsupported_exc(_req("en"), {"reason": "no_worker"}, platform="line", kind="voice")
    assert "can't send voice" in e2.detail["message"]


def test_capability_or_none_falls_back_for_old_orchestrators():
    from src.web.routes.unified_inbox_send_routes import media_capability_or_none

    class _Old:
        def owns_media(self, p, a):
            return a == "ok"

    assert media_capability_or_none(_Old(), "line", "ok")["owns"] is True
    cap = media_capability_or_none(_Old(), "line", "nope")
    assert cap["owns"] is False and cap["reason"] == "no_worker"


def test_i18n_keys_bilingual():
    from src.web.i18n_packs import errors_stock as ES
    for k in ("err.inbox.voice_unsupported_platform", "err.inbox.voice_reconnecting",
              "err.inbox.media_unsupported_platform", "err.inbox.media_reconnecting"):
        assert k in ES.ZH and k in ES.EN, k
    assert "{platform}" in ES.ZH["err.inbox.voice_reconnecting"]
    assert "{state}" in ES.EN["err.inbox.media_reconnecting"]


def test_route_wiring_uses_capability_split():
    src = (REPO / "src/web/routes/unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(501, tr(request, "err.inbox.voice_unsupported"))' not in src
    assert 'raise HTTPException(501, tr(request, "err.inbox.media_unsupported"))' not in src
    assert src.count("_media_unsupported_exc(request, _cap") == 2
    # send-caps 把分因带给前端（媒体按钮灰态 tooltip 同源）
    assert '"worker_state": worker_state' in src and '"backoff_sec": backoff_sec' in src


# ─────────────────── ④ receiver 跨重启退避 ───────────────────

def _worker():
    from src.integrations.account_orchestrator import LineProtocolWorker
    w = LineProtocolWorker({"account_id": "Uacct", "meta": {}}, {})
    w.client = object()
    w.state = "running"
    return w


@pytest.fixture(autouse=True)
def _clean_ledger():
    from src.integrations import account_orchestrator as AO
    from src.integrations.line_media_stats import get_line_media_stats
    AO._LINE_RECV_GIVEUP.clear()
    get_line_media_stats().reset()
    yield
    AO._LINE_RECV_GIVEUP.clear()
    get_line_media_stats().reset()


def test_hold_schedule_doubles_and_caps():
    from src.integrations.account_orchestrator import line_recv_hold_sec
    assert line_recv_hold_sec(0) == 0
    assert line_recv_hold_sec(1) == 60
    assert line_recv_hold_sec(2) == 120
    assert line_recv_hold_sec(4) == 480
    assert line_recv_hold_sec(99) == 1800
    assert line_recv_hold_sec("x") == 0


def test_giveup_records_ledger_stats_and_root_cause(caplog):
    import logging
    from src.integrations import account_orchestrator as AO
    from src.integrations.line_media_stats import get_line_media_stats
    w = _worker()
    with caplog.at_level(logging.ERROR, logger="src.integrations.account_orchestrator"):
        hold = w._note_recv_giveup("LineTransportError: request to https://line-chrome-gw.line-apps.com/api/operation/receive failed")
    assert hold == 60
    assert AO._LINE_RECV_GIVEUP["Uacct"]["streak"] == 1
    assert w._note_recv_giveup("again") == 120
    d = get_line_media_stats().dump()
    assert d["receiver"]["restarts_total"] == 2 and d["receiver"]["by_account"] == {"Uacct": 2}
    assert d["receiver"]["hold_total_sec"] == 180 and d["receiver"]["last_reason"] == "again"
    assert "line_receiver_restarts_total 2" in get_line_media_stats().dump_prom()
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "operation/receive" in joined and "hold=60s" in joined, "根因原文必须进日志"


def test_start_with_hold_defers_receiver_and_stays_healthy(monkeypatch):
    from src.integrations import account_orchestrator as AO
    timers = []

    class _Timer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.daemon, self.name = delay, fn, False, ""
            self._alive = False
            timers.append(self)

        def start(self):
            self._alive = True

        def is_alive(self):
            return self._alive

        def cancel(self):
            self._alive = False

    monkeypatch.setattr(threading, "Timer", _Timer)
    w = _worker()
    started = []
    monkeypatch.setattr(w, "_start_receiver", lambda: started.append(1))
    AO._LINE_RECV_GIVEUP["Uacct"] = {"streak": 2, "ts": time.time(), "reason": "x"}
    w._start_receiver_with_hold()
    assert started == [] and len(timers) == 1 and 115 <= timers[0].delay <= 120
    assert w._recv_state == "reconnecting" and w._recv_hold_until > time.time()
    st = w.status()
    assert st["recv_state"] == "reconnecting" and st["recv_giveup_streak"] == 2
    # 延后期不算不健康（否则编排器再重启一轮＝把退避变回风暴）
    assert asyncio.run(w.healthy()) is True
    timers[0].fn()          # 到点：拉 receiver
    assert started == [1]
    # 停 worker 要取消未到点的 timer
    w2 = _worker()
    AO._LINE_RECV_GIVEUP["Uacct"] = {"streak": 1, "ts": time.time(), "reason": ""}
    w2._start_receiver_with_hold()
    asyncio.run(w2.stop())
    assert timers[-1]._alive is False and w2._recv_state == "idle"


def test_no_hold_when_ledger_clean_or_elapsed(monkeypatch):
    from src.integrations import account_orchestrator as AO
    w = _worker()
    started = []
    monkeypatch.setattr(w, "_start_receiver", lambda: started.append(1))
    w._start_receiver_with_hold()
    assert started == [1] and w._recv_state == "idle"
    # 上次放弃已经过去比 hold 更久 → 直接拉
    AO._LINE_RECV_GIVEUP["Uacct"] = {"streak": 1, "ts": time.time() - 3600, "reason": ""}
    w._start_receiver_with_hold()
    assert started == [1, 1]


def test_stable_receiver_resets_ledger():
    from src.integrations import account_orchestrator as AO
    w = _worker()
    AO._LINE_RECV_GIVEUP["Uacct"] = {"streak": 3, "ts": time.time(), "reason": ""}
    w._recv_started_ts = time.time() - 100
    w._maybe_reset_recv_giveup()
    assert AO._LINE_RECV_GIVEUP["Uacct"]["streak"] == 3, "没稳跑够不清"
    w._recv_started_ts = time.time() - AO._LINE_RECV_STABLE_RESET_SEC - 1
    w._maybe_reset_recv_giveup()
    assert "Uacct" not in AO._LINE_RECV_GIVEUP


def test_hold_timer_does_not_start_receiver_on_stopped_worker(monkeypatch):
    from src.integrations import account_orchestrator as AO
    fired = []

    class _Timer:
        def __init__(self, delay, fn):
            self.fn = fn
            fired.append(self)

        def start(self):
            pass

        def is_alive(self):
            return True

        def cancel(self):
            pass

    monkeypatch.setattr(threading, "Timer", _Timer)
    w = _worker()
    started = []
    monkeypatch.setattr(w, "_start_receiver", lambda: started.append(1))
    AO._LINE_RECV_GIVEUP["Uacct"] = {"streak": 1, "ts": time.time(), "reason": ""}
    w._start_receiver_with_hold()
    w.state = "stopped"     # 编排器已停掉 worker
    fired[0].fn()
    assert started == [], "尸体上不起线程"


def test_receiver_loop_wiring_notes_giveup():
    src = (REPO / "src/integrations/account_orchestrator.py").read_text(encoding="utf-8")
    i = src.index("def _start_receiver(self)")
    seg = src[i:i + 6000]
    assert "self._note_recv_giveup(self._recv_last_error)" in seg
    assert "self._maybe_reset_recv_giveup()" in seg
    assert "self._start_receiver_with_hold()" in src[src.index("async def start(self)"):]
