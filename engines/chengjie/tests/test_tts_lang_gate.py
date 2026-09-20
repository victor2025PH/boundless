"""合成层克隆语种能力闸 + 繁→简发音输入（P0 2026-08-31「日文怪声」全链收口）。

UI 的 voice_langs 警示只护坐席手动面板；A 线自动语音回复 / B 线 autosend /
主动触达三条自动链靠 TTSPipeline 单点闸兜底。本文件钉住闸门语义：

- 超能力语种（日文 × hub IndexTTS-2 仅中英）绝不进克隆后端（省一次注定
  怪声的 GPU 往返）；
- fallback 开但未二次确认 → Q-22 阻断（clone_unavailable，零系统音）；
  confirm_system_voice=True 才 edge 按语种对齐音色（绝不能用中文声念外文）；
- fallback 关（no_edge 部署）→ 如实失败 ``clone_lang_unsupported:<lang>``，
  调用方回落文字；
- 中文/英文/短文本/能力未知 → 行为与闸前完全一致（宁可漏拦不误拦）；
- 繁体中文 → 简体发音输入（繁简同音；仅克隆后端、日文/粤语豁免、可 opt-out）。
"""
from pathlib import Path

import pytest

from src.ai.tts_pipeline import TTSPipeline

_JA = "どこにいるの。ご飯は食べた？一緒に遊びに行こうよ。"
_ZH = "今天过得怎么样呀？晚上一起吃饭吧。"


def _cfg(tmp_path, **over):
    """backend=avatar_clone + hub 钉 IndexTTS-2（事故根因形态）的最小管线配置。"""
    cfg = {
        "enabled": True,
        "backend": "avatar_clone",
        "fallback_on_error": False,
        "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "persona_id": "p1",
        "avatar_voice": {
            "enabled": True,
            "hub_fish": {
                "enabled": True,
                "tts_engine": "index_tts",
                "persona_allowlist": ["p1"],
            },
        },
    }
    cfg.update(over)
    return cfg


def _deny_clone(monkeypatch, called):
    """克隆后端一被调用即记账（闸门生效=永不触发）。"""

    async def fake_avatar(self, rv, out, t0, **kw):
        called["avatar_clone"] = True
        return None

    async def fake_minicpm(self, rv, out, t0, **kw):
        called["minicpm_clone"] = True
        return None

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)
    monkeypatch.setattr(TTSPipeline, "_try_minicpm_clone", fake_minicpm)


async def test_gate_blocks_ja_and_fails_honestly_without_fallback(
        tmp_path, monkeypatch):
    """日文 × IndexTTS-2 × 无兜底 → 不打克隆、如实失败（调用方回落文字）。"""
    called: dict = {}
    _deny_clone(monkeypatch, called)
    tts = TTSPipeline(_cfg(tmp_path))
    rv = await tts.synthesize(_JA)
    assert not rv.ok
    assert str(rv.error or "").startswith("clone_lang_unsupported:ja")
    assert rv.extra.get("clone_lang_blocked") == "ja"
    assert not called, f"克隆后端不应被调用: {called}"


async def test_gate_falls_back_to_lang_aligned_edge_voice(tmp_path, monkeypatch):
    """Q-22：兜底开但未确认 → 阻断不出系统音；confirm 后 edge 按语种对齐（ja→ja 声）。"""
    called: dict = {}
    _deny_clone(monkeypatch, called)
    seen: dict = {}

    async def fake_run_backend(self, rv, text, out, voice, backend, fmt,
                               timeout_sec, *, spec=None):
        seen["voice"] = voice
        seen["backend"] = backend
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"ID3" + b"\x00" * 600)
        rv.ok = True
        rv.audio_path = str(out)
        rv.extra["bytes"] = 603
        return None

    monkeypatch.setattr(TTSPipeline, "_run_backend", fake_run_backend)
    tts = TTSPipeline(_cfg(tmp_path, fallback_on_error=True))
    rv = await tts.synthesize(_JA)
    assert not rv.ok
    assert "clone_unavailable" in (rv.error or "")
    assert "clone_lang_unsupported:ja" in (rv.error or "")
    assert rv.extra.get("degrade_to_text") is True
    assert not seen, f"未二次确认不得调用 edge：{seen}"
    assert not called, "克隆后端不应被调用"

    rv2 = await tts.synthesize(_JA, confirm_system_voice=True)
    assert rv2.ok
    assert rv2.provider == "edge_tts"
    assert seen["backend"] == "edge_tts"
    assert seen["voice"] == "ja-JP-NanamiNeural"     # EDGE_VOICE_BY_LANG[ja]
    assert rv2.voice == "ja-JP-NanamiNeural"
    assert rv2.extra.get("fallback_from") == "avatar_clone"
    assert rv2.extra.get("system_voice_confirmed") is True


