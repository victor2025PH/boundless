# -*- coding: utf-8 -*-
"""C0-3b 档位功能闸门纯逻辑门禁（融合实例 P1）。

覆盖：档位序 / 总开关默认关=恒放行 / plan_override / license 显式功能位
越档授予与单点禁用 / 过期回落 community / 未注册功能 fail-open /
API 前缀匹配 / 锁定清单 / nav 过滤视图（锁定项不渲染、gate 关零变化）。
全部用显式 fake status，绝不触碰 license 单例（避免吃到宿主机真实授权）。
"""
from types import SimpleNamespace

from src.licensing.feature_gate import (
    FEATURE_MIN_PLAN,
    PLAN_ORDER,
    effective_plan,
    feature_enabled,
    feature_for_api_path,
    gate_enabled,
    gate_snapshot,
    locked_features,
    plan_rank,
)


def _st(plan="community", licensed=True, features=None, state="active"):
    return SimpleNamespace(
        plan=plan, licensed=licensed, features=features or {}, state=state)


def _cfg(enabled=True, override=None):
    fg = {"enabled": enabled}
    if override is not None:
        fg["plan_override"] = override
    return {"licensing": {"feature_gate": fg}}


# ── 档位序 ────────────────────────────────────────────────────────────────

def test_plan_order_and_rank():
    assert PLAN_ORDER == ("community", "basic", "pro", "flagship")
    assert plan_rank("community") < plan_rank("basic") < plan_rank("pro") \
        < plan_rank("flagship")
    # 未知/空档位按 community（档位串只来自我方履约表，未知即笔误保守处理）
    assert plan_rank("enterprise") == plan_rank("community")
    assert plan_rank("") == 0
    assert plan_rank("  FLAGSHIP ") == plan_rank("flagship")


# ── 总开关 ────────────────────────────────────────────────────────────────

def test_gate_disabled_is_default_and_allows_everything():
    assert gate_enabled({}) is False
    assert gate_enabled(None) is False
    assert gate_enabled(_cfg(enabled=False)) is False
    st = _st(plan="community", licensed=False, state="unlicensed")
    for name in FEATURE_MIN_PLAN:
        assert feature_enabled(name, {}, st) is True
        assert feature_enabled(name, _cfg(enabled=False), st) is True
    assert locked_features({}, st) == []


# ── 档位默认位矩阵 ─────────────────────────────────────────────────────────

def test_tier_matrix_semantics():
    cfg = _cfg()
    cases = {
        "community": {"translation_suite": False, "ai_autosend": False,
                      "kb": False, "companion": False, "rpa": False},
        "basic": {"translation_suite": True, "ai_autosend": False,
                  "kb": False, "companion": False, "rpa": False},
        "pro": {"translation_suite": True, "ai_autosend": True, "kb": True,
                "personas": True, "care": True, "analytics": True,
                "companion": False, "rpa": False, "voice_clone": False,
                "bazi": False, "monetization": False},
        "flagship": {n: True for n in FEATURE_MIN_PLAN},
    }
    for plan, expect in cases.items():
        st = _st(plan=plan)
        for name, allowed in expect.items():
            assert feature_enabled(name, cfg, st) is allowed, (plan, name)


def test_locked_features_listing():
    cfg = _cfg()
    assert locked_features(cfg, _st(plan="flagship")) == []
    locked_pro = locked_features(cfg, _st(plan="pro"))
    assert "companion" in locked_pro and "rpa" in locked_pro
    assert "kb" not in locked_pro
    locked_comm = locked_features(cfg, _st(plan="community"))
    assert set(locked_comm) == set(FEATURE_MIN_PLAN)


# ── plan_override（厂商自营/灰度）─────────────────────────────────────────

def test_plan_override_wins_over_license():
    # 授权是 pro，override 提到 flagship → 全解锁（智聊灰度配方）
    cfg = _cfg(override="flagship")
    st = _st(plan="pro")
    for name in FEATURE_MIN_PLAN:
        assert feature_enabled(name, cfg, st) is True
    # override 压到 basic → pro 功能也锁
    cfg2 = _cfg(override="basic")
    assert feature_enabled("kb", cfg2, _st(plan="flagship")) is False
    # 非法 override 值忽略 → 回落 license.plan
    cfg3 = _cfg(override="platinum")
    assert feature_enabled("kb", cfg3, _st(plan="pro")) is True
    assert effective_plan(cfg3, _st(plan="pro")) == "pro"


# ── license 显式功能位 ─────────────────────────────────────────────────────

def test_explicit_license_features_override_plan_defaults():
    cfg = _cfg()
    # 越档授予：basic 档 license 单点给 companion
    st_grant = _st(plan="basic", features={"companion": True})
    assert feature_enabled("companion", cfg, st_grant) is True
    assert feature_enabled("rpa", cfg, st_grant) is False  # 其余仍按档位
    # 单点禁用：flagship 档 license 显式关 rpa
    st_deny = _st(plan="flagship", features={"rpa": False})
    assert feature_enabled("rpa", cfg, st_deny) is False
    assert feature_enabled("companion", cfg, st_deny) is True
    # 未 licensed（过期超宽限）→ 显式位失效，按 community
    st_expired = _st(plan="flagship", licensed=False,
                     features={"companion": True}, state="expired")
    assert feature_enabled("companion", cfg, st_expired) is False
    assert effective_plan(cfg, st_expired) == "community"


def test_unknown_feature_fails_open():
    cfg = _cfg()
    st = _st(plan="community")
    assert feature_enabled("some_future_feature", cfg, st) is True
    assert feature_enabled("", cfg, st) is True


