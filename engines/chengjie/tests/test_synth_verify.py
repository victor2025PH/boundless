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
    """按序吐预设转写结果；None 条目=转写失败。"""

    def __init__(self, texts):
        self._texts = list(texts)
        self.calls = 0

    async def transcribe_voice_message(self, path, language="zh"):
        self.calls += 1
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


def test_gate_short_or_foreign_text_skipped(tmp_path):
    _, info, calls, t = _run_gate(tmp_path, [GOOD], [], text="好呀")
    assert info is None and t.calls == 0     # 短句不评
    _, info2, _, t2 = _run_gate(tmp_path, ["ok"], [], text="See you tomorrow!")
    assert info2 is None and t2.calls == 0   # 非中文不评


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
