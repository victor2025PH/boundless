# -*- coding: utf-8 -*-
"""跨重启提醒状态账本（src/inbox/remind_ledger.py）门禁——2026-09-10 运维群降噪 P0.2/P0.3。"""
from __future__ import annotations

import json

from src.inbox.remind_ledger import FIRST, HOLD, REMIND, RemindLedger, fingerprint


def test_first_then_hold_then_remind_and_fingerprint_relaxes_interval():
    led = RemindLedger(None)
    t0 = 1_700_000_000.0
    assert led.decide("k", now=t0, after_sec=0, interval_sec=3600) == FIRST
    led.mark_sent("k", now=t0, fp="a")
    assert led.decide("k", now=t0 + 600, interval_sec=3600, fp="a") == HOLD
    # 指纹相同：常规间隔到了也不提，要等 unchanged 间隔
    assert led.decide("k", now=t0 + 3700, interval_sec=3600, fp="a",
                      unchanged_interval_sec=86400) == HOLD
    assert led.decide("k", now=t0 + 86401, interval_sec=3600, fp="a",
                      unchanged_interval_sec=86400) == REMIND
    # 指纹变了：常规间隔即重提
    assert led.decide("k", now=t0 + 3700, interval_sec=3600, fp="b",
                      unchanged_interval_sec=86400) == REMIND
    # 不给指纹：退回纯间隔语义
    assert led.decide("k", now=t0 + 3700, interval_sec=3600) == REMIND


def test_after_sec_uses_persisted_first_seen():
    led = RemindLedger(None)
    t0 = 1_700_000_000.0
    assert led.decide("k", now=t0, after_sec=1800, interval_sec=3600) == HOLD
    assert led.first_seen("k") == t0
    assert led.decide("k", now=t0 + 1799, after_sec=1800, interval_sec=3600) == HOLD
    assert led.decide("k", now=t0 + 1800, after_sec=1800, interval_sec=3600) == FIRST


def test_resolve_reports_whether_alerted_and_clears():
    led = RemindLedger(None)
    led.observe("k", now=1.0)
    assert led.resolve("k") is False          # 只观察过、没首提 → 不发恢复
    led.decide("k", now=1.0, interval_sec=10)
    led.mark_sent("k", now=1.0)
    assert led.resolve("k") is True
    assert led.alerted("k") is False and led.first_seen("k") == 0.0


def test_persist_roundtrip_and_stale_entries_dropped(tmp_path):
    p = tmp_path / "health_remind_state.json"
    led = RemindLedger(p)
    t0 = 1_700_000_000.0
    led.decide("live", now=t0, interval_sec=10)
    led.mark_sent("live", now=t0, fp="x")
    led.set_meta("live", "kind", "down")
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["ancient"] = {"alerted": True, "first_seen": t0 - 90 * 86400,
                      "last_remind": t0 - 90 * 86400, "touched": t0 - 90 * 86400}
    p.write_text(json.dumps(raw), encoding="utf-8")

    led2 = RemindLedger(p, now=t0 + 60)
    assert led2.alerted("live") and led2.last_remind("live") == t0
    assert led2.meta("live", "kind") == "down"
    assert led2.get("live")["fingerprint"] == "x"
    assert "ancient" not in led2.keys()


def test_corrupt_file_degrades_to_empty(tmp_path):
    p = tmp_path / "health_remind_state.json"
    p.write_text("{not json", encoding="utf-8")
    led = RemindLedger(p)
    assert led.keys() == []
    led.mark_sent("k", now=5.0)
    assert json.loads(p.read_text(encoding="utf-8"))["k"]["alerted"] is True


def test_since_backfills_first_seen_and_only_moves_earlier():
    """条件真实成立时刻：首次登记以 since 为 first_seen；已有条目只许往早修正；未来值忽略。"""
    led = RemindLedger(None)
    t0 = 1_700_000_000.0
    led.decide("k", now=t0, interval_sec=10, since=t0 - 30 * 86400)
    assert led.first_seen("k") == t0 - 30 * 86400
    led.decide("k", now=t0 + 60, interval_sec=10, since=t0 - 10 * 86400)   # 更晚 → 不动
    assert led.first_seen("k") == t0 - 30 * 86400
    led.decide("k", now=t0 + 120, interval_sec=10, since=t0 - 40 * 86400)  # 更早 → 修正
    assert led.first_seen("k") == t0 - 40 * 86400
    led.decide("j", now=t0, interval_sec=10, since=t0 + 999)               # 未来 → 忽略
    assert led.first_seen("j") == t0
    led.decide("i", now=t0, interval_sec=10, since="bad")                  # 垃圾 → 忽略
    assert led.first_seen("i") == t0


def test_open_items_lists_alerted_with_summary_oldest_first():
    """每日摘要的数据面：只列仍在告警中的键，带外发时登记的一行人话，按首次成立升序。"""
    led = RemindLedger(None)
    t0 = 1_700_000_000.0
    led.decide("late", now=t0 + 100, interval_sec=10)
    led.mark_sent("late", now=t0 + 100, summary="客户消息 1 条没人回")
    led.decide("early", now=t0, interval_sec=10)
    led.mark_sent("early", now=t0, summary="待审草稿 5 条无人处理（最久 30 天）")
    led.observe("watching", now=t0)          # 只观察、未首提 → 不在摘要里
    items = led.open_items(now=t0 + 200)
    assert [x["key"] for x in items] == ["early", "late"]
    assert items[0]["summary"].startswith("待审草稿 5 条") and items[0]["first_seen"] == t0
    led.resolve("early")
    assert [x["key"] for x in led.open_items()] == ["late"]
    # summary 会截断到 200 字，防长文塞爆摘要卡
    led.mark_sent("late", now=t0 + 300, summary="x" * 500)
    assert len(led.open_items()[0]["summary"]) == 200


def test_fingerprint_is_order_and_key_stable():
    assert fingerprint(["b", "a"]) != fingerprint(["a", "b"])
    assert fingerprint({"x": 1, "y": 2}) == fingerprint({"y": 2, "x": 1})
    assert len(fingerprint("z")) == 16
