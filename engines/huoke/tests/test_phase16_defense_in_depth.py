# -*- coding: utf-8 -*-
"""Phase 16 (2026-04-25): record_contact_event 入口 sanitize +
长预览截断标记 ban + 防 p0/p1/p2 二次入侵."""
from __future__ import annotations

import pytest


class TestRecordContactEventSanitize:
    """fb_store.record_contact_event 入口 peer_name 校验."""

    def test_valid_peer_name_inserts(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          count_contact_events,
                                          reset_peer_name_reject_count)
        reset_peer_name_reject_count()
        rid = record_contact_event("D1", "山田花子", "greeting_sent",
                                     meta={"k": "v"})
        assert rid > 0
        n = count_contact_events(device_id="D1", peer_name="山田花子",
                                   event_type="greeting_sent", hours=24)
        assert n == 1

    def test_invalid_peer_name_rejected(self, tmp_db):
        """UI 文本被入口拦, 不写入."""
        from src.host.fb_store import (record_contact_event,
                                          count_contact_events,
                                          reset_peer_name_reject_count,
                                          get_peer_name_reject_count)
        reset_peer_name_reject_count()
        rid = record_contact_event("D1", "查看翻译", "greeting_sent")
        assert rid == 0
        # 计数器 +1
        assert get_peer_name_reject_count() == 1
        # DB 没写入
        assert count_contact_events(device_id="D1", peer_name="查看翻译",
                                       event_type="greeting_sent",
                                       hours=24) == 0

    def test_p0_rejected_at_entry(self, tmp_db):
        """p0/p1/p2 测试残留入口拦."""
        from src.host.fb_store import (record_contact_event,
                                          reset_peer_name_reject_count,
                                          get_peer_name_reject_count)
        reset_peer_name_reject_count()
        for n in ("p0", "p1", "p2"):
            assert record_contact_event("D1", n, "greeting_sent") == 0
        assert get_peer_name_reject_count() == 3

    def test_skip_sanitize_explicit_bypass(self, tmp_db):
        """skip_sanitize=True 显式 bypass (合法 e2e seed 用).

        2026-04-27 P5: 反例改用 "p0" (规则 7: ASCII 短串含数字) — 删 ASCII 启发后
        "Alice" 是合法英文名, 不再被拒.
        """
        from src.host.fb_store import (record_contact_event,
                                          count_contact_events,
                                          reset_peer_name_reject_count)
        reset_peer_name_reject_count()
        rid = record_contact_event("D1", "p0", "greeting_sent",
                                     skip_sanitize=True)
        assert rid > 0
        # 但默认会被拦 (规则 7: ASCII 短串 ≤4 含数字 = 测试残留)
        rid2 = record_contact_event("D1", "p0", "greeting_replied")
        assert rid2 == 0


class TestTruncationMarkerBan:
    """Phase 16: '... 更多' / '... more' 等截断标记 ban."""

    def setup_method(self):
        from src.app_automation.facebook import FacebookAutomation
        self.fn = FacebookAutomation._is_valid_peer_name

    def test_zh_truncation_more_rejected(self):
        # 注意: "... 更多" 含 "..." 已被旧 PREVIEW_HINTS 拦, 这里测仅 末尾"更多"
        # 但实际生产 "50ご飯…(更多)" 类型, "…" 拦了. 这里 explicit truncation:
        assert self.fn("ありがとうございます 更多") is False
        assert self.fn("こんにちは 更多") is False

    def test_en_truncation_more_rejected(self):
        # 长串预览含末尾 "more" 截断
        assert self.fn("Hi how are you more") is False
        assert self.fn("today's lunch was great more") is False

    def test_jp_truncation_motto_rejected(self):
        assert self.fn("天気いいね もっと見る") is False
        assert self.fn("あめ もっと") is False

    def test_real_name_with_more_in_middle_passes(self):
        """末尾不是 'more' 的真名通过 (虽然实际罕见)."""
        # 如 'Moremore' 这种 hypothetical 真名 — 我们只 ban 末尾匹配
        # 'Mxxmore' 末尾 'more' 会被 ban (有歧义但安全保守)
        # 这里测一个不带末尾 marker 的:
        assert self.fn("田中 太郎") is True
        assert self.fn("Maria Mori") is True  # 'Mori' 末尾不是 marker

    def test_marker_in_middle_passes(self):
        """marker 不在末尾不 ban (避免误杀真名)."""
        # 假设有人名含 '更多' 子串但不在末尾 (罕见但允许)
        # 例: 'もっと愛して' (marker 在开头) — 不 ban, 因截断 marker 总在末尾
        assert self.fn("もっと愛して") is True


class TestRegressionPhase15Plus16:
    """确保 Phase 16 加 record_contact_event sanitize 不破坏 Phase 15 真用例."""

    def test_jp_real_name_still_passes(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          reset_peer_name_reject_count)
        reset_peer_name_reject_count()
        for name in ["山田花子", "佐藤美咲", "田中由紀子", "中村恵"]:
            assert record_contact_event("D1", name, "greeting_sent") > 0

    def test_english_name_passes(self, tmp_db):
        from src.host.fb_store import record_contact_event
        assert record_contact_event("D1", "John Smith", "greeting_sent") > 0
        assert record_contact_event("D1", "Maria Rossi", "greeting_sent") > 0

    def test_emoji_decorated_name_passes(self, tmp_db):
        """名字 + 装饰 emoji 通过."""
        from src.host.fb_store import record_contact_event
        assert record_contact_event("D1", "花子🌸", "greeting_sent") > 0
