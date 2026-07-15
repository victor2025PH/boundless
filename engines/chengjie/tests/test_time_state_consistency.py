"""P1 时间/状态一致性 + 依赖健康告警单测（2026-07-15 事故第二阶段修复）。

覆盖：
- scene_conflicts_with_hour 上午/午后收紧（清晨不再取到 afternoon/evening 场景）
- resolve_meal_state / meal_state_note 确定性饮食状态事实源
- build_time_context_line 清晨/深夜作息白名单
- voice_colloquial_llm.health_signal + HealthWatchdog 连败升级提醒生命周期
- SER 危机联动需连续 2 条声学困扰（单条满分误判不再触发）
- voice_autosend 截断质量闸门（拒发回落）
- VoiceBurstGuard 连发监测 + note_voice_send 告警链
"""
from __future__ import annotations

import datetime as dt
import time
from types import SimpleNamespace

import pytest


# ── 场景-时段过滤收紧 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("scene,hour,conflict", [
    # 2026-07-15 收紧：上午剔下午/黄昏/正午词（事故：7:44 取到 afternoon 场景）
    ("campus walkway, afternoon light", 7, True),
    ("convenience store, evening shift", 8, True),
    ("rooftop at noon", 9, True),
    ("morning light kitchen", 8, False),          # 上午 vs 清晨词 ✓
    # 午后剔清晨词
    ("morning light kitchen", 14, True),
    ("campus walkway, afternoon light", 15, False),
    # 原有行为保持
    ("city night lights bokeh", 10, True),
    ("cozy dorm room, warm lamp light", 7, False),  # 中性词任何时段可用
])
def test_scene_hour_conflict_tightened(scene, hour, conflict):
    from src.ai.companion_selfie import scene_conflicts_with_hour
    assert scene_conflicts_with_hour(scene, hour) is conflict


def test_pick_scene_morning_avoids_afternoon():
    """事故场景回归：清晨/上午任何 salt 都不许取到下午/傍晚场景。"""
    from src.ai.companion_selfie import pick_scene_hint
    p = {"selfie_scenes": [
        "university campus walkway, afternoon light",
        "cozy dorm room desk with study notes, warm lamp light",
        "convenience store where she works part-time, evening shift",
        "matcha dessert cafe, soft window light",
        "night street food market with warm lantern lights",
    ]}
    for salt in range(8):
        sc = pick_scene_hint(p, now=dt.datetime(2026, 7, 15, 7, 44), salt=salt)
        assert "afternoon" not in sc and "evening" not in sc and "night" not in sc


# ── 饮食状态事实源 ───────────────────────────────────────────────────────────
def test_meal_state_deterministic_within_day():
    from src.ai.companion_selfie import resolve_meal_state
    t = dt.datetime(2026, 7, 15, 12, 10)
    a = resolve_meal_state("lin_xiaoyu", now=t)
    b = resolve_meal_state("lin_xiaoyu", now=t)
    assert a and a == b                       # 同刻恒定
    # 同一天不同分钟（同一状态区间内）也不翻转事实方向：只要还没到确定性
    # 进餐时刻，"还没吃"不会闪回"吃过"
    later = resolve_meal_state("lin_xiaoyu", now=dt.datetime(2026, 7, 15, 12, 11))
    assert ("还没吃" in a) == ("还没吃" in later) or "刚吃过" in later


def test_meal_state_key_hours():
    from src.ai.companion_selfie import resolve_meal_state
    # 清晨 7:44（事故时刻）：只能是「还没吃早饭」或「刚吃过早饭」——绝无午饭/晚饭
    out = resolve_meal_state("lin_xiaoyu", now=dt.datetime(2026, 7, 15, 7, 44))
    assert "早饭" in out
    # 深夜 3 点：夜宵口径
    out3 = resolve_meal_state("lin_xiaoyu", now=dt.datetime(2026, 7, 15, 3, 0))
    assert "深夜" in out3 and "夜宵" in out3
    # 22 点：晚饭已吃
    out22 = resolve_meal_state("lin_xiaoyu", now=dt.datetime(2026, 7, 15, 22, 0))
    assert "晚饭" in out22 and "吃过" in out22
    # 10:30：早饭方向（吃过/刚吃过），不该出现午饭晚饭断言
    out10 = resolve_meal_state("lin_xiaoyu", now=dt.datetime(2026, 7, 15, 10, 30))
    assert "早饭" in out10


def test_meal_state_note_wraps_fact():
    from src.ai.companion_selfie import meal_state_note
    note = meal_state_note("lin_xiaoyu", now=dt.datetime(2026, 7, 15, 7, 44))
    assert note.startswith("【你的饮食状态")
    assert "事实不能变" in note


# ── 时间注入 + 作息白名单 ────────────────────────────────────────────────────
def test_time_context_line_morning_whitelist():
    from src.ai.ai_client import build_time_context_line
    line = build_time_context_line(dt.datetime(2026, 7, 15, 7, 44))
    assert "清晨" in line and "作息合理性" in line and "刚下课" in line


