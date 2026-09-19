# -*- coding: utf-8 -*-
"""语音活人感 P0 批次门禁（2026-08-03）。

覆盖四件事：
  P0-1 语音稿生成范式（spoken_variant 指令 + LLM vivid prompt 的「禁感叹词开头/
       短句」钉子 + 提示词版本 bump=旧缓存失效）；
  P0-2 开场词会话级去重守卫（voice_opener_guard 纯函数 + 注册表语义 +
       TTSPipeline/persona_voice 接线）；
  P0-4 B 线分条（resolve_split_send_cfg / part_gap_seconds / stage_voice_parts
       的门控与「全部合成成功才返回、任一失败清理回落」不变量）。
  P0-3 hub 情感阈值覆写（hub_fish.emotion_threshold → to_cosyvoice_emotion）。
"""
from __future__ import annotations

import asyncio
import types

import pytest

from src.ai.voice_opener_guard import (
    guard_opener,
    leading_opener,
    normalize_opener,
    peek_history,
    reset_opener_state,
    should_strip,
    strip_opener,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_opener_state()
    yield
    reset_opener_state()


# ── P0-2 纯函数 ───────────────────────────────────────────────────────────────
def test_leading_opener_basic_and_normalize():
    op, rest = leading_opener("嘿，这回应该能听清我说的了。")
    assert op == "嘿" and rest.startswith("这回")
    op, rest = leading_opener("哈哈哈，你还真执着啊")
    assert op == "哈哈"                      # 连字笑声归一
    assert normalize_opener("哈哈哈哈") == "哈哈"
    op, _ = leading_opener("哎呀，这话说得我都不知咋回了")
    assert op == "哎呀"


def test_leading_opener_requires_separator():
    # 「啊？」是真实反应不是话语标记：？不算分隔符 → 不认为开场词
    op, _ = leading_opener("啊？你说什么")
    assert op == ""
    # 开场词直接连正文（无分隔）也不认——「嘿嘿的笑着」是句子成分
    op, _ = leading_opener("嘿嘿的笑着不说话")
    assert op == ""
    # 承接连接词不在词表（剥了伤句意）
    op, _ = leading_opener("不过，我觉得也行")
    assert op == ""


def test_strip_opener_safety():
    out, removed = strip_opener("嘿，这回应该能听清我说的了。")
    assert removed == "嘿" and out.startswith("这回")
    # 剥后余文过短 → 不剥（整条几乎就是开场词）
    out, removed = strip_opener("嗯嗯，好")
    assert removed == "" and out == "嗯嗯，好"
    # 无开场词 → 原样
    out, removed = strip_opener("今天过得怎么样呀")
    assert removed == "" and out == "今天过得怎么样呀"


def test_should_strip_window_and_fatigue():
    # 重复规则：最近 window 条出现同款 → 剥
    assert should_strip("嘿", ["嘿"], window=2)
    assert should_strip("嘿", ["哈哈", "嘿"], window=2)
    assert not should_strip("嘿", ["哈哈", "哎呀"], window=2)
    assert not should_strip("嘿", [], window=2)
    # 疲劳规则：最近 fatigue 条全带开场词（不论哪个）→ 剥
    assert should_strip("诶", ["嘿", "哈哈", "哎呀"], window=2, fatigue_streak=3)
    assert not should_strip("诶", ["嘿", "", "哎呀"], window=2, fatigue_streak=3)


def test_guard_opener_end_to_end_dedupe_and_isolation():
    k = "telegram:acct:chat1"
    t1 = "嘿，这回应该能听清我说的了吧。"
    t2 = "嘿，刚看见票房新闻了，国漫真不容小觑。"
    # 首次使用：放行并记录
    assert guard_opener(k, t1) == t1
    assert peek_history(k) == ["嘿"]
    # 紧跟同款开场 → 剥除，且本条按「无开场词」入账
    out2 = guard_opener(k, t2)
    assert not out2.startswith("嘿") and "刚看见" in out2
    assert peek_history(k) == ["嘿", ""]
    # 第三条又用嘿：窗口=2 内（["嘿",""]）仍有嘿 → 继续剥
    out3 = guard_opener(k, "嘿，晚上好呀，今天咋样？")
    assert not out3.startswith("嘿")
    # 第四条：窗口内两条都是 "" → 放行（每 ~3 条允许一次开场词，保留活气）
    out4 = guard_opener(k, "嘿，跟你说个事儿哈。")
    assert out4.startswith("嘿")
    # 不同会话互不影响
    assert guard_opener("telegram:acct:chat2", t2) == t2
    # 空 key → 原样、不记录（预渲染/试听）
    assert guard_opener("", t1) == t1


def test_guard_opener_records_no_opener_messages():
    k = "telegram:acct:chat3"
    guard_opener(k, "今天去了趟超市，买了点水果。")
    assert peek_history(k) == [""]


# ── P0-2 接线 ────────────────────────────────────────────────────────────────
def test_tts_pipeline_accepts_variety_key():
    from src.ai.tts_pipeline import TTSPipeline
    tts = TTSPipeline({"variety_key": "telegram:a:c"})
    assert tts.variety_key == "telegram:a:c"
    assert TTSPipeline({}).variety_key == ""


def test_resolve_effective_voice_context_injects_variety_key(monkeypatch):
    import src.ai.persona_voice as pv
    monkeypatch.setattr(
        pv, "resolve_voice_cfg_for_contact", lambda *a, **k: {})
    monkeypatch.setattr(
        pv, "resolve_emotion_for_send", lambda *a, **k: None)
    ctx = pv.resolve_effective_voice_context(
        {}, persona_id="p1", chat_key="chat9",
        platform="telegram", account_id="acct7")
    assert ctx["voice_cfg"].get("variety_key") == "telegram:acct7:chat9"
    # 无会话上下文（预渲染/试听）→ 不注入
    ctx2 = pv.resolve_effective_voice_context({}, persona_id="p1")
    assert "variety_key" not in ctx2["voice_cfg"]


# ── P0-1 生成指令 / 改写 prompt ───────────────────────────────────────────────
def test_spoken_variant_instruction_new_paradigm():
    from src.ai.spoken_variant import SPOKEN_MARKER, build_spoken_variant_instruction
    ins = build_spoken_variant_instruction()
    assert SPOKEN_MARKER in ins and "口语" in ins          # 旧契约不动
    assert "哈哈" in ins and "嘿" in ins                    # 禁感叹词开头点名在场
    assert "十五个字" in ins                                # 句长预算
    assert "反问" in ins                                    # 反问预算
    assert "口误" not in ins
    assert "口误" in build_spoken_variant_instruction(disfluency=True)


def test_vivid_prompt_bans_openers_and_long_sentences():
    from src.ai.voice_colloquial_llm import build_colloquial_prompt
    p = build_colloquial_prompt("warm", lead=True, intensity="vivid")
    assert "感叹词" in p and "开场" in p       # 禁开场词规则
    assert "十五个字" in p                     # 句长规则
    assert "嘿，" not in p                     # 不再教「最多偶尔一个嘿，」
    # 反面钉子（既有门禁口径不回归）
    assert "绝不能回答" in p and "我叫林佳欣" in p


def test_prompt_version_bumped_for_cache_invalidation():
    from src.ai.voice_colloquial_llm import _PROMPT_VERSION
    assert _PROMPT_VERSION >= 3


# ── P0-3 hub 情感阈值 ────────────────────────────────────────────────────────
def test_hub_emotion_threshold_lowers_neutral_collapse():
    from src.ai.voice_emotion import to_cosyvoice_emotion

    class _Spec:
        emotion = "happy"
        intensity = 0.45

        def is_neutral(self):
            return False

    # 全局阈值（0.5）下弱情绪塌缩为 neutral；hub 阈值 0.35 放行
    assert to_cosyvoice_emotion(_Spec(), default="neutral",
                                strong_threshold=0.5) == "neutral"
    assert to_cosyvoice_emotion(_Spec(), default="neutral",
                                strong_threshold=0.35) != "neutral"


# ── P0-4 分条 ────────────────────────────────────────────────────────────────
def test_resolve_split_send_cfg_defaults_off():
    from src.inbox.voice_autosend import resolve_split_send_cfg
    assert resolve_split_send_cfg({})["enabled"] is False
    assert resolve_split_send_cfg(None)["enabled"] is False
    got = resolve_split_send_cfg({"split_send": {
        "enabled": True, "part_max_chars": 30, "max_parts": 2}})
    assert got["enabled"] and got["part_max_chars"] == 30 and got["max_parts"] == 2
    assert got["min_total_chars"] == 40      # 缺省值
    # 坏值回退默认
    bad = resolve_split_send_cfg({"split_send": {"enabled": True,
                                                 "part_max_chars": "x"}})
    assert bad["part_max_chars"] == 36


def test_part_gap_seconds_bounds_and_determinism():
    from src.inbox.voice_autosend import part_gap_seconds
    g1 = part_gap_seconds("你今天咋样呀", 4000)
    g2 = part_gap_seconds("你今天咋样呀", 4000)
    assert g1 == g2                            # 确定性（crc32 抖动，无 RNG）
    assert 0.9 <= g1 <= 6.0
    # 时长越长间隔越长（录音物理节奏）
    assert part_gap_seconds("abc", 12000) > part_gap_seconds("abc", 1000)
    # 上限收口
    assert part_gap_seconds("abc", 120000, gap_max_sec=5.0) == 5.0


class _FakeResult:
    def __init__(self, text, ok=True):
        self.ok = ok
        self.text = text
        self.provider = "hub_fish"
        self.voice = "spk"
        self.extra = {}
        self.latency_ms = 5
        self.duration_sec = max(1.0, len(text) * 0.25)
        self.audio_path = ""


def _mk_fake_tts(tmp_path, fail_at=None, calls=None):
    class _FakeTTS:
        def __init__(self, cfg):
            self.cfg = cfg

        async def prepass_colloquial_llm(self, text, **kw):
            return None

        async def synthesize(self, text, **kw):
            if calls is not None:
                calls.append({"text": text, **kw})
            idx = len([c for c in (calls or [])])
            if fail_at is not None and idx == fail_at:
                return _FakeResult(text, ok=False)
            r = _FakeResult(text)
            # 真 OggS+OpusHead 魔数（PTT 闸要求；假 WAV 会被拒）
            p = tmp_path / f"part_{idx}_{len(text)}.ogg"
            p.write_bytes(b"OggS" + b"\x00" * 60 + b"OpusHead" + b"\x00" * 32)
            r.audio_path = str(p)
            return r

    return _FakeTTS


def _split_cfg(enabled=True):
    return {
        "inbox": {"l2_autosend": {"voice": {
            "enabled": True, "trigger": "always",
            "split_send": {"enabled": enabled, "min_total_chars": 40,
                           "part_max_chars": 24, "max_parts": 3},
            # 质量闸门显式关（Fake 时长与文本无物理关系，防误拦）
            "quality_gate": {"enabled": False},
        }}},
    }


_LONG = ("我刚从健身房回来，顺路去超市买了点水果和牛奶。"
         "今天教练加了新动作，胳膊现在还有点酸。你那边晚饭吃了没呀？")


@pytest.mark.asyncio
async def test_stage_voice_parts_disabled_or_short_returns_none(tmp_path, monkeypatch):
    from src.inbox import voice_autosend as va
    # 未开 → None（不做任何解析）
    out = await va.stage_voice_parts(
        _split_cfg(enabled=False), "telegram", "a1", "p1", _LONG)
    assert out is None
    # 开了但文本短 → None
    out = await va.stage_voice_parts(
        _split_cfg(), "telegram", "a1", "p1", "短句子。")
    assert out is None


@pytest.mark.asyncio
async def test_stage_voice_parts_success_all_parts(tmp_path, monkeypatch):
    from src.inbox import voice_autosend as va
    calls = []
    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "fake"}, "emotion": None})
    monkeypatch.setattr(
        "src.ai.tts_pipeline.TTSPipeline", _mk_fake_tts(tmp_path, calls=calls))
    monkeypatch.setattr(
        "src.client.voice_sender.convert_to_ogg_opus",
        lambda p, delete_src=True: p)
    saved = []

    def _fake_save(platform, account_id, name, data):
        loc = tmp_path / f"out_{len(saved)}.ogg"
        loc.write_bytes(data)
        saved.append(str(loc))
        return str(loc), f"/static/outbound/{name}", "voice"

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", _fake_save)

    out = await va.stage_voice_parts(
        _split_cfg(), "telegram", "a1", "p1", _LONG)
    assert out is not None and len(out) >= 2
    # 每条 meta 带转写与序号；首条 colloquial_lead=True 其余 False
    assert all(m.get("part_text") for _, _, m in out)
    assert [m["part_index"] for _, _, m in out] == list(range(len(out)))
    leads = [c["colloquial_lead"] for c in calls]
    assert leads[0] is True and all(x is False for x in leads[1:])
    assert all(c["split_part"] is True for c in calls)
    assert all(c["skip_llm_colloquial"] is True for c in calls)
    # 拼回全文=完整内容（分条绝不丢句子）
    joined = "".join(m["part_text"] for _, _, m in out)
    assert joined == _LONG


