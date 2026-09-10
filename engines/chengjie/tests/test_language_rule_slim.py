"""B6：语言规则块瘦身门禁——装配 ≤400 字，语义钉子仍在。"""
from __future__ import annotations

from src.ai.language_rule import MAX_CHARS, fits_budget, language_rule_block


def test_non_zh_block_under_budget_and_keeps_pins():
    for lang, name in (("en", "English"), ("ja", "Japanese"), ("ko", "Korean"),
                       ("es", "Spanish"), ("ur", "Urdu")):
        for companion in (True, False):
            b = language_rule_block(lang, name, companion=companion)
            assert fits_budget(b), (lang, companion, len(b), b)
            assert "【LANGUAGE RULE" in b
            assert name in b
            assert "/cxds" in b
            if lang == "ja":
                assert "Japanese only" in b
            if lang == "ko":
                assert "Korean only" in b
            if lang not in ("ja", "ko") and not companion:
                assert "EasyPaisa" in b


def test_zh_block_under_budget():
    for companion in (True, False):
        b = language_rule_block("zh", "", companion=companion)
        assert fits_budget(b), (companion, len(b), b)
        assert "【多语言回复规则" in b
        assert "SAME language" in b
        if not companion:
            assert "EasyPaisa" in b


def test_empty_lang_falls_to_zh_rule():
    b = language_rule_block("", "")
    assert "【多语言回复规则" in b and len(b) <= MAX_CHARS


def test_ai_client_uses_slim_builder():
    import inspect
    from src.ai.ai_client import AIClient
    src = inspect.getsource(AIClient._build_system_instruction)
    assert "language_rule_block" in src
    assert "You MUST reply ENTIRELY" not in src
    assert "_lang_rule_emitted" in src


def test_full_system_skips_duplicate_output_lang():
    from src.ai.ai_client import AIClient

    class _Cfg:
        config_path = None
        config = {"domain": "conversion", "web_admin": {"site_name": "T"}, "ai": {}}
        def get_ai_config(self):
            return {}

    client = AIClient(_Cfg())
    ctx = {"reply_lang": "zh", "channel": "telegram", "chat_type": "private",
           "_current_user_message_for_lang": "我被咬了？"}
    out = client._build_system_instruction(ctx)
    assert "ALWAYS reply in the SAME language" in out
    assert "用户当前消息语言为「中文」" not in out
    assert ctx.get("_lang_rule_emitted") is True
