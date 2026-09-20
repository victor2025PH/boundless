"""V：语音克隆合成「语言纠正」观测单测。

覆盖：计数（total/corrected/by-lang）、dump 形状 + 纠正率、Prometheus 文本、reset、
best-effort（脏输入不抛），以及 synthesize_clone 端到端真实打点（英文文本→纠正计一次）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from src.ai import voice_clone_client as vcc
from src.ai.voice_synth_stats import VoiceSynthLangStats, get_voice_synth_stats


# ── 计数 / dump / prom ────────────────────────────────────────────
def test_record_counts_total_and_corrected():
    s = VoiceSynthLangStats()
    s.record(default_lang="zh", used_lang="zh")   # 未纠正
    s.record(default_lang="zh", used_lang="en")   # 纠正 → en
    s.record(default_lang="zh", used_lang="en")   # 纠正 → en
    s.record(default_lang="zh", used_lang="ja")   # 纠正 → ja
    d = s.dump()
    assert d["total_synth"] == 4
    assert d["corrected"] == 3
    assert d["corrected_rate"] == round(3 / 4, 4)
    assert d["by_lang"] == {"en": 2, "ja": 1}


def test_record_case_insensitive_no_false_correction():
    # 大小写不算纠正（EN==en）
    s = VoiceSynthLangStats()
    s.record(default_lang="ZH", used_lang="zh")
    d = s.dump()
    assert d["total_synth"] == 1 and d["corrected"] == 0


def test_record_bad_input_never_raises():
    s = VoiceSynthLangStats()
    s.record(default_lang=None, used_lang=None)   # type: ignore[arg-type]
    s.record(default_lang="zh", used_lang="")     # 空 used → 不计纠正
    d = s.dump()
    assert d["total_synth"] == 2 and d["corrected"] == 0


def test_dump_empty_rate_zero():
    assert VoiceSynthLangStats().dump()["corrected_rate"] == 0


def test_dump_prom_shape():
    s = VoiceSynthLangStats()
    s.record(default_lang="zh", used_lang="en")
    prom = s.dump_prom()
    assert "voice_synth_total 1" in prom
    assert "voice_synth_language_corrected_total 1" in prom
    assert 'voice_synth_language_corrected_by_lang_total{to_lang="en"} 1' in prom
    # 带标签与不带标签用不同 metric 名（避免 Prometheus 同名混用）
    assert "voice_synth_language_corrected_total{" not in prom


def test_reset_clears():
    s = VoiceSynthLangStats()
    s.record(default_lang="zh", used_lang="en")
    s.reset()
    d = s.dump()
    assert d["total_synth"] == 0 and d["corrected"] == 0 and d["by_lang"] == {}


def test_singleton_stable():
    assert get_voice_synth_stats() is get_voice_synth_stats()


# ── P0/P1 观测扩展（2026-08-31）：语种闸拦截 + 语种→引擎改派 ─────────────────
def test_record_blocked_and_routed_counts():
    s = VoiceSynthLangStats()
    s.record_blocked("ja")
    s.record_blocked("JA-jp")     # 前缀归一
    s.record_blocked("th")
    s.record_lang_routed("ja", "fish_speech")
    d = s.dump()
    assert d["lang_blocked"] == 3
    assert d["blocked_by_lang"] == {"ja": 2, "th": 1}
    assert d["lang_routed"] == 1
    assert d["routed_by_pair"] == {"ja:fish_speech": 1}
    prom = s.dump_prom()
    assert 'voice_synth_lang_blocked_total{lang="ja"} 2' in prom
    assert 'voice_synth_lang_routed_total{pair="ja:fish_speech"} 1' in prom
    s.record_blocked("")          # 脏输入不计不抛
    s.record_lang_routed("", "")
    assert s.dump()["lang_blocked"] == 3
    s.reset()
    d2 = s.dump()
    assert d2["lang_blocked"] == 0 and d2["blocked_by_lang"] == {}
    assert d2["lang_routed"] == 0 and d2["routed_by_pair"] == {}


# ── synthesize_clone 端到端真实打点 ───────────────────────────────
class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self) -> bytes:
        return self._body


def _ok_body() -> bytes:
    return json.dumps(
        {"ok": True, "audio_base64": base64.b64encode(b"WAV").decode()}).encode()


def test_synthesize_clone_records_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(
        vcc.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(_ok_body()))
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFFxxxx")
    stats = get_voice_synth_stats()
    stats.reset()
    client = vcc.VoiceCloneClient({"language": "zh"})  # 默认 auto_language=True
    # 英文文本 → 合成语言纠正为 en，应打一次点
    client.synthesize_clone("Hello, how are you today?", str(ref), tmp_path / "o1.wav")
    # 中文文本 → 不纠正
    client.synthesize_clone("你好呀最近怎么样", str(ref), tmp_path / "o2.wav")
    d = stats.dump()
    assert d["total_synth"] == 2
    assert d["corrected"] == 1
    assert d["by_lang"] == {"en": 1}
    stats.reset()
