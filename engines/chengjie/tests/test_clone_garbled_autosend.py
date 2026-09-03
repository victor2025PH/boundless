# -*- coding: utf-8 -*-
"""#161 自动链事后腿：克隆成品判「念错」→ 作废并改走标准声（2026-09-04）。

0903 补的是**试听链**的事后腿（voice_routes._redispatch_garbled_to_edge）；
自动出站链（A 线语音回复 / B 线 autosend / 主动触达）当时还只有合成前的语种
能力闸。表准了就够，但表来不及更新、或 hub 换引擎后能力变了时，自动链没有
第二道——1145 实录的日语乱音正是从这条路发给客户的。

本文件钉住：
  · 正常产物零成本零影响（CER≈0 的绝大多数流量连文件都不读）；
  · 判乱码 → **作废这版成品**（清 ok/audio_path + 删文件）后才走兜底——只往下
    走而不清的话，兜底再失败时末尾 return rv 会把 ok=True + 乱音路径原样交出去，
    比不修更糟（实现时正是先写错成这样）；
  · 兜底音色按语种对齐、extra 让前端归因成「语种不支持」而非「通道中断」；
  · 判定本身绝不成为故障点（能量判不了/文件没了/异常 → 放行）。
"""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import pytest

from src.ai.speech_verdict import garbled_suspect
from src.ai.tts_pipeline import TTSPipeline, TTSResult

#: 1145 证据群形状：日语轨 CER 0.85、重合成过一次仍这么差、字节有能量
_GARBLED_SV = {"cer": 0.85, "retried": 1, "lang": "ja", "hyp_chars": 22}
_CLEAN_SV = {"cer": 0.03, "retried": 0, "lang": "ja", "hyp_chars": 30}
_JA_TEXT = "どこにいるの。ご飯は食べた？一緒に遊びに行こうよ。"


def _loud_wav(path: Path, sec: float = 1.0, sr: int = 22050) -> Path:
    """有能量的真 wav（振幅足以让 detect_silent_audio 判「有声」）。"""
    import math
    import struct

    n = int(sec * sr)
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(i * 0.07))) for i in range(n))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return path


def _pipeline() -> TTSPipeline:
    return TTSPipeline({"enabled": True, "backend": "avatar_clone"})


def _result(path: Path, sv, text: str = _JA_TEXT) -> TTSResult:
    rv = TTSResult(ok=True, audio_path=str(path), text=text,
                   provider="avatar_clone", format="wav", duration_sec=1.0)
    rv.extra = {"synth_verify": dict(sv)} if sv else {}
    return rv


# ══ 1. 零成本前置筛（正常流量一次文件都不读） ═══════════════════════════════

def test_suspect_is_pure_and_cheap():
    """garbled_suspect 只看已在手的回验证据——不碰文件系统。"""
    assert garbled_suspect(_GARBLED_SV) is True
    assert garbled_suspect(_CLEAN_SV) is False
    assert garbled_suspect(None) is False
    assert garbled_suspect({}) is False
    # 中间地带（CER 过门槛但没重合成过、又没高到单发定案）→ 不疑
    assert garbled_suspect({"cer": 0.40, "retried": 0, "hyp_chars": 30}) is False
    assert garbled_suspect({"cer": 0.40, "retried": 1, "hyp_chars": 30}) is True
    assert garbled_suspect({"cer": 0.72, "retried": 0, "hyp_chars": 30}) is True
    # 垃圾形状不抛
    assert garbled_suspect({"cer": "x", "hyp_chars": "y"}) is False


def test_clean_take_never_reads_the_file(tmp_path, monkeypatch):
    """正常产物：连能量检测都不该被调用（成本纪律的行为钉）。"""
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("正常产物不该做能量检测")

    monkeypatch.setattr("src.ai.avatar_voice.detect_silent_audio", _boom)
    rv = _result(_loud_wav(tmp_path / "ok.wav"), _CLEAN_SV)
    assert asyncio.run(_pipeline()._clone_take_garbled(rv)) == ""
    assert calls["n"] == 0


# ══ 2. 判乱码 → 返回语种 ════════════════════════════════════════════════════

def test_garbled_take_detected_with_lang(tmp_path):
    rv = _result(_loud_wav(tmp_path / "bad.wav"), _GARBLED_SV)
    assert asyncio.run(_pipeline()._clone_take_garbled(rv)) == "ja"


def test_silent_take_is_not_garbled(tmp_path):
    """真静音属 silent 的地盘，不抢（那条另有既有处置）。"""
    p = tmp_path / "hollow.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(b"\x00\x00" * 22050)
    assert asyncio.run(_pipeline()._clone_take_garbled(_result(p, _GARBLED_SV))) == ""


def test_never_raises_on_bad_inputs(tmp_path, monkeypatch):
    """判定绝不成为故障点：文件不在 / 能量判不了 / 探测器炸 → 一律放行。"""
    pipe = _pipeline()
    assert asyncio.run(pipe._clone_take_garbled(
        _result(tmp_path / "missing.wav", _GARBLED_SV))) == ""
    blob = tmp_path / "blob.mp3"
    blob.write_bytes(b"AAAA" * 64)          # 判不了能量 → None → 不改判
    rv = _result(blob, _GARBLED_SV)
    rv.format = "mp3"
    assert asyncio.run(pipe._clone_take_garbled(rv)) == ""

    def _boom(*a, **k):
        raise RuntimeError("detector down")

    monkeypatch.setattr("src.ai.avatar_voice.detect_silent_audio", _boom)
    assert asyncio.run(pipe._clone_take_garbled(
        _result(_loud_wav(tmp_path / "x.wav"), _GARBLED_SV))) == ""


# ══ 3. 作废这版成品（本腿的要害） ═══════════════════════════════════════════

