"""生成层口语分叉门禁 — src/ai/spoken_variant.py（Phase G）。

守住的不变量：
  - **标记绝不泄漏**：请求过口语版后，书面版任何情况下不含 [口语版] 字样
    （畸形输出/复读标记/口语段为空都剥干净）——泄漏=用户看到内部指令，事故级；
  - **哈希失配即回落**：书面文本被守卫/兜底改过 → take 返回 None（危机兜底
    文案绝不会被错配口语版顶替）；
  - 门控克制：voice_reply 关/trigger 不命中/generated 关/非中文 → 不请求；
  - 口语段验证：语言串/长度失衡 → 丢弃（书面版仍干净返回）；
  - 管线契约：sender 命中口语版时 synthesize 收到 pre_colloquialized=True
    → TTS 前口语化改写跳过（防二次改写）、来源计入 stats。
"""
from __future__ import annotations

import pytest

from src.ai.spoken_variant import (
    SPOKEN_MARKER,
    build_spoken_variant_instruction,
    reset_store,
    should_request_spoken_variant,
    should_request_spoken_variant_autosend,
    split_spoken_variant,
    stash_spoken_variant,
    take_spoken_variant,
)

_W = "我今天工作确实有点忙，不过晚上会准时和你聊天的，放心吧。"
_S = "今天是有点忙啦，不过晚上肯定准时来找你聊天，放心哈。"


# ── split_spoken_variant ─────────────────────────────────────────────────────
def test_split_normal_marker():
    raw = f"{_W}\n{SPOKEN_MARKER} {_S}"
    written, spoken = split_spoken_variant(raw)
    assert written == _W
    assert spoken == _S


def test_split_marker_variants():
    for mk in ("[口语版]：", "【口语版】:", "【口语版】", "- [口语版]"):
        written, spoken = split_spoken_variant(f"{_W}\n{mk} {_S}")
        assert written == _W, mk
        assert spoken == _S, mk


def test_split_no_marker_passthrough():
    written, spoken = split_spoken_variant(_W)
    assert written == _W and spoken is None


def test_split_never_leaks_marker():
    """核心红线：不管 LLM 输出多畸形，书面版绝不含标记。"""
    cases = [
        f"{_W}\n{SPOKEN_MARKER} {_S}\n{SPOKEN_MARKER} 又来一遍的口语版内容啊",
        f"{_W}\n{SPOKEN_MARKER}",                       # 口语段为空
        f"{SPOKEN_MARKER} {_S}",                        # 没有正文
        f"{_W}\n{SPOKEN_MARKER} short",                 # 口语段不合格（串语言）
    ]
    for raw in cases:
        written, _spoken = split_spoken_variant(raw)
        assert "口语版" not in written, raw


def test_split_rejects_language_mismatch_and_bad_ratio():
    # 中文书面 + 英文口语段 → 拒
    w1, s1 = split_spoken_variant(
        f"{_W}\n{SPOKEN_MARKER} i am busy today but will chat with you tonight")
    assert w1 == _W and s1 is None
    # 口语段过长（>2.2x+16）→ 拒
    w2, s2 = split_spoken_variant(f"{_W}\n{SPOKEN_MARKER} {'今天忙' * 60}")
    assert w2 == _W and s2 is None
    # 口语段过短 → 拒
    w3, s3 = split_spoken_variant(f"{_W}\n{SPOKEN_MARKER} 嗯")
    assert w3 == _W and s3 is None


def test_split_empty_input():
    assert split_spoken_variant("") == ("", None)
    assert split_spoken_variant(None) == ("", None)  # type: ignore[arg-type]


# ── store ────────────────────────────────────────────────────────────────────
def test_store_roundtrip_and_pop():
    reset_store()
    stash_spoken_variant(_W, _S)
    assert take_spoken_variant(_W) == _S
    assert take_spoken_variant(_W) is None      # 取即消费
    reset_store()


def test_store_mismatch_after_postprocess():
    """书面文本被守卫改写 → 哈希失配 → None（危机兜底绝不被口语版顶替）。"""
    reset_store()
    stash_spoken_variant(_W, _S)
    assert take_spoken_variant(_W + "（已被守卫改写）") is None
    reset_store()


def test_store_ttl_expiry(monkeypatch):
    import src.ai.spoken_variant as sv
    reset_store()
    t = [1000.0]
    monkeypatch.setattr(sv.time, "monotonic", lambda: t[0])
    stash_spoken_variant(_W, _S)
    t[0] += sv.STORE_TTL_SEC + 1
    assert take_spoken_variant(_W) is None
    reset_store()


