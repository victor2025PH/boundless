"""时间断层单一时钟 + 天数模糊化（2026-09-18 「57 天没聊」事故沉淀）。

实录：客户 5.4 天后回来，草稿链的【时间感知】读持久 user_context.last_message_time
（只有 A 线 process_message 写，B 线恒陈旧，停在 07-22）→ 提示写成「57 天」→ LLM
原样念出「57天没聊了」。同一稿里 inbound_enrich / persona_reply 锚点各算一套 gap。

钉住：
1. ``describe_age`` ≥14 天模糊化（两周多/一个多月/半年多），≤13 天保留整数天；
2. ``derive_turn_gap``：以 inbox 时间线推「本条 vs 对方上一条」间隔，无 ts → None；
3. ``classify_time_gap(gap_sec=…)`` 覆盖陈旧 last_ts；``build_emotional_context_block``
   有 ``_turn_gap_sec``（>0）就不读 last_message_time；0 哨兵不覆盖；
4. B 线 ``generate_inbox_draft`` 每稿以 inbox 时间线覆盖写 ``_turn_gap_sec`` /
   ``last_message_time``，情感块不再出现几十天；
5. ``build_time_gap_hint`` ≥14 天带「不要报精确天数」规则；同头【时间提示】不重复注入。
"""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.inbox.inbound_enrich import apply_inbound_enrichments, build_time_gap_hint
from src.inbox.time_context import derive_turn_gap, describe_age, humanize_long_span
from src.utils.emotional_context import build_emotional_context_block, classify_time_gap

DAY = 86400.0


# ── 1. 模糊化 ──────────────────────────────────────────────────────────────────

def test_describe_age_keeps_integer_days_under_two_weeks():
    assert describe_age(3 * DAY + 100) == "3 天"
    assert describe_age(10 * DAY) == "10 天"
    assert describe_age(13 * DAY + 3600) == "13 天"


def test_describe_age_fuzzes_from_two_weeks():
    assert describe_age(14 * DAY) == "两周多"
    assert describe_age(22 * DAY) == "三周左右"
    assert describe_age(57 * DAY) == "一个多月"        # 事故值
    assert describe_age(75 * DAY) == "两个多月"
    assert describe_age(100 * DAY) == "三四个月"
    assert describe_age(200 * DAY) == "半年多"
    assert describe_age(400 * DAY) == "一年多"
    assert humanize_long_span("x") == ""


def test_time_gap_hint_no_exact_number_for_long_gaps():
    h = build_time_gap_hint(57 * DAY)
    assert "一个多月" in h
    assert "57" not in h
    assert "不要报精确天数" in h
    h10 = build_time_gap_hint(10 * DAY)
    assert "10 天" in h10 and "不要报精确天数" not in h10


# ── 2. derive_turn_gap ─────────────────────────────────────────────────────────

def test_derive_gap_current_row_present_uses_its_ts_as_now():
    now = time.time()
    hist = [
        {"role": "user", "content": "早", "ts": now - 5.4 * DAY},
        {"role": "assistant", "content": "早呀", "ts": now - 5.4 * DAY + 30},
        {"role": "user", "content": "在吗", "ts": now - 2 * 3600},   # 本条（迟回复 2h）
    ]
    g = derive_turn_gap(hist, "在吗")
    assert g is not None
    assert abs(g - (5.4 * DAY - 2 * 3600)) < 5     # 以本条 ts 为「现在」，不算迟回复那 2h


def test_derive_gap_current_row_absent_uses_wall_clock():
    now = time.time()
    hist = [{"role": "user", "content": "早", "ts": now - 3 * DAY}]
    g = derive_turn_gap(hist, "又来了", now=now)
    assert g is not None and abs(g - 3 * DAY) < 1


def test_derive_gap_none_without_ts_or_prev_user():
    assert derive_turn_gap([{"role": "user", "content": "在吗"}], "在吗") is None
    assert derive_turn_gap([], "在吗") is None
    assert derive_turn_gap([{"role": "assistant", "content": "x", "ts": 1.0}], "在吗") is None
    # 只有本条自己带 ts，没有上一条用户消息 → None
    assert derive_turn_gap([{"role": "user", "content": "在吗", "ts": time.time()}], "在吗") is None


def test_derive_gap_accepts_store_rows():
    now = time.time()
    rows = [
        {"direction": "in", "text": "hi", "ts": now - 7 * DAY},
        {"direction": "out", "text": "hey", "ts": now - 7 * DAY + 10},
        {"direction": "in", "text": "back", "ts": now},
    ]
    g = derive_turn_gap(rows, "back")
    assert g is not None and abs(g - 7 * DAY) < 1


# ── 3. classify_time_gap / 情感块 ──────────────────────────────────────────────

def test_classify_gap_override_beats_stale_last_ts():
    stale = time.time() - 57 * DAY
    info = classify_time_gap(stale, gap_sec=5.4 * DAY)
    assert info["gap_label"] == "long_time"
    assert "5 天" in info["gap_hint"]
    assert "57" not in info["gap_hint"] and "57" not in info["opening_guidance"]


def test_classify_gap_long_time_is_fuzzy():
    info = classify_time_gap(time.time() - 57 * DAY)
    assert "一个多月" in info["gap_hint"]
    assert "57" not in info["opening_guidance"]
    assert "不要报精确天数" in info["opening_guidance"]


def test_emotional_block_prefers_turn_gap_over_stale_last_message_time():
    ctx = {"last_message_time": time.time() - 57 * DAY, "_turn_gap_sec": 5.4 * DAY}
    block = build_emotional_context_block(
        "在吗", ctx, "", enable_strategy=False, enable_wellbeing=False,
        enable_anti_sycophancy=False)
    assert "【时间感知】" in block
    assert "5 天" in block
    assert "57" not in block and "一个多月" not in block


