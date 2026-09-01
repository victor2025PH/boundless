"""合成后 ASR 回转校验门（synth_verify）：CER 纯函数 + 重合成编排 + 共享转写器登记。

背景（2026-07-24 v5 烘焙实测）：零样本克隆同参考音同文本单次合成 CER 可从 0.08
摆到 0.20+（开头幻觉「好,」/结尾含混「反馈→把盔」）。门 = 合成产物转写回来与
送稿比 CJK 字错率，超阈值同参数重合成取较优；全链 fail-open 绝不阻塞发声。
"""
import asyncio

import src.voice_transcriber as vt_mod
from src.ai.tts_pipeline import _cer_cjk, verify_and_retry_synth
from src.voice_transcriber import (
    get_shared_transcriber,
    register_shared_transcriber,
)

GOOD = "这个方案我建议你稳一点别急着一次全投进去"
GARBLE = "目前目五一个以科莫大表几百各个方案国进也"


class _FakeTranscriber:
    """按序吐预设转写结果；None 条目=转写失败。langs 记每次收到的语种码。"""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0
        self.langs = []

    async def transcribe_voice_message(self, path, language="zh"):
        self.calls += 1
        self.langs.append(language)
        if not self._texts:
            return None
        return self._texts.pop(0)


# ── CER 纯函数 ────────────────────────────────────────────────────────────────
def test_cer_identical_zero():
    assert _cer_cjk(GOOD, GOOD) == 0.0


def test_cer_substitution_ratio():
    assert _cer_cjk("你好地球", "你好世界") == 0.5


def test_cer_no_cjk_not_evaluable():
    assert _cer_cjk("hello", "hello world") == -1.0


def test_cer_marks_and_punct_stripped():
    # 副语言标记/省略号/标点天然剥离，不干扰内容级比对
    assert _cer_cjk("你好，世界。", "[sigh]你好……世界") == 0.0


def test_cer_empty_hyp_is_total_loss():
    assert _cer_cjk("", "你好世界") == 1.0


# ── 门编排 ────────────────────────────────────────────────────────────────────
def _run_gate(tmp_path, stt_texts, resynth_payloads, cfg=None, text=GOOD):
    av = tmp_path / "o.wav"
    av.write_bytes(b"v1")
    calls = {"n": 0}

    def resynth():
        calls["n"] += 1
        av.write_bytes(resynth_payloads[calls["n"] - 1])

    t = _FakeTranscriber(stt_texts)
    info = asyncio.run(verify_and_retry_synth(
        av, text,
        cfg if cfg is not None else
        {"enabled": True, "cer_threshold": 0.30, "max_retries": 1},
        t, resynth))
    return av, info, calls, t


def test_gate_pass_no_retry(tmp_path):
    av, info, calls, t = _run_gate(tmp_path, [GOOD], [])
    assert info == {"cer": 0.0, "retried": 0}
    assert calls["n"] == 0 and t.calls == 1
    assert av.read_bytes() == b"v1"


def test_gate_retry_better_keeps_new(tmp_path):
    av, info, calls, _ = _run_gate(tmp_path, [GARBLE, GOOD], [b"v2"])
    assert calls["n"] == 1
    assert info["retried"] == 1 and info["cer"] == 0.0
    assert av.read_bytes() == b"v2"


def test_gate_retry_worse_rolls_back(tmp_path):
    # 第二次比第一次更差（空转写=全损 1.0 > 首次）→ 回滚第一版字节
    av, info, calls, _ = _run_gate(tmp_path, [GARBLE, ""], [b"v2"])
    assert calls["n"] == 1 and info["retried"] == 1
    assert av.read_bytes() == b"v1"
    assert info["cer"] == _cer_cjk(GARBLE, GOOD)


def test_gate_resynth_failure_restores(tmp_path):
    av = tmp_path / "o.wav"
    av.write_bytes(b"v1")

    def bad_resynth():
        av.write_bytes(b"broken")
        raise RuntimeError("gpu down")

    info = asyncio.run(verify_and_retry_synth(
        av, GOOD, {"enabled": True, "cer_threshold": 0.30, "max_retries": 1},
        _FakeTranscriber([GARBLE]), bad_resynth))
    assert av.read_bytes() == b"v1"          # 失败自动回滚
    assert info["retried"] == 1


