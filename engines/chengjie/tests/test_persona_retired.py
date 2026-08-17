# -*- coding: utf-8 -*-
"""撤销旧设定结构化核心门禁（P2 期，2026-08-04）。

覆盖：双形态条目规范化（str / {text,added,terms}）、prompt 文案口径、
守卫锚词并集、条目年龄、prompt 泄漏检查、写边界复活检测（含「钉子自身
含锚词不算冲突」的排除语义）、以及 persona_guard 的「第一人称认领」
出站兜底（否定澄清/聊客户的猫绝不误伤——钉子要求的正确行为不能被守卫剥掉）。
"""
from src.utils.persona_guard import find_violations, sanitize
from src.utils.persona_retired import (
    entry_age_days,
    normalize_retired_entries,
    prompt_leaks,
    retired_conflicts,
    retired_guard_terms,
    retired_prompt_items,
)


def _p(retired):
    return {"name": "小雨", "role": "大学生",
            "boundaries": {"retired_facts": retired}}


# ── 条目规范化（宽进严出）────────────────────────────────────────────────────

def test_normalize_mixed_forms():
    entries = normalize_retired_entries(_p([
        "纯文案条目",
        {"text": "养猫（已删）", "added": "2026-08-03", "terms": ["猫", "猫咖", "猫"]},
        {"text": "  ", "terms": ["空文案该丢弃"]},
        {"added": "2026-08-03"},          # 无文案 → 丢弃
        {"text": "非法日期", "added": "昨天", "terms": "单串也行"},
        123,                               # 非法类型 → 丢弃
    ]))
    assert [e["text"] for e in entries] == ["纯文案条目", "养猫（已删）", "非法日期"]
    assert entries[0] == {"text": "纯文案条目", "added": "", "terms": []}
    assert entries[1]["added"] == "2026-08-03"
    assert entries[1]["terms"] == ["猫", "猫咖"]          # 去重保序
    assert entries[2]["added"] == ""                      # 非法日期归空
    assert entries[2]["terms"] == ["单串也行"]


def test_normalize_tolerates_bad_shapes():
    assert normalize_retired_entries(None) == []
    assert normalize_retired_entries({}) == []
    assert normalize_retired_entries({"boundaries": "not a dict"}) == []
    assert normalize_retired_entries(_p("单条字符串")) == [
        {"text": "单条字符串", "added": "", "terms": []}]
    assert normalize_retired_entries(_p({"text": "单条 dict", "terms": ["a"]})) == [
        {"text": "单条 dict", "added": "", "terms": ["a"]}]


def test_prompt_items_and_guard_terms():
    p = _p(["legacy 文案",
            {"text": "养猫已删", "terms": ["猫", "毛豆"]},
            {"text": "猫咖已删", "terms": ["猫咖", "猫"]}])
    assert retired_prompt_items(p) == ["legacy 文案", "养猫已删", "猫咖已删"]
    # 锚词并集去重保序；legacy 条目无锚词=不参与守卫（软降级，如实）
    assert retired_guard_terms(p) == ["猫", "毛豆", "猫咖"]


def test_entry_age_days():
    import datetime as _dt
    import time as _time
    today = _dt.date.fromtimestamp(_time.time())
    d3 = (today - _dt.timedelta(days=3)).isoformat()
    assert entry_age_days({"added": d3}) == 3
    assert entry_age_days({"added": ""}) is None
    assert entry_age_days({"added": "not-a-date"}) is None
    assert entry_age_days({}) is None


# ── prompt 泄漏检查（验证生效的确定性内核）──────────────────────────────────

def test_prompt_leaks_basic():
    text = "你是小雨。\n【你的兴趣爱好】撸猫、煮茶。\n回复要短。"
    leaks = prompt_leaks(text, ["猫", "狗"])
    assert len(leaks) == 1 and leaks[0]["term"] == "猫"
    assert "撸猫" in leaks[0]["snippet"]
    assert prompt_leaks(text, ["拍立得"]) == []
    assert prompt_leaks("", ["猫"]) == []
    assert prompt_leaks(text, []) == []


