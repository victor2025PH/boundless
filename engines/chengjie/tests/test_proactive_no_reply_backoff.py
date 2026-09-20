# -*- coding: utf-8 -*-
"""未回退避门禁（P0 2026-07-29）。

实锤事故：冷却只看时间不看响应 → 对从不回复的联系人每到冷却点（生产中位
10.6h）就再发一条「好久没联系」，近 14 天 45% 的主动发送是对空气连发。
锁四条不变量：
  1. 纯函数语义：对方回过话 streak 归零；未回按 multiplier^streak 拉长冷却、
     封顶；stop_after 硬停。
  2. 账本双格式：旧 ``{cid: float}`` 冷却文件透明升级（streak 按 1 保守），
     写盘新格式可回读。
  3. mark_send 响应语义：回过话 → streak 重置 1；没回 → +1；mark_attempt
     只推时间不动 streak（变体守卫拦下的不算打扰）。
  4. 规划器接线：退避中的会话不进计划；对方开口后立即恢复正常节奏；
     stop_after 达到后彻底出圈；plan 带 unanswered_streak 观测字段。
"""

from __future__ import annotations

import time

from src.integrations.companion_proactive import (
    JsonProactiveLedger,
    plan_proactive_sends,
)
from src.utils.proactive_pacing import (
    backoff_cooldown_hours,
    backoff_exhausted,
    parse_no_reply_backoff_cfg,
    unanswered_streak,
)


def _noon_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                        lt.tm_wday, lt.tm_yday, -1))


# ── 1. 纯函数 ───────────────────────────────────────────────────────────

def test_parse_defaults_enabled():
    cfg = parse_no_reply_backoff_cfg({})
    assert cfg["enabled"] is True
    assert cfg["multiplier"] == 3.0
    assert cfg["max_backoff_hours"] == 720.0
    assert cfg["stop_after"] == 0


def test_parse_explicit_off_and_bad_values():
    assert parse_no_reply_backoff_cfg(
        {"no_reply_backoff": {"enabled": False}})["enabled"] is False
    cfg = parse_no_reply_backoff_cfg(
        {"no_reply_backoff": {"multiplier": "bad", "stop_after": 2.9}})
    assert cfg["multiplier"] == 3.0  # 坏值回默认
    assert cfg["stop_after"] == 2


def test_unanswered_streak_semantics():
    # 从未主动过 → 0
    assert unanswered_streak(0, 5, 100) == 0
    # 上次主动之后对方回过话 → 归零
    assert unanswered_streak(1000, 3, 1500) == 0
    # 没回 → 按存量
    assert unanswered_streak(1000, 3, 500) == 3
    # 对方最后开口时间未知（0）→ 保守按存量（方向=更少打扰）
    assert unanswered_streak(1000, 2, 0) == 2


def test_backoff_scales_and_caps():
    cfg = parse_no_reply_backoff_cfg({})
    assert backoff_cooldown_hours(48, 0, cfg) == 48
    assert backoff_cooldown_hours(48, 1, cfg) == 144    # ×3
    assert backoff_cooldown_hours(48, 2, cfg) == 432    # ×9
    assert backoff_cooldown_hours(48, 3, cfg) == 720    # 1296 → 封顶 30 天
    assert backoff_cooldown_hours(48, 99, cfg) == 720   # 大 streak 不溢出


def test_backoff_disabled_passthrough():
    off = {"enabled": False, "multiplier": 3.0, "max_backoff_hours": 720}
    assert backoff_cooldown_hours(48, 5, off) == 48
    assert backoff_cooldown_hours(48, 5, None) == 48


def test_backoff_exhausted_only_with_stop_after():
    on = {"enabled": True, "stop_after": 3}
    assert backoff_exhausted(2, on) is False
    assert backoff_exhausted(3, on) is True
    assert backoff_exhausted(9, {"enabled": True, "stop_after": 0}) is False
    assert backoff_exhausted(9, {"enabled": False, "stop_after": 3}) is False


# ── 2/3. 账本 ───────────────────────────────────────────────────────────

