"""#139-3（2026-09-01 钧）回归：粤语客户消息不出中文翻译。

实锤链（996 图：『我今朝早起身見到個天靚到咁，掛住你掛到爆』无普通话译文）：
detect_language 对粤语恒回 zh（实施89 契约，检测端不产变体码）→
① 入站 enrich 的 _lang_matches 判「源=目标」直接跳过候选；
② 就算送到 TranslationService.translate，source==target 的 identity 短路
   也把原文原样吐回。0830 断电 NUL 重建只恢复了 34 语目录/选语页，
   实施89 批次 4 的变体发现字表没有回来——本批把字表下沉服务端
   （detect_zh_variant）并在两处旁路接线。

口径：粤语判定是**旁路**不是覆写——detect_language / conversations.language /
语言投票照旧回 zh（实施89 §三-2 契约不动）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import src.workspace.inbound_translate as IT
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import (
    TranslationService,
    detect_language,
    detect_zh_variant,
)
from src.inbox.normalizer import message_obj
from src.workspace.inbound_translate import enrich_inbound_translations

YUE_TICKET_TEXT = "我今朝早起身見到個天靚到咁，掛住你掛到爆"


@pytest.fixture(autouse=True)
def _clear_xlate_runtime_state():
    IT._FAILED_AT.clear()
    IT._BG_CONVS.clear()
    IT._INFLIGHT_MIDS.clear()
    yield
    IT._FAILED_AT.clear()
    IT._BG_CONVS.clear()
    IT._INFLIGHT_MIDS.clear()


# ── 字表判定（保守：宁漏勿错）────────────────────────────────────────

@pytest.mark.parametrize("text", [
    YUE_TICKET_TEXT,                      # 强特征词组合（今朝/靚到/掛住/咁）
    "你食咗饭未啊",                        # 专用语法字 咗
    "我哋听日一齐去饮茶好唔好",             # 专用语法字 哋
    "呢个嘢好靚啊唔该晒",                   # 嘅族外：嘢 + 好靚 + 唔该
])
def test_detect_zh_variant_yue(text):
    assert detect_zh_variant(text) == "yue", text


@pytest.mark.parametrize("text", [
    "我今天很开心，谢谢你",                  # 普通话
    "我今天很開心，謝謝你",                  # 繁体普通话（不是粤文）
    "唔……让我想想再回复你",                  # 拟声「唔」单证据不判
    "这个方案冇问题",                        # 「冇问题」梗用法单证据不判
    "hello there my friend",               # 非中文
    "",
])
def test_detect_zh_variant_negative(text):
    assert detect_zh_variant(text) == "", text


def test_main_detector_contract_unchanged():
    """实施89 契约：主检测对粤语仍回 zh——变体判定是旁路，不改语言投票口径。"""
    assert detect_language(YUE_TICKET_TEXT) == "zh"


# ── translate() identity 短路旁路 ───────────────────────────────────

class _StubEngine:
    name = "stub"
    available = True

    def __init__(self):
        self.calls = []

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat",
                        glossary_hint=""):
        self.calls.append({"source": source_lang, "target": target_lang})
        return EngineResult("我今天早上起床看到天空美爆了，想你想到不行", self.name, True)


def _svc(engine) -> TranslationService:
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([engine])
    return svc


@pytest.mark.asyncio
async def test_translate_yue_to_zh_not_identity():
    """粤语文本 × 目标 zh：不再走 identity 短路，真到引擎（源=yue）。"""
    eng = _StubEngine()
    res = await _svc(eng).translate(YUE_TICKET_TEXT, target_lang="zh")
    assert res.ok and res.provider != "identity"
    assert eng.calls and eng.calls[0]["source"] == "yue"
    assert "美爆" in res.translated_text


@pytest.mark.asyncio
async def test_translate_mandarin_to_zh_still_identity():
    """普通话 × 目标 zh：identity 短路行为不变（零引擎调用）。"""
    eng = _StubEngine()
    res = await _svc(eng).translate("我今天很开心，谢谢你", target_lang="zh")
    assert res.ok and res.provider == "identity"
    assert not eng.calls


# ── 入站 enrich 全链 ─────────────────────────────────────────────────

def _req():
    from fastapi import FastAPI, Request
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": [], "app": FastAPI()})


@pytest.mark.asyncio
async def test_enrich_translates_yue_inbound():
    """粤语入站（store 语言标签 zh）不再被「源=目标」跳过，译出普通话。"""
    eng = _StubEngine()
    msgs = [message_obj(text=YUE_TICKET_TEXT, direction="in", message_id="m1")]
    msgs[0]["language"] = "zh"   # protocol 落库常态：中文家族标 zh
    out, stats = await enrich_inbound_translations(
        _req(), msgs, conversation_id="telegram:a:1",
        config_manager=SimpleNamespace(config={
            "workspace": {"auto_translate_inbound": {
                "enabled": True, "target_lang": "zh"}}}),
        translation_svc=_svc(eng),
    )
    assert stats["translated"] == 1
    assert out[0]["translated_text"] == "我今天早上起床看到天空美爆了，想你想到不行"
    assert eng.calls and eng.calls[0]["source"] == "yue"


@pytest.mark.asyncio
async def test_enrich_mandarin_inbound_still_skipped():
    """普通话入站 × 目标 zh：仍按同语跳过（不烧引擎、不产译文行）。"""
    eng = _StubEngine()
    msgs = [message_obj(text="我今天很开心，谢谢你", direction="in", message_id="m1")]
    out, stats = await enrich_inbound_translations(
        _req(), msgs, conversation_id="telegram:a:1",
        config_manager=SimpleNamespace(config={
            "workspace": {"auto_translate_inbound": {
                "enabled": True, "target_lang": "zh"}}}),
        translation_svc=_svc(eng),
    )
    assert stats["translated"] == 0
    assert stats["skipped"] == 1
    assert not eng.calls
