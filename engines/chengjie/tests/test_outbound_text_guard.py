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
    # vocative/lang_pin/shared_past（实施91 #105/#106/#110）与旧五键同段
    # 配置、同默认开；goal_meta（#109 第4病）同族同默认。
    full_on = {"enabled": True, "monologue": True, "lang_mix": True,
               "unfounded_recall": True, "recall_grounding": True,
               "apology_dedup": True, "vocative": True, "lang_pin": True,
               "shared_past": True, "goal_meta": True}
    assert resolve_cfg(None) == full_on
    assert resolve_cfg({}) == full_on
    got = resolve_cfg({"companion": {"outbound_text_guard": {
        "enabled": False, "lang_mix": False, "unfounded_recall": False,
        "recall_grounding": False, "apology_dedup": False}}})
    assert got == {"enabled": False, "monologue": True, "lang_mix": False,
                   "unfounded_recall": False, "recall_grounding": False,
                   "apology_dedup": False, "vocative": True, "lang_pin": True,
                   "shared_past": True, "goal_meta": True}


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


# ── #71（0830 午批）：舞台指示中英全家族 ─────────────────────────────────────

def test_tone_shift_stage_direction_stripped_71():
    """0830 实锤原文：'(tone shifts to playful)' 字面直发客户。"""
    src = ("You're making I swear. (tone shifts to playful)"
           "Maybe one day you cook for me")
    cleaned, hits = sanitize_inner_monologue(src)
    assert "tone shifts" not in cleaned
    assert "Maybe one day you cook for me" in cleaned
    assert hits


def test_zh_tone_stage_direction_stripped_71():
    """中文变体（#68 单里 skuio 同日实录「（语气转为 playful）」）。"""
    cleaned, hits = sanitize_inner_monologue("好呀好呀（语气转为 playful）明天见啦")
    assert "语气" not in cleaned
    assert "好呀好呀" in cleaned and "明天见啦" in cleaned
    assert hits


def test_stage_direction_family_stripped_71():
    """中英舞台指示家族逐形态验证（语气/声线/动作/方式副词）。"""
    cases = [
        ("Sure! (voice softens) I'm always here for you.", "voice softens"),
        ("Haha (winks) you know me so well my friend.", "winks"),
        ("Well (leans in closer) tell me more about it.", "leans in"),
        ("Okay (takes a deep breath) let's talk about us.", "deep breath"),
        ("Fine (rolls her eyes) whatever you say dear.", "rolls her eyes"),
        ("Hmm (playfully) guess what I did today?", "playfully"),
        ("Come on (shifts to teasing) you missed me right?", "shifts to"),
        ("嗯嗯（撒娇）人家想你了嘛", "撒娇"),
        ("好啦（坏笑）等你哦", "坏笑"),
        ("知道啦（温柔）早点休息", "温柔"),
        ("那说好了（清了清嗓子）下次一起去", "清了清嗓"),
    ]
    for s, gone in cases:
        cleaned, hits = sanitize_inner_monologue(s)
        assert gone not in cleaned, s
        assert hits, s


def test_legit_parenthetical_still_kept_after_71():
    """#71 扩表后仍不误伤正常括号补充语（宁可漏拦不误伤的底线不动）。"""
    for s in ("我买了新手机（iPhone 15）超好用",
              "会议改到明天（周三）下午三点",
              "That costs $20 (about 145 RMB) in total price.",
              "下载链接（官网首页）已经发你了"):
        cleaned, hits = sanitize_inner_monologue(s)
        assert cleaned == s, s
        assert not hits, s


# ── #64（0830 三度复报）：拉丁主体夹 CJK 残字 → 拦截重写档 ───────────────────

def test_reply_lang_mismatch_latin_cjk_residue_64():
    """「I'm 我」形态判 mismatch → _guard_reply_language 走一次重写。"""
    from src.ai.ai_client import AIClient
    f = AIClient._reply_lang_mismatch
    assert f("I'm 我, and I'm not going anywhere.", "en") is True
    assert f("I'm me, and I'm not going anywhere.", "en") is False
    # 旧口径（CJK 绝对主体）不回归
    assert f("这是一段很长的中文回复内容哦亲爱的", "en") is True
    # 字母不足 6 的短句不动（防误伤「ok 我」类正常混说）
    assert f("ok 我", "en") is False
    # zh / ja 目标语分支不受扩展影响
    assert f("哈哈 ok 啦没问题", "zh") is False
    assert f("こんにちは、元気ですか", "ja") is False


