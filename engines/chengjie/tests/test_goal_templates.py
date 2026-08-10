"""营销目标「模板注册表」门禁（纯函数，零 IO）。

覆盖：
- 7 模板结构不变量：里程碑数 4/5、push_curve 与里程碑等长且全在 PUSH_LEVELS、
  intents 池键覆盖 0..N-1；acquire_and_convert 的 phase_days 相位表严格递增
  且末位=default_days（时间兑底/封顶的锚）；
- list_templates 公开形状不泄漏 intents 内部池，且返回副本（改返回值不脏注册表）；
- pick_intent crc32 确定性轮换（同目标同日恒定、跨日采样至少两种）+ {item}/{note}
  参数代入与缺参留白；
- pick_care_intent 确定性；push_for_milestone 越界夹取；milestone_label zh/en 与越界。
"""

from __future__ import annotations

from src.companion.goals.templates import (
    AUTONOMY_LEVELS,
    CARE_INTENTS,
    GOAL_STATUSES,
    PUSH_LEVELS,
    STAGE_ORDER,
    TEMPLATES,
    get_template,
    list_templates,
    milestone_label,
    pick_care_intent,
    pick_intent,
    push_for_milestone,
    template_ids,
)

EXPECTED_IDS = {
    "conversion_unlock", "conversion_subscribe", "relationship_stage",
    "relationship_intimacy", "engagement_reactivate", "acquire_and_convert",
    "retention_expand", "profile_discovery", "custom",
}

# 14 天采样窗：意图池最小 size=2，crc32 确定性下必然轮换出 ≥2 种（已实测）
_DAYS = [f"2026-07-{d:02d}" for d in range(1, 15)]


# ── 结构不变量 ──────────────────────────────────────────────────────────────

def test_exactly_nine_templates():
    assert set(TEMPLATES) == EXPECTED_IDS
    assert set(template_ids()) == EXPECTED_IDS


def test_every_template_structural_invariants():
    for tid, t in TEMPLATES.items():
        ms = t["milestones"]
        n = len(ms)
        assert n in (4, 5), tid          # 现有模板 4 段；获客转化漏斗 5 段
        for m in ms:
            assert m.get("id") and m.get("zh") and m.get("en"), tid
        curve = t["push_curve"]
        assert len(curve) == n, tid      # 曲线与里程碑一一对应
        assert all(lvl in PUSH_LEVELS for lvl in curve), tid
        assert set(t["intents"].keys()) == set(range(n)), tid
        for pool in t["intents"].values():
            assert pool and all(isinstance(s, str) and s for s in pool), tid
        assert t["kind"] and t["name_zh"] and t["name_en"], tid
        assert int(t["default_days"]) > 0, tid
        phase = t.get("phase_days")
        if phase is not None:            # 相位表：与里程碑等长、严格递增、末位=默认天数
            assert len(phase) == n, tid
            assert all(phase[i] < phase[i + 1] for i in range(n - 1)), tid
            assert int(phase[-1]) == int(t["default_days"]), tid


def test_shared_vocabularies():
    assert PUSH_LEVELS == ("none", "soft", "direct")
    assert AUTONOMY_LEVELS == ("observe", "suggest", "auto")
    assert set(GOAL_STATUSES) == {
        "active", "paused", "done", "failed", "expired", "cancelled"}
    assert STAGE_ORDER[0] == "initial" and STAGE_ORDER[-1] == "converted"
    assert len(CARE_INTENTS) >= 2


def test_get_template_hit_strip_and_miss():
    assert get_template("custom") is TEMPLATES["custom"]
    assert get_template(" custom ") is TEMPLATES["custom"]
    assert get_template("no_such") is None
    assert get_template("") is None
    assert get_template(None) is None


def test_list_templates_public_shape_no_intents_leak():
    out = list_templates()
    assert len(out) == 9
    assert {e["id"] for e in out} == EXPECTED_IDS
    for e in out:
        assert "intents" not in e, e["id"]
        assert {"id", "name_zh", "name_en", "kind", "default_days",
                "params", "milestones"} <= set(e)
        assert len(e["milestones"]) in (4, 5)


def test_list_templates_returns_copies_not_registry_refs():
    out = list_templates()
    e = next(x for x in out if x["id"] == "custom")
    e["params"][0]["key"] = "hacked"
    e["milestones"][0]["zh"] = "hacked"
    assert TEMPLATES["custom"]["params"][0]["key"] == "note"
    assert TEMPLATES["custom"]["milestones"][0]["zh"] == "起步"


# ── pick_intent：确定性轮换 + 参数代入 ──────────────────────────────────────

def test_pick_intent_deterministic_same_goal_same_day():
    t = TEMPLATES["conversion_unlock"]
    a = pick_intent(t, 0, "goal-x", "2026-07-01")
    b = pick_intent(t, 0, "goal-x", "2026-07-01")
    assert a == b
    assert a in t["intents"][0]      # 里程碑 0 池无占位符 → 原样命中池内

