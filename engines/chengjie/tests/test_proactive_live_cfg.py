# -*- coding: utf-8 -*-
"""P1-198：主动触达 pacing 配置现读（热重载后不重启即生效）。

实锤背景（2026-07-31，198 坐席机）：演示档把 min_silent 24h→4h 写进 overlay，
配置热重载成功，但 CompanionProactiveLoop 在构造时把 pacing 固化进实例属性，
节奏纹丝不动，最终靠重启应用才生效。修法＝可选 ``live_cfg_provider``：
每 tick 现读解析后的 pacing 覆盖值；无 provider / 异常 → 构造值（旧行为）。
"""
from __future__ import annotations

import pytest

from src.integrations.companion_proactive import CompanionProactiveLoop


NOW = 2_000_000.0


class _CD:
    def __init__(self):
        self.marks = {}

    def snapshot(self):
        return dict(self.marks)

    def mark(self, k, ts):
        self.marks[k] = ts


def _mk_loop(*, conv_silent_hours, sent_box, baked_min_silent=24.0,
             live_cfg_provider=None, baked_dry_run=False, n_convs=1,
             baked_max_per_tick=10):
    convs = [{
        "conversation_id": f"c{i}", "platform": "telegram",
        "account_id": "default", "chat_key": f"c{i}",
        "last_ts": NOW - conv_silent_hours * 3600,
        "last_direction": "out", "memory_key": f"c{i}",
        "stage": "", "intimacy": 50.0,
    } for i in range(n_convs)]

    def _opener(**kwargs):
        return {"mode": "follow_up", "directive": "关心一下", "context_facts": ["x"]}

    async def _send(p):
        sent_box.append(p["conversation_id"])
        return True

    return CompanionProactiveLoop(
        conversations_provider=lambda: convs,
        opener_fn=_opener,
        send_fn=_send,
        cooldown_store=_CD(),
        min_silent_hours=baked_min_silent,
        cooldown_hours=0.0,
        max_per_tick=baked_max_per_tick,
        quiet_start_hour=0, quiet_end_hour=0,
        dry_run=baked_dry_run,
        live_cfg_provider=live_cfg_provider,
        now=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_baked_threshold_blocks_without_provider():
    """基线：沉默 6h < 固化 24h → 不发（旧行为对照组）。"""
    sent = []
    loop = _mk_loop(conv_silent_hours=6, sent_box=sent)
    res = await loop.run_once()
    assert res == {"planned": 0, "sent": 0} and sent == []


@pytest.mark.asyncio
async def test_live_min_silent_takes_effect_without_restart():
    """现读 4h 覆盖固化 24h → 沉默 6h 的会话本 tick 即发（热更新语义）。"""
    sent = []
    loop = _mk_loop(
        conv_silent_hours=6, sent_box=sent,
        live_cfg_provider=lambda: {"min_silent_hours": 4.0})
    res = await loop.run_once()
    assert res["sent"] == 1 and sent == ["c0"]


@pytest.mark.asyncio
async def test_live_provider_exception_falls_back_to_baked():
    """provider 抛异常 → 静默回退构造值，行为与无 provider 完全一致。"""
    sent = []

    def _boom():
        raise RuntimeError("config unavailable")

    loop = _mk_loop(conv_silent_hours=6, sent_box=sent, live_cfg_provider=_boom)
    res = await loop.run_once()
    assert res == {"planned": 0, "sent": 0}


@pytest.mark.asyncio
async def test_live_dry_run_toggle_stops_real_send():
    """现读 dry_run=True → 计划照出、send_fn 不被调用（计数仍记 sent）。"""
    sent = []
    loop = _mk_loop(
        conv_silent_hours=100, sent_box=sent,
        live_cfg_provider=lambda: {"dry_run": True})
    res = await loop.run_once()
    assert res["sent"] == 1
    assert sent == []  # 真发被 dry_run 拦下


@pytest.mark.asyncio
async def test_live_max_per_tick_caps():
    """现读 max_per_tick=1 覆盖固化 10 → 两个合格会话只发一个。"""
    sent = []
    loop = _mk_loop(
        conv_silent_hours=100, sent_box=sent, n_convs=2,
        live_cfg_provider=lambda: {"max_per_tick": 1})
    res = await loop.run_once()
    assert res["sent"] == 1 and len(sent) == 1


@pytest.mark.asyncio
async def test_live_bad_value_type_falls_back_per_key():
    """脏值（不可转型）→ 该键回退构造值，其余键照常生效。"""
    sent = []
    loop = _mk_loop(
        conv_silent_hours=6, sent_box=sent,
        live_cfg_provider=lambda: {
            "min_silent_hours": "not-a-number",  # 坏键 → 回退 24h → 不发
            "max_per_tick": 5,
        })
    res = await loop.run_once()
    assert res == {"planned": 0, "sent": 0}


# ── 候选诊断收集器（P1-198：静默闸口 → 可读原因）────────────────────────────
def _conv(cid="c1", *, silent_h=6.0, direction="out", last_ts=None, **kw):
    d = {
        "conversation_id": cid, "platform": "telegram", "account_id": "default",
        "chat_key": cid, "last_ts": NOW - silent_h * 3600 if last_ts is None else last_ts,
        "last_direction": direction, "memory_key": cid, "stage": "",
        "intimacy": 50.0,
    }
    d.update(kw)
    return d


def _opener_ok(**kwargs):
    return {"mode": "follow_up", "directive": "关心一下", "context_facts": []}


def test_diag_min_silent_reports_hours_needed():
    from src.integrations.companion_proactive import plan_proactive_sends
    diags: list = []
    plans = plan_proactive_sends(
        [_conv(silent_h=6.0)], cooldown_map={}, opener_fn=_opener_ok,
        now=NOW, min_silent_hours=24.0, cooldown_hours=0.0,
        quiet_start_hour=0, quiet_end_hour=0, diagnostics=diags)
    assert plans == []
    assert len(diags) == 1
    d = diags[0]
    assert d["reason"] == "min_silent" and d["conversation_id"] == "c1"
    assert "还差" in d["detail"] and d["silent_hours"] == 6.0


def test_diag_cooldown_and_awaiting_reply():
    from src.integrations.companion_proactive import plan_proactive_sends
    diags: list = []
    plans = plan_proactive_sends(
        [
            _conv("c_cool", silent_h=100.0),
            _conv("c_in", silent_h=100.0, direction="in"),
        ],
        cooldown_map={"c_cool": NOW - 1 * 3600},  # 1h 前发过，冷却 12h
        opener_fn=_opener_ok, now=NOW,
        min_silent_hours=4.0, cooldown_hours=12.0,
        quiet_start_hour=0, quiet_end_hour=0, diagnostics=diags)
    assert plans == []
    reasons = {d["conversation_id"]: d["reason"] for d in diags}
    assert reasons["c_cool"] == "cooldown"
    assert reasons["c_in"] == "awaiting_reply"


def test_diag_quiet_hours_global_single_row():
    from src.integrations.companion_proactive import plan_proactive_sends
    import time as _t
    diags: list = []
    hour = _t.localtime(NOW).tm_hour
    plans = plan_proactive_sends(
        [_conv(silent_h=100.0)], cooldown_map={}, opener_fn=_opener_ok,
        now=NOW, min_silent_hours=4.0, cooldown_hours=0.0,
        quiet_start_hour=hour, quiet_end_hour=(hour + 1) % 24,
        diagnostics=diags)
    assert plans == []
    assert len(diags) == 1 and diags[0]["reason"] == "quiet_hours_global"


def test_diag_none_means_zero_overhead_and_same_result():
    """不传收集器 → 行为与旧签名逐位一致（回归对照）。"""
    from src.integrations.companion_proactive import plan_proactive_sends
    plans = plan_proactive_sends(
        [_conv(silent_h=100.0)], cooldown_map={}, opener_fn=_opener_ok,
        now=NOW, min_silent_hours=4.0, cooldown_hours=0.0,
        quiet_start_hour=0, quiet_end_hour=0)
    assert len(plans) == 1


def test_diag_no_opener_reason():
    from src.integrations.companion_proactive import plan_proactive_sends
    diags: list = []
    plans = plan_proactive_sends(
        [_conv(silent_h=100.0)], cooldown_map={},
        opener_fn=lambda **k: {"mode": "", "directive": ""},
        now=NOW, min_silent_hours=4.0, cooldown_hours=0.0,
        quiet_start_hour=0, quiet_end_hour=0, diagnostics=diags)
    assert plans == []
    assert diags and diags[0]["reason"] == "no_opener"