def test_deferred_translate_exit_guarded_64():
    """deferred 触达翻译出口必须挂混语守卫（三条翻译出口唯一裸奔的那条）。"""
    import re as _re
    src = (_ENGINE_ROOT / "main.py").read_text(encoding="utf-8")
    m = _re.search(
        r"async def _maybe_translate_outbound.*?(?=\n    (?:async )?def )",
        src, _re.S)
    assert m, "main.py 里找不到 _maybe_translate_outbound"
    assert "_guard_translated_lang_mix" in m.group(0)


# ── #74（0830）：媒体轮语言锚——识图中文描述不得冒充用户语言 ──────────────────

def test_strip_system_injected_media_lines_74():
    from src.ai.lang_policy import strip_system_injected
    assert strip_system_injected("[图片内容] 白盘装熟虾，旁边有蘑菇汤") == ""
    got = strip_system_injected(
        "this looks so yummy today\n[图片内容] 白盘装熟虾和螃蟹")
    assert "yummy" in got and "熟虾" not in got
    # 语音标记只剥标记本身，转写正文（客户原话）保留
    assert "hello there" in strip_system_injected("[语音] hello there")


def test_media_turn_lang_rule_follows_reply_lang_74():
    """英文会话发图（无 caption）：语言指令跟 reply_lang，绝不变中文命令。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "[图片内容] 白盘装熟虾，旁边有蘑菇汤和螃蟹",
        "reply_lang": "en",
        "channel": "telegram", "chat_type": "private",
    })
    assert "「English」" in p
    assert "用户当前消息语言为「中文」" not in p


def test_media_turn_caption_language_wins_74():
    """带英文 caption 的媒体轮：按 caption 判语言，不被中文描述压过。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": (
            "this looks so yummy, what do you think about it\n"
            "[图片内容] 白盘装熟虾和蘑菇汤"),
        "channel": "telegram", "chat_type": "private",
    })
    assert "「English」" in p


def test_media_turn_without_lang_never_commands_zh_74():
    """纯媒体轮且无 reply_lang：宁可不注入语言指令，绝不默认命令中文。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "[图片内容] 白盘装熟虾，旁边有蘑菇汤",
        "channel": "telegram", "chat_type": "private",
    })
    assert "你必须用该语言回复" not in p


def test_zh_text_turn_lang_rule_unchanged_74():
    """普通中文文本轮：行为与改前一致（仍注入中文指令）。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "今天吃了火锅超开心呀",
        "channel": "telegram", "chat_type": "private",
    })
    assert "「中文」" in p


def test_vision_block_marked_as_system_annotation_74():
    """image_ocr_text 块必须自带「系统标注/勿跟随其语言」声明。"""
    p = _mk_ai()._build_context_prompt({
        "last_message": "look at this",
        "image_ocr_text": "白盘装熟虾，旁边有蘑菇汤",
    })
    assert "系统自动标注" in p
    assert "非对方原话" in p


# ── #91-A（0830 OMEN 实锤）：回忆类断言接地锁 ────────────────────────────────

_OMEN_REPLY = "你上次提过那台惠普OMEN电竞系列吗？对吧，我记性可好了😊"
_HIST_NO_PC = ["今天好累啊", "晚上想吃点清淡的", "你平时都几点睡"]


def test_recall_grounding_strips_omen_case_91():
    """事故金标：历史/记忆全无 OMEN → 现编断言整句剥除。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    out, hits = strip_hallucinated_recall(
        _OMEN_REPLY, user_texts=_HIST_NO_PC, memory_text="")
    assert hits, "OMEN 现编断言必须被抓"
    assert "OMEN" not in out and "惠普" not in out


def test_recall_grounding_keeps_grounded_claim_91():
    """用户真提过 → 合法回忆放行（原话词汇重叠即接地）。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    out, hits = strip_hallucinated_recall(
        "你上次提过想去大阪玩，机票看了吗？",
        user_texts=["我一直想去大阪玩来着", "最近好忙"], memory_text="")
    assert not hits
    assert "大阪" in out


def test_recall_grounding_memory_counts_as_evidence_91():
    """记忆条目也是合法出处（对上记忆块即放行）。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    out, hits = strip_hallucinated_recall(
        "记得你提过你家养了只橘猫",
        user_texts=["早", "嗯嗯"], memory_text="- 用户养了一只橘猫（2026-08）")
    assert not hits
    assert "橘猫" in out


def test_recall_grounding_no_corpus_no_action_91():
    """语料没传（历史缺席）→ 整体不动手，缺料绝不乱杀。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    out, hits = strip_hallucinated_recall(
        _OMEN_REPLY, user_texts=[], memory_text="")
    assert not hits and out == _OMEN_REPLY


