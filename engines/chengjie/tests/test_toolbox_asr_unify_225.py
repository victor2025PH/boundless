# -*- coding: utf-8 -*-
"""M-5 D（#225，2026-09-06）：工具箱音频转录链并入工作台 voice_transcriber + 收件核查
+ 错误分类 + asr 探针夹具随包。

5NXHUW 实录：15:03 工具箱 ``audio_pipeline transcribe … ok=False dur=0.0s len=0
latency=16602ms err=APITimeoutError``；15:05 工作台 ``voice_transcriber`` 客户语音 1 秒
转录成功——服务可用，只有工具箱这条链（自己的 openai 客户端 / 模型名 / 超时）超时；
提示把超时一律译成「服务不可用」；true_probe asr 域缺夹具 ``asr_probe.wav``（仓库里有，
桌面包没打）。钉住：

- ``resolve_toolbox_transcribe``：voice_recognition 可用 → ``workspace``（同一 transcriber
  对象）；只有要分段（SRT）且 audio_pipeline 开才走 ``pipeline``；两者皆无 → None；
- ``probe_audio_file``：WAV / OGG 头 + 时长；0 字节 / 头不对 → 「文件未正确接收」；
- ``classify_asr_error``：超时 / 未接收 / 格式 / 被拒 / 不可达 / 无语音 六分类；
- ``VoiceTranscriber.last_error``：失败原因随 None 一起留下，级联拼各级；
- 路由：``received`` 回显 + ``asr_code`` → ``err.asr.<code>`` 人话；
- 夹具：``build_backend.DATAS`` 含 ``assets/probe`` 且与 ``true_probe.ASR_FIXTURE`` 同路径；
- 前端：cp-xlate-tools 回显「已收到 x 秒音频」，cp-i18n 双语键，双树同步。
"""
from __future__ import annotations

import asyncio
import os
import struct
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.ai import audio_pipeline_intake as intake
from src.ai import voice_translate as vt

REPO = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


# ── 收件核查 ────────────────────────────────────────────────────────────────

def _wav(path: str, seconds: float = 1.5, rate: int = 16000) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


def _ogg_opus(path: str, seconds: float = 2.0) -> None:
    """最小可被本模块识别的 OGG Opus：首页含 OpusHead，末页 granule=48k×秒。"""
    def page(flags: int, granule: int, payload: bytes) -> bytes:
        return (b"OggS" + bytes([0, flags]) + struct.pack("<q", granule)
                + b"\x00" * 12 + bytes([1, len(payload)]) + payload)
    head = b"OpusHead" + b"\x01\x02\x38\x01\x80\xbb\x00\x00\x00\x00\x00"
    with open(path, "wb") as f:
        f.write(page(2, 0, head) + page(4, int(48000 * seconds), b"\x00"))


def test_probe_wav_reports_duration(tmp_path):
    p = str(tmp_path / "a.wav")
    _wav(p, 1.5)
    r = intake.probe_audio_file(p, use_ffprobe=False)
    assert r["ok"] and r["container"] == "wav" and r["bytes"] > 44
    assert abs(r["duration_sec"] - 1.5) < 0.01
    assert intake.received_summary(r) == {"bytes": r["bytes"], "duration_sec": 1.5,
                                          "container": "wav"}


def test_probe_ogg_opus_reports_duration(tmp_path):
    p = str(tmp_path / "a.ogg")
    _ogg_opus(p, 2.0)
    r = intake.probe_audio_file(p, use_ffprobe=False)
    assert r["ok"] and r["container"] == "ogg" and r["duration_sec"] == 2.0


def test_probe_failures(tmp_path):
    assert intake.probe_audio_file(str(tmp_path / "nope.ogg"))["reason"] == "missing"
    empty = tmp_path / "e.ogg"
    empty.write_bytes(b"")
    r = intake.probe_audio_file(str(empty))
    assert r["ok"] is False and r["reason"] == "empty" and r["bytes"] == 0
    junk = tmp_path / "j.ogg"
    junk.write_bytes(b"this is not audio at all")
    r2 = intake.probe_audio_file(str(junk), use_ffprobe=False)
    assert r2["ok"] is False and r2["reason"] == "bad_header" and r2["bytes"] > 0
    # 认得容器但算不出时长（mp3 头，无 ffprobe）→ ok + duration None（不算失败）
    mp3 = tmp_path / "m.mp3"
    mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" + b"\x00" * 64)
    r3 = intake.probe_audio_file(str(mp3), use_ffprobe=False)
    assert r3["ok"] and r3["container"] == "mp3" and r3["duration_sec"] is None
    assert intake.received_summary(r3)["duration_sec"] is None


