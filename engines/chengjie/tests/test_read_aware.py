# -*- coding: utf-8 -*-
"""已读/未读分流门禁（P1 2026-08-18）。

语义：未回退避此前把「看了没回」与「压根没看到」当同一信号。分流后——
read（已读不回）＝软拒绝 → 退避×read_multiplier（默认4）+ prompt 无压力文体 +
停语音照片；unread（未读）＝没看到≠拒绝 → 首条不罚（折算 streak 向下取整），
连续 ≥2 条未读＝疑似被静音 → 月频地板；unknown（非 TG/无回执）＝旧退避语义
逐位不变。默认关（unread 首条不罚会增发，须 overlay 显式开）。
"""
from __future__ import annotations

from pathlib import Path

from src.inbox.store import InboxStore
from src.integrations.companion_proactive import plan_proactive_sends
from src.utils.proactive_pacing import (
    backoff_cooldown_hours,
    parse_no_reply_backoff_cfg,
    parse_read_aware_cfg,
    read_aware_cooldown_hours,
)

NOW = 1_800_000_000.0
_H = 3600.0
_BK = parse_no_reply_backoff_cfg({})          # multiplier 3, cap 720
_RA = parse_read_aware_cfg({"read_aware": {"enabled": True}})


# ── 配置解析 ─────────────────────────────────────────────────────────────────

def test_parse_defaults_disabled_and_clamps():
    assert parse_read_aware_cfg({})["enabled"] is False
    cfg = parse_read_aware_cfg({"read_aware": {
        "enabled": True, "read_multiplier": 0.2, "unread_discount": 3,
        "mute_after_unread": -1}})
    assert cfg["enabled"] is True
    assert cfg["read_multiplier"] == 1.0     # 下限 1
    assert cfg["unread_discount"] == 1.0     # 上限 1
    assert cfg["mute_after_unread"] == 0     # 负数归 0=关
    assert _RA["read_multiplier"] == 4.0
    assert _RA["unread_discount"] == 0.5
    assert _RA["mute_after_unread"] == 2
    assert _RA["mute_cooldown_hours"] == 720.0


# ── read_aware_cooldown_hours 三态 ───────────────────────────────────────────

def test_unknown_state_identical_to_plain_backoff():
    for st in (0, 1, 2, 5):
        assert read_aware_cooldown_hours(48.0, st, "", _BK, _RA) == \
            backoff_cooldown_hours(48.0, st, _BK)


def test_disabled_cfg_identical_to_plain_backoff():
    off = parse_read_aware_cfg({})
    assert read_aware_cooldown_hours(48.0, 2, "read", _BK, off) == \
        backoff_cooldown_hours(48.0, 2, _BK)


def test_read_no_reply_steeper():
    # 已读不回：×4^streak（vs 基线 ×3^streak），封顶沿用 720
    assert read_aware_cooldown_hours(48.0, 1, "read", _BK, _RA) == 192.0
    assert backoff_cooldown_hours(48.0, 1, _BK) == 144.0
    assert read_aware_cooldown_hours(48.0, 2, "read", _BK, _RA) == 720.0  # 768→cap


def test_unread_first_miss_not_punished():
    # 未读首条：折算 streak int(1*0.5)=0 → 正常节奏（换个时段对方可能就看到）
    assert read_aware_cooldown_hours(48.0, 1, "unread", _BK, _RA) == 48.0


def test_unread_streak_two_hits_mute_floor():
    # 连续 2 条未读＝疑似被静音 → 月频地板 720
    assert read_aware_cooldown_hours(48.0, 2, "unread", _BK, _RA) == 720.0


def test_zero_streak_returns_base():
    assert read_aware_cooldown_hours(48.0, 0, "read", _BK, _RA) == 48.0


# ── store：最后出站已读态批量口 ──────────────────────────────────────────────

def _seed_conv(store, cid, *, platform="telegram"):
    with store._lock:
        store._conn.execute(
            "INSERT INTO conversations (conversation_id, platform, account_id,"
            " chat_key, chat_type, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cid, platform, "a1", cid.rsplit(":", 1)[-1], "private",
             NOW - 30 * 86400, NOW))
        store._conn.commit()


def _seed_out(store, cid, ts, status):
    import uuid
    with store._lock:
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " ts, ingested_at, status) VALUES (?,?,?,?,?,?)",
            (f"{cid}:{uuid.uuid4().hex[:8]}", cid, "out", ts, ts, status))
        store._conn.commit()


