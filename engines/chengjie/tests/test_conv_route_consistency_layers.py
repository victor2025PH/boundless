# -*- coding: utf-8 -*-
"""守卫分层（2026-09-18 P1-9）：一致性（防穿帮）层在无限制会话里默认**不让路**。

钉住：
- :data:`CONSISTENCY_LAYERS` ⊂ :data:`QUALITY_LAYERS`，且与安全层不相交；
- ``skip_guard``：无限制会话跳 persona_guard（尺度层），**不跳** fabrication / world_clock /
  media_promise / commitment / outbound_dup 等一致性层；
- ``keep_consistency_guards: false`` → ``attach`` 折入 ``_unrestricted_drop_consistency``，一致性层回旧行为（跳）；
- ``skip_for_conv``（drafts / autosend 用）同一口径；
- 统计：``consistency_kept`` 计数、一致性层不计入 ``skipped_total``；
- 标准会话零变化；``apply_context`` 清残留键。
"""
from __future__ import annotations

from typing import Any, Dict

import pytest

from src.ai import conv_route
from src.ai.conv_route import CONSISTENCY_LAYERS, QUALITY_LAYERS, SAFETY_LAYERS, Route


class _KV:
    def __init__(self) -> None:
        self.kv: Dict[str, str] = {}

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def list_app_settings(self, prefix=""):
        return [{"key": k, "value": v, "updated_by": "t", "updated_at": 0}
                for k, v in sorted(self.kv.items()) if k.startswith(prefix)]


CID = "telegram:acct1:777"
CFG_DROP = {"ai": {"unrestricted": {"keep_consistency_guards": False}}}


@pytest.fixture(autouse=True)
def _reset():
    conv_route.reset_for_tests()
    yield
    conv_route.reset_for_tests()


def test_layer_sets_are_well_formed():
    assert CONSISTENCY_LAYERS <= QUALITY_LAYERS
    assert not (CONSISTENCY_LAYERS & SAFETY_LAYERS)
    for name in ("fabrication_guard", "world_clock_guard", "media_promise_guard",
                 "commitment_guard", "outbound_dup", "song_claim_guard",
                 "status_fabrication_guard", "photo_capability_sanitize"):
        assert name in CONSISTENCY_LAYERS
    # 尺度 / 人设 / 风控层仍是普通质量层
    for name in ("persona_guard", "risk_level", "global_rules", "adult_grader"):
        assert name in QUALITY_LAYERS and name not in CONSISTENCY_LAYERS


def test_skip_guard_keeps_consistency_layers_by_default():
    ctx = {"_unrestricted": True}
    assert conv_route.skip_guard(ctx, "persona_guard") is True
    assert conv_route.skip_guard(ctx, "risk_level") is True
    for lyr in sorted(CONSISTENCY_LAYERS):
        assert conv_route.skip_guard(ctx, lyr) is False, lyr
    snap = conv_route.stats_snapshot()
    assert snap["skipped_total"] == 2
    assert snap["consistency_kept"] == len(CONSISTENCY_LAYERS)
    assert "fabrication_guard" not in snap["by_layer"]


def test_skip_guard_drop_flag_restores_legacy_behaviour():
    ctx = {"_unrestricted": True, "_unrestricted_drop_consistency": True}
    for lyr in sorted(CONSISTENCY_LAYERS):
        assert conv_route.skip_guard(ctx, lyr) is True, lyr
    # 安全层仍要另一把钥匙
    assert conv_route.skip_guard(ctx, "kill_switch") is False


def test_standard_conversation_unchanged():
    ctx = {}
    for lyr in sorted(QUALITY_LAYERS | SAFETY_LAYERS):
        assert conv_route.skip_guard(ctx, lyr) is False, lyr
    assert conv_route.stats_snapshot()["consistency_kept"] == 0


def test_attach_folds_keep_flag_from_config():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    ctx: Dict[str, Any] = {}
    conv_route.attach(ctx, CID, store=st, config={})
    assert ctx.get("_unrestricted") is True
    assert "_unrestricted_drop_consistency" not in ctx          # 默认：保留一致性层
    assert conv_route.skip_guard(ctx, "fabrication_guard") is False

    ctx2: Dict[str, Any] = {}
    conv_route.attach(ctx2, CID, store=st, config=CFG_DROP)
    assert ctx2.get("_unrestricted_drop_consistency") is True
    assert conv_route.skip_guard(ctx2, "fabrication_guard") is True

    # 切回标准 → 残留键清掉
    Route.standard().apply_context(ctx2)
    assert "_unrestricted_drop_consistency" not in ctx2
    assert "_unrestricted" not in ctx2


def test_skip_for_conv_same_semantics():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"})
    assert conv_route.skip_for_conv(st, CID, "persona_guard", {}) is True
    assert conv_route.skip_for_conv(st, CID, "outbound_dup", {}) is False
    assert conv_route.skip_for_conv(st, CID, "media_promise_guard", {}) is False
    assert conv_route.skip_for_conv(st, CID, "outbound_dup", CFG_DROP) is True
    assert conv_route.skip_for_conv(st, CID, "hard_stop", CFG_DROP) is False   # 安全层不受此开关影响


def test_keep_consistency_guards_reader_is_forgiving():
    assert conv_route.keep_consistency_guards({}) is True
    assert conv_route.keep_consistency_guards(None) in (True, False)      # 读运行时配置，不抛
    assert conv_route.keep_consistency_guards({"ai": {"unrestricted": {"keep_consistency_guards": 0}}}) is False
    assert conv_route.keep_consistency_guards({"ai": "garbage"}) is True


def test_session_drop_consistency_overrides_keep_default():
    """会话级 drop_consistency（composer「防穿帮：让路」）= YAML keep 仍为 True 也让路。"""
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted", "drop_consistency": True})
    r = conv_route.get(st, CID)
    assert r.drop_consistency is True and r.unrestricted
    ctx: Dict[str, Any] = {}
    conv_route.attach(ctx, CID, store=st, config={})
    assert ctx.get("_unrestricted_drop_consistency") is True
    assert conv_route.skip_guard(ctx, "fabrication_guard") is True
    assert conv_route.skip_for_conv(st, CID, "outbound_dup", {}) is True
    # 标准档不许挂
    r2 = conv_route.normalize({"profile": "standard", "drop_consistency": True})
    assert r2.drop_consistency is False and r2.is_default


def test_skill_manager_wrapper_routes_through_skip_guard():
    from src.skills.skill_manager import SkillManager
    ctx = {"_unrestricted": True}
    assert SkillManager._cr_skip(ctx, "persona_guard") is True
    assert SkillManager._cr_skip(ctx, "world_clock_guard") is False
    assert SkillManager._cr_skip({}, "persona_guard") is False
