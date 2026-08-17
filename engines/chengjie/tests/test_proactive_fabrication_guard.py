"""反编造守卫门禁（P1 2026-08-03）——用事故真实语料 + 易误伤反例双面钉死。

核心不变量：
  1. 事故正例（无 facts + 断言共同过去）必判编造；
  2. 日常开场（想你了/早安/纯新闻分享/切入点问候）零误伤；
  3. 有真实记忆事实支撑的 follow_up 默认放行（信任 grounding）；
  4. strict_when_facts 档下「facts 存在但不支撑该往事」才拦。
"""
from src.utils.proactive_fabrication_guard import (
    build_precise_evidence,
    build_reply_evidence,
    detect_fabricated_memory,
    fabrication_counters,
    past_claim_markers_hit,
    record_fabrication_guard,
    strip_fabricated_sentences,
)


class TestAccidentPositive:
    """事故真实语料：必须判为编造。"""

    def test_shenma_search_accident_verbatim(self):
        # 2026-08-03 13:36 神马搜索会话原文（该会话 episodic_memory 零条目）
        text = ("哈，刷到粤BA决赛新闻，第一反应就是你以前拽我去打球的画面😂 "
                "最近还有在打吗？")
        fab, ev = detect_fabricated_memory(text, context_facts=[])
        assert fab is True
        assert "你以前" in ev

    def test_no_facts_various_claims(self):
        for text in (
            "还记得我们上次一起看的那场电影吗，最近又出续集了",
            "咱俩上次说好要去爬山的呀，什么时候有空",
            "you used to love this song, they just released a remix",
            "remember when we talked about that trip? tickets are cheap now",
            "你之前说想学吉他来着，学了没",
        ):
            fab, ev = detect_fabricated_memory(text, context_facts=[])
            assert fab is True, f"应判编造: {text!r}"
            assert ev


class TestDailyOpenersNoFalsePositive:
    """日常开场：绝不能误伤（零误判是第一优先级）。"""

    def test_plain_openers_pass(self):
        for text in (
            "想你了，最近怎么样呀",
            "早安~新的一天也要元气满满哦",
            "晚安啦，做个好梦",
            "刚路过一家奶茶店，突然就想到你了，最近忙不忙",
            "刷到一条粤BA决赛的新闻，感觉挺热闹的，你最近在追吗",
            "今天降温了，记得多穿点别感冒",
            "hey, how have you been lately?",
            "good morning! hope you have a lovely day",
        ):
            fab, ev = detect_fabricated_memory(text, context_facts=[])
            assert fab is False, f"日常开场被误伤: {text!r} -> {ev}"

    def test_markers_empty_for_daily(self):
        assert past_claim_markers_hit("想你了，最近怎么样") == []
        assert past_claim_markers_hit("记得多穿点") == []   # 裸「记得」不算断言


class TestFactsSupportedPass:
    """有真实记忆事实：默认档放行（信任 grounding），不误伤 follow_up。"""

    def test_follow_up_with_supporting_fact_passes(self):
        text = "还记得你说在备考嘛，考得怎么样啦"
        fab, _ = detect_fabricated_memory(text, context_facts=["用户在准备考试"])
        assert fab is False

    def test_any_fact_present_default_lenient(self):
        # 默认档：只要有 facts 就放行（哪怕不完全对得上），零误伤优先
        text = "你以前提过喜欢喝手冲咖啡对吧"
        fab, _ = detect_fabricated_memory(
            text, context_facts=["用户喜欢手冲咖啡"])
        assert fab is False


class TestStrictWhenFacts:
    """strict 档：facts 存在但不支撑该往事 → 拦。"""

    def test_strict_unsupported_claim_blocked(self):
        text = "你以前拽我去打球的画面还历历在目"
        # facts 里全是无关内容 → 该往事（打球）零支撑
        fab, ev = detect_fabricated_memory(
            text, context_facts=["用户喜欢喝咖啡", "用户是程序员"],
            strict_when_facts=True)
        assert fab is True
        assert ev

    def test_strict_supported_claim_passes(self):
        text = "还记得你说在备考嘛，考得怎么样"
        fab, _ = detect_fabricated_memory(
            text, context_facts=["用户最近在备考"], strict_when_facts=True)
        assert fab is False