def test_emotional_block_zero_sentinel_does_not_override():
    # A 线首聊写 _turn_gap_sec=0.0 → 不算已知间隔；此时 last_message_time 也没有 → 首次对话
    ctx = {"_turn_gap_sec": 0.0}
    block = build_emotional_context_block(
        "在吗", ctx, "", enable_strategy=False, enable_wellbeing=False,
        enable_anti_sycophancy=False)
    assert "第一次" in block


# ── 4. B 线：generate_inbox_draft 每稿覆盖时钟 ─────────────────────────────────

async def _make_cm(tmp_path: Path):
    cfg = {
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "intent": {"keywords": {}, "patterns": {}},
        "reply": {},
        "context_store": {"ttl_days": 30},
        "memory": {"enabled": False},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text("greeting: hi\n", encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text("channels: {}\n", encoding="utf-8")
    from src.utils.config_manager import ConfigManager
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    return cm


@pytest.mark.asyncio
async def test_inbox_draft_overrides_stale_clock_from_history_ts(tmp_path):
    from src.skills.skill_manager import SkillManager
    cm = await _make_cm(tmp_path)
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="好久不见呀")
    sm = SkillManager(cm, ai)
    now = time.time()
    hist = [
        {"role": "user", "content": "早安", "ts": now - 5.4 * DAY},
        {"role": "assistant", "content": "早呀", "ts": now - 5.4 * DAY + 20},
        {"role": "user", "content": "在吗", "ts": now - 60},
    ]
    out = await sm.generate_inbox_draft(
        text="在吗", chat_key="u1", platform="telegram", history=hist)
    assert out is not None
    ctx = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    assert abs(float(ctx["_turn_gap_sec"]) - (5.4 * DAY - 60)) < 5
    assert abs(float(ctx["last_message_time"]) - (now - 60)) < 1
    emo = str(ctx.get("_emotional_context_block") or "")
    assert "5 天" in emo
    assert "57" not in emo


@pytest.mark.asyncio
async def test_inbox_draft_stale_clock_replaced_even_when_context_persisted(tmp_path):
    from src.skills.skill_manager import SkillManager
    cm = await _make_cm(tmp_path)
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="嗨")
    sm = SkillManager(cm, ai)
    now = time.time()
    # 第一稿：写入上下文
    await sm.generate_inbox_draft(
        text="第一条", chat_key="u2", platform="telegram",
        history=[{"role": "user", "content": "第一条", "ts": now - 57 * DAY}])
    ctx1 = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    assert "_turn_gap_sec" not in ctx1          # 没有上一条 → 不猜
    # 第二稿（同一持久上下文）：inbox 说只隔了 3 天，不是 57 天
    hist = [
        {"role": "user", "content": "第一条", "ts": now - 3 * DAY - 100},
        {"role": "assistant", "content": "嗨", "ts": now - 3 * DAY - 90},
        {"role": "user", "content": "第二条", "ts": now - 100},
    ]
    await sm.generate_inbox_draft(
        text="第二条", chat_key="u2", platform="telegram", history=hist)
    ctx2 = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    assert abs(float(ctx2["_turn_gap_sec"]) - 3 * DAY) < 5
    emo = str(ctx2.get("_emotional_context_block") or "")
    assert "3 天" in emo and "57" not in emo


@pytest.mark.asyncio
async def test_inbox_draft_dedupes_time_hint_from_extra_hint(tmp_path):
    from src.skills.skill_manager import SkillManager
    cm = await _make_cm(tmp_path)
    ai = MagicMock()
    ai.generate_reply_with_intent = AsyncMock(return_value="嗨")
    sm = SkillManager(cm, ai)
    now = time.time()
    hist = [
        {"role": "user", "content": "早安", "ts": now - 5 * DAY},
        {"role": "assistant", "content": "早", "ts": now - 5 * DAY + 20},
        {"role": "user", "content": "在吗", "ts": now - 60},
    ]
    # persona_reply 锚点判定也会产同一条【时间提示】并经 extra_hint 送入
    dup_hint = "【当前时刻】周五 晚上\n" + build_time_gap_hint(5 * DAY - 60)
    await sm.generate_inbox_draft(
        text="在吗", chat_key="u3", platform="telegram", history=hist, extra_hint=dup_hint)
    ctx = ai.generate_reply_with_intent.await_args.kwargs["user_context"]
    tsh = str(ctx.get("_topic_switch_hint") or "")
    assert tsh.count("【时间提示——重要】") == 1
    assert "【当前时刻】" in tsh


# ── 5. inbound_enrich 消费同一时钟 ─────────────────────────────────────────────

def test_inbound_enrich_uses_preset_gap_and_derives_when_absent():
    now = time.time()
    hist = [
        {"role": "user", "content": "早", "ts": now - 3 * DAY},
        {"role": "user", "content": "在吗", "ts": now},
    ]
    uc = {}
    apply_inbound_enrichments(uc, text="在吗", history=hist)
    assert abs(uc["_turn_gap_sec"] - 3 * DAY) < 2
    assert "3 天" in uc.get("_topic_switch_hint", "")
    # 调用方已写（B 线覆盖写）→ 尊重
    uc2 = {"_turn_gap_sec": 8 * 3600}
    apply_inbound_enrichments(uc2, text="在吗", history=hist)
    assert uc2["_turn_gap_sec"] == 8 * 3600
    assert "8 小时" in uc2.get("_topic_switch_hint", "")
