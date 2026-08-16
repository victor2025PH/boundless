# -*- coding: utf-8 -*-
"""P0 (2026-08-15) Messenger 读取判定纯函数层测试。

覆盖 messenger_inbox_parse 的全部判定分支。这些是「消息不读 / 人名错乱 /
消息归属错」三类事故的判定核心，全部离线夹具、零真机依赖。
"""
from __future__ import annotations

import pytest

from src.app_automation.messenger_inbox_parse import (
    BubbleRow,
    TitleCandidate,
    detect_unread,
    is_ui_noise,
    looks_outgoing,
    looks_time_segment,
    message_fingerprint,
    name_from_desc,
    names_match,
    normalize_name,
    pick_latest_incoming,
    pick_thread_title,
    preview_core,
    preview_fingerprint,
)


# ─── detect_unread ───────────────────────────────────────────────────
class TestDetectUnread:
    def test_selected_true(self):
        v = detect_unread("山田花子", selected=True)
        assert v.is_unread and v.reason == "selected"

    def test_lead_dot(self):
        v = detect_unread("• 山田花子")
        assert v.is_unread and v.reason == "lead_dot"

    def test_middle_dot_is_separator_not_unread(self):
        # 「名字 • 5分钟前」的中间 • 是分隔符，绝不能判未读（原实现的坑）
        v = detect_unread("山田花子 • 5分钟前")
        assert not v.is_unread

    def test_desc_keyword_unread_en(self):
        v = detect_unread("Alice", row_desc="Alice, 2 unread messages, 5m")
        assert v.is_unread and v.reason == "kw"

    def test_desc_keyword_new_messages_number(self):
        v = detect_unread("Bob", row_desc="Bob, 3 new messages")
        assert v.is_unread

    def test_desc_keyword_zh(self):
        v = detect_unread("小明", row_desc="小明，3 条新消息，刚刚")
        assert v.is_unread

    def test_desc_keyword_ja(self):
        v = detect_unread("さくら", row_desc="さくら、新着メッセージ")
        assert v.is_unread

    def test_sibling_dot_view(self):
        v = detect_unread("Carol", sibling_texts=["●"])
        assert v.is_unread and v.reason == "sib_dot"

    def test_no_signal_is_read(self):
        v = detect_unread("David", row_desc="David, 昨天, 你: 好的")
        assert not v.is_unread

    def test_active_now_not_unread(self):
        # 「在线」不是未读
        v = detect_unread("Eve", row_desc="Eve, Active now")
        assert not v.is_unread

    def test_empty_safe(self):
        assert not detect_unread("").is_unread


# ─── looks_outgoing ──────────────────────────────────────────────────
class TestLooksOutgoing:
    @pytest.mark.parametrize("s", [
        "You: hey", "You sent a photo", "你: 好的", "你发送了一张图片",
        "已发送", "あなた: こんにちは", "나: 안녕",
    ])
    def test_outgoing_prefixes(self, s):
        assert looks_outgoing(s)

    def test_thank_you_is_not_outgoing(self):
        # 关键回归：原实现 "you" 子串会把对方的 "thank you" 整条丢掉
        assert not looks_outgoing("thank you so much!")

    def test_incoming_plain(self):
        assert not looks_outgoing("你今天有空吗")

    def test_desc_channel(self):
        assert looks_outgoing("", desc="You sent: ok")

    def test_empty(self):
        assert not looks_outgoing("")


# ─── pick_latest_incoming ────────────────────────────────────────────
class TestPickLatestIncoming:
    def test_picks_bottom_most_incoming(self):
        rows = [
            BubbleRow(top=100, x_center=200, text="早上好"),
            BubbleRow(top=500, x_center=200, text="在吗"),
            BubbleRow(top=300, x_center=200, text="你好呀"),
        ]
        assert pick_latest_incoming(rows, screen_w=1080) == "在吗"

    def test_skips_own_right_side_bubble(self):
        rows = [
            BubbleRow(top=100, x_center=200, text="对方消息"),
            BubbleRow(top=500, x_center=900, text="我的回复"),  # 靠右=自己
        ]
        assert pick_latest_incoming(rows, screen_w=1080) == "对方消息"

    def test_skips_outgoing_prefix_even_if_left(self):
        rows = [
            BubbleRow(top=100, x_center=200, text="对方说的话"),
            BubbleRow(top=500, x_center=200, text="You: my reply"),  # 前缀=自己
        ]
        assert pick_latest_incoming(rows, screen_w=1080) == "对方说的话"

    def test_thank_you_not_dropped(self):
        # 原实现 "you" 子串会丢掉这条 → 漏读
        rows = [BubbleRow(top=300, x_center=200, text="thank you so much")]
        assert pick_latest_incoming(rows, screen_w=1080) == "thank you so much"

    def test_cjk_single_char_kept(self):
        rows = [BubbleRow(top=300, x_center=200, text="好")]
        assert pick_latest_incoming(rows, screen_w=1080) == "好"

    def test_single_ascii_dropped(self):
        rows = [BubbleRow(top=300, x_center=200, text="x")]
        assert pick_latest_incoming(rows, screen_w=1080) == ""

    def test_desc_incoming_beats_geometry(self):
        # content-desc 明确对方发来 → 即使几何靠右也采信
        rows = [BubbleRow(top=300, x_center=950, text="靠右但是对方发的",
                          desc="Alice sent: 靠右但是对方发的")]
        assert pick_latest_incoming(rows, screen_w=1080) == "靠右但是对方发的"

    def test_ui_noise_skipped(self):
        rows = [
            BubbleRow(top=100, x_center=200, text="真实消息"),
            BubbleRow(top=900, x_center=200, text="Type a message…"),
        ]
        assert pick_latest_incoming(rows, screen_w=1080) == "真实消息"

    def test_empty_rows(self):
        assert pick_latest_incoming([], screen_w=1080) == ""

    def test_truncates_500(self):
        rows = [BubbleRow(top=1, x_center=100, text="字" * 600)]
        assert len(pick_latest_incoming(rows, screen_w=1080)) == 500


