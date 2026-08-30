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
    CARE_PAIRS,
    GOAL_STATUSES,
    PUSH_LEVELS,
    STAGE_ORDER,
    TEMPLATES,
    get_template,
    intent_en_for,
    list_templates,
    milestone_label,
    pick_care_intent,
    pick_intent,
    pick_sprint_intent,
    push_for_milestone,
    template_ids,
)


def _has_cjk(s: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)

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
        # i18n P0（2026-08-19）：意图池条目必须是 (zh, en) 双语对——zh 是
        # planner/prompt 权威文案（含 CJK），en 是 UI 英文态展示（零 CJK）。
        # 漏写 en ＝ 英文界面「今日节拍」直接漏中文（本门禁的由来）。
        for pool in t["intents"].values():
            assert pool, tid
            for entry in pool:
                assert isinstance(entry, tuple) and len(entry) == 2, (tid, entry)
                zh, en = entry
                assert isinstance(zh, str) and zh and _has_cjk(zh), (tid, zh)
                assert isinstance(en, str) and en and not _has_cjk(en), (tid, en)
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
        assert "intents_sprint" not in e, e["id"]
        assert {"id", "name_zh", "name_en", "kind", "default_days",
                "params", "milestones"} <= set(e)
        assert "sprint_ok" in e
        if e["id"] in ("custom", "conversion_unlock", "conversion_subscribe",
                       "acquire_and_convert"):
            assert e["sprint_ok"] is True
        else:
            assert e["sprint_ok"] is False
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
    # 里程碑 0 池无占位符 → 原样命中池内 zh 臂（返回值恒为中文权威文案）
    assert a in [e[0] for e in t["intents"][0]]

def test_pick_intent_rotates_across_days():
    t = TEMPLATES["conversion_unlock"]
    seen = {pick_intent(t, 0, "goal-rot", d) for d in _DAYS}
    assert len(seen) >= 2


def test_pick_intent_clamps_milestone_and_handles_empty():
    t = TEMPLATES["conversion_unlock"]
    zh0 = [e[0] for e in t["intents"][0]]
    zh3 = [e[0] for e in t["intents"][3]]
    # 3 号池含 {item} 占位符 → 比对代入留白词后的渲染集
    zh3_rendered = [s.replace("{item}", "它") for s in zh3]
    assert pick_intent(t, 99, "g", "2026-07-01") in zh3_rendered   # 上越界夹到末段
    assert pick_intent(t, -5, "g", "2026-07-01") in zh0            # 下越界夹到 0
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


# ── 限时意图池（P1 2026-08-29）：按拍序取，避免日历话术进 60 分钟目标 ────────

def test_sprint_intent_pools_structure_bilingual():
    """sprint-ok 模板必须带 intents_sprint（键 0/1/2，条目 (zh,en) 双语对、
    每段 ≥2 条防复读）；非 sprint 模板不得声明（声明了也没人消费=死数据）。"""
    from src.companion.goals.pace import SPRINT_OK

    for tid in SPRINT_OK:
        pools = TEMPLATES[tid].get("intents_sprint")
        assert pools and set(pools.keys()) == {0, 1, 2}, tid
        for pool in pools.values():
            assert len(pool) >= 2, tid
            for entry in pool:
                assert isinstance(entry, tuple) and len(entry) == 2, (tid, entry)
                zh, en = entry
                assert isinstance(zh, str) and zh and _has_cjk(zh), (tid, zh)
                assert isinstance(en, str) and en and not _has_cjk(en), (tid, en)
    for tid, t in TEMPLATES.items():
        if tid not in SPRINT_OK:
            assert "intents_sprint" not in t, tid


def test_pick_sprint_intent_deterministic_substitution_and_clamps():
    t = TEMPLATES["custom"]
    p = {"note": "留联系方式"}
    a = pick_sprint_intent(t, 0, "g-s", "s:1000", params=p)
    assert a == pick_sprint_intent(t, 0, "g-s", "s:1000", params=p)
    assert "「留联系方式」" in a and "{note}" not in a
    # 上越界夹到末段（收口拍）；下越界夹到 0
    zh2 = [e[0].replace("{note}", "留联系方式") for e in t["intents_sprint"][2]]
    assert pick_sprint_intent(t, 99, "g", "s:1", params=p) in zh2
    assert pick_sprint_intent(t, -1, "g", "s:1", params=p) != ""
    # 无限时池的模板 / 空模板 → ""（调用方保留日历池意图）
    assert pick_sprint_intent(TEMPLATES["relationship_stage"], 0, "g", "s:1") == ""
    assert pick_sprint_intent({}, 0, "g", "s:1") == ""


