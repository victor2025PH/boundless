"""minicpm_clone TTS 后端单测（异步「真人声情感语音消息」专用）。

覆盖：
  - 成功：health ok + clone 写 WAV → provider=minicpm_clone / format=wav / 带 base_url
  - 情感：非中性 → 结构化 instructions 下发主机（绝不读出）；neutral → 不下发
  - 回落：主机不可达且 cloud_fallback → 端到端回落 edge（绝不卡死出站）
  - 硬失败：不可达且不兜底 → 报错；缺同意 / 缺参考音 → 配置类硬失败、不绕过 owner_consent
  - 登记：CLONE_BACKENDS 含 minicpm_clone；resolve_voice_cfg 注入 minicpm_clone；缓存键并入参考音指纹

minicpm_clone 与 fish_speech 共用 /v1/tts/clone 契约 → 复用 VoiceCloneClient，仅换 base_url。
"""
from __future__ import annotations

import time
from pathlib import Path

from src.ai import voice_clone_client as vcc
from src.ai.tts_pipeline import TTSPipeline, TTSResult


def _mc_pipeline(tmp_path: Path, *, fallback=True, with_ref=True, consent=True):
    """构造 backend=minicpm_clone 的 TTSPipeline（经 voice_profile.backend 选中）。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    return TTSPipeline({
        "enabled": True,
        "backend": "edge_tts",            # 顶层兜底后端；克隆后端经 voice_profile 选中
        "format": "mp3",
        "out_dir": str(tmp_path / "out"),
        "voice_profile": {
            "enabled": True,
            "owner_consent": consent,
            "backend": "minicpm_clone",
            "reference_audio_path": str(ref) if with_ref else "",
        },
        "minicpm_clone": {
            "base_url": "http://mc:7860",
            "cloud_fallback": fallback,
        },
    })


# ── 成功路径 ─────────────────────────────────────────────────────────────────
async def test_minicpm_clone_success(tmp_path, monkeypatch):
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _mc_pipeline(tmp_path)
    rv = await p.synthesize("你好世界，今天过得怎么样")
    assert rv.ok is True
    assert rv.provider == "minicpm_clone"
    assert rv.format == "wav"
    assert rv.audio_path.endswith(".wav")
    assert Path(rv.audio_path).is_file()
    assert rv.extra.get("minicpm_base_url") == "http://mc:7860"


async def test_minicpm_clone_base_url_override_via_voice_profile(tmp_path, monkeypatch):
    """voice_profile.clone_base_url 覆写端点（2026-08-30 粤语克隆分流）：
    粤语路由把合成指到粤语克隆节点（117:7852），全局 minicpm_clone 段不动；
    普通话链（无此键）仍打全局端点——见 test_minicpm_clone_success 的断言。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _mc_pipeline(tmp_path)
    p.voice_profile["clone_base_url"] = "http://127.0.0.1:7852"
    # Q-22：粤语专线路由改派克隆节点时携带 _lang_route_cleared=yue（运营声明会念
    # 粤语）——本测直构管线，按路由口径补上；否则语种闸按「克隆不支持 yue」阻断。
    p.lang_route_cleared = "yue"
    rv = await p.synthesize("我哋今日去饮茶好唔好")
    assert rv.ok is True
    assert rv.provider == "minicpm_clone"
    assert rv.extra.get("minicpm_base_url") == "http://127.0.0.1:7852", \
        "clone_base_url 覆写必须真的换掉请求端点"


