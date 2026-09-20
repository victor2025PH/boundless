# -*- coding: utf-8 -*-
"""引擎拒绝话术拦截门禁（P1-198，2026-08-05）。

生产实锤（198 收件箱 6/25 历史消息）：坐席经出站翻译发「1」，LLM 引擎输出
「谢谢，您只提供了『1』，没有可翻译的内容。请您提供需要翻译的文本」——
这句客套话被当译文**原样发给了客户**。两层门禁：

1. 纯函数 ``looks_like_engine_refusal``：双正交信号（译文谈论翻译本身而源文
   没有 + 拒绝框架词），重点覆盖**不该误伤**的边界（源文本来就聊翻译/正常
   意译/短文本直译）。
2. ``TranslationService.translate`` 拦截：命中 → ok=False +
   error=engine_refusal + 回落原文；绝不进翻译记忆（L2）。
"""
from src.ai.translation_confidence import looks_like_engine_refusal
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService

_PROD_REFUSAL = "谢谢，您只提供了『1』，没有可翻译的内容。请您提供需要翻译的文本，我会尽力为您翻译。"


class _StubEngine:
    def __init__(self, name, *, out, ok=True):
        self.name = name
        self._out = out
        self._ok = ok

    @property
    def available(self):
        return True

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang,
                        style="chat", glossary_hint=""):
        if not self._ok:
            return EngineResult("", self.name, False, error="boom")
        return EngineResult(self._out, self.name, True)


# ── 纯函数：检测语义 ────────────────────────────────────────────────────

def test_refusal_detects_production_sample():
    assert looks_like_engine_refusal("1", _PROD_REFUSAL)


def test_refusal_detects_english_boilerplate():
    assert looks_like_engine_refusal(
        "??", "Sorry, there is nothing to translate. Please provide the text "
              "you would like me to translate.")


def test_refusal_not_triggered_on_legit_translations():
    # 正常意译（无翻译元词）
    assert not looks_like_engine_refusal("你好呀", "Hey there!")
    # 短文本直译
    assert not looks_like_engine_refusal("1", "1")
    assert not looks_like_engine_refusal("好", "OK")
    # 译文长于源文但内容正常（zh→en 天然膨胀）
    assert not looks_like_engine_refusal(
        "明天上午十点开会", "The meeting is at 10 a.m. tomorrow morning")


def test_refusal_not_triggered_when_source_talks_about_translation():
    """源文本来就聊翻译 → 译文出现 translate 是合格翻译，不许误伤。"""
    assert not looks_like_engine_refusal(
        "帮我把这句话翻译成英文", "Please help me translate this into English")
    # 元词在源文（翻译）+ 框架词在译文（please）也不许误伤
    assert not looks_like_engine_refusal(
        "这句需要翻译吗？", "Does this need translating, please?")


def test_refusal_meta_without_frame_words_passes():
    """只谈到翻译但没有拒绝框架（正常聊翻译话题的意译）→ 放行。"""
    assert not looks_like_engine_refusal(
        "这个软件很好用", "This translation software works great")


# ── #115（0831 钧原图 906）：实弹漏网句 + 空源硬化 ─────────────────────────

_115_REFUSAL = "您没有提供需要翻译的消息内容。请发送您想翻译的文本。"


def test_115_refusal_detects_field_sample():
    """钧机实锤原句：「没有提供/请发送」框架形态此前不在词表 → 曾以气泡进会话流。"""
    assert looks_like_engine_refusal("[贴纸]", _115_REFUSAL)
    assert looks_like_engine_refusal("??", _115_REFUSAL)
    # 繁体形态同拦
    assert looks_like_engine_refusal(
        "??", "您沒有提供需要翻譯的訊息內容。請發送您想翻譯的文本。")


def test_115_refusal_empty_source_no_longer_bypasses():
    """空源 + meta 拒绝话术＝最典型该拦的形态（旁路直连引擎的防线）。"""
    assert looks_like_engine_refusal("", _115_REFUSAL)
    # 空源 + 正常文本仍放行（宁漏勿误伤不变）
    assert not looks_like_engine_refusal("", "Hello there!")


async def test_115_media_placeholder_short_circuits_before_engine():
    """纯媒体占位整串（「[图片]」）→ identity 早退，绝不送引擎。"""
    class _Boom:
        name = "ai"
        available = True

        def supports_target(self, target_lang):
            return True

        async def translate(self, *a, **kw):
            raise AssertionError("placeholder must not reach engine")

    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_Boom()])
    for ph in ("[图片]", "[语音消息]", " [视频] "):
        res = await svc.translate(ph, target_lang="en", source_lang="zh")
        assert res.ok and res.provider == "identity"
        assert res.translated_text == ph
    # 带配文的占位不早退（有真实正文要译）
    svc2 = TranslationService(ai_client=None)
    svc2._router = EngineRouter([_StubEngine("ai", out="photo from today")])
    res2 = await svc2.translate("[图片] 今天拍的", target_lang="en", source_lang="zh")
    assert res2.ok and res2.translated_text == "photo from today"


# ── 服务级拦截 ──────────────────────────────────────────────────────────

async def test_service_intercepts_refusal_and_falls_back():
    # M-1 B #234 起单字符（"1"）不进引擎（identity 早退），改用能到引擎的短句验拦截
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out=_PROD_REFUSAL)])
    res = await svc.translate("ok ok", target_lang="en", source_lang="zh")
    assert res.ok is False
    assert res.error == "engine_refusal"
    assert res.translated_text == "ok ok"   # 回落原文，绝不把客套话给到调用方当译文


async def test_service_refusal_never_enters_translation_memory():
    calls = {"put": 0}

    class _Mem:
        def get(self, key):
            return None

        def put(self, **kw):
            calls["put"] += 1

    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out=_PROD_REFUSAL)])
    svc._memory_store = _Mem()
    res = await svc.translate("ok ok", target_lang="en", source_lang="zh")
    assert res.ok is False and calls["put"] == 0


async def test_service_normal_translation_unaffected():
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([_StubEngine("ai", out="Hello!")])
    res = await svc.translate("你好", target_lang="en", source_lang="zh")
    assert res.ok and res.translated_text == "Hello!"