# ── 错误分类 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("err,code", [
    ("APITimeoutError: Request timed out.", "timeout"),
    ("transcribe_timeout(30s)", "timeout"),
    ("OpenAITranscriber: APITimeoutError: Request timed out.", "timeout"),
    ("APIConnectionError: Connection error.", "unavailable"),
    ("cb_open: missing api_key for openai ASR", "unavailable"),
    ("hosted_token_missing: AITR_HOSTED_AI_KEY empty", "unavailable"),
    ("model_load_failed: missing dependency: No module named faster_whisper", "unavailable"),
    ("asr_unconfigured: voice_recognition disabled", "unavailable"),
    ("AuthenticationError: Error code: 401 - invalid api key", "rejected"),
    ("RateLimitError: Error code: 429 - quota exceeded", "rejected"),
    ("NotFoundError: Error code: 404 - model 'x' not found", "rejected"),
    ("BadRequestError: Error code: 400 - Invalid file format", "format"),
    ("file_too_large: 30000000 > 16777216", "format"),
    ("file_not_found", "upload_failed"),
    ("no_speech: hallucination_guard", "no_speech"),
    ("empty_result", "unknown"),
    ("", "unknown"),
])
def test_classify_asr_error(err, code):
    assert intake.classify_asr_error(err) == code
    assert intake.asr_error_i18n_key(code) == f"err.asr.{code}"


def test_i18n_key_unknown_code_falls_back():
    assert intake.asr_error_i18n_key("weird") == "err.asr.unknown"
    from src.web.i18n_packs.errors import EN, ZH
    for c in intake.ASR_CODES:
        k = f"err.asr.{c}"
        assert k in ZH and k in EN, k
    assert "超时" in ZH["err.asr.timeout"] and "未正确接收" in ZH["err.asr.upload_failed"]
    assert "联系运维" not in "".join(v for k, v in ZH.items() if k.startswith("err.asr."))


# ── voice_transcriber.last_error ────────────────────────────────────────────

def _vr_cfg(tmp_path):
    return {"provider": "openai", "api_key": "k", "base_url": "http://127.0.0.1:1/v1",
            "temp_dir": str(tmp_path / "t"), "timeout": 1, "max_retries": 0}


def test_openai_transcriber_records_last_error(tmp_path, monkeypatch):
    from src.voice_transcriber import OpenAITranscriber
    t = OpenAITranscriber(_vr_cfg(tmp_path))

    class _Boom:
        class audio:
            class transcriptions:
                @staticmethod
                def create(**kw):
                    raise TimeoutError("Request timed out.")

    monkeypatch.setattr(t, "_get_client", lambda api_key: _Boom())
    p = str(tmp_path / "v.ogg")
    Path(p).write_bytes(b"OggS" + b"\x00" * 40)
    assert _run(t.transcribe_voice_message(p, "auto")) is None
    assert "TimeoutError" in t.last_error and "timed out" in t.last_error
    assert intake.classify_asr_error(t.last_error) == "timeout"
    # 文件不在 → file_not_found；成功一次后 last_error 清空
    assert _run(t.transcribe_voice_message(str(tmp_path / "gone.ogg"), "auto")) is None
    assert t.last_error == "file_not_found"


