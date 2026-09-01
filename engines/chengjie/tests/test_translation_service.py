import pytest

from src.ai.translation_service import TranslationService, detect_language


def test_detect_language_common_scripts():
    assert detect_language("你好，今天怎么样") == "zh"
    assert detect_language("こんにちは、元気？") == "ja"
    assert detect_language("안녕하세요") == "ko"
    assert detect_language("مرحبا كيف حالك") == "ar"
    assert detect_language("Привет как дела") == "ru"
    assert detect_language("hola, gracias") == "es"
    assert detect_language("hello friend") == "en"


def test_detect_language_southeast_asian_and_more():
    # 跨境客服高频客户语种（此前会落 en/unknown，现确定性识别）
    assert detect_language("สวัสดีครับ อยากสอบถามราคา") == "th"   # 泰语
    assert detect_language("Xin chào, tôi muốn mua sản phẩm này") == "vi"  # 越南语
    assert detect_language("ជំរាបសួរ តើតម្លៃប៉ុន្មាន") == "km"      # 高棉语
    assert detect_language("Γειά σου τι κάνεις") == "el"           # 希腊语
    assert detect_language("שלום מה שלומך") == "he"               # 希伯来语
    assert detect_language("Halo, saya mau tanya harga") == "id"   # 印尼语（关键词）
    assert detect_language("Salamat, magkano po ito") == "tl"      # 菲律宾语
    assert detect_language("") == "unknown"


def test_detect_language_thai_baht_symbol_not_misdetected():
    # 跨境电商 THB 报价：泰铢符号 ฿ 不应让纯英文消息被判成泰语
    assert detect_language("Price: 100฿ only, free shipping") == "en"


@pytest.mark.asyncio
async def test_translation_service_identity_and_cache():
    svc = TranslationService(default_target_lang="zh")
    same = await svc.translate("你好", target_lang="zh")
    assert same.ok is True
    assert same.provider == "identity"
    assert same.translated_text == "你好"

    first = await svc.translate("hello friend", target_lang="zh")
    assert first.ok is False
    assert first.error == "provider_unavailable"
    second = await svc.translate("hello friend", target_lang="zh")
    assert second.cached is True


@pytest.mark.asyncio
async def test_translation_service_uses_ai_client():
    class FakeAI:
        async def chat(self, prompt, context=None):
            assert "Translate" in prompt
            return "你好朋友"

    svc = TranslationService(ai_client=FakeAI())
    rv = await svc.translate("hello friend", target_lang="zh")
    assert rv.ok is True
    assert rv.provider == "ai"
    assert rv.translated_text == "你好朋友"


# ── 中文变体（繁体/粤语）目标语（2026-08-29）──────────────────────────────────

def test_normalize_lang_zh_variants_first_class():
    """zh-tw 不再折叠成 zh（否则简→繁在 identity 短路里恒原样返回）；
    zh-hant/zh-hk 归一到 zh-tw；简体折叠语义不变。"""
    from src.ai.translation_service import LANG_NAMES, normalize_lang
    assert normalize_lang("zh-TW") == "zh-tw"
    assert normalize_lang("zh_Hant") == "zh-tw"
    assert normalize_lang("zh-HK") == "zh-tw"
    assert normalize_lang("zh-CN") == "zh"
    assert normalize_lang("yue") == "yue"
    # AI 线 prompt 显名（缺了会把裸码写进 prompt）
    assert LANG_NAMES["zh-tw"] == "Traditional Chinese"
    assert LANG_NAMES["yue"] == "Cantonese"


@pytest.mark.asyncio
async def test_translate_zh_to_traditional_and_cantonese_not_identity():
    """简体→繁体/粤语必须真走引擎（修复前 zh-tw 被 normalize 折叠 → 恒 identity）。"""
    class FakeAI:
        def __init__(self, out):
            self._out = out

        async def chat(self, prompt, context=None):
            return self._out

    svc_tw = TranslationService(ai_client=FakeAI("謝謝你的幫忙"))
    r_tw = await svc_tw.translate("谢谢你的帮忙", target_lang="zh-TW")
    assert r_tw.ok is True and r_tw.provider == "ai"
    assert r_tw.translated_text == "謝謝你的幫忙"
    assert r_tw.target_lang == "zh-tw"

    svc_yue = TranslationService(ai_client=FakeAI("唔該晒你幫手"))
    r_yue = await svc_yue.translate("谢谢你的帮忙", target_lang="yue")
    assert r_yue.ok is True and r_yue.provider == "ai"
    assert r_yue.translated_text == "唔該晒你幫手"