def test_store_capacity_eviction():
    import src.ai.spoken_variant as sv
    reset_store()
    for i in range(sv.STORE_CAP + 8):
        stash_spoken_variant(f"{_W}{i}", _S)
    assert take_spoken_variant(f"{_W}0") is None       # 最老的被逐出
    assert take_spoken_variant(f"{_W}{sv.STORE_CAP + 7}") == _S
    reset_store()


# ── 门控 ─────────────────────────────────────────────────────────────────────
def _cfg(trigger="always", vr_enabled=True, col_enabled=True, generated=True):
    return {
        "telegram": {"voice_reply": {"enabled": vr_enabled, "trigger": trigger}},
        "avatar_voice": {"colloquial": {"enabled": col_enabled,
                                        "generated": generated}},
    }


def test_gate_happy_path_and_disables():
    zh = "今天过得怎么样呀"
    assert should_request_spoken_variant(_cfg(), is_peer_voice=False, text=zh)
    assert not should_request_spoken_variant(
        _cfg(vr_enabled=False), is_peer_voice=False, text=zh)
    assert not should_request_spoken_variant(
        _cfg(col_enabled=False), is_peer_voice=False, text=zh)
    assert not should_request_spoken_variant(
        _cfg(generated=False), is_peer_voice=False, text=zh)
    assert not should_request_spoken_variant(
        _cfg(trigger="never"), is_peer_voice=True, text=zh)


def test_gate_trigger_when_peer_voice():
    zh = "今天过得怎么样呀"
    assert should_request_spoken_variant(
        _cfg(trigger="when_peer_voice"), is_peer_voice=True, text=zh)
    assert not should_request_spoken_variant(
        _cfg(trigger="when_peer_voice"), is_peer_voice=False, text=zh)


def test_gate_non_chinese_skipped():
    assert not should_request_spoken_variant(
        _cfg(), is_peer_voice=False, text="how are you doing today")
    assert not should_request_spoken_variant(
        _cfg(), is_peer_voice=False, text="")


def test_instruction_contains_marker():
    ins = build_spoken_variant_instruction()
    assert SPOKEN_MARKER in ins and "口语" in ins


# ── B 线门控（inbox.l2_autosend.voice 口径）──────────────────────────────────
def _cfg_b(trigger="always", vb_enabled=True, generated=True):
    return {
        "inbox": {"l2_autosend": {"voice": {"enabled": vb_enabled,
                                            "trigger": trigger}}},
        "avatar_voice": {"colloquial": {"enabled": True,
                                        "generated": generated}},
    }


def test_gate_autosend_paths():
    zh = "今天过得怎么样呀想你了"
    assert should_request_spoken_variant_autosend(
        _cfg_b(), peer_sent_voice=False, text=zh)
    assert should_request_spoken_variant_autosend(
        _cfg_b(trigger="smart"), peer_sent_voice=False, text=zh)
    assert not should_request_spoken_variant_autosend(
        _cfg_b(vb_enabled=False), peer_sent_voice=False, text=zh)
    assert not should_request_spoken_variant_autosend(
        _cfg_b(generated=False), peer_sent_voice=False, text=zh)
    assert not should_request_spoken_variant_autosend(
        _cfg_b(trigger="never"), peer_sent_voice=True, text=zh)
    # when_peer_voice：对方是语音才要
    assert should_request_spoken_variant_autosend(
        _cfg_b(trigger="when_peer_voice"), peer_sent_voice=True, text=zh)
    assert not should_request_spoken_variant_autosend(
        _cfg_b(trigger="when_peer_voice"), peer_sent_voice=False, text=zh)
    assert not should_request_spoken_variant_autosend(
        _cfg_b(), peer_sent_voice=False, text="english only text here")