def test_pick_sprint_intent_rotates_across_slots():
    t = TEMPLATES["conversion_unlock"]
    seen = {pick_sprint_intent(t, 1, "g-rot", f"s:{i}",
                               params={"item_label": "详批"}) for i in range(14)}
    assert len(seen) >= 2


def test_sprint_intents_avoid_calendar_wording():
    """限时池文案不得出现「隔天/改天/明天/每天」类日历词——限时档存在的
    全部理由就是这轮/今天收口，日历话术漏进来＝功能自相矛盾。"""
    for tid in ("custom", "conversion_unlock", "conversion_subscribe",
                "acquire_and_convert"):
        for pool in TEMPLATES[tid]["intents_sprint"].values():
            for zh, _en in pool:
                for bad in ("隔天", "改天", "明天", "每天", "分天"):
                    assert bad not in zh, (tid, zh)


def test_intent_en_for_covers_sprint_pools():
    params = {"item_label": "八字详批", "note": "推广新品"}
    for tid in ("custom", "conversion_unlock", "conversion_subscribe",
                "acquire_and_convert"):
        t = TEMPLATES[tid]
        for bi in (0, 1, 2):
            for i in range(6):
                zh = pick_sprint_intent(t, bi, f"g-{tid}", f"s:{i}",
                                        params=params)
                en = intent_en_for(t, params, zh)
                assert en, (tid, bi, zh)
                assert not _has_cjk(
                    en.replace("八字详批", "").replace("推广新品", "")), (tid, en)


def test_pick_care_intent_deterministic_and_rotates():
    a = pick_care_intent("goal-x", "2026-07-01")
    assert a == pick_care_intent("goal-x", "2026-07-01")
    assert a in CARE_INTENTS
    seen = {pick_care_intent("goal-x", d) for d in _DAYS}
    assert len(seen) >= 2


# ── intent_en_for：中文权威文案 → 英文展示态（i18n P0） ─────────────────────

def test_intent_en_for_roundtrip_every_pool_entry():
    """全池反查闭环：任一 pick 出的中文意图都能查到英文对应文案且零 CJK。"""
    for tid, t in TEMPLATES.items():
        params = {"item_label": "八字详批", "note": "推广新品"}
        for mi in t["intents"]:
            for d in _DAYS:
                zh = pick_intent(t, mi, f"g-{tid}", d, params=params)
                en = intent_en_for(t, params, zh)
                assert en, (tid, mi, zh)
                assert not _has_cjk(en.replace("八字详批", "").replace("推广新品", "")), (tid, en)


def test_intent_en_for_keeps_operator_data_verbatim():
    """{note}/{item} 是运营手输数据：英文句里原样保留，不翻译。"""
    t = TEMPLATES["custom"]
    zh = pick_intent(t, 0, "g", "2026-07-01", params={"note": "推广新品"})
    en = intent_en_for(t, {"note": "推广新品"}, zh)
    assert "推广新品" in en


def test_intent_en_for_covers_care_pairs_and_misses_fall_back_empty():
    care_zh = pick_care_intent("goal-x", "2026-07-01")
    en = intent_en_for(TEMPLATES["custom"], {}, care_zh)
    assert en and not _has_cjk(en)
    assert intent_en_for(TEMPLATES["custom"], {}, "人工改写过的意图") == ""
    assert intent_en_for(TEMPLATES["custom"], {}, "") == ""
    assert len(CARE_PAIRS) == len(CARE_INTENTS)


def test_intent_en_blank_params_use_english_fillers():
    """缺参时英文臂用英文留白词（"it"/"this goal"），不带中文留白。"""
    t = TEMPLATES["conversion_unlock"]
    zh = pick_intent(t, 1, "g", "2026-07-01")           # {item} 缺参 → 「它」
    en = intent_en_for(t, {}, zh)
    assert en and not _has_cjk(en)


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
