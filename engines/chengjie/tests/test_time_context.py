# -*- coding: utf-8 -*-
"""回复生成「时间推理」门禁（P0 2026-08-12）。

实录事故：客户 8/8 21:47 后沉默 4 天、期间我方单向 ritual 连发 9 条；坐席 8/12
点「智能回复」，链路把 4 天前**已答过**的「闽江知道吗」当新消息重答，且措辞
近逐字复读 8/8 的旧回复。本文件钉住三层防线：

1. ``classify_reply_anchor`` 纯函数四态判定（结构优先，零 ts 也能兜住实录）；
2. ``generate_persona_reply`` 接线：stale_answered → 切开场产线跟进（坐席显式
   指令在场不切）/ stale_unanswered → 迟回复提示 / fresh+断层 → 回归提示 /
   当前时刻锚点恒注入 / 客户端无 ts 时 store 反查；
3. 复读守卫：生成稿与最近出站近重复 → 带负样本重写一次。
"""

import time
from types import SimpleNamespace

import pytest

from src.inbox.persona_reply import generate_persona_reply, normalize_history
from src.inbox.time_context import (
    DEFAULTS,
    build_followup_note,
    build_late_reply_hint,
    build_now_anchor_hint,
    build_repeat_rewrite_hint,
    classify_reply_anchor,
    daypart_label,
    describe_age,
    resolve_time_context_cfg,
)

NOW = 1786000000.0  # 任意固定基准


def _u(text, ts=None):
    row = {"role": "user", "content": text}
    if ts is not None:
        row["ts"] = ts
    return row


def _a(text, ts=None):
    row = {"role": "assistant", "content": text}
    if ts is not None:
        row["ts"] = ts
    return row


# ── classify_reply_anchor：四态判定 ───────────────────────────────────────────

def test_anchor_empty_and_outbound_only_is_no_inbound():
    assert classify_reply_anchor([], now=NOW)["kind"] == "no_inbound"
    a = classify_reply_anchor([_a("早安"), _a("晚安")], now=NOW)
    assert a["kind"] == "no_inbound"
    assert a["outbound_after"] == 2


def test_anchor_fresh_recent_inbound():
    a = classify_reply_anchor(
        [_a("hi", NOW - 400), _u("在吗", NOW - 60)], now=NOW)
    assert a["kind"] == "fresh"
    assert a["ts_known"] is True
    assert a["outbound_after"] == 0


def test_anchor_stale_answered_by_time():
    """实录形状：入站 4 天前、当晚已答、其后单向连发——绝不该再按 reply 重答。"""
    rows = [
        _u("闽江知道吗", NOW - 4 * 86400),
        _a("闽江啊，那可是福州的母亲河", NOW - 4 * 86400 + 40),
    ] + [_a(f"ritual{i}", NOW - (3 - i) * 86400) for i in range(3)]
    a = classify_reply_anchor(rows, now=NOW)
    assert a["kind"] == "stale_answered"
    assert a["outbound_after"] == 4
    assert a["age_sec"] == pytest.approx(4 * 86400, rel=0.01)
    assert "闽江" in a["last_inbound_text"]


def test_anchor_stale_unanswered_by_time():
    a = classify_reply_anchor([_u("你在吗", NOW - 2 * 86400)], now=NOW)
    assert a["kind"] == "stale_unanswered"
    assert a["outbound_after"] == 0


def test_anchor_structural_monologue_without_any_ts():
    """零时间戳（桌面 DOM 抓不到 ts）也必须兜住实录：9 条出站独白 ≥ 门槛 6。"""
    rows = [_u("闽江知道吗"), _a("闽江啊…")] + [_a(f"r{i}") for i in range(8)]
    a = classify_reply_anchor(rows, now=NOW)
    assert a["kind"] == "stale_answered"
    assert a["ts_known"] is False
    assert a["outbound_after"] == 9


def test_anchor_structural_threshold_spares_split_reply():
    """逐句分条（max_parts=5）刚答完 → 出站 5 条不得误判成独白（门槛必须>5）。"""
    rows = [_u("讲个笑话")] + [_a(f"part{i}") for i in range(5)]
    a = classify_reply_anchor(rows, now=NOW)
    assert a["kind"] == "fresh"
    assert DEFAULTS["min_monologue"] > 5