def test_ledger_upgrades_legacy_float_file(tmp_path):
    p = tmp_path / "cooldown.json"
    p.write_text('{"tg:a:1": 1000.5}', "utf-8")
    led = JsonProactiveLedger(p)
    e = led.entry("tg:a:1")
    # v3 起条目还带 sent_ts（=ts，旧格式的 ts 就是真实发送）与 obs 观察字段
    assert e["ts"] == 1000.5 and e["streak"] == 1 and e["last_text"] == ""
    assert e["sent_ts"] == 1000.5 and e["obs_n"] == 0
    snap = led.snapshot()
    assert snap["tg:a:1"]["streak"] == 1


def test_ledger_mark_send_streak_semantics(tmp_path):
    led = JsonProactiveLedger(tmp_path / "cd.json")
    # 首次主动
    led.mark_send("c1", 1000, last_in_ts=0, text="第一条")
    assert led.entry("c1")["streak"] == 1
    # 对方没回，又发 → +1，文案更新
    led.mark_send("c1", 2000, last_in_ts=500, text="第二条")
    e = led.entry("c1")
    assert e["streak"] == 2 and e["last_text"] == "第二条"
    # 对方在上次主动(2000)之后开口(2500) → 新一轮，重置为 1
    led.mark_send("c1", 3000, last_in_ts=2500, text="第三条")
    assert led.entry("c1")["streak"] == 1


def test_ledger_mark_attempt_keeps_streak(tmp_path):
    led = JsonProactiveLedger(tmp_path / "cd.json")
    led.mark_send("c1", 1000, last_in_ts=0, text="问候")
    led.mark_attempt("c1", 2000)  # 变体守卫拦下：只推时间
    e = led.entry("c1")
    assert e["ts"] == 2000 and e["streak"] == 1 and e["last_text"] == "问候"


def test_ledger_persist_roundtrip(tmp_path):
    p = tmp_path / "cd.json"
    JsonProactiveLedger(p).mark_send("c1", 1234, last_in_ts=0, text="你好呀")
    led2 = JsonProactiveLedger(p)
    e = led2.entry("c1")
    assert e["ts"] == 1234 and e["streak"] == 1 and e["last_text"] == "你好呀"


def test_ledger_legacy_mark_counts_as_unanswered(tmp_path):
    led = JsonProactiveLedger(tmp_path / "cd.json")
    led.mark("c1", 100)
    led.mark("c1", 200)
    assert led.entry("c1")["streak"] == 2  # 旧接口按未回保守累加


def test_ledger_text_truncated(tmp_path):
    led = JsonProactiveLedger(tmp_path / "cd.json")
    led.mark_send("c1", 1, last_in_ts=0, text="长" * 200)
    assert len(led.entry("c1")["last_text"]) == 80


# ── 4. 规划器接线 ───────────────────────────────────────────────────────

_BK = parse_no_reply_backoff_cfg({})   # 默认：×3 封顶 720h，不硬停


def _conv(cid="tg:a:1", *, silent_h=100.0, last_in_offset_h=None, now=None):
    """快照工厂：silent_h=沉默小时；last_in_offset_h=对方最后开口距 now 的小时
    （None=从未开口 → last_in_ts=0）。"""
    now = now if now is not None else _noon_today()
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "a",
        "chat_key": "1", "last_ts": now - silent_h * 3600.0,
        "last_direction": "out", "archived": False, "memory_key": "1",
        "intimacy": 0.0,
        "last_in_ts": (0.0 if last_in_offset_h is None
                       else now - last_in_offset_h * 3600.0),
    }


def _op(**kw):
    return {"mode": "gentle_checkin", "directive": "hi", "fact": "",
            "gap_bucket": "week"}


def _plan(convs, cooldown_map, now=None, **kw):
    now = now if now is not None else _noon_today()
    args = dict(
        cooldown_map=cooldown_map, opener_fn=_op, now=now,
        min_silent_hours=24, cooldown_hours=48, max_per_tick=5,
        quiet_start_hour=23, quiet_end_hour=8, backoff_cfg=_BK)
    args.update(kw)
    return plan_proactive_sends(convs, **args)


def test_planner_unanswered_backoff_blocks_within_window():
    now = _noon_today()
    # 上次主动 100h 前、streak=1、对方从未开口：退避后冷却=48×3=144h → 不发
    cd = {"tg:a:1": {"ts": now - 100 * 3600, "streak": 1, "last_text": "x"}}
    assert _plan([_conv(silent_h=100, now=now)], cd, now=now) == []
    # 同条件关退避 → 100h > 48h 冷却，会发（对照组证明是退避在拦）
    plans = _plan([_conv(silent_h=100, now=now)], cd, now=now, backoff_cfg=None)
    assert len(plans) == 1


