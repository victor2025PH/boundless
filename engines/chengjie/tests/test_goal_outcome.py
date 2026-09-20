# -*- coding: utf-8 -*-
"""自定义目标「像是达成了」信号检测门禁（P1 2026-08-29）。

哲学＝宁漏勿误：误报一次坐席就不再信这个提示，负例（日期/价格/英文词内
子串/at 简写）比正例更重要。落库入口只提示不结算——确认权在人。
"""

from __future__ import annotations

import time

import pytest

from src.companion.goals.outcome import (
    detect_contact,
    detect_outcome,
    maybe_outcome_signal,
    note_outcome_kind,
)
from src.companion.goals.store import GoalStore


# ── note 分类：坐席写的推进方向在要什么 ─────────────────────────────────────

@pytest.mark.parametrize("note", [
    "这轮聊完要到TA的微信",
    "引导对方留下联系方式",
    "拿到客户电话",
    "让TA留个邮箱",
    "get their phone number",
    "ask for WhatsApp contact",
])
def test_note_contact_kind(note):
    assert note_outcome_kind(note) == "contact"


@pytest.mark.parametrize("note", [
    "",
    None,
    "引导TA把我们推荐给一位同行",
    "约TA试用国际版",
    "别错过 deadline，今天收口",     # line 词内子串不得误判
])
def test_note_non_contact_kind(note):
    assert note_outcome_kind(note) == ""


# ── 联系方式检测：正例 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expect", [
    ("写邮箱 someone@test.com 找我", "someone@test.com"),
    ("我手机 13812345678 随时打", "13812345678"),
    ("call me +65 91234567", "+65 91234567"),
    ("加我 telegram @john_doe88", "@john_doe88"),
    ("我微信是 abc12345", "abc12345"),
    ("微信号chatx2026 加我", "chatx2026"),
    ("加我微信 chatx2026", "chatx2026"),
    ("line id: sunny123", "sunny123"),
    ("whatsapp号：mywa888", "mywa888"),
])
def test_detect_contact_hits(text, expect):
    assert detect_contact(text) == expect


# ── 联系方式检测：负例（误报一次提示就报废） ────────────────────────────────

@pytest.mark.parametrize("text", [
    "",
    None,
    "2026-08-29 之前给你答复",
    "价格是 1999 元",
    "订单号 123456789",
    "the deadline tomorrow is fine",   # deadline 词内 line 不触发
    "Line tomorrow works for me",      # 裸空格不当分隔符
    "meet me @noon",                   # at 简写（<5 位 handle）
    "微信支付也可以",                    # 键词后无 ASCII id
    "whatsapp group 里聊",              # 停用词不当 id
])
def test_detect_contact_rejects(text):
    assert detect_contact(text) == ""


def test_detect_outcome_kind_gate():
    assert detect_outcome("contact", "我微信是 abc12345") == "abc12345"
    assert detect_outcome("", "我微信是 abc12345") == ""
    # appointment 走影子轨检测器：应允词缺席不算
    assert detect_outcome("appointment", "明天下午三点") == ""


# ── 影子轨（P2）：约时间 / 付款——只记事件不出提示 ────────────────────────────

def test_note_shadow_kinds():
    from src.companion.goals.outcome import note_shadow_kinds

    assert note_shadow_kinds("约TA试用国际版") == ("appointment",)
    assert note_shadow_kinds("引导TA下单付款") == ("payment",)
    assert note_shadow_kinds("约个演示然后引导付款") == (
        "appointment", "payment")
    assert note_shadow_kinds("聊聊近况拉近关系") == ()
    assert note_shadow_kinds("") == ()


@pytest.mark.parametrize("text,hit", [
    ("好的，明天下午3点可以", True),
    ("行啊，周三晚上聊", True),
    ("sure, tomorrow at 3 pm works", True),
    ("明天下午三点", False),          # 只有时间没有应允
    ("好的没问题", False),            # 只有应允没有时间
    ("", False),
])
def test_detect_appointment(text, hit):
    from src.companion.goals.outcome import detect_appointment

    assert bool(detect_appointment(text)) is hit


@pytest.mark.parametrize("text,hit", [
    ("我已付款成功了", True),
    ("刚转了，你查一下", True),
    ("just ordered from the site", True),
    ("我想买一个", False),            # 意向≠完成
    ("多少钱？", False),
    ("", False),
])
def test_detect_payment(text, hit):
    from src.companion.goals.outcome import detect_payment

    assert bool(detect_payment(text)) is hit


def test_shadow_records_event_only_once_per_kind(store):
    from src.companion.goals.outcome import maybe_outcome_shadow

    now = time.time()
    g = store.create_goal(
        conversation_id="telegram:a1:sh1", platform="telegram",
        account_id="a1", chat_key="sh1", template="custom",
        params={"note": "约TA试用并引导付款"}, deadline_days=1)
    got = maybe_outcome_shadow(store, g, "好的，明天下午3点；我已付款成功",
                               now=now)
    assert set(got) == {"appointment", "payment"}
    assert store.count_events_since(
        g["goal_id"], "outcome_shadow_appointment", 0) == 1
    assert store.count_events_since(
        g["goal_id"], "outcome_shadow_payment", 0) == 1
    # params 不写（影子轨零 UI 面）；再次命中不重复记
    assert "outcome_signal" not in (
        store.get_goal(g["goal_id"])["params"] or {})
    assert maybe_outcome_shadow(store, g, "好的明天下午再聊", now=now) == ()
    assert store.count_events_since(
        g["goal_id"], "outcome_shadow_appointment", 0) == 1


