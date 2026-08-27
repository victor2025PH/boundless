# -*- coding: utf-8 -*-
"""实施74 阶段1 门禁：出站文本形态守卫（B118 内心独白 + B121 语种混杂）。

金标全部来自 0826 实录：
- B118 ``_578``：客户「我只是生你的气」→ AI 直发「（我没生气，就是心里堵得慌）」；
- B121 ``_590``：出站「I'm 我 going anywhere.」直发客户。

守卫哲学（勿在测试里放松）：宁可漏拦不误伤——正常括号补充语、中文夹短英文
不得被动；清洗后为空必须回退原文。
"""
from __future__ import annotations

from pathlib import Path

from src.ai.outbound_text_guard import (
    apply_outbound_text_guard,
    detect_lang_mix,
    guard_stats,
    resolve_cfg,
    sanitize_inner_monologue,
    strip_minority_script,
    strip_unfounded_recall,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── B118 内心独白 ────────────────────────────────────────────────────────────

def test_whole_wrap_monologue_unwrapped_578():
    """_578 实录：整条被括号包裹 → 去壳保台词（内容本身是合法台词）。"""
    cleaned, hits = sanitize_inner_monologue("（我没生气，就是心里堵得慌）")
    assert cleaned == "我没生气，就是心里堵得慌"
    assert hits


def test_whole_wrap_halfwidth_variant():
    cleaned, hits = sanitize_inner_monologue("(我没生气，就是心里堵得慌)")
    assert cleaned == "我没生气，就是心里堵得慌"
    assert hits


def test_embedded_narration_stripped():
    cleaned, hits = sanitize_inner_monologue("好啦别气了（叹了口气）明天见")
    assert "叹了口气" not in cleaned
    assert "好啦别气了" in cleaned and "明天见" in cleaned
    assert hits


def test_asterisk_action_stripped():
    cleaned, hits = sanitize_inner_monologue("*叹了口气* 我真的没生气")
    assert cleaned == "我真的没生气"
    assert hits


def test_english_narration_stripped():
    cleaned, hits = sanitize_inner_monologue("I'm fine (sighs softly) really")
    assert "sighs" not in cleaned
    assert "I'm fine" in cleaned and "really" in cleaned
    assert hits


def test_legit_parenthetical_kept():
    """正常补充语绝不误伤（宁可漏拦不误伤）。"""
    src = "我买了新手机（iPhone 15）超好用"
    cleaned, hits = sanitize_inner_monologue(src)
    assert cleaned == src
    assert not hits


def test_not_whole_wrap_when_inner_has_same_bracket():
    src = "(a) 正文在这里 (b)"
    cleaned, _ = sanitize_inner_monologue(src)
    assert cleaned == src


def test_markdown_bold_not_treated_as_action():
    src = "这个 **很重要** 记得看"
    cleaned, hits = sanitize_inner_monologue(src)
    assert cleaned == src
    assert not hits


# ── B121 语种混杂 ────────────────────────────────────────────────────────────

def test_lang_mix_hard_590():
    """_590 实录：拉丁主体夹 CJK → hard，剥少数派后正常出站。"""
    v = detect_lang_mix("I'm 我 going anywhere.")
    assert v["mixed"] and v["action"] == "hard"
    assert strip_minority_script("I'm 我 going anywhere.") == "I'm going anywhere."


def test_chinese_with_short_english_not_flagged():
    for s in ("明天一起去 gym 吗", "我在用 iPhone 和 MacBook Pro 哈哈",
              "OK 呀，没问题"):
        v = detect_lang_mix(s)
        assert not v["mixed"], s


def test_cjk_dominant_long_english_run_is_soft_only():
    """中文主体夹整句英文（教学等合法场景）→ 只观测不动手。"""
    s = "今天教你一句英文表达 I will always be there for you 记得练习哦"
    v = detect_lang_mix(s)
    assert v["mixed"] and v["action"] == "soft"
    out, meta = apply_outbound_text_guard(s)
    assert out == s
    assert meta["lang_mix"] == "soft"


def test_url_and_handle_exempt():
    assert not detect_lang_mix("Check this https://example.com/图片页 ok friend?")["mixed"]
    assert not detect_lang_mix("Add me @user_名字 on the app please now")["mixed"]


def test_strip_minority_safety_valve():
    """剥后拉丁字母太少 → 返回原文，绝不出站残句。"""
    src = "你好呀朋友 hey"
    assert strip_minority_script(src) == src


# ── 编排入口 ─────────────────────────────────────────────────────────────────

def test_apply_guard_end_to_end_both_incidents():
    out1, meta1 = apply_outbound_text_guard("（我没生气，就是心里堵得慌）")
    assert out1 == "我没生气，就是心里堵得慌"
    assert meta1["monologue_hits"]

    out2, meta2 = apply_outbound_text_guard("I'm 我 going anywhere.")
    assert out2 == "I'm going anywhere."
    assert meta2["lang_mix"] == "hard_stripped"


def test_apply_guard_never_returns_empty():
    out, _ = apply_outbound_text_guard("（微笑）")
    assert out.strip()


def test_apply_guard_disabled_passthrough():
    src = "（我没生气，就是心里堵得慌）"
    out, meta = apply_outbound_text_guard(
        src, {"enabled": False, "monologue": True, "lang_mix": True})
    assert out == src
    assert not meta["monologue_hits"]


def test_resolve_cfg_defaults_on_and_overridable():
    full_on = {"enabled": True, "monologue": True, "lang_mix": True,
               "unfounded_recall": True}
    assert resolve_cfg(None) == full_on
    assert resolve_cfg({}) == full_on
    got = resolve_cfg({"companion": {"outbound_text_guard": {
        "enabled": False, "lang_mix": False, "unfounded_recall": False}}})
    assert got == {"enabled": False, "monologue": True, "lang_mix": False,
                   "unfounded_recall": False}


def test_empty_input_passthrough():
    out, meta = apply_outbound_text_guard("")
    assert out == ""
    assert not meta["monologue_hits"] and not meta["lang_mix"]


# ── B104 无出处引用（实施74 二批）────────────────────────────────────────────

def test_recall_stripped_for_first_reply():
    """首答（0 用户轮）+ 无记忆 → 编造引用整句剥除，其余保留。"""
    out, hits = strip_unfounded_recall(
        "你之前说过喜欢旅行。这次想去哪玩？", user_turns=0, has_memory=False)
    assert "你之前说过" not in out
    assert "这次想去哪玩" in out
    assert hits


def test_recall_english_first_reply():
    out, hits = strip_unfounded_recall(
        "As you mentioned before, you like coffee. How is work today?",
        user_turns=0, has_memory=False)
    assert "mentioned" not in out and "How is work today" in out
    assert hits


def test_recall_passes_when_history_memory_or_unknown():
    """有来回/有记忆/轮数未知（老会话被压缩）→ 一律放行（宁可漏拦）。"""
    src = "你之前说过喜欢旅行。"
    assert strip_unfounded_recall(src, user_turns=1, has_memory=False)[0] == src
    assert strip_unfounded_recall(src, user_turns=5, has_memory=False)[0] == src
    assert strip_unfounded_recall(src, user_turns=0, has_memory=True)[0] == src
    assert strip_unfounded_recall(src, user_turns=None, has_memory=False)[0] == src


def test_recall_whole_reply_fails_open():
    """整条都是编造引用 → 剥空回退原文（绝不吞回复），命中仍记录（可观测）。"""
    src = "你之前说过喜欢旅行。"
    out, hits = strip_unfounded_recall(src, user_turns=0, has_memory=False)
    assert out == src and hits


def test_recall_benign_phrases_not_hit():
    """会话内引用（你刚才说）与「说过年」类子串绝不误伤。"""
    for s in ("你刚才说想去海边，我记下了。", "你说过年要回家吗？",
              "You just said you like coffee."):
        out, hits = strip_unfounded_recall(s, user_turns=0, has_memory=False)
        assert out == s and not hits, s


def test_apply_guard_recall_end_to_end_and_kill_switch():
    out, meta = apply_outbound_text_guard(
        "你之前说过喜欢旅行。今天天气不错。", user_turns=0)
    assert meta["recall_hits"] and "你之前说过" not in out
    assert "今天天气不错" in out
    src = "你之前说过喜欢旅行。今天天气不错。"
    out2, meta2 = apply_outbound_text_guard(
        src, {"enabled": True, "monologue": True, "lang_mix": True,
              "unfounded_recall": False}, user_turns=0)
    assert out2 == src and not meta2["recall_hits"]


def test_guard_stats_exposes_recall_counter():
    assert "unfounded_recall" in guard_stats()


# ── B104 生成端提示（实施74 三批：第一道防线在源头）─────────────────────────

def _mk_ai(cfg=None):
    from types import SimpleNamespace

    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)  # 绕过 __init__ 重依赖，只测纯 prompt 构建
    c.config = SimpleNamespace(config=cfg or {})
    return c