async def test_gate_passes_chinese_to_clone_unchanged(tmp_path, monkeypatch):
    """中文照常进克隆链（行为与闸前完全一致），extra 不带拦截标记。"""

    async def fake_avatar(self, rv, out, t0, **kw):
        rv.ok = True
        rv.provider = "avatar_clone"
        rv.audio_path = str(out)
        return rv

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)
    tts = TTSPipeline(_cfg(tmp_path))
    rv = await tts.synthesize(_ZH)
    assert rv.ok and rv.provider == "avatar_clone"
    assert "clone_lang_blocked" not in rv.extra


async def test_gate_open_when_capability_unknown(tmp_path, monkeypatch):
    """hub 引擎未钉=能力未知 → 放行（宁可漏拦不误拦=旧行为）。"""
    hit: dict = {}

    async def fake_avatar(self, rv, out, t0, **kw):
        hit["avatar"] = True
        rv.ok = True
        rv.provider = "avatar_clone"
        rv.audio_path = str(out)
        return rv

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)
    cfg = _cfg(tmp_path)
    cfg["avatar_voice"]["hub_fish"].pop("tts_engine")
    # avatar_clone 后端缺省表含 ja（7852 CosyVoice3）——hub 引擎未钉时回落
    # 后端表，ja 在表内 → 放行进克隆链
    tts = TTSPipeline(cfg)
    rv = await tts.synthesize(_JA)
    assert rv.ok and hit.get("avatar")


async def test_t2s_traditional_reaches_clone_as_simplified(tmp_path, monkeypatch):
    """繁体输入 → 克隆后端收到简体（繁简同音）；extra 打 tts_t2s 标记。"""
    pytest.importorskip("opencc")
    seen: dict = {}

    async def fake_avatar(self, rv, out, t0, **kw):
        seen["text"] = rv.text
        rv.ok = True
        rv.provider = "avatar_clone"
        rv.audio_path = str(out)
        return rv

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)
    tts = TTSPipeline(_cfg(tmp_path))
    rv = await tts.synthesize("時間還早，我們一起去聽音樂吧")
    assert rv.ok
    assert "时间" in seen["text"] and "听音乐" in seen["text"]
    assert "們" not in seen["text"]
    assert rv.extra.get("tts_t2s") is True


async def test_t2s_opt_out_keeps_traditional(tmp_path, monkeypatch):
    """avatar_voice.clone_t2s=false → 原样直送（运营 opt-out 逃生门）。"""
    seen: dict = {}

    async def fake_avatar(self, rv, out, t0, **kw):
        seen["text"] = rv.text
        rv.ok = True
        rv.provider = "avatar_clone"
        rv.audio_path = str(out)
        return rv

    monkeypatch.setattr(TTSPipeline, "_try_avatar_clone", fake_avatar)
    cfg = _cfg(tmp_path)
    cfg["avatar_voice"]["clone_t2s"] = False
    tts = TTSPipeline(cfg)
    rv = await tts.synthesize("時間還早，我們一起去聽音樂吧")
    assert rv.ok
    assert "時間" in seen["text"]
    assert not rv.extra.get("tts_t2s")


async def test_gate_skips_cache_for_blocked_lang(tmp_path, monkeypatch):
    """被拦语种不查缓存——修复前入缓存的怪声音频不得借命中复活。"""
    import src.ai.tts_pipeline as tp

    def boom(*a, **k):
        raise AssertionError("被拦语种不应查询 TTS 缓存")

    monkeypatch.setattr(tp, "_tts_cache_get", boom)
    called: dict = {}
    _deny_clone(monkeypatch, called)
    cfg = _cfg(tmp_path)
    cfg["tts_cache"] = {"enabled": True}
    tts = TTSPipeline(cfg)
    rv = await tts.synthesize(_JA)
    assert not rv.ok
    assert rv.extra.get("clone_lang_blocked") == "ja"


# ══ P1 语种→引擎路由（hub_fish.lang_engines）：闸门放行 + 合成真改派 ══════════

def _tone_wav(ms: int = 300, rate: int = 24000) -> bytes:
    """有能量的 WAV（440Hz 正弦）——全零夹具会撞 B61 哑音闸。"""
    import io
    import math
    import wave

    buf = io.BytesIO()
    n = int(rate * ms / 1000)
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
        frames += int(v).to_bytes(2, "little", signed=True)
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def _hub_cfg(tmp_path, ref, **hub_over):
    """backend=avatar_clone + hub 钉 index_tts + {ja→fish_speech} 语种改派。"""
    hf = {
        "enabled": True,
        "base_url": "http://hub.invalid:9000",
        "timeout_sec": 5.0,
        "best_of": 1,
        "tts_engine": "index_tts",
        "verify_engine": False,          # 单测不打目录/指纹网络
        "persona_allowlist": ["p1"],
        "lang_engines": {"ja": "fish_speech"},
    }
    hf.update(hub_over)
    return {
        "enabled": True,
        "backend": "avatar_clone",
        "format": "wav",
        "out_dir": str(tmp_path / "out"),
        "fallback_on_error": False,
        "tts_cache": {"enabled": False},
        "persona_id": "p1",
        "voice_profile": {
            "enabled": True,
            "owner_consent": True,
            "backend": "avatar_clone",
            "reference_audio_path": str(ref),
        },
        "avatar_voice": {
            "enabled": True,
            "cloud_fallback": False,
            "chunk_max_chars": 0,
            "retries": 0,
            "hub_fish": hf,
        },
    }


