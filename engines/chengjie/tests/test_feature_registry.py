"""功能交付注册表（src/utils/feature_registry，P1 单一事实源）门禁。

注册表同一张表驱动三个消费面（种子门禁 / 桌面基线补齐 / 功能总览 API），
本文件钉住：表结构不变量、纯函数语义（baseline_patch「只补缺失」/ 状态四态 /
依赖判定 / as_nested）、以及 **show=True 条目的 i18n 两语齐备**——功能总览的
name/desc/依赖/原因标签全部由路由按 slug/码动态拼键（fc_f_<slug> 等），模板
window.T 门禁扫不到动态键，缺词条会直接把裸键名亮给用户，只能在这里钉。
"""

from __future__ import annotations

from src.utils.feature_registry import (
    DEP_CHECKERS,
    FEATURES,
    as_nested,
    baseline_patch,
    by_key,
    dig,
    feature_state,
    missing_deps,
    product_baseline_map,
    seed_forbidden_map,
    ui_features,
)
from src.web.web_i18n import get_translations


def _f(slug: str):
    for f in FEATURES:
        if f.slug == slug:
            return f
    raise AssertionError(f"registry 缺 slug={slug}")


# ── 表结构不变量 ─────────────────────────────────────────────────────────────

def test_registry_shape():
    assert product_baseline_map(), "A 类不得为空"
    assert seed_forbidden_map(), "C 类不得为空"
    assert "companion.goals.enabled" in product_baseline_map()
    # A/C 由同一 cls 字段派生，天然互斥
    assert not set(product_baseline_map()) & set(seed_forbidden_map())
    for f in FEATURES:
        assert f.note, f"{f.key} 必须带内部备注（门禁红时的人话）"
        # 零依赖 B 合法（P2 起）：A vs B 的分界是「要不要默认开」这个产品决策，
        # 纯软件但未拍板进基线的功能（deep_persona/bubbles）就是零依赖 B。


def test_gate_feature_refs_resolve():
    """gate_feature 必须指向 feature_gate.FEATURE_MIN_PLAN 的真实 family——
    档位映射的单一事实源在 licensing 模块，这里只挂引用；拼错=静默恒放行。"""
    from src.licensing.feature_gate import FEATURE_MIN_PLAN
    bad = [f.key for f in FEATURES
           if f.gate_feature and f.gate_feature not in FEATURE_MIN_PLAN]
    assert not bad, f"gate_feature 指向不存在的 family: {bad}"


def test_by_key():
    assert by_key("companion.goals.enabled").slug == "goals"
    assert by_key("no.such.key") is None


# ── baseline_patch：只补缺失，尊重显式值 ────────────────────────────────────

def test_baseline_patch_only_fills_missing():
    # 空配置 → 全部 A 类入 patch（2026-07-31 拍板后 = goals + deep_persona + bubbles）
    patch = baseline_patch({})
    assert patch == {k: True for k in product_baseline_map()}
    assert set(patch) == {"companion.goals.enabled",
                          "companion.deep_persona.enabled",
                          "inbox.reply_style.bubbles.enabled"}
    # 显式 false = 用户决定，绝不覆盖；其余缺失键照补
    part = baseline_patch({"companion": {"goals": {"enabled": False}}})
    assert "companion.goals.enabled" not in part
    assert "companion.deep_persona.enabled" in part
    # 全部已表态（开/关混合）→ 零 patch
    assert baseline_patch({
        "companion": {"goals": {"enabled": True},
                      "deep_persona": {"enabled": False}},
        "inbox": {"reply_style": {"bubbles": {"enabled": True}}},
    }) == {}


def test_as_nested():
    assert as_nested({"a.b.c": True, "a.b.d": 1, "x": "y"}) == {
        "a": {"b": {"c": True, "d": 1}}, "x": "y"}
    assert as_nested({}) == {}


# ── 状态四态与依赖判定 ───────────────────────────────────────────────────────

def test_state_on_wins_regardless_of_cls():
    """运营机上 C 类经 overlay 打开是合法形态——总览必须如实报 on，不装「未开放」。"""
    cfg = {"avatar_voice": {"enabled": True}}
    assert feature_state(_f("avatar_voice"), cfg) == "on"


def test_state_locked_for_c_class_off():
    assert feature_state(_f("avatar_voice"), {}) == "locked"
    assert feature_state(_f("proactive"), {}) == "locked"


def test_state_available_vs_needs_dep():
    memvec = _f("memvec")
    assert feature_state(memvec, {}) == "needs_dep"
    assert missing_deps(memvec, {}) == ["embedding"]
    ok_cfg = {"ai": {"embedding_base_url": "http://127.0.0.1:11434"}}
    assert feature_state(memvec, ok_cfg) == "available"
    # 列表键优先形态也认
    urls_cfg = {"ai": {"embedding_base_urls": ["http://h:1"]}}
    assert feature_state(memvec, urls_cfg) == "available"
    # 空串/空列表不算配置
    assert feature_state(memvec, {"ai": {"embedding_base_url": " "}}) == "needs_dep"
    assert feature_state(memvec, {"ai": {"embedding_base_urls": ["", None]}}) == "needs_dep"


def test_translation_dep_codes():
    one = {"translation": {"engines": {"order": ["ai"]}}}
    two = {"translation": {"engines": {"order": ["ollama_mt", "ai"]}}}
    assert DEP_CHECKERS["translation_any"](one) is True
    assert DEP_CHECKERS["translation_any"]({}) is False
    assert DEP_CHECKERS["translation_multi"](one) is False
    assert DEP_CHECKERS["translation_multi"](two) is True
    assert feature_state(_f("xlate_conf"), one) == "needs_dep"
    assert feature_state(_f("xlate_conf"), two) == "available"
    assert feature_state(_f("out_xlate"), one) == "available"


def test_dig_soft_on_bad_shapes():
    assert dig(None, "a.b") is None
    assert dig({"a": "scalar"}, "a.b") is None
    assert dig({"a": {"b": 0}}, "a.b") == 0


# ── i18n 完备性（动态拼键的唯一门禁） ────────────────────────────────────────

def test_ui_feature_i18n_keys_bilingual():
    zh = get_translations("zh")
    en = get_translations("en")
    need = []
    for f in ui_features():
        need += [f"fc_f_{f.slug}", f"fc_f_{f.slug}_d"]
        for code in f.requires:
            need.append(f"fc_dep_{code}")
        if f.cls == "C":
            need.append(f"fc_rsn_{f.reason}")
    need += ["fc_state_on", "fc_state_available", "fc_state_needs_dep",
             "fc_state_locked", "fc_state_needs_upgrade", "fc_rsn_upgrade",
             "fc_meta_version", "fc_meta_plan", "fc_js_missing",
             "err.fc.readonly", "err.fc.unknown", "err.fc.locked",
             "err.fc.needs_dep", "err.fc.needs_plan", "err.fc.write_failed"]
    missing = [k for k in need if k not in zh or k not in en]
    assert not missing, f"功能总览 i18n 键缺失（zh/en 必须齐备）: {missing}"
