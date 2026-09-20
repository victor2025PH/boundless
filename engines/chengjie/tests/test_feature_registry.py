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
    product_baseline_values,
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
    # 空配置 → 全部 A 类入 patch，且值＝注册表**声明值**（2026-08-20 起 baseline
    # 不再一律为 True：contacts.mode 声明 "lite"，按 True 断言会把档位型基线判错）
    patch = baseline_patch({})
    assert patch == product_baseline_values()
    # 2026-09-06 D-L3（#210 L-1）：bubbles 由 A 降 B——拆条出厂关，不再进基线补齐
    assert "inbox.reply_style.bubbles.enabled" not in patch
    assert by_key("inbox.reply_style.bubbles.enabled").cls == "B"
    assert set(patch) == {"companion.goals.enabled",
                          "companion.deep_persona.enabled",
                          "avatar_voice._hosted_auto",
                          "voice_recognition._hosted_auto",
                          "vision._hosted_auto",
                          "inbox.auto_draft.media_degrade_reply",
                          "companion.goals.notify.enabled",
                          "telegram.poll_fallback.mirror_outgoing",
                          # 2026-08-22 全自动开箱配套：两个防双发守卫升 A 类
                          # （纯软件零依赖、只减发送不增，B41 防线默认在岗）
                          "inbox.outbound_dup_guard.enabled",
                          "inbox.l2_autosend.fresh_guard.enabled",
                          "contacts.enabled",
                          "contacts.mode",
                          "contacts.origin_profile.enabled",
                          # 2026-08-23 小智全家桶进基线（1.0.51 全功能开箱：
                          # 1.0.50 发布说明宣传了小智、包里开关却是关的）
                          "assistant.enabled",
                          "assistant.agent.enabled",
                          "assistant.vision.enabled",
                          # 2026-09-06 L-5 D-L1：老板决策 D1（#166 冲刺推进器）/
                          # D7（#185 客户安全预警留痕+升级）出厂开进基线——此前只在
                          # cloud_light（仅全新安装）与内测种子（仅 smart 包）里，
                          # 走 clean 包升级的内测机两条路都没经过
                          "companion.goals.sprint.enabled",
                          "companion.wellbeing.crisis_audit",
                          "companion.wellbeing.crisis_escalation",
                          # 2026-09-08 O-2 A D-O5（#201 WYNN22）：情景记忆抽取白名单进
                          # 基线——clean 包 min 种子此前没有任何 memory 块，全新安装
                          # intents=[] 从首日起不抽（第三次「出厂默认没进基线」）
                          "memory.extract.enabled",
                          "memory.extract.use_llm",
                          "memory.extract.intents",
                          # 2026-09-08 O-1 D D-O4（#252 #254 FW78ZP）：拟人节奏出厂基线
                          # ——三档 profile=natural / 入站连发合并
                          "inbox.l2_autosend.deliver_delay.profile",
                          "inbox.auto_draft.inbound_merge.enabled",
                          # 2026-09-09 Q-4（Q-5 代，#267）：摸底目标 LLM 摘录补槽进基线
                          "companion.goals.profile_llm.enabled"}
    # 2026-09-09 D-Q1（#267 KYHGSZ）：班表三键撤出基线（A→B 出厂关；红线②③）
    for k in ("inbox.work_schedule.enabled", "inbox.work_schedule.default.start",
              "inbox.work_schedule.default.end"):
        assert k not in patch
    assert by_key("inbox.work_schedule.enabled").cls == "B"
    assert by_key("inbox.work_schedule.default.start") is None
    # D-Q2：额度闸门入表为 B（默认关、人工永不限）
    assert by_key("companion_send_gate.enabled").cls == "B"
    assert patch["contacts.mode"] == "lite"
    assert patch["inbox.l2_autosend.deliver_delay.profile"] == "natural"
    assert patch["companion.goals.profile_llm.enabled"] is True
    assert patch["memory.extract.intents"] == [
        "direct_chat", "small_talk", "greeting", "complaint"]
    # 列表型 baseline 必须是副本：改 patch 里的列表不得污染注册表声明值
    patch["memory.extract.intents"].append("order_query")
    assert by_key("memory.extract.intents").baseline == [
        "direct_chat", "small_talk", "greeting", "complaint"]
    # 显式 false = 用户决定，绝不覆盖；其余缺失键照补
    part = baseline_patch({"companion": {"goals": {"enabled": False}}})
    assert "companion.goals.enabled" not in part
    assert "companion.deep_persona.enabled" in part
    # 全部已表态（开/关混合）→ 零 patch
    assert baseline_patch({
        "companion": {"goals": {"enabled": True,
                                "notify": {"enabled": True},
                                # L-5 D-L1 三键：一关两开＝显式表态都不被覆盖
                                "sprint": {"enabled": False},
                                "profile_llm": {"enabled": False}},
                      "wellbeing": {"crisis_audit": True,
                                    "crisis_escalation": False},
                      "deep_persona": {"enabled": False}},
        "inbox": {"reply_style": {"bubbles": {"enabled": True}},
                  "auto_draft": {"media_degrade_reply": True,
                                 "inbound_merge": {"enabled": False}},
                  # 守卫一开一关＝显式表态的两种形态都不被覆盖
                  "outbound_dup_guard": {"enabled": True},
                  # O-1 D：显式 custom（旧模型）/ 班表显式关 + 自定班次 → 一字不动
                  "l2_autosend": {"fresh_guard": {"enabled": False},
                                  "deliver_delay": {"profile": "custom"}},
                  "work_schedule": {"enabled": False,
                                    "default": {"start": "09:00", "end": "23:00"}}},
        "avatar_voice": {"_hosted_auto": True},
        "voice_recognition": {"_hosted_auto": True},
        "vision": {"_hosted_auto": True},
        "telegram": {"poll_fallback": {"mirror_outgoing": True}},
        # 已显式选 full 的部署（内部/坐席包）不得被补成 lite——存量安装的档位是
        # 运营决定，基线补齐只负责「从未表态」的键。
        "contacts": {"enabled": True, "mode": "full",
                     "origin_profile": {"enabled": False}},
        # 小智三键（主开关开、子键一开一关＝显式表态不被覆盖）
        "assistant": {"enabled": True, "agent": {"enabled": False},
                      "vision": {"enabled": True}},
        # 记忆抽取三键：显式空白名单 = 用户关掉，与显式 false 同一条三态语义
        "memory": {"extract": {"enabled": True, "use_llm": False, "intents": []}},
    }) == {}