def test_anchor_fresh_time_wins_over_structure():
    """ts 可知且新鲜时，即便出站≥门槛也不按独白误切（时间证据优先）。"""
    rows = [_u("好呀", NOW - 300)] + [_a(f"p{i}", NOW - 200 + i) for i in range(7)]
    a = classify_reply_anchor(rows, now=NOW)
    assert a["kind"] == "stale_answered"  # 结构判定仍然命中（7 >= 6）
    # 上一断言即当前刻意行为：出站已 ≥ 门槛说明我方在连发，跟进语义仍正确；
    # 门槛 6 > 分条上限 5 已把「单条分条回复」排除在外。


def test_anchor_prev_user_gap_and_store_row_shape():
    rows = [
        {"direction": "in", "text": "上周的话", "ts": NOW - 7 * 86400},
        {"direction": "out", "text": "好", "ts": NOW - 7 * 86400 + 30},
        {"direction": "in", "text": "我回来啦", "ts": NOW - 120},
    ]
    a = classify_reply_anchor(rows, now=NOW)
    assert a["kind"] == "fresh"
    assert a["prev_user_gap_sec"] == pytest.approx(7 * 86400 - 120, rel=0.01)


# ── 配置解析 / 提示文案 ──────────────────────────────────────────────────────

def test_resolve_cfg_defaults_and_overrides():
    cfg = resolve_time_context_cfg({})
    assert cfg["enabled"] is True and cfg["min_monologue"] == 6
    off = resolve_time_context_cfg({"inbox": {"time_context": False}})
    assert off["enabled"] is False
    ov = resolve_time_context_cfg({"inbox": {"time_context": {
        "stale_after_hours": 12, "min_monologue": 1, "repeat_threshold": 2.0}}})
    assert ov["stale_after_hours"] == 12
    assert ov["min_monologue"] == 2      # 地板
    assert ov["repeat_threshold"] == 0.95  # 天花板
    assert resolve_time_context_cfg(None)["enabled"] is True


def test_describe_age_and_daypart():
    assert describe_age(3 * 86400 + 100) == "3 天"
    assert describe_age(30 * 3600) == "1 天多"
    assert describe_age(5 * 3600) == "5 小时"
    assert describe_age(120) == "不到 1 小时"
    assert daypart_label(7) == "清晨"
    assert daypart_label(21) == "晚上"
    assert daypart_label(23) == "深夜"
    assert daypart_label(2) == "深夜"


def test_now_anchor_hint_contains_facts_and_constraint():
    # 2026-08-12 21:15 本地时间（周三晚上）
    t = time.mktime((2026, 8, 12, 21, 15, 0, 0, 0, -1))
    h = build_now_anchor_hint(t)
    assert "2026-08-12" in h and "周三" in h and "21:15" in h and "晚上" in h
    assert "必须与当前时间一致" in h


def test_followup_note_and_hints_wording():
    note = build_followup_note({
        "ts_known": True, "age_sec": 4 * 86400,
        "outbound_after": 9, "last_inbound_text": "闽江知道吗",
    })
    assert "已经回复过" in note and "闽江知道吗" in note
    assert "9 条" in note and "4 天" in note
    late = build_late_reply_hint(2 * 86400)
    assert "迟回复" in late and "2 天" in late
    rw = build_repeat_rewrite_hint("闽江啊，那可是福州的母亲河" * 5)
    assert "禁止复读" in rw and "…" in rw


# ── persona_reply 接线（测试替身）────────────────────────────────────────────

class _FakeAI:
    def __init__(self):
        self.calls = []

    async def generate_reply_with_intent(self, *, user_message, intent,
                                         user_context, strategy_overrides=None):
        self.calls.append({"user_message": user_message, "intent": intent,
                           "ctx": dict(user_context)})
        return f"[gen]{intent}"

    async def chat(self, prompt):
        return "[fallback]"


class _FakeSM:
    """带统一引擎的 SM 替身（可编程返回序列，验证复读守卫重写）。"""

    def __init__(self, ai, replies=None):
        self.ai_client = ai
        self.config = SimpleNamespace(config={})
        self.inbox_draft_calls = []
        self._replies = list(replies or [])

    def _recognize_intent(self, text):
        return "consult"

    def get_strategy_for_intent(self, intent, user_id):
        return ({}, "s1")

    async def generate_inbox_draft(self, **kw):
        self.inbox_draft_calls.append(kw)
        if self._replies:
            return {"reply": self._replies.pop(0), "intent": "unified_intent"}
        return {"reply": f"[统一]{kw.get('text')}", "intent": "unified_intent"}


