# -*- coding: utf-8 -*-
"""主动触达 P2 门禁（2026-07-29）。

锁五条不变量：
  1. 账本 v3：sent_ts 与 ts 分离——mark_attempt（守卫拦下）绝不算「又一次未回」，
     也不产生幻影回应观察；obs_n/obs_replied 半衰滑窗累计「发送→结局」。
  2. 回复率反哺纯函数：样本不足不判；慢性低回复 stretch≥1；高回复 relax∈[0.5,1]。
  3. 规划器接线：低回复率拉长冷却拦发送、高回复率缩短、plan 带观测字段；
     attempt 推时后用户回过话 → streak 仍归零（P0 误伤回归钉）。
  4. 事实轮换：variety_key 当日恒定、跨天在 Top-K 内轮换；无 key = 旧 top-1；
     选中事实不进 context_facts。
  5. store：outreach_mode_histogram 前缀 + 时间窗 + 仅 sent。
"""

from __future__ import annotations

import time

from src.integrations.companion_proactive import (
    JsonProactiveLedger,
    plan_proactive_sends,
)
from src.utils.proactive_pacing import (
    parse_no_reply_backoff_cfg,
    parse_response_pacing_cfg,
    response_rate_factor,
)
from src.utils.proactive_topic import select_proactive_topic


def _noon_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                        lt.tm_wday, lt.tm_yday, -1))


# ── 1. 账本 v3 ──────────────────────────────────────────────────────────

def test_ledger_v3_sent_ts_and_obs_accumulation(tmp_path):
    led = JsonProactiveLedger(tmp_path / "cd.json")
    led.mark_send("c1", 1000, last_in_ts=0, text="第一条")
    e = led.entry("c1")
    assert e["sent_ts"] == 1000 and e["obs_n"] == 0  # 首条：无上一条可观察
    # 第二条：上一条(1000)后对方回了(1500) → 观察 +1 命中，streak 重置 1
    led.mark_send("c1", 2000, last_in_ts=1500, text="第二条")
    e = led.entry("c1")
    assert e["obs_n"] == 1 and e["obs_replied"] == 1 and e["streak"] == 1
    # 第三条：上一条(2000)后没回 → 观察 +1 未命中，streak +1
    led.mark_send("c1", 3000, last_in_ts=1500, text="第三条")
    e = led.entry("c1")
    assert e["obs_n"] == 2 and e["obs_replied"] == 1 and e["streak"] == 2


def test_ledger_attempt_no_phantom_observation(tmp_path):
    """attempt 推时不产生幻影观察，且不污染下一次真发的响应判定。"""
    led = JsonProactiveLedger(tmp_path / "cd.json")
    led.mark_send("c1", 1000, last_in_ts=0, text="真发")
    led.mark_attempt("c1", 5000)  # 变体守卫拦下：只推 ts
    e = led.entry("c1")
    assert e["ts"] == 5000 and e["sent_ts"] == 1000  # 真实发送时刻不动
    # 用户在真发(1000)后、attempt(5000)前回过话(2000)：
    # 旧实现对照被推高的 ts=5000 → 误判未回 streak=2；v3 对照 sent_ts=1000 → 重置 1
    led.mark_send("c1", 6000, last_in_ts=2000, text="下一条")
    e = led.entry("c1")
    assert e["streak"] == 1 and e["obs_replied"] == 1


def test_ledger_attempt_only_entry_never_counts_as_send(tmp_path):
    """从未真发、只 attempt 过的条目：落盘重载后 sent_ts 仍为 0（不回落 ts）。"""
    p = tmp_path / "cd.json"
    JsonProactiveLedger(p).mark_attempt("c1", 7000)
    led2 = JsonProactiveLedger(p)
    e = led2.entry("c1")
    assert e["ts"] == 7000 and e["sent_ts"] == 0.0
    # 首次真发：无上一条真实发送 → 不产生观察
    led2.mark_send("c1", 8000, last_in_ts=7500, text="首条")
    e = led2.entry("c1")
    assert e["obs_n"] == 0 and e["streak"] == 1