def test_time_context_line_late_night_and_plain_daytime():
    from src.ai.ai_client import build_time_context_line
    night = build_time_context_line(dt.datetime(2026, 7, 15, 23, 30))
    assert "深夜" in night and "作息合理性" in night
    noonish = build_time_context_line(dt.datetime(2026, 7, 15, 15, 0))
    assert "下午" in noonish and "作息合理性" not in noonish


# ── 口语化 LLM 健康信号 + watchdog 升级提醒 ──────────────────────────────────
def _mk_watchdog(cfg=None):
    from src.inbox.health_watchdog import HealthWatchdog
    return HealthWatchdog(
        app=SimpleNamespace(state=SimpleNamespace()),
        config_manager=SimpleNamespace(config=cfg or {}))


def test_colloquial_health_signal_lifecycle():
    import src.ai.voice_colloquial_llm as vc
    vc.reset_state()
    sig = vc.health_signal()
    assert sig["fail_streak"] == 0 and sig["last_fail_ts"] == 0.0
    for _ in range(4):
        vc._record_failure()
    sig = vc.health_signal()
    assert sig["fail_streak"] == 4 and sig["in_cooldown"] and sig["last_fail_ts"] > 0
    vc._record_success()
    sig = vc.health_signal()
    assert sig["fail_streak"] == 0 and sig["last_ok_ts"] >= sig["last_fail_ts"]
    vc.reset_state()


def test_watchdog_colloquial_llm_alert_and_recovery(monkeypatch):
    import src.ai.voice_colloquial_llm as vc
    from src.integrations.shared import event_bus as eb

    published = []
    monkeypatch.setattr(
        eb, "get_event_bus",
        lambda: SimpleNamespace(publish=lambda t, d: published.append((t, d))))

    vc.reset_state()
    cfg = {"health_watchdog": {"colloquial_llm_remind": {
        "fail_streak": 6, "fresh_min": 99999, "after_min": 30,
        "interval_min": 240}}}
    wd = _mk_watchdog(cfg)
    for _ in range(9):                      # 事故样本：九连败
        vc._record_failure()
    t0 = time.time()
    wd._check_colloquial_llm(now=t0)        # 首个周期只建 down_since
    assert published == []
    wd._check_colloquial_llm(now=t0 + 31 * 60)   # 超 after_min → 首提
    assert published[-1][0] == "colloquial_llm_alert"
    assert published[-1][1]["fail_streak"] == 9
    assert published[-1][1]["reminder"] is False
    n = len(published)
    wd._check_colloquial_llm(now=t0 + 32 * 60)   # 未到重提间隔 → 静默
    assert len(published) == n
    wd._check_colloquial_llm(now=t0 + 31 * 60 + 241 * 60)   # 超重提间隔
    assert published[-1][1]["reminder"] is True
    vc._record_success()                    # 正面恢复证据
    wd._check_colloquial_llm(now=t0 + 31 * 60 + 242 * 60)
    assert published[-1][1].get("recovered") is True
    vc.reset_state()


def test_watchdog_colloquial_llm_silent_when_clean():
    import src.ai.voice_colloquial_llm as vc
    vc.reset_state()
    wd = _mk_watchdog()
    wd._check_colloquial_llm(now=time.time())   # 无失败信号 → 全静默不炸
    assert wd.total_colloquial_llm_reminders == 0


# ── SER 危机联动需连续 2 条 ──────────────────────────────────────────────────
def _sad_audio():
    from src.ai.speech_emotion import map_audio_emotion
    return map_audio_emotion("sad", 1.0)    # 事故样本：满分 sad


def test_audio_distress_requires_two_consecutive_voices():
    from src.utils.emotional_context import build_emotional_context_block
    ctx = {"_peer_audio_emotion": _sad_audio()}
    build_emotional_context_block("吃饭了吗", ctx)
    # 第一条满分 sad：不联动（事故修复点——单条误判不再触发危机）
    assert ctx.get("_wellbeing_crisis_level") == "none"
    assert ctx.get("_audio_distress_streak") == 1
    build_emotional_context_block("在忙吗", ctx)
    # 第二条连续声学困扰 → 才抬 elevated
    assert ctx.get("_wellbeing_crisis_level") == "elevated"
    assert ctx.get("_wellbeing_crisis_category") == "audio_distress"


def test_audio_distress_streak_resets_on_text_message():
    from src.utils.emotional_context import build_emotional_context_block
    ctx = {"_peer_audio_emotion": _sad_audio()}
    build_emotional_context_block("吃饭了吗", ctx)
    assert ctx.get("_audio_distress_streak") == 1
    ctx["_peer_audio_emotion"] = None       # 中间夹一条文字消息
    build_emotional_context_block("哈哈哈没有啦", ctx)
    assert ctx.get("_audio_distress_streak") == 0
    assert ctx.get("_wellbeing_crisis_level") == "none"