async def test_minicpm_clone_text_prefix_reaches_engine(tmp_path, monkeypatch):
    """voice_profile.clone_text_prefix（CosyVoice 方言标签 <|yue|>）必须拼进
    **送引擎的合成文本**最前面；原文/缓存键/镜像不受影响（extra 带观测锚）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["text"] = text
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _mc_pipeline(tmp_path)
    p.voice_profile["clone_text_prefix"] = "<|yue|>"
    rv = await p.synthesize("我哋今日去饮茶好唔好啦")
    assert rv.ok is True
    assert seen["text"].startswith("<|yue|>"), \
        f"方言标签必须在送引擎文本最前面，实得 {seen['text'][:20]!r}"
    assert rv.extra.get("clone_text_prefix") == "<|yue|>"
    assert not str(rv.text or "").startswith("<|yue|>"), "原文不得被前缀污染"


async def test_minicpm_clone_passes_emotion_instructions(tmp_path, monkeypatch):
    """情感非中性 → 结构化 instructions 下发主机（系统侧语气，绝不读出 → 零 garble）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["text"] = text
        seen["instructions"] = instructions
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _mc_pipeline(tmp_path)
    p.cache_enabled = False
    rv = await p.synthesize("你好世界", emotion="happy")
    assert rv.ok is True
    assert seen["instructions"] and "语气" in seen["instructions"]
    assert seen["text"] == "你好世界"          # minicpm 不做内联标记 → 文本不动


async def test_minicpm_clone_neutral_no_instructions(tmp_path, monkeypatch):
    """neutral → 不下发 instructions（与升级前一致）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_clone(self, text, ref, out, *, reference_text="", instructions=""):
        seen["instructions"] = instructions
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEcloned")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", fake_clone)
    p = _mc_pipeline(tmp_path)
    p.cache_enabled = False
    rv = await p.synthesize("你好世界", emotion="neutral")
    assert rv.ok is True
    assert seen["instructions"] == ""


# ── 回落 / 硬失败 ────────────────────────────────────────────────────────────
async def test_minicpm_clone_unreachable_degrades_to_text_not_edge(tmp_path, monkeypatch):
    """主机不可达且 cloud_fallback → **不换声**改发文字（L-2 #205 / D-L4，2026-09-06）。

    旧契约「端到端回落 edge 出声」让克隆人设在引擎离线时对客户发出通用声
    （steven 男人设变女声实锤）；现在克隆态引擎失败 ok=False + degrade_to_text，
    edge 零调用，调用方改发文字并提示引擎离线。"""
    vcc.reset_health_cache()
    vcc.reset_load_state()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    # 彻底不可达（reachable=False）→ 不触发按需载入，纯回落
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "probe_health_detail",
        lambda self: {"reachable": False, "model_loaded": None, "loading": False})
    edge = {"n": 0}

    async def fake_edge(self, text, out, voice, spec=None):
        edge["n"] += 1
        out.write_bytes(b"ID3edge-audio" + b"\x00" * 512)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)
    p = _mc_pipeline(tmp_path, fallback=True)
    rv = await p.synthesize("你好")
    assert rv.ok is False
    assert rv.extra.get("fallback_blocked") == "clone_engine_offline"
    assert rv.extra.get("degrade_to_text") is True
    assert rv.extra.get("primary_error") == "minicpm_clone_unreachable"
    assert edge["n"] == 0


async def test_minicpm_clone_unreachable_hard_fail_without_fallback(tmp_path, monkeypatch):
    """不可达且关闭兜底 → 直接报 minicpm_clone_unreachable（_try_minicpm_clone 直测）。"""
    vcc.reset_health_cache()
    vcc.reset_load_state()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "probe_health_detail",
        lambda self: {"reachable": False, "model_loaded": None, "loading": False})
    p = _mc_pipeline(tmp_path, fallback=False)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_minicpm_clone(rv, out, time.monotonic())
    assert res is rv
    assert res.error == "minicpm_clone_unreachable"


async def test_minicpm_clone_synth_failure_falls_back(tmp_path, monkeypatch):
    """合成抛错（server 500 等）且 cloud_fallback → 回落（_try_minicpm_clone 返 None）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "synthesize_clone",
        lambda self, text, ref, out, **kw: (_ for _ in ()).throw(RuntimeError("500")))
    p = _mc_pipeline(tmp_path, fallback=True)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_minicpm_clone(rv, out, time.monotonic())
    assert res is None