# ─── pick_thread_title ───────────────────────────────────────────────
class TestPickThreadTitle:
    def test_picks_top_toolbar_name(self):
        cands = [
            TitleCandidate(top=80, x_left=300, text="山田花子"),   # toolbar
            TitleCandidate(top=1200, x_left=100, text="消息正文很长的一段"),
        ]
        assert pick_thread_title(cands, screen_h=2400) == "山田花子"

    def test_ignores_below_band(self):
        cands = [TitleCandidate(top=1500, x_left=100, text="佐藤")]
        assert pick_thread_title(cands, screen_h=2400) == ""

    def test_custom_valid_fn(self):
        cands = [
            TitleCandidate(top=50, x_left=300, text="Reply"),  # 被自定义校验拒
            TitleCandidate(top=90, x_left=300, text="山田花子"),
        ]
        def only_cjk(s):
            return any('\u4e00' <= c <= '\u9fff' for c in s)
        assert pick_thread_title(cands, screen_h=2400,
                                 valid_name_fn=only_cjk) == "山田花子"

    def test_empty(self):
        assert pick_thread_title([], screen_h=2400) == ""


# ─── names_match / normalize_name ────────────────────────────────────
class TestNamesMatch:
    def test_exact(self):
        assert names_match("山田花子", "山田花子")

    def test_separator_and_case(self):
        assert names_match("Alice ", "alice")
        assert names_match("山田花子 •", "山田花子")

    def test_truncated_prefix(self):
        # 列表截断名 vs 会话全名
        assert names_match("山田花...", "山田花子")

    def test_different_people_no_match(self):
        assert not names_match("山田花子", "佐藤美咲")

    def test_empty_no_match(self):
        assert not names_match("", "山田")
        assert not names_match("山田", "")

    def test_normalize(self):
        assert normalize_name("  Alice •, ") == "alice"


# ─── message_fingerprint ─────────────────────────────────────────────
class TestFingerprint:
    def test_stable(self):
        a = message_fingerprint("Alice", "hello")
        b = message_fingerprint("Alice", "hello")
        assert a == b and len(a) == 16

    def test_peer_normalized(self):
        assert message_fingerprint("Alice ", "hi") == message_fingerprint("alice", "hi")

    def test_text_sensitive(self):
        assert message_fingerprint("A", "hi") != message_fingerprint("A", "ho")


# ─── name_from_desc / is_ui_noise ────────────────────────────────────
class TestHelpers:
    def test_name_from_desc(self):
        assert name_from_desc("山田花子, 3 条新消息, 5分钟") == "山田花子"
        assert name_from_desc("Alice，unread") == "Alice"
        assert name_from_desc("") == ""

    def test_is_ui_noise(self):
        assert is_ui_noise("Send")
        assert is_ui_noise("发送")
        assert is_ui_noise("Type a message…")
        assert not is_ui_noise("你好呀在吗")
        assert is_ui_noise("")


# ─── P1: 预览指纹（增量检测「消息不读」兜底） ────────────────────────
class TestPreviewFingerprint:
    @pytest.mark.parametrize("seg", [
        "5分钟前", "3 小时前", "刚刚", "昨天", "今天", "现在",
        "12:30", "3m", "2 hours ago", "just now", "yesterday",
        "周三", "星期五", "10月5日", "5分前", "3時間前", "Mon", "Tuesday",
    ])
    def test_time_segments_detected(self, seg):
        assert looks_time_segment(seg)

    @pytest.mark.parametrize("seg", [
        "山田花子", "你好呀在吗", "photo", "在吗", "5个苹果",
    ])
    def test_non_time_segments(self, seg):
        assert not looks_time_segment(seg)

    def test_core_strips_name_and_time(self):
        # 名字段 + 时间段被剔, 只留内容核
        core = preview_core("山田花子", "山田花子, 你好呀在吗, 5分钟前")
        assert core == "你好呀在吗"

    def test_core_strips_unread_count(self):
        # 未读计数段不进指纹 (否则「已读→未读」翻转触发假变化)
        core = preview_core("Alice", "Alice, 2 unread messages, 你好, 3m")
        assert "unread" not in core.lower()
        assert "你好" in core

    def test_fingerprint_stable_across_time_drift(self):
        # 只有时间段变化 → 指纹不变 (这是本功能的关键: 不因时间抖动误报)
        fp1 = preview_fingerprint("山田花子", "山田花子, 在吗, 5分钟前")
        fp2 = preview_fingerprint("山田花子", "山田花子, 在吗, 8分钟前")
        assert fp1 == fp2 and fp1 != ""

    def test_fingerprint_changes_on_new_content(self):
        fp1 = preview_fingerprint("山田花子", "山田花子, 在吗, 5分钟前")
        fp2 = preview_fingerprint("山田花子", "山田花子, 新的一条消息, 4分钟前")
        assert fp1 != fp2

    def test_empty_core_empty_fingerprint(self):
        # 内容核为空 (只有名字+时间) → 空指纹 = 无信号, 不触发变化
        assert preview_fingerprint("山田花子", "山田花子, 5分钟前") == ""
        assert preview_fingerprint("山田花子", "") == ""
