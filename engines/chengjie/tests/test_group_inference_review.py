# -*- coding: utf-8 -*-
"""群判定复核 CLI 门禁（tools/group_inference_review.py）。

复核工具的价值全在「判词方向对不对」：把假阳（私聊被看成群 → 客户被群护栏
静默）和假阴（群被看成私聊 → 群闸被绕过）分开报，且**只在样本够时**下判词。
方向反了/样本不足还敢下结论，比没有工具更坏（会误导人去收紧或放松判据）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.group_inference_review import (  # noqa: E402
    WEAK_PLATFORMS,
    collect_conversations,
    summarize_group_inference,
)


def _row(plat, cid, chat_type, inbound_n, distinct, named=None):
    return {
        "platform": plat, "conversation_id": cid, "chat_type": chat_type,
        "display_name": cid, "inbound_n": inbound_n,
        "distinct_senders": distinct,
        "named_senders": distinct if named is None else named,
    }


def _plat(summary, name):
    return next(p for p in summary["platforms"] if p["platform"] == name)


def test_weak_platform_list_matches_runtime_guard():
    """复核口径与运行时行为必须同名单——一边收紧另一边没跟上＝复核在骗人。"""
    from src.inbox.automation_mode import _GROUP_EVIDENCE_WEAK_PLATFORMS
    assert set(WEAK_PLATFORMS) == set(_GROUP_EVIDENCE_WEAK_PLATFORMS)


def test_false_positive_group_flagged_on_weak_platform():
    s = summarize_group_inference([
        _row("instagram", "instagram:a:g1", "group", 9, 1, named=9),   # 假阳嫌疑
        _row("instagram", "instagram:a:g2", "group", 9, 3),            # 真群
        _row("instagram", "instagram:a:u1", "private", 9, 0, named=0),  # 正常私聊
    ])
    p = _plat(s, "instagram")
    assert p["weak_evidence"] is True
    assert p["group_convs"] == 2 and p["private_convs"] == 1
    assert p["suspect_false_group_n"] == 1
    assert p["suspect_false_group"][0]["conversation_id"] == "instagram:a:g1"
    assert p["suspect_false_group"][0]["why"] == "single_speaker"
    assert p["suspect_missed_group_n"] == 0 and p["gap_no_sender_n"] == 0
    assert any(v.startswith("⚠") and "假阳" in v for v in p["verdicts"])


def test_no_sender_name_is_a_gap_not_a_false_positive():
    """一个发言人名都没落库＝复核盲区（落库链缺陷），不许算判定假阳。

    首版把它并进假阳清单，生产首跑 telegram 12 条全是缺口，真信号
    （single_speaker）被淹没，判词还在教人「收紧 DOM 判据」——方向全错。
    """
    s = summarize_group_inference([
        _row("instagram", "instagram:a:g1", "group", 9, 0, named=0),
    ])
    p = _plat(s, "instagram")
    assert p["suspect_false_group_n"] == 0
    assert p["gap_no_sender_n"] == 1
    assert p["gap_no_sender"][0]["why"] == "no_sender_name"
    verdicts = " ".join(p["verdicts"])
    assert "复核盲区" in verdicts and "假阳嫌疑" not in verdicts
    # 弱证据平台的缺口要额外点名「假阳可能藏在里面」，别让人误读成健康
    assert "藏在" in verdicts
    assert not any(v.startswith("✅") and "假阳" in v for v in p["verdicts"])


def test_gap_on_hard_platform_still_flagged_but_not_as_inference_bug():
    """硬证据平台的缺口同样要报（群气泡渲染不出发言人），但不扯 DOM 判据。"""
    s = summarize_group_inference([
        _row("telegram", "telegram:a:-100", "group", 20, 0, named=0),
    ])
    p = _plat(s, "telegram")
    assert p["gap_no_sender_n"] == 1 and p["suspect_false_group_n"] == 0
    verdicts = " ".join(p["verdicts"])
    assert "复核盲区" in verdicts and "藏在" not in verdicts


def test_missed_group_flagged_on_any_platform():
    """私聊里冒出多个发言人＝群漏判（群闸被绕过），任何平台都要报。"""
    s = summarize_group_inference([
        _row("whatsapp", "whatsapp:a:u1", "private", 7, 3),
    ])
    p = _plat(s, "whatsapp")
    assert p["weak_evidence"] is False
    assert p["suspect_missed_group_n"] == 1
    assert p["suspect_missed_group"][0]["why"] == "multi_speaker"
    assert any("假阴" in v for v in p["verdicts"])


def test_hard_platform_single_speaker_group_is_not_alarmed():
    """地址自描述平台的「群里只有一个人说话」不是判定问题，判词必须降级为 ℹ。"""
    s = summarize_group_inference([
        _row("whatsapp", "whatsapp:a:g1", "group", 6, 1, named=6),
    ])
    p = _plat(s, "whatsapp")
    assert p["suspect_false_group_n"] == 1
    assert not any(v.startswith("⚠") and "假阳" in v for v in p["verdicts"])
    assert any(v.startswith("ℹ") for v in p["verdicts"])


def test_low_sample_leaves_verdict_blank():
    """入站不足 min_inbound：不进清单、不下判词（两条消息的群本就一个人说话）。"""
    s = summarize_group_inference([
        _row("instagram", "instagram:a:g1", "group", 2, 0, named=0),
    ], min_inbound=3)
    p = _plat(s, "instagram")
    assert p["group_convs"] == 1                 # 计数照算
    assert p["suspect_false_group_n"] == 0       # 但不下嫌疑
    assert p["gap_no_sender_n"] == 0
    assert any("样本不足" in v for v in p["verdicts"])


def test_channel_counts_as_group_and_empty_platform_dropped():
    s = summarize_group_inference([
        _row("telegram", "telegram:a:-100", "channel", 8, 0, named=0),
        _row("", "x", "group", 8, 0),
    ])
    assert [p["platform"] for p in s["platforms"]] == ["telegram"]
    assert _plat(s, "telegram")["group_convs"] == 1


def test_collect_is_read_only_and_tolerates_missing_db(tmp_path):
    assert collect_conversations(tmp_path / "nope.db", 30) is None
    assert not (tmp_path / "nope.db").exists(), "只读工具绝不许建库"


def test_collect_old_db_without_sender_name_returns_empty(tmp_path):
    """老库无 sender_name 列＝第二信号不存在 → 回空，绝不把全库报成嫌疑。"""
    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, "
        " platform TEXT, chat_type TEXT, display_name TEXT);"
        "CREATE TABLE messages (message_id TEXT PRIMARY KEY, "
        " conversation_id TEXT, direction TEXT, ts REAL);")
    con.commit()
    con.close()
    assert collect_conversations(db, 30) == []


def test_collect_end_to_end_counts_distinct_senders(tmp_path):
    db = tmp_path / "inbox.db"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE conversations (conversation_id TEXT PRIMARY KEY, "
        " platform TEXT, chat_type TEXT, display_name TEXT);"
        "CREATE TABLE messages (message_id TEXT PRIMARY KEY, "
        " conversation_id TEXT, direction TEXT, ts REAL, sender_name TEXT);")
    con.execute("INSERT INTO conversations VALUES ('instagram:a:g1','instagram','group','G')")
    now = 2_000_000_000.0
    rows = [("m1", "Alice"), ("m2", "Bob"), ("m3", ""), ("m4", "Alice")]
    for mid, sender in rows:
        con.execute("INSERT INTO messages VALUES (?,?,?,?,?)",
                    (mid, "instagram:a:g1", "in", now, sender))
    # 出站不计入发言人
    con.execute("INSERT INTO messages VALUES ('m5','instagram:a:g1','out',?,'Me')", (now,))
    con.commit()
    con.close()
    got = collect_conversations(db, 30)
    assert len(got) == 1
    r = got[0]
    assert r["inbound_n"] == 4 and r["distinct_senders"] == 2 and r["named_senders"] == 3
