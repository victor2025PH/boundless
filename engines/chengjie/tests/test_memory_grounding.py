"""记忆接地护栏 + 时间断层/语言事实提示 单测（2026-07-13「精神错乱」事故回归网）。

事故链：AI 臆测被抽成"用户事实"入库 → 假记忆注入 prompt → AI 复读幻觉；
10 天前的历史轮次被当"刚才"；历史里的旧日语轮次诱发"你换日文了"幻觉。
三道防线各自可测，全离线。
"""
from __future__ import annotations

import pytest

from src.ai.memory_grounding import fact_grounded_in_user_msg, filter_grounded_facts
from src.inbox.inbound_enrich import (
    build_language_anchor_hint,
    build_time_gap_hint,
)


# ── 接地护栏（真实事故案例）─────────────────────────────────────────────────
def test_hallucinated_facts_rejected_real_incident():
    """AI 臆测内容（用户从没说过）→ 拒。全部来自 2026-07-13 真实污染记录。"""
    assert not fact_grounded_in_user_msg("用户想去大阪玩", "好呀好呀")
    assert not fact_grounded_in_user_msg("用户明天不用上班", "好呀好呀")
    assert not fact_grounded_in_user_msg(
        "用户深夜还在线，可能明天休息", "你在干嘛呢 干嘛呢")


def test_grounded_facts_kept_real_history():
    """锚定用户原话的合法事实 → 留。来自同一用户的真实历史记忆。"""
    assert fact_grounded_in_user_msg("用户被虫咬了", "我被咬了？")
    assert fact_grounded_in_user_msg("用户询问风油精的使用方法", "知道怎么使用吗")
    assert fact_grounded_in_user_msg("用户自称：你爸", "我是你爸")
    assert fact_grounded_in_user_msg("用户叫 Mike", "Hi, I'm Mike")
    assert fact_grounded_in_user_msg("用户 25 岁", "我25了")


def test_grounding_edge_cases():
    # 取不出内容 token → 保守放行
    assert fact_grounded_in_user_msg("用户：好", "嗯")
    # 空输入不炸
    assert fact_grounded_in_user_msg("", "") is True
    kept, dropped = filter_grounded_facts(
        ["用户被虫咬了", "用户想去大阪玩"], "我被咬了？")
    assert kept == ["用户被虫咬了"]
    assert dropped == ["用户想去大阪玩"]
    assert filter_grounded_facts([], "x") == ([], [])


def test_ai_client_ground_wrapper():
    """AIClient._ground_extracted_facts 接线：过滤 + 异常放行。"""
    from src.ai.ai_client import AIClient

    class _Stub:
        logger = __import__("logging").getLogger("t")
        _ground_extracted_facts = AIClient._ground_extracted_facts

    s = _Stub()
    out = s._ground_extracted_facts(
        ["用户被虫咬了", "用户想去大阪玩"], "我被咬了？")
    assert out == ["用户被虫咬了"]
    assert s._ground_extracted_facts([], "x") == []


# ── 时间断层提示 ─────────────────────────────────────────────────────────────
def test_time_gap_hint_thresholds():
    assert build_time_gap_hint(0) == ""
    assert build_time_gap_hint(3600 * 2) == ""          # 2h 正常连聊 → 无
    h8 = build_time_gap_hint(3600 * 8)
    assert "8 小时" in h8 and "不是刚才" in h8
    h30 = build_time_gap_hint(3600 * 30)
    assert "1 天多" in h30
    h10d = build_time_gap_hint(86400 * 10)
    assert "10 天" in h10d and "亲口说过" in h10d
    assert build_time_gap_hint("bogus") == ""            # 脏输入不炸


# ── 语言事实钉子 ─────────────────────────────────────────────────────────────
def test_language_anchor_fires_on_risky_history():
    """本条中文 + 历史含日语轮次/语言点评 → 注入钉子（真实事故语境）。"""
    hist = [
        {"role": "user", "content": "何してるの？"},
        {"role": "assistant", "content": "あ、日本語に戻したね 😊"},
        {"role": "user", "content": "好呀"},
        {"role": "assistant", "content": "嗯，涂上去试试～ 突然变成日语了呢！"},
    ]
    hint = build_language_anchor_hint(hist, current_text="好呀好呀")
    assert "本条消息用的是中文" in hint
    assert "换日文" in hint


def test_language_anchor_quiet_paths():
    # 纯中文历史 → 不注入（防 prompt 膨胀）
    zh_hist = [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "content": "在呀，想我了？"},
    ]
    assert build_language_anchor_hint(zh_hist, current_text="好呀好呀") == ""
    # 本条非中文 → 不注入（那是 build_language_switch_hint 的场景）
    ja_hist = [{"role": "assistant", "content": "日本語に戻したね"}]
    assert build_language_anchor_hint(ja_hist, current_text="Hello") == ""
    # 空历史/空文本
    assert build_language_anchor_hint([], current_text="好呀") == ""
    assert build_language_anchor_hint(ja_hist, current_text="") == ""


def test_language_anchor_assistant_comment_alone_triggers():
    """历史里只有 assistant 的语言点评（无外语字符）也算风险语境。"""
    hist = [{"role": "assistant", "content": "诶～突然换日文了，好可爱！那我也用日文回你！"}]
    hint = build_language_anchor_hint(hist, current_text="我说的是中文啊")
    assert hint != ""


# ── enrich 汇入 ──────────────────────────────────────────────────────────────
def test_apply_enrichments_combines_hints():
    from src.inbox.inbound_enrich import apply_inbound_enrichments

    ctx = {"_turn_gap_sec": 86400 * 10}
    hist = [
        {"role": "user", "content": "何してるの？"},
        {"role": "assistant", "content": "あ、日本語に戻したね"},
    ]
    apply_inbound_enrichments(
        ctx, text="好呀好呀", history=hist, reply_lang="zh")
    hint = ctx.get("_topic_switch_hint") or ""
    assert "语言事实" in hint       # 钉子
    assert "时间提示" in hint       # 断层
    # 正常连聊（无 gap、纯中文历史）→ 不产生提示
    ctx2 = {"_turn_gap_sec": 60}
    apply_inbound_enrichments(
        ctx2, text="好呀", history=[{"role": "user", "content": "在吗"}],
        reply_lang="zh")
    assert "_topic_switch_hint" not in ctx2
