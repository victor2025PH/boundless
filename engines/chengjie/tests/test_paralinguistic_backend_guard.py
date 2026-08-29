"""#58/#59（2026-08-30 凌晨批）：副语言标记消费侧守卫 + 交互式克隆动态预算。

#58 事故：voice_emotion.inject_paralinguistic 注入的 [breath]（逗号后气口）随
avatar 路径送进 104 IndexTTS-2（桌面托管网关中继实锤），该引擎不消费标记、按
英文念出＝用户听到固定「PLAS」。修复面：
  - 家族正则单源（voice_emotion.strip_paralinguistic_marks，补上 <strong> 尖括号对）
  - 上游引擎形状判定（avatar_voice.health_shape_of，7852/7865 健康键分形）
  - 注入闸（marks_safe：全部候选端点实证 CosyVoice 才注入）
  - 双兜底剥除（AvatarVoiceClient.tts / VoiceCloneClient.synthesize_clone）
#59 事故：预览/发送固定 45s 预算在 76-80 字被击穿（104 RTF 1.7-1.8 经隧道
~0.4s/字，服务端 42 发全 200 成功、引擎预算先到期回落默认音）。修复面：
  - tts_preview.clone_budget_sec 按字数动态（0.45s/字+10s，下限 45 上限 190），
    tts-test 与 send-voice 同函数（试听=发送同口径）。

全部离线可跑，零真实网络/GPU。
"""
from __future__ import annotations

import base64
import io
import json
import urllib.error
import wave
from unittest.mock import patch

import pytest

from src.ai import avatar_voice as av
from src.ai.avatar_voice import AvatarVoiceClient, health_shape_of
from src.ai.voice_emotion import strip_paralinguistic_marks
from src.integrations.shared.tts_preview import clone_budget_sec


def _wav_bytes(ms: int = 120, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clean_shape_cache():
    """形状缓存是模块级：每例前后清空，防跨测试污染 marks_safe 判定。"""
    av._SHAPE_CACHE.clear()
    yield
    av._SHAPE_CACHE.clear()


# ── 家族剥除（单一事实源）────────────────────────────────────────────────────
def test_strip_marks_full_family():
    t = "唉[sigh]，今天好累，[breath]晚点再聊[laughter]，真的<strong>超好吃</strong>！"
    out = strip_paralinguistic_marks(t)
    for mark in ("[sigh]", "[breath]", "[laughter]", "<strong>", "</strong>"):
        assert mark not in out
    # <strong> 只剥标签，被强调的词本体必须保留
    assert "超好吃" in out
    assert out == "唉，今天好累，晚点再聊，真的超好吃！"


def test_strip_marks_case_insensitive_and_variants():
    assert strip_paralinguistic_marks("[Breath]你好[LAUGHS]") == "你好"
    assert strip_paralinguistic_marks("[laugh]嘿[strong]") == "嘿"


def test_strip_marks_idempotent_and_passthrough():
    clean = "普通的一句话，没有任何标记。"
    assert strip_paralinguistic_marks(clean) == clean
    once = strip_paralinguistic_marks("[sigh]唉，走吧")
    assert strip_paralinguistic_marks(once) == once
    assert strip_paralinguistic_marks("") == ""
    assert strip_paralinguistic_marks(None) == ""  # type: ignore[arg-type]


def test_strip_marks_collapses_leftover_spaces():
    # 标记两侧原有空格：剥除后不留双空格
    assert strip_paralinguistic_marks("hello [breath] world") == "hello world"


def test_polish_hub_speak_text_strips_strong_pair():
    """polish（hub/IndexTTS 送稿清洗）此前只剥方括号族——<strong> 是漏网口。"""
    from src.ai.tts_pipeline import polish_hub_speak_text
    out = polish_hub_speak_text("真的<strong>超好吃</strong>，[breath]你快来尝尝")
    assert "<strong>" not in out and "</strong>" not in out
    assert "[breath]" not in out
    assert "超好吃" in out


# ── 上游引擎形状判定 ─────────────────────────────────────────────────────────
def test_health_shape_of_families():
    assert health_shape_of({"ok": True, "models_loaded": True}) == "cosyvoice"
    assert health_shape_of({"ok": False}) == "cosyvoice"
    assert health_shape_of({"status": "ok", "model_loaded": True}) == "indextts2"
    assert health_shape_of({"status": "loading"}) == "indextts2"
    assert health_shape_of({"weird": 1}) == ""
    assert health_shape_of(None) == ""
    assert health_shape_of("ok") == ""


def test_marks_safe_semantics():
    c = AvatarVoiceClient({
        "enabled": True,
        "base_urls": ["http://a:7852", "http://b:7865"],
    })
    # 形状未知 → 不安全（宁可少一口气声，不当着客户念英文）
    assert c.marks_safe() is False
    av._SHAPE_CACHE["http://a:7852"] = "cosyvoice"
    # 混合池：任一端点非 Cosy/未知 → 不安全
    assert c.marks_safe() is False
    av._SHAPE_CACHE["http://b:7865"] = "indextts2"
    assert c.marks_safe() is False
    # 全部实证 Cosy → 安全
    av._SHAPE_CACHE["http://b:7865"] = "cosyvoice"
    assert c.marks_safe() is True


def test_probe_records_shape_via_health_ok_base():
    """健康探测顺带落形状：7865 家族响应 → _SHAPE_CACHE 记 indextts2。"""
    c = AvatarVoiceClient({"enabled": True, "base_url": "http://x:7865"})
    with patch.object(
        AvatarVoiceClient, "_probe",
        return_value={"reachable": True, "models_loaded": True,
                      "shape": "indextts2"},
    ):
        assert c._health_ok_base("http://x:7865", use_cache=False) is True
    assert av._SHAPE_CACHE.get("http://x:7865") == "indextts2"
    assert c.marks_safe() is False


# ── 消费侧兜底剥除 ───────────────────────────────────────────────────────────
def _fake_post_any(captured: dict):
    wav = _wav_bytes()

    def _fp(self, path, payload, *, timeout):
        captured["path"] = path
        captured["body"] = json.loads(payload.decode("utf-8"))
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    return _fp


def test_avatar_tts_strips_marks_for_non_cosy_upstream():
    marked = "唉[sigh]，今天好累，[breath]晚点聊，<strong>真的</strong>想你。"
    c = AvatarVoiceClient({"enabled": True, "base_url": "http://x:7865",
                           "chunk_max_chars": 0, "retries": 0})
    av._SHAPE_CACHE["http://x:7865"] = "indextts2"
    captured: dict = {}
    with patch.object(AvatarVoiceClient, "_post_any", _fake_post_any(captured)):
        c.tts(marked, reference_audio_b64="")
    sent = captured["body"]["text"]
    for mark in ("[sigh]", "[breath]", "<strong>", "</strong>"):
        assert mark not in sent
    assert "真的" in sent


def test_avatar_tts_keeps_marks_for_confirmed_cosy_upstream():
    marked = "唉[sigh]，今天好累。"
    c = AvatarVoiceClient({"enabled": True, "base_url": "http://y:7852",
                           "chunk_max_chars": 0, "retries": 0})
    av._SHAPE_CACHE["http://y:7852"] = "cosyvoice"
    captured: dict = {}
    with patch.object(AvatarVoiceClient, "_post_any", _fake_post_any(captured)):
        c.tts(marked, reference_audio_b64="")
    assert "[sigh]" in captured["body"]["text"]


def test_voice_clone_client_strips_marks_unconditionally(tmp_path):
    """/v1/tts/clone 契约家族（MiniCPM/fish/IndexTTS-2）无一消费标记 → 恒剥。"""
    from src.ai.voice_clone_client import VoiceCloneClient
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes())
    out = tmp_path / "out.wav"
    captured: dict = {}

    def _fake_request(self, text, ref_b64, lang, reference_text, instructions):
        captured["text"] = text
        return _wav_bytes()

    c = VoiceCloneClient({"enabled": True, "base_url": "http://x:7865",
                          "chunk_max_chars": 0})
    with patch.object(VoiceCloneClient, "_request_clone", _fake_request):
        c.synthesize_clone(
            "好呀[breath]，我这就来，<strong>马上</strong>到！", str(ref), out)
    assert "[breath]" not in captured["text"]
    assert "<strong>" not in captured["text"]
    assert "马上" in captured["text"]
    assert out.read_bytes()[:4] == b"RIFF"