def test_first_contact_hint_injected():
    p = _mk_ai()._build_context_prompt(
        {"last_message": "hi", "_conversation_history": []})
    assert "初次对话" in p and "你之前说过" in p


def test_first_contact_hint_absent_with_history():
    p = _mk_ai()._build_context_prompt({
        "last_message": "hi",
        "_conversation_history": [{"role": "user", "content": "早"},
                                  {"role": "assistant", "content": "早呀"}],
    })
    assert "初次对话" not in p


def test_first_contact_hint_absent_with_memory_or_summary():
    base = {"last_message": "hi", "_conversation_history": []}
    p1 = _mk_ai()._build_context_prompt(
        dict(base, _episodic_memory_text="- 喜欢旅行"))
    p2 = _mk_ai()._build_context_prompt(
        dict(base, _conversation_summary="早期聊过旅行计划"))
    assert "初次对话" not in p1 and "初次对话" not in p2


def test_first_contact_hint_requires_history_key():
    """不带历史跟踪的流（试聊/copilot）绝不注入——对老会话谎称初次比编造引用更伤。"""
    p = _mk_ai()._build_context_prompt({"last_message": "hi"})
    assert "初次对话" not in p


# ── 观测接线（实施74 二批）──────────────────────────────────────────────────

def test_metrics_route_wired():
    src = (_ENGINE_ROOT / "src" / "web" / "routes" / "drafts_routes.py"
           ).read_text(encoding="utf-8")
    assert 'metrics["outbound_text_guard"]' in src
    assert "guard_stats" in src


# ── 接线契约（静态）：A/B 两线 + 生成端硬禁双模式 ────────────────────────────

def test_skill_manager_wired_both_lines():
    src = (_ENGINE_ROOT / "src" / "skills" / "skill_manager.py").read_text(
        encoding="utf-8")
    # def + A 线 5c1b + B 线 9a2 —— 少一处即断链
    assert src.count("self._apply_outbound_text_guard(") >= 2
    assert "def _apply_outbound_text_guard(" in src
    assert "5c1b" in src and "9a2" in src
    # B104：两个调用点都必须透传 user_context（否则轮数恒未知=守卫不动手）
    import re as _re
    calls = _re.findall(
        r"self\._apply_outbound_text_guard\(([^)]*)\)", src)
    assert len(calls) >= 2
    assert all("user_context=user_context" in c for c in calls), calls


def test_persona_prompt_hard_ban_in_both_modes():
    src = (_ENGINE_ROOT / "src" / "utils" / "persona_manager.py").read_text(
        encoding="utf-8")
    # 常量定义 + full 模式 append + compact 模式 append
    assert src.count("_INNER_MONOLOGUE_BAN") >= 3
    assert "禁止内心独白" in src