def test_ledger_legacy_v2_upgrade_sets_sent_ts(tmp_path):
    p = tmp_path / "cd.json"
    p.write_text(
        '{"a": 1000.5, "b": {"ts": 2000, "streak": 2, "last_text": "x"}}',
        "utf-8")
    led = JsonProactiveLedger(p)
    ea, eb = led.entry("a"), led.entry("b")
    # 旧格式的 ts 就是真实发送 → sent_ts 回落 ts；obs 从零起步
    assert ea["sent_ts"] == 1000.5 and ea["obs_n"] == 0
    assert eb["sent_ts"] == 2000 and eb["streak"] == 2


def test_ledger_obs_halving_window(tmp_path):
    led = JsonProactiveLedger(tmp_path / "cd.json")
    ts = 1000.0
    led.mark_send("c1", ts, last_in_ts=0, text="t")
    # 连打 25 次「发送→回了」观察：命中率恒 1.0，obs_n 被半衰控制在窗口内
    for i in range(25):
        ts += 100
        led.mark_send("c1", ts, last_in_ts=ts - 50, text="t")
    e = led.entry("c1")
    assert e["obs_n"] < 20  # 半衰生效，不无限增长
    assert e["obs_replied"] <= e["obs_n"]
    assert e["obs_replied"] / e["obs_n"] > 0.9  # 比率语义保持


# ── 2. 回复率反哺纯函数 ─────────────────────────────────────────────────

def test_parse_response_pacing_defaults():
    cfg = parse_response_pacing_cfg({})
    assert cfg["enabled"] is True
    assert cfg["min_obs"] == 4 and cfg["stretch"] == 1.5
    assert cfg["relax"] == 1.0  # 默认不提频（提频是运营决策）


def test_parse_response_pacing_clamps_relax():
    cfg = parse_response_pacing_cfg(
        {"response_pacing": {"relax": 0.1}})  # 手滑配置
    assert cfg["relax"] == 0.5  # 防骚扰底线


def test_response_rate_factor_semantics():
    cfg = parse_response_pacing_cfg(
        {"response_pacing": {"relax": 0.85}})
    assert response_rate_factor(2, 0, cfg) == 1.0      # 样本不足
    assert response_rate_factor(10, 0, cfg) == 1.5     # 慢性低回复 → stretch
    assert response_rate_factor(10, 1, cfg) == 1.5     # 0.1 ≤ low_rate
    assert response_rate_factor(10, 3, cfg) == 1.0     # 中间带不动
    assert response_rate_factor(10, 8, cfg) == 0.85    # 高回复 → relax
    off = dict(cfg, enabled=False)
    assert response_rate_factor(10, 0, off) == 1.0


# ── 3. 规划器接线 ───────────────────────────────────────────────────────

def _conv(cid="tg:a:1", *, silent_h=100.0, last_in_offset_h=None, now=None):
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
    return {"mode": "gentle_checkin", "directive": "hi", "fact": ""}


_RP = parse_response_pacing_cfg({"response_pacing": {"relax": 0.85}})


def _plan(convs, cooldown_map, now=None, **kw):
    now = now if now is not None else _noon_today()
    args = dict(
        cooldown_map=cooldown_map, opener_fn=_op, now=now,
        min_silent_hours=24, cooldown_hours=48, max_per_tick=5,
        quiet_start_hour=23, quiet_end_hour=8,
        backoff_cfg=None, response_pacing_cfg=_RP)
    args.update(kw)
    return plan_proactive_sends(convs, **args)


