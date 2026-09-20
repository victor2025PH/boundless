# -*- coding: utf-8 -*-
"""L-7 C-2（2026-09-06）：序号哨兵豁免本账号自己 API 出站的消息。

0906 03:43 实锤：值守用支持号经 /api/unified-inbox/send 发出 mid 1224，TelegramClient 收不到
自己 API 出站的 update → 1225 到达时 1224 成「洞」→ 宽限期后 P1 seq_gap 告警 + 云端补拉
（拉回来的是自己那条）。契约：收割前查 inbox messages（direction='out'，conversation_id =
telegram:<acct>:<chat>）——命中的号记 seq_gap_own 事件、不告警不补拉；真洞照旧；inbox 缺库 /
账号不明 / 查询异常 → 退回旧行为（宁可多告警不漏告警）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

CHAT = -100123
ACCT = "6834964252"
CFG_BASE = {
    "bug_intake": {
        "enabled": True,
        "groups": [CHAT],
        "support_accounts": ["777"],
        "max_replies_per_user_hour": 5,
        "max_replies_per_group_hour": 20,
        "collect_window_min": 30,
    }
}


class _BusStub:
    def __init__(self):
        self.published = []

    def publish(self, etype, payload):
        self.published.append((etype, payload))


@pytest.fixture()
def bi(tmp_path, monkeypatch):
    from src.ops import bug_intake
    from src.ops import bug_intake_backfill as bfm
    bug_intake.reset_state_for_tests()
    monkeypatch.setattr(bug_intake, "_db_path", lambda: tmp_path / "bug_intake.db")
    monkeypatch.setattr(bfm, "_SEEN_PATH_OVERRIDE", tmp_path / "bug_intake_seen.json")
    yield bug_intake
    bug_intake.reset_state_for_tests()


@pytest.fixture()
def bus(monkeypatch):
    from src.integrations.shared import event_bus as eb
    stub = _BusStub()
    monkeypatch.setattr(eb, "get_event_bus", lambda: stub)
    return stub


def _mk_inbox(path: Path, rows):
    """最小 inbox.db：messages(conversation_id, direction, platform_msg_id, text)。"""
    con = sqlite3.connect(str(path))
    con.execute("CREATE TABLE messages (message_id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " conversation_id TEXT, direction TEXT, platform_msg_id TEXT, text TEXT)")
    con.executemany("INSERT INTO messages (conversation_id, direction, platform_msg_id, text)"
                    " VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


def _cfg_with_inbox(db: Path):
    cfg = {k: dict(v) for k, v in CFG_BASE.items()}
    cfg["inbox"] = {"db_path": str(db)}
    return cfg


def _grace(bi):
    return 101.0 + bi.seq_grace_sec() + 0.5


def test_own_outbound_hole_is_exempt_no_alert_no_backfill(bi, bus, tmp_path):
    conv = f"telegram:{ACCT}:{CHAT}"
    db = _mk_inbox(tmp_path / "inbox.db", [
        (conv, "in", "1223", "这里标记：需人工…"),
        (conv, "out", "1224", "先认账：你 09-05 传的 28 份报告…"),     # 值守经 API 发的
        (conv, "in", "1225", "朋友 ↩ 00:56 你的图…"),
    ])
    cfg = _cfg_with_inbox(db)
    bi.note_group_msg_seq(cfg, CHAT, 1223, account_id=ACCT, now=100.0)
    assert bi.note_group_msg_seq(cfg, CHAT, 1225, account_id=ACCT, now=101.0) == [1224]
    due = bi.due_seq_gaps(cfg, now=_grace(bi))
    assert due == []                                   # 不返回补拉计划
    assert bus.published == []                         # 不告警
    assert bi.list_events(["seq_gap"]) == []
    own = bi.list_events(["seq_gap_own"])
    assert len(own) == 1 and "own=1224" in own[0]["detail"]
    st = bi.dump_stats()
    assert st["seq_gap_own"] == 1 and st["seq_gap"] == 0
    # 洞已从 pending 摘除：不会二次收割
    assert bi.seq_sentinel_snapshot()[f"{ACCT}:{CHAT}"]["pending"] == 0


def test_mixed_holes_alert_only_for_real_missing(bi, bus, tmp_path):
    conv = f"telegram:{ACCT}:{CHAT}"
    db = _mk_inbox(tmp_path / "inbox.db", [(conv, "out", "11", "自发")])
    cfg = _cfg_with_inbox(db)
    bi.note_group_msg_seq(cfg, CHAT, 10, account_id=ACCT, now=100.0)
    assert bi.note_group_msg_seq(cfg, CHAT, 13, account_id=ACCT, now=101.0) == [11, 12]
    due = bi.due_seq_gaps(cfg, now=_grace(bi))
    assert due == [{"account_id": ACCT, "chat_id": str(CHAT),
                    "missing_ids": [12], "last_mid": 13}]
    assert len(bus.published) == 1
    payload = bus.published[0][1]
    assert payload["kind"] == "seq_gap" and payload["missing_ids"] == [12]
    assert payload["rate_key"] == f"bug_intake:seq_gap:{CHAT}:12"
    evs = bi.list_events(["seq_gap"])
    assert len(evs) == 1 and "missing=12 " in evs[0]["detail"] + " "
    assert "own=11" in bi.list_events(["seq_gap_own"])[0]["detail"]


def test_inbound_row_with_same_mid_is_not_exempt(bi, bus, tmp_path):
    """只有 direction='out' 才算自发；别人的消息即便镜像在库里（in），漏收仍要告警。"""
    conv = f"telegram:{ACCT}:{CHAT}"
    db = _mk_inbox(tmp_path / "inbox.db", [(conv, "in", "1084", "用户提问原话")])
    cfg = _cfg_with_inbox(db)
    bi.note_group_msg_seq(cfg, CHAT, 1083, account_id=ACCT, now=100.0)
    bi.note_group_msg_seq(cfg, CHAT, 1085, account_id=ACCT, now=101.0)
    due = bi.due_seq_gaps(cfg, now=_grace(bi))
    assert due and due[0]["missing_ids"] == [1084]
    assert len(bus.published) == 1


def test_other_account_outbound_is_not_exempt(bi, bus, tmp_path):
    """conversation_id 按账号隔离：另一个支持号发的消息对本 worker 是真到达面，缺了就是漏收。"""
    db = _mk_inbox(tmp_path / "inbox.db", [(f"telegram:999:{CHAT}", "out", "1224", "别的号发的")])
    cfg = _cfg_with_inbox(db)
    bi.note_group_msg_seq(cfg, CHAT, 1223, account_id=ACCT, now=100.0)
    bi.note_group_msg_seq(cfg, CHAT, 1225, account_id=ACCT, now=101.0)
    due = bi.due_seq_gaps(cfg, now=_grace(bi))
    assert due and due[0]["missing_ids"] == [1224]
    assert len(bus.published) == 1


def test_missing_inbox_db_or_unknown_account_falls_back_to_alert(bi, bus, tmp_path):
    cfg = _cfg_with_inbox(tmp_path / "nope.db")          # 库不存在 → 旧行为
    bi.note_group_msg_seq(cfg, CHAT, 10, account_id=ACCT, now=100.0)
    bi.note_group_msg_seq(cfg, CHAT, 12, account_id=ACCT, now=101.0)
    assert bi.due_seq_gaps(cfg, now=_grace(bi))[0]["missing_ids"] == [11]
    assert len(bus.published) == 1
    # 账号为空 → 不查库直接旧行为
    assert bi._own_outbound_mids(cfg, "", str(CHAT), [11]) == set()
    assert bi._own_outbound_mids(cfg, ACCT, str(CHAT), []) == set()


def test_own_outbound_lookup_honours_default_config_dir(bi, tmp_path, monkeypatch):
    """不配 inbox.db_path 时按 licensing.data_paths.config_dir()/inbox.db 找（与 web_app 一致）。"""
    conv = f"telegram:{ACCT}:{CHAT}"
    _mk_inbox(tmp_path / "inbox.db", [(conv, "out", "77", "x"), (conv, "out", "abc", "坏 id")])
    from src.licensing import data_paths
    monkeypatch.setattr(data_paths, "config_dir", lambda: tmp_path)
    assert bi._own_outbound_mids(CFG_BASE, ACCT, str(CHAT), [77, 78]) == {77}


def test_telegram_client_sweep_still_wired_to_due_seq_gaps():
    """接线不变：收割仍走 due_seq_gaps（豁免在其内部），补拉只对返回的 missing_ids。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "client" / "telegram_client.py").read_text(
        encoding="utf-8")
    assert "due = due_seq_gaps(cfg, account_id=acct)" in src
    assert 'item.get("missing_ids")' in src