async def test_lang_engines_routes_ja_to_mapped_engine(tmp_path):
    """日文 + {ja→fish_speech} → 闸门放行、hub 请求带改派引擎与 ja 语言。"""
    from unittest.mock import patch

    from src.ai.voice_synth_stats import get_voice_synth_stats

    stats = get_voice_synth_stats()
    stats.reset()
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_tone_wav(200))
    seen: dict = {}

    def fake_hub(base_url, profile, text, **kw):
        seen["tts_engine"] = kw.get("tts_engine")
        seen["language"] = kw.get("language")
        return _tone_wav(400), "wav"

    tts = TTSPipeline(_hub_cfg(tmp_path, ref))
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub):
        rv = await tts.synthesize(_JA)
    assert rv.ok and rv.provider == "hub_fish"
    assert seen["tts_engine"] == "fish_speech"
    assert seen["language"] == "ja"
    assert rv.extra.get("hub_engine_lang_routed") == "ja:fish_speech"
    assert "clone_lang_blocked" not in rv.extra
    # 观测接线：改派命中进 voice_synth_stats（metrics/Prom 零新接线暴露）
    assert stats.dump()["routed_by_pair"].get("ja:fish_speech") == 1
    stats.reset()


async def test_gate_block_feeds_voice_synth_stats(tmp_path, monkeypatch):
    """拦截分布进 voice_synth_stats——「客户在要哪些我们念不了的语种」的读数。"""
    from src.ai.voice_synth_stats import get_voice_synth_stats

    stats = get_voice_synth_stats()
    stats.reset()
    called: dict = {}
    _deny_clone(monkeypatch, called)
    tts = TTSPipeline(_cfg(tmp_path))
    rv = await tts.synthesize(_JA)
    assert not rv.ok
    d = stats.dump()
    assert d["blocked_by_lang"].get("ja") == 1 and d["lang_blocked"] == 1
    stats.reset()


# ══ 语种路由放行握手（voice_lang_route.clone_langs → _lang_route_cleared）═════

async def test_lang_route_cleared_handshake_allows_routed_lang(
        tmp_path, monkeypatch):
    """clone_langs 路由改写后（backend=minicpm + cleared=ja）：闸门放行、直达
    minicpm 分支；同配置无 cleared 标记 → 仍拦（回归钉）。"""
    hit: dict = {}

    async def fake_minicpm(self, rv, out, t0, **kw):
        hit["minicpm"] = True
        rv.ok = True
        rv.provider = "minicpm_clone"
        rv.audio_path = str(out)
        return rv

    monkeypatch.setattr(TTSPipeline, "_try_minicpm_clone", fake_minicpm)
    cfg = _cfg(tmp_path, backend="minicpm_clone")
    cfg["_lang_route_cleared"] = "ja"
    rv = await TTSPipeline(cfg).synthesize(_JA)
    assert rv.ok and hit.get("minicpm")
    assert "clone_lang_blocked" not in rv.extra

    hit.clear()
    cfg2 = _cfg(tmp_path, backend="minicpm_clone")   # 无 cleared
    rv2 = await TTSPipeline(cfg2).synthesize(_JA)
    assert not rv2.ok and not hit
    assert rv2.extra.get("clone_lang_blocked") == "ja"


async def test_lang_engines_zh_keeps_pinned_engine(tmp_path):
    """中文不受映射影响：仍用钉住的 index_tts（缺省行为零变化）。"""
    from unittest.mock import patch

    ref = tmp_path / "ref.wav"
    ref.write_bytes(_tone_wav(200))
    seen: dict = {}

    def fake_hub(base_url, profile, text, **kw):
        seen["tts_engine"] = kw.get("tts_engine")
        return _tone_wav(400), "wav"

    tts = TTSPipeline(_hub_cfg(tmp_path, ref))
    with patch("src.ai.avatar_voice.hub_fish_synthesize", side_effect=fake_hub):
        rv = await tts.synthesize(_ZH)
    assert rv.ok and rv.provider == "hub_fish"
    assert seen["tts_engine"] == "index_tts"
    assert "hub_engine_lang_routed" not in rv.extra