@pytest.mark.asyncio
async def test_voice_autosend_synth_uses_spoken_variant(monkeypatch, tmp_path):
    """B 线端到端：暂存命中 → _synth_ogg 用口语版合成且 pre_colloquialized=True。"""
    import src.inbox.voice_autosend as va
    reset_store()
    audio = tmp_path / "a.ogg"
    audio.write_bytes(b"OGGfake")

    class _FakeResult:
        ok = True
        provider = "avatar_clone"
        voice = "t"
        latency_ms = 10
        duration_sec = 5.0
        error = ""
        extra: dict = {}

        def __init__(self, path):
            self.audio_path = path

    seen = {}

    class _FakeTTS:
        def __init__(self, cfg):
            pass

        async def synthesize(self, text, timeout_sec=45.0, emotion=None, **kw):
            seen["text"] = text
            seen.update(kw)
            return _FakeResult(str(audio))

    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "fake"}, "emotion": None})
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _FakeTTS)
    monkeypatch.setattr("src.client.voice_sender.convert_to_ogg_opus",
                        lambda p, delete_src=True: p)

    stash_spoken_variant(_W, _S)
    path, meta = await va._synth_ogg(
        {}, "p1", _W, out_dir=str(tmp_path), platform="telegram")
    assert path == str(audio)
    assert seen["text"] == _S                      # 合成的是口语版
    assert seen.get("pre_colloquialized") is True  # 跳过二次改写
    assert meta.get("spoken_variant") is True
    # 未暂存 → 原文合成，标志 False
    seen.clear()
    path2, meta2 = await va._synth_ogg(
        {}, "p1", _W, out_dir=str(tmp_path), platform="telegram")
    assert seen["text"] == _W
    assert seen.get("pre_colloquialized") is False
    assert "spoken_variant" not in meta2
    reset_store()


# ── ai_client 接线：生成后剥离 + 暂存 ────────────────────────────────────────
class _Cfg:
    config_path = None
    config = {"web_admin": {"site_name": "T"}, "ai": {}}

    def get_ai_config(self):
        return {}


@pytest.mark.asyncio
async def test_generate_reply_with_intent_strips_and_stashes(monkeypatch):
    from src.ai.ai_client import AIClient
    reset_store()
    c = AIClient(_Cfg())

    async def fake_generate_reply(user_message, context=None, **kw):
        return f"{_W}\n{SPOKEN_MARKER} {_S}"

    monkeypatch.setattr(c, "generate_reply", fake_generate_reply)
    out = await c.generate_reply_with_intent(
        "在忙吗", "greeting", {"_spoken_variant_request": True})
    assert out == _W                      # 书面版：标记剥净
    assert take_spoken_variant(_W) == _S  # 口语版已暂存
    reset_store()


@pytest.mark.asyncio
async def test_generate_reply_without_flag_untouched(monkeypatch):
    from src.ai.ai_client import AIClient
    reset_store()
    c = AIClient(_Cfg())
    raw = f"{_W}\n{SPOKEN_MARKER} {_S}"

    async def fake_generate_reply(user_message, context=None, **kw):
        return raw

    monkeypatch.setattr(c, "generate_reply", fake_generate_reply)
    out = await c.generate_reply_with_intent("在忙吗", "greeting", {})
    assert out == raw                     # 未请求 → 不解析（零行为变化）
    assert take_spoken_variant(_W) is None
    reset_store()


# ── tts_pipeline 契约：pre_colloquialized 透传 ───────────────────────────────
@pytest.mark.asyncio
async def test_synthesize_passes_pre_colloquialized(monkeypatch):
    from src.ai.tts_pipeline import TTSPipeline, TTSResult
    tts = TTSPipeline({"enabled": True, "backend": "edge_tts"})
    seen = {}

    async def fake_uncached(text, **kw):
        seen.update(kw)
        return TTSResult(ok=True, text=text, audio_path="")

    monkeypatch.setattr(tts, "_synthesize_uncached", fake_uncached)
    await tts.synthesize("这是一句足够长的测试文本内容啊", pre_colloquialized=True)
    assert seen.get("pre_colloquialized") is True
    seen.clear()
    await tts.synthesize("这是一句足够长的测试文本内容啊")
    assert seen.get("pre_colloquialized") is False


def test_stats_colloquial_generated_counter():
    from src.ai.avatar_voice_stats import get_avatar_voice_stats
    st = get_avatar_voice_stats()
    st.reset()
    try:
        st.record_synth(ok=True, channel="emotion", emotion="neutral",
                        colloquial_generated=True)
        st.record_synth(ok=True, channel="emotion", emotion="neutral")
        d = st.dump()
        assert d["colloquial_gen"] == 1
        assert d["colloquial_gen_rate"] == 0.5
        assert "avatar_voice_colloquial_gen_total 1" in st.dump_prom()
    finally:
        st.reset()
