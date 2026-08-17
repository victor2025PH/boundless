"""P7 2026-08-03：主动关怀「AI 运行方案」确定性核心门禁。

守四件事：时间桶分组语义（与旧前端 _csBucket 同口径）、四灯状态码逐分支、
整体 chip（off/ok/warn）判定、结构化建议规则（含优先级与截断——建议错一次
运营就不信管家了，规则必须钉死）。
"""
from datetime import datetime

from src.contacts.care_advisor import (
    GO_LIVE_MIN_LIKE_RATE,
    GO_LIVE_MIN_REVIEWED,
    bucket_of,
    build_advice,
    build_lights,
    engine_overall,
    group_plan_items,
)

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()  # 周三 10:00
_DAY = 86400.0


# ── 时间桶 ─────────────────────────────────────────────────────────────
def test_bucket_semantics():
    assert bucket_of(NOW - 60, NOW) == "overdue"
    assert bucket_of(NOW, NOW) == "overdue"          # due==now 视为到点
    assert bucket_of(NOW + 8 * 3600, NOW) == "today"     # 当天 18:00
    assert bucket_of(NOW + 20 * 3600, NOW) == "tomorrow"  # 次日 06:00
    assert bucket_of(NOW + 3 * _DAY, NOW) == "week"
    assert bucket_of(NOW + 10 * _DAY, NOW) == "later"


def test_group_plan_items_order_and_omit_empty():
    items = [
        {"id": 1, "due_at": NOW + 10 * _DAY},
        {"id": 2, "due_at": NOW - 60},
        {"id": 3, "due_at": NOW + 3600},
    ]
    groups = group_plan_items(items, NOW)
    assert [g["key"] for g in groups] == ["overdue", "today", "later"]
    assert [it["id"] for g in groups for it in g["items"]] == [2, 3, 1]
    assert group_plan_items([], NOW) == []


# ── 四灯 ───────────────────────────────────────────────────────────────
def test_lights_capture_branches():
    base = dict(enabled=True, dry_run=False, dispatch_running=True,
                multiplatform_deferred=True)
    assert build_lights(capture_wired=True, capture_config_on=True, **base)["capture"] == "ok"
    assert build_lights(capture_wired=True, capture_config_on=False, **base)["capture"] == "standby"
    assert build_lights(capture_wired=False, capture_config_on=True, **base)["capture"] == "broken"


def test_lights_dispatch_and_delivery_branches():
    base = dict(capture_wired=True, capture_config_on=True)
    on = build_lights(enabled=True, dry_run=False, dispatch_running=True,
                      multiplatform_deferred=True, **base)
    assert on["dispatch"] == "ok" and on["delivery"] == "ready" and on["engine"] == "on"

    standby = build_lights(enabled=False, dry_run=False, dispatch_running=True, **base)
    assert standby["dispatch"] == "standby" and standby["engine"] == "off"

    ai_miss = build_lights(enabled=True, dry_run=False, dispatch_running=False,
                           dispatch_skip="ai_missing", **base)
    assert ai_miss["dispatch"] == "ai_missing"

    broken = build_lights(enabled=True, dry_run=False, dispatch_running=False, **base)
    assert broken["dispatch"] == "broken"

    dry = build_lights(enabled=True, dry_run=True, dispatch_running=True, **base)
    assert dry["delivery"] == "dry"

    rpa_only = build_lights(enabled=True, dry_run=False, dispatch_running=True,
                            messenger_rpa=True, **base)
    assert rpa_only["delivery"] == "ready"

    off = build_lights(enabled=True, dry_run=False, dispatch_running=True, **base)
    assert off["delivery"] == "off"


def test_engine_overall():
    ok_lights = {"capture": "ok", "dispatch": "ok", "delivery": "ready"}
    assert engine_overall(ok_lights, enabled=False, dry_run=False) == "off"
    assert engine_overall(ok_lights, enabled=True, dry_run=False) == "ok"
    assert engine_overall({**ok_lights, "capture": "broken"},
                          enabled=True, dry_run=False) == "warn"
    assert engine_overall({**ok_lights, "dispatch": "ai_missing"},
                          enabled=True, dry_run=False) == "warn"
    # 真发模式投递通道未开 = warn；试运行不要求投递通道
    assert engine_overall({**ok_lights, "delivery": "off"},
                          enabled=True, dry_run=False) == "warn"
    assert engine_overall({**ok_lights, "delivery": "dry"},
                          enabled=True, dry_run=True) == "ok"


