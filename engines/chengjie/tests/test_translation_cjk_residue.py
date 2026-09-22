"""译文残留汉字（专名被整段保留）→ 定向重译一次。

背景（Bug 群 2026-09-22）：中→英手动翻译时「智聊」「西贡十五日」等带引号的专名
被引擎按"品牌名保留"规则原样留在英文里。这里验证：
- 非 CJK 目标语且译文含汉字 → 用同一引擎带残留词提示重译一次；
- 重译成功（无残留）→ 采用重译结果；重译仍有残留但汉字更少 → 也采用；
- 重译失败 → 退回首译，不报错；
- CJK 目标语（zh/ja/ko）不触发。
"""
import pytest

from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_fidelity import FIDELITY_PROMPT_RULE
from src.ai.translation_service import TranslationService


class _SeqEngine:
    """按调用次序返回预设译文，并记录每次收到的 glossary_hint。"""

    def __init__(self, outs, *, fail_second=False):
        self.name = "ai"
        self._outs = list(outs)
        self._fail_second = fail_second
        self.calls = []

    @property
    def available(self):
        return True

    def supports_target(self, target_lang):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        self.calls.append(glossary_hint)
        if self._fail_second and len(self.calls) == 2:
            return EngineResult("", self.name, False, error="boom")
        i = min(len(self.calls) - 1, len(self._outs) - 1)
        return EngineResult(self._outs[i], self.name, True)


def _svc(engine):
    svc = TranslationService(ai_client=None)
    svc._router = EngineRouter([engine])
    return svc


@pytest.mark.asyncio
async def test_residue_retry_adopts_clean_second_pass():
    eng = _SeqEngine(['Which software exactly is "智聊"?',
                      'Which software exactly is "Zhiliao"?'])
    res = await _svc(eng).translate('「智聊」具体是哪个软件', target_lang="en", source_lang="zh")
    assert res.ok and res.translated_text == 'Which software exactly is "Zhiliao"?'
    assert len(eng.calls) == 2
    assert "智聊" in eng.calls[1] and "English" in eng.calls[1]


@pytest.mark.asyncio
async def test_residue_retry_falls_back_when_second_pass_fails():
    eng = _SeqEngine(['the "西贡十五日" guide site'], fail_second=True)
    res = await _svc(eng).translate('「西贡十五日」攻略站', target_lang="en", source_lang="zh")
    assert res.ok and res.translated_text == 'the "西贡十五日" guide site'
    assert len(eng.calls) == 2


@pytest.mark.asyncio
async def test_residue_retry_prefers_fewer_cjk_chars():
    eng = _SeqEngine(['A 智聊 and 西贡十五日', 'A Zhiliao and 西贡十五日'])
    res = await _svc(eng).translate('智聊和西贡十五日', target_lang="en", source_lang="zh")
    assert res.translated_text == 'A Zhiliao and 西贡十五日'


@pytest.mark.asyncio
async def test_no_retry_for_cjk_target_or_clean_output():
    eng = _SeqEngine(['智聊是什么软件'])
    res = await _svc(eng).translate("what is Zhiliao", target_lang="zh", source_lang="en")
    assert res.ok and len(eng.calls) == 1
    eng2 = _SeqEngine(['Which software is Zhiliao?'])
    res2 = await _svc(eng2).translate('智聊是哪个软件', target_lang="en", source_lang="zh")
    assert res2.ok and len(eng2.calls) == 1


def test_prompt_rules_forbid_source_script_residue():
    from src.ai.translation_engines import AIEngine
    for rule in (FIDELITY_PROMPT_RULE, AIEngine._BARE_TRANSLATION_SYSTEM):
        assert "different writing system" in rule
        assert "Chinese characters" in rule