def test_baseline_patch_l5_factory_defaults_respect_explicit_false():
    """L-5 D-L1（2026-09-06）：sprint / crisis_audit / crisis_escalation 三键
    是 A 类基线——缺键补 True；用户显式写过 false（例如运营刻意关掉留痕）的
    机器一个字都不动（_ensure_baseline 三态语义）。"""
    trio = ("companion.goals.sprint.enabled",
            "companion.wellbeing.crisis_audit",
            "companion.wellbeing.crisis_escalation")
    full = baseline_patch({})
    assert all(full[k] is True for k in trio)
    # 模拟 skuio 机 1.0.74 clean 包升级态：goals/wellbeing 段存在但三键缺失 → 全补
    cfg = {"companion": {"goals": {"enabled": True},
                         "wellbeing": {"enabled": True}}}
    part = baseline_patch(cfg)
    assert all(part[k] is True for k in trio)
    # 显式 false 全部尊重；其中一键缺失仍单独补
    cfg = {"companion": {"goals": {"sprint": {"enabled": False}},
                         "wellbeing": {"crisis_audit": False}}}
    part = baseline_patch(cfg)
    assert "companion.goals.sprint.enabled" not in part
    assert "companion.wellbeing.crisis_audit" not in part
    assert part["companion.wellbeing.crisis_escalation"] is True


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
    # Q-8 E / D-Q5（#264）：proactive_topic C→B——无域信息时缺席=available（不再 locked）
    assert _f("proactive").cls == "B" and _f("proactive").reason == ""
    assert feature_state(_f("proactive"), {}) == "available"


def test_domain_defaults_only_b_class_and_companion_proactive_on():
    """Q-8 E / D-Q5：陪伴域缺席即开、销售域缺席仍关、显式值永远优先；域默认只许挂 B 类。"""
    from src.utils import feature_registry as fr
    key = "companion.proactive_topic.enabled"
    assert fr.DOMAIN_DEFAULTS["companion"][key] is True
    for dom, kv in fr.DOMAIN_DEFAULTS.items():
        for k in kv:
            assert fr.by_key(k) is not None and fr.by_key(k).cls == "B", (dom, k)
    assert fr.effective_flag({}, key, "companion") is True
    assert fr.effective_flag({}, key, "sales", fallback=False) is False
    assert fr.effective_flag({"companion": {"proactive_topic": {"enabled": False}}}, key, "companion") is False
    assert fr.effective_flag({"companion": {"proactive_topic": {"enabled": True}}}, key, "sales") is True
    assert feature_state(_f("proactive"), {}, "companion") == "on"
    assert feature_state(_f("proactive"), {}, "sales") == "available"
    assert feature_state(_f("proactive"), {"companion": {"proactive_topic": {"enabled": False}}}, "companion") == "available"
    # 红线②仍在：会改变发送行为的键不进 A 类；域默认不落 baseline_patch（不静默改写配置）
    assert fr.is_send_behavior_key(key) and key not in fr.baseline_patch({})
    assert key not in fr.seed_forbidden_map()


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