# ── 建议规则 ────────────────────────────────────────────────────────────
def _adv(**kw):
    base = dict(enabled=True, dry_run=True, reviewed=0, like_rate_pct=None,
                samples_7d=0, pending_total=0, captured_24h=0,
                last_captured_ts=NOW - 3600, shadow_llm_only=0,
                llm_capture_enabled=False, effect_rate=None,
                effect_matured=0, effect_replied=0, now=NOW)
    base.update(kw)
    return build_advice(**base)


def test_advice_disabled_is_silent():
    assert _adv(enabled=False, reviewed=99, like_rate_pct=100.0) == []


def test_advice_go_live_ready_thresholds():
    a = _adv(reviewed=GO_LIVE_MIN_REVIEWED, like_rate_pct=GO_LIVE_MIN_LIKE_RATE,
             samples_7d=10, pending_total=1)
    assert a[0]["code"] == "go_live_ready"
    assert a[0]["reviewed"] == GO_LIVE_MIN_REVIEWED
    # 差一条 / 差一个点都不劝开闸
    assert all(x["code"] != "go_live_ready" for x in _adv(
        reviewed=GO_LIVE_MIN_REVIEWED - 1, like_rate_pct=99.0, samples_7d=10))
    assert all(x["code"] != "go_live_ready" for x in _adv(
        reviewed=20, like_rate_pct=GO_LIVE_MIN_LIKE_RATE - 1, samples_7d=20))


def test_advice_review_more_and_waiting_due():
    a = _adv(samples_7d=3, reviewed=2, pending_total=5)
    assert a[0]["code"] == "review_more" and a[0]["need"] == GO_LIVE_MIN_REVIEWED
    b = _adv(samples_7d=0, pending_total=5)
    assert b[0]["code"] == "waiting_due" and b[0]["pending"] == 5


def test_advice_capture_quiet_only_when_truly_quiet():
    assert _adv(last_captured_ts=NOW - 3 * _DAY)[0]["code"] == "capture_quiet"
    assert _adv(last_captured_ts=0.0)[0]["code"] == "capture_quiet"
    # 有 pending / 有近捕获 / 24h 有量 → 不算静默
    assert all(x["code"] != "capture_quiet" for x in _adv(
        pending_total=1, last_captured_ts=NOW - 3 * _DAY))
    assert all(x["code"] != "capture_quiet" for x in _adv(
        last_captured_ts=NOW - 3600))
    assert all(x["code"] != "capture_quiet" for x in _adv(
        captured_24h=2, last_captured_ts=NOW - 3 * _DAY))


def test_advice_llm_candidate_gated_on_enabled_flag():
    a = _adv(shadow_llm_only=6, last_captured_ts=NOW - 60)
    assert a and a[0]["code"] == "llm_capture_candidate" and a[0]["llm_only"] == 6
    assert _adv(shadow_llm_only=6, llm_capture_enabled=True,
                last_captured_ts=NOW - 60) == []
    assert _adv(shadow_llm_only=4, last_captured_ts=NOW - 60) == []


def test_advice_low_reply_rate_needs_mature_sample():
    a = _adv(dry_run=False, effect_rate=0.1, effect_matured=6, effect_replied=1,
             last_captured_ts=NOW - 60)
    assert a[0]["code"] == "low_reply_rate" and a[0]["matured"] == 6
    assert _adv(dry_run=False, effect_rate=0.1, effect_matured=2,
                last_captured_ts=NOW - 60) == []
    assert _adv(dry_run=False, effect_rate=0.5, effect_matured=10,
                last_captured_ts=NOW - 60) == []


def test_advice_priority_and_cap():
    a = _adv(reviewed=10, like_rate_pct=95.0, samples_7d=10,
             shadow_llm_only=9, last_captured_ts=NOW - 3 * _DAY)
    # go_live_ready 优先；capture_quiet（pending=0、无近捕获）与 llm_candidate 竞争第二席
    assert [x["code"] for x in a] == ["go_live_ready", "capture_quiet"]
    assert len(a) == 2  # 默认截断 2 条
    a3 = build_advice(enabled=True, dry_run=True, reviewed=10, like_rate_pct=95.0,
                      samples_7d=10, pending_total=0, captured_24h=0,
                      last_captured_ts=NOW - 3 * _DAY, shadow_llm_only=9,
                      llm_capture_enabled=False, effect_rate=None,
                      effect_matured=0, effect_replied=0, now=NOW, max_items=3)
    assert [x["code"] for x in a3] == [
        "go_live_ready", "capture_quiet", "llm_capture_candidate"]
