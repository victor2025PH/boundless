# -*- coding: utf-8 -*-
"""F6 `fb_store.count_unreplied_greetings_to_peer` 单测。

为 A 机 send_greeting_after_add_friend 的 per-peer 5 次上限提供数据源。
语义: 同 peer 最后一次 incoming 之后 B 发出的 greeting 条数。
"""
from __future__ import annotations


class TestCountUnrepliedGreetings:
    def test_empty_db_returns_zero(self, tmp_db):
        from src.host.fb_store import count_unreplied_greetings_to_peer
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 0

    def test_empty_inputs_return_zero(self, tmp_db):
        from src.host.fb_store import count_unreplied_greetings_to_peer
        assert count_unreplied_greetings_to_peer("", "Alice") == 0
        assert count_unreplied_greetings_to_peer("devA", "") == 0

    def test_single_greeting_no_incoming(self, tmp_db):
        """对方从未发过 → 数所有 greetings。"""
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="hi")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 1

    def test_multiple_greetings_no_incoming(self, tmp_db):
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        for i in range(4):
            record_inbox_message("devA", "Alice", direction="outgoing",
                                 ai_decision="greeting",
                                 message_text=f"g{i}")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 4

    def test_incoming_resets_count(self, tmp_db):
        """对方回过后,其前的 greetings 不算;其后的 greeting 重新累计。"""
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        # 2 条 greeting → 对方 incoming → 1 条 greeting
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g2")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="reply")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g3")
        # 分界是 incoming 的 id,之后只有 g3
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 1

    def test_incoming_at_end_zeros_count(self, tmp_db):
        """对方最后发 → 之后无 greeting → 0 (关系活跃,不该再引流式破冰)。"""
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        record_inbox_message("devA", "Alice", direction="incoming",
                             message_text="ok")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 0

    def test_other_decisions_not_counted(self, tmp_db):
        """只数 ai_decision='greeting',reply/wa_referral 不计。"""
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="reply", message_text="r1")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="wa_referral", message_text="r2")
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 1

    def test_incoming_direction_irrelevant_to_ai_decision(self, tmp_db):
        """incoming 行的 ai_decision 无关 (通常是空串),仍作为分界。"""
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        record_inbox_message("devA", "Alice", direction="incoming",
                             ai_decision="",  # 通常 incoming 就是空
                             message_text="ok")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 0

    def test_device_isolation(self, tmp_db):
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        record_inbox_message("devB", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 1
        assert count_unreplied_greetings_to_peer("devB", "Alice") == 1

    def test_peer_isolation(self, tmp_db):
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        record_inbox_message("devA", "Alice", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        record_inbox_message("devA", "Bob", direction="outgoing",
                             ai_decision="greeting", message_text="g1")
        record_inbox_message("devA", "Bob", direction="incoming",
                             message_text="hi")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 1
        assert count_unreplied_greetings_to_peer("devA", "Bob") == 0

    def test_five_cap_boundary_usage(self, tmp_db):
        """端到端模拟 A 的 5 次上限用法。"""
        from src.host.fb_store import (
            count_unreplied_greetings_to_peer, record_inbox_message,
        )
        for i in range(5):
            n = count_unreplied_greetings_to_peer("devA", "Alice")
            if n >= 5:
                break  # 这一次应该 skip
            record_inbox_message("devA", "Alice", direction="outgoing",
                                 ai_decision="greeting",
                                 message_text=f"g{i}")
        assert count_unreplied_greetings_to_peer("devA", "Alice") == 5
        # 第 6 次调用 gate 应该判 skip
        assert count_unreplied_greetings_to_peer("devA", "Alice") >= 5