def test_discard_clears_success_state_and_deletes_file(tmp_path):
    """只往下走兜底而不清成功态的话，兜底再失败时末尾 return rv 会把
    ok=True + 乱音路径原样交给调用方——发出去的还是乱音，比不修更糟。"""
    p = _loud_wav(tmp_path / "bad.wav")
    rv = _result(p, _GARBLED_SV)
    _pipeline()._discard_garbled_take(rv, "ja", "avatar_clone")
    assert rv.ok is False
    assert rv.audio_path == ""
    assert rv.duration_sec == -1.0
    assert not p.exists()          # 乱音文件不留在盘上等着被别人捡去发


def test_discard_records_into_the_same_ledger(tmp_path, monkeypatch):
    """事前拦与事后判进同一本账——分两本会让「该给哪门语言补引擎」少看一半。"""
    seen = []

    class _Stats:
        @staticmethod
        def record_blocked(lang):
            seen.append(lang)

    monkeypatch.setattr("src.ai.voice_synth_stats.get_voice_synth_stats",
                        lambda: _Stats())
    _pipeline()._discard_garbled_take(
        _result(_loud_wav(tmp_path / "b.wav"), _GARBLED_SV), "ja", "avatar_clone")
    assert seen == ["ja"]


# ══ 4. 端到端：自动链判乱码 → 改走标准声 ════════════════════════════════════

def _synth_with(monkeypatch, tmp_path, sv, *, edge_ok=True):
    """跑一遍 synthesize()：克隆分支产「有能量的 wav」+ 给定回验证据；
    edge 兜底写一个 mp3。返回 (result, 调用记录)。

    ``voice_langs: [zh, en, ja]``＝**运营显式为日语背书**（#161 后 ja 不在缺省
    表里，要开必须这么写）。这正是本腿存在的意义：闸门尊重运营的登记放行，
    而实际念出来仍是乱音——事前的表信不过时，事后这条腿兜住。
    """
    pipe = TTSPipeline({
        "enabled": True, "backend": "avatar_clone",
        "fallback_on_error": True, "fallback_backend": "edge_tts",
        "fallback_voice": "zh-CN-XiaoxiaoNeural",
        "out_dir": str(tmp_path), "format": "mp3",
        "avatar_voice": {"enabled": True, "voice_langs": ["zh", "en", "ja"]},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "backend": "avatar_clone",
                          "reference_audio_path": str(tmp_path / "ref.wav")},
    })
    calls = []

    async def _fake_clone(self, rv, out, t0, **k):
        calls.append(("clone", None))
        p = _loud_wav(Path(str(out)).with_suffix(".wav"))
        rv.ok = True
        rv.provider = "avatar_clone"
        rv.format = "wav"
        rv.audio_path = str(p)
        rv.duration_sec = 1.0
        rv.extra["synth_verify"] = dict(sv)
        return rv

    async def _fake_backend(self, rv, text, out, voice, backend, fmt,
                            timeout, **k):
        calls.append((backend, voice))
        if not edge_ok:
            return "edge down"
        Path(str(out)).write_bytes(b"\x00" * 512)
        rv.ok = True
        rv.audio_path = str(out)
        rv.duration_sec = 1.0
        return None

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", _fake_clone)
    monkeypatch.setattr(TTSPipeline, "_run_backend", _fake_backend)
    return asyncio.run(pipe.synthesize(_JA_TEXT)), calls


def test_autosend_garbled_falls_back_to_edge(tmp_path, monkeypatch):
    rv, calls = _synth_with(monkeypatch, tmp_path, _GARBLED_SV)
    assert rv.ok is True
    assert rv.provider == "edge_tts"
    # 兜底音色按**文本语种**对齐（配置默认 zh 声念日文与乱音同罪）
    assert calls == [("clone", None), ("edge_tts", "ja-JP-NanamiNeural")]
    # 前端归因：语种不支持（保护性改道），不是「通道中断→重试/报障」
    from src.ai.lang_voice_route import fallback_reason_from_extra
    assert fallback_reason_from_extra(rv.extra) == ("lang_unsupported", "ja")
    assert rv.extra["fallback_from"] == "avatar_clone"
    assert rv.extra["primary_error"] == "clone_lang_garbled:ja"


def test_autosend_clean_take_unaffected(tmp_path, monkeypatch):
    """99% 的流量：克隆成品照常出货，一次兜底都不打。"""
    rv, calls = _synth_with(monkeypatch, tmp_path, _CLEAN_SV)
    assert rv.ok is True and rv.provider == "avatar_clone"
    assert calls == [("clone", None)]
    assert "clone_lang_blocked" not in rv.extra


def test_autosend_garbled_and_edge_down_fails_honestly(tmp_path, monkeypatch):
    """兜底也挂了 → **如实失败**（调用方回落文字），绝不把乱音当成品交出去。"""
    rv, _ = _synth_with(monkeypatch, tmp_path, _GARBLED_SV, edge_ok=False)
    assert rv.ok is False
    assert rv.audio_path == ""
    assert "clone_lang_garbled:ja" in (rv.error or "")


@pytest.mark.parametrize("marker,lang", [
    ("clone_lang_unsupported:ja", "ja"),   # 事前拦（闸门按表）
    ("clone_lang_garbled:ja", "ja"),       # 事后判（成品乱码）
])
def test_both_gates_map_to_one_user_facing_reason(marker, lang):
    """两道闸对用户是同一件事：这门语言克隆声念不了、本条已改标准声。"""
    from src.ai.lang_voice_route import fallback_reason_from_extra
    assert fallback_reason_from_extra(
        {"primary_error": marker, "fallback_from": "avatar_clone"}
    ) == ("lang_unsupported", lang)
