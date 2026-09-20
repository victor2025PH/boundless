"""198/176 GPU ASR 服务（scripts/asr176/asr_server.py）解码纪律纯函数门禁（ASR P0）。

不加载模型、不起服务：只钉 ``decode_options`` / ``summarize_segments`` 两个纯函数——
它们决定了「短语音是否被 VAD 整段吃掉」「热词是否进 initial_prompt」「置信度是否
回给客户端」。fastapi 缺失则跳过（脚本模块顶层建 app）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

_SRV = Path(__file__).resolve().parents[1] / "scripts" / "asr176" / "asr_server.py"


@pytest.fixture(scope="module")
def srv():
    spec = importlib.util.spec_from_file_location("asr176_server_under_test", _SRV)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_decode_options_pins_discipline_and_prompt(srv):
    o = srv.decode_options("智聊ChatX、无界科技")
    assert o["condition_on_previous_text"] is False
    assert o["beam_size"] == 5
    assert o["no_speech_threshold"] == 0.6 and o["log_prob_threshold"] == -1.0
    assert o["compression_ratio_threshold"] == 2.4
    assert o["vad_filter"] is True
    # VAD 参数＝**只**传旧服务传过的 min_silence 300，其余交给库默认（09-12 A/B：放宽到
    # 0.35/100ms → 探针夹具变成不确定乱码；钉 min_speech=250（旧版库默认，1.2.1 是 0）
    # → 开头「嗯嗯」块被丢、稳定输出错句「這件衣服也不錯」。没量过的值不许钉）
    assert o["vad_parameters"] == {"min_silence_duration_ms": 300}
    assert o["initial_prompt"] == "智聊ChatX、无界科技"
    assert "initial_prompt" not in srv.decode_options("   ")
    assert srv.decode_options(None, beam=1)["beam_size"] == 1
    # 显式覆写才进参数
    o2 = srv.decode_options(None, vad_threshold=0.4, vad_min_speech_ms=100)
    assert o2["vad_parameters"] == {"min_silence_duration_ms": 300, "threshold": 0.4,
                                    "min_speech_duration_ms": 100}


def test_decode_options_no_vad_pass(srv):
    o = srv.decode_options(None, vad=False)
    assert o["vad_filter"] is False and "vad_parameters" not in o
    assert o["condition_on_previous_text"] is False


def test_decode_options_prompt_capped(srv):
    o = srv.decode_options("词" * 1000)
    assert len(o["initial_prompt"]) == srv.PROMPT_MAX_CHARS


def test_short_rescue_acceptance_floor(srv):
    # 09-12 A/B 实锤：1.47s 片段无 VAD 解出 "Thank you."（p=0.42, lp=-0.90）＝幻觉 → 拒
    assert not srv.accept_short_rescue("Thank you.", {"avg_logprob": -0.90, "language_probability": 0.42})
    # 自信的短应答 → 救回
    assert srv.accept_short_rescue("嗯好的", {"avg_logprob": -0.3, "language_probability": 0.95,
                                           "compression_ratio": 0.8})
    # 缺证据 / 空文本 / 复读（压缩比高）→ 拒
    assert not srv.accept_short_rescue("嗯", {"language_probability": 0.95})
    assert not srv.accept_short_rescue("", {"avg_logprob": -0.1, "language_probability": 0.99})
    assert not srv.accept_short_rescue("嗯嗯嗯嗯嗯嗯嗯", {"avg_logprob": -0.2, "language_probability": 0.9,
                                                    "compression_ratio": 3.2})
    # 阈值可调
    assert srv.accept_short_rescue("ok", {"avg_logprob": -0.9, "language_probability": 0.5},
                                   min_logprob=-1.0, min_langprob=0.4)
    assert srv.SHORT_RESCUE_SEC == 2.0


def test_summarize_segments_aggregates_confidence(srv):
    segs = [
        SimpleNamespace(text=" 你好", start=0.0, end=0.8, avg_logprob=-0.2,
                        no_speech_prob=0.05, compression_ratio=1.1),
        SimpleNamespace(text="呀", start=0.8, end=1.5, avg_logprob=-0.6,
                        no_speech_prob=0.4, compression_ratio=1.3),
        SimpleNamespace(text="  ", start=1.5, end=1.6, avg_logprob=-2.0,
                        no_speech_prob=0.9, compression_ratio=0.9),
    ]
    info = SimpleNamespace(language="zh", language_probability=0.97, duration=1.6)
    text, seg_list, conf = srv.summarize_segments(segs, info)
    assert text == "你好呀"
    assert [s["text"] for s in seg_list] == ["你好", "呀"]     # 空白段不进 SRT 列表
    assert abs(conf["avg_logprob"] - round((-0.2 - 0.6 - 2.0) / 3, 4)) < 1e-9
    assert conf["no_speech_prob"] == 0.9 and conf["compression_ratio"] == 1.3
    assert conf["language"] == "zh" and conf["language_probability"] == 0.97
    assert conf["duration"] == 1.6


def test_summarize_segments_missing_fields_absent(srv):
    text, seg_list, conf = srv.summarize_segments([SimpleNamespace(text="hi", start=0, end=1)], None)
    assert text == "hi" and seg_list == [{"start": 0.0, "end": 1.0, "text": "hi"}]
    assert "avg_logprob" not in conf and "language" not in conf
    assert srv.summarize_segments([], None) == ("", [], {})
