"""一键预设档 + 快照/回滚纯计划层单测。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.companion.capability_presets import (
    EXTRA_FLAG_DEFAULTS, PRESETS, build_preset_plan, capture_extra_flags,
    capture_snapshot, preset_extras, preview_preset, snapshot_to_plan,
)
from src.web.routes.companion_capability_routes import (
    register_companion_capability_routes,
)


def _find(plan, key, field):
    return next((it for it in plan if it["key"] == key and it["field"] == field), None)


def _preview_auth(request: Request):
    # Request 注解必须在模块级可解析，否则 FastAPI 把 request 当 query → 422
    return True


# ── 预设计划 ───────────────────────────────────────────────────────────────

def test_unknown_preset_returns_none():
    assert build_preset_plan("nope") is None


def test_four_presets_exist():
    assert set(PRESETS) == {"safe_default", "dry_run_trial", "nurture_mode", "full_auto"}


def test_safe_default_kills_outbound_keeps_safeguards():
    plan = build_preset_plan("safe_default")
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is False
    assert _find(plan, "voice_autosend", "enabled")["value"] is False
    assert _find(plan, "realtime_voice", "enabled")["value"] is False
    assert _find(plan, "proactive_topic", "enabled")["value"] is False
    # 安全栈开
    assert _find(plan, "persona_guard", "enabled")["value"] is True
    assert _find(plan, "companion_send_gate", "enabled")["value"] is True


def test_dry_run_trial_proactive_is_dry_run():
    plan = build_preset_plan("dry_run_trial")
    assert _find(plan, "proactive_topic", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is True
    assert _find(plan, "l2_autosend_worker", "enabled")["value"] is True
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is False  # 不真发


def test_nurture_mode_is_dry_run_trial_base():
    """养号模式底座＝灰度试运行：worker 开但绝不真发，主动触达只演练。"""
    plan = build_preset_plan("nurture_mode")
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is False   # 不真发
    assert _find(plan, "l2_autosend_worker", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is True
    assert _find(plan, "voice_autosend", "enabled")["value"] is False
    # send-gate＝预热爬坡执行者，必须开
    assert _find(plan, "companion_send_gate", "enabled")["value"] is True
    assert _find(plan, "persona_guard", "enabled")["value"] is True


def test_nurture_mode_arms_canary_manual_whitelist():
    """extras 预先武装金丝雀：enabled=true + manual（白名单=pinned_accounts）。"""
    extras = preset_extras("nurture_mode")
    by_path = {e["path"]: e["value"] for e in extras}
    assert by_path.get("ops.canary.enabled") is True
    assert by_path.get("ops.canary.mode") == "manual"
    # extras 路径必须全部在快照缺省表里（否则回滚还原不了）
    for p in by_path:
        assert p in EXTRA_FLAG_DEFAULTS


def test_other_presets_have_no_extras():
    """既有三档不碰金丝雀（最小侵入：canary 状态由养号档武装、由运营面板解除）。"""
    for name in ("safe_default", "dry_run_trial", "full_auto"):
        assert preset_extras(name) == []


def test_capture_extra_flags_defaults_and_override():
    assert capture_extra_flags({}) == {"inbox.auto_draft.automation_mode": "auto_ai",
                                       "inbox.auto_draft.bootstrap_automation_mode": True,
                                       "ops.canary.enabled": False,
                                       "ops.canary.mode": "manual"}
    cfg = {"ops": {"canary": {"enabled": True, "mode": "auto_health"}}}
    got = capture_extra_flags(cfg)
    assert got["ops.canary.enabled"] is True
    assert got["ops.canary.mode"] == "auto_health"


def test_full_auto_enables_everything_real():
    plan = build_preset_plan("full_auto")
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is False      # 真发
    assert _find(plan, "voice_autosend", "enabled")["value"] is True
    assert _find(plan, "realtime_voice", "enabled")["value"] is False       # 实时通话仍独立 opt-in


def test_full_auto_order_gate_and_worker_before_deliver():
    """send_gate / worker 必须排在真发主开关之前（开 deliver 时已无裸奔 warn）。"""
    plan = build_preset_plan("full_auto")
    keys = [(it["key"], it["field"]) for it in plan]
    i_deliver = keys.index(("l2_autosend_deliver", "enabled"))
    i_gate = keys.index(("companion_send_gate", "enabled"))
    i_worker = keys.index(("l2_autosend_worker", "enabled"))
    assert i_gate < i_deliver and i_worker < i_deliver


def test_operator_off_skips_send_gate_on_all_presets():
    """overlay 显式关闸 → 四档预设都不写 companion_send_gate；PRESETS 字面量不变。"""
    overlay = {"companion_send_gate": {"enabled": False}}
    before = PRESETS["full_auto"]["states"]["companion_send_gate"]
    for name in PRESETS:
        plan = build_preset_plan(name, overlay=overlay)
        assert plan is not None, name
        keys = {it["key"] for it in plan}
        assert "companion_send_gate" not in keys, name
    assert PRESETS["full_auto"]["states"]["companion_send_gate"] == before


def test_missing_overlay_still_opens_send_gate():
    """缺 overlay / 空 overlay / 缺键 = 新装机，预设仍开闸。"""
    for overlay in (None, {}, {"companion_send_gate": {}}):
        plan = build_preset_plan("full_auto", overlay=overlay)
        assert _find(plan, "companion_send_gate", "enabled")["value"] is True
    # 字符串 "false" 不算显式 False（与 send_gate_operator_off 同口径）
    plan = build_preset_plan(
        "safe_default",
        overlay={"companion_send_gate": {"enabled": "false"}},
    )
    assert _find(plan, "companion_send_gate", "enabled")["value"] is True
    # 无 overlay 形参的旧调用保持原语义
    assert _find(build_preset_plan("nurture_mode"),
                 "companion_send_gate", "enabled")["value"] is True


def test_preview_preset_unknown_returns_none():
    assert preview_preset("nope") is None


def test_preview_preset_operator_off_matches_plan():
    overlay = {"companion_send_gate": {"enabled": False}}
    p = preview_preset("full_auto", overlay=overlay)
    assert p["send_gate_skipped"] == "operator_off"
    assert p["would_write_send_gate"] is False
    assert p["preset"] == "full_auto"
    assert p["intent_count"] == len(p["plan"])
    assert not any(it["key"] == "companion_send_gate" for it in p["plan"])
    # 与 apply 计划字节级一致
    assert p["plan"] == build_preset_plan("full_auto", overlay=overlay)


def test_preview_preset_missing_overlay_would_open_gate():
    p = preview_preset("nurture_mode")
    assert p["send_gate_skipped"] is None
    assert p["would_write_send_gate"] is True
    extras = {e["path"]: e["value"] for e in p["extras"]}
    assert extras.get("ops.canary.enabled") is True


# ── 快照 / 回滚 ────────────────────────────────────────────────────────────

def test_capture_snapshot_shape():
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
           "companion": {"enabled": True, "proactive_topic": {"enabled": True, "dry_run": True}}}
    snap = capture_snapshot(cfg)
    assert snap["l2_autosend_deliver"]["enabled"] is True
    assert snap["proactive_topic"]["enabled"] is True
    assert snap["proactive_topic"]["dry_run"] is True
    # 无 dry_run 档的能力快照不含 dry_run 键
    assert "dry_run" not in snap["persona_guard"]


def test_snapshot_roundtrip_to_plan():
    cfg = {"inbox": {"l2_autosend": {"deliver": True}},
           "companion": {"proactive_topic": {"enabled": True, "dry_run": True}}}
    snap = capture_snapshot(cfg)
    plan = snapshot_to_plan(snap)
    assert _find(plan, "l2_autosend_deliver", "enabled")["value"] is True
    assert _find(plan, "proactive_topic", "dry_run")["value"] is True


def test_snapshot_to_plan_ignores_unknown_keys():
    plan = snapshot_to_plan({"bogus_cap": {"enabled": True}})
    assert plan == []


# ── 预览路由（只读，零 overlay 写）────────────────────────────────────────

def _preview_client(tmp_path, overlay=None):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("telegram: {enabled: false}\n", encoding="utf-8")
    if overlay is not None:
        import yaml
        (tmp_path / "config.local.yaml").write_text(
            yaml.dump(overlay, allow_unicode=True), encoding="utf-8")
    writes = []

    class CM:
        config_path = str(cfg)
        config = {"telegram": {"enabled": False}}

        def set_overlay_flag(self, *a, **k):
            writes.append((a, k))

    app = FastAPI()
    app.state.config_manager = CM()
    register_companion_capability_routes(app, api_auth=_preview_auth)
    return TestClient(app), writes


def test_preset_preview_route_operator_off_is_read_only(tmp_path):
    client, writes = _preview_client(
        tmp_path, {"companion_send_gate": {"enabled": False}})
    d = client.get(
        "/api/companion/capabilities/preset-preview",
        params={"name": "full_auto"},
    ).json()
    assert d["ok"] is True
    assert d["send_gate_skipped"] == "operator_off"
    assert d["would_write_send_gate"] is False
    assert writes == []


def test_preset_preview_route_unknown_and_default_open(tmp_path):
    client, writes = _preview_client(tmp_path)
    bad = client.get(
        "/api/companion/capabilities/preset-preview",
        params={"name": "nope"},
    ).json()
    assert bad["ok"] is False and bad["error"] == "unknown_preset"
    d = client.get(
        "/api/companion/capabilities/preset-preview",
        params={"name": "safe_default"},
    ).json()
    assert d["ok"] is True
    assert d["send_gate_skipped"] is None
    assert d["would_write_send_gate"] is True
    assert writes == []


def test_rpa_overview_preset_previews_before_confirm():
    """看板套用预设前先 GET 预览；旧后端 404 时 extra 为空、确认框仍可用。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent
            / "src" / "web" / "templates" / "rpa_overview.html").read_text(
                encoding="utf-8")
    assert "/api/companion/capabilities/preset-preview" in html
    assert "ov_js_preset_gate_kept_off" in html
    assert "send_gate_skipped==='operator_off'" in html