def test_recall_grounding_generic_bigrams_not_evidence_91():
    """通用功能词（那台/这个/时候）不算接地证据——碰瓷放行是不设防。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    out, hits = strip_hallucinated_recall(
        "今晚打算干嘛呀？你上次提过那台惠普OMEN电竞本对吧。",
        user_texts=["那台空调好吵", "这个时候你还没睡呀"], memory_text="")
    assert hits, "仅「那台/这个」重叠不构成接地"
    assert "OMEN" not in out and "今晚打算干嘛呀" in out


def test_recall_grounding_strip_all_falls_back_91():
    """整条都是现编断言 → 剥空回退原文（绝不吞掉回复）。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    src = "你上次提过那台惠普OMEN电竞系列吗"
    out, hits = strip_hallucinated_recall(
        src, user_texts=_HIST_NO_PC, memory_text="")
    assert hits and out == src


def test_recall_grounding_wired_via_apply_91():
    """编排入口穿线：user_texts/memory_text 进 apply → meta 带命中。"""
    out, meta = apply_outbound_text_guard(
        "哈哈今天好玩吗？你上次提过那台惠普OMEN电竞系列吧。",
        resolve_cfg(None), user_turns=5, has_memory=False,
        user_texts=_HIST_NO_PC, memory_text="")
    assert meta["recall_grounding_hits"]
    assert "OMEN" not in out and "好玩吗" in out


def test_recall_grounding_en_variant_91():
    """英文形态：you mentioned X before 同样接地校验。"""
    from src.ai.outbound_text_guard import strip_hallucinated_recall
    out, hits = strip_hallucinated_recall(
        "You mentioned that HP OMEN laptop before, right?",
        user_texts=["so tired today", "what did you eat"], memory_text="")
    assert hits
    out2, hits2 = strip_hallucinated_recall(
        "You mentioned that HP OMEN laptop before, right?",
        user_texts=["I want an HP OMEN so bad"], memory_text="")
    assert not hits2 and "OMEN" in out2


# ── #91-C（同单）：认错口癖同会话去重 ────────────────────────────────────────

def test_apology_dedup_swaps_variant_91():
    """近史用过「被你抓包了」→ 本条换变体，句子结构完整。"""
    from src.ai.outbound_text_guard import dedup_apology_catchphrase
    out, hits = dedup_apology_catchphrase(
        "哎呀，被你抓包了啦，我记岔了",
        recent_assistant_texts=["嘿嘿被你抓包了，上次说错了"])
    assert hits
    assert "被你抓包" not in out
    assert "我记岔了" in out          # 句子其余部分保留


def test_apology_dedup_first_use_untouched_91():
    """近史没用过 → 首次使用合法，原样放行。"""
    from src.ai.outbound_text_guard import dedup_apology_catchphrase
    src = "哎呀，被你抓包了啦"
    out, hits = dedup_apology_catchphrase(
        src, recent_assistant_texts=["昨晚睡得好吗", "我去做饭啦"])
    assert not hits and out == src


def test_apology_dedup_no_history_no_action_91():
    """近史缺席 → 不动手（缺料纪律与接地锁一致）。"""
    from src.ai.outbound_text_guard import dedup_apology_catchphrase
    src = "被你抓包了"
    out, hits = dedup_apology_catchphrase(src, recent_assistant_texts=None)
    assert not hits and out == src


def test_apology_dedup_deterministic_91():
    """确定性：同稿同近史两次运行结果一致（缓存/重试友好）。"""
    from src.ai.outbound_text_guard import dedup_apology_catchphrase
    recent = ["被你抓包了哈"]
    a = dedup_apology_catchphrase("被你抓包了，我认", recent)
    b = dedup_apology_catchphrase("被你抓包了，我认", recent)
    assert a == b


def test_apology_dedup_picks_unused_variant_91():
    """变体选择避开近史已出现的（同会话不重样的核心语义）。"""
    from src.ai.outbound_text_guard import dedup_apology_catchphrase
    recent = ["被你抓包了", "让你说着了", "好吧我承认"]
    out, hits = dedup_apology_catchphrase("被你抓包了啦", recent)
    assert hits
    assert "让你说着了" not in out and "好吧我承认" not in out


# ── #109 第4病（0831 skuio 实锤）：目标话术渗味——步骤感元话术 ────────────────


def test_109_goal_meta_field_sample_en():
    """事故原话金标：『that's the first step』子段剥除，同句正常内容保住。"""
    from src.ai.outbound_text_guard import strip_goal_meta_talk
    out, hits = strip_goal_meta_talk(
        "Glad you're still interested — that's the first step.")
    assert hits and "first step" not in out.lower()
    assert "Glad you're still interested" in out


