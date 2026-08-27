# -*- coding: utf-8 -*-
"""user_clock 覆盖率盘点 CLI 门禁：桶语义 + 只读 + 软失败。

不变量：
- 群/频道/bot 会话不进分母（问候择时语义）；
- 负结果缓存（tz_confidence<0）≠ 可调度；仅国家（tz_hint 空）≠ 可调度；
- 偏移差 ≥3h 才计入 shifted_meaningful（开 schedule 会实质改变发送时段）；
- 库打不开/查询失败返回 {"error": ...} 绝不抛。
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from user_clock_coverage import collect, render  # noqa: E402

NOW = time.time()


def _mk_db(tmp_path, rows):
    db = tmp_path / "inbox.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conversations (conversation_id TEXT, platform TEXT,"
        " chat_type TEXT, peer_is_bot INTEGER, chat_key TEXT, language TEXT,"
        " last_ts REAL)")
    conn.execute(
        "CREATE TABLE conversation_meta (conversation_id TEXT, tz_hint TEXT,"
        " tz_confidence REAL, tz_source TEXT, tz_country TEXT,"
        " tz_offset REAL, tz_resolved_at REAL)")
    conn.execute(
        "CREATE TABLE messages (conversation_id TEXT, direction TEXT, ts REAL)")
    for r in rows:
        conn.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?,?)",
            (r["cid"], r.get("platform", "telegram"), r.get("chat_type", "private"),
             int(r.get("bot", 0)), r.get("chat_key", ""), r.get("language", ""),
             r.get("last_ts", NOW)))
        if r.get("meta"):
            m = r["meta"]
            conn.execute(
                "INSERT INTO conversation_meta VALUES (?,?,?,?,?,?,?)",
                (r["cid"], m.get("tz_hint", ""), m.get("tz_confidence", -1),
                 m.get("tz_source", ""), m.get("tz_country", ""),
                 m.get("tz_offset", 0.0), m.get("tz_resolved_at", NOW)))
        for ts in r.get("in_ts", []):
            conn.execute(
                "INSERT INTO messages VALUES (?,?,?)", (r["cid"], "in", ts))
    conn.commit()
    conn.close()
    return str(db)


def test_buckets_and_shift_semantics(tmp_path):
    db = _mk_db(tmp_path, [
        # 可调度：曼谷 UTC+7（与服务器 +8 差 1h → 不算实质位移）
        {"cid": "a", "meta": {"tz_hint": "Asia/Bangkok", "tz_confidence": 0.9,
                              "tz_source": "stated_city", "tz_country": "TH",
                              "tz_offset": 7.0}},
        # 可调度：温哥华 UTC-7（差 15h → 实质位移）
        {"cid": "b", "meta": {"tz_hint": "America/Vancouver", "tz_confidence": 0.8,
                              "tz_source": "phone_cc", "tz_country": "CA",
                              "tz_offset": -7.0}},
        # 仅国家（多时区国）：不可调度
        {"cid": "c", "meta": {"tz_hint": "", "tz_confidence": 0.5,
                              "tz_source": "phone_cc_country", "tz_country": "US",
                              "tz_offset": 0.0}},
        # 负结果缓存
        {"cid": "d", "meta": {"tz_hint": "", "tz_confidence": -1,
                              "tz_source": "", "tz_country": ""}},
        # 从未解析（无 meta 行）
        {"cid": "e"},
        # 群聊/bot：不进分母
        {"cid": "f", "chat_type": "group"},
        {"cid": "g", "bot": 1},
        # 窗外旧会话：不进分母
        {"cid": "h", "last_ts": NOW - 90 * 86400},
    ])
    st = collect(db, days=14, now=NOW)
    assert st["total"] == 5
    assert st["resolved"] == 4
    assert st["schedulable"] == 2
    assert st["country_only"] == 1
    assert st["negative_cached"] == 1
    assert st["never_resolved"] == 1
    assert st["shifted_meaningful"] == 1  # 只有温哥华
    assert st["by_source"]["stated_city"] == 1
    assert st["replace"] == 2            # stated_city + phone_cc（有 tz）
    assert st["narrow"] == 0
    assert st.get("advisory", 0) == 0
    txt = render(st)
    assert "可调度" in txt and "判词" in txt


def test_lang_default_is_advisory_not_schedulable(tmp_path):
    """语种默认钟有 IANA 名也不能进可调度——schedule_clock 只吃 replace。"""
    db = _mk_db(tmp_path, [
        {"cid": "a", "meta": {"tz_hint": "Asia/Bangkok", "tz_confidence": 0.3,
                              "tz_source": "lang_default", "tz_country": "TH",
                              "tz_offset": 7.0}},
    ])
    st = collect(db, days=14, now=NOW)
    assert st["total"] == 1 and st["resolved"] == 1
    assert st["schedulable"] == 0
    assert st["replace"] == 0 and st["narrow"] == 0
    assert st["advisory"] == 1


def test_open_failure_soft(tmp_path):
    st = collect(str(tmp_path / "nope" / "inbox.db"), days=14, now=NOW)
    assert "error" in st
    assert "ERROR" in render(st)


def test_recompute_mode_offline_signals(tmp_path):
    """离线重算口径（落库为空时的 dry-run）：WA 泰国号码 → 单时区国 replace 档
    可调度；Telegram 仅 th 语种 → advisory 档不可调度（只够节日用）。"""
    db = _mk_db(tmp_path, [
        # WhatsApp 泰国号（+66 单时区国）→ phone_cc replace Asia/Bangkok
        {"cid": "wa1", "platform": "whatsapp", "chat_key": "66812345678"},
        # Telegram 纯语种信号（th → 泰国默认）→ advisory，不参与调度
        {"cid": "tg1", "platform": "telegram", "language": "th"},
        # Telegram 无任何信号 → 推不出
        {"cid": "tg2", "platform": "telegram"},
    ])
    st = collect(db, days=14, now=NOW, recompute=True)
    assert st["mode"] == "recomputed"
    assert st["total"] == 3 and st["resolved"] == 3
    assert st["schedulable"] == 1          # 只有 WA 泰国号
    assert st["shifted_meaningful"] == 0   # 曼谷 UTC+7 与服务器 +8 差 1h < 3h
    assert st["negative_cached"] == 1      # tg2 推不出
    assert "phone_cc" in st["by_source"]
    txt = render(st)
    assert "离线重算" in txt
