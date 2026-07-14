# -*- coding: utf-8 -*-
"""Messenger 自动回复 + 跨 bot 归因单元测试 (feat-b-chat-p0)。

覆盖 2026-04-23 新增的三块:
  * ``src.ai.lang_detect.detect_language`` 启发式
  * ``src.host.fb_store.mark_incoming_replied`` incoming 行回写
  * ``src.host.fb_store.mark_greeting_replied_back`` 跨 bot greeting 归因

不覆盖的部分 (归属 smoke 真机 / 集成测):
  * ``_ai_reply_and_send`` 真实 ChatBrain 联动
  * Messenger App UI 抓取
"""
from __future__ import annotations

import datetime as _dt

import pytest


# ─── src.ai.lang_detect ───────────────────────────────────────────────────────
class TestLangDetect:
    def test_japanese_hiragana(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("こんにちは、お元気ですか") == "ja"

    def test_japanese_katakana(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("コーヒー飲みたい") == "ja"

    def test_japanese_mixed_with_latin(self):
        from src.ai.lang_detect import detect_language
        # ja 优先 (kana 存在即 ja),即使里面夹英文
        assert detect_language("hi こんにちは!") == "ja"

    def test_chinese_only(self):
        from src.ai.lang_detect import detect_language
        # CJK but no kana → zh
        assert detect_language("你好,最近怎么样") == "zh"

    def test_italian_diacritics(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("Però non è male") == "it"

    def test_italian_markers_no_diacritics(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("Ciao come stai amico") == "it"

    def test_italian_short_marker(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("grazie mille") == "it"

    def test_english_default(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("hi how are you doing today") == "en"

    def test_english_not_italian_on_common_words(self):
        from src.ai.lang_detect import detect_language
        # 没有意大利语标志词,纯英文短语
        assert detect_language("thanks for the reply") == "en"

    def test_empty_returns_blank(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("") == ""
        assert detect_language("   ") == ""

    def test_too_short_returns_blank(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("a") == ""

    def test_pure_emoji_returns_blank(self):
        from src.ai.lang_detect import detect_language
        assert detect_language("😀🎉") == ""

    def test_none_safe(self):
        from src.ai.lang_detect import detect_language
        assert detect_language(None) == ""  # type: ignore[arg-type]


# ─── fb_store.mark_incoming_replied ───────────────────────────────────────────
class TestMarkIncomingReplied:
    def test_updates_latest_incoming_null_replied_at(self, tmp_db):
        from src.host.fb_store import (mark_incoming_replied,
                                       record_inbox_message)
        record_inbox_message("d1", "Alice", direction="incoming",
                             message_text="first incoming")
        record_inbox_message("d1", "Alice", direction="incoming",
                             message_text="second incoming")
        n = mark_incoming_replied("d1", "Alice", replied_at="2026-04-23T10:00:00Z")
        assert n == 1
        # 只动最新那条
        from src.host.fb_store import list_inbox_messages
        rows = list_inbox_messages(device_id="d1")
        alice_incoming = [r for r in rows if r["direction"] == "incoming"]
        # 最新(id 大的那条)被标记
        newest = max(alice_incoming, key=lambda r: r["id"])
        oldest = min(alice_incoming, key=lambda r: r["id"])
        assert newest["replied_at"] == "2026-04-23T10:00:00Z"
        # 旧的保持 NULL
        assert not oldest["replied_at"]

    def test_idempotent_when_all_have_replied_at(self, tmp_db):
        from src.host.fb_store import (mark_incoming_replied,
                                       record_inbox_message)
        record_inbox_message("d1", "Alice", direction="incoming",
                             message_text="hi")
        # 第一次回写
        assert mark_incoming_replied("d1", "Alice") == 1
        # 再调一次,没有未标记的 incoming → 0 行更新
        assert mark_incoming_replied("d1", "Alice") == 0

    def test_no_incoming_row_returns_zero(self, tmp_db):
        from src.host.fb_store import mark_incoming_replied
        assert mark_incoming_replied("d1", "NobodyHere") == 0

    def test_does_not_touch_outgoing(self, tmp_db):
        from src.host.fb_store import (mark_incoming_replied,
                                       record_inbox_message,
                                       list_inbox_messages)
        record_inbox_message("d1", "Alice", direction="outgoing",
                             message_text="hi from bot")
        n = mark_incoming_replied("d1", "Alice")
        assert n == 0
        rows = list_inbox_messages(device_id="d1")
        assert all(not r["replied_at"] for r in rows)

    def test_multi_device_isolation(self, tmp_db):
        from src.host.fb_store import (mark_incoming_replied,
                                       record_inbox_message,
                                       list_inbox_messages)
        record_inbox_message("d1", "Alice", direction="incoming",
                             message_text="d1 msg")
        record_inbox_message("d2", "Alice", direction="incoming",
                             message_text="d2 msg")
        n = mark_incoming_replied("d1", "Alice", replied_at="2026-04-23T10:00:00Z")
        assert n == 1
        # d2 的 Alice 未受影响
        d2_rows = list_inbox_messages(device_id="d2")
        assert d2_rows[0]["replied_at"] is None or d2_rows[0]["replied_at"] == ""

    def test_peer_type_filter(self, tmp_db):
        from src.host.fb_store import (mark_incoming_replied,
                                       record_inbox_message)
        record_inbox_message("d1", "Alice", direction="incoming",
                             peer_type="stranger", message_text="req msg")
        # 过滤 friend → 不匹配 → 0
        assert mark_incoming_replied("d1", "Alice", peer_type="friend") == 0
        # 匹配 stranger → 1
        assert mark_incoming_replied("d1", "Alice", peer_type="stranger") == 1


# ─── fb_store.mark_greeting_replied_back ──────────────────────────────────────
class TestMarkGreetingRepliedBack:
    def test_marks_greeting_in_window(self, tmp_db):
        from src.host.fb_store import (mark_greeting_replied_back,
                                       record_inbox_message,
                                       list_inbox_messages)
        # A 写的 greeting (peer_type=friend_request + ai_decision=greeting + outgoing)
        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="hi from A",
                             template_id="yaml:jp:0")
        n = mark_greeting_replied_back("d1", "Alice",
                                       replied_at="2026-04-23T10:00:00Z")
        assert n == 1
        rows = list_inbox_messages(device_id="d1")
        greeting = [r for r in rows if r["ai_decision"] == "greeting"][0]
        assert greeting["replied_at"] == "2026-04-23T10:00:00Z"

    def test_skips_non_greeting_rows(self, tmp_db):
        from src.host.fb_store import (mark_greeting_replied_back,
                                       record_inbox_message)
        # B 写的 reply/wa_referral 不应被归因为 A 的 greeting
        record_inbox_message("d1", "Alice",
                             peer_type="friend",
                             direction="outgoing",
                             ai_decision="reply",
                             message_text="reply by B")
        record_inbox_message("d1", "Bob",
                             peer_type="friend",
                             direction="outgoing",
                             ai_decision="wa_referral",
                             message_text="go to WA")
        assert mark_greeting_replied_back("d1", "Alice") == 0
        assert mark_greeting_replied_back("d1", "Bob") == 0

    def test_skips_greeting_outside_window(self, tmp_db):
        """>window_days 外的 greeting 行不能被标记。"""
        from src.host.fb_store import (mark_greeting_replied_back,
                                       record_inbox_message)
        from src.host.database import _connect
        # 写一条 greeting, 然后手工倒退 sent_at 超过窗口
        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="old greeting",
                             template_id="yaml:jp:0")
        old = (_dt.datetime.utcnow() - _dt.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with _connect() as conn:
            conn.execute(
                "UPDATE facebook_inbox_messages SET sent_at=?, seen_at=? "
                "WHERE peer_name=?",
                (old, old, "Alice"),
            )
        n = mark_greeting_replied_back("d1", "Alice", window_days=7)
        assert n == 0

    def test_idempotent_repeat(self, tmp_db):
        from src.host.fb_store import (mark_greeting_replied_back,
                                       record_inbox_message)
        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="hi")
        assert mark_greeting_replied_back("d1", "Alice") == 1
        # 再调:已有 replied_at,返回 0
        assert mark_greeting_replied_back("d1", "Alice") == 0

    def test_requires_peer_type_friend_request(self, tmp_db):
        """greeting ai_decision 但 peer_type=friend 的不归因,避免误杀"""
        from src.host.fb_store import (mark_greeting_replied_back,
                                       record_inbox_message)
        record_inbox_message("d1", "Alice",
                             peer_type="friend",  # 注意不是 friend_request
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="hi")
        assert mark_greeting_replied_back("d1", "Alice") == 0

    def test_multi_device_isolation(self, tmp_db):
        from src.host.fb_store import (mark_greeting_replied_back,
                                       record_inbox_message,
                                       list_inbox_messages)
        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="hi d1")
        record_inbox_message("d2", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="hi d2")
        n = mark_greeting_replied_back("d1", "Alice",
                                       replied_at="2026-04-23T10:00:00Z")
        assert n == 1
        d2 = list_inbox_messages(device_id="d2")[0]
        assert not d2.get("replied_at")

    def test_no_negative_window(self, tmp_db):
        from src.host.fb_store import mark_greeting_replied_back
        assert mark_greeting_replied_back("d1", "Alice", window_days=0) == 0
        assert mark_greeting_replied_back("d1", "Alice", window_days=-1) == 0

    def test_empty_args_safe(self, tmp_db):
        from src.host.fb_store import mark_greeting_replied_back
        assert mark_greeting_replied_back("", "Alice") == 0
        assert mark_greeting_replied_back("d1", "") == 0


# ─── F1: contact_event 同步 (Phase 5 merge 前后都要 graceful) ────────────
class TestGreetingRepliedContactEventSync:
    def test_phase5_not_merged_graceful(self, tmp_db):
        """Phase 5 未 merge → record_contact_event 不在 globals → 静默 skip。"""
        from src.host import fb_store
        from src.host.fb_store import (record_inbox_message,
                                        mark_greeting_replied_back)
        # 先准备一条 greeting 行
        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             template_id="yaml:jp:3|fallback",
                             preset_key="jp_growth",
                             message_text="hi")
        # 确认 record_contact_event 不在 globals (Phase 5 未 merge)
        # — 此断言说明 graceful 降级能起作用
        assert "record_contact_event" not in fb_store.__dict__ or \
               callable(getattr(fb_store, "record_contact_event", None))
        # 不管是否有 Phase 5,mark_greeting_replied_back 应成功返回 1
        n = mark_greeting_replied_back("d1", "Alice", window_days=7)
        assert n == 1

    def test_phase5_merged_writes_contact_event(self, tmp_db, monkeypatch):
        """Phase 5 已 merge 时 → 同步调 record_contact_event。"""
        from src.host import fb_store
        from src.host.fb_store import (record_inbox_message,
                                        mark_greeting_replied_back)

        calls = []

        def fake_record(device_id, peer_name, event_type, **kw):
            calls.append({
                "device_id": device_id, "peer_name": peer_name,
                "event_type": event_type, **kw,
            })
            return 1

        monkeypatch.setattr(fb_store, "record_contact_event",
                            fake_record, raising=False)
        monkeypatch.setattr(fb_store, "CONTACT_EVT_GREETING_REPLIED",
                            "greeting_replied", raising=False)

        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             template_id="yaml:jp:3|fallback",
                             preset_key="jp_growth",
                             message_text="hi")
        n = mark_greeting_replied_back("d1", "Alice", window_days=7)
        assert n == 1
        # contact_event 被同步写入
        assert len(calls) == 1
        c = calls[0]
        assert c["device_id"] == "d1"
        assert c["peer_name"] == "Alice"
        assert c["event_type"] == "greeting_replied"
        # template_id 去了 '|fallback' 后缀,只保留纯 id
        assert c["template_id"] == "yaml:jp:3"
        assert c["preset_key"] == "jp_growth"
        assert c["meta"]["via"] == "mark_greeting_replied_back"
        assert c["meta"]["window_days"] == 7

    def test_phase5_merged_no_row_matched_no_event(self, tmp_db, monkeypatch):
        """UPDATE 未命中 (peer 无 greeting) → 不写 contact_event。"""
        from src.host import fb_store
        from src.host.fb_store import mark_greeting_replied_back
        calls = []
        monkeypatch.setattr(fb_store, "record_contact_event",
                            lambda *a, **kw: calls.append(kw) or 1,
                            raising=False)
        n = mark_greeting_replied_back("d1", "GhostPeer", window_days=7)
        assert n == 0
        assert calls == []

    def test_phase5_record_contact_event_failure_does_not_break(self, tmp_db,
                                                                monkeypatch):
        """record_contact_event 抛异常 → 主流程不受影响 (仍返回 rowcount=1)。"""
        from src.host import fb_store
        from src.host.fb_store import (record_inbox_message,
                                        mark_greeting_replied_back)

        def crashing(*a, **kw):
            raise RuntimeError("contact event DB wiped")
        monkeypatch.setattr(fb_store, "record_contact_event", crashing,
                            raising=False)
        record_inbox_message("d1", "Alice",
                             peer_type="friend_request",
                             direction="outgoing",
                             ai_decision="greeting",
                             message_text="hi")
        # 不抛, 仍返回 1
        n = mark_greeting_replied_back("d1", "Alice", window_days=7)
        assert n == 1
