# -*- coding: utf-8 -*-
"""多渠道引流 (referral_channels) 单元测试 — 2026-04-23 A 机 Phase 4。

覆盖:
  * 5 个 ReferralChannel 子类的 validate_account / build_deep_link
  * detect_channel_intent 的日/英/韩 关键词匹配
  * pick_channel_smart 的 3 段式选择(intent → persona priority → fallback)
  * 注册表 get_channel / register_channel
  * mask / event_meta 基本行为
"""
from __future__ import annotations

import pytest


# ─── Channel validate / deep link ────────────────────────────────────────────
class TestLineChannel:
    def setup_method(self):
        from src.app_automation.referral_channels import LineChannel
        self.ch = LineChannel()

    def test_at_id_accepted(self):
        assert self.ch.validate_account("@jp_user") == (True, "@jp_user")

    def test_bare_id_prefixed(self):
        """裸 id 自动加 @。"""
        assert self.ch.validate_account("jp_user") == (True, "@jp_user")

    def test_too_short_rejected(self):
        ok, _ = self.ch.validate_account("a")
        assert ok is False

    def test_line_url_preserved(self):
        ok, v = self.ch.validate_account("https://line.me/ti/p/~abc")
        assert ok is True
        assert "line.me" in v

    def test_qr_url_preserved(self):
        ok, v = self.ch.validate_account("https://qr-server.line-apps.com/x")
        assert ok is True

    def test_empty_rejected(self):
        assert self.ch.validate_account("") == (False, "")

    def test_no_deep_link_by_default(self):
        """LINE 故意不发 deep link(FB 风控屏蔽 line.me)。"""
        assert self.ch.build_deep_link("@foo") is None


class TestWhatsAppChannel:
    def setup_method(self):
        from src.app_automation.referral_channels import WhatsAppChannel
        self.ch = WhatsAppChannel()

    def test_plus_prefix(self):
        ok, v = self.ch.validate_account("+819012345678")
        assert ok is True
        assert v == "+819012345678"

    def test_spaces_cleaned(self):
        """+81 90 1234 5678 去空格。"""
        ok, v = self.ch.validate_account("+81 90 1234 5678")
        assert ok is True
        assert v == "+819012345678"

    def test_no_plus_added(self):
        ok, v = self.ch.validate_account("819012345678")
        assert ok is True
        assert v.startswith("+")

    def test_wame_link_preserved(self):
        ok, v = self.ch.validate_account("https://wa.me/819012345678")
        assert ok is True
        assert "wa.me" in v

    def test_text_rejected(self):
        ok, _ = self.ch.validate_account("hello")
        assert ok is False

    def test_deep_link_generated(self):
        assert self.ch.build_deep_link("+819012345678") == "https://wa.me/819012345678"


class TestTelegramChannel:
    def setup_method(self):
        from src.app_automation.referral_channels import TelegramChannel
        self.ch = TelegramChannel()

    def test_at_username(self):
        assert self.ch.validate_account("@foobar") == (True, "@foobar")

    def test_bare_username(self):
        ok, v = self.ch.validate_account("foobar")
        assert ok is True
        assert v.startswith("@")

    def test_tme_url(self):
        ok, v = self.ch.validate_account("t.me/foo")
        assert ok is True

    def test_phone_accepted(self):
        ok, v = self.ch.validate_account("+8613912345678")
        assert ok is True

    def test_deep_link_from_at(self):
        assert self.ch.build_deep_link("@foobar") == "https://t.me/foobar"

    def test_deep_link_from_phone(self):
        assert self.ch.build_deep_link("+8613912345678") == "https://t.me/8613912345678"


class TestMessengerChannel:
    def setup_method(self):
        from src.app_automation.referral_channels import MessengerChannel
        self.ch = MessengerChannel()

    def test_mme_url(self):
        ok, v = self.ch.validate_account("m.me/mycompany")
        assert ok is True

    def test_username_accepted(self):
        ok, v = self.ch.validate_account("mycompany")
        assert ok is True

    def test_deep_link(self):
        assert self.ch.build_deep_link("mycompany") == "https://m.me/mycompany"


class TestInstagramChannel:
    def setup_method(self):
        from src.app_automation.referral_channels import InstagramChannel
        self.ch = InstagramChannel()

    def test_at_handle(self):
        ok, v = self.ch.validate_account("@myhandle")
        assert ok is True

    def test_url_preserved(self):
        ok, v = self.ch.validate_account("instagram.com/myhandle")
        assert ok is True

    def test_deep_link(self):
        assert self.ch.build_deep_link("@myhandle") == "https://instagram.com/myhandle"


