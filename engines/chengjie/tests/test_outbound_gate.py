"""冷启动隔离 / 导入无入站 / 额度闸 纯函数门禁（outbound_gate）。

重点覆盖 2026-08-04 智拓机 .198 事故的根因场景，以及**不该误拦**的真实关系。
"""
from __future__ import annotations

from src.inbox.outbound_gate import (
    COLD_START_WARMING,
    DISABLED,
    IMPORTED_NO_INBOUND,
    OK,
    QUOTA_EXHAUSTED,
    account_earliest_created,
    account_warming,
    imported_no_inbound,
    may_contact,
    record_suppression,
    reset_suppression_stats,
    resolve_cold_start_cfg,
    suppression_snapshot,
)

HOUR = 3600.0
DAY = 24 * HOUR


# ── resolve_cold_start_cfg ──────────────────────────────────────────────────
def test_cfg_defaults_when_missing():
    for cfg in (None, {}, {"companion": {}}, {"companion": {"proactive_topic": {}}}):
        c = resolve_cold_start_cfg(cfg)
        assert c["enabled"] is True
        assert c["warmup_hours"] == 72.0
        assert c["require_inbound_since_connect"] is True
        assert c["quota_gate"] is True


def test_cfg_overrides_and_bad_types():
    cfg = {"companion": {"proactive_topic": {"cold_start": {
        "enabled": False, "warmup_hours": "not-a-number",
        "require_inbound_since_connect": False, "quota_gate": False}}}}
    c = resolve_cold_start_cfg(cfg)
    assert c["enabled"] is False
    assert c["warmup_hours"] == 72.0  # 坏类型回落默认
    assert c["require_inbound_since_connect"] is False
    assert c["quota_gate"] is False


def test_cfg_warmup_zero_allowed():
    cfg = {"companion": {"proactive_topic": {"cold_start": {"warmup_hours": 0}}}}
    assert resolve_cold_start_cfg(cfg)["warmup_hours"] == 0.0


# ── account_warming ─────────────────────────────────────────────────────────
def test_warming_within_window():
    now = 1_700_000_000.0
    assert account_warming(now - 10 * HOUR, now, 72) is True


def test_warming_past_window():
    now = 1_700_000_000.0
    assert account_warming(now - 100 * HOUR, now, 72) is False


def test_warming_unknown_connect_is_conservative():
    # connected_at<=0（接入时刻未知）→ 保守判为预热中（宁少发不误发）。
    assert account_warming(0.0, 1_000_000.0, 72) is True
    assert account_warming(-1.0, 1_000_000.0, 72) is True


def test_warming_disabled_when_zero_hours():
    assert account_warming(0.0, 1_000_000.0, 0) is False


# ── imported_no_inbound ─────────────────────────────────────────────────────
def test_imported_when_no_inbound_since_connect():
    conn = 1_000_000.0
    assert imported_no_inbound(0.0, conn) is True            # 从未入站
    assert imported_no_inbound(conn - HOUR, conn) is True    # 入站早于接入
    assert imported_no_inbound(conn + HOUR, conn) is False   # 接入后真开过口


def test_imported_unknown_connect_no_extra_block():
    assert imported_no_inbound(0.0, 0.0) is False


# ── may_contact 组合判定 ─────────────────────────────────────────────────────
def _cfg(**over):
    base = {"enabled": True, "warmup_hours": 72.0,
            "require_inbound_since_connect": True, "quota_gate": True}
    base.update(over)
    return base


def test_disabled_gate_passes():
    v = may_contact({"last_in_ts": 0}, connected_at=0, now=1_000_000.0,
                    cfg=_cfg(enabled=False))
    assert v.ok is True and v.reason == DISABLED


def test_quota_exhausted_blocks_when_gated():
    now = 1_700_000_000.0
    v = may_contact({"last_in_ts": now}, connected_at=now - 100 * DAY, now=now,
                    cfg=_cfg(), quota_exhausted=True)
    assert v.ok is False and v.reason == QUOTA_EXHAUSTED


def test_quota_exhausted_ignored_when_gate_off():
    now = 1_700_000_000.0
    v = may_contact({"last_in_ts": now}, connected_at=now - 100 * DAY, now=now,
                    cfg=_cfg(quota_gate=False), quota_exhausted=True)
    assert v.ok is True and v.reason == OK


def test_new_account_blast_is_blocked():
    # 事故复刻：新号刚接入（connected_at≈now），一条导入的老会话。
    now = 1_700_000_000.0
    v = may_contact({"last_in_ts": now - 90 * DAY}, connected_at=now - 1 * HOUR,
                    now=now, cfg=_cfg())
    assert v.ok is False and v.reason == COLD_START_WARMING