def test_planner_backoff_expires_then_allows():
    now = _noon_today()
    # streak=1 退避 144h；上次主动 150h 前 → 窗口已过，放行并带观测字段
    cd = {"tg:a:1": {"ts": now - 150 * 3600, "streak": 1, "last_text": "x"}}
    plans = _plan([_conv(silent_h=150, now=now)], cd, now=now)
    assert len(plans) == 1
    assert plans[0]["unanswered_streak"] == 1
    assert plans[0]["effective_cooldown_hours"] == 144.0


def test_planner_reply_resets_streak():
    now = _noon_today()
    # streak=2 但对方在上次主动之后开口过（50h 前 > 上次主动 100h 前）
    # → streak 归零、正常 48h 冷却；沉默 100h > 48h → 放行
    cd = {"tg:a:1": {"ts": now - 100 * 3600, "streak": 2, "last_text": "x"}}
    plans = _plan(
        [_conv(silent_h=100, last_in_offset_h=50, now=now)], cd, now=now)
    assert len(plans) == 1
    assert plans[0]["unanswered_streak"] == 0
    assert plans[0]["effective_cooldown_hours"] == 48.0


def test_planner_legacy_float_cooldown_still_backs_off():
    now = _noon_today()
    # 存量 float 冷却条目 → streak 按 1 保守：100h < 144h 退避窗 → 不发
    cd = {"tg:a:1": now - 100 * 3600}
    assert _plan([_conv(silent_h=100, now=now)], cd, now=now) == []


def test_planner_stop_after_hard_stops():
    now = _noon_today()
    bk = dict(_BK, stop_after=3)
    cd = {"tg:a:1": {"ts": now - 10000 * 3600, "streak": 3, "last_text": "x"}}
    # 窗口早就过了，但 stop_after=3 已达 → 彻底停发
    assert _plan([_conv(silent_h=10000, now=now)], cd, now=now,
                 backoff_cfg=bk) == []
    # 对方开口 → streak 归零 → 恢复
    plans = _plan([_conv(silent_h=100, last_in_offset_h=50, now=now)], cd,
                  now=now, backoff_cfg=bk)
    assert len(plans) == 1


def test_planner_no_backoff_cfg_is_legacy_behavior():
    now = _noon_today()
    cd = {"tg:a:1": {"ts": now - 100 * 3600, "streak": 5, "last_text": "x"}}
    # 不传 backoff_cfg：streak 不参与，48h 冷却已过 → 放行（向后兼容）
    plans = _plan([_conv(silent_h=100, now=now)], cd, now=now, backoff_cfg=None)
    assert len(plans) == 1


def test_planner_first_touch_unaffected():
    now = _noon_today()
    plans = _plan([_conv(silent_h=100, now=now)], {}, now=now)
    assert len(plans) == 1
    assert plans[0]["unanswered_streak"] == 0


# ── 判据数据源：store.last_inbound_ts_map ───────────────────────────────

def test_store_last_inbound_ts_map(tmp_path):
    from src.inbox.models import InboxConversation, InboxMessage
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    conv = InboxConversation(
        conversation_id="tg:a:1", platform="telegram", chat_key="1")
    store.ingest_batch(conv, [
        InboxMessage(conversation_id="tg:a:1", platform_msg_id="m1",
                     direction="in", text="hi", ts=100.0),
        InboxMessage(conversation_id="tg:a:1", platform_msg_id="m2",
                     direction="in", text="再一条", ts=200.0),
        InboxMessage(conversation_id="tg:a:1", platform_msg_id="m3",
                     direction="out", text="我方回复", ts=300.0),
    ])
    conv2 = InboxConversation(
        conversation_id="tg:a:2", platform="telegram", chat_key="2")
    store.ingest_batch(conv2, [
        InboxMessage(conversation_id="tg:a:2", platform_msg_id="m4",
                     direction="out", text="只出不进", ts=400.0),
    ])
    got = store.last_inbound_ts_map(["tg:a:1", "tg:a:2", "tg:a:404"])
    assert got.get("tg:a:1") == 200.0     # 最后入站，而非最后消息(300 out)
    assert "tg:a:2" not in got            # 从未入站 → 不返回（调用方按 0）
    assert "tg:a:404" not in got
    assert store.last_inbound_ts_map([]) == {}