def test_pick_intent_rotates_across_days():
    t = TEMPLATES["conversion_unlock"]
    seen = {pick_intent(t, 0, "goal-rot", d) for d in _DAYS}
    assert len(seen) >= 2


def test_pick_intent_clamps_milestone_and_handles_empty():
    t = TEMPLATES["conversion_unlock"]
    assert pick_intent(t, 99, "g", "2026-07-01") in t["intents"][3]   # 上越界夹到末段
    assert pick_intent(t, -5, "g", "2026-07-01") in t["intents"][0]   # 下越界夹到 0
    assert pick_intent({}, 0, "g", "2026-07-01") == ""
    assert pick_intent({"intents": {}}, 0, "g", "2026-07-01") == ""


def test_pick_intent_item_substitution_and_fallbacks():
    t = TEMPLATES["conversion_unlock"]
    out = pick_intent(t, 1, "g", "2026-07-01", params={"item_label": "八字详批"})
    assert "「八字详批」" in out and "{item}" not in out
    # item_label 缺省回落 item_id
    out2 = pick_intent(t, 1, "g", "2026-07-01", params={"item_id": "bazi_reading"})
    assert "「bazi_reading」" in out2 and "{item}" not in out2
    # 全缺 → 留白词「它」，不留花括号
    out3 = pick_intent(t, 1, "g", "2026-07-01")
    assert "「它」" in out3 and "{item}" not in out3


def test_pick_intent_note_substitution_and_fallback():
    t = TEMPLATES["custom"]
    out = pick_intent(t, 0, "g", "2026-07-01", params={"note": "推广新品"})
    assert "「推广新品」" in out and "{note}" not in out
    out2 = pick_intent(t, 0, "g", "2026-07-01", params={})
    assert "「这个目标」" in out2 and "{note}" not in out2


def test_pick_care_intent_deterministic_and_rotates():
    a = pick_care_intent("goal-x", "2026-07-01")
    assert a == pick_care_intent("goal-x", "2026-07-01")
    assert a in CARE_INTENTS
    seen = {pick_care_intent("goal-x", d) for d in _DAYS}
    assert len(seen) >= 2


# ── push_for_milestone / milestone_label ────────────────────────────────────

def test_profile_discovery_shape():
    """P26 摸底模板结构钉：profile_slots 能力位 + 缺口从里程碑 0 起 +
    曲线 soft 起步 + 默认勾选含 age/occupation（「获取年龄职业」开箱即用）。"""
    t = TEMPLATES["profile_discovery"]
    assert t["profile_slots"] is True
    assert t["gap_from_milestone"] == 0
    assert [push_for_milestone(t, i) for i in range(4)] == [
        "soft", "soft", "direct", "soft"]
    slots_param = next(p for p in t["params"] if p["key"] == "slots")
    assert "age" in slots_param["default"]
    assert "occupation" in slots_param["default"]
    # 每段意图池 ≥2 条（单条池=同一里程碑期间每天同一句）
    assert all(len(pool) >= 2 for pool in t["intents"].values())


def test_custom_intent_pools_expanded():
    """P26：custom 每段池 ≥2 条（crc32 轮换只在池内生效，单条池=复读机）。"""
    assert all(len(pool) >= 2
               for pool in TEMPLATES["custom"]["intents"].values())


def test_custom_curve_never_starts_none():
    """P25（2026-08-05）回归钉：custom 里程碑纯时间驱动，首段若为 none，
    14 天目标头 3~4 天注入块全程「营销内容只字不提」——坐席实测「设了目标
    AI 完全不往那个方向聊」的软根因。custom 是坐席显式写下的方向，
    起步必须至少 soft（顺势自然带到）。"""
    t = TEMPLATES["custom"]
    assert push_for_milestone(t, 0) == "soft"
    assert [push_for_milestone(t, i) for i in range(4)] == [
        "soft", "soft", "direct", "soft"]


def test_push_for_milestone_curve_and_clamps():
    t = TEMPLATES["conversion_unlock"]
    assert [push_for_milestone(t, i) for i in range(4)] == [
        "none", "soft", "direct", "soft"]
    assert push_for_milestone(t, -3) == "none"    # 下越界 → 首段
    assert push_for_milestone(t, 99) == "soft"    # 上越界 → 末段
    assert push_for_milestone({}, 0) == "soft"    # 无曲线 → soft 兜底
    assert push_for_milestone({"push_curve": ("bogus",)}, 0) == "soft"  # 非法值兜底


def test_milestone_label_zh_en_and_clamps():
    t = TEMPLATES["conversion_unlock"]
    assert milestone_label(t, 0) == "破冰回暖"
    assert milestone_label(t, 0, "en") == "Reconnect"
    assert milestone_label(t, 0, "en-US") == "Reconnect"   # en 前缀即认
    assert milestone_label(t, 99) == "跟进收口"            # 越界夹到末段
    assert milestone_label(t, -2) == "破冰回暖"
    assert milestone_label({}, 0) == ""