class TestRobustness:
    def test_empty_and_none(self):
        assert detect_fabricated_memory("", []) == (False, "")
        assert detect_fabricated_memory(None, None) == (False, "")

    def test_never_raises(self):
        # 各种脏输入不抛
        for bad in (123, [], {"x": 1}, "🐴🔍"):
            detect_fabricated_memory(bad, context_facts=[1, None, "x"])


# ── 回复链（A/B 线）守卫 ─────────────────────────────────────────────────────


class TestReplyEvidencePool:
    """依据池宽口径：任何一处有内容都算「这会话有据可依」。"""

    def test_user_text_counts_as_evidence(self):
        # 客户自己提起往事 → AI 顺着复述合法，必须算依据
        ev = build_reply_evidence({}, "还记得我们上次聊的旅行吗")
        assert ev and any("旅行" in e for e in ev)

    def test_collects_all_context_sources(self):
        ctx = {
            "_episodic_memory_text": "用户在备考",
            "_conversation_summary": "聊过考试安排",
            "last_message": "我昨天没睡好",
            "last_reply": "早点休息呀",
            "_conversation_history": [
                {"content": "上周去爬山了"}, "纯字符串条目",
            ],
            "_user_profile": {"job": "程序员"},
        }
        ev = build_reply_evidence(ctx, "在吗")
        blob = " ".join(ev)
        for expect in ("备考", "考试安排", "没睡好", "早点休息",
                       "爬山", "纯字符串条目", "程序员", "在吗"):
            assert expect in blob, f"依据漏收: {expect}"

    def test_empty_context_is_empty_pool(self):
        assert build_reply_evidence({}, "") == []
        assert build_reply_evidence(None, None) == []

    def test_dirty_fields_skipped_not_raised(self):
        ctx = {
            "_episodic_memory_text": 123,
            "_conversation_history": {"not": "a list"},
            "_user_profile": 42,
            "last_message": "   ",
        }
        assert build_reply_evidence(ctx, "") == []

    def test_history_limit_takes_recent(self):
        ctx = {"_conversation_history": [f"msg{i}" for i in range(30)]}
        ev = build_reply_evidence(ctx, "", history_limit=3)
        assert ev == ["msg27", "msg28", "msg29"]


class TestReplyStripNoFalsePositive:
    """不该动手的场景（误伤比偶发编造更伤对话）。"""

    def test_no_claim_untouched(self):
        for text in ("在的呀，今天怎么样", "记得多穿点，别感冒了",
                     "好呀，那就这么定啦"):
            out, info = strip_fabricated_sentences(text, [])
            assert out == text
            assert info["stripped"] == [] and info["suspect"] is False

    def test_user_raised_the_past_is_legit(self):
        # 客户先问「还记得我们上次聊的旅行吗」→ AI 复述必须放行
        user_text = "还记得我们上次聊的旅行吗"
        reply = "记得我们说过想去大阪呀，你还在计划吗"
        ev = build_reply_evidence({}, user_text)
        out, info = strip_fabricated_sentences(reply, ev)
        assert out == reply
        assert info["evidence_empty"] is False

    def test_recent_history_evidence_blocks_stripping(self):
        # 五分钟前刚聊过 → 引用「你上次说」合法，不得剥离
        ctx = {"last_message": "我最近在准备考试"}
        reply = "你上次说在备考，复习得怎么样啦"
        out, info = strip_fabricated_sentences(
            reply, build_reply_evidence(ctx, ""))
        assert out == reply
        assert info["stripped"] == []

    def test_unsupported_with_evidence_only_flags_suspect(self):
        # 有依据但长期记忆对不上 → 只记疑似，文本一字不改（攒数据，不拍脑袋收紧）
        ctx = {"_episodic_memory_text": "用户喜欢喝咖啡"}
        reply = "你以前拽我去打球那次可太逗了"
        out, info = strip_fabricated_sentences(
            reply, build_reply_evidence(ctx, ""),
            precise_evidence=build_precise_evidence(ctx))
        assert out == reply
        assert info["suspect"] is True
        assert info["stripped"] == []


