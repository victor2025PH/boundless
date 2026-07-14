"""AvatarHub Phase2 单测：预渲染命中层 / 动态 instruct / 观测 / enroll profile。

全部离线可跑，零真实网络。
"""
from __future__ import annotations

import base64
import io
import json
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from src.ai.avatar_voice import AvatarVoiceClient, reset_caches
from src.ai.avatar_voice_stats import AvatarVoiceStats, get_avatar_voice_stats
from src.ai.voice_prerender import (
    copy_for_send,
    find_prerendered,
    normalize_prerender_text,
    prerender_key,
    write_prerendered,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_caches()
    get_avatar_voice_stats().reset()
    yield
    reset_caches()
    get_avatar_voice_stats().reset()


def _wav_bytes(ms: int = 200, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * ms / 1000))
    return buf.getvalue()


# ── 预渲染纯函数 ─────────────────────────────────────────────────────────────
def test_normalize_prerender_text_stable():
    assert normalize_prerender_text(" 早安呀 \n") == "早安呀"
    assert normalize_prerender_text("早安呀🌞") == "早安呀"      # emoji 剔除
    # 换行折空格（2026-07-14：折「，」会诱发克隆引擎在假逗号处早停念半截）
    assert normalize_prerender_text("早安\n呀") == "早安 呀"
    assert normalize_prerender_text("") == ""


def test_prerender_key_deterministic():
    k1 = prerender_key("早安呀")
    assert k1 == prerender_key(" 早安呀 ")   # 归一化后同键
    assert len(k1) == 8
    assert prerender_key("") == ""
    assert prerender_key("晚安") != k1


def test_write_and_find_prerendered_roundtrip(tmp_path):
    ogg_src = tmp_path / "raw.ogg"
    ogg_src.write_bytes(b"OggS-fake")
    final = write_prerendered("p1", "早安呀 ", ogg_src, base_dir=str(tmp_path / "voices"))
    assert final.is_file()
    assert final.name == f"{prerender_key('早安呀')}.ogg"

    hit = find_prerendered("p1", "早安呀", base_dir=str(tmp_path / "voices"))
    assert hit == final
    # 归一化差异也命中
    assert find_prerendered("p1", " 早安呀\n", base_dir=str(tmp_path / "voices")) == final
    # 未命中：不同文本 / 不同人设
    assert find_prerendered("p1", "晚安", base_dir=str(tmp_path / "voices")) is None
    assert find_prerendered("p2", "早安呀", base_dir=str(tmp_path / "voices")) is None


def test_find_prerendered_rejects_mismatched_sidecar(tmp_path):
    """sidecar 原文与查询文本不一致（哈希碰撞/放错文件）→ 拒绝命中。"""
    base = tmp_path / "voices"
    d = base / "p1" / "prerendered"
    d.mkdir(parents=True)
    key = prerender_key("早安呀")
    (d / f"{key}.ogg").write_bytes(b"OggS-fake")
    (d / f"{key}.txt").write_text("完全不同的台词", encoding="utf-8")
    assert find_prerendered("p1", "早安呀", base_dir=str(base)) is None


def test_copy_for_send_preserves_original(tmp_path):
    src = tmp_path / "a.ogg"
    src.write_bytes(b"OggS-original")
    dst = copy_for_send(src, tmp_path / "out")
    assert dst.read_bytes() == b"OggS-original"
    dst.unlink()                      # 模拟发送后 unlink
    assert src.is_file()              # 原件保住


# ── TTSPipeline 预渲染接线 ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_pipeline_prerender_hit_skips_synth(tmp_path):
    base = tmp_path / "voices"
    ogg_src = tmp_path / "raw.ogg"
    ogg_src.write_bytes(b"OggS-prerendered-bytes")
    write_prerendered("lin_xiaoyu", "早安呀", ogg_src, base_dir=str(base))

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({
        "enabled": True,
        "backend": "avatar_clone",
        "persona_id": "lin_xiaoyu",
        "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "reference_audio_path": "unused.wav"},
        "avatar_voice": {"enabled": True,
                         "prerender": {"enabled": True, "base_dir": str(base)}},
    })
    with patch.object(AvatarVoiceClient, "health_ok", side_effect=AssertionError(
            "prerender hit must not touch backend")):
        rv = await tts.synthesize("早安呀")
    assert rv.ok
    assert rv.provider == "prerendered"
    assert rv.format == "ogg"
    assert Path(rv.audio_path).read_bytes() == b"OggS-prerendered-bytes"
    # 发送副本可安全删除，备货原件不动
    Path(rv.audio_path).unlink()
    assert find_prerendered("lin_xiaoyu", "早安呀", base_dir=str(base)) is not None
    assert get_avatar_voice_stats().dump()["prerender_hits"] == 1


@pytest.mark.asyncio
async def test_pipeline_prerender_miss_falls_through(tmp_path):
    """未命中/未配 persona_id/开关关 → 走正常合成路径（此处 backend disabled 显错）。"""
    from src.ai.tts_pipeline import TTSPipeline

    common = {
        "enabled": True, "backend": "disabled", "fallback_on_error": False,
        "out_dir": str(tmp_path / "out"), "tts_cache": {"enabled": False},
    }
    # 未命中（无备货）
    tts = TTSPipeline({**common, "persona_id": "p1",
                       "avatar_voice": {"enabled": True,
                                        "prerender": {"base_dir": str(tmp_path / "v")}}})
    rv = await tts.synthesize("早安呀")
    assert not rv.ok and "backend disabled" in rv.error
    # 无 persona_id
    tts2 = TTSPipeline({**common, "avatar_voice": {"enabled": True}})
    rv2 = await tts2.synthesize("早安呀")
    assert not rv2.ok
    # 预渲染开关关
    base = tmp_path / "voices"
    src = tmp_path / "raw.ogg"
    src.write_bytes(b"OggS")
    write_prerendered("p1", "早安呀", src, base_dir=str(base))
    tts3 = TTSPipeline({**common, "persona_id": "p1",
                        "avatar_voice": {"enabled": True,
                                         "prerender": {"enabled": False,
                                                       "base_dir": str(base)}}})
    rv3 = await tts3.synthesize("早安呀")
    assert not rv3.ok  # 没命中直接走 backend=disabled → 错误暴露