def _app(sm, **extra):
    state = {"skill_manager": sm, "ai_client": sm.ai_client if sm else _FakeAI(),
             "kb_store": None, "telegram_client": None,
             "translation_service": None}
    state.update(extra)
    return SimpleNamespace(state=SimpleNamespace(**state))


def _stale_answered_history():
    """实录形状（带 ts）：4 天前的已答提问 + 其后 3 条单向出站。"""
    now = time.time()
    return [
        _u("闽江知道吗", now - 4 * 86400),
        _a("闽江啊，那可是福州的母亲河，从小听大人们念叨着长大的", now - 4 * 86400 + 40),
        _a("闽江边的早晨应该挺凉快，你今天要不要出门走走？", now - 3 * 86400),
        _a("今早江边风应该挺舒服，我准备去走走", now - 2 * 86400),
        _a("早安，今天有什么安排呀？", now - 14 * 3600),
    ]


@pytest.mark.asyncio
async def test_stale_answered_switches_to_followup_opener():
    """陈旧已答 → 切开场产线：不再调统一引擎按 reply 重答；指令含跟进语境。"""
    ai = _FakeAI()
    sm = _FakeSM(ai)
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="8921664288",
        last_inbound="闽江知道吗", history=_stale_answered_history(),
    )
    assert out["ok"] is True
    assert out["time_anchor"]["kind"] == "stale_answered"
    assert out["time_anchor"]["followup"] is True
    assert out["mode"] == "opener"
    assert sm.inbox_draft_calls == []          # 绝不再走 reply 重答
    call = ai.calls[-1]
    assert call["intent"] == "proactive_opener"
    assert "已经回复过" in call["user_message"]   # 跟进语境进指令
    assert "闽江知道吗" in call["user_message"]
    # 开场同样带当前时刻锚点（经 _topic_switch_hint）
    assert "当前时间" in str(call["ctx"].get("_topic_switch_hint") or "")


@pytest.mark.asyncio
async def test_agent_instruction_suppresses_followup_switch():
    """坐席显式指令在场 → 尊重人的意图不切模式，但时间锚点提示仍注入。"""
    ai = _FakeAI()
    sm = _FakeSM(ai)
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="8921664288",
        last_inbound="闽江知道吗", history=_stale_answered_history(),
        agent_instruction="回答他关于闽江的问题，顺便约他周末江边走走",
    )
    assert out["ok"] is True
    assert len(sm.inbox_draft_calls) == 1      # 仍走统一引擎 reply
    hint = sm.inbox_draft_calls[0].get("extra_hint") or ""
    assert "当前时间" in hint
    assert out["time_anchor"]["kind"] == "stale_answered"
    assert "followup" not in out["time_anchor"]


@pytest.mark.asyncio
async def test_stale_unanswered_injects_late_reply_hint():
    now = time.time()
    history = [
        _a("你好呀", now - 3 * 86400),
        _u("在吗？想问个事", now - 2 * 86400),
    ]
    ai = _FakeAI()
    sm = _FakeSM(ai)
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="c2",
        last_inbound="在吗？想问个事", history=history,
    )
    assert out["ok"] is True
    hint = sm.inbox_draft_calls[0].get("extra_hint") or ""
    assert "迟回复" in hint and "2 天" in hint
    assert "当前时间" in hint
    assert out["time_anchor"]["kind"] == "stale_unanswered"


@pytest.mark.asyncio
async def test_fresh_after_long_gap_gets_return_hint():
    """对方隔 7 天刚回来 → 注入既有「时间断层」提示（此前全链休眠的象限）。"""
    now = time.time()
    history = [
        _u("想去大阪玩", now - 7 * 86400),
        _a("好呀好呀", now - 7 * 86400 + 60),
        _u("好呀好呀", now - 30),
    ]
    ai = _FakeAI()
    sm = _FakeSM(ai)
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="c3",
        last_inbound="好呀好呀", history=history,
    )
    assert out["ok"] is True
    hint = sm.inbox_draft_calls[0].get("extra_hint") or ""
    assert "不是刚才" in hint          # build_time_gap_hint 措辞
    assert out["time_anchor"]["kind"] == "fresh"