def test_fallback_transcriber_aggregates_child_errors(tmp_path):
    from src.voice_transcriber import FallbackTranscriber, VoiceTranscriber

    class _A(VoiceTranscriber):
        async def _transcribe_impl(self, path, language):
            self.last_error = "APITimeoutError: Request timed out."
            return None

    class _B(VoiceTranscriber):
        async def _transcribe_impl(self, path, language):
            raise ConnectionRefusedError("refused")

    cfg = {"temp_dir": str(tmp_path / "t")}
    fb = FallbackTranscriber(cfg, [_A(cfg), _B(cfg)])
    p = str(tmp_path / "v.ogg")
    Path(p).write_bytes(b"OggS" + b"\x00" * 40)
    assert _run(fb.transcribe_voice_message(p, "auto")) is None
    assert "_A: APITimeoutError" in fb.last_error and "_B: ConnectionRefusedError" in fb.last_error
    assert intake.classify_asr_error(fb.last_error) == "timeout"   # 首级超时优先说超时


# ── 入口选择 + 工作台适配 ───────────────────────────────────────────────────

class _FakeWs:
    def __init__(self, text=None, err=""):
        self._text, self.last_error = text, err
        self.calls = []

    async def transcribe_voice_message(self, path, language="zh"):
        self.calls.append((path, language))
        return self._text


def test_resolve_prefers_workspace_then_pipeline(monkeypatch):
    fake = _FakeWs("你好")
    monkeypatch.setattr(vt, "workspace_transcriber", lambda cfg: fake)
    chain, fn = vt.resolve_toolbox_transcribe({"voice_recognition": {"enabled": True}})
    assert chain == "workspace" and fn is not None
    rv = _run(fn("/tmp/x.ogg"))
    assert rv.ok and rv.text == "你好" and rv.model.startswith("workspace:")
    assert fake.calls == [("/tmp/x.ogg", "auto")]      # 上传音频不知客户语言 → auto
    # 工作台 + audio_pipeline 都在：默认仍工作台；要分段（SRT）才走 pipeline
    cfg2 = {"voice_recognition": {"enabled": True},
            "audio_pipeline": {"enabled": True, "backend": "openai", "api_key": "k"}}
    assert vt.resolve_toolbox_transcribe(cfg2)[0] == "workspace"
    assert vt.resolve_toolbox_transcribe(cfg2, want_segments=True)[0] == "pipeline"
    # 工作台没有 → audio_pipeline 兜底；都没有 → None
    monkeypatch.setattr(vt, "workspace_transcriber", lambda cfg: None)
    assert vt.resolve_toolbox_transcribe(cfg2)[0] == "pipeline"
    assert vt.resolve_toolbox_transcribe({"audio_pipeline": {"enabled": False}}) == ("", None)


def test_workspace_fn_carries_last_error_and_unconfigured(monkeypatch):
    fake = _FakeWs(None, "APITimeoutError: Request timed out.")
    monkeypatch.setattr(vt, "workspace_transcriber", lambda cfg: fake)
    rv = _run(vt.build_workspace_transcribe_fn({})("/tmp/x.ogg"))
    assert rv.ok is False and rv.error == "APITimeoutError: Request timed out."
    monkeypatch.setattr(vt, "workspace_transcriber", lambda cfg: None)
    rv2 = _run(vt.build_workspace_transcribe_fn({})("/tmp/x.ogg"))
    assert rv2.ok is False and rv2.error.startswith("asr_unconfigured")


def test_workspace_transcriber_uses_media_enrich_lazy_builder(monkeypatch):
    """同入口＝media_enrich.lazy_voice_transcriber（工作台客户语音就是它建的）。"""
    import src.inbox.media_enrich as me
    sentinel = _FakeWs("x")
    monkeypatch.setattr(me, "lazy_voice_transcriber", lambda cfg: sentinel)
    assert vt.workspace_transcriber({"voice_recognition": {"enabled": True}}) is sentinel


def test_translate_voice_service_attaches_asr_code(tmp_path):
    class _Rv:
        ok = False
        text = ""
        language = ""
        latency_ms = 16602
        model = "workspace:OpenAITranscriber"
        error = "APITimeoutError: Request timed out."
        extra: dict = {}

    async def _tr(path):
        return _Rv()

    class _X:
        async def translate(self, *a, **k):
            raise AssertionError("ASR 失败不该走到翻译")

    p = str(tmp_path / "v.ogg")
    _ogg_opus(p)
    out = _run(vt.VoiceTranslateService(_X(), _tr).translate_voice(p, target_lang="zh"))
    assert out["ok"] is False and out["reason"] == "asr_failed"
    assert out["asr_code"] == "timeout" and out["asr_latency_ms"] == 16602
    assert out["asr_model"] == "workspace:OpenAITranscriber"