# ── 动态 instruct 模板库 ─────────────────────────────────────────────────────
def test_to_cosyvoice_instruct_deterministic_and_varied():
    from src.ai.voice_emotion import EmotionSpec, to_cosyvoice_instruct
    s = EmotionSpec("playful", intensity=0.6)
    i1 = to_cosyvoice_instruct(s, seed_text="今天吃什么呀")
    i2 = to_cosyvoice_instruct(s, seed_text="今天吃什么呀")
    assert i1 == i2 and i1                      # 同文本确定性（缓存友好）
    seeds = ["a", "bb", "ccc", "dddd", "eeeee", "ffffff"]
    variants = {to_cosyvoice_instruct(s, seed_text=x) for x in seeds}
    assert len(variants) >= 2                   # 不同文本有变化


def test_to_cosyvoice_instruct_intensity_pace_and_priority():
    from src.ai.voice_emotion import EmotionSpec, NEUTRAL, to_cosyvoice_instruct
    hi = to_cosyvoice_instruct(EmotionSpec("warm", intensity=0.9), seed_text="x")
    lo = to_cosyvoice_instruct(EmotionSpec("warm", intensity=0.3), seed_text="x")
    slow = to_cosyvoice_instruct(EmotionSpec("warm", pace="slow"), seed_text="x")
    assert "情绪饱满" in hi and "情绪收着" in lo and "语速放慢" in slow
    # neutral → 空（回落 emotion 标签通道）
    assert to_cosyvoice_instruct(NEUTRAL, seed_text="x") == ""
    assert to_cosyvoice_instruct(None, seed_text="x") == ""
    # 运营显式 base 永远最高优先
    assert to_cosyvoice_instruct(
        EmotionSpec("warm"), seed_text="x", base="用耳语说") == "用耳语说"


