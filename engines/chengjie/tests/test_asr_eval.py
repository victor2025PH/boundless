# -*- coding: utf-8 -*-
"""ASR 转写质量评测轨（ASR P1）+ 坐席改正台账 门禁。零网络零模型：转写函数用假函数。"""
from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from src.eval.asr_eval import (
    DEFAULT_MANIFEST,
    ASRSample,
    cer,
    duration_bucket,
    evaluate_asr,
    format_asr_report,
    load_asr_samples,
    normalize_for_cer,
)
from src.inbox import asr_corrections as corr

_ROOT = Path(__file__).resolve().parents[1]


def _wav(path: Path, seconds: float) -> str:
    rate, byte_rate = 16000, 32000
    data_len = int(seconds * byte_rate)
    hdr = b"RIFF" + struct.pack("<I", 36 + data_len) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, byte_rate, 2, 16)
    hdr += b"data" + struct.pack("<I", data_len)
    path.write_bytes(hdr + b"\x00" * data_len)
    return str(path)


# ── CER ─────────────────────────────────────────────────────────────────────
def test_cer_basic_and_normalization():
    assert cer("你好", "你好") == 0.0
    assert cer("今天天气不错", "今天天氣不錯") == 0.0          # 繁简不算错（opencc 缺失时恒等仍相等）
    assert cer("今天，天气 不错！", "今天天气不错") == 0.0      # 标点/空格不算错
    assert cer("Hello World", "hello world") == 0.0
    assert cer("大造成呢", "打造成呢") == 0.25                   # 1/4
    assert cer("你好", "") == 1.0 and cer("", "") == 0.0 and cer("", "x") == 1.0
    assert normalize_for_cer("  嗯嗯，今天。 ") == "嗯嗯今天"


def test_duration_bucket():
    assert duration_bucket(1.4) == "<2s" and duration_bucket(3) == "2-5s"
    assert duration_bucket(7) == "5-10s" and duration_bucket(11) == "10-30s"
    assert duration_bucket(45) == ">=30s" and duration_bucket(None) == "unknown"


# ── 清单 + 改正合并 ─────────────────────────────────────────────────────────
def test_load_samples_manifest_and_corrections(tmp_path):
    a = _wav(tmp_path / "a.wav", 3.0)
    manifest = tmp_path / "m.jsonl"
    manifest.write_text(
        "# 注释行\n"
        + json.dumps({"audio": "a.wav", "ref": "你好呀", "lang": "zh", "tags": ["t"]}, ensure_ascii=False) + "\n"
        + json.dumps({"audio": "missing.wav", "ref": "x"}) + "\n"
        + json.dumps({"audio": "a.wav"}) + "\n"
        + "not json\n", encoding="utf-8")
    cpath = tmp_path / "asr_corrections.jsonl"
    rec = corr.build_correction(conversation_id="wa:a:1", machine_text="大造成呢", corrected_text="打造成呢",
                                audio_path=a, lang="zh", agent="op", platform="whatsapp", ts=1.0)
    assert rec["audio_sha1"] and rec["corrected_text"] == "打造成呢"
    assert corr.append_correction(cpath, rec)
    assert corr.append_correction(cpath, corr.build_correction(
        conversation_id="c2", machine_text="", corrected_text="音频不在", audio_path=str(tmp_path / "gone.wav")))
    loaded = load_asr_samples(str(manifest), corrections_path=str(cpath))
    srcs = [(s.source, s.ref) for s in loaded["samples"]]
    assert ("manifest", "你好呀") in srcs and ("correction", "打造成呢") in srcs
    reasons = sorted(sk["reason"] for sk in loaded["skipped"])
    assert reasons == ["audio_missing", "audio_missing", "bad_json", "missing_field"]
    # 台账读取：坏行跳过、limit 取尾
    (cpath).open("a", encoding="utf-8").write("garbage\n")
    assert len(corr.iter_corrections(cpath)) == 2
    assert corr.iter_corrections(cpath, limit=1)[0]["corrected_text"] == "音频不在"
    assert corr.iter_corrections(tmp_path / "nope.jsonl") == []
    with pytest.raises(ValueError):
        corr.build_correction(conversation_id="c", machine_text="x", corrected_text="  ")


