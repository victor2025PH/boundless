"""outbound.unlimited_mode 单一开关 + 拦截统一计数（src/ops/outbound_policy）契约。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.ops import outbound_policy as op

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _iso():
    op.reset_for_tests()
    yield
    op.reset_for_tests()


# ── 开关读取 ──────────────────────────────────────────────────────────

def test_default_off_without_provider():
    assert op.is_unlimited() is False
    assert op.runtime_config() == {}


def test_explicit_config_wins_over_provider():
    op.set_config_provider(lambda: {"outbound": {"unlimited_mode": True}})
    assert op.is_unlimited() is True
    assert op.is_unlimited({"outbound": {"unlimited_mode": False}}) is False
    assert op.is_unlimited({}) is False


def test_provider_exception_means_off():
    def boom():
        raise RuntimeError("x")
    op.set_config_provider(boom)
    assert op.is_unlimited() is False


def test_malformed_section_is_off():
    assert op.is_unlimited({"outbound": "yes"}) is False
    assert op.is_unlimited({"outbound": None}) is False


# ── business_cap：0=不限 单一语义 ─────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (15, 15), ("15", 15), (0, 0), (-3, 0), (None, 0), ("abc", 0),
])
def test_business_cap_normalizes(raw, expected):
    assert op.business_cap(raw, {}) == expected


def test_business_cap_unlimited_forces_zero():
    cfg = {"outbound": {"unlimited_mode": True}}
    assert op.business_cap(15, cfg) == 0
    assert op.cap_allows(15, 999, cfg) is True


def test_cap_allows_limited():
    assert op.cap_allows(3, 2, {}) is True
    assert op.cap_allows(3, 3, {}) is False
    assert op.cap_allows(0, 10 ** 6, {}) is True  # 0=不限


# ── 主动触达 live 覆盖 ────────────────────────────────────────────────

def test_proactive_overrides_noop_when_limited():
    live = {"min_silent_hours": 4.0, "max_per_tick": 2,
            "backoff_cfg": {"enabled": True}}
    out = op.proactive_live_overrides(live, {})
    assert out == live
    assert out is not live  # 拷贝，不动调用方字典


def test_proactive_overrides_strip_stacking_layers_but_keep_cadence():
    cfg = {"outbound": {"unlimited_mode": True}}
    live = {
        "min_silent_hours": 4.0, "cooldown_hours": 24.0, "max_per_tick": 2,
        "quiet_start_hour": 23.0, "quiet_end_hour": 8.0, "dry_run": True,
        "pacing_cfg": {"enabled": True}, "backoff_cfg": {"enabled": True},
        "response_pacing_cfg": {"enabled": True},
        "read_aware_cfg": {"enabled": True},
        "wall_cfg": {"enabled": True, "max_trailing": 6},
    }
    out = op.proactive_live_overrides(live, cfg)
    # 节奏与演练档原样保留
    assert out["min_silent_hours"] == 4.0
    assert out["cooldown_hours"] == 24.0
    assert out["quiet_start_hour"] == 23.0 and out["quiet_end_hour"] == 8.0
    assert out["dry_run"] is True
    # 叠加降频层全关
    assert out["max_per_tick"] == 0
    for k in ("pacing_cfg", "backoff_cfg", "response_pacing_cfg",
              "read_aware_cfg"):
        assert out[k] is None, k
    assert out["wall_cfg"]["enabled"] is False
    assert out["unlimited"] is True


# ── P5 拦截统一计数 ───────────────────────────────────────────────────

def test_record_block_counts_and_layers():
    op.record_block("business", "skill_cooldown", platform="telegram")
    op.record_block("business", "skill_cooldown", platform="telegram")
    op.record_block("safety", "kill_switch")
    op.record_block("token", "token_exhausted", platform="line")
    op.record_block("bogus-layer", "x")  # 未知层归 business
    snap = op.blocked_snapshot()
    assert snap["total"] == 5
    # "token" 是别名 → 归一为 tok（坐席可读端点有「响应体不含 token 子串」门禁）
    assert snap["by_layer"] == {"business": 3, "safety": 1, "tok": 1}
    assert snap["by_reason"]["tok:token_exhausted"] == 1
    assert op.LAYER_TOKEN == "tok"
    assert snap["by_reason"]["business:skill_cooldown"] == 2
    assert snap["by_reason"]["business:x"] == 1
    assert snap["by_platform"] == {"telegram": 2, "line": 1}
    assert snap["unlimited_mode"] is False


def test_record_unlimited_bypass():
    op.record_unlimited_bypass("skill_cooldown")
    op.record_unlimited_bypass()
    snap = op.blocked_snapshot()
    assert snap["unlimited_bypass"] == 2
    assert snap["by_reason"]["bypass:skill_cooldown"] == 1
    assert snap["total"] == 0  # bypass 不算拦截


def test_reason_key_cap_prevents_unbounded_growth():
    for i in range(op._MAX_KEYS + 50):
        op.record_block("business", f"r{i}")
    by_reason = op.blocked_snapshot()["by_reason"]
    assert len(by_reason) <= op._MAX_KEYS + 1
    assert by_reason.get("_other", 0) >= 50


def test_dump_prom_shape():
    op.set_config_provider(lambda: {"outbound": {"unlimited_mode": True}})
    op.record_block("business", "skill_cooldown", platform="telegram")
    op.record_block("safety", 'weird"reason')
    op.record_unlimited_bypass("s5_probability")
    text = op.dump_prom()
    assert "outbound_unlimited_mode 1" in text
    assert "outbound_unlimited_bypass_total 1" in text
    assert ('outbound_blocked_total{layer="business",reason="skill_cooldown"} 1'
            in text)
    assert 'reason="weird\\"reason"' in text
    assert 'outbound_blocked_by_platform_total{platform="telegram"} 1' in text
    # bypass 桶不冒充拦截
    assert "reason=\"s5_probability\"" not in text
    for line in text.strip().splitlines():
        assert line.startswith("#") or re.match(r"^[a-z_]+(\{[^}]*\})? \d+$", line), line


def test_record_block_never_raises():
    op.record_block(None, None)  # type: ignore[arg-type]
    op.record_block("", "", platform=None)  # type: ignore[arg-type]
    assert op.blocked_snapshot()["total"] == 2


def test_blocked_brief_top_reasons_sorted_and_leak_safe():
    op.set_config_provider(lambda: {"outbound": {"unlimited_mode": True}})
    for _ in range(3):
        op.record_block("business", "skill_cooldown", platform="telegram")
    op.record_block("safety", "kill_switch")
    op.record_block("token", "degrade_ai_reply")
    op.record_unlimited_bypass("s5_probability")
    brief = op.blocked_brief(top=2)
    assert brief["unlimited"] is True
    assert brief["blocked_total"] == 5
    assert brief["bypass"] == 1
    assert brief["by_layer"] == {"business": 3, "safety": 1, "tok": 1}
    assert brief["top_reasons"] == [
        {"reason": "business:skill_cooldown", "n": 3},
        {"reason": "safety:kill_switch", "n": 1},
    ]
    # 随坐席可读的 quota-state 下发：键名/层名不得含 token 子串；平台维度不下发
    assert "token" not in json.dumps({k: v for k, v in brief.items() if k != "top_reasons"})
    assert "by_platform" not in brief


def test_quota_state_carries_outbound_brief():
    from src.licensing.quota_state import collect_quota_state

    op.record_block("business", "reply_cooldown")
    state = collect_quota_state(quota={"source": "license"}, level="ok")
    assert isinstance(state.get("outbound"), dict)
    assert state["outbound"]["blocked_total"] == 1
    assert state["outbound"]["top_reasons"][0]["reason"] == "business:reply_cooldown"
    # 展示数据不参与裁决
    assert state["verdict"] == "ok"


def test_metrics_routes_wire_outbound_blocked():
    src = (_REPO / "src" / "web" / "routes" / "drafts_routes.py").read_text(encoding="utf-8")
    assert 'metrics["outbound_blocked"]' in src
    assert "from src.ops.outbound_policy import dump_prom as _ob_dump_prom" in src


# ── 接线 / 配置示例 ───────────────────────────────────────────────────

def test_admin_wires_provider():
    src = (_REPO / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert "src.ops.outbound_policy" in src
    assert "_set_outbound_cfg_provider(" in src


def test_config_example_has_outbound_section():
    text = (_REPO / "config" / "config.example.yaml").read_text(encoding="utf-8")
    assert re.search(r"^outbound:\s*$", text, re.M)
    assert re.search(r"^\s+unlimited_mode:\s*false\s*$", text, re.M)