@pytest.mark.asyncio
async def test_pipeline_dynamic_instruct_channel(tmp_path):
    """dynamic_instruct 开 + 情绪非中性 → 走 /v1/tts/instruct；关 → emotion 标签。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    # 逐字稿 sidecar：Phase12 守卫要求情感标签必须配逐字稿（混合保真路径）
    (tmp_path / "ref.txt").write_text("参考稿", encoding="utf-8")
    wav = _wav_bytes(300)
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["url"] = url
        sent["body"] = json.loads(payload.decode())
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    def cfg(dyn: bool) -> dict:
        return {
            "enabled": True, "backend": "avatar_clone", "format": "wav",
            "out_dir": str(tmp_path / "out"), "fallback_on_error": False,
            "tts_cache": {"enabled": False},
            "voice_profile": {"enabled": True, "owner_consent": True,
                              "backend": "avatar_clone",
                              "reference_audio_path": str(ref)},
            "avatar_voice": {"enabled": True, "cloud_fallback": False,
                             "dynamic_instruct": dyn,
                             "chunk_max_chars": 0, "retries": 0},
        }

    from src.ai.tts_pipeline import TTSPipeline
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await TTSPipeline(cfg(True)).synthesize("宝贝晚安哦", emotion="warm")
    assert rv.ok
    assert sent["url"].endswith("/v1/tts/instruct")
    assert "语气" in sent["body"]["instruct"]
    assert rv.extra["avatar_channel"] == "instruct"

    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv2 = await TTSPipeline(cfg(False)).synthesize("宝贝晚安哦", emotion="warm")
    assert rv2.ok
    assert sent["url"].endswith("/v1/tts/clone")
    # 2026-07-13 音色保真：warm(0.6) 弱情绪 → neutral 保真路径（音色最像）
    assert sent["body"]["emotion"] == "neutral"
    assert rv2.extra["avatar_channel"] == "emotion"

    # 强情绪（intensity≥0.7）→ 情感标签切 instruct2 情感路径
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv3 = await TTSPipeline(cfg(False)).synthesize(
            "宝贝晚安哦", emotion={"emotion": "warm", "intensity": 0.85})
    assert rv3.ok
    assert sent["body"]["emotion"] == "gentle"


# ── 观测单例 ─────────────────────────────────────────────────────────────────
def test_stats_counters_and_prom():
    st = AvatarVoiceStats()
    st.record_synth(ok=True, latency_ms=2000, channel="emotion", emotion="gentle")
    st.record_synth(ok=True, latency_ms=4000, channel="instruct")
    st.record_synth(ok=False)
    st.record_prerender_hit()
    st.queue_enter()
    st.queue_enter()
    st.queue_exit()
    st.record_stt(ok=True)
    st.record_stt(ok=False)
    d = st.dump()
    assert d["synth_total"] == 3 and d["synth_ok"] == 2 and d["synth_fail"] == 1
    assert d["avg_latency_ms"] == 3000
    assert d["by_channel"] == {"emotion": 1, "instruct": 1}
    assert d["by_emotion"] == {"gentle": 1}
    assert d["prerender_hits"] == 1
    assert d["queue_depth"] == 1 and d["queue_peak"] == 2
    assert d["stt_total"] == 2 and d["stt_ok"] == 1
    prom = st.dump_prom()
    assert "avatar_voice_synth_total 3" in prom
    assert 'avatar_voice_by_channel_total{channel="instruct"} 1' in prom


def test_queue_stats_wired_into_client():
    """_post_with_retry 应经 stats 记队列水位（enter/exit 平衡）。"""
    c = AvatarVoiceClient({"enabled": True, "retries": 0, "chunk_max_chars": 0})
    with patch.object(
        AvatarVoiceClient, "_post",
        return_value=json.dumps(
            {"audio_base64": base64.b64encode(b"OK").decode()}).encode()):
        c.tts("hi", reference_audio_b64="QQ==")
    d = get_avatar_voice_stats().dump()
    assert d["queue_peak"] >= 1
    assert d["queue_depth"] == 0   # exit 平衡


# ── Phase3：备货缺口观测 ─────────────────────────────────────────────────────
def test_stats_prerender_miss_and_coverage():
    st = AvatarVoiceStats()
    st.record_prerender_hit()
    st.record_prerender_miss("想你啦")
    st.record_prerender_miss("想你啦")
    st.record_prerender_miss("吃了吗")
    d = st.dump()
    assert d["prerender_miss"] == 3
    assert d["prerender_coverage"] == 0.25   # 1/(1+3)
    assert d["top_misses"][0] == {"text": "想你啦", "n": 2, "personas": {}}
    prom = st.dump_prom()
    assert "avatar_voice_prerender_miss_total 3" in prom


def test_stats_miss_texts_capped():
    st = AvatarVoiceStats()
    for i in range(st._MISS_TEXT_CAP + 20):
        st.record_prerender_miss(f"台词{i}")
    d = st.dump()
    assert d["prerender_miss"] == st._MISS_TEXT_CAP + 20
    # distinct 收集不超上限（防刷量撑爆）
    assert len(st._miss_texts) == st._MISS_TEXT_CAP
    # 空样本 → coverage None
    st2 = AvatarVoiceStats()
    assert st2.dump()["prerender_coverage"] is None


@pytest.mark.asyncio
async def test_pipeline_records_short_miss_not_long(tmp_path):
    """短句未命中记缺口；长句（动态对话）不记。"""
    from src.ai.tts_pipeline import TTSPipeline

    cfg = {
        "enabled": True, "backend": "disabled", "fallback_on_error": False,
        "persona_id": "p1", "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "avatar_voice": {"enabled": True,
                         "prerender": {"base_dir": str(tmp_path / "v")}},
    }
    tts = TTSPipeline(cfg)
    await tts.synthesize("想你啦")                                # 短句 → 记缺口
    await tts.synthesize("这是一条很长很长的动态对话内容，不属于固定台词的范畴，不该进备货缺口统计里")
    d = get_avatar_voice_stats().dump()
    assert d["prerender_miss"] == 1
    assert d["top_misses"][0]["text"] == "想你啦"


# ── Phase3：人设声线底色（instruct_style）────────────────────────────────────
def test_to_cosyvoice_instruct_style_composition():
    from src.ai.voice_emotion import EmotionSpec, to_cosyvoice_instruct
    s = EmotionSpec("warm", intensity=0.6)
    plain = to_cosyvoice_instruct(s, seed_text="x")
    styled = to_cosyvoice_instruct(s, seed_text="x", style="撒娇")
    assert plain.startswith("用") and plain.endswith("的语气说")
    assert styled.startswith("用撒娇黏人、")
    assert styled != plain
    # 未知风格忽略（等同不配）；base 仍最高优先
    assert to_cosyvoice_instruct(s, seed_text="x", style="不存在") == plain
    assert to_cosyvoice_instruct(s, seed_text="x", style="撒娇", base="用耳语说") == "用耳语说"


def test_to_cosyvoice_instruct_style_dedupe_with_core():
    """底色与内核语义重叠（俏皮底色 × playful 内核）→ 跳过底色防冗余。"""
    from src.ai.voice_emotion import EmotionSpec, to_cosyvoice_instruct
    s = EmotionSpec("playful", intensity=0.6)
    for seed in ("a", "b", "c", "dd", "ee"):   # 覆盖全部变体
        out = to_cosyvoice_instruct(s, seed_text=seed, style="俏皮")
        plain = to_cosyvoice_instruct(s, seed_text=seed)
        assert out == plain                     # 重叠 → 不加底色
    # 不重叠组合正常复合（沉稳 × playful）
    styled = to_cosyvoice_instruct(s, seed_text="a", style="沉稳")
    assert styled.startswith("用沉稳可靠、")


@pytest.mark.asyncio
async def test_pipeline_instruct_style_from_voice_profile(tmp_path):
    """voice_profile.instruct_style 应进入动态 instruct 指令。"""
    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(300))
    wav = _wav_bytes(300)
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["url"] = url
        sent["body"] = json.loads(payload.decode())
        return json.dumps(
            {"audio_base64": base64.b64encode(wav).decode()}).encode()

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({
        "enabled": True, "backend": "avatar_clone", "format": "wav",
        "out_dir": str(tmp_path / "out"), "fallback_on_error": False,
        "tts_cache": {"enabled": False},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "backend": "avatar_clone", "instruct_style": "沉稳",
                          "reference_audio_path": str(ref)},
        "avatar_voice": {"enabled": True, "cloud_fallback": False,
                         "dynamic_instruct": True,
                         "chunk_max_chars": 0, "retries": 0},
    })
    with patch.object(AvatarVoiceClient, "health_ok", return_value=True), \
         patch.object(AvatarVoiceClient, "_post", fake_post):
        rv = await tts.synthesize("今天讲个新故事", emotion="warm")
    assert rv.ok
    assert sent["url"].endswith("/v1/tts/instruct")
    assert "沉稳可靠" in sent["body"]["instruct"]


# ── Phase3：台词库读取 ───────────────────────────────────────────────────────
def test_read_prerender_lines_merge_dedupe(tmp_path):
    from src.ai.voice_prerender import read_prerender_lines
    d = tmp_path / "lines"
    d.mkdir()
    (d / "_common.txt").write_text(
        "# 注释\n早安呀\n晚安\n\n想你啦\n", encoding="utf-8")
    (d / "p1.txt").write_text(
        "想你啦\n我下课啦！\n", encoding="utf-8")   # 想你啦 与 common 重复 → 去重
    lines = read_prerender_lines("p1", lines_dir=str(d))
    assert lines == ["早安呀", "晚安", "想你啦", "我下课啦！"]
    # 无专属文件 → 只有 common；目录不存在 → 空
    assert read_prerender_lines("p2", lines_dir=str(d)) == ["早安呀", "晚安", "想你啦"]
    assert read_prerender_lines("p1", lines_dir=str(tmp_path / "nope")) == []


# ── Phase3：健康灯 ───────────────────────────────────────────────────────────
def test_avatar_probe_target_decision():
    from src.inbox.health_watchdog import avatar_probe_target
    assert avatar_probe_target({}) == ""
    assert avatar_probe_target({"avatar_voice": {"enabled": False}}) == ""
    assert avatar_probe_target(
        {"avatar_voice": {"enabled": True}}) == "http://127.0.0.1:7852/health"
    assert avatar_probe_target(
        {"avatar_voice": {"enabled": True, "base_url": "http://10.0.0.5:7852/"}}
    ) == "http://10.0.0.5:7852/health"


def test_build_health_avatar_component():
    from src.utils.health import build_health

    def comp(h):
        return next((c for c in h["components"] if c["id"] == "avatar_voice"), None)

    # 未启用（None）→ 不出组件
    h = build_health(db_ok=True, avatar_voice=None)
    assert comp(h) is None
    # 就绪 → ok
    h = build_health(db_ok=True, avatar_voice={
        "url": "http://127.0.0.1:7852/health", "reachable": True,
        "models_loaded": True, "latency_ms": 12})
    assert comp(h)["status"] == "ok"
    # 不可达 → warn（软降级不红灯）
    h = build_health(db_ok=True, avatar_voice={
        "url": "http://127.0.0.1:7852/health", "reachable": False,
        "error": "conn refused"})
    assert comp(h)["status"] == "warn"
    assert h["light"] in ("yellow", "red")   # db ok → yellow
    # 可达但未载入 → warn
    h = build_health(db_ok=True, avatar_voice={
        "url": "x", "reachable": True, "models_loaded": False})
    assert comp(h)["status"] == "warn"


def test_probe_avatar_voice_cache_and_disabled(tmp_path):
    from src.inbox import health_watchdog as hw
    # 未启用 → None（不打网络）
    assert hw.probe_avatar_voice({"avatar_voice": {"enabled": False}}) is None
    # 死端口探测 → reachable False，且 TTL 缓存生效（第二次不再探测）
    hw._AVATAR_PROBE_CACHE.update({"ts": 0.0, "url": "", "result": None})
    cfg = {"avatar_voice": {"enabled": True, "base_url": "http://127.0.0.1:1"}}
    r1 = hw.probe_avatar_voice(cfg)
    assert r1 is not None and r1["reachable"] is False
    with patch("urllib.request.urlopen", side_effect=AssertionError("cached!")):
        r2 = hw.probe_avatar_voice(cfg)
    assert r2 == r1
    hw._AVATAR_PROBE_CACHE.update({"ts": 0.0, "url": "", "result": None})


# ── Phase4：台词库一键入库 ───────────────────────────────────────────────────
def test_sanitize_lines_target():
    from src.ai.voice_prerender import sanitize_lines_target
    assert sanitize_lines_target("_common") == "_common"
    assert sanitize_lines_target("lin_xiaoyu") == "lin_xiaoyu"
    assert sanitize_lines_target("") == "_common"          # 缺省 → 公共库
    assert sanitize_lines_target("../evil") == ""          # 路径穿越 → 拒绝
    assert sanitize_lines_target("a/b") == ""
    assert sanitize_lines_target("c:\\x") == ""
    assert sanitize_lines_target("x" * 65) == ""


def test_append_prerender_line_roundtrip(tmp_path):
    from src.ai.voice_prerender import (
        append_prerender_line,
        list_lines_files,
        read_prerender_lines,
    )
    d = str(tmp_path / "lines")
    r1 = append_prerender_line("周末去哪玩呀？", target="_common", lines_dir=d)
    assert r1["ok"] and r1["added"]
    # 同键重复（含归一化差异）→ duplicate 不重写
    r2 = append_prerender_line(" 周末去哪玩呀？\n", target="_common", lines_dir=d)
    assert r2["ok"] and not r2["added"] and r2["reason"] == "duplicate"
    # 人设专属库
    r3 = append_prerender_line("我下播啦", target="p1", lines_dir=d)
    assert r3["added"]
    assert read_prerender_lines("p1", lines_dir=d) == ["周末去哪玩呀？", "我下播啦"]
    # 非法目标 / 空文本 / 超长
    assert append_prerender_line("x", target="../e", lines_dir=d)["reason"] == "bad_target"
    assert append_prerender_line("  ", lines_dir=d)["reason"] == "empty_text"
    assert append_prerender_line("长" * 61, lines_dir=d)["reason"] == "too_long"
    files = list_lines_files(lines_dir=d)
    assert files[0]["target"] == "_common" and files[0]["lines"] == 1
    assert {f["target"] for f in files} == {"_common", "p1"}


def test_append_preserves_comments(tmp_path):
    """追加不破坏文件已有注释/台词。"""
    from src.ai.voice_prerender import append_prerender_line
    d = tmp_path / "lines"
    d.mkdir()
    (d / "_common.txt").write_text("# 注释头\n早安呀\n", encoding="utf-8")
    append_prerender_line("晚安", target="_common", lines_dir=str(d))
    content = (d / "_common.txt").read_text(encoding="utf-8")
    assert content.startswith("# 注释头\n早安呀\n")
    assert content.rstrip().endswith("晚安")


# ── Phase4：看门狗升级提醒 ───────────────────────────────────────────────────
class _FakeBus:
    def __init__(self):
        self.events = []

    def publish(self, etype, data):
        self.events.append((etype, data))


def _watchdog(cfg: dict):
    from src.inbox.health_watchdog import HealthWatchdog

    class _CM:
        config = cfg

    class _App:
        class state:
            pass

    return HealthWatchdog(app=_App(), config_manager=_CM())


def test_watchdog_avatar_voice_escalation(monkeypatch):
    from src.inbox import health_watchdog as hw
    from src.ai.avatar_voice_stats import get_avatar_voice_stats

    get_avatar_voice_stats().reset()   # 防同 worker 先跑的管线测试污染半死信号
    cfg = {"avatar_voice": {"enabled": True},
           "health_watchdog": {"avatar_voice_remind": {
               "enabled": True, "after_min": 30, "interval_min": 240}}}
    wd = _watchdog(cfg)
    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)

    down = {"url": "http://127.0.0.1:7852/health", "reachable": False,
            "models_loaded": False, "error": "conn refused"}
    up = {"url": "http://127.0.0.1:7852/health", "reachable": True,
          "models_loaded": True}

    t0 = 1_000_000.0
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: down)
    wd._check_avatar_voice(now=t0)             # 首次发现 → 只记时间不告警
    assert bus.events == []
    wd._check_avatar_voice(now=t0 + 10 * 60)   # 10min < 30min → 不告警
    assert bus.events == []
    wd._check_avatar_voice(now=t0 + 31 * 60)   # ≥30min → 首提
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "avatar_voice_alert"
    assert data["down_minutes"] == 31 and data["reminder"] is False
    assert data["rate_key"] == "avatar_voice:remind"
    wd._check_avatar_voice(now=t0 + 60 * 60)   # 距首提 29min < 4h → 不重提
    assert len(bus.events) == 1
    wd._check_avatar_voice(now=t0 + 31 * 60 + 241 * 60)  # ≥4h → 重提
    assert len(bus.events) == 2
    assert bus.events[1][1]["reminder"] is True
    assert wd.total_avatar_voice_reminders == 2

    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: up)
    wd._check_avatar_voice(now=t0 + 500 * 60)  # 恢复 → 恢复通知 + 清零
    assert bus.events[-1][0] == "avatar_voice_alert"
    assert bus.events[-1][1].get("recovered") is True
    assert wd._avatar_down_since == 0.0 and wd._avatar_alerted is False


def test_watchdog_avatar_voice_quiet_paths(monkeypatch):
    """未启用/健康/短暂抖动恢复 → 一律零事件（防噪）。"""
    from src.inbox import health_watchdog as hw
    from src.ai.avatar_voice_stats import get_avatar_voice_stats

    get_avatar_voice_stats().reset()
    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    # avatar_voice 未启用 → probe None → 静默
    wd = _watchdog({"health_watchdog": {}})
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: None)
    wd._check_avatar_voice(now=1.0)
    assert bus.events == []
    # 开关显式关 → 静默
    wd2 = _watchdog({"health_watchdog": {"avatar_voice_remind": {"enabled": False}}})
    wd2._check_avatar_voice(now=1.0)
    assert bus.events == []
    # 掉线未到阈值就恢复（抖动）→ 不发恢复通知（从未告警过）
    wd3 = _watchdog({"avatar_voice": {"enabled": True}})
    down = {"reachable": False, "models_loaded": False}
    up = {"reachable": True, "models_loaded": True}
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: down)
    wd3._check_avatar_voice(now=100.0)
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: up)
    wd3._check_avatar_voice(now=200.0)
    assert bus.events == []
    assert wd3._avatar_down_since == 0.0


def test_watchdog_avatar_voice_hang_detection(monkeypatch):
    """半死（2026-07-14 事故形态）：health 绿但真实合成连败 → 升级告警。

    时间线复刻：合成连续失败 ≥3 次（fresh）→ kind=hang 计时 → 20min 首提
    （带 hang/fail_streak 字段）→ 无流量证据陈旧不误报恢复 → 一次真实成功
    合成后才发恢复通知。
    """
    from src.inbox import health_watchdog as hw
    from src.ai.avatar_voice_stats import get_avatar_voice_stats

    stats = get_avatar_voice_stats()
    stats.reset()
    cfg = {"avatar_voice": {"enabled": True},
           "health_watchdog": {"avatar_voice_remind": {
               "enabled": True, "after_min": 30, "interval_min": 240,
               "hang_fail_streak": 3, "hang_after_min": 20,
               "hang_fresh_min": 30}}}
    wd = _watchdog(cfg)
    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    up = {"url": "http://127.0.0.1:7852/health", "reachable": True,
          "models_loaded": True}
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: up)

    t0 = 2_000_000.0
    base = time.time()  # stats 用真实时钟；watchdog now 取 base 邻域保 fresh

    # 连败 2 次 < 阈值 3 → 未激活
    stats.record_synth(ok=False)
    stats.record_synth(ok=False)
    wd._check_avatar_voice(now=base)
    assert wd._avatar_down_since == 0.0 and bus.events == []

    # 第 3 次失败 → hang 激活开始计时；20min 到点首提
    stats.record_synth(ok=False)
    wd._check_avatar_voice(now=base)
    assert wd._avatar_down_since == base and wd._avatar_alert_kind == "hang"
    wd._check_avatar_voice(now=base + 10 * 60)   # 10min < 20min → 不提
    assert bus.events == []
    # 期间又一条真实失败保持信号新鲜（挂死期间本来就持续有回落流量）
    stats.record_synth(ok=False)
    wd._check_avatar_voice(now=base + 21 * 60)   # ≥20min → 首提
    assert len(bus.events) == 1
    etype, data = bus.events[0]
    assert etype == "avatar_voice_alert"
    assert data["hang"] is True and data["fail_streak"] >= 3
    assert data["reachable"] is True and data["models_loaded"] is True

    # 信号变陈旧（无流量）→ hang 不再 active，但无正面证据 → 不发恢复、状态保留
    wd._check_avatar_voice(now=base + 21 * 60 + 25 * 60)
    assert len(bus.events) == 1
    assert wd._avatar_alerted is True

    # 一次真实成功合成（正面证据）→ 恢复通知 + 清零
    stats.record_synth(ok=True, latency_ms=2000)
    wd._check_avatar_voice(now=base + 60 * 60)
    assert bus.events[-1][1].get("recovered") is True
    assert wd._avatar_alerted is False and wd._avatar_alert_kind == ""
    stats.reset()


def test_watchdog_avatar_voice_hang_streak_reset_no_alert(monkeypatch):
    """连败未达阈值就有成功合成 → streak 清零，永不告警（防对偶发失败误报）。"""
    from src.inbox import health_watchdog as hw
    from src.ai.avatar_voice_stats import get_avatar_voice_stats

    stats = get_avatar_voice_stats()
    stats.reset()
    wd = _watchdog({"avatar_voice": {"enabled": True}})
    bus = _FakeBus()
    monkeypatch.setattr(
        "src.integrations.shared.event_bus.get_event_bus", lambda: bus)
    up = {"reachable": True, "models_loaded": True}
    monkeypatch.setattr(hw, "probe_avatar_voice", lambda cfg, **kw: up)

    stats.record_synth(ok=False)
    stats.record_synth(ok=False)
    stats.record_synth(ok=True, latency_ms=1500)   # 成功 → streak 归零
    stats.record_synth(ok=False)                   # 又一次孤立失败
    base = time.time()
    wd._check_avatar_voice(now=base)
    assert wd._avatar_down_since == 0.0 and bus.events == []
    assert stats.hang_signal()["fail_streak"] == 1
    stats.reset()


def test_avatar_voice_stats_hang_signal_fields():
    """hang_signal/dump 字段契约：streak 累计/成功清零/时间戳单调。"""
    from src.ai.avatar_voice_stats import AvatarVoiceStats

    s = AvatarVoiceStats()
    sig = s.hang_signal()
    assert sig == {"fail_streak": 0, "last_ok_ts": 0.0, "last_fail_ts": 0.0}
    s.record_synth(ok=False)
    s.record_synth(ok=False)
    sig = s.hang_signal()
    assert sig["fail_streak"] == 2 and sig["last_fail_ts"] > 0
    d = s.dump()
    assert d["synth_fail_streak"] == 2 and d["last_synth_ok_ts"] == 0.0
    s.record_synth(ok=True, latency_ms=100)
    sig = s.hang_signal()
    assert sig["fail_streak"] == 0
    assert sig["last_ok_ts"] >= sig["last_fail_ts"] > 0
    s.reset()
    assert s.hang_signal()["fail_streak"] == 0


def test_webhook_alias_and_message_for_avatar_voice():
    from src.inbox.webhook_notifier import _EVENT_ALIASES, _build_message
    assert _EVENT_ALIASES["avatar_voice"]["types"] == {"avatar_voice_alert"}
    t1, x1 = _build_message("avatar_voice_alert", {
        "reachable": False, "down_minutes": 65, "url": "http://127.0.0.1:7852/health",
        "error": "conn refused"})
    assert "AvatarHub" in t1 and "1 小时 5 分钟" in t1
    assert "edge" in x1 and "EmotionTTS_Boot" in x1
    t2, x2 = _build_message("avatar_voice_alert", {"recovered": True})
    assert t2.startswith("✅") and "恢复" in t2
    t3, _ = _build_message("avatar_voice_alert", {
        "reachable": True, "models_loaded": False, "down_minutes": 31,
        "reminder": True})
    assert t3.startswith("⏰")
    # 半死形态（2026-07-14 事故）：文案点名「合成挂死」+ 连败数 + 上机排查指引
    t4, x4 = _build_message("avatar_voice_alert", {
        "reachable": True, "models_loaded": True, "hang": True,
        "fail_streak": 5, "down_minutes": 25,
        "url": "http://127.0.0.1:7852/health"})
    assert "合成挂死" in t4 and "连败 5 次" in x4
    assert "EmotionTTSWatchdog" in x4


# ── Phase4：同音色跨人设复用渲染 ─────────────────────────────────────────────
def test_render_persona_reuses_same_ref_output(tmp_path):
    """两个人设共享参考音：同一台词只合成一次，第二人设复制成品（零 GPU）。"""
    import scripts.avatar_prerender as ap

    ref = tmp_path / "shared.wav"
    ref.write_bytes(_wav_bytes(200))
    base = str(tmp_path / "voices")
    calls = {"n": 0}

    class _FakeClient:
        def batch_clone(self, texts, **kw):
            calls["n"] += len(texts)
            return [_wav_bytes(150) for _ in texts]

        def ensure_ready(self, **kw):
            return True

    lines = ["早安呀", "晚安"]
    cache: dict = {}

    # to_voice_note 需要 ffmpeg——在测试里替换为直接落 ogg 假文件
    def fake_to_voice_note(wav, out_dir=None):
        import uuid
        p = Path(out_dir) / f"t-{uuid.uuid4().hex[:6]}.ogg"
        p.write_bytes(b"OggS" + wav[:64])
        return str(p), 1

    with patch("src.ai.avatar_voice.to_voice_note", side_effect=fake_to_voice_note):
        d1, s1, f1 = ap.render_persona(
            _FakeClient(), "p1", str(ref), lines, base_dir=base, ref_cache=cache)
        d2, s2, f2 = ap.render_persona(
            _FakeClient(), "p2", str(ref), lines, base_dir=base, ref_cache=cache)
    assert (d1, f1) == (2, 0)
    assert (d2, f2) == (2, 0)
    assert calls["n"] == 2          # 两条台词只各合成一次（p2 全复用）
    from src.ai.voice_prerender import find_prerendered
    assert find_prerendered("p1", "早安呀", base_dir=base) is not None
    assert find_prerendered("p2", "早安呀", base_dir=base) is not None
    assert find_prerendered("p2", "晚安", base_dir=base) is not None


# ── Phase5：备货生命周期（参考音指纹）───────────────────────────────────────
def test_ref_content_fp_cached_and_changes(tmp_path):
    from src.ai.voice_prerender import ref_content_fp
    f = tmp_path / "ref.wav"
    f.write_bytes(b"VOICE-A")
    fp1 = ref_content_fp(str(f))
    assert fp1 and fp1 == ref_content_fp(str(f))     # 缓存命中一致
    import time as _t
    _t.sleep(0.02)
    f.write_bytes(b"VOICE-B-DIFFERENT")              # 内容变 → 指纹变
    assert ref_content_fp(str(f)) != fp1
    assert ref_content_fp(str(tmp_path / "nope.wav")) == ""


def test_stock_staleness_lifecycle(tmp_path):
    from src.ai.voice_prerender import (
        find_prerendered,
        read_ref_manifest,
        stock_is_stale,
        write_prerendered,
        write_ref_manifest,
    )
    base = str(tmp_path / "voices")
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"OLD-VOICE")
    ogg = tmp_path / "raw.ogg"
    ogg.write_bytes(b"OggS-old")
    write_prerendered("p1", "早安呀", ogg, base_dir=base)
    # 无登记（legacy）→ 不判过期、可命中
    assert stock_is_stale("p1", str(ref), base_dir=base) is False
    assert find_prerendered("p1", "早安呀", base_dir=base, ref_path=str(ref)) is not None
    # 登记指纹后一致 → 可命中
    write_ref_manifest("p1", str(ref), base_dir=base)
    assert read_ref_manifest("p1", base_dir=base)["ref_sha1"]
    assert stock_is_stale("p1", str(ref), base_dir=base) is False
    assert find_prerendered("p1", "早安呀", base_dir=base, ref_path=str(ref)) is not None
    # 换参考音（新音色）→ 过期，命中被拒（回落现场合成，防发错声）
    import time as _t
    _t.sleep(0.02)
    ref.write_bytes(b"NEW-VOICE-CONTENT")
    assert stock_is_stale("p1", str(ref), base_dir=base) is True
    assert find_prerendered("p1", "早安呀", base_dir=base, ref_path=str(ref)) is None
    # 不传 ref_path（旧调用方）→ 行为不变仍命中（向后兼容）
    assert find_prerendered("p1", "早安呀", base_dir=base) is not None
    # 当前参考音读不到 → 视为过期（宁可现场合成）
    assert stock_is_stale("p1", str(tmp_path / "gone.wav"), base_dir=base) is True


@pytest.mark.asyncio
async def test_pipeline_refuses_stale_stock(tmp_path):
    """管线级：参考音换了 → 预渲染拒绝命中 → 走正常合成路径。"""
    from src.ai.voice_prerender import write_prerendered, write_ref_manifest
    base = tmp_path / "voices"
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"OLD")
    src = tmp_path / "raw.ogg"
    src.write_bytes(b"OggS-old-voice")
    write_prerendered("p1", "早安呀", src, base_dir=str(base))
    write_ref_manifest("p1", str(ref), base_dir=str(base))
    import time as _t
    _t.sleep(0.02)
    ref.write_bytes(b"NEW-VOICE")   # 换声

    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({
        "enabled": True, "backend": "disabled", "fallback_on_error": False,
        "persona_id": "p1", "out_dir": str(tmp_path / "out"),
        "tts_cache": {"enabled": False},
        "voice_profile": {"enabled": True, "owner_consent": True,
                          "reference_audio_path": str(ref)},
        "avatar_voice": {"enabled": True,
                         "prerender": {"enabled": True, "base_dir": str(base)}},
    })
    rv = await tts.synthesize("早安呀")
    assert not rv.ok and "backend disabled" in rv.error   # 未命中 → 落正常合成


def test_render_persona_auto_force_on_ref_drift(tmp_path):
    """渲染检测指纹漂移 → 整目录重渲（旧 clips 不再被 skip 卡死）+ 登记新指纹。"""
    import scripts.avatar_prerender as ap
    from src.ai.voice_prerender import read_ref_manifest, write_ref_manifest

    ref = tmp_path / "ref.wav"
    ref.write_bytes(_wav_bytes(200))
    base = str(tmp_path / "voices")
    calls = {"n": 0}

    class _FakeClient:
        def batch_clone(self, texts, **kw):
            calls["n"] += len(texts)
            return [_wav_bytes(120) for _ in texts]

        def ensure_ready(self, **kw):
            return True

    def fake_to_voice_note(wav, out_dir=None):
        import uuid
        p = Path(out_dir) / f"t-{uuid.uuid4().hex[:6]}.ogg"
        p.write_bytes(b"OggS" + wav[:32])
        return str(p), 1

    with patch("src.ai.avatar_voice.to_voice_note", side_effect=fake_to_voice_note):
        d1, s1, _ = ap.render_persona(_FakeClient(), "p1", str(ref), ["早安呀"],
                                      base_dir=base)
        assert (d1, s1) == (1, 0)
        m1 = read_ref_manifest("p1", base_dir=base)
        assert m1 and m1["ref_sha1"]
        # 同参考音再跑 → 全 skip 零合成
        d2, s2, _ = ap.render_persona(_FakeClient(), "p1", str(ref), ["早安呀"],
                                      base_dir=base)
        assert (d2, s2) == (0, 1)
        # 换参考音 → 指纹漂移 → 自动 force 重渲 + 新指纹
        import time as _t
        _t.sleep(0.02)
        ref.write_bytes(_wav_bytes(300, rate=16000))
        d3, s3, _ = ap.render_persona(_FakeClient(), "p1", str(ref), ["早安呀"],
                                      base_dir=base)
        assert (d3, s3) == (1, 0)
        m2 = read_ref_manifest("p1", base_dir=base)
        assert m2["ref_sha1"] != m1["ref_sha1"]
    assert calls["n"] == 2   # 首渲 1 + 漂移重渲 1（中间零合成）


# ── Phase5：缺口自动入库 ─────────────────────────────────────────────────────
def test_qualify_auto_stock_guards():
    from src.ai.voice_prerender import qualify_auto_stock
    assert qualify_auto_stock("想你啦", 5) == (True, "")
    assert qualify_auto_stock("想你啦", 4)[1] == "below_threshold"
    assert qualify_auto_stock("这句话特别长超过了十六个字的体裁上限啦", 9)[1] == "too_long"
    assert qualify_auto_stock("明早8点见", 9)[1] == "has_digit"
    assert qualify_auto_stock("看 www.x.com", 9)[1] == "bad_marks"
    assert qualify_auto_stock("加我微信号哦", 9)[1] == "blocked_word"
    assert qualify_auto_stock("给我转账吧", 9)[1] == "blocked_word"
    assert qualify_auto_stock("", 9)[1] == "empty"


def test_auto_stock_from_misses_targets_and_budget(tmp_path):
    from src.ai.voice_prerender import auto_stock_from_misses, read_prerender_lines
    d = str(tmp_path / "lines")
    misses = [
        # 单人设占比 ≥80% → 进专属库
        {"text": "我下播啦", "n": 6, "personas": {"lin_xiaoyu": 6}},
        # 多人设分散 → 公共库
        {"text": "想你啦", "n": 8, "personas": {"a": 4, "b": 4}},
        # 不达标 → skip
        {"text": "偶尔一句", "n": 1, "personas": {}},
        # 守卫拦截 → skip
        {"text": "转账给我", "n": 9, "personas": {}},
    ]
    rv = auto_stock_from_misses(misses, min_count=3, max_add=10, lines_dir=d)
    assert {a["text"]: a["target"] for a in rv["added"]} == {
        "我下播啦": "lin_xiaoyu", "想你啦": "_common"}
    reasons = {s["text"]: s["reason"] for s in rv["skipped"]}
    assert reasons["偶尔一句"] == "below_threshold"
    assert reasons["转账给我"] == "blocked_word"
    assert read_prerender_lines("lin_xiaoyu", lines_dir=d) == ["想你啦", "我下播啦"]
    # 预算=1 → 只入第一条
    rv2 = auto_stock_from_misses(
        [{"text": "早点睡哦", "n": 9}, {"text": "路上小心", "n": 9}],
        min_count=3, max_add=1, lines_dir=d)
    assert len(rv2["added"]) == 1


def test_watchdog_auto_stock_throttle_and_budget(monkeypatch, tmp_path):
    from src.ai.avatar_voice_stats import get_avatar_voice_stats

    cfg = {"avatar_voice": {"enabled": True, "prerender": {"auto_stock": {
        "enabled": True, "min_count": 2, "max_per_day": 3}}}}
    wd = _watchdog(cfg)
    st = get_avatar_voice_stats()
    st.record_prerender_miss("想你啦", persona_id="p1")
    st.record_prerender_miss("想你啦", persona_id="p1")

    calls = {"n": 0, "budget": []}

    def fake_auto_stock(misses, *, min_count, max_add, **kw):
        calls["n"] += 1
        calls["budget"].append(max_add)
        return {"added": [{"text": "想你啦", "target": "p1"}][:max_add],
                "skipped": []}

    monkeypatch.setattr(
        "src.ai.voice_prerender.auto_stock_from_misses", fake_auto_stock)
    t0 = 1_700_000_000.0
    wd._check_avatar_auto_stock(now=t0)
    assert calls["n"] == 1 and wd.total_auto_stocked == 1
    wd._check_avatar_auto_stock(now=t0 + 600)      # <1h 节流 → 不跑
    assert calls["n"] == 1
    wd._check_avatar_auto_stock(now=t0 + 3700)     # ≥1h → 再跑，预算递减
    assert calls["n"] == 2 and calls["budget"] == [3, 2]
    # 关闭/未启用 → 静默
    wd2 = _watchdog({"avatar_voice": {"enabled": True}})
    wd2._check_avatar_auto_stock(now=t0)
    assert wd2.total_auto_stocked == 0


def test_stats_miss_persona_attribution():
    st = AvatarVoiceStats()
    st.record_prerender_miss("想你啦", persona_id="a")
    st.record_prerender_miss("想你啦", persona_id="a")
    st.record_prerender_miss("想你啦", persona_id="b")
    st.record_prerender_miss("晚安", persona_id="")
    top = st.dump()["top_misses"]
    m = {x["text"]: x for x in top}
    assert m["想你啦"]["n"] == 3
    assert m["想你啦"]["personas"] == {"a": 2, "b": 1}
    assert m["晚安"]["personas"] == {}


# ── Phase6：7854 语言语义修正 + NLLB translate ──────────────────────────────
def test_build_stt_payload_language_semantics():
    """具体语种原样传；auto/空 → 空串（服务端自动检测）。

    背景（2026-07-13 实测契约）：language 是 Whisper 的**强制转写语言**——英文
    音频 + "zh" 会输出中文**译文**而非转写；"auto" 直接 500。空串=自动检测。
    """
    from src.ai.avatar_voice import build_stt_payload
    assert json.loads(build_stt_payload(b"x", language="zh").decode())["language"] == "zh"
    assert json.loads(build_stt_payload(b"x", language="en").decode())["language"] == "en"
    assert json.loads(build_stt_payload(b"x", language="auto").decode())["language"] == ""
    assert json.loads(build_stt_payload(b"x", language="AUTO").decode())["language"] == ""
    assert json.loads(build_stt_payload(b"x", language="").decode())["language"] == ""


@pytest.mark.asyncio
async def test_avatar_whisper_auto_language_not_forced_zh(tmp_path):
    """回落转录器收到 language='auto' → 送空串（自动检测），绝不硬编 zh。"""
    from src.voice_transcriber import AvatarWhisperTranscriber

    tok = tmp_path / "tok.txt"
    tok.write_text("tk", encoding="utf-8")
    voice = tmp_path / "v.wav"
    voice.write_bytes(_wav_bytes(300, rate=16000))
    t = AvatarWhisperTranscriber({
        "temp_dir": str(tmp_path / "tmp"),
        "base_url": "http://198.51.100.1:7854",
        "token_file": str(tok),
    })
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["payload"] = json.loads(req.data.decode())

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"ok": True, "text": "hello there", "no_speech_prob": 0.0}).encode()

        return _R()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = await t.transcribe_voice_message(str(voice), "auto")
    assert out == "hello there"
    assert sent["payload"]["language"] == ""   # 自动检测，不是 zh


def test_avatar_translate_method(tmp_path):
    tok = tmp_path / "t.txt"
    tok.write_text("tk", encoding="utf-8")
    c = AvatarVoiceClient({"enabled": True, "stt": {"token_file": str(tok)}})
    sent = {}

    def fake_post(self, url, payload, *, timeout, headers=None):
        sent["url"] = url
        sent["body"] = json.loads(payload.decode())
        sent["headers"] = headers
        return json.dumps({"ok": True, "text": "Hello there", "elapsed_ms": 70}).encode()

    with patch.object(AvatarVoiceClient, "_post", fake_post):
        out = c.translate("你好呀", src="zh", dest="en")
    assert out == "Hello there"
    assert sent["url"].endswith("/translate")
    assert sent["body"] == {"text": "你好呀", "src": "zh", "dest": "en"}
    assert sent["headers"]["X-AH-Svc"] == "tk"
    # 令牌缺失 / 空文本 / 服务失败 → None（不抛）
    c2 = AvatarVoiceClient({"enabled": True,
                            "stt": {"token_file": str(tmp_path / "nope.txt")}})
    assert c2.translate("你好") is None
    assert c.translate("") is None
    with patch.object(AvatarVoiceClient, "_post", side_effect=OSError("down")):
        assert c.translate("你好") is None


# ── enroll profile 纯函数 ────────────────────────────────────────────────────
def test_build_avatar_voice_profile():
    from src.ai.voice_enroll import build_avatar_voice_profile
    vp = build_avatar_voice_profile(
        reference_audio_path="voice_samples/x.wav", speaker_id="x",
        reference_text="逐字稿")
    assert vp["backend"] == "avatar_clone"
    assert vp["enabled"] and vp["owner_consent"]
    assert vp["reference_text"] == "逐字稿"
    assert vp["emotion_default"] == "gentle"
    vp2 = build_avatar_voice_profile(
        reference_audio_path="a.wav", speaker_id="a")
    assert "reference_text" not in vp2