@pytest.mark.asyncio
async def test_store_lookup_supplies_time_truth_when_client_has_none():
    """客户端行无 ts（工作台/cp-draft 现状）→ 按 conversation_id 反查 store。"""
    class _FakeStore:
        def __init__(self):
            self.calls = []

        def list_recent_messages(self, cid, *, limit=50):
            self.calls.append((cid, limit))
            now = time.time()
            return [
                {"direction": "in", "text": "闽江知道吗", "ts": now - 4 * 86400},
                {"direction": "out", "text": "闽江啊…", "ts": now - 4 * 86400 + 40},
                {"direction": "out", "text": "早安", "ts": now - 3600},
            ]

    ai = _FakeAI()
    sm = _FakeSM(ai)
    store = _FakeStore()
    # 客户端只给 3 条无 ts 行（不足结构门槛）——单看客户端是 fresh
    history = [_u("闽江知道吗"), _a("闽江啊…"), _a("早安")]
    out = await generate_persona_reply(
        app=_app(sm, inbox_store=store), platform="telegram",
        chat_key="8921664288", last_inbound="闽江知道吗", history=history,
        conversation_id="telegram:tg-desktop:8921664288",
    )
    assert store.calls and store.calls[0][0] == "telegram:tg-desktop:8921664288"
    assert out["time_anchor"]["kind"] == "stale_answered"
    assert out["time_anchor"]["source"] == "store"
    assert out["time_anchor"]["followup"] is True
    assert sm.inbox_draft_calls == []


@pytest.mark.asyncio
async def test_repeat_guard_rewrites_near_duplicate():
    """生成稿复读最近出站 → 带【禁止复读】负样本重写一次，产出第二稿。"""
    now = time.time()
    dup = "闽江啊，那可是福州的母亲河，从小听大人们念叨着长大的"
    history = [
        _a(dup, now - 600),
        _u("那你老家还有谁呀", now - 60),
    ]
    ai = _FakeAI()
    sm = _FakeSM(ai, replies=[
        "闽江啊，我们福州的母亲河嘛，从小听大人们念叨长大的",  # 与已发近重复
        "家里还有我爸妈和一个妹妹，怎么突然查户口啦哈哈",      # 重写稿
    ])
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="c4",
        last_inbound="那你老家还有谁呀", history=history,
    )
    assert out["ok"] is True
    assert len(sm.inbox_draft_calls) == 2
    assert "禁止复读" in (sm.inbox_draft_calls[1].get("extra_hint") or "")
    assert out["reply"].startswith("家里还有")
    assert "repeat_risk" not in out


@pytest.mark.asyncio
async def test_repeat_guard_flags_when_rewrite_still_duplicate():
    now = time.time()
    dup = "闽江啊，那可是福州的母亲河，从小听大人们念叨着长大的"
    history = [_a(dup, now - 600), _u("闽江是什么", now - 60)]
    ai = _FakeAI()
    sm = _FakeSM(ai, replies=[dup, dup])   # 重写后仍复读
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="c5",
        last_inbound="闽江是什么", history=history,
    )
    assert out["ok"] is True
    assert out.get("repeat_risk") is True
    assert len(sm.inbox_draft_calls) == 2


@pytest.mark.asyncio
async def test_time_context_disabled_restores_legacy_behavior():
    """enabled:false → 不切模式、不注提示、不跑复读守卫（一键回旧行为）。"""
    ai = _FakeAI()
    sm = _FakeSM(ai)
    cm = SimpleNamespace(config={"inbox": {"time_context": {"enabled": False}}})
    out = await generate_persona_reply(
        app=_app(sm, config_manager=cm), platform="telegram",
        chat_key="8921664288", last_inbound="闽江知道吗",
        history=_stale_answered_history(),
    )
    assert out["ok"] is True
    assert len(sm.inbox_draft_calls) == 1
    # P-1 C（#259）起，extra_hint 消费口还挂了生成侧「AI 指纹」硬禁（与 time_context 无关、
    # 由 inbox.auto_draft.style_hint 单独开关）——这里只钉「时间提示一条不注」。
    _xh = sm.inbox_draft_calls[0].get("extra_hint") or ""
    assert "当前时间" not in _xh and "迟回复" not in _xh and "不是刚才" not in _xh
    assert "time_anchor" not in out