def test_planner_low_rate_stretches_cooldown():
    now = _noon_today()
    # 慢性低回复（0/6）：冷却 48×1.5=72h；上次真发 60h 前 → 72h 窗内不发。
    # 对方 50h 前开口过（streak 归零）→ 拦它的只能是回复率 stretch。
    cd = {"tg:a:1": {"ts": now - 60 * 3600, "sent_ts": now - 60 * 3600,
                     "streak": 1, "last_text": "x",
                     "obs_n": 6, "obs_replied": 0}}
    conv = _conv(silent_h=60, last_in_offset_h=50, now=now)
    assert _plan([conv], cd, now=now) == []
    # 对照组：不反哺 → 48h 已过，放行（证明是 stretch 在拦）
    plans = _plan([conv], cd, now=now, response_pacing_cfg=None)
    assert len(plans) == 1 and plans[0]["response_factor"] == 1.0


def test_planner_high_rate_relaxes_cooldown():
    now = _noon_today()
    # 慢性高回复（5/6）：冷却 48×0.85=40.8h；上次真发 44h 前（<48h 旧口径不发）
    cd = {"tg:a:1": {"ts": now - 44 * 3600, "sent_ts": now - 44 * 3600,
                     "streak": 1, "last_text": "x",
                     "obs_n": 6, "obs_replied": 5}}
    conv = _conv(silent_h=44, last_in_offset_h=30, now=now)
    plans = _plan([conv], cd, now=now)
    assert len(plans) == 1
    assert plans[0]["response_factor"] == 0.85
    assert plans[0]["response_obs"] == 6
    # 对照组：不反哺 → 44h < 48h 冷却，不发
    assert _plan([conv], cd, now=now, response_pacing_cfg=None) == []


def test_planner_rate_stacks_with_streak_backoff():
    now = _noon_today()
    bk = parse_no_reply_backoff_cfg({})
    # 低回复率(0/6)×streak=1：48×1.5×3=216h；上次 200h 前 → 不发
    cd = {"tg:a:1": {"ts": now - 200 * 3600, "sent_ts": now - 200 * 3600,
                     "streak": 1, "last_text": "x",
                     "obs_n": 6, "obs_replied": 0}}
    conv = _conv(silent_h=200, now=now)
    assert _plan([conv], cd, now=now, backoff_cfg=bk) == []
    # 220h 前 → 窗口已过，放行且两个观测字段都在
    cd2 = {"tg:a:1": dict(cd["tg:a:1"], ts=now - 220 * 3600,
                          sent_ts=now - 220 * 3600)}
    plans = _plan([_conv(silent_h=220, now=now)], cd2, now=now, backoff_cfg=bk)
    assert len(plans) == 1
    assert plans[0]["response_factor"] == 1.5
    assert plans[0]["unanswered_streak"] == 1


def test_planner_attempt_bump_does_not_punish_replied_user():
    """P0 误伤回归钉：attempt 推高 ts 后，用户其实回过话 → streak 必须归零。"""
    now = _noon_today()
    bk = parse_no_reply_backoff_cfg({})
    # 真发 100h 前；用户 50h 前回话；attempt 30h 前把 ts 推到 30h 前。
    # 冷却窗从 ts(30h) 算：48h 未过 → 本 tick 不发（防重烧 LLM，正确）；
    # 但 streak 判定必须对照 sent_ts(100h)：li(50h) > sent → streak 0。
    cd = {"tg:a:1": {"ts": now - 30 * 3600, "sent_ts": now - 100 * 3600,
                     "streak": 1, "last_text": "x",
                     "obs_n": 0, "obs_replied": 0}}
    conv = _conv(silent_h=50, last_in_offset_h=50, now=now)
    assert _plan([conv], cd, now=now, backoff_cfg=bk) == []  # 冷却窗内
    # 50h 后 attempt 冷却过窗：streak=0 → 正常 48h 口径放行（旧实现按 streak=1
    # 退避 144h 还要再等 4 天——用户回了话却被自己的被拦尝试惩罚）
    cd2 = {"tg:a:1": {"ts": now - 49 * 3600, "sent_ts": now - 120 * 3600,
                      "streak": 1, "last_text": "x",
                      "obs_n": 0, "obs_replied": 0}}
    conv2 = _conv(silent_h=70, last_in_offset_h=70, now=now)
    plans = _plan([conv2], cd2, now=now, backoff_cfg=bk)
    assert len(plans) == 1 and plans[0]["unanswered_streak"] == 0