def test_shadow_gates(store):
    from src.companion.goals.outcome import maybe_outcome_shadow

    now = time.time()
    # note 与影子类型无关不判
    g1 = _custom_goal(store)
    assert maybe_outcome_shadow(store, g1, "好的明天下午3点", now=now) == ()
    # 非 custom 不判；坏 store 不抛
    g2 = store.create_goal(
        conversation_id="telegram:a1:sh2", platform="telegram",
        account_id="a1", chat_key="sh2", template="conversion_unlock",
        params={"note": "约TA试用"}, deadline_days=14)
    assert maybe_outcome_shadow(store, g2, "好的明天下午3点", now=now) == ()
    assert maybe_outcome_shadow(object(), g1, "好的明天3点", now=now) == ()


# ── maybe_outcome_signal：落库 + 幂等 + 门控 ────────────────────────────────

@pytest.fixture()
def store():
    s = GoalStore(":memory:")
    yield s
    s.close()


def _custom_goal(store, note="这轮聊完要到TA的微信", **kw):
    g = store.create_goal(
        conversation_id="telegram:a1:ow1", platform="telegram",
        account_id="a1", chat_key="ow1", template="custom",
        params={"pace": "session", "note": note},
        deadline_days=60 / 1440.0, **kw)
    assert g is not None
    return g


def test_signal_written_once_and_idempotent(store):
    now = time.time()
    g = _custom_goal(store)
    assert maybe_outcome_signal(store, g, "好呀，我微信是 abc12345", now=now)
    sig = g["params"]["outcome_signal"]           # 入参就地更新（同轮可见）
    assert sig["kind"] == "contact" and sig["v"] == "abc12345"
    row = store.get_goal(g["goal_id"])
    assert row["params"]["outcome_signal"]["v"] == "abc12345"
    assert store.count_events_since(g["goal_id"], "outcome_signal", 0) == 1
    # 幂等：再次命中不重写不重复记事件
    assert not maybe_outcome_signal(store, row, "还有邮箱 a@b.co", now=now)
    assert store.count_events_since(g["goal_id"], "outcome_signal", 0) == 1


def test_signal_gates(store):
    now = time.time()
    # 非 custom 模板不判
    g1 = store.create_goal(
        conversation_id="telegram:a1:ow2", platform="telegram",
        account_id="a1", chat_key="ow2", template="conversion_unlock",
        params={"note": "留联系方式"}, deadline_days=14)
    assert not maybe_outcome_signal(store, g1, "我微信是 abc12345", now=now)
    # note 不在要联系方式不判
    g2 = store.create_goal(
        conversation_id="telegram:a1:ow3", platform="telegram",
        account_id="a1", chat_key="ow3", template="custom",
        params={"note": "约TA试用"}, deadline_days=14)
    assert not maybe_outcome_signal(store, g2, "我微信是 abc12345", now=now)
    # 文本无信号不判；终态目标不判
    g3 = _custom_goal(store)
    assert not maybe_outcome_signal(store, g3, "在忙，晚点说", now=now)
    store.update_goal_fields(g3["goal_id"], status="done", done_at=now)
    done = store.get_goal(g3["goal_id"])
    assert not maybe_outcome_signal(store, done, "我微信是 abc12345", now=now)
    # 坏 store 不抛
    assert not maybe_outcome_signal(object(), _custom_goal(store, note="留微信"),
                                    "我微信是 abc12345", now=now)


def test_build_block_wiring_records_signal(monkeypatch):
    """接线面：build_block_for_chat 带 inbound_text 时顺手检测（observe 档
    早退之前）——params.outcome_signal 当轮写库，卡片下一次取数即见。"""
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc
    from src.companion.goals.service import (
        build_block_for_chat,
        get_configured_store,
    )
    from src.companion.goals.store import reset_goal_store

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    try:
        class _Cfg:
            config = {"companion": {"goals": {
                "enabled": True, "db_path": ":memory:"}}}
            config_path = None

        now = time.time()
        store = get_configured_store(_Cfg.config, None)
        g = store.create_goal(
            conversation_id="telegram:a1:ow9", platform="telegram",
            account_id="a1", chat_key="ow9", template="custom",
            autonomy="observe",                     # 观察档也要能记信号
            params={"pace": "session", "note": "这轮要到TA的微信"},
            deadline_days=60 / 1440.0, now=now)
        out = build_block_for_chat(
            _Cfg, platform="telegram", chat_key="ow9", account_id="a1",
            conversation_id="telegram:a1:ow9",
            inbound_text="好呀 我微信是 abc12345", now=now)
        assert out is None                          # observe 不注入
        row = store.get_goal(g["goal_id"])
        assert row["params"]["outcome_signal"]["v"] == "abc12345"
    finally:
        reset_goal_store()