def test_109_goal_meta_field_sample_zh():
    """工作台中译形态：『——这是第一步』同样剥除（破折号子段）。"""
    from src.ai.outbound_text_guard import strip_goal_meta_talk
    out, hits = strip_goal_meta_talk("很高兴你还对这个感兴趣——这是第一步。")
    assert hits and "第一步" not in out
    assert "很高兴你还对这个感兴趣" in out


def test_109_goal_meta_milestone_and_goal_forms():
    from src.ai.outbound_text_guard import strip_goal_meta_talk
    out, hits = strip_goal_meta_talk(
        "今天聊得很开心，离我们的目标又近了一步。")
    assert hits and "近了一步" not in out and "聊得很开心" in out
    out2, hits2 = strip_goal_meta_talk(
        "That was lovely. We just hit our first milestone together!")
    assert hits2 and "milestone" not in out2.lower()
    assert "That was lovely" in out2


def test_109_goal_meta_everyday_step_talk_untouched():
    """日常「下一步/step」用语绝不误伤（宁可漏拦不误伤）。"""
    from src.ai.outbound_text_guard import strip_goal_meta_talk
    for s in ("下一步打算去哪玩？",
              "你到楼下之后下一步就是右转直走。",
              "The next step for your order is the payment link I sent.",
              "Watch your step, the floor is wet.",
              "我们一步一步来，不着急。"):
        out, hits = strip_goal_meta_talk(s)
        assert not hits and out == s, s


def test_109_goal_meta_all_meta_falls_back_to_src():
    """整段皆元话术 → 剥空回退原文（守卫绝不吞掉整条回复）。"""
    from src.ai.outbound_text_guard import strip_goal_meta_talk
    src = "That's the first step."
    out, hits = strip_goal_meta_talk(src)
    assert hits and out == src


def test_109_goal_meta_wired_into_apply_and_stats():
    """apply 编排接线 + 配置键 + 计数器（三件套缺一=静默缺陷）。"""
    from src.ai.outbound_text_guard import (
        apply_outbound_text_guard, guard_stats, resolve_cfg,
    )
    assert resolve_cfg(None).get("goal_meta") is True
    assert resolve_cfg(
        {"companion": {"outbound_text_guard": {"goal_meta": False}}}
    ).get("goal_meta") is False
    before = guard_stats().get("goal_meta", 0)
    out, meta = apply_outbound_text_guard(
        "Glad you're still interested — that's the first step.")
    assert meta.get("goal_meta_hits")
    assert "first step" not in out.lower()
    assert guard_stats().get("goal_meta", 0) == before + 1
    # 关子开关 → 原样放行
    out2, meta2 = apply_outbound_text_guard(
        "that's the first step for us",
        {"enabled": True, "goal_meta": False})
    assert "first step" in out2.lower() and not meta2.get("goal_meta_hits")


# ── #91-B（同单）：他人串扰自曝红线（persona_guard 家族） ────────────────────

def test_peer_leak_redline_golden_91():
    """事故原话金标：『可能把别人的事记到你头上了』必拦。"""
    from src.utils.persona_guard import matches_multi_peer_leak
    assert matches_multi_peer_leak("我记岔了，可能把别人的事记到你头上了")
    assert matches_multi_peer_leak("不好意思，把你和别人搞混了")
    assert matches_multi_peer_leak("我聊的人太多，记混了")
    assert matches_multi_peer_leak("Sorry, I mixed you up with someone else")


def test_peer_leak_plain_apology_not_hit_91():
    """单纯认错不命中——「我记岔了」是正常人也说的话，红线只拦自曝他人。"""
    from src.utils.persona_guard import matches_multi_peer_leak
    for s in ("哎呀我记岔了，抱歉", "我记错了，你说的对",
              "最近脑子不好使，记不清了", "你和别人不一样呀",
              "别人都说这家店好吃"):
        assert not matches_multi_peer_leak(s), s


def test_peer_leak_sanitize_strips_sentence_91():
    """sanitize 无条件剥除红线句（不依赖人设 forbidden_phrases 配置）。"""
    from src.utils.persona_guard import sanitize
    out, hits = sanitize(
        "哎呀被你抓包了。我记岔了，可能把别人的事记到你头上了。别生气嘛。",
        {})   # 空人设也要拦——红线与人设配置无关
    assert hits
    assert "别人的事" not in out
    assert "别生气嘛" in out


def test_peer_leak_honest_identity_exempt_91():
    """honest_identity 合规模式随身份家族豁免（诚实档自述多客户不算穿帮）。"""
    from src.utils.persona_guard import sanitize
    src = "抱歉，我可能把别人的事记到你头上了"
    out, hits = sanitize(src, {}, honest_identity=True)
    assert not hits and out == src