class TestTwoTierEvidence:
    """两档分工：剥离用宽池（零误伤），疑似用窄池（有信息量）。

    首轮真实语料实测 suspect=0 暴露了「两档共用宽池」的哑仪表问题：宽池含整段
    对话历史，词汇交集几乎必然命中 → 疑似永远指零。这组钉住修好后的分工。
    """

    def test_precise_pool_excludes_history_and_user_text(self):
        ctx = {
            "_episodic_memory_text": "用户在备考",
            "_conversation_history": ["上周我们去打球了"],
            "last_message": "打球真累",
        }
        precise = build_precise_evidence(ctx)
        blob = " ".join(precise)
        assert "备考" in blob
        assert "打球" not in blob, "窄池不得含对话历史（否则疑似档失效）"
        # 宽池反之必须含历史与用户本条（剥离档零误伤靠它）
        wide = " ".join(build_reply_evidence(ctx, "你还记得吗"))
        assert "打球" in wide and "你还记得吗" in wide

    def test_history_only_evidence_yields_no_suspect_but_no_strip(self):
        # 只有对话历史（无长期记忆）→ 不剥（有依据）且不判疑似（窄池空=无从判定）
        ctx = {"_conversation_history": ["昨天聊到打球"]}
        reply = "你以前拽我去打球那次太逗了"
        out, info = strip_fabricated_sentences(
            reply, build_reply_evidence(ctx, ""),
            precise_evidence=build_precise_evidence(ctx))
        assert out == reply
        assert info["stripped"] == []
        assert info["suspect"] is False

    def test_precise_supported_claim_not_suspect(self):
        ctx = {"_episodic_memory_text": "用户最近在备考公务员"}
        reply = "你之前说在备考，复习得怎么样啦"
        out, info = strip_fabricated_sentences(
            reply, build_reply_evidence(ctx, ""),
            precise_evidence=build_precise_evidence(ctx))
        assert out == reply
        assert info["suspect"] is False

    def test_precise_pool_dirty_input(self):
        assert build_precise_evidence(None) == []
        assert build_precise_evidence({"_episodic_memory_text": 1}) == []


class TestReplyStripZeroEvidence:
    """零依据（真正首轮 / bot 会话）＝事故形态，必须剥。"""

    def test_strips_only_the_fabricated_sentence(self):
        reply = "在的呀！你以前拽我去打球那次太好笑了。最近忙不忙？"
        out, info = strip_fabricated_sentences(reply, [])
        assert "打球" not in out
        assert "在的呀" in out and "最近忙不忙" in out
        assert info["evidence_empty"] is True
        assert len(info["stripped"]) == 1
        assert info["all_stripped"] is False

    def test_all_fabricated_falls_back_to_original(self):
        # 整条都是编造 → 应答不能发空，如实回落原文并标记（价值在被看见）
        reply = "还记得我们上次一起看的那场电影吗？"
        out, info = strip_fabricated_sentences(reply, [])
        assert out == reply
        assert info["all_stripped"] is True
        assert info["stripped"]

    def test_multiple_sentences_stripped(self):
        reply = "嗨。你之前说想学吉他。我们上次还约了爬山。今天天气不错呢。"
        out, info = strip_fabricated_sentences(reply, [])
        assert "吉他" not in out and "爬山" not in out
        assert "今天天气不错呢" in out
        assert len(info["stripped"]) == 2

    def test_never_raises_on_dirty_input(self):
        for bad in (None, "", 123, "🐴"):
            strip_fabricated_sentences(bad, None)


class TestCounters:
    def test_counters_bucket_by_source(self):
        before = (fabrication_counters().get("unit_test_src")
                  or {"strip": 0, "suspect": 0, "all_stripped": 0})
        record_fabrication_guard(
            {"stripped": ["x"], "suspect": False, "all_stripped": False},
            source="unit_test_src")
        record_fabrication_guard(
            {"stripped": [], "suspect": True, "all_stripped": False},
            source="unit_test_src")
        record_fabrication_guard(
            {"stripped": ["y"], "suspect": False, "all_stripped": True},
            source="unit_test_src")
        after = fabrication_counters()["unit_test_src"]
        assert after["strip"] == before["strip"] + 2
        assert after["suspect"] == before["suspect"] + 1
        assert after["all_stripped"] == before["all_stripped"] + 1

    def test_bad_info_ignored(self):
        record_fabrication_guard(None, source="unit_test_src")
        record_fabrication_guard("nope", source="unit_test_src")