def test_gate_disabled_or_unwired_noop(tmp_path):
    av, info, calls, t = _run_gate(tmp_path, [GARBLE], [b"v2"],
                                   cfg={"enabled": False})
    assert info is None and calls["n"] == 0 and t.calls == 0

    info2 = asyncio.run(verify_and_retry_synth(
        tmp_path / "o.wav", GOOD, {"enabled": True}, None, lambda: None))
    assert info2 is None                     # 转写器未登记 → fail-open


def test_gate_stt_unavailable_failopen(tmp_path):
    av, info, calls, _ = _run_gate(tmp_path, [None], [b"v2"])
    assert info is None and calls["n"] == 0
    assert av.read_bytes() == b"v1"


def test_gate_short_text_skipped(tmp_path):
    _, info, calls, t = _run_gate(tmp_path, [GOOD], [], text="好呀")
    assert info is None and t.calls == 0     # 短句不评（中外同口径）
    _, info2, _, t2 = _run_gate(tmp_path, ["hi"], [], text="Hi!")
    assert info2 is None and t2.calls == 0   # 外语短句同样不评


# ── 多语轨（P2-9 2026-08-31：旧「非中文不评」＝新语种开闸后最需要质检的语种
#    恰好零回验——日文怪声就是这么漏网的。中文路径逐字节不变）────────────────

_EN = "See you tomorrow my friend, take care on the way home!"
_JA = "今日はほんとうに楽しかったよ、また一緒に遊ぼうね"


def test_gate_foreign_en_evaluated_and_forced_lang(tmp_path):
    """英文参评：按目标语种强制转写（stt 收到 en）、通用字符级 CER、结果带 lang。"""
    _, info, calls, t = _run_gate(tmp_path, [_EN], [], text=_EN)
    assert info == {"cer": 0.0, "retried": 0, "lang": "en"}
    assert calls["n"] == 0 and t.calls == 1
    assert t.langs == ["en"]


def test_gate_foreign_ja_evaluated(tmp_path):
    """日文参评（旧行为整条跳过）：假名+汉字全进字符级比对，stt 收到 ja。"""
    _, info, _, t = _run_gate(tmp_path, [_JA], [], text=_JA)
    assert info == {"cer": 0.0, "retried": 0, "lang": "ja"}
    assert t.langs == ["ja"]


def test_gate_foreign_garble_retries_and_keeps_better(tmp_path):
    """外语坏 take（转写与送稿天差地别）→ 重合成取较优（与中文轨同编排）。"""
    av, info, calls, _ = _run_gate(
        tmp_path, ["totally unrelated words here", _EN], [b"v2"], text=_EN)
    assert calls["n"] == 1
    assert info["retried"] == 1 and info["cer"] == 0.0 and info["lang"] == "en"
    assert av.read_bytes() == b"v2"


def test_gate_multilingual_off_restores_old_skip(tmp_path):
    """multilingual:false → 旧行为：非中文一律不评（运营逃生门）。"""
    _, info, calls, t = _run_gate(
        tmp_path, [_EN], [],
        cfg={"enabled": True, "multilingual": False}, text=_EN)
    assert info is None and calls["n"] == 0 and t.calls == 0


def test_cer_chars_normalization_and_edge():
    """通用字符级 CER：大小写/标点/空白归一；ref 归一后为空=不可评。"""
    from src.ai.tts_pipeline import _cer_chars
    assert _cer_chars("See you, TOMORROW!", "see you tomorrow") == 0.0
    assert _cer_chars("こんにちは、せかい。", "こんにちは せかい") == 0.0
    assert _cer_chars("", _EN) == 1.0
    assert _cer_chars("anything", "！？…") == -1.0


# ── 共享转写器登记 ────────────────────────────────────────────────────────────
def test_shared_transcriber_first_wins():
    old = vt_mod._SHARED_TRANSCRIBER
    try:
        vt_mod._SHARED_TRANSCRIBER = None
        assert get_shared_transcriber() is None
        a, b = _FakeTranscriber([]), _FakeTranscriber([])
        register_shared_transcriber(None)    # None 忽略
        assert get_shared_transcriber() is None
        register_shared_transcriber(a)
        register_shared_transcriber(b)       # 重复登记忽略
        assert get_shared_transcriber() is a
    finally:
        vt_mod._SHARED_TRANSCRIBER = old