@pytest.mark.asyncio
async def test_stage_voice_parts_one_fail_cleans_and_falls_back(tmp_path, monkeypatch):
    from src.ai.spoken_variant import stash_spoken_variant, take_spoken_variant
    from src.inbox import voice_autosend as va
    calls = []
    monkeypatch.setattr(
        "src.ai.persona_voice.resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "fake"}, "emotion": None})
    monkeypatch.setattr(
        "src.ai.tts_pipeline.TTSPipeline",
        _mk_fake_tts(tmp_path, fail_at=2, calls=calls))
    monkeypatch.setattr(
        "src.client.voice_sender.convert_to_ogg_opus",
        lambda p, delete_src=True: p)
    saved = []

    def _fake_save(platform, account_id, name, data):
        loc = tmp_path / f"out_{len(saved)}.ogg"
        loc.write_bytes(data)
        saved.append(loc)
        return str(loc), f"/static/outbound/{name}", "voice"

    monkeypatch.setattr(
        "src.integrations.protocol_bridge.save_outbound_media", _fake_save)
    # 预置生成层口语版：失败路径必须把它重新入暂存（单条回落还能用）
    stash_spoken_variant(_LONG, _LONG + "口语说法版本呀", scope="a1")

    out = await va.stage_voice_parts(
        _split_cfg(), "telegram", "a1", "p1", _LONG)
    assert out is None
    # 已 stage 的第 1 条文件被清理
    assert all(not p.exists() for p in saved)
    # 口语版被重新入暂存（可再次取到）
    assert take_spoken_variant(_LONG, scope="a1")


def test_autosend_helpers_split_wiring_present():
    """接线静态钉子：autosend_voice 分条优先 + 回落单条旧路径都在场。"""
    import inspect

    from src.inbox import autosend_helpers as ah
    # 2026-09-19 投递逻辑抽到 _deliver_voice_via_orchestrator（决策闸 _voice_gate 与桌面桥共用）；
    # 钉子看「入口 + 投递」两段合体源码，语义不变。
    src = inspect.getsource(ah.autosend_voice) + inspect.getsource(ah._deliver_voice_via_orchestrator)
    assert "_voice_gate" in src
    assert "stage_voice_parts" in src
    assert "part_gap_seconds" in src
    assert "stage_voice_file" in src          # 单条旧路径仍在（回落）
    # 分条中途失败语义：已发算数、剩余丢弃（绝不重发已出口条目）
    assert "剩余丢弃" in src