# ── 路由层人话 ─────────────────────────────────────────────────────────────

def test_attach_message_uses_asr_code():
    from src.web.routes.unified_inbox_translate_routes import _attach_asr_failure_message
    req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"))
    m = _attach_asr_failure_message(req, {"ok": False, "reason": "asr_failed",
                                          "asr_code": "timeout"})["message"]
    assert "超时" in m and "不可用" not in m
    m2 = _attach_asr_failure_message(req, {"ok": False, "reason": "asr_failed",
                                           "asr_code": "rejected"})["message"]
    assert "拒绝" in m2
    m3 = _attach_asr_failure_message(req, {"ok": False, "reason": "upload_failed",
                                           "asr_code": "upload_failed"})["message"]
    assert "未正确接收" in m3
    # 无 code 的旧形状原样回落（L-6 B 契约不变）
    assert _attach_asr_failure_message(req, {"ok": False, "reason": "asr_failed"})["message"] \
        == "转录服务暂不可用，稍后重试"
    en = SimpleNamespace(state=SimpleNamespace(ui_lang="en"))
    assert "timed out" in _attach_asr_failure_message(
        en, {"ok": False, "reason": "asr_error", "asr_code": "timeout"})["message"]


def test_intake_helper_rejects_empty_file(tmp_path):
    from src.web.routes.unified_inbox_translate_routes import _toolbox_asr_intake
    req = SimpleNamespace(state=SimpleNamespace(ui_lang="zh"))
    empty = tmp_path / "e.ogg"
    empty.write_bytes(b"")
    received, fail = _toolbox_asr_intake(req, str(empty))
    assert fail is not None and fail["reason"] == "upload_failed"
    assert fail["asr_code"] == "upload_failed" and fail["intake_reason"] == "empty"
    assert "未正确接收" in fail["message"] and fail["received"]["bytes"] == 0
    good = str(tmp_path / "g.wav")
    _wav(good, 0.5)
    received2, fail2 = _toolbox_asr_intake(req, good)
    assert fail2 is None and received2["duration_sec"] == 0.5


# ── 夹具随包 ───────────────────────────────────────────────────────────────

def test_asr_probe_fixture_packaged_and_path_matches():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_bb", REPO / "desktop" / "build" / "build_backend.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dests = {str(d).replace("\\", "/") for _s, d in mod.DATAS}
    assert "assets/probe" in dests
    src = [s for s, d in mod.DATAS if str(d).replace("\\", "/") == "assets/probe"][0]
    assert (Path(src) / "asr_probe.wav").is_file() and (Path(src) / "asr_probe.txt").is_file()
    from src.ops.true_probe import ASR_FIXTURE
    # true_probe 用 parents[2]/assets/probe 定位 → 与 DATAS 目标相对路径一致
    assert ASR_FIXTURE.parent.name == "probe" and ASR_FIXTURE.parent.parent.name == "assets"
    assert ASR_FIXTURE.is_file()


# ── 前端 / i18n ─────────────────────────────────────────────────────────────

def test_cp_xlate_tools_received_echo_and_mirror_sync():
    a = (REPO / "shared" / "copilot" / "components" / "cp-xlate-tools.js").read_bytes()
    b = (REPO / "desktop" / "renderer" / "shared" / "copilot" / "components"
         / "cp-xlate-tools.js").read_bytes()
    assert a == b, "cp-xlate-tools.js 双树不同步"
    js = a.decode("utf-8")
    assert "_receivedLine(d.received)" in js and 'this.tf("cp.xlate.received_audio"' in js
    assert 'this.tf("cp.xlate.received_audio_kb"' in js
    i18n = (REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js").read_text(encoding="utf-8")
    for k in ("cp.xlate.received_audio", "cp.xlate.received_audio_kb"):
        assert i18n.count(f'"{k}"') == 2, k
    hant = (REPO / "shared" / "copilot" / "i18n" / "cp-i18n-ext.zh_hant.js").read_text(
        encoding="utf-8")
    assert '"cp.xlate.received_audio"' in hant