# ── 惰性主机自愈：可达但模型未载入 → 后台触发载入 ────────────────────────────
async def test_minicpm_clone_not_loaded_triggers_background_load(tmp_path, monkeypatch):
    """可达但模型未载入（惰性 supervisor worker 未起）→ 触发一次后台载入自愈 + 本条回落。"""
    vcc.reset_health_cache()
    vcc.reset_load_state()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "probe_health_detail",
        lambda self: {"reachable": True, "model_loaded": False, "loading": False})
    triggered = {"n": 0}
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "request_model_load_async",
        lambda self: (triggered.__setitem__("n", triggered["n"] + 1) or True))
    p = _mc_pipeline(tmp_path, fallback=False)   # 不兜底 → 直测 _try 返回
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_minicpm_clone(rv, out, time.monotonic())
    assert triggered["n"] == 1                    # 已触发后台载入自愈
    assert res is rv and res.error == "minicpm_clone_unreachable"


async def test_minicpm_clone_loading_in_progress_no_retrigger(tmp_path, monkeypatch):
    """正在载入中（loading=True）→ 不重复触发（等它载完即可）。"""
    vcc.reset_health_cache()
    vcc.reset_load_state()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "probe_health_detail",
        lambda self: {"reachable": True, "model_loaded": False, "loading": True})
    triggered = {"n": 0}
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "request_model_load_async",
        lambda self: (triggered.__setitem__("n", triggered["n"] + 1) or True))
    p = _mc_pipeline(tmp_path, fallback=False)
    out = tmp_path / "out.mp3"
    res = await p._try_minicpm_clone(TTSResult(text="hi", format="mp3"), out, time.monotonic())
    assert triggered["n"] == 0                    # 载入中 → 不重复触发
    assert res.error == "minicpm_clone_unreachable"


async def test_minicpm_clone_not_loaded_no_trigger_when_auto_load_off(tmp_path, monkeypatch):
    """auto_load=false（opt-out）→ 退回旧行为，仅回落不触发载入。"""
    vcc.reset_health_cache()
    vcc.reset_load_state()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: False)
    triggered = {"n": 0}
    monkeypatch.setattr(
        vcc.VoiceCloneClient, "request_model_load_async",
        lambda self: (triggered.__setitem__("n", triggered["n"] + 1) or True))
    p = _mc_pipeline(tmp_path, fallback=False)
    p.minicpm_clone["auto_load"] = False
    out = tmp_path / "out.mp3"
    res = await p._try_minicpm_clone(TTSResult(text="hi", format="mp3"), out, time.monotonic())
    assert triggered["n"] == 0
    assert res.error == "minicpm_clone_unreachable"


async def test_minicpm_clone_missing_consent_hard_fails_no_fallback(tmp_path, monkeypatch):
    """缺 owner_consent → 配置类硬失败，不得用通用音色（edge）悄悄绕过同意门。"""
    vcc.reset_health_cache()
    edge = {"n": 0}

    async def fake_edge(self, text, out, voice, spec=None):
        edge["n"] += 1
        out.write_bytes(b"ID3edge" + b"\x00" * 512)

    monkeypatch.setattr(TTSPipeline, "_edge_tts", fake_edge)
    p = _mc_pipeline(tmp_path, consent=False)
    rv = await p.synthesize("你好")
    assert rv.ok is False
    assert "voice_profile_requires_owner_consent" in rv.error
    assert edge["n"] == 0


async def test_minicpm_clone_missing_reference_hard_fails(tmp_path, monkeypatch):
    """缺参考音 → 无法克隆音色，配置类硬失败（暴露而非掩盖）。"""
    vcc.reset_health_cache()
    p = _mc_pipeline(tmp_path, with_ref=False)
    out = tmp_path / "out.mp3"
    rv = TTSResult(text="hi", format="mp3")
    res = await p._try_minicpm_clone(rv, out, time.monotonic())
    assert res is rv
    assert "voice_profile_missing_reference_audio_path" in res.error


# ── 登记：CLONE_BACKENDS / 注入 / 缓存键 ─────────────────────────────────────
def test_minicpm_clone_registered_as_clone_backend():
    from src.ai.voice_routing import CLONE_BACKENDS
    assert "minicpm_clone" in CLONE_BACKENDS


