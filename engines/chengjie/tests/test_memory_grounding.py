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


# ── J-10 A1：引文级接地（跨语言，#183）──────────────────────────────────────
from src.ai.memory_grounding import (  # noqa: E402
    DROP_EVIDENCE_MISMATCH,
    DROP_FACT_UNANCHORED,
    DROP_NO_EVIDENCE,
    evidence_matches_user_msg,
    ground_fact_items,
    ground_fact_with_evidence,
    normalize_for_match,
    summarize_drop_reasons,
)


def test_xlang_english_customer_facts_kept_88mp86():
    """88MP86 实锤：英文客户的中文事实附原话引文 → 留（旧判据下 100% 被丢）。"""
    um = "I've only had three boyfriends in my whole life. And I was married once."
    assert ground_fact_with_evidence(
        "客户结过一次婚", "I was married once", um) == (True, "")
    assert ground_fact_with_evidence(
        "客户一生只交过三个男朋友",
        "I've only had three boyfriends in my whole life", um) == (True, "")
    # 指令验收句
    assert ground_fact_with_evidence(
        "客户有一个女儿", "I have a daughter",
        "I have a daughter and she is my greatest treasure") == (True, "")
    # 旧判据对同一输入的结论（证明修的就是这条）
    assert not fact_grounded_in_user_msg("客户结过一次婚", um)


def test_xlang_thai_japanese_taglish_kept():
    assert ground_fact_with_evidence(
        "客户有一个女儿", "ฉันมีลูกสาว", "ฉันมีลูกสาวหนึ่งคน น่ารักมาก") == (True, "")
    assert ground_fact_with_evidence(
        "客户有一个女儿", "私には娘がいます", "私には娘がいます。もう高校生です。") == (True, "")
    assert ground_fact_with_evidence(
        "客户25岁", "I'm 25 na", "Sige, I'm 25 na. Taga-Davao ako.") == (True, "")


def test_evidence_normalization_and_token_overlap():
    assert normalize_for_match("I’m Tom!!") == normalize_for_match("i'm tom")
    # 子串（标点/大小写/弯引号差异不算不符）
    assert evidence_matches_user_msg("I have a daughter", "i HAVE a daughter, yes!!")
    # LLM 微改引文（she’s→she is）：子串失败但 token 重叠 ≥60%
    assert evidence_matches_user_msg(
        "I have a daughter and she is my greatest treasure",
        "I have a daughter and she’s my greatest treasure!!")
    # 一半是编的 → 不符
    assert not evidence_matches_user_msg("lives in Manila with kids", "I live in Cebu")
    assert not evidence_matches_user_msg("", "anything")
    assert not evidence_matches_user_msg("anything", "")


def test_phase8_incident_still_rejected_under_evidence_rule():
    """Phase8 事故语料三种形态一条不过：引文抄自助手 / 无引文 / 真引文洗白假事实。"""
    um = "好呀好呀"
    assert ground_fact_with_evidence("客户想去大阪玩", "想去大阪玩", um) == (
        False, DROP_EVIDENCE_MISMATCH)
    assert ground_fact_with_evidence("客户明天不用上班", "", um) == (
        False, DROP_NO_EVIDENCE)
    assert ground_fact_with_evidence("客户想去大阪玩", "好呀好呀", um) == (
        False, DROP_FACT_UNANCHORED)
    assert ground_fact_with_evidence(
        "客户深夜还在线，可能明天休息", "你这么晚还在线", "你在干嘛呢 干嘛呢") == (
        False, DROP_EVIDENCE_MISMATCH)


def test_xlang_filler_and_invariant_token_guards():
    """跨语种不能被「真引文」洗白：应答词不算引文；事实里的名字/数字必须在原话里。"""
    assert ground_fact_with_evidence("客户明天不用上班", "yes", "yes")[0] is False
    assert ground_fact_with_evidence("客户明天不用上班", "yes I do", "yes I do")[0] is False
    assert ground_fact_with_evidence("客户来自日本", "ok sure haha", "ok sure haha")[0] is False
    assert ground_fact_with_evidence("客户自称Tom", "I'm Bob", "I'm Bob") == (
        False, DROP_FACT_UNANCHORED)
    assert ground_fact_with_evidence("客户自称Bob", "I'm Bob", "I'm Bob") == (True, "")
    assert ground_fact_with_evidence("客户30岁", "I'm 25", "I'm 25") == (
        False, DROP_FACT_UNANCHORED)


def test_same_language_keeps_legacy_rule():
    """同语种：旧词汇判据照旧叠加——行为与 Phase8 以来一致，不弱化。"""
    um = "我现在住在 Cebu，working as a nurse"
    assert ground_fact_with_evidence("客户住在宿务", "住在 Cebu", um) == (True, "")
    assert ground_fact_with_evidence("客户是护士", "working as a nurse", um) == (True, "")
    # 同语种且事实与原话零重叠 → 丢（旧口径）
    assert ground_fact_with_evidence("客户喜欢猫", "住在 Cebu", um) == (
        False, DROP_FACT_UNANCHORED)