def test_prompt_leaks_latin_case_insensitive():
    assert prompt_leaks("scene: Cat Cafe by the window", ["cat"])[0]["term"] == "cat"


# ── 写边界复活检测 ───────────────────────────────────────────────────────────

def test_retired_conflicts_detects_resurrection():
    p = _p([{"text": "养猫已删", "added": "2026-08-03", "terms": ["猫"]}])
    p["context"] = {"hobbies": ["撸学校后门的流浪猫"]}       # 丰富管线把猫写回来了
    hits = retired_conflicts(p)
    assert any(h["path"] == "context.hobbies[0]" and h["term"] == "猫" for h in hits)
    # 钉子自身的文案含「猫」是设计使然，绝不能算冲突
    assert not any(h["path"].startswith("boundaries.retired_facts") for h in hits)


def test_retired_conflicts_clean_and_no_terms():
    clean = _p([{"text": "养猫已删", "terms": ["猫"]}])
    clean["context"] = {"hobbies": ["煮茶"]}
    assert retired_conflicts(clean) == []
    legacy_only = _p(["纯文案没有锚词"])
    legacy_only["context"] = {"hobbies": ["撸猫"]}
    assert retired_conflicts(legacy_only) == []              # 无锚词=不检测（如实降级）


# ── persona_guard 出站兜底：只拦第一人称认领，绝不误伤 ──────────────────────

_GUARD_P = {
    "name": "小雨",
    "boundaries": {"retired_facts": [
        {"text": "养猫已删", "added": "2026-08-03", "terms": ["猫"]},
    ]},
}


def test_guard_strips_first_person_claim():
    text = "刚下课啦。我家猫今天特别黏人。你吃饭了吗？"
    cleaned, violations = sanitize(text, _GUARD_P)
    assert violations, "第一人称认领必须命中"
    assert "我家猫" not in cleaned
    assert "你吃饭了吗" in cleaned                     # 只剥违规句，其余保留


def test_guard_strips_claim_in_single_sentence_reply():
    """真机实测缺口（2026-08-04）：单句回复只有逗号——句级剥离会把整段删光
    触发「回退原文」，认领句原样出站。子句级降级必须接住这一类。"""
    text = "刚下课～我家猫今天特别黏人，你吃了吗？"
    cleaned, violations = sanitize(text, _GUARD_P)
    assert violations
    assert "我家猫" not in cleaned
    assert "你吃了吗" in cleaned
    # 整段全是认领、无处可剥 → 如实回退原文（由调用方记日志），绝不返回空
    all_claim = "我家猫超可爱"
    cleaned2, violations2 = sanitize(all_claim, _GUARD_P)
    assert violations2 and cleaned2 == all_claim


def test_guard_keeps_denial():
    """否定澄清是钉子要求的正确行为——守卫剥掉它=自己打自己。"""
    text = "我没有养猫呀，你是不是记成别人啦哈哈"
    cleaned, violations = sanitize(text, _GUARD_P)
    assert cleaned == text
    assert violations == []


def test_guard_keeps_customer_topic():
    """聊客户的猫是正常社交（撤销语义只禁认领，不禁话题）。"""
    text = "你家的猫真可爱，看着就想rua一把"
    cleaned, violations = sanitize(text, _GUARD_P)
    assert cleaned == text
    assert violations == []


def test_guard_noop_without_terms():
    legacy = {"name": "小雨", "boundaries": {"retired_facts": ["纯文案无锚词"]}}
    text = "我家猫今天很乖"
    cleaned, violations = sanitize(text, legacy)
    assert cleaned == text and violations == []
    no_pin = {"name": "小雨"}
    cleaned2, violations2 = sanitize(text, no_pin)
    assert cleaned2 == text and violations2 == []


def test_guard_preference_claim():
    text = "哈哈我超喜欢猫的！改天一起去撸"
    assert find_violations(text, _GUARD_P), "第一人称偏好认领也该命中"