def test_repo_manifest_resolves_probe_fixture():
    fixture = _ROOT / "assets" / "probe" / "asr_probe.wav"
    if not fixture.is_file():
        pytest.skip("probe fixture missing in this checkout")
    loaded = load_asr_samples(str(_ROOT / DEFAULT_MANIFEST))
    assert loaded["samples"] and Path(loaded["samples"][0].audio) == fixture.resolve()
    assert loaded["samples"][0].lang == "zh"


def test_corrections_path_resolution(tmp_path):
    class _CM:
        config_path = str(tmp_path / "config" / "config.yaml")
    assert corr.corrections_path(_CM()) == tmp_path / "config" / corr.FILENAME
    assert corr.corrections_path(config_dir=str(tmp_path)) == tmp_path / corr.FILENAME
    assert corr.corrections_path(None) is None


def test_hotword_candidates_from_corrections():
    recs = [
        {"machine_text": "治療的价格", "corrected_text": "智聊的价格"},
        {"machine_text": "治疗多少钱", "corrected_text": "智聊多少钱"},
        {"machine_text": "回复我", "corrected_text": "回复我"},
    ]
    cands = corr.hotword_candidates(recs, min_count=2)
    assert cands and cands[0]["word"] == "智聊" and cands[0]["count"] == 2


# ── 评测 ────────────────────────────────────────────────────────────────────
def _samples(tmp_path, n):
    out = []
    for i in range(n):
        out.append(ASRSample(audio=_wav(tmp_path / f"s{i}.wav", 1.0 + i), ref=f"第{i}句测试文本", lang="zh"))
    return out


def test_evaluate_asr_insufficient_then_pass_fail(tmp_path):
    samples = _samples(tmp_path, 2)
    rep = evaluate_asr(samples, lambda p, l: ("第0句测试文本", {"duration": 1.0}) if "s0" in p else "第1句测试文本")
    assert rep["available"] and rep["status"] == "insufficient" and rep["passed"] is None
    assert rep["summary"]["mean_cer"] == 0.0
    six = _samples(tmp_path, 6)
    rep2 = evaluate_asr(six, lambda p, l: "第X句测试文本", min_samples=5, threshold=0.2)
    assert rep2["status"] == "pass" and rep2["passed"] is True     # 1/7 ≈ 0.143 ≤ 0.2
    assert rep2["by_lang"]["zh"]["n"] == 6 and set(rep2["by_bucket"]) >= {"<2s", "2-5s", "5-10s"}
    rep3 = evaluate_asr(six, lambda p, l: "完全不对的话", min_samples=5, threshold=0.2)
    assert rep3["status"] == "fail" and rep3["passed"] is False
    assert len(rep3["summary"]["worst"]) == 5
    txt = format_asr_report(rep3, [{"audio": "x", "reason": "audio_missing", "source": "manifest"}])
    assert "FAIL" in txt and "按语种" in txt and "跳过 x" in txt


def test_evaluate_asr_handles_errors_and_meta(tmp_path):
    samples = _samples(tmp_path, 1)

    def _boom(p, l):
        raise RuntimeError("endpoint down")
    rep = evaluate_asr(samples, _boom)
    assert rep["rows"][0]["cer"] == 1.0 and "RuntimeError" in rep["rows"][0]["error"]
    rep2 = evaluate_asr(samples, lambda p, l: ("第0句测试文本", {"duration": 12.0, "language": "zh"}))
    assert rep2["rows"][0]["bucket"] == "10-30s"
    assert evaluate_asr([], lambda p, l: "")["available"] is False