def test_ground_fact_items_batch_and_reason_summary():
    um = "I have a daughter and she is my greatest treasure"
    kept, dropped = ground_fact_items([
        {"fact": "客户有一个女儿", "evidence": "I have a daughter"},
        {"fact": "客户想去大阪玩", "evidence": "想去大阪玩"},
        {"fact": "客户明天不用上班", "evidence": ""},
        {"fact": "", "evidence": "x"},          # 空事实跳过
        "客户是护士",                            # 裸字符串 → 无引文
    ], um)
    assert [k["text"] for k in kept] == ["客户有一个女儿"]
    assert kept[0]["evidence"] == "I have a daughter"
    reasons = summarize_drop_reasons(dropped)
    assert reasons == {DROP_EVIDENCE_MISMATCH: 1, DROP_NO_EVIDENCE: 2}
    assert ground_fact_items([], um) == ([], [])


def test_ai_client_evidence_ground_wrapper():
    """AIClient._ground_extracted_fact_items：过滤 + 丢弃原因回传 + 输出形状 {fact, evidence}。"""
    from src.ai.ai_client import AIClient

    class _Stub:
        logger = __import__("logging").getLogger("t")
        _ground_extracted_fact_items = AIClient._ground_extracted_fact_items
        _ground_extracted_facts = AIClient._ground_extracted_facts

    s = _Stub()
    out = s._ground_extracted_fact_items([
        {"fact": "客户有一个女儿", "evidence": "I have a daughter"},
        {"fact": "客户想去大阪玩", "evidence": "想去大阪玩"},
    ], "I have a daughter and she is my greatest treasure")
    assert out["candidates"] == 2
    assert out["facts"] == [{"fact": "客户有一个女儿", "evidence": "I have a daughter"}]
    assert out["dropped"] == [
        {"fact": "客户想去大阪玩", "evidence": "想去大阪玩", "reason": DROP_EVIDENCE_MISMATCH}]
    assert s._ground_extracted_fact_items([], "x") == {"facts": [], "dropped": [], "candidates": 0}


def test_ai_client_parses_structured_and_legacy_fact_json():
    from src.ai.ai_client import AIClient

    class _Stub:
        _MEMORY_IDENTITY_FILTERS = AIClient._MEMORY_IDENTITY_FILTERS
        _parse_memory_fact_items = AIClient._parse_memory_fact_items
        _parse_memory_facts_json = AIClient._parse_memory_facts_json

    s = _Stub()
    raw = ('```json\n{"facts":[{"fact":"客户有一个女儿","evidence":"I have a daughter"},'
           '{"text":"客户25岁","quote":"I\'m 25"},"客户住在宿务",'
           '{"fact":"用户称呼助手为小美","evidence":"hi 小美"}]}\n```')
    items = s._parse_memory_fact_items(raw)
    assert items == [
        {"fact": "客户有一个女儿", "evidence": "I have a daughter"},
        {"fact": "客户25岁", "evidence": "I'm 25"},
        {"fact": "客户住在宿务", "evidence": ""},     # 旧格式 → 无引文（护栏按 no_evidence 丢）
    ]                                                   # 助手身份事实被过滤
    assert s._parse_memory_facts_json(raw) == ["客户有一个女儿", "客户25岁", "客户住在宿务"]
    assert s._parse_memory_fact_items("not json") == []
    assert s._parse_memory_fact_items('{"facts": "x"}') == []


def test_extract_prompt_pins_evidence_rule():
    """抽取 prompt 必须要求逐字引文且禁止取自助手回复（措辞可改，语义锚不许丢）。"""
    import inspect
    from src.ai.ai_client import AIClient
    src = inspect.getsource(AIClient.extract_memory_facts)
    assert "引文铁律" in src
    assert "evidence" in src and "逐字" in src
    assert "不能取自 ASSISTANT" in src or "不得取自 ASSISTANT" in src


def test_grounding_eval_gate():
    """常驻门禁：跨语言 keep 一条不漏，Phase8/洗白 drop 一条不漏网。"""
    from src.eval.memory_extract_eval import (
        evaluate_evidence_grounding, format_grounding_report, load_grounding_samples,
    )
    samples = load_grounding_samples()
    assert len(samples) >= 10
    rep = evaluate_evidence_grounding(samples)
    assert rep["passed"], "\n接地护栏评测未过：\n" + format_grounding_report(rep)
    assert rep["summary"]["leaked"] == 0
    assert rep["summary"]["lost"] == 0
    assert rep["summary"]["reason_mismatch"] == 0, format_grounding_report(rep)
    assert "记忆接地护栏报告" in format_grounding_report(rep)


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