# ── voice_autosend 截断质量闸门 ──────────────────────────────────────────────
class _ShortCloneTTS:
    """假克隆 TTS：18 字文本只合成 1.0s 音频（事故形态）。"""

    def __init__(self, cfg):
        self.cfg = cfg

    async def synthesize(self, text, timeout_sec=45.0, emotion=None,
                         pre_colloquialized=False, **_kw):
        return SimpleNamespace(
            ok=True, audio_path=str(_ShortCloneTTS.audio), provider="avatar_clone",
            voice="lin_xiaoyu", latency_ms=900, duration_sec=1.0, error="",
            extra={})


@pytest.mark.asyncio
async def test_autosend_rejects_truncated_audio(monkeypatch, tmp_path):
    import src.ai.persona_voice as pv
    import src.inbox.voice_autosend as va

    fake_audio = tmp_path / "short.wav"
    fake_audio.write_bytes(b"RIFFxxxx")
    _ShortCloneTTS.audio = fake_audio

    monkeypatch.setattr(
        pv, "resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "avatar_clone"}, "emotion": None})
    monkeypatch.setattr(va, "preflight_voice_synth", lambda *a, **k: None)
    monkeypatch.setattr(va, "resolve_voice_autosend_cfg", lambda c: {})
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _ShortCloneTTS)

    path, meta = await va._synth_ogg(
        {}, "lin_xiaoyu", "不过你要是请我吃好的我还能再吃一顿哦",
        out_dir=str(tmp_path))
    assert path is None
    assert meta.get("truncation_rejected") is True
    assert va.pop_synth_failure_reason() == "truncation_rejected"
    assert not fake_audio.exists()          # 坏音清理干净


@pytest.mark.asyncio
async def test_autosend_gate_disabled_keeps_old_behavior(monkeypatch, tmp_path):
    import src.ai.persona_voice as pv
    import src.inbox.voice_autosend as va

    fake_audio = tmp_path / "short2.wav"
    fake_audio.write_bytes(b"RIFFxxxx")
    _ShortCloneTTS.audio = fake_audio

    monkeypatch.setattr(
        pv, "resolve_effective_voice_context",
        lambda *a, **k: {"voice_cfg": {"backend": "avatar_clone"}, "emotion": None})
    monkeypatch.setattr(va, "preflight_voice_synth", lambda *a, **k: None)
    monkeypatch.setattr(
        va, "resolve_voice_autosend_cfg",
        lambda c: {"quality_gate": {"enabled": False}})
    monkeypatch.setattr("src.ai.tts_pipeline.TTSPipeline", _ShortCloneTTS)
    # OGG 转码直接回传原路径（不真跑 ffmpeg）
    monkeypatch.setattr(
        "src.client.voice_sender.convert_to_ogg_opus",
        lambda p, delete_src=True: p)

    path, meta = await va._synth_ogg(
        {}, "lin_xiaoyu", "不过你要是请我吃好的我还能再吃一顿哦",
        out_dir=str(tmp_path))
    assert path is not None                 # 闸门关 → 旧行为照发
    assert not meta.get("truncation_rejected")


# ── 连发监测 ─────────────────────────────────────────────────────────────────
def test_voice_burst_guard_windows_and_cooldown():
    from src.client.voice_burst_guard import VoiceBurstGuard
    g = VoiceBurstGuard()
    t0 = 1000.0
    assert g.record(7, now=t0) is None
    assert g.record(7, now=t0 + 1) is None
    assert g.record(7, now=t0 + 2) is None          # 3 条=分条上限，正常
    breach = g.record(7, now=t0 + 3)                # 第 4 条 → 异常
    assert breach and breach["count"] == 4 and breach["chat_id"] == "7"
    assert g.record(7, now=t0 + 4) is None          # 本地冷却，不刷屏
    assert g.record(8, now=t0 + 4) is None          # 其他会话不受影响
    assert g.record(7, now=t0 + 400) is None        # 窗口滑出 → 重新计数
    assert g.total_bursts == 1


def test_note_voice_send_publishes_alert(monkeypatch):
    import src.client.voice_burst_guard as vbg
    from src.integrations.shared import event_bus as eb
    from src.monitoring.metrics_store import get_metrics_store

    published = []
    monkeypatch.setattr(
        eb, "get_event_bus",
        lambda: SimpleNamespace(publish=lambda t, d: published.append((t, d))))
    monkeypatch.setattr(vbg, "_SINGLETON", vbg.VoiceBurstGuard())

    base = get_metrics_store().snapshot().get("voice_bursts", 0)
    for _ in range(5):
        vbg.note_voice_send(99, {"burst_alert": {"window_sec": 60, "max_sends": 3}})
    assert published and published[0][0] == "voice_burst_alert"
    assert published[0][1]["chat_id"] == "99"
    assert published[0][1]["rate_key"] == "voice_burst:99"
    assert get_metrics_store().snapshot().get("voice_bursts", 0) == base + 1
    # 开关关 → 完全静默
    published.clear()
    vbg.note_voice_send(99, {"burst_alert": {"enabled": False}})
    assert published == []


def test_metrics_dedup_blocked_counter():
    from src.monitoring.metrics_store import get_metrics_store
    m = get_metrics_store()
    base = m.snapshot().get("dedup_blocked", 0)
    m.record_dedup_blocked()
    assert m.snapshot().get("dedup_blocked", 0) == base + 1