# ── 4. 事实轮换 ─────────────────────────────────────────────────────────

def _facts(n=4):
    now = time.time()
    return [
        {"content": f"事实{i}备考{i}", "source": "user_stated",
         "hits": 5 - i, "last_seen": now - 86400 * (i + 1)}
        for i in range(n)
    ]


def test_fact_rotation_stable_same_day_and_rotates():
    t0 = _noon_today()
    sel1 = select_proactive_topic(
        _facts(), silent_hours=50, min_silent_hours=24,
        variety_key="cid:9", now=t0)
    sel2 = select_proactive_topic(
        _facts(), silent_hours=50, min_silent_hours=24,
        variety_key="cid:9", now=t0 + 3600)
    assert sel1["mode"] == "follow_up"
    assert sel1["fact"] == sel2["fact"]  # 同日恒定（跨 planner/preview 一致）
    # 一周内应轮到 ≥2 条不同事实（Top-3 crc32 轮换，7 天全撞同一条极不可能）
    week = {
        select_proactive_topic(
            _facts(), silent_hours=50, min_silent_hours=24,
            variety_key="cid:9", now=t0 + d * 86400)["fact"]
        for d in range(7)
    }
    assert len(week) >= 2


def test_fact_rotation_only_with_variety_key():
    t0 = _noon_today()
    baseline = select_proactive_topic(
        _facts(), silent_hours=50, min_silent_hours=24, now=t0)
    # 无 variety_key：恒 top-1（旧行为），与日期无关
    for d in range(5):
        again = select_proactive_topic(
            _facts(), silent_hours=50, min_silent_hours=24,
            now=t0 + d * 86400)
        assert again["fact"] == baseline["fact"]


def test_fact_rotation_chosen_not_in_context():
    t0 = _noon_today()
    sel = select_proactive_topic(
        _facts(), silent_hours=50, min_silent_hours=24,
        variety_key="cid:7", now=t0)
    assert sel["fact"] not in sel["context_facts"]
    assert sel["context_facts"]  # 其余事实仍作背景


def test_fact_rotation_single_fact_unaffected():
    t0 = _noon_today()
    sel = select_proactive_topic(
        _facts(1), silent_hours=50, min_silent_hours=24,
        variety_key="cid:7", now=t0)
    assert sel["fact"] == "事实0备考0"


# ── 5. store：mode 直方图 ───────────────────────────────────────────────

def test_store_outreach_mode_histogram(tmp_path):
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    store.record_outreach("c1", batch_id="proactive_topic:text",
                          note="gentle_checkin", ts=now - 3600)
    store.record_outreach("c2", batch_id="proactive_topic:voice",
                          note="life_share", ts=now - 7200)
    store.record_outreach("c3", batch_id="proactive_topic:text",
                          note="gentle_checkin", ts=now - 10800)
    # 窗口外 / 非 sent / 其他批次都不计
    store.record_outreach("c4", batch_id="proactive_topic:text",
                          note="gentle_checkin", ts=now - 20 * 86400)
    store.record_outreach("c5", batch_id="proactive_topic:text",
                          note="follow_up", status="failed", ts=now - 3600)
    store.record_outreach("c6", batch_id="reactivation:x",
                          note="other", ts=now - 3600)
    hist = store.outreach_mode_histogram("proactive_topic:", days=14.0, now=now)
    assert hist == {"gentle_checkin": 2, "life_share": 1}
    assert store.outreach_mode_histogram("", days=14.0) == {}