# ── API 前缀匹配 ──────────────────────────────────────────────────────────

def test_feature_for_api_path():
    assert feature_for_api_path("/api/kb/entries") == "kb"
    assert feature_for_api_path("/api/kb") == "kb"  # 无尾斜杠精确族根
    assert feature_for_api_path("/api/line-rpa/status") == "rpa"
    assert feature_for_api_path("/api/messenger-rpa/x") == "rpa"
    assert feature_for_api_path("/api/whatsapp-rpa/x") == "rpa"
    assert feature_for_api_path("/api/companion/capabilities") == "companion"
    assert feature_for_api_path("/api/voice/enroll") == "voice_clone"
    assert feature_for_api_path("/api/monetize/grant") == "monetization"
    assert feature_for_api_path("/api/personas/p1/media") == "personas"
    assert feature_for_api_path("/api/care/schedule") == "care"
    # 不在表内 → None（工作台/翻译/草稿等核心面永不守卫）
    for p in ("/api/workspace/me", "/api/drafts/pending", "/api/translate",
              "/workspace", "/api/kbx/other", "/api/", ""):
        assert feature_for_api_path(p) is None


# ── 快照 ─────────────────────────────────────────────────────────────────

def test_gate_snapshot_shape():
    snap = gate_snapshot(_cfg(override="pro"), _st(plan="basic"))
    assert snap["enabled"] is True
    assert snap["plan"] == "pro"
    assert snap["plan_source"] == "override"
    assert snap["features"]["kb"]["allowed"] is True
    assert snap["features"]["companion"]["allowed"] is False
    assert "companion" in snap["locked"]
    assert snap["plan_order"] == list(PLAN_ORDER)
    # gate 关：allowed 全 True（展示口径与运行口径一致）
    snap_off = gate_snapshot(_cfg(enabled=False), _st(plan="community"))
    assert snap_off["enabled"] is False
    assert snap_off["locked"] == []
    assert all(v["allowed"] for v in snap_off["features"].values())


# ── nav 锁标视图（P3：锁定项不消失，带 locked=True 注解 → 锁标+升级引导）──

def _nav_paths(ctx, locked_only=None):
    keys = set()
    for g in ctx["nav_groups"]:
        for it in g["items"]:
            if not isinstance(it, dict):
                continue
            if locked_only is None or bool(it.get("locked")) is locked_only:
                keys.add(it.get("path", ""))
    return keys


def test_nav_context_marks_locked_items(monkeypatch):
    import src.web.nav_schema as ns

    # 不传 config → 静态全量（同一对象，零开销零变化）
    assert ns.get_nav_context() is ns._NAV_CONTEXT
    # 传了 config 就会套 ui_visibility 缺省隐藏（矩阵/群脉/转接/未开变现），
    # 即使 feature_gate 关着也不是同一对象——这是 2026-08-16 显隐层的契约。
    ctx_off = ns.get_nav_context(_cfg(enabled=False))
    assert ctx_off is not ns._NAV_CONTEXT
    assert not _nav_paths(ctx_off, locked_only=True)

    # 锁标测试把显隐层打开，避免缺省藏矩阵把「锁着的升级面」一起滤掉
    def _nav_vis(enabled=True):
        c = _cfg(enabled=enabled)
        c["ui_visibility"] = {
            "matrix_nav": True, "group_show": True, "ai_settings": True,
        }
        return c

    # pro 档：flagship 项（RPA 渠道 / 变现）带锁标仍渲染，pro 项无锁标
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(status=lambda: _st(plan="pro")),
    )
    ctx = ns.get_nav_context(_nav_vis())
    unlocked = _nav_paths(ctx, locked_only=False)
    locked = _nav_paths(ctx, locked_only=True)
    assert "/knowledge" in unlocked and "/personas" in unlocked
    assert "/monetization" in locked
    assert "/workspace/channels/line" in locked
    assert "/workspace/channels/whatsapp" in locked
    # Telegram 渠道与工作台核心永不上锁
    assert "/workspace/channels/telegram" in unlocked
    assert "/workspace" in unlocked
    # 命令面板仍直接隐藏锁定项（跳转列表无升级语义）
    cmd_paths = {d.get("path") for d in ctx["nav_cmd_items"]}
    assert "/monetization" not in cmd_paths and "/knowledge" in cmd_paths
    # 单例不被污染：静态全量里绝无 locked 注解
    assert not _nav_paths(ns._NAV_CONTEXT, locked_only=True)

    # community 档：pro/flagship 项全部带锁标（可见=升级面），核心项照常
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(
            status=lambda: _st(plan="community", licensed=False,
                               state="unlicensed")),
    )
    ctx2 = ns.get_nav_context(_nav_vis())
    locked2 = _nav_paths(ctx2, locked_only=True)
    assert "/knowledge" in locked2 and "/care-schedule" in locked2
    assert "/workspace" in _nav_paths(ctx2, locked_only=False)

    # flagship：与静态全量等价（零锁标）
    monkeypatch.setattr(
        "src.licensing.license_manager.get_license_manager",
        lambda: SimpleNamespace(status=lambda: _st(plan="flagship")),
    )
    ctx3 = ns.get_nav_context(_nav_vis())
    assert not _nav_paths(ctx3, locked_only=True)
    # flagship 零锁标。显隐层仍按缺省藏矩阵/群脉/未开变现，不等于静态全量。
    assert "/workspace" in _nav_paths(ctx3)
    assert "/knowledge" in _nav_paths(ctx3)
    assert "/monetization" not in _nav_paths(ctx3)