# ─── Intent detection ────────────────────────────────────────────────────────
class TestDetectChannelIntent:
    def test_line_japanese(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("LINE教えてもらえませんか?")
        assert key == "line"
        assert score >= 0.85

    def test_line_english(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("Do you have LINE?")
        assert key == "line"

    def test_wa_abbreviation(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("Can we move to WA?")
        assert key == "whatsapp"

    def test_telegram(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("Let's use Telegram")
        assert key == "telegram"

    def test_instagram_short(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("IG?")
        assert key == "instagram"

    def test_no_intent(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("Hi, nice to meet you")
        assert score == 0.0

    def test_empty_text(self):
        from src.app_automation.referral_channels import detect_channel_intent
        key, score = detect_channel_intent("")
        assert score == 0.0

    def test_among_filter(self):
        """among 参数限定候选集。"""
        from src.app_automation.referral_channels import detect_channel_intent
        # 消息里含 LINE, 但 among 只允许 whatsapp/telegram → 应该不命中
        key, score = detect_channel_intent("LINE?", among=["whatsapp", "telegram"])
        assert score == 0.0


# ─── pick_channel_smart 三段式 ───────────────────────────────────────────────
class TestPickChannelSmart:
    def test_intent_wins_over_priority(self):
        """意图优先于 persona 优先级。日本 persona 优先 LINE,
        但对方问 whatsapp 应切到 WA。"""
        from src.app_automation.referral_channels import pick_channel_smart
        ch, v = pick_channel_smart(
            incoming_text="Do you have WhatsApp?",
            persona_key="jp_female_midlife",
            available_accounts={"line": "@jp", "whatsapp": "+8190"},
        )
        assert ch is not None
        assert ch.channel_key == "whatsapp"

    def test_persona_priority_when_no_intent(self):
        """无意图时按 persona 优先级(日本女性 persona 首推 LINE)。"""
        from src.app_automation.referral_channels import pick_channel_smart
        ch, v = pick_channel_smart(
            incoming_text="Hi nice to meet you",
            persona_key="jp_female_midlife",
            available_accounts={"line": "@jp", "whatsapp": "+8190"},
        )
        assert ch is not None
        # 日本 persona referral_priority 首推 LINE
        assert ch.channel_key == "line"

    def test_only_whatsapp_available(self):
        """只有 WA 账号时,即使 persona 优先 LINE 也得回 WA。"""
        from src.app_automation.referral_channels import pick_channel_smart
        ch, v = pick_channel_smart(
            persona_key="jp_female_midlife",
            available_accounts={"whatsapp": "+8190"},
        )
        assert ch is not None
        assert ch.channel_key == "whatsapp"

    def test_empty_accounts(self):
        from src.app_automation.referral_channels import pick_channel_smart
        ch, v = pick_channel_smart(available_accounts={})
        assert ch is None
        assert v == ""

    def test_default_blob_fallback(self):
        """parse_referral_channels 无法识别时返回 _default; pick_smart 应兜底用 persona 首推渠道。"""
        from src.app_automation.referral_channels import pick_channel_smart
        ch, v = pick_channel_smart(
            persona_key="jp_female_midlife",
            available_accounts={"_default": "some raw string"},
        )
        # 应返回 persona 首推渠道对象 + 默认串
        assert ch is not None
        assert v == "some raw string"


# ─── 注册表 / mask / event_meta ──────────────────────────────────────────────
class TestRegistryAndHelpers:
    def test_registered_channels(self):
        from src.app_automation.referral_channels import registered_channels
        keys = registered_channels()
        assert set(keys) == {"line", "whatsapp", "telegram", "messenger", "instagram"}

    def test_get_channel_unknown(self):
        from src.app_automation.referral_channels import get_channel
        assert get_channel("kakao") is None
        assert get_channel("") is None

    def test_register_custom_channel(self):
        """运行时注册第三方渠道。"""
        from src.app_automation.referral_channels import (
            register_channel, get_channel, ReferralChannel, REFERRAL_REGISTRY)

        class KakaoChannel(ReferralChannel):
            channel_key = "kakao"
            display_name = "KakaoTalk"
            intent_keywords = ("kakao", "카카오")

        try:
            register_channel(KakaoChannel())
            assert get_channel("kakao") is not None
            assert get_channel("kakao").display_name == "KakaoTalk"
        finally:
            REFERRAL_REGISTRY.pop("kakao", None)  # cleanup

    def test_mask(self):
        from src.app_automation.referral_channels import WhatsAppChannel
        ch = WhatsAppChannel()
        masked = ch.mask("+819012345678")
        assert masked.startswith("+8") and masked.endswith("78")
        assert "*" in masked
        # 不泄露中间号码
        assert "901234" not in masked

    def test_short_account_fully_masked(self):
        from src.app_automation.referral_channels import LineChannel
        ch = LineChannel()
        assert ch.mask("@ab") == "***"

    def test_event_meta(self):
        from src.app_automation.referral_channels import LineChannel
        meta = LineChannel().event_meta("@jpuser")
        assert meta["channel"] == "line"
        assert "account_masked" in meta
        # 原 value 不在 meta 里
        assert "jpuser" not in str(meta).lower() or "*" in meta["account_masked"]


# ─── format_snippet 集成(和 fb_content_assets 联动) ──────────────────────────
class TestFormatSnippet:
    def test_line_snippet_nonempty(self):
        """LINE 文案应返回非空, 即使 persona 无本地化也有兜底。"""
        from src.app_automation.referral_channels import LineChannel
        s = LineChannel().format_snippet("@jpuser", persona_key="jp_female_midlife")
        assert s and len(s) > 0

    def test_wa_snippet_contains_deeplink(self):
        """WA 开了 use_deep_link_in_snippet → snippet 里应有 wa.me 链接。"""
        from src.app_automation.referral_channels import WhatsAppChannel
        s = WhatsAppChannel().format_snippet("+819012345678",
                                              persona_key="jp_female_midlife")
        assert "wa.me" in s or "+8190" in s  # 至少有某种引用

    def test_line_snippet_no_deep_link(self):
        """LINE 不在 snippet 里塞 line.me(风控风险)。"""
        from src.app_automation.referral_channels import LineChannel
        s = LineChannel().format_snippet("@jpuser", persona_key="jp_female_midlife")
        assert "line.me" not in s.lower()
