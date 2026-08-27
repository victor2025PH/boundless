# -*- coding: utf-8 -*-
"""未回消息墙 + 仪式素材化门禁（2026-08-18 仪式线）。

三件事的回归网：
1. ``trailing_unanswered_out_count``：末尾连续未回出站**条数**（含媒体/空文本行，
   与取文案的 trailing_unanswered_texts 刻意不同口径）。
2. 发送回路「未回消息墙」：末尾 ≥N 条出站未回 → 一切主动（含仪式）跳过、不记
   冷却；provider 异常 fail-open；live 配置热读可关。
3. ``build_ritual_opener`` 素材化：切入角轮换（早晚各自池、同日恒定）、记忆钩子
   与切入角共存、soft 档保持克制不带素材、轻话题佐料默认关/抽中顶替切入角且
   自带反编造与语言让行钉子。
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace as NS

import pytest

from src.integrations.companion_proactive import CompanionProactiveLoop
from src.utils.proactive_topic import (
    _RITUAL_MORNING_ANGLES,
    _RITUAL_NIGHT_ANGLES,
    ritual_angle,
)
from src.utils.proactive_variety import trailing_unanswered_out_count

NOW = 1_000_000.0


# ── trailing_unanswered_out_count 纯函数 ────────────────────────────────────

def _m(direction, text="x"):
    return {"direction": direction, "text": text}


def test_count_empty_and_all_inbound():
    assert trailing_unanswered_out_count([]) == 0
    assert trailing_unanswered_out_count(None) == 0
    assert trailing_unanswered_out_count([_m("in"), _m("in")]) == 0


def test_count_tail_outbound_only():
    msgs = [_m("in"), _m("out"), _m("out")]
    assert trailing_unanswered_out_count(msgs) == 2
    # 入站截断：更早的出站不算
    msgs2 = [_m("out"), _m("in"), _m("out")]
    assert trailing_unanswered_out_count(msgs2) == 1


def test_count_includes_media_empty_text_rows():
    # 与 trailing_unanswered_texts 的关键差异：空文本媒体行也占一条可见消息
    msgs = [_m("in"), _m("out", ""), _m("out", ""), _m("out", "[图片] 早")]
    assert trailing_unanswered_out_count(msgs) == 3


def test_count_skips_junk_rows():
    msgs = [_m("in"), _m("out"), "junk", _m("out")]
    assert trailing_unanswered_out_count(msgs) == 2


# ── ritual_angle 轮换 ────────────────────────────────────────────────────────

def test_ritual_angle_deterministic_and_in_pool():
    a1 = ritual_angle("morning", "tg:acc:u1", now=NOW)
    a2 = ritual_angle("morning", "tg:acc:u1", now=NOW)
    assert a1 == a2
    assert a1 in _RITUAL_MORNING_ANGLES
    assert ritual_angle("night", "tg:acc:u1", now=NOW) in _RITUAL_NIGHT_ANGLES


def test_ritual_angle_rotates_across_days_and_users():
    # 14 天内必然轮到不止一个切入角（crc 确定性，非随机 flaky）
    angles = {ritual_angle("morning", "u1", now=NOW + d * 86400.0)
              for d in range(14)}
    assert len(angles) > 1
    # 同一天不同用户不会全员同款
    users = {ritual_angle("morning", f"u{i}", now=NOW) for i in range(30)}
    assert len(users) > 1


def test_ritual_angle_invalid_slot_empty():
    assert ritual_angle("noon", "u1") == ""
    assert ritual_angle("", "u1") == ""


# ── 发送回路：未回消息墙 ─────────────────────────────────────────────────────

class _CD:
    def __init__(self):
        self.marks = {}

    def snapshot(self):
        return dict(self.marks)

    def mark(self, k, ts):
        self.marks[k] = ts

    def mark_send(self, k, ts, **_kw):
        self.marks[k] = ts


def _mk_loop(*, sent_box, wall_provider, wall_cfg=None, ritual=False,
             live_cfg_provider=None):
    convs = [{
        "conversation_id": "c1", "platform": "telegram", "account_id": "a1",
        "chat_key": "c1", "last_ts": NOW - 100 * 3600.0,
        "last_direction": "out", "memory_key": "c1", "stage": "",
        "intimacy": 50.0, "archived": False,
    }]

    def _opener(**_kw):
        return {"mode": "follow_up", "directive": "回访", "context_facts": []}

    async def _send(p):
        sent_box.append((p["conversation_id"], p.get("mode")))
        return True

    kw = {}
    rit_cd = _CD()
    if ritual:
        def _ritual_fn(_cs, _now):
            return [{
                "conversation_id": "c1", "platform": "telegram",
                "account_id": "a1", "chat_key": "c1",
                "mode": "ritual_morning", "directive": "早安", "fact": "",
                "context_facts": [], "scenario_id": "", "feature": "",
                "slot": "morning", "ritual_key": "c1:20260618:morning",
                "intimacy": 50.0,
            }]
        kw["ritual_fn"] = _ritual_fn
        kw["ritual_cooldown"] = rit_cd
    loop = CompanionProactiveLoop(
        conversations_provider=lambda: ([] if ritual else convs),
        opener_fn=_opener,
        send_fn=_send,
        cooldown_store=_CD(),
        min_silent_hours=24.0,
        cooldown_hours=0.0,
        max_per_tick=10,
        quiet_start_hour=0, quiet_end_hour=0,
        unanswered_wall_provider=wall_provider,
        wall_cfg=wall_cfg,
        live_cfg_provider=live_cfg_provider,
        now=lambda: NOW,
        **kw,
    )
    return loop, rit_cd


@pytest.mark.asyncio
async def test_wall_blocks_at_threshold():
    sent = []
    loop, _ = _mk_loop(sent_box=sent, wall_provider=lambda cid: 3,
                       wall_cfg={"enabled": True, "max_trailing": 3})
    res = await loop.run_once()
    assert sent == []
    assert res["sent"] == 0
    # 墙拦截计数进观测
    from src.companion.proactive_stats import metrics_snapshot
    assert metrics_snapshot()["wall_skips"] >= 1


@pytest.mark.asyncio
async def test_wall_below_threshold_passes():
    sent = []
    loop, _ = _mk_loop(sent_box=sent, wall_provider=lambda cid: 2,
                       wall_cfg={"enabled": True, "max_trailing": 3})
    await loop.run_once()
    assert sent == [("c1", "follow_up")]


@pytest.mark.asyncio
async def test_wall_provider_error_fails_open():
    sent = []

    def _boom(cid):
        raise RuntimeError("store down")

    loop, _ = _mk_loop(sent_box=sent, wall_provider=_boom,
                       wall_cfg={"enabled": True, "max_trailing": 3})
    await loop.run_once()
    assert sent == [("c1", "follow_up")]  # 防骚扰护栏绝不变成新的静默故障源


@pytest.mark.asyncio
async def test_wall_blocks_rituals_and_skips_cooldown_mark():
    sent = []
    loop, rit_cd = _mk_loop(sent_box=sent, wall_provider=lambda cid: 6,
                            wall_cfg={"enabled": True, "max_trailing": 6},
                            ritual=True)
    await loop.run_once()
    assert sent == []
    assert rit_cd.snapshot() == {}  # 没发就不烧当日档（对方开口后当天仍可问候）


@pytest.mark.asyncio
async def test_wall_disabled_or_missing_provider_old_behavior():
    sent = []
    loop, _ = _mk_loop(sent_box=sent, wall_provider=lambda cid: 99,
                       wall_cfg={"enabled": False, "max_trailing": 3})
    await loop.run_once()
    assert sent == [("c1", "follow_up")]
    sent2 = []
    loop2, _ = _mk_loop(sent_box=sent2, wall_provider=None,
                        wall_cfg={"enabled": True, "max_trailing": 3})
    await loop2.run_once()
    assert sent2 == [("c1", "follow_up")]


@pytest.mark.asyncio
async def test_wall_live_cfg_override_wins():
    sent = []
    loop, _ = _mk_loop(
        sent_box=sent, wall_provider=lambda cid: 9,
        wall_cfg={"enabled": True, "max_trailing": 3},
        live_cfg_provider=lambda: {
            "wall_cfg": {"enabled": False, "max_trailing": 3}})
    await loop.run_once()
    assert sent == [("c1", "follow_up")]  # overlay 热改可即时关墙


# ── build_ritual_opener 素材化 ───────────────────────────────────────────────

_SMcls = (
    __import__("src.skills.skill_manager", fromlist=["SkillManager"]).SkillManager
)


class _StubStore:
    def __init__(self, rows):
        self._rows = rows

    def list_rows(self, *, prefix="", limit=50, source=""):
        return list(self._rows)


class _SM:
    build_ritual_opener = _SMcls.build_ritual_opener
    _ritual_smalltalk_line = _SMcls._ritual_smalltalk_line
    _proactive_emotion_gate = _SMcls._proactive_emotion_gate
    _proactive_crisis_window_days = _SMcls._proactive_crisis_window_days

    def __init__(self, store=None, cfg=None):
        self._episodic_store = store or _StubStore([])
        self.logger = logging.getLogger("test_proactive_wall")
        self.config = NS(config=cfg or {})
        self._context_store = NS(get=lambda uid: {})
        self._crisis_latest = None
        self._crisis_store = None


def _fact(content, *, tier="stable", hits=3):
    return {"content": content, "source": "user_stated", "tier": tier,
            "hits": hits, "last_seen": time.time()}


def test_opener_carries_daily_angle():
    sm = _SM()
    out = sm.build_ritual_opener("morning", memory_key="u1", intimacy=50.0,
                                 contact_key="tg:acc:u1")
    assert out["mode"] == "ritual_morning"
    assert "开场切入" in out["directive"]
    assert any(a in out["directive"] for a in _RITUAL_MORNING_ANGLES)
    assert "不查岗" in out["directive"]


def test_opener_fact_rides_with_angle():
    sm = _SM(_StubStore([_fact("在备考")]))
    out = sm.build_ritual_opener("night", memory_key="u1", intimacy=50.0,
                                 contact_key="tg:acc:u1")
    assert out["fact"] == "在备考"
    assert "在备考" in out["directive"]  # 记忆钩子仍在
    assert "开场切入" in out["directive"]  # 切入角与钩子共存（结构性变化的核心）


def test_opener_soft_stays_restrained_no_angle():
    sm = _SM(_StubStore([_fact("在备考")]))
    out = sm.build_ritual_opener("morning", memory_key="u1", intimacy=50.0,
                                 contact_key="tg:acc:u1", last_emotion="焦虑")
    assert out["mode"] == "ritual_morning"
    assert "开场切入" not in out["directive"]  # 低落时克制陪伴，不塞素材
    assert "在备考" not in out["directive"]


def test_opener_angle_deterministic_same_day():
    sm = _SM()
    d1 = sm.build_ritual_opener("morning", memory_key="u1", intimacy=50.0,
                                contact_key="k1")["directive"]
    d2 = sm.build_ritual_opener("morning", memory_key="u1", intimacy=50.0,
                                contact_key="k1")["directive"]
    assert d1 == d2  # 同用户同日恒定（缓存/复现友好）


# ── 轻话题佐料（_ritual_smalltalk_line）─────────────────────────────────────

_ST_CFG = {
    "companion": {
        "daily_topics": {"enabled": True},
        "proactive_topic": {"daily_ritual": {"smalltalk_probability": 1.0}},
    },
}


def test_smalltalk_off_by_default_and_needs_topics():
    assert _SM(cfg={})._ritual_smalltalk_line("u1", "morning") == ""
    cfg_no_topics = {
        "companion": {"proactive_topic": {
            "daily_ritual": {"smalltalk_probability": 1.0}}},
    }
    assert _SM(cfg=cfg_no_topics)._ritual_smalltalk_line("u1", "morning") == ""


def test_smalltalk_line_carries_title_and_guards(monkeypatch):
    import src.companion.daily_topics as dt
    monkeypatch.setattr(
        dt, "pick_topics_for",
        lambda tastes, k=3, **kw: [{"title": "世界杯决赛今晚打响", "summary": ""}])
    monkeypatch.setattr(dt, "smalltalk_topic", lambda t: True)
    line = _SM(cfg=_ST_CFG)._ritual_smalltalk_line("u1", "night")
    assert "世界杯决赛今晚打响" in line
    assert "编造" in line          # 反编造钉子必须随行
    assert "忽略" in line          # 非中文会话让行钉子


def test_smalltalk_hard_news_filtered_out(monkeypatch):
    import src.companion.daily_topics as dt
    monkeypatch.setattr(
        dt, "pick_topics_for",
        lambda tastes, k=3, **kw: [{"title": "某地爆发冲突", "summary": ""}])
    monkeypatch.setattr(dt, "smalltalk_topic", lambda t: False)  # 重词否决
    assert _SM(cfg=_ST_CFG)._ritual_smalltalk_line("u1", "night") == ""


def test_smalltalk_replaces_angle_in_opener(monkeypatch):
    import src.companion.daily_topics as dt
    monkeypatch.setattr(
        dt, "pick_topics_for",
        lambda tastes, k=3, **kw: [{"title": "披荆斩棘新一季开播", "summary": ""}])
    monkeypatch.setattr(dt, "smalltalk_topic", lambda t: True)
    sm = _SM(cfg=_ST_CFG)
    out = sm.build_ritual_opener("night", memory_key="u1", intimacy=50.0,
                                 contact_key="tg:acc:u1")
    assert "披荆斩棘新一季开播" in out["directive"]
    assert "参考方向" not in out["directive"]  # 抽中轻话题＝顶替当日切入角，不叠料


def test_smalltalk_interest_words_flow_from_memory(monkeypatch):
    """兴趣个性化（2026-08-18）：记忆事实的内容词到达话题挑选器的 tastes 位
    ——聊过球的人优先轮到球赛话题（news_share 泛发 16.7% 的教训＝话题与人无关）。"""
    import src.companion.daily_topics as dt
    seen = {}

    def _spy_pick(tastes, k=3, **kw):
        seen["tastes"] = list(tastes or [])
        return [{"title": "CBA 季前赛开打", "summary": ""}]

    monkeypatch.setattr(dt, "pick_topics_for", _spy_pick)
    monkeypatch.setattr(dt, "smalltalk_topic", lambda t: True)
    sm = _SM(_StubStore([_fact("喜欢打篮球")]), cfg=_ST_CFG)
    out = sm.build_ritual_opener("night", memory_key="u1", intimacy=50.0,
                                 contact_key="tg:acc:u1")
    assert "CBA 季前赛开打" in out["directive"]
    # 「篮球」的 CJK bigram 必须在 tastes 里（与反编造守卫同一取词口径）
    assert "篮球" in (seen.get("tastes") or [])


def test_smalltalk_no_memory_keeps_plain_rotation(monkeypatch):
    """无记忆事实 → tastes=None（原轮换），行为零回退风险。"""
    import src.companion.daily_topics as dt
    seen = {}

    def _spy_pick(tastes, k=3, **kw):
        seen["tastes"] = tastes
        return []

    monkeypatch.setattr(dt, "pick_topics_for", _spy_pick)
    sm = _SM(cfg=_ST_CFG)
    sm.build_ritual_opener("night", memory_key="u1", intimacy=50.0,
                           contact_key="tg:acc:u1")
    assert seen.get("tastes") is None
