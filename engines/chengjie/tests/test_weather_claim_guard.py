# -*- coding: utf-8 -*-
"""#104（实施91）出站天气断言守卫门禁。

实锤（0831 原图 860，LINE Kevin 会话）：客户发下雨视频，AI 回「It's drizzling
lightly on my side too / 我这边也在下着小雨」——人设所在地真实天气零事实来源，
纯共情镜像编造。不变量：
- 无天气事实（bucket 空）→ 第一人称本地天气现象断言整句剥除；
- 有事实 → 只剥与 bucket 相斥的断言（说雨实际晴），一致断言放行；
- 问对方天气/聊天气常识/非第一人称锚 → 零误伤。
"""

from __future__ import annotations

import inspect

from src.companion.weather_state import strip_weather_claim_conflicts

INCIDENT_ZH = "我这边也在下着小雨，和你那边一样呢。"
INCIDENT_EN = "It's drizzling lightly on my side too."


# ── 无事实：断言必检出 ───────────────────────────────────────────────────────
# 注意家族不变量：整条消息只有这一句断言时「剥空回退原文」（绝不产出空消息），
# hits 仍非空供调用方观测；带其他内容的消息才真剥句（见 rest_of_message 例）。

def test_incident_zh_detected_without_facts():
    out, hits = strip_weather_claim_conflicts(INCIDENT_ZH, "")
    assert hits, "事故原句必须被检出"
    # 多句形态：断言句离场、其余保留
    out2, hits2 = strip_weather_claim_conflicts(
        "哈哈是嘛！" + INCIDENT_ZH, "")
    assert hits2 and "下着小雨" not in out2 and "哈哈是嘛" in out2


def test_incident_en_detected_without_facts():
    out, hits = strip_weather_claim_conflicts(INCIDENT_EN, "")
    assert hits
    out2, hits2 = strip_weather_claim_conflicts(
        "Haha right! " + INCIDENT_EN + " Stay dry out there.", "")
    assert hits2 and "on my side" not in out2 and "Stay dry" in out2


def test_rest_of_message_survives():
    text = "哈哈是嘛。我这边也在下雨。你注意别淋湿了呀。"
    out, hits = strip_weather_claim_conflicts(text, "")
    assert hits
    assert "注意别淋湿" in out and "我这边也在下雨" not in out


def test_snow_and_sun_claims_stripped_without_facts():
    assert strip_weather_claim_conflicts("我这边在下雪呢", "")[1]
    assert strip_weather_claim_conflicts("我这边太阳好大", "")[1]


# ── 有事实：一致放行 / 冲突剥除 ─────────────────────────────────────────────

def test_consistent_claim_passes_with_facts():
    out, hits = strip_weather_claim_conflicts(INCIDENT_ZH, "rain")
    assert not hits and out == INCIDENT_ZH
    out2, hits2 = strip_weather_claim_conflicts(INCIDENT_ZH, "drizzle")
    assert not hits2


def test_conflicting_claim_stripped_with_facts():
    # 事实=晴，断言下雨 → 剥
    out, hits = strip_weather_claim_conflicts(INCIDENT_ZH, "clear")
    assert hits
    # 事实=雨，断言大太阳 → 剥
    out2, hits2 = strip_weather_claim_conflicts("我这边太阳好大呀", "rain")
    assert hits2


def test_unknown_bucket_treated_as_no_fact():
    assert strip_weather_claim_conflicts(INCIDENT_ZH, "unknown")[1]


# ── 零误伤面 ─────────────────────────────────────────────────────────────────

def test_asking_peer_weather_untouched():
    for t in ("你那边下雨了吗？", "你们那边天气怎么样",
              "listen to the rain in your video, so cozy"):
        out, hits = strip_weather_claim_conflicts(t, "")
        assert not hits and out == t


def test_generic_weather_talk_untouched():
    for t in ("最近老下雨，记得带伞", "雨天适合喝热汤",
              "It rains a lot in Manila this season."):
        out, hits = strip_weather_claim_conflicts(t, "")
        assert not hits and out == t


def test_all_stripped_falls_back_to_original():
    src = "我这边也在下雨。"
    out, hits = strip_weather_claim_conflicts(src, "")
    assert hits and out == src   # 剥空回退原文（绝不产出空消息）


def test_empty_input():
    assert strip_weather_claim_conflicts("", "") == ("", [])


# ── 接线钉 ───────────────────────────────────────────────────────────────────

def test_wired_into_skill_manager_guard():
    from src.skills.skill_manager import SkillManager
    src = inspect.getsource(SkillManager._apply_outbound_text_guard)
    assert "strip_weather_claim_conflicts" in src, \
        "#104 天气断言守卫被从出稿口摘除"
    assert "_persona_weather_snap" in src, "守卫必须消费天气事实快照 bucket"


def test_weather_default_on_in_injection():
    """#104 修向①：weather.enabled 运行默认必须为 True（显式 false 可关）。"""
    from src.skills.skill_manager import SkillManager
    src = inspect.getsource(SkillManager._inject_time_grounding)
    assert '_wcfg.get("enabled", True)' in src.replace("'", '"'), \
        "weather 注入默认开被关回去了（#104）"
