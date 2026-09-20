# -*- coding: utf-8 -*-
"""P1 (2026-08-15) `dumpsys notification` 预扫解析纯函数测试。

全离线夹具, 覆盖: 目标包过滤 / group summary 剔除 / redact(text 缺失) /
系统 UI 噪声剔除 / 汇总 title 剔除 / peer 去重。
"""
from __future__ import annotations

from src.app_automation.messenger_notifications import (
    active_peer_names,
    extract_active_peers,
    offscreen_search_targets,
    parse_dumpsys_notifications,
)

# 真实感 dumpsys notification --noredact 片段
_DUMP = """Current Notification List:
  NotificationRecord(0xabc pkg=com.facebook.orca user=UserHandle{0} id=1 tag=null key=0|com.facebook.orca|1|null|10234 groupKey=...)
    uid=10234 opPkg=com.facebook.orca
    flags=0x8
    extras={
      android.title=山田花子 (String)
      android.text=你好呀，在吗 (String)
      android.showChronometer=false (Boolean)
    }
  NotificationRecord(0xdef pkg=com.facebook.orca user=UserHandle{0} id=0 tag=null key=0|com.facebook.orca|0|group|10234)
    flags=0x208
    extras={
      android.title=3 条新消息 (String)
      android.text=Messenger (String)
    }
  NotificationRecord(0x111 pkg=com.facebook.orca user=UserHandle{0} id=2 key=0|com.facebook.orca|2|null|10234)
    flags=0x8
    extras={
      android.title=佐藤美咲 (String)
      android.text=null
    }
  NotificationRecord(0x999 pkg=com.android.systemui id=5 key=0|com.android.systemui|5|null|1000)
    flags=0x2
    extras={
      android.title=充电中 (String)
      android.text=已充电 80% (String)
    }
"""


class TestParse:
    def test_only_target_package(self):
        recs = parse_dumpsys_notifications(_DUMP)
        # 3 条 orca, systemui 被过滤
        assert len(recs) == 3
        assert all(r.package == "com.facebook.orca" for r in recs)

    def test_title_text_extracted(self):
        recs = parse_dumpsys_notifications(_DUMP)
        first = recs[0]
        assert first.title == "山田花子"
        assert first.text == "你好呀，在吗"
        assert first.key == "0|com.facebook.orca|1|null|10234"

    def test_group_summary_flagged(self):
        recs = parse_dumpsys_notifications(_DUMP)
        summary = [r for r in recs if r.title == "3 条新消息"][0]
        assert summary.is_group_summary is True  # flags 0x208 含 0x200

    def test_redacted_text_becomes_empty(self):
        recs = parse_dumpsys_notifications(_DUMP)
        sato = [r for r in recs if r.title == "佐藤美咲"][0]
        assert sato.text == ""  # android.text=null → ""

    def test_empty_and_garbage_safe(self):
        assert parse_dumpsys_notifications("") == []
        assert parse_dumpsys_notifications("random garbage no records") == []
        # 不抛
        parse_dumpsys_notifications(None)  # type: ignore[arg-type]


class TestExtractActivePeers:
    def test_summary_and_invalid_filtered(self):
        recs = parse_dumpsys_notifications(_DUMP)
        peers = extract_active_peers(recs)
        names = [p.peer for p in peers]
        # 山田花子 + 佐藤美咲; "3 条新消息" 被 group_summary + summary-title 双剔
        assert "山田花子" in names
        assert "佐藤美咲" in names
        assert "3 条新消息" not in names
        assert len(names) == 2

    def test_redacted_peer_still_surfaced(self):
        """MIUI redact 掉正文, 但只要有 title 就仍捞出该 peer (有活动 = 要看)."""
        recs = parse_dumpsys_notifications(_DUMP)
        peers = {p.peer: p for p in extract_active_peers(recs)}
        assert peers["佐藤美咲"].text == ""  # 正文没了但人还在

    def test_valid_name_fn_injection(self):
        recs = parse_dumpsys_notifications(_DUMP)

        def only_yamada(s):
            return s == "山田花子"
        peers = extract_active_peers(recs, valid_name_fn=only_yamada)
        assert [p.peer for p in peers] == ["山田花子"]

    def test_active_peer_names_set(self):
        recs = parse_dumpsys_notifications(_DUMP)
        assert active_peer_names(recs) == {"山田花子", "佐藤美咲"}

    def test_dedup_same_peer_keeps_latest(self):
        dump = _DUMP + """  NotificationRecord(0xaaa pkg=com.facebook.orca id=9 key=0|com.facebook.orca|9|null|10234)
    flags=0x8
    extras={
      android.title=山田花子 (String)
      android.text=第二条新消息 (String)
    }
"""
        recs = parse_dumpsys_notifications(dump)
        peers = {p.peer: p for p in extract_active_peers(recs)}
        assert peers["山田花子"].text == "第二条新消息"  # 保留最后出现
        assert len(peers) == 2  # 仍去重成 2 人


# ─── P2: 屏外搜索目标选择 ────────────────────────────────────────────
class TestOffscreenTargets:
    def test_only_offscreen_kept(self):
        # B 在列表 → 已由 should_open 处理; 只搜屏外 A/C
        assert offscreen_search_targets(["A", "B", "C"], ["B"], 5) == ["A", "C"]

    def test_limit_capped(self):
        assert offscreen_search_targets(["A", "B", "C"], [], 2) == ["A", "B"]

    def test_zero_disables(self):
        assert offscreen_search_targets(["A", "B"], [], 0) == []

    def test_dedup_preserves_order(self):
        assert offscreen_search_targets(["A", "A", "B"], [], 5) == ["A", "B"]

    def test_empty_inputs(self):
        assert offscreen_search_targets([], ["X"], 3) == []
        assert offscreen_search_targets(["A"], ["A"], 3) == []  # 全在列表
