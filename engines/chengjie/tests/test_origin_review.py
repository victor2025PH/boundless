"""跨平台档案灰度裁决 CLI（tools/origin_review.py）门禁：只读台账/对比/判词。

夹具库全建在 tmp_path（contacts.db + inbox.db 最小 schema），CLI 只读打开；
时间锚 now（防夹具时间炸弹——窗口判定全部相对当前时钟）。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))

from tools.origin_review import (  # noqa: E402
    MIN_COHORT,
    cohort_readout,
    ledger_readout,
    verdict_lines,
)


def _mk_contacts_db(tmp_path, *, profiles=0, imports=(0, 0)):
    db = tmp_path / "contacts.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE contact_profiles (contact_id TEXT PRIMARY KEY,"
        " ai_visible INTEGER DEFAULT 1, created_at INTEGER DEFAULT 0);"
        "CREATE TABLE contact_memory_imports (batch_id TEXT PRIMARY KEY,"
        " contact_id TEXT, status TEXT, facts_written INTEGER DEFAULT 0);")
    now = int(time.time())
    for i in range(profiles):
        conn.execute("INSERT INTO contact_profiles VALUES (?, 1, ?)",
                     (f"c{i}", now - i * 60))
    confirmed, revoked = imports
    for i in range(confirmed):
        conn.execute("INSERT INTO contact_memory_imports VALUES (?, ?, 'confirmed', 3)",
                     (f"b{i}", f"c{i % max(profiles, 1)}"))
    for i in range(revoked):
        conn.execute("INSERT INTO contact_memory_imports VALUES (?, ?, 'revoked', 0)",
                     (f"r{i}", "c0"))
    conn.commit()
    conn.close()
    return db


def _mk_inbox_db(tmp_path, *, convs):
    """convs: list of (conversation_id, contact_id, has_echo, active_7d)。

    时间设计（全部锚 now，防夹具时间炸弹）：active 组出站 3 天前、回音 1 小时前
    （7 天窗内）；非 active 组出站 10 天前、回音 9 天前（在 14 天裁决窗内构成
    「有回音」，但踩不进 7 天活跃窗）。
    """
    db = tmp_path / "inbox.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY,"
        " contact_id TEXT DEFAULT '', chat_type TEXT DEFAULT 'private');"
        "CREATE TABLE messages (message_id TEXT PRIMARY KEY,"
        " conversation_id TEXT, direction TEXT, ts REAL);")
    now = time.time()
    mid = 0
    for cid, contact, echo, active in convs:
        conn.execute("INSERT INTO conversations VALUES (?, ?, 'private')",
                     (cid, contact))
        mid += 1
        out_ts = now - (3 * 86400 if active else 10 * 86400)
        conn.execute("INSERT INTO messages VALUES (?, ?, 'out', ?)",
                     (f"m{mid}", cid, out_ts))
        if echo:
            mid += 1
            ts_in = now - (3600 if active else 9 * 86400)
            conn.execute("INSERT INTO messages VALUES (?, ?, 'in', ?)",
                         (f"m{mid}", cid, ts_in))
    conn.commit()
    conn.close()
    return db


def test_ledger_readout_missing_table_and_counts(tmp_path):
    # 库不存在 → 全零
    empty = ledger_readout(tmp_path / "nope.db")
    assert empty["profiles"] == 0 and empty["imports_confirmed"] == 0
    db = _mk_contacts_db(tmp_path, profiles=3, imports=(4, 2))
    led = ledger_readout(db)
    assert led["profiles"] == 3 and led["profiles_ai_visible"] == 3
    assert led["imports_confirmed"] == 4 and led["imports_revoked"] == 2
    assert led["facts_written"] == 12
    assert led["revoke_rate"] == round(2 / 6, 3)
    assert led["first_profile_at"]
    assert len(led["profile_contact_ids"]) == 3


def test_cohort_readout_partitions_and_rates(tmp_path):
    convs = (
        [(f"t:d:p{i}", f"c{i % 2}", True, True) for i in range(6)]      # 有档案组（c0/c1）
        + [(f"t:d:q{i}", f"x{i}", i % 2 == 0, False) for i in range(6)]  # 无档案组
    )
    db = _mk_inbox_db(tmp_path, convs=convs)
    out = cohort_readout(db, ["c0", "c1"], days=14)
    wp, np_ = out["with_profile"], out["without_profile"]
    assert wp["convs"] == 6 and np_["convs"] == 6
    assert wp["echo_rate"] == 1.0
    assert np_["echo_rate"] == 0.5
    assert wp["active_7d_rate"] == 1.0 and np_["active_7d_rate"] == 0.0


def test_verdicts_zero_usage_sample_floor_and_direction(tmp_path):
    # 零使用
    led0 = ledger_readout(tmp_path / "nope.db")
    v0 = verdict_lines(led0, {
        "with_profile": {"convs": 0, "echo_rate": None, "active_7d_rate": None},
        "without_profile": {"convs": 0, "echo_rate": None, "active_7d_rate": None}})
    assert any("零使用" in ln for ln in v0)
    # 样本不足
    led = {"profiles": 5, "profiles_ai_visible": 5, "imports_confirmed": 1,
           "imports_revoked": 0, "facts_written": 3, "revoke_rate": 0.0,
           "first_profile_at": "2026-08-18 00:00"}
    small = {"with_profile": {"convs": MIN_COHORT - 1, "echoed": 0, "active_7d": 0,
                              "echo_rate": 0.5, "active_7d_rate": 0.5},
             "without_profile": {"convs": 50, "echoed": 0, "active_7d": 0,
                                 "echo_rate": 0.5, "active_7d_rate": 0.5}}
    v1 = verdict_lines(led, small)
    assert any("样本不足" in ln for ln in v1)
    # 方向性正向
    big = {"with_profile": {"convs": 30, "echoed": 24, "active_7d": 20,
                            "echo_rate": 0.8, "active_7d_rate": 0.66},
           "without_profile": {"convs": 30, "echoed": 15, "active_7d": 10,
                               "echo_rate": 0.5, "active_7d_rate": 0.33}}
    v2 = verdict_lines(led, big)
    assert any("方向性正向" in ln for ln in v2)
    # 反向 → 选择偏差提醒而非关功能
    neg = {"with_profile": {"convs": 30, "echoed": 9, "active_7d": 9,
                            "echo_rate": 0.3, "active_7d_rate": 0.3},
           "without_profile": {"convs": 30, "echoed": 18, "active_7d": 15,
                               "echo_rate": 0.6, "active_7d_rate": 0.5}}
    v3 = verdict_lines(led, neg)
    assert any("选择偏差" in ln for ln in v3)
    # 高撤销率提醒
    led_hi = dict(led, imports_confirmed=4, imports_revoked=3, revoke_rate=3 / 7)
    v4 = verdict_lines(led_hi, big)
    assert any("撤销率偏高" in ln for ln in v4)