def test_store_read_state_map(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _seed_conv(store, "telegram:a1:1")
    _seed_out(store, "telegram:a1:1", NOW - 100, "read")
    _seed_conv(store, "telegram:a1:2")
    _seed_out(store, "telegram:a1:2", NOW - 200, "read")
    _seed_out(store, "telegram:a1:2", NOW - 100, "sent")  # 最后一条未读为准
    _seed_conv(store, "line:a1:3", platform="line")
    _seed_out(store, "line:a1:3", NOW - 100, "sent")
    _seed_conv(store, "telegram:a1:4")  # 无出站
    out = store.last_outbound_read_state_map(
        ["telegram:a1:1", "telegram:a1:2", "line:a1:3", "telegram:a1:4"])
    assert out.get("telegram:a1:1") == "read"
    assert out.get("telegram:a1:2") == "unread"
    assert out.get("line:a1:3") == ""      # 非 TG 无回执契约 → 未知
    assert "telegram:a1:4" not in out      # 无出站不出现
    assert store.last_outbound_read_state_map([]) == {}


# ── 规划器集成 ───────────────────────────────────────────────────────────────

def _conv(cid, *, read_state="", last_in=0.0):
    return {
        "conversation_id": cid, "platform": "telegram", "account_id": "a1",
        "chat_key": cid, "last_ts": NOW - 200 * _H, "last_direction": "out",
        "archived": False, "memory_key": cid, "stage": "", "intimacy": 50.0,
        "last_emotion": "", "last_in_ts": last_in, "read_state": read_state,
    }


def _opener(**_kw):
    return {"mode": "follow_up", "directive": "问候", "context_facts": []}


def _plan(convs, cooldown_map, read_aware):
    return plan_proactive_sends(
        convs, cooldown_map=cooldown_map, opener_fn=_opener, now=NOW,
        min_silent_hours=24.0, cooldown_hours=48.0, max_per_tick=10,
        quiet_start_hour=0, quiet_end_hour=0,
        backoff_cfg=_BK, read_aware_cfg=(_RA if read_aware else None))


def _entry(sent_ago_h, streak):
    ts = NOW - sent_ago_h * _H
    return {"ts": ts, "sent_ts": ts, "streak": streak, "last_text": "",
            "obs_n": 0, "obs_replied": 0}


def test_planner_read_blocked_unknown_allowed():
    # streak=1、距上次主动 150h：unknown → 144h 冷却已过（发）；read → 192h（拦）
    cd = {"c_read": _entry(150, 1), "c_unknown": _entry(150, 1)}
    plans = _plan([_conv("c_read", read_state="read"),
                   _conv("c_unknown", read_state="")], cd, read_aware=True)
    ids = [p["conversation_id"] for p in plans]
    assert "c_unknown" in ids and "c_read" not in ids
    # 分流关（None）＝两个都按旧退避（144h）→ 都发
    plans_off = _plan([_conv("c_read", read_state="read"),
                       _conv("c_unknown", read_state="")], cd, read_aware=False)
    assert len(plans_off) == 2


def test_planner_unread_first_miss_retries_sooner():
    # streak=1、距上次主动 60h：unknown → 144h（拦）；unread → 48h（发=换时段再试）
    cd = {"c_unread": _entry(60, 1), "c_unknown": _entry(60, 1)}
    plans = _plan([_conv("c_unread", read_state="unread"),
                   _conv("c_unknown", read_state="")], cd, read_aware=True)
    ids = [p["conversation_id"] for p in plans]
    assert ids == ["c_unread"]
    assert plans[0]["read_state"] == "unread"  # 观测字段随计划


def test_planner_unread_streak2_muted():
    # streak=2、距上次主动 500h：unknown → 432h（发）；unread → 720h 地板（拦）。
    # last_in 给「很久前回过一次」——否则先撞「从未回复出圈」（streak≥2）护栏。
    _li = NOW - 600 * _H
    cd = {"c_unread": _entry(500, 2), "c_unknown": _entry(500, 2)}
    plans = _plan([_conv("c_unread", read_state="unread", last_in=_li),
                   _conv("c_unknown", read_state="", last_in=_li)],
                  cd, read_aware=True)
    ids = [p["conversation_id"] for p in plans]
    assert "c_unknown" in ids and "c_unread" not in ids


# ── prompt 层：无压力文体行 ──────────────────────────────────────────────────

def test_prompt_pressure_free_line_only_for_read_no_reply():
    from src.utils.proactive_prompt import build_proactive_prompt
    base = {"mode": "gentle_checkin", "directive": "问候"}
    p1 = build_proactive_prompt(
        "小林", dict(base, read_state="read", unanswered_streak=1))
    assert "无压力" in p1 and "不要问号" in p1
    p2 = build_proactive_prompt(
        "小林", dict(base, read_state="unread", unanswered_streak=1))
    assert "无压力" not in p2
    p3 = build_proactive_prompt(
        "小林", dict(base, read_state="read", unanswered_streak=0))
    assert "无压力" not in p3  # 没有未回 streak 就没有「软拒绝」语义


# ── _send 接线静态钉（媒体降级被移除时先红）──────────────────────────────────

def test_send_path_wires_read_no_reply_media_downgrade():
    src = Path("src/companion/proactive_topic.py").read_text(encoding="utf-8")
    assert "_rn_forced_text" in src
    assert src.count("read_no_reply") >= 2  # voice + photo 两处 skip 归因
