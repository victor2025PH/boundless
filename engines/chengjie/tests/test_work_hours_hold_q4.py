# -*- coding: utf-8 -*-
"""Q-4（#267 D-Q1）commit 3：休息期扣留的日志 / 会话标签 / 过夜积压全部重拟。

- ``off_hours_cfg`` 默认 ``catch_up_regenerate_hours=0`` ⇒ ``catch_up_regenerate_all``；
- ``catch_up_regen_due``：0 档按「稿拟于本班次开始之前」判，无班次锚点不作废；
  N>0 档按稿龄；catch_up 关恒 False；
- ``off_hours_hold_info`` / ``log_off_hours_hold``：在班 None；休息期给 until/tz，
  日志 ``[work_schedule] hold=off_hours conv=… until=hh:mm tz=…``，同会话同到点只打一次；
- 标签「作息外 · 到点重新拟稿」：zh/en 词条齐、``strip_off_hours_hold_tags`` 任何语言都剥；
- ``InboxStore.conversations_with_pending_drafts`` 只回有 pending 稿的会话；
- autodraft_helpers 休息期路径调用 ``log_off_hours_hold``（源码接线断言）。
时间全部显式时区构造，与跑测试的机器时区无关。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import src.inbox.work_hours_gate as whg
from src.inbox.work_hours_gate import (
    OFF_HOURS_HOLD_TAG_KEY,
    OFF_HOURS_HOLD_TAG_ZH,
    catch_up_regen_due,
    log_off_hours_hold,
    off_hours_cfg,
    off_hours_hold_info,
    strip_off_hours_hold_tags,
)

_ROOT = Path(__file__).resolve().parents[1]
_NY = "America/New_York"


def _ts(y, m, d, hh, mm, tz=_NY):
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).timestamp()


def _ws(**over):
    cfg = {
        "enabled": True, "timezone": _NY,
        "default": {"workdays": [1, 2, 3, 4, 5, 6, 7], "start": "09:00", "end": "23:00"},
        "edge_jitter_min": 0, "crisis_bypass": True,
    }
    cfg.update(over)
    return cfg


# ── 过夜积压全部重拟 ──────────────────────────────────────────────

def test_regen_default_zero_means_all():
    oh = off_hours_cfg({})
    assert oh["catch_up_regenerate_hours"] == 0.0 and oh["catch_up_regenerate_all"] is True
    assert off_hours_cfg({"off_hours": {"catch_up_regenerate_hours": 2}})["catch_up_regenerate_all"] is False
    # 负数 / 坏值 → 0 档
    assert off_hours_cfg({"off_hours": {"catch_up_regenerate_hours": -3}})["catch_up_regenerate_all"] is True
    assert off_hours_cfg({"off_hours": {"catch_up_regenerate_hours": "x"}})["catch_up_regenerate_all"] is True


def test_regen_due_all_mode_uses_shift_anchor():
    oh = off_hours_cfg({})
    shift = _ts(2026, 9, 10, 9, 0)
    now = _ts(2026, 9, 10, 9, 5)
    # 昨夜 01:00 拟的 → 过夜积压 → 重拟
    assert catch_up_regen_due(oh, _ts(2026, 9, 10, 1, 0), now, shift) is True
    # 复班后 09:02 拟的 → 不是积压
    assert catch_up_regen_due(oh, _ts(2026, 9, 10, 9, 2), now, shift) is False
    # 无班次锚点：判不出 → 不作废（宁发陈稿不空转）
    assert catch_up_regen_due(oh, _ts(2026, 9, 10, 1, 0), now, 0) is False
    # 稿时间缺失
    assert catch_up_regen_due(oh, 0, now, shift) is False


def test_regen_due_threshold_mode_and_catch_up_off():
    oh = off_hours_cfg({"off_hours": {"catch_up_regenerate_hours": 2}})
    now = _ts(2026, 9, 10, 9, 5)
    assert catch_up_regen_due(oh, now - 3 * 3600, now, 0) is True
    assert catch_up_regen_due(oh, now - 1 * 3600, now, _ts(2026, 9, 10, 9, 0)) is False
    off = off_hours_cfg({"off_hours": {"catch_up": False}})
    assert catch_up_regen_due(off, now - 9 * 3600, now, _ts(2026, 9, 10, 9, 0)) is False


# ── hold_info / 日志 ─────────────────────────────────────────────

def test_hold_info_none_in_hours_and_when_disabled():
    ws = _ws()
    assert off_hours_hold_info(ws, "telegram", "a", now_ts=_ts(2026, 9, 10, 13, 0)) is None
    assert off_hours_hold_info(_ws(enabled=False), "telegram", "a", now_ts=_ts(2026, 9, 10, 3, 0)) is None


def test_hold_info_off_hours_gives_until_in_roster_tz():
    ws = _ws()
    # 纽约 03:00（= 上海 15:00）：按纽约作息是休息期 → until=09:00 纽约
    info = off_hours_hold_info(ws, "telegram", "a", now_ts=_ts(2026, 9, 10, 3, 0))
    assert info and info["tz"] == _NY and info["until_hhmm"] == "09:00"
    assert abs(info["until_ts"] - _ts(2026, 9, 10, 9, 0)) < 1
    # 反例：同一 epoch 若时区填了上海 → 上海 15:00 在班 → None（红线③ 的价值所在）
    assert off_hours_hold_info(_ws(timezone="Asia/Shanghai"), "telegram", "a",
                               now_ts=_ts(2026, 9, 10, 3, 0)) is None


def test_log_off_hours_hold_format_and_dedupe(caplog, monkeypatch):
    monkeypatch.setattr(whg, "_hold_logged", {})
    ws = _ws()
    now = _ts(2026, 9, 10, 3, 0)
    with caplog.at_level(logging.INFO, logger="src.inbox.work_hours_gate"):
        info = log_off_hours_hold("telegram:a:peer1", ws, "telegram", "a", now_ts=now)
        assert info and info["until_hhmm"] == "09:00"
        log_off_hours_hold("telegram:a:peer1", ws, "telegram", "a", now_ts=now + 600)
        log_off_hours_hold("telegram:a:peer2", ws, "telegram", "a", now_ts=now)
    lines = [r.getMessage() for r in caplog.records if "[work_schedule] hold=off_hours" in r.getMessage()]
    assert len(lines) == 2, lines
    assert re.fullmatch(
        r"\[work_schedule\] hold=off_hours conv=telegram:a:peer1 until=09:00 tz=America/New_York",
        lines[0]), lines[0]
    # 在班时不打、返回 None
    with caplog.at_level(logging.INFO, logger="src.inbox.work_hours_gate"):
        assert log_off_hours_hold("telegram:a:peer3", ws, "telegram", "a",
                                  now_ts=_ts(2026, 9, 10, 13, 0)) is None
    assert not any("peer3" in r.getMessage() for r in caplog.records)


# ── 标签 ──────────────────────────────────────────────────────────

def test_tag_i18n_and_strip():
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    assert zh[OFF_HOURS_HOLD_TAG_KEY] == OFF_HOURS_HOLD_TAG_ZH
    assert OFF_HOURS_HOLD_TAG_KEY in en and not re.search(r"[\u4e00-\u9fff]", en[OFF_HOURS_HOLD_TAG_KEY])
    assert "{until}" in zh[OFF_HOURS_HOLD_TAG_KEY + "_t"] and "{tz}" in en[OFF_HOURS_HOLD_TAG_KEY + "_t"]
    tags = ["VIP", OFF_HOURS_HOLD_TAG_ZH, en[OFF_HOURS_HOLD_TAG_KEY], "需人工"]
    assert strip_off_hours_hold_tags(tags) == ["VIP", "需人工"]
    assert strip_off_hours_hold_tags(None) == []


def test_tag_write_routes_strip_computed_tag():
    for rel in ("src/web/routes/unified_inbox_workspace_tags_routes.py",
                "src/web/routes/unified_inbox_batch_notif_routes.py"):
        src = (_ROOT / rel).read_text("utf-8")
        assert "strip_off_hours_hold_tags" in src, rel


def test_enrich_wiring_and_no_persist():
    src = (_ROOT / "src/web/routes/unified_inbox_read_routes.py").read_text("utf-8")
    body = src[src.index("def _enrich_chat_list("):]
    body = body[:body.index("\ndef ", 10)]
    assert "off_hours_hold_info" in body and "conversations_with_pending_drafts" in body
    assert 'c["off_hours_hold"]' in body
    assert "set_conv_tags" not in body  # 读侧绝不落库


def test_autodraft_logs_hold():
    src = (_ROOT / "src/inbox/autodraft_helpers.py").read_text("utf-8")
    assert "log_off_hours_hold(" in src
    # 仍先判 generate_drafts=false 的「不拟稿」分支，再打 hold 日志
    i_skip = src.index("休息期不拟稿 cid=")
    i_log = src.index("log_off_hours_hold(")
    assert i_skip < i_log


# ── store：pending 草稿集合 ───────────────────────────────────────

def test_store_conversations_with_pending_drafts(tmp_path):
    from src.inbox.store import InboxStore
    st = InboxStore(str(tmp_path / "inbox.db"))
    # a pending / b 已处置 / c 无稿
    did_a = st.upsert_draft({"source_kind": "inbox", "source_id": "a", "conversation_id": "a",
                             "platform": "telegram", "account_id": "x", "chat_key": "a",
                             "text": "hi", "status": "pending"})
    did_b = st.upsert_draft({"source_kind": "inbox", "source_id": "b", "conversation_id": "b",
                             "platform": "telegram", "account_id": "x", "chat_key": "b",
                             "text": "hi", "status": "pending"})
    assert did_a and did_b
    assert st.conversations_with_pending_drafts(["a", "b", "c"]) == {"a", "b"}
    st.update_draft_status(did_b, status="sent", decided_by="test")
    assert st.conversations_with_pending_drafts(["a", "b", "c"]) == {"a"}
    assert st.conversations_with_pending_drafts([]) == set()