# ── register_spk 契约残留（网关 404×2 实锤）─────────────────────────────────
def test_register_spk_404_is_expected_not_error():
    c = AvatarVoiceClient({"enabled": True, "base_url": "http://x:7865"})
    ref_b64 = base64.b64encode(b"fake-ref-bytes").decode("ascii")
    calls = {"n": 0}

    def _raise_404(self, url, payload, *, timeout, headers=None,
                   serialize_gpu=True):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    av._REGISTERED_SPK.clear()
    try:
        with patch.object(AvatarVoiceClient, "_post_with_retry", _raise_404):
            assert c.register_spk(ref_b64) is False
            # 404=上游无该端点（IndexTTS-2 零样本无需预热）：记指纹止住重试
            assert calls["n"] == 1
            assert c.register_spk(ref_b64) is True   # 幂等短路，不再打 404
            assert calls["n"] == 1
    finally:
        av._REGISTERED_SPK.clear()


# ── #59 交互式克隆动态预算（试听=发送同口径）────────────────────────────────
def test_clone_budget_sec_boundaries():
    assert clone_budget_sec("啥", fast=True) == 15.0
    # 短文本维持旧 45s 下限（预算只放宽不收紧）
    assert clone_budget_sec("") == 45.0
    assert clone_budget_sec("字" * 77) == 45.0
    # 76-80 字击穿区：预算随字数越过 45（0.45*80+10=46）
    assert clone_budget_sec("字" * 80) == pytest.approx(46.0)
    # 400 字顶格输入（cp-voice/tts-test 同一上限）→ 封顶 190
    assert clone_budget_sec("字" * 400) == 190.0
    assert clone_budget_sec("字" * 999) == 190.0
    # 单调不减
    prev = 0.0
    for n in (0, 40, 80, 120, 200, 400, 500):
        cur = clone_budget_sec("字" * n)
        assert cur >= prev
        prev = cur


def test_routes_share_budget_function():
    """tts-test 与 send-voice 必须同用 clone_budget_sec（试听=发送契约）：
    同一段文字不能「试听得出来、发送发不出」。源级断言防静默分叉。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
    for fname in ("voice_routes.py", "unified_inbox_send_routes.py"):
        src = (root / fname).read_text(encoding="utf-8")
        assert "clone_budget_sec" in src, f"{fname} 未接动态预算函数"