def test_resolve_voice_cfg_injects_minicpm_clone():
    from src.ai.persona_voice import resolve_voice_cfg
    full = {
        "telegram": {"voice_reply": {"backend": "edge_tts"}},
        "minicpm_clone": {"base_url": "http://mc:7860", "cloud_fallback": True},
    }
    cfg = resolve_voice_cfg(None, full)
    assert cfg.get("minicpm_clone", {}).get("base_url") == "http://mc:7860"
    assert cfg["minicpm_clone"]["cloud_fallback"] is True


def test_minicpm_clone_cache_key_includes_ref_fingerprint(tmp_path):
    """minicpm_clone 属克隆后端 → 缓存键并入参考音指纹（换参考音自动失效）。"""
    from src.ai.voice_emotion import NEUTRAL
    p = _mc_pipeline(tmp_path)
    ref = Path(p.voice_profile["reference_audio_path"])
    k1 = p._cache_key("hi", "v", "minicpm_clone", NEUTRAL)
    # 改参考音内容（size 变）→ 指纹变 → 缓存键失效
    ref.write_bytes(b"RIFF" + b"\x00" * 64 + b"WAVEdifferent")
    k2 = p._cache_key("hi", "v", "minicpm_clone", NEUTRAL)
    assert k1 != k2


def test_minicpm_cache_key_unshipped_dialect_does_not_split(tmp_path):
    """未出货方言与普通话同一声学路径，不得为假口音分缓存。"""
    from src.ai.voice_emotion import NEUTRAL
    p = _mc_pipeline(tmp_path)
    k0 = p._cache_key("hi", "v", "minicpm_clone", NEUTRAL)
    p.voice_profile["dialect_flavor"] = "chuanyu"
    k1 = p._cache_key("hi", "v", "minicpm_clone", NEUTRAL)
    assert k0 == k1
    p.voice_profile["dialect_flavor"] = "cantonese"
    k2 = p._cache_key("hi", "v", "minicpm_clone", NEUTRAL)
    assert k0 != k2


async def test_clone_instruct_hits_instruct_endpoint(tmp_path, monkeypatch):
    """voice_profile.clone_instruct → /v1/tts/instruct，不走 clone。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)
    seen = {}

    def fake_instruct(self, text, ref, out, *, instruct=""):
        seen["text"] = text
        seen["instruct"] = instruct
        seen["url"] = self.base_url + self.instruct_path
        Path(out).write_bytes(b"RIFF\x00\x00\x00\x00WAVEinstruct")

    def boom(*a, **k):
        raise AssertionError("clone_instruct 路径不得再打 synthesize_clone")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_instruct", fake_instruct)
    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_clone", boom)
    p = _mc_pipeline(tmp_path)
    p.voice_profile["clone_instruct"] = "请用四川话表达。"
    p.voice_profile["clone_base_url"] = "http://127.0.0.1:7852"
    rv = await p.synthesize("今天天气不错我们出去走走吧")
    assert rv.ok is True
    assert seen["instruct"] == "请用四川话表达。"
    assert seen["url"] == "http://127.0.0.1:7852/v1/tts/instruct"
    assert rv.extra.get("clone_instruct") == "请用四川话表达。"
    assert rv.extra.get("minicpm_base_url") == "http://127.0.0.1:7852"


async def test_dialect_flavor_chuanyu_does_not_switch_to_7852(
        tmp_path, monkeypatch):
    """未出货川渝不得改走 117:7852 instruct（无标准生成链）。"""
    vcc.reset_health_cache()
    monkeypatch.setattr(vcc.VoiceCloneClient, "health_ok", lambda self, **kw: True)

    def boom(*a, **k):
        raise AssertionError("未出货方言不得打 synthesize_instruct")

    monkeypatch.setattr(vcc.VoiceCloneClient, "synthesize_instruct", boom)
    p = _mc_pipeline(tmp_path)
    p.voice_profile["backend"] = "avatar_clone"
    p.voice_profile["dialect_flavor"] = "chuanyu"
    p.backend = "avatar_clone"
    assert p._effective_backend() == "avatar_clone"
