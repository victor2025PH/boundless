"""智能养号 · 执行引擎门禁（P1，2026-08-21）——重点：dry_run 绝不碰账号、
go_live 只对金丝雀且尊重 kill-switch、no_adapter 诚实不假装。"""
from datetime import datetime

import pytest

from src.nurture.nurture_engine import NurtureEngine
from src.nurture.nurture_ledger import NurtureLedger


def _ts(hour: int) -> float:
    return datetime(2026, 8, 21, hour, 0, 0).timestamp()


_ACTIVE = _ts(10)


def _accounts(behaviors=None):
    return [{
        "key": "telegram:a", "platform": "telegram", "account_id": "a",
        "plan": {"enabled": True, "profile": "balanced", "ramp_days": 14,
                 "behaviors": behaviors or {"read": True}},
    }]


def _signals(_accts):
    return {"telegram:a": {"stage": "active", "age_days": 100}}


class _Rec:
    """记录 cb 是否被真调用（验证 dry_run 零真动作）。"""
    def __init__(self):
        self.read_calls = []
        self.chat_calls = []
        self.block = False

    async def read(self, p, a):
        self.read_calls.append((p, a))
        return True

    async def self_chat(self, p, a):
        self.chat_calls.append((p, a))
        return True

    async def blocked(self, p, a):
        return self.block


def _engine(tmp_path, cfg, rec, **kw):
    led = NurtureLedger(tmp_path / "nl.json")
    return NurtureEngine(
        led, cfg_provider=lambda: cfg,
        accounts_provider=lambda: _accounts(kw.get("behaviors")),
        signals_provider=_signals,
        read_cb=rec.read, self_chat_cb=rec.self_chat, block_check=rec.blocked,
    ), led


@pytest.mark.asyncio
async def test_pause_no_actions(tmp_path):
    rec = _Rec()
    eng, led = _engine(tmp_path, {"enabled": False}, rec)
    n = await eng.run_once(now=_ACTIVE)
    assert n == 0 and eng.last_tick_gated is True
    assert not rec.read_calls and led.stats()["planned"] == 0


@pytest.mark.asyncio
async def test_dry_run_never_calls_real_cb(tmp_path):
    rec = _Rec()
    eng, led = _engine(tmp_path, {"enabled": True, "dry_run": True}, rec)
    n = await eng.run_once(now=_ACTIVE)
    assert n == 1                       # 规划了一条
    assert not rec.read_calls           # 但绝不真调 read
    assert not rec.chat_calls
    assert led.stats()["executed"] == 0
    sh = led.recent_shadow()
    assert sh and sh[0]["dry_run"] is True and sh[0]["detail"] == "dry_run"


@pytest.mark.asyncio
async def test_go_live_non_canary_is_shadow(tmp_path):
    rec = _Rec()
    # go_live 但金丝雀名单为空 → 非金丝雀 → 影子，不真动作
    eng, led = _engine(tmp_path, {"enabled": True, "dry_run": False, "canary_accounts": []}, rec)
    await eng.run_once(now=_ACTIVE)
    assert not rec.read_calls
    assert led.recent_shadow()[0]["detail"] == "non_canary"


@pytest.mark.asyncio
async def test_go_live_canary_executes_read(tmp_path):
    rec = _Rec()
    eng, led = _engine(tmp_path, {"enabled": True, "dry_run": False,
                                  "canary_accounts": ["telegram:a"]}, rec)
    await eng.run_once(now=_ACTIVE)
    assert rec.read_calls == [("telegram", "a")]   # 真调了 read
    assert led.stats()["executed"] == 1


@pytest.mark.asyncio
async def test_go_live_canary_blocked_skips(tmp_path):
    rec = _Rec()
    rec.block = True                                # kill-switch 命中
    eng, led = _engine(tmp_path, {"enabled": True, "dry_run": False,
                                  "canary_accounts": ["telegram:a"]}, rec)
    await eng.run_once(now=_ACTIVE)
    assert not rec.read_calls                       # 被拦，不真动作
    assert led.recent_shadow()[0]["detail"] == "blocked"


