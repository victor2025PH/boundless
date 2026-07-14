# -*- coding: utf-8 -*-
"""Phase 12.1 (2026-04-25): get_referral_snippet 个性化占位单测.

覆盖 peer_name / age_band / gender 占位符替换 + 缺失字段不报错.
"""
from __future__ import annotations

from unittest.mock import patch


class TestReferralSnippetPersonalization:
    def test_peer_name_placeholder_substituted(self):
        """模板含 {peer_name} 时用传入值替换."""
        from src.app_automation.fb_content_assets import get_referral_snippet
        with patch("src.app_automation.fb_content_assets._country_bundle",
                    return_value={
                        "referral_line": [
                            "{peer_name}さん, LINE で繋がりましょう: {line}"
                        ],
                    }), \
             patch("src.app_automation.fb_content_assets._resolve_context",
                    return_value={"country_code": "jp", "language": "ja"}):
            out = get_referral_snippet(
                channel="line", value="along2026",
                peer_name="花子",
            )
        assert "花子さん" in out
        assert "along2026" in out

    def test_missing_peer_name_empty_string_no_error(self):
        """没传 peer_name 时 {peer_name} 变空, 不抛 KeyError."""
        from src.app_automation.fb_content_assets import get_referral_snippet
        with patch("src.app_automation.fb_content_assets._country_bundle",
                    return_value={
                        "referral_line": ["Hi {peer_name}! LINE: {line}"],
                    }), \
             patch("src.app_automation.fb_content_assets._resolve_context",
                    return_value={"country_code": "jp", "language": "ja"}):
            out = get_referral_snippet(channel="line", value="along2026")
        # _safe_format 把 {peer_name} 替换成 '', 整体不崩
        assert "along2026" in out
        # 空 peer → "Hi ! LINE: along2026" (中间有个空格, 无所谓)

    def test_age_band_gender_placeholders(self):
        """{age_band} / {gender} 都支持."""
        from src.app_automation.fb_content_assets import get_referral_snippet
        with patch("src.app_automation.fb_content_assets._country_bundle",
                    return_value={
                        "referral_line": [
                            "({age_band} {gender}): 我的LINE → {line}"
                        ],
                    }), \
             patch("src.app_automation.fb_content_assets._resolve_context",
                    return_value={"country_code": "jp", "language": "ja"}):
            out = get_referral_snippet(
                channel="line", value="along2026",
                peer_name="X", age_band="40s", gender="female",
            )
        assert "40s" in out
        assert "female" in out

    def test_name_alias_works(self):
        """{name} 别名也能替换 peer_name (chat_messages.yaml 老模板常用 {name})."""
        from src.app_automation.fb_content_assets import get_referral_snippet
        with patch("src.app_automation.fb_content_assets._country_bundle",
                    return_value={
                        "referral_line": ["{name}~ LINE: {line}"],
                    }), \
             patch("src.app_automation.fb_content_assets._resolve_context",
                    return_value={"country_code": "jp", "language": "ja"}):
            out = get_referral_snippet(channel="line", value="along2026",
                                         peer_name="美咲")
        assert "美咲" in out
