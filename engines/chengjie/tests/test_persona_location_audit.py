# -*- coding: utf-8 -*-
"""实施53 P2-1 人设时区底账（persona_location_audit）纯函数门禁。

钉三件事：① location 状态分类与红黄项语义（显式 vs 文本推断 vs 无/禁用；
文本城市与 location **时区级**不一致才 RED，同钟只 WARN——红项是「LLM 看得到
的档案自相矛盾」清单，误报会让运营不再信它）；② 海外钟决策表（绑定会话数/
平台默认位聚合）；③ 两地时差纯函数与服务器时区无关（跨机器确定）。
"""
from src.companion.persona_location import resolve_persona_place
from tools.persona_location_audit import audit_personas, place_gap_hours

VAN = resolve_persona_place({"location": "vancouver"})
SHA = resolve_persona_place({"location": "shanghai"})
SZX = resolve_persona_place({"location": "shenzhen"})
HKG = resolve_persona_place({"location": "hong_kong"})


def _fake_offset(place):
    """机器无关的假「相对服务器」偏移：温哥华 -15h，其余 0。"""
    return -15.0 if place is not None and place.slug == "vancouver" else 0.0


_PROFILES = {
    "lin_far": {
        "name": "林佳欣", "location": "vancouver",
        "role": "温哥华注册护士",
    },
    "p_mismatch": {
        "name": "阿澈", "location": "vancouver",
        "role": "住在上海的自由插画师",
    },
    "p_warn": {
        "name": "深深", "location": "shenzhen",
        "role": "经常往返香港的设计师",
    },
    "p_inferred": {
        "name": "小旅", "background": "长居温哥华，爱摄影",
    },
    "p_none": {"name": "客服"},
    "p_disabled": {"name": "关地点", "location": "none"},
}


def _audit():
    return audit_personas(
        _PROFILES,
        ref_bindings={
            "telegram:a:1": "lin_far",
            "telegram:a:2": "lin_far",
            "whatsapp:b:1": "p_none",
        },
        platform_defaults={"telegram": "p_mismatch", "whatsapp": ""},
        far_threshold=3.0,
        offset_fn=_fake_offset,
    )


def test_place_gap_hours_server_independent():
    assert place_gap_hours(VAN, SHA) >= 10.0     # 温哥华↔上海跨太平洋
    assert place_gap_hours(SZX, HKG) == 0.0      # 同钟
    assert place_gap_hours(None, SHA) == 0.0     # 缺地即 0（不判）


def test_red_only_when_timezone_level_mismatch():
    out = _audit()
    reds = {r["pid"]: r for r in out["reds"]}
    assert set(reds) == {"p_mismatch"}
    assert "上海" in reds["p_mismatch"]["text_place"]
    assert reds["p_mismatch"]["gap_h"] >= 3.0
    warns = {w["pid"]: w for w in out["warns"]}
    assert set(warns) == {"p_warn"}              # 深圳×香港同钟 → 只 WARN
    assert "香港" in warns["p_warn"]["text_place"]
    # 文本城市 == location（温哥华护士写温哥华）绝不误报
    rows = {r["pid"]: r for r in out["personas"]}
    assert not any(f.startswith(("RED", "WARN")) for f in rows["lin_far"]["flags"])


def test_loc_state_classification_and_info_flags():
    rows = {r["pid"]: r for r in _audit()["personas"]}
    assert rows["lin_far"]["loc_state"] == "explicit"
    assert rows["p_inferred"]["loc_state"] == "inferred"
    assert "INFO:inferred_from_text" in rows["p_inferred"]["flags"]
    assert "温哥华" in rows["p_inferred"]["place"]   # 生产真按推断钟跑，必须可见
    assert rows["p_none"]["loc_state"] == "none"
    assert "INFO:server_clock" in rows["p_none"]["flags"]
    assert rows["p_disabled"]["loc_state"] == "disabled"   # 显式 "none"=禁用
    assert "INFO:server_clock" in rows["p_disabled"]["flags"]


def test_far_table_aggregates_bindings_and_defaults():
    out = _audit()
    far = {r["pid"]: r for r in out["far"]}
    # 假偏移下所有温哥华人设（显式两个 + 推断一个）都在海外钟表里
    assert set(far) == {"lin_far", "p_mismatch", "p_inferred"}
    assert far["lin_far"]["bound_convs"] == 2            # ref_bindings 聚合
    assert far["p_mismatch"]["default_platforms"] == ["telegram"]
    s = out["summary"]
    assert s["total"] == 6 and s["far_tz"] == 3
    assert s["bound_on_far_tz"] == 2                     # 挂在海外钟上的会话数
    assert s["reds"] == 1 and s["warns"] == 1


def test_default_offset_fn_smoke():
    out = audit_personas({"x": {"name": "x"}}, {}, {})
    assert out["summary"]["total"] == 1                  # 生产 offset 口径不炸
