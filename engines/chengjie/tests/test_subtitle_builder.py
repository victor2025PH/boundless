"""P4（2026-08-18）：分段→双语 SRT 构建 单测。

时间戳格式化边界 / 双语 cue 拼装 / 失败段回退原文 / identity 不计 translated /
无分段诚实拒绝（绝不伪造时间轴）/ 空段跳过与重编号。
"""
import pytest

from src.ai.subtitle_builder import (
    build_bilingual_srt,
    format_srt_time,
    segments_to_srt,
)
from src.ai.translation_engines import EngineResult, EngineRouter
from src.ai.translation_service import TranslationService


class _StubEngine:
    def __init__(self, name="ai", *, fail_on=None, identity_on=None):
        self.name = name
        self._fail = set(fail_on or [])
        self._ident = set(identity_on or [])

    @property
    def available(self):
        return True

    def supports_target(self, t):
        return True

    async def translate(self, text, *, source_lang, target_lang, style="chat", glossary_hint=""):
        if text in self._fail:
            return EngineResult("", self.name, False, error="boom")
        if text in self._ident:
            return EngineResult(text, self.name, True)
        return EngineResult(f"{text}#tr", self.name, True)


def _xlate(engine=None):
    s = TranslationService(ai_client=None)
    s._router = EngineRouter([engine or _StubEngine()])
    return s


def test_format_srt_time_boundaries():
    assert format_srt_time(0) == "00:00:00,000"
    assert format_srt_time(1.5) == "00:00:01,500"
    assert format_srt_time(59.9995) == "00:01:00,000"      # 毫秒进位不溢出
    assert format_srt_time(3661.042) == "01:01:01,042"
    assert format_srt_time(-5) == "00:00:00,000"           # 负值按 0


def test_segments_to_srt_bilingual_and_renumber():
    segs = [
        {"start": 0.0, "end": 1.2, "text": "Hello"},
        {"start": 1.2, "end": 2.0, "text": ""},            # 空段跳过
        {"start": 2.0, "end": 3.5, "text": "World"},
    ]
    srt = segments_to_srt(segs, ["你好", "", "世界"], bilingual=True)
    blocks = srt.strip().split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].splitlines() == [
        "1", "00:00:00,000 --> 00:00:01,200", "Hello", "你好"]
    # 空段被跳过后重编号为 2（不是 3）
    assert blocks[1].splitlines()[0] == "2"
    assert "00:00:02,000 --> 00:00:03,500" in blocks[1]


def test_segments_to_srt_translated_only_mode():
    srt = segments_to_srt([{"start": 0, "end": 1, "text": "Hi"}], ["嗨"], bilingual=False)
    body = srt.strip().splitlines()
    assert body[2] == "嗨" and "Hi" not in srt


async def test_build_bilingual_srt_end_to_end():
    segs = [{"start": 0.0, "end": 1.0, "text": "Hello"},
            {"start": 1.0, "end": 2.0, "text": "World"}]
    out = await build_bilingual_srt(segs, _xlate(), target_lang="zh", source_lang="en")
    assert out["ok"] is True
    assert out["stats"]["translated"] == 2
    assert "Hello\nHello#tr" in out["srt_text"]
    assert "00:00:01,000 --> 00:00:02,000" in out["srt_text"]


async def test_build_srt_failure_keeps_original_cue():
    segs = [{"start": 0, "end": 1, "text": "keep"},
            {"start": 1, "end": 2, "text": "bad"}]
    out = await build_bilingual_srt(
        segs, _xlate(_StubEngine(fail_on=["bad"])), target_lang="zh", source_lang="en")
    assert out["ok"] is True and out["stats"]["failed"] == 1
    blocks = out["srt_text"].strip().split("\n\n")
    assert blocks[1].splitlines()[-1] == "bad"             # 失败段只出原文行


async def test_build_srt_no_segments_honest_fail():
    out = await build_bilingual_srt([], _xlate(), target_lang="zh")
    assert out["ok"] is False and out["reason"] == "no_segments"
    out2 = await build_bilingual_srt(
        [{"start": 0, "end": 1, "text": "  "}], _xlate(), target_lang="zh")
    assert out2["ok"] is False and out2["reason"] == "no_segments"


async def test_build_srt_identity_not_counted_translated():
    segs = [{"start": 0, "end": 1, "text": "同文"}]
    out = await build_bilingual_srt(
        segs, _xlate(_StubEngine(identity_on=["同文"])), target_lang="zh")
    assert out["ok"] is True
    assert out["stats"]["translated"] == 0
    assert out["srt_text"].strip().splitlines()[-1] == "同文"
