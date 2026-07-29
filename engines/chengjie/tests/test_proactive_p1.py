# -*- coding: utf-8 -*-
"""主动触达 P1 门禁（2026-07-29）。

锁六条不变量（承接 P0「好久没联系」连发事故）：
  1. life_beat 死路径修复：gentle_checkin 时先试生活分享/天气，不再被恒非空 mode 遮蔽。
  2. checkin_angle：同用户同天恒定、隔天换切入点。
  3. never_replied：0 入站 + streak≥N → 出圈；对方一开口即失效。
  4. opt-out：高置信退订识别 + 对方回归自动解除。
  5. media gate 带原因：voice/photo 跳过可归因（修「配置 50% 实际 6%」盲区）。
  6. stats：mode/skip/variety/optout 进程级计数可导出。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.companion.proactive_stats import (
    metrics_snapshot,
    record_media_skip,
    record_optout_mute,
    record_sent_mode,
    record_variety_block,
)
from src.companion.proactive_topic import (
    photo_share_verdict,
    voice_gate_verdict,
)
from src.integrations.companion_proactive import plan_proactive_sends
from src.utils.proactive_optout import detect_optout, optout_active
from src.utils.proactive_pacing import (
    never_replied_exhausted,
    parse_no_reply_backoff_cfg,
)
from src.utils.proactive_topic import (
    MODE_GENTLE_CHECKIN,
    checkin_angle,
    select_proactive_topic,
)


def _noon_today() -> float:
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 12, 0, 0,
                        lt.tm_wday, lt.tm_yday, -1))


# ── 1. life_beat 死路径 ─────────────────────────────────────────────────

def _fake_skill_manager(**overrides):
    """轻量桩：只绑 build_proactive_opener 需要的属性（避开 SkillManager 构造）。"""
    from src.skills.skill_manager import SkillManager

    sm = SkillManager.__new__(SkillManager)
    store = MagicMock()
    store.list_rows.return_value = []  # 无记忆 → gentle_checkin
    defaults = dict(
        _episodic_store=store,
        config=SimpleNamespace(config={"companion": {}}),
        _proactive_emotion_gate=lambda *a, **k: "",
        _story_cfg=lambda: {},
        _proactive_story_invite=lambda *a, **k: None,
        _proactive_story_teaser=lambda *a, **k: None,
        _life_beat_opener=lambda *a, **k: {},
        _weather_opener=lambda *a, **k: {},
    )
    defaults.update(overrides)
    for k, v in defaults.items():
        object.__setattr__(sm, k, v)
    return sm


def test_gentle_checkin_upgrades_to_life_share():
    """select 在沉默达标时必返回 gentle_checkin；P1 必须升级到 life_share。"""
    sm = _fake_skill_manager(
        _life_beat_opener=lambda *a, **k: {
            "mode": "life_share", "fact": "今天路过花店",
            "directive": "分享路过花店",
        },
    )
    out = sm.build_proactive_opener(
        "u1", silent_hours=50.0, min_silent_hours=24.0, contact_key="u1")
    assert out.get("mode") == "life_share"
    assert out.get("gap_bucket")  # 从 checkin 透传


def test_gentle_checkin_falls_to_weather_then_checkin():
    sm = _fake_skill_manager(
        _weather_opener=lambda *a, **k: {
            "mode": "weather_hook", "fact": "暴雨", "directive": "说说暴雨",
        },
    )
    out = sm.build_proactive_opener(
        "u1", silent_hours=50.0, min_silent_hours=24.0, contact_key="u1")
    assert out.get("mode") == "weather_hook"

    object.__setattr__(sm, "_weather_opener", lambda *a, **k: {})
    out2 = sm.build_proactive_opener(
        "u1", silent_hours=50.0, min_silent_hours=24.0, contact_key="u1")
    assert out2.get("mode") == MODE_GENTLE_CHECKIN


# ── 2. checkin_angle ────────────────────────────────────────────────────

def test_checkin_angle_stable_same_day():
    t0 = _noon_today()
    a = checkin_angle("alice", t0)
    b = checkin_angle("alice", t0 + 3600)
    assert a and a == b  # 同天恒定、非空
    # 扫一周应出现 ≥2 个不同切入点（8 档 crc32 轮换，连续 7 天撞全同极不可能）
    week = {checkin_angle("alice", t0 + d * 86400) for d in range(7)}
    assert len(week) >= 2


def test_select_attaches_angle_when_variety_key():
    sel = select_proactive_topic(
        [], silent_hours=50.0, min_silent_hours=24.0,
        variety_key="cid:1", now=_noon_today())
    assert sel["mode"] == MODE_GENTLE_CHECKIN
    assert "切入点参考" in sel["directive"]


def test_select_no_angle_without_variety_key():
    sel = select_proactive_topic(
        [], silent_hours=50.0, min_silent_hours=24.0, now=_noon_today())
    assert "切入点参考" not in sel["directive"]


# ── 3. never_replied ────────────────────────────────────────────────────

def test_never_replied_exhausted_semantics():
    cfg = parse_no_reply_backoff_cfg({})
    assert cfg["stop_after_never_replied"] == 2
    assert never_replied_exhausted(2, 0.0, cfg) is True
    assert never_replied_exhausted(1, 0.0, cfg) is False
    assert never_replied_exhausted(9, 100.0, cfg) is False  # 有入站 → 不触发
    off = dict(cfg, stop_after_never_replied=0)
    assert never_replied_exhausted(9, 0.0, off) is False


def test_planner_never_replied_drops_candidate():
    now = _noon_today()
    bk = parse_no_reply_backoff_cfg({})
    # streak=2、从未入站、冷却窗早过 → P1 出圈
    cd = {"tg:a:1": {"ts": now - 10000 * 3600, "streak": 2, "last_text": "x"}}
    conv = {
        "conversation_id": "tg:a:1", "platform": "telegram", "account_id": "a",
        "chat_key": "1", "last_ts": now - 100 * 3600.0,
        "last_direction": "out", "archived": False, "memory_key": "1",
        "intimacy": 0.0, "last_in_ts": 0.0,
    }

    def _op(**kw):
        return {"mode": "gentle_checkin", "directive": "hi", "fact": ""}

    plans = plan_proactive_sends(
        [conv], cooldown_map=cd, opener_fn=_op, now=now,
        min_silent_hours=24, cooldown_hours=48, max_per_tick=5,
        quiet_start_hour=23, quiet_end_hour=8, backoff_cfg=bk)
    assert plans == []
    # 对方开口 → 恢复
    conv2 = dict(conv, last_in_ts=now - 10 * 3600)
    plans2 = plan_proactive_sends(
        [conv2], cooldown_map=cd, opener_fn=_op, now=now,
        min_silent_hours=24, cooldown_hours=48, max_per_tick=5,
        quiet_start_hour=23, quiet_end_hour=8, backoff_cfg=bk)
    assert len(plans2) == 1


# ── 4. opt-out ──────────────────────────────────────────────────────────

def test_detect_optout_zh_en_and_negatives():
    assert "别再发了" in detect_optout(["行吧", "别再发了谢谢"])
    assert detect_optout(["please stop messaging me"])
    assert detect_optout(["leave me alone"])
    # 情绪宣泄/打情骂俏不误伤
    assert detect_optout(["烦死了", "闭嘴", "滚"]) == ""
    assert detect_optout(["不要发这张图"]) == ""
    assert detect_optout([]) == ""


def test_optout_active_expiry_and_user_return():
    now = 1_000_000.0
    entry = {"ts": now - 100, "until": now + 86400, "hit": "别再发了"}
    assert optout_active(entry, now=now, last_in_ts=0) is True
    assert optout_active(entry, now=now + 90000, last_in_ts=0) is False  # 过期
    # 开口早于静默记录 → 仍生效；开口晚于静默 → 对方回来了，自动解除
    assert optout_active(entry, now=now, last_in_ts=now - 200) is True
    assert optout_active(entry, now=now, last_in_ts=now - 10) is False


# ── 5. media gate 归因 ──────────────────────────────────────────────────

def test_voice_gate_verdict_reasons():
    assert voice_gate_verdict({"enabled": False}, "你好呀", 0.1)[1] == "disabled"
    assert voice_gate_verdict(
        {"enabled": True, "min_chars": 4, "max_chars": 80, "probability": 0.5},
        "嗯", 0.0)[1] == "length"
    assert voice_gate_verdict(
        {"enabled": True, "min_chars": 4, "max_chars": 80, "probability": 0.5},
        "你好呀朋友", 0.9)[1] == "probability"
    ok, why = voice_gate_verdict(
        {"enabled": True, "min_chars": 4, "max_chars": 80, "probability": 0.5},
        "你好呀朋友", 0.1)
    assert ok and why == "ok"


def test_photo_share_verdict_reasons():
    cfg = {"enabled": True, "min_intimacy": 20, "probability": 0.25}
    assert photo_share_verdict(
        cfg, mode="story_teaser", intimacy=50, rand01=0.0)[1] == "mode"
    assert photo_share_verdict(
        cfg, mode="gentle_checkin", intimacy=5, rand01=0.0)[1] == "min_intimacy"
    assert photo_share_verdict(
        cfg, mode="gentle_checkin", intimacy=50, rand01=0.9)[1] == "probability"
    ok, why = photo_share_verdict(
        cfg, mode="follow_up", intimacy=50, rand01=0.1)
    assert ok and why == "ok"


# ── 6. stats ────────────────────────────────────────────────────────────

def test_proactive_stats_p1_counters():
    before = metrics_snapshot()
    record_sent_mode("life_share")
    record_sent_mode("gentle_checkin")
    record_media_skip("voice", "probability")
    record_media_skip("photo", "min_intimacy")
    record_variety_block()
    record_optout_mute()
    snap = metrics_snapshot()
    assert snap["sent_modes"].get("life_share", 0) >= 1
    assert snap["media_skips"].get("voice", {}).get("probability", 0) >= 1
    assert snap["media_skips"].get("photo", {}).get("min_intimacy", 0) >= 1
    assert snap["variety_blocks"] >= before.get("variety_blocks", 0) + 1
    assert snap["optout_mutes"] >= before.get("optout_mutes", 0) + 1