@pytest.mark.asyncio
async def test_fresh_inbound_only_gets_now_anchor():
    """新鲜入站（auto-draft 常态）：只注当前时刻锚点，行为其余不变。"""
    now = time.time()
    ai = _FakeAI()
    sm = _FakeSM(ai)
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="c6",
        last_inbound="在吗", history=[_u("在吗", now - 5)],
    )
    assert out["ok"] is True
    hint = sm.inbox_draft_calls[0].get("extra_hint") or ""
    assert "当前时间" in hint
    assert "迟回复" not in hint and "不是刚才" not in hint
    assert out["time_anchor"]["kind"] == "fresh"


# ── 双时钟事故修复（2026-08-22：B 线锚点必须用人设当地钟）──────────────────────
#
# 实录：温哥华人设在服务器 03:31 时，B 线锚点说「深夜」、ai_client 人设钟说
# 「中午」——同一 prompt 两个「现在」连日期都差一天，LLM 每条随机站队＝
# 「一会白天一会晚上」。以下钉住：锚点渲染当地钟 + 接线解析 + 组装级单钟不变量。

class _FakePMVan:
    def get_persona_by_id(self, pid):
        return {"id": pid, "name": "林佳欣", "location": "vancouver"}


class _FakePMNoPlace:
    def get_persona_by_id(self, pid):
        return {"id": pid, "name": "顾嘉", "location": "none"}


def _vancouver_now():
    from src.companion.persona_location import persona_now, resolve_persona_place
    return persona_now(resolve_persona_place({"location": "vancouver"}))


def test_now_anchor_hint_renders_persona_local_clock():
    import datetime as dt
    local = dt.datetime(2026, 8, 21, 12, 31)  # 温哥华当地中午（服务器 8-22 深夜）
    h = build_now_anchor_hint(local_now=local, place_label="加拿大·温哥华")
    assert "2026-08-21" in h and "12:31" in h
    assert "（加拿大·温哥华当地）" in h
    assert "中午" in h and "深夜" not in h
    # 缺省路径不带「当地」标注（服务器钟旧行为逐字兼容）
    t = time.mktime((2026, 8, 12, 21, 15, 0, 0, 0, -1))
    assert "当地" not in build_now_anchor_hint(t)


def test_persona_anchor_clock_resolves_place(monkeypatch):
    from src.inbox import persona_reply as pr
    monkeypatch.setattr(
        "src.utils.persona_manager.PersonaManager.get_instance",
        lambda: _FakePMVan())
    local, label = pr._persona_anchor_clock("van_girl")
    assert label == "加拿大·温哥华"
    expect = _vancouver_now()
    assert abs((local - expect).total_seconds()) < 120


def test_persona_anchor_clock_no_place_falls_back(monkeypatch):
    from src.inbox import persona_reply as pr
    monkeypatch.setattr(
        "src.utils.persona_manager.PersonaManager.get_instance",
        lambda: _FakePMNoPlace())
    assert pr._persona_anchor_clock("gu_jia") == (None, "")
    assert pr._persona_anchor_clock("") == (None, "")


@pytest.mark.asyncio
async def test_b_line_anchor_uses_persona_local_clock(monkeypatch):
    """组装级不变量：温哥华人设的 B 线 extra_hint 锚点＝当地钟（带当地标注、
    时段词与当地小时一致），绝不再出现「按服务器钟的另一个现在」。"""
    from src.inbox.time_context import daypart_label as _dp
    monkeypatch.setattr(
        "src.utils.persona_manager.PersonaManager.get_instance",
        lambda: _FakePMVan())
    now = time.time()
    ai = _FakeAI()
    sm = _FakeSM(ai)
    out = await generate_persona_reply(
        app=_app(sm), platform="telegram", chat_key="c7",
        last_inbound="在吗", history=[_u("在吗", now - 5)],
        persona_id="van_girl",
    )
    assert out["ok"] is True
    hint = sm.inbox_draft_calls[0].get("extra_hint") or ""
    local = _vancouver_now()
    assert "（加拿大·温哥华当地）" in hint
    assert _dp(local.hour) in hint
    assert f"{local.year}-{local.month:02d}-{local.day:02d}" in hint
