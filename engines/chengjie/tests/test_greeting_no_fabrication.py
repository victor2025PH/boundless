"""#208（L-1 C，2026-09-06）：问候不编事实 + greeting 分类收紧。

FTK6S7 全链：steven→cinderella 15:03 一句问候，15:04 AI 答「Just got back from a walk,
the rain's light here」——档案无雨、无天气源、KB 跳过、记忆 0 条；编造进
_conversation_history 后被当事实反复提。同号「I want to buy vitamins」「Send me the
receipt」也被标 greeting。

四道钉子：
1. 问候 prompt 硬约束（ai_client._get_intent_prompt，companion / 通用两条都带）；
2. 出口守卫 strip_status_claims：无来源的自指近况句剥离；有来源（天气源 / 场景 /
   人设档案 / 记忆 / 客户原话）不动；问对方近况不动；AI 自己的历史句**不算来源**；
   剥空回落原文并标 all_stripped；
3. 历史里 AI 自己的无来源近况 → prompt 注入「随口一说，不延续」说明行；
4. 分类：交易 / 收据 / 购买 / 媒体请求不判 greeting；≥20 字陈述句不再兜底判 greeting。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from src.utils.greeting_lexicon import has_request_context, is_greeting_message
from src.utils.proactive_fabrication_guard import (
    build_status_evidence,
    history_status_claim_note,
    is_self_status_claim,
    persona_status_facts,
    strip_status_claims,
)

ROOT = Path(__file__).resolve().parents[1]
_SM = ROOT / "src" / "skills" / "skill_manager.py"
_AI = ROOT / "src" / "ai" / "ai_client.py"

_FTK = "Just got back from a walk, the rain's light here. How's your day going?"
_EMPTY_EV = {"weather": False, "scene": False, "texts": []}


# ── 1. 问候 prompt 硬约束 ─────────────────────────────────────────────────────

def test_greeting_intent_prompt_forbids_fabrication_both_domains():
    from src.ai.ai_client import AIClient

    line = AIClient.GREETING_NO_FABRICATION_LINE
    assert "不得编造天气" in line and "Do not invent weather" in line

    def _dummy(cfg):
        # 只借 _get_intent_prompt 用到的两样：config + 类常量（不构造真 AIClient）
        return SimpleNamespace(config=cfg, GREETING_NO_FABRICATION_LINE=line,
                               _INTENT_SUPPLEMENTS=AIClient._INTENT_SUPPLEMENTS)

    # 通用域（config=None → 非 conversion）
    generic = AIClient._get_intent_prompt(_dummy(None), "greeting")
    assert generic and line in generic and "Intent: Greeting" in generic
    # 陪伴域（domain=conversion）
    comp = AIClient._get_intent_prompt(
        _dummy(SimpleNamespace(config={"domain": "conversion"})), "greeting")
    assert comp and line in comp and "情感陪伴" in comp
    # 其它意图不受影响
    assert line not in (AIClient._get_intent_prompt(_dummy(None), "complaint") or "")


# ── 2. 出口守卫 ───────────────────────────────────────────────────────────────

def test_self_status_claim_detection():
    assert set(is_self_status_claim("Just got back from a walk, the rain's light here.")) == {"weather", "trip", "activity"}
    assert is_self_status_claim("刚散完步回来，这边下着小雨。")
    assert is_self_status_claim("I'm at the gym rn, talk later 😊") == ["activity"]
    # 问对方 → 不是自述
    assert is_self_status_claim("Is it raining where you are?") == []
    assert is_self_status_claim("你那边下雨了吗？") == []
    assert is_self_status_claim("Hope the weather's nice for you today!") == []
    # 与近况无关的句子
    assert is_self_status_claim("Hey you 😊 how's your day going?") == []
    assert is_self_status_claim("在呀～怎么啦") == []


def test_ftk6s7_ungrounded_status_is_stripped():
    out, info = strip_status_claims(_FTK, _EMPTY_EV)
    assert out == "How's your day going?"
    assert info["status_stripped"] == ["Just got back from a walk, the rain's light here."]
    assert set(info["status_cats"]) >= {"weather"}
    assert info["all_stripped"] is False
    for w in ("rain", "walk"):
        assert w not in out.lower()


def test_all_stripped_falls_back_to_original_with_flag():
    txt = "Just got back from a walk, the rain's light here 😊"
    out, info = strip_status_claims(txt, _EMPTY_EV)
    assert out == txt and info["all_stripped"] is True and info["status_stripped"]


def test_grounded_status_claims_are_kept():
    # 天气源在场 → 天气类有据（AI 拿到的是真数据）
    ev = build_status_evidence({"_persona_weather_note": "当地此刻小雨 22°C"}, "hi")
    out, info = strip_status_claims("It's drizzling here, cozy day. How are you?", ev)
    assert "drizzling" in out and not info["status_stripped"]
    # 场景状态在场 → 地点/活动有据
    ev = build_status_evidence({"_current_scene_note": "【当前场景】你在健身房刚练完"}, "hi")
    out, info = strip_status_claims("Just finished at the gym, dead tired 😂", ev)
    assert not info["status_stripped"]
    # 客户自己说了下雨 → AI 接话有据
    ev = build_status_evidence({}, "it's raining so hard here today")
    out, info = strip_status_claims("Ugh, raining here too. Stay dry!", ev)
    assert not info["status_stripped"]
    # 人设档案写明日常（中文）→ 英文说 walk 有据（类别级跨语言）
    persona = {"background": "住在宿务，每天傍晚沿海边散步", "context": {"hobbies": ["散步", "做饭"]}}
    ev = build_status_evidence({}, "hi")
    ev["texts"].append(persona_status_facts(persona))
    out, info = strip_status_claims("Just got back from my walk 😊 what are you up to?", ev)
    assert not info["status_stripped"]


def test_ai_own_history_is_not_evidence_but_user_side_is():
    hist = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Just got back from a walk, the rain's light here."},
    ]
    ev = build_status_evidence({"_conversation_history": hist, "last_reply": hist[1]["content"]}, "how are you")
    assert not ev["weather"] and not ev["scene"]
    assert all("rain" not in t for t in ev["texts"]), "AI 自己的编造不得成为下一次的依据"
    # 再提同一近况 → 仍剥
    out, info = strip_status_claims("Still raining here, just chilling after my walk. You?", ev)
    assert info["status_stripped"]
    # 客户说过（user 侧）→ 有据
    ev2 = build_status_evidence({"_conversation_history": [
        {"role": "user", "content": "it's been raining all day here"}]}, "and you?")
    out2, info2 = strip_status_claims("Raining here as well, staying in.", ev2)
    assert not info2["status_stripped"]


def test_history_status_claim_note_only_when_ai_fabricated():
    ctx = {"_conversation_history": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Just got back from a walk, the rain's light here."},
    ]}
    note = history_status_claim_note(ctx)
    assert "随口一说" in note and "offhand remark" in note
    # 有天气源 → 不算编造 → 不注入
    assert history_status_claim_note({**ctx, "_persona_weather_note": "小雨"}) == ""
    # 历史里 AI 没说过近况 → 空
    assert history_status_claim_note({"_conversation_history": [
        {"role": "assistant", "content": "在呀～怎么啦"}]}) == ""
    assert history_status_claim_note({}) == ""


def test_guard_wired_into_both_reply_chains_and_prompt():
    sm = _SM.read_text(encoding="utf-8")
    assert sm.count("self._apply_status_fabrication_guard(") == 2, "A 线 5c2d + B 线 9b3 各接一次"
    assert 'source="a_line"' in sm and 'source="b_line"' in sm
    ai = _AI.read_text(encoding="utf-8")
    assert "history_status_claim_note(context)" in ai


# ── 4. 分类收紧 ───────────────────────────────────────────────────────────────

def test_request_texts_are_not_greetings():
    for t in ("Send me the receipt", "I want to buy vitamins", "hi send me a pic",
              "hello how much is it", "发我一张照片", "你好 我要买维生素", "在吗 转账给我"):
        assert has_request_context(t), t
        assert is_greeting_message(t) is False, t
    for t in ("hi", "hello", "good morning", "在吗", "哈喽", "hey you"):
        assert is_greeting_message(t) is True, t


def _dummy_sm():
    return SimpleNamespace(skills={}, intent_keywords={}, intent_patterns={})


def test_recognize_intent_no_longer_defaults_long_statements_to_greeting():
    from src.skills.skill_manager import SkillManager

    rec = SkillManager._recognize_intent
    assert rec(_dummy_sm(), "hi") == "greeting"
    assert rec(_dummy_sm(), "good morning") == "greeting"
    assert rec(_dummy_sm(), "I want to buy vitamins") == "direct_chat"
    assert rec(_dummy_sm(), "Send me the receipt") == "direct_chat"       # 19 字：以前落 small_talk（S5 可能静默）
    assert rec(_dummy_sm(), "I stayed home the whole afternoon and slept") == "direct_chat"  # ≥20 字陈述：以前兜底 greeting
    assert rec(_dummy_sm(), "miss you a lot") == "small_talk"
