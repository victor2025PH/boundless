"""试用领取的后台兑现巡检门禁（P2）。

为什么这条巡检必须存在：用户在首启向导点完「免费领取」就去干活了，而厂商机可能
一分钟后才签发、客服核销赠量更可能是几小时后。没有后台轮询，授权就只在「那个
一辈子只弹一次的向导恰好还开着」时才落地。

所以这里守两件事：**该跑的时候真的跑**（有未兑现的单 → 轮询并落地），
以及**不该跑的时候一次网络都不发**（没领过/已完成/已用尽的部署零开销）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.inbox.health_watchdog import HealthWatchdog


@pytest.fixture
def wd(tmp_path, monkeypatch):
    from src.licensing import trial_claim_client as tc
    monkeypatch.setattr(tc, "state_path", lambda config=None: tmp_path / "trial_claim.json")
    w = HealthWatchdog.__new__(HealthWatchdog)
    w._config_manager = SimpleNamespace(config={})
    w._last_trial_poll_ts = 0.0
    return w


@pytest.fixture
def spy(monkeypatch):
    """记录 poll 调用次数与落地调用，避免任何真网络。"""
    from src.licensing import trial_claim_client as tc
    from src.web.routes import license_routes as lr

    calls = {"poll": 0, "consume": []}

    def fake_poll(*, config=None, fetch=None):
        calls["poll"] += 1
        return dict(calls.get("_resp") or {"ok": True})

    def fake_consume(res, lic, voucher):
        calls["consume"].append((lic, voucher))
        return {"ok": True, "activated": bool(lic), "gift_redeemed": bool(voucher)}

    monkeypatch.setattr(tc, "poll", fake_poll)
    monkeypatch.setattr(lr, "_consume_trial_payload", fake_consume)
    return calls


def _write(tmp_state, **fields):
    from src.licensing import trial_claim_client as tc
    tc.save_state(dict(fields))


# ── 静默路径：正常部署一次网络都不该发 ────────────────────────────────────

def test_never_claimed_is_silent(wd, spy):
    wd._check_trial_claim()
    assert spy["poll"] == 0, "没领过试用的部署不该产生任何轮询"


def test_fully_settled_claim_stops_polling(wd, spy):
    _write(None, claim_id="tc_1", activated_at=111, topup_redeemed_at=222)
    wd._check_trial_claim()
    assert spy["poll"] == 0, "授权与赠量都到位 = 这条单子生命周期结束"


def test_exhausted_machine_stops_polling(wd, spy):
    _write(None, claim_id="tc_1", exhausted=True)
    wd._check_trial_claim()
    assert spy["poll"] == 0, "本机试用已用尽，再查也不会有结果"


def test_throttled_between_ticks(wd, spy):
    _write(None, claim_id="tc_1")
    wd._check_trial_claim(now=1000.0)
    wd._check_trial_claim(now=1010.0)
    assert spy["poll"] == 1, "默认 120s 节流，看门狗每 tick 都打官网是不可接受的"
    wd._check_trial_claim(now=1000.0 + 130)
    assert spy["poll"] == 2


def test_interval_is_configurable(wd, spy):
    wd._config_manager.config = {"licensing": {"trial": {"poll_interval_sec": 5}}}
    _write(None, claim_id="tc_1")
    wd._check_trial_claim(now=1000.0)
    wd._check_trial_claim(now=1006.0)
    assert spy["poll"] == 2


# ── 兑现路径 ───────────────────────────────────────────────────────────────

def test_license_lands_without_any_ui(wd, spy):
    """核心不变量：向导早就关了，授权照样落地。"""
    _write(None, claim_id="tc_1")
    spy["_resp"] = {"ok": True, "license": "LIC.TOKEN"}
    wd._check_trial_claim()
    assert spy["consume"] == [("LIC.TOKEN", "")]


def test_gift_lands_hours_later(wd, spy):
    """客服几小时后才核销——用户不必回到任何界面。"""
    _write(None, claim_id="tc_1", activated_at=111)
    spy["_resp"] = {"ok": True, "topup_voucher": "V.TOKEN"}
    wd._check_trial_claim()
    assert spy["consume"] == [("", "V.TOKEN")]


def test_pending_poll_does_not_call_consume(wd, spy):
    _write(None, claim_id="tc_1")
    spy["_resp"] = {"ok": True, "status": "pending"}
    wd._check_trial_claim()
    assert spy["poll"] == 1 and spy["consume"] == []


def test_network_failure_is_quiet_and_retries(wd, spy):
    """网络抖动不告警：用户此刻正用体验档干活，弹窗只会制造焦虑。"""
    _write(None, claim_id="tc_1")
    spy["_resp"] = {"ok": False, "error": "network"}
    wd._check_trial_claim(now=1000.0)
    assert spy["consume"] == []
    spy["_resp"] = {"ok": True, "license": "LIC.TOKEN"}
    wd._check_trial_claim(now=1000.0 + 130)
    assert spy["consume"] == [("LIC.TOKEN", "")], "下一轮该照常兑现"


def test_check_is_wired_into_watchdog_tick():
    """防「写了方法但没挂进 tick」——那样整条后台兑现链是死的。"""
    import inspect
    src = inspect.getsource(HealthWatchdog)
    assert "self._check_trial_claim()" in src