def test_imported_relationship_blocked_after_warmup():
    # 账号已过预热窗，但这条会话对方从未在本系统开口 → 仍不冷开场。
    now = 1_700_000_000.0
    conn = now - 100 * DAY
    v = may_contact({"last_in_ts": conn - DAY}, connected_at=conn, now=now,
                    cfg=_cfg())
    assert v.ok is False and v.reason == IMPORTED_NO_INBOUND


def test_real_quiet_relationship_allowed():
    # 老账号 + 对方接入后真的聊过（哪怕已沉默数月）→ 正是沉默回访要服务的场景。
    now = 1_700_000_000.0
    conn = now - 200 * DAY
    v = may_contact({"last_in_ts": now - 30 * DAY}, connected_at=conn, now=now,
                    cfg=_cfg())
    assert v.ok is True and v.reason == OK


def test_require_inbound_off_allows_imported_after_warmup():
    now = 1_700_000_000.0
    conn = now - 100 * DAY
    v = may_contact({"last_in_ts": 0}, connected_at=conn, now=now,
                    cfg=_cfg(require_inbound_since_connect=False))
    assert v.ok is True and v.reason == OK


# ── account_earliest_created ────────────────────────────────────────────────
def test_earliest_created_per_account():
    rows = [
        {"platform": "telegram", "account_id": "A", "created_at": 100.0},
        {"platform": "telegram", "account_id": "A", "created_at": 50.0},
        {"platform": "telegram", "account_id": "B", "created_at": 999.0},
        {"platform": "whatsapp", "account_id": "A", "created_at": 10.0},
    ]
    got = account_earliest_created(rows)
    assert got[("telegram", "A")] == 50.0
    assert got[("telegram", "B")] == 999.0
    assert got[("whatsapp", "A")] == 10.0


def test_earliest_created_skips_bad_rows():
    rows = [
        {"platform": "telegram", "account_id": "A"},          # 无 created_at
        {"platform": "telegram", "account_id": "A", "created_at": 0},   # 0 跳过
        {"platform": "telegram", "account_id": "A", "created_at": "x"}, # 非法跳过
        {"platform": "telegram", "account_id": "A", "created_at": 77.0},
    ]
    assert account_earliest_created(rows) == {("telegram", "A"): 77.0}
    assert account_earliest_created(None) == {}
    assert account_earliest_created([]) == {}


def test_closure_composition_198_scenario():
    """复刻 proactive_topic 闭包里的判定链：account_earliest_created → observe →
    may_contact，验证新号会话被压、老关系放行——不依赖重型 assistant harness。"""
    from src.inbox.account_connection import AccountConnectionLog
    now = 1_700_000_000.0
    rows = [
        # 新号：所有会话都是刚同步的占位（created_at≈now），一条导入的老会话
        {"platform": "telegram", "account_id": "NEW", "conversation_id": "c1",
         "created_at": now - 1 * HOUR, "last_in_ts": now - 90 * DAY},
        {"platform": "telegram", "account_id": "NEW", "conversation_id": "c2",
         "created_at": now - 2 * HOUR, "last_in_ts": now - 30 * DAY},
        # 老号：最早会话在很久以前，且对方接入后真的聊过（沉默 20 天的真实关系）
        {"platform": "telegram", "account_id": "OLD", "conversation_id": "c3",
         "created_at": now - 300 * DAY, "last_in_ts": now - 20 * DAY},
    ]
    earliest = account_earliest_created(rows)
    log = AccountConnectionLog(None)
    cfg = _cfg()
    kept, blocked = [], {}
    for r in rows:
        conn = log.observe(r["platform"], r["account_id"],
                           earliest.get((r["platform"], r["account_id"]), 0.0),
                           now=now)
        v = may_contact({"last_in_ts": r["last_in_ts"]}, connected_at=conn,
                        now=now, cfg=cfg)
        if v.ok:
            kept.append(r["conversation_id"])
        else:
            blocked[r["conversation_id"]] = v.reason
    assert kept == ["c3"]                          # 只有老号真实关系放行
    assert blocked["c1"] == COLD_START_WARMING      # 新号预热窗压住
    assert blocked["c2"] == COLD_START_WARMING


# ── 观测计数 ─────────────────────────────────────────────────────────────────
def test_suppression_stats_roundtrip():
    reset_suppression_stats()
    record_suppression(COLD_START_WARMING)
    record_suppression(COLD_START_WARMING)
    record_suppression(IMPORTED_NO_INBOUND)
    record_suppression("")  # 空 reason 不计
    snap = suppression_snapshot()
    assert snap == {COLD_START_WARMING: 2, IMPORTED_NO_INBOUND: 1}
    reset_suppression_stats()
    assert suppression_snapshot() == {}
