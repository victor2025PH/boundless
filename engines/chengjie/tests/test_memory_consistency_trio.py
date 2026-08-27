# -*- coding: utf-8 -*-
"""实施64 P1-2 记忆一致性三件（B50 / B51 / B52，2026-08-23）。

- B50（`_299`）：年龄捕获进画像 + 已知画像硬注入 + 禁复问；
- B51（`_285`）：出稿反复读闸（回复 ≈ 对方上一条原话必拦）;
- B52（`_287`）：人设自述状态短期记忆 + 衔接指令 + proactive 同源消费。
"""
from __future__ import annotations

import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# ── B50：年龄捕获（heuristic + slot） ────────────────────────────────────────

def test_age_captured_from_first_person_statements():
    from src.utils.memory_heuristic import extract_heuristic_facts
    assert "用户年龄：38岁" in extract_heuristic_facts("我38岁，怎么了")
    assert "用户年龄：38岁" in extract_heuristic_facts("我今年38")  # noqa: RUF001
    assert "用户年龄：38岁" in extract_heuristic_facts("我都38岁了还要被问")
    assert "用户年龄：27岁" in extract_heuristic_facts("I'm 27 years old")


def test_age_not_captured_from_recall_or_third_person():
    from src.utils.memory_heuristic import extract_heuristic_facts
    for msg in ("我38岁的时候在上海", "我28岁那年结的婚",
                "他38岁", "你看起来像38岁", "等我38岁之前要买房"):
        facts = extract_heuristic_facts(msg)
        assert not any("年龄" in f for f in facts), (msg, facts)


def test_age_slot_extract_and_conflict():
    from src.utils.memory_slots import SLOT_AGE, extract_slot, slots_conflict
    a = extract_slot("用户年龄：38岁")
    b = extract_slot("我今年39岁")
    assert a == (SLOT_AGE, "38", 0)
    assert b == (SLOT_AGE, "39", 0)
    assert slots_conflict(a, b)  # 过生日 +1 → 新值顶旧值
    assert extract_slot("我住在上海")[0] != SLOT_AGE
    assert extract_slot("我8岁") is None      # <10 不采信（多为玩笑/转述）
    assert extract_slot("我120岁") is None


def test_known_profile_block_wired_in_both_chains_and_prompt():
    sm = (REPO / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert sm.count("self._inject_known_profile(user_context, _kp_key)") == 2, \
        "A/B 两链都必须注入已知画像块"
    assert sm.count("self._inject_self_state(user_context)") == 2
    ai = (REPO / "src" / "ai" / "ai_client.py").read_text(encoding="utf-8")
    assert "_known_profile_block" in ai and "_self_state_block" in ai, \
        "_build_context_prompt 必须消费两个新块"


# ── B51：反复读闸纯函数 ──────────────────────────────────────────────────────

def test_echo_exact_and_near_exact_blocked():
    from src.ai.reply_echo_guard import reply_echoes_inbound
    inbound = "你晚上吃饭了吗，晚上有什么安排"
    assert reply_echoes_inbound(inbound, inbound)
    assert reply_echoes_inbound("你晚上吃饭了吗，晚上有什么安排？", inbound)
    assert reply_echoes_inbound("你晚上吃饭了吗 晚上有什么安排~", inbound)
    assert reply_echoes_inbound(
        "Did you have dinner tonight? Any plans?",
        "did you have dinner tonight, any plans")


def test_echo_containment_blocked():
    from src.ai.reply_echo_guard import reply_echoes_inbound
    inbound = "以后给你介绍一个做你老公的人好不好"
    assert reply_echoes_inbound(f"{inbound}哈哈", inbound)


def test_normal_replies_not_blocked():
    from src.ai.reply_echo_guard import reply_echoes_inbound
    # 短呼应词雷同是常态，不判
    assert not reply_echoes_inbound("好呀", "好呀")
    assert not reply_echoes_inbound("哈哈哈", "哈哈哈")
    # 围绕对方话题展开的正常回复
    assert not reply_echoes_inbound(
        "还没吃呢，正想问你要不要一起？晚上我没什么安排",
        "你晚上吃饭了吗，晚上有什么安排")
    # 引用几个词再展开
    assert not reply_echoes_inbound(
        "介绍老公？哈哈你可别逗我了，我眼光可高了",
        "以后给你介绍一个做你老公的人好不好")


def test_echo_guard_wired_in_both_chains():
    ah = (REPO / "src" / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    assert "reply_echoes_inbound" in ah
    pa = (REPO / "src" / "integrations" / "protocol_autoreply.py"
          ).read_text(encoding="utf-8")
    assert "reply_echoes_inbound" in pa


# ── B52：自述状态捕获 / TTL / 注入 ──────────────────────────────────────────

def test_self_state_extract_positive():
    from src.companion.self_state import extract_self_state
    assert extract_self_state("聊了一天啦，我先去睡了，晚安")["state"] == "sleep"
    assert extract_self_state("我要去健身了，回来找你")["state"] == "gym"
    assert extract_self_state("我去洗澡啦")["state"] == "shower"
    assert extract_self_state("我先去忙工作了")["state"] == "work"


def test_self_state_extract_negative():
    from src.companion.self_state import extract_self_state
    for msg in ("你先睡吧，我再等等", "你快去睡觉啦", "我睡不着",
                "还不睡吗？", "今天不去健身了", "宝你去洗澡吧"):
        assert extract_self_state(msg) is None, msg


def test_self_state_record_and_ttl_window():
    from src.companion.self_state import (
        active_self_state, record_self_state, self_state_note,
    )
    ctx: dict = {}
    t0 = time.time()
    assert record_self_state(ctx, "我先去睡了，晚安", now=t0)
    cur = active_self_state(ctx, now=t0 + 3600)  # 1h 后仍在睡觉窗
    assert cur and cur["state"] == "sleep"
    note = self_state_note(ctx, now=t0 + 3600)
    assert "睡" in note and "衔接" in note
    # 窗过了（>9h）不再注入
    assert active_self_state(ctx, now=t0 + 10 * 3600) is None
    assert self_state_note(ctx, now=t0 + 10 * 3600) == ""


def test_self_state_log_bounded():
    from src.companion.self_state import LOG_KEY, record_self_state
    ctx: dict = {}
    for i in range(6):
        record_self_state(ctx, "我先去睡了", now=1000.0 + i)
    assert len(ctx[LOG_KEY]) == 3


def test_self_state_capture_wired_in_update_after_reply():
    sm = (REPO / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8")
    assert "record_self_state(user_context, reply)" in sm


def test_proactive_prompt_consumes_self_state():
    from src.utils.proactive_prompt import build_proactive_prompt
    plan = {"directive": "问候一下", "mode": "gentle_checkin"}
    p = build_proactive_prompt(
        "小雨", plan, self_state_note="【你自己最近说过的状态】你 30 分钟前亲口说过要去睡觉。")
    assert "你自己最近说过的状态" in p
    p2 = build_proactive_prompt("小雨", plan)
    assert "你自己最近说过的状态" not in p2
    pt = (REPO / "src" / "companion" / "proactive_topic.py").read_text(encoding="utf-8")
    assert "self_state_note=_ss_note" in pt