@pytest.mark.asyncio
async def test_self_chat_go_live_calls_chat_cb(tmp_path):
    rec = _Rec()
    cfg = {"enabled": True, "dry_run": False, "canary_accounts": ["telegram:a"],
           "self_chat": {"enabled": True}}
    eng, led = _engine(tmp_path, cfg, rec, behaviors={"self_chat": True})
    await eng.run_once(now=_ACTIVE)
    assert rec.chat_calls == [("telegram", "a")]
    assert not rec.read_calls


@pytest.mark.asyncio
async def test_no_adapter_kind_recorded_not_executed(tmp_path):
    rec = _Rec()
    cfg = {"enabled": True, "dry_run": False, "canary_accounts": ["telegram:a"]}
    # 只启用 browse（无现成 API）→ go_live 记 no_adapter，不真调任何 cb
    eng, led = _engine(tmp_path, cfg, rec, behaviors={"browse": True})
    await eng.run_once(now=_ACTIVE)
    assert not rec.read_calls and not rec.chat_calls
    assert led.recent_shadow()[0]["detail"] == "no_adapter"


@pytest.mark.asyncio
async def test_max_actions_per_tick_cap(tmp_path):
    rec = _Rec()
    accts = [{"key": f"telegram:{i}", "platform": "telegram", "account_id": str(i),
              "plan": {"enabled": True, "profile": "aggressive", "behaviors": {"read": True}}}
             for i in range(10)]
    led = NurtureLedger(tmp_path / "nl.json")
    eng = NurtureEngine(
        led, cfg_provider=lambda: {"enabled": True, "dry_run": True, "max_actions_per_tick": 3},
        accounts_provider=lambda: accts,
        signals_provider=lambda a: {x["key"]: {"stage": "active"} for x in a},
        read_cb=rec.read, self_chat_cb=rec.self_chat, block_check=rec.blocked)
    n = await eng.run_once(now=_ACTIVE)
    assert n == 3                                   # 全局封顶 3


@pytest.mark.asyncio
async def test_probe_preview_never_calls_cb(tmp_path):
    rec = _Rec()
    eng, led = _engine(tmp_path, {"enabled": False}, rec)  # 引擎关也能探针
    out = await eng.probe_action("telegram", "a", "read", confirm=False)
    assert out["executable"] is True and out["blocked"] is False
    assert out["detail"] == "preview_ok"
    assert not rec.read_calls   # 预检绝不真跑


@pytest.mark.asyncio
async def test_probe_confirm_runs_read(tmp_path):
    rec = _Rec()
    eng, led = _engine(tmp_path, {"enabled": False}, rec)
    out = await eng.probe_action("telegram", "a", "read", confirm=True)
    assert rec.read_calls == [("telegram", "a")] and out["ok"] is True
    assert out["detail"] == "ok"
    assert led.stats()["executed"] == 1   # probe 真动作落账


@pytest.mark.asyncio
async def test_probe_blocked_skips(tmp_path):
    rec = _Rec()
    rec.block = True
    eng, led = _engine(tmp_path, {"enabled": False}, rec)
    out = await eng.probe_action("telegram", "a", "self_chat", confirm=True)
    assert out["blocked"] is True and out["detail"] == "blocked"
    assert not rec.chat_calls   # 被 kill-switch 拦，不真跑


@pytest.mark.asyncio
async def test_probe_no_adapter(tmp_path):
    rec = _Rec()
    eng, led = _engine(tmp_path, {"enabled": False}, rec)
    out = await eng.probe_action("telegram", "a", "browse", confirm=True)
    assert out["executable"] is False and out["detail"] == "no_adapter"
    assert not rec.read_calls and not rec.chat_calls


def test_ledger_date_rollover(tmp_path):
    led = NurtureLedger(tmp_path / "nl.json")
    led.record_action("telegram:a", "read", now=_ACTIVE, dry_run=True)
    snap = led.snapshot(now=_ACTIVE)
    assert snap["telegram:a"]["count_today"] == 1
    # 次日快照：计数清零
    nextday = datetime(2026, 8, 22, 10, 0, 0).timestamp()
    assert led.snapshot(now=nextday)["telegram:a"]["count_today"] == 0


def test_ledger_shadow_bounded(tmp_path):
    led = NurtureLedger(tmp_path / "nl.json")
    for i in range(260):
        led.record_action("telegram:a", "read", now=_ACTIVE + i, dry_run=True)
    assert led.stats()["shadow_len"] <= 200          # 环上限
