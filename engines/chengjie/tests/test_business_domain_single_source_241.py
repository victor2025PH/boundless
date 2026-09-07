# -*- coding: utf-8 -*-
"""N-3 #241（D-N1，2026-09-08）：业务域单一来源——陪伴人设别再跑在销售域物料上。

skuio 3NVM7Q / TN736F / VAQGZY 实录：13:14 启动同一秒 ``Domain hook registered:
ConversionDomainHook`` + ``Domain persona set: 线上陪伴`` + ``KB categories set from
domain 'conversion': [产品价值, 常见问题, 转化话术, 异议处理, 命理, 其他]``；工作目标
「客户摸底」标签是 BANT 六项、右栏画像同一套、「标成交」弹产品 / 金额。

根因不是装错包（conversion 包在代码里就是陪伴包：十几处 ``effective_domain_name ==
"conversion"`` 当陪伴判定），而是挂在包上的销售物料没有「这台机器做什么生意」这个真值
可读。本文件钉住：

A. ``business_domain`` 单一真值：显式配置 > 部署形态推导（桌面客户机 → companion，
   服务器 → sales，payment 等业务包 → sales）；缺省推导一次并写 overlay；
   ``effective_domain_name`` **不变**（仍是 conversion——别有人用换包来「修」）。
   域包装配层按它选 hook 类（陪伴 → CompanionDomainHook）与 KB 分类变体（陪伴集不含
   销售 / 命理）；启动日志一行 ``business_domain=companion（hook=…，pack=…）``。
   模板库 / 路由 / 后台生命周期自建：陪伴域一律不下发、不建「转化成交」类目
   （M-5 A 的用户版隐藏升成域级规则）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals import profile_slots as ps
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.companion.goals.templates import (
    COMPANION_HIDDEN_KINDS,
    TEMPLATES,
    hidden_template_kinds,
    is_hidden_template,
    list_templates,
)
from src.utils import business_domain as bdm
from src.utils.domain_loader import DomainLoader
from src.utils.domain_policy import effective_domain_name
from src.web.routes.goal_routes import register_goal_routes

REPO = Path(__file__).resolve().parents[1]
DOMAINS = REPO / "domains"
CONV = "telegram:a1:100"
CONVERSION_IDS = {"conversion_unlock", "conversion_subscribe",
                  "acquire_and_convert", "retention_expand"}
SALES_KB_CATS = {"产品价值", "转化话术", "异议处理", "命理"}


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    bdm.reset_active_business_domain()
    ps.clear_custom_slots()
    reset_goal_store()
    yield
    bdm.reset_active_business_domain()
    ps.clear_custom_slots()
    reset_goal_store()


# ── A1 单一真值：解析序 ─────────────────────────────────────────────────────

def test_normalize_accepts_aliases_and_rejects_junk():
    assert bdm.normalize_business_domain("companion") == "companion"
    assert bdm.normalize_business_domain(" 陪伴 ") == "companion"
    assert bdm.normalize_business_domain("Sales") == "sales"
    assert bdm.normalize_business_domain("销售") == "sales"
    assert bdm.normalize_business_domain("conversion") == "sales"   # 旧词：域包名≠陪伴
    assert bdm.normalize_business_domain("") == ""
    assert bdm.normalize_business_domain("whatever") == ""
    assert bdm.normalize_business_domain(None) == ""


def test_explicit_config_wins_over_form(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")          # 桌面客户机
    assert bdm.resolve_business_domain({"business_domain": "sales"}) == "sales"
    monkeypatch.delenv("AITR_DESKTOP_MODE")
    assert bdm.resolve_business_domain({"business_domain": "陪伴"}) == "companion"
    # ConfigManager 形态（带 .config）同样认
    assert bdm.resolve_business_domain(
        SimpleNamespace(config={"business_domain": "companion"})) == "companion"


def test_inference_desktop_client_is_companion_server_is_sales(monkeypatch):
    assert bdm.infer_business_domain({}) == "sales"                    # 服务器：零变化
    assert bdm.infer_business_domain({"domain": "conversion"}) == "sales"
    assert bdm.infer_business_domain({"app": {"desktop_mode": True}}) == "companion"
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert bdm.infer_business_domain({}) == "companion"
    assert bdm.infer_business_domain({"domain": "conversion"}) == "companion"
    # 显式装了业务域包（payment / ecommerce …）→ 就是做生意的，桌面也不推陪伴
    assert bdm.infer_business_domain(
        {"domain": "payment", "domain_plugins": {"payment": {"enabled": True}}}) == "sales"
    assert bdm.infer_business_domain({"domain": "ecommerce"}) == "sales"
    # payment 关插件回落 conversion → 按 conversion 处理（桌面 → 陪伴）
    assert bdm.infer_business_domain(
        {"domain": "payment", "domain_plugins": {"payment": {"enabled": False}}}) == "companion"


def test_effective_domain_name_unchanged_for_companion():
    """陪伴域仍装 conversion 包——陪伴 system prompt / 人设 / 十几处 == "conversion"
    的陪伴判定都在那里。业务域与域包名正交，别用换包来修错配。"""
    assert effective_domain_name({"business_domain": "companion"}) == "conversion"
    assert effective_domain_name({}) == "conversion"
    assert effective_domain_name(
        {"domain": "payment", "domain_plugins": {"payment": {"enabled": False}}}) == "conversion"


class _CM:
    """ConfigManager 桩：只带 .config 与 set_overlay_flag。"""

    def __init__(self, cfg, ok=True):
        self.config = dict(cfg)
        self.writes = []
        self._ok = ok

    def set_overlay_flag(self, path, value):
        self.writes.append((path, value))
        if self._ok:
            self.config[path] = value
            return True, "已保存"
        return False, "只读"


def test_ensure_persists_inferred_value_once(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    cm = _CM({})
    bd, persisted = bdm.ensure_business_domain(cm)
    assert (bd, persisted) == ("companion", True)
    assert cm.writes == [("business_domain", "companion")]
    assert bdm.active_business_domain() == "companion"
    # 第二次：已显式 → 不再写
    bd2, persisted2 = bdm.ensure_business_domain(cm)
    assert (bd2, persisted2) == ("companion", False)
    assert len(cm.writes) == 1


def test_ensure_never_raises_and_tolerates_readonly_or_dict(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    cm = _CM({}, ok=False)
    assert bdm.ensure_business_domain(cm) == ("sales", False)
    assert bdm.ensure_business_domain({"business_domain": "companion"}) == ("companion", False)
    assert bdm.ensure_business_domain(None) == ("sales", False)


def test_describe_line_shape():
    bdm.set_active_business_domain("companion")
    line = bdm.describe(hook_name="CompanionDomainHook", pack="conversion")
    assert line == "business_domain=companion（hook=CompanionDomainHook，pack=conversion）"
    assert bdm.business_domain_label("companion") == "陪伴"
    assert bdm.business_domain_label("sales", "en") == "Sales"


# ── A2 域包装配层：hook 类 + KB 分类变体随业务域 ────────────────────────────

class _Skill:
    pass


def _load_conversion(cfg):
    from src.hooks.registry import HookRegistry
    HookRegistry.reset()
    loader = DomainLoader(DOMAINS)
    return loader.load("conversion", _Skill, None, SimpleNamespace(config=cfg, config_path=None))


def test_companion_loads_companion_hook_and_kb_categories(caplog):
    caplog.set_level(logging.INFO)
    pack = _load_conversion({"business_domain": "companion"})
    assert pack is not None
    assert pack.business_domain == "companion"
    assert pack.hook_class.__name__ == "CompanionDomainHook"
    names = [c["name"] if isinstance(c, dict) else c for c in pack.kb_categories]
    assert names and set(names).isdisjoint(SALES_KB_CATS), names
    assert "其他" in names
    # 陪伴 system prompt / 人设仍来自同一个包（没换包）
    assert "companion" in pack.system_prompt.lower()
    assert pack.persona.get("name") == "线上陪伴"
    assert bdm.active_business_domain() == "companion"
    assert any("business_domain=companion（hook=CompanionDomainHook，pack=conversion）" in r.message
               for r in caplog.records)


def test_sales_loads_conversion_hook_and_original_categories():
    pack = _load_conversion({"business_domain": "sales"})
    assert pack.business_domain == "sales"
    assert pack.hook_class.__name__ == "ConversionDomainHook"
    names = [c["name"] if isinstance(c, dict) else c for c in pack.kb_categories]
    assert SALES_KB_CATS <= set(names)


def test_manifest_declares_variant_and_hooks_table():
    import importlib
    import yaml
    man = yaml.safe_load((DOMAINS / "conversion" / "manifest.yaml").read_text(encoding="utf-8"))
    alt = man["kb"]["categories_by_business_domain"]["companion"]
    assert (DOMAINS / "conversion" / alt).exists()
    mod = importlib.import_module("domains.conversion.hooks")
    table = getattr(mod, "HOOKS_BY_BUSINESS_DOMAIN")
    assert set(table) == {"companion", "sales"}
    assert table["companion"].__name__ == "CompanionDomainHook"
    assert table["sales"].__name__ == "ConversionDomainHook"


def test_loader_ignores_business_domain_failure(monkeypatch):
    """业务域解析炸了不能拖垮域包装载：回落 sales，整包原样。"""
    monkeypatch.setattr(bdm, "ensure_business_domain",
                        lambda cm: (_ for _ in ()).throw(RuntimeError("boom")))
    pack = _load_conversion({"business_domain": "companion"})
    assert pack is not None and pack.business_domain == "sales"


# ── A3 模板库：陪伴域不下发「转化成交」（域级，形态无关） ─────────────────────

def test_hidden_kinds_union_of_client_hide_and_companion():
    assert COMPANION_HIDDEN_KINDS == ("conversion",)
    assert hidden_template_kinds() == ()
    assert hidden_template_kinds(client_hide=True) == ("conversion",)
    assert hidden_template_kinds(business_domain="companion") == ("conversion",)
    assert hidden_template_kinds(business_domain="sales") == ()
    for tid in CONVERSION_IDS:
        assert is_hidden_template(tid, business_domain="companion")
        assert not is_hidden_template(tid, business_domain="sales")
    assert not is_hidden_template("custom", business_domain="companion")
    assert not is_hidden_template("nope", business_domain="companion")


def test_list_templates_by_business_domain():
    full = {t["id"] for t in list_templates()}
    assert full == set(TEMPLATES)
    comp = {t["id"] for t in list_templates(business_domain="companion")}
    assert comp == full - CONVERSION_IDS
    assert {t["id"] for t in list_templates(business_domain="sales")} == full


def _build_client(cfg_extra=None, flavor="internal", developer_mode=False):
    goals = {"enabled": True, "db_path": ":memory:"}
    cfg = {"companion": {"goals": goals}, "ui_visibility": {"flavor": flavor}}
    cfg.update(cfg_extra or {})
    sess = {"role": "", "user": "tester"}
    if developer_mode:
        sess.update({"developer_mode": True, "dev_unlocked": True})
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app)


@pytest.mark.parametrize("flavor,dev", [("internal", False), ("partner", False),
                                        ("client", True)])
def test_templates_endpoint_companion_hides_conversion_regardless_of_form(flavor, dev):
    client = _build_client({"business_domain": "companion"}, flavor=flavor, developer_mode=dev)
    d = client.get("/api/goals/templates").json()
    assert d["business_domain"] == "companion"
    ids = {t["id"] for t in d["templates"]}
    assert ids.isdisjoint(CONVERSION_IDS) and "custom" in ids and "profile_discovery" in ids


def test_templates_endpoint_sales_internal_keeps_all():
    client = _build_client({"business_domain": "sales"}, flavor="internal")
    d = client.get("/api/goals/templates").json()
    assert d["business_domain"] == "sales"
    assert {t["id"] for t in d["templates"]} == set(TEMPLATES)


def test_companion_cannot_create_conversion_goal_even_internal():
    client = _build_client({"business_domain": "companion"}, flavor="internal")
    r = client.post("/api/goals", json={"template": "acquire_and_convert",
                                         "conversation_id": CONV})
    assert r.status_code == 403
    r2 = client.post("/api/goals", json={"template": "profile_discovery",
                                          "conversation_id": CONV,
                                          "params": {"slots": "age,occupation"}})
    assert r2.status_code == 200 and r2.json()["ok"] is True


def test_lifecycle_auto_create_blocked_in_companion_domain():
    from src.companion.goals import product_guard as pg
    cfg = {"companion": {"goals": {"enabled": True}}, "ui_visibility": {"flavor": "internal"},
           "business_domain": "companion"}
    assert pg.lifecycle_template_blocked(cfg, "acquire_and_convert") is True
    assert pg.lifecycle_template_blocked(cfg, "custom") is False
    cfg["business_domain"] = "sales"
    assert pg.lifecycle_template_blocked(cfg, "acquire_and_convert") is False
    assert pg.lifecycle_template_blocked({}, "acquire_and_convert") is False   # 服务器零变化


# ── A4 配置基线：桌面客户包出厂就是陪伴，且不是靠改 domain ─────────────────

def test_desktop_baseline_and_companion_preset_declare_business_domain():
    import yaml
    for rel in ("config/config.desktop.min.yaml", "config/presets/companion.yaml"):
        data = yaml.safe_load((REPO / rel).read_text(encoding="utf-8"))
        assert data.get("business_domain") == "companion", rel
        assert data.get("domain", "conversion") == "conversion", rel


def test_developer_route_reads_and_writes_business_domain():
    from src.web.routes.ui_visibility_routes import register_ui_visibility_routes
    cm = _CM({"business_domain": "sales"})
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"dev_unlocked": True, "username": "dev"}

    register_ui_visibility_routes(app, auth_dep, cm)
    c = TestClient(app)
    d = c.get("/api/developer/business-domain").json()
    assert d["business_domain"] == "sales" and d["explicit"] == "sales"
    assert {o["id"] for o in d["options"]} == {"companion", "sales"}
    r = c.post("/api/developer/business-domain", json={"business_domain": "陪伴"})
    assert r.status_code == 200
    body = r.json()
    assert body["business_domain"] == "companion" and body["restart_required"] is True
    assert cm.writes == [("business_domain", "companion")]
    assert bdm.active_business_domain() == "companion"
    assert c.post("/api/developer/business-domain",
                  json={"business_domain": "junk"}).status_code == 400


# ═══ B 陪伴域摸底标签集（D-N1）═══════════════════════════════════════════════

RELATION = ["name", "location", "occupation", "age", "interests"]
BANT = ["need", "channel", "team_size", "budget", "authority", "timeline"]
PERSONAL = ["family_status", "marital_status", "income_level", "residence", "assets"]


def test_slot_tables_by_domain_and_legacy_registry_untouched():
    # 旧契约：SLOTS 仍是销售域表（relation + bant + lifecycle = 12）
    assert [s["key"] for s in ps.SLOTS] == RELATION + BANT + ["churn_reason"]
    assert [s["key"] for s in ps.PERSONAL_SLOTS] == PERSONAL
    assert [s["key"] for s in ps.slots_for_domain("companion")] == RELATION + PERSONAL
    assert [s["key"] for s in ps.slots_for_domain("sales")] == RELATION + BANT + ["churn_reason"]
    assert ps.tracks_for("companion") == ("relation", "personal")
    assert ps.tracks_for("sales") == ("relation", "bant")
    assert ps.secondary_track("companion") == "personal"
    assert ps.secondary_track("sales") == "bant"
    # 缺省读进程级 active（装配层登记）
    bdm.set_active_business_domain("companion")
    assert [s["key"] for s in ps.slots_for_domain()] == RELATION + PERSONAL
    bdm.set_active_business_domain("sales")
    assert ps.secondary_track() == "bant"
    # 全表都认：陪伴机器上升级前填的 BANT 值仍可读写
    for k in RELATION + BANT + PERSONAL + ["churn_reason"]:
        assert ps.get_slot(k) is not None, k
    assert set(ps.slot_keys("personal")) == set(PERSONAL)
    assert len(ps.slot_keys("bant")) == 6 and len(ps.slot_keys("relation")) == 5


def test_sensitive_slots_default_unchecked_and_prompt_hard_constraint():
    assert ps.slot_is_sensitive("income_level") and ps.slot_is_sensitive("assets")
    for k in RELATION + BANT + ["family_status", "marital_status", "residence"]:
        assert not ps.slot_is_sensitive(k), k
    # 摸底模板缺省勾选不含敏感项
    default = TEMPLATES["profile_discovery"]["params"][0]["default"].split(",")
    assert set(default).isdisjoint({"income_level", "assets"})
    assert "BANT" not in TEMPLATES["profile_discovery"]["params"][0]["help_zh"]
    assert "team_size" not in TEMPLATES["profile_discovery"]["params"][0]["help_zh"]
    # UI 建议问法干净；注入短语带硬约束
    assert "敏感" not in ps.slot_ask("income_level")
    assert ps.SENSITIVE_ASK_DISCIPLINE in ps.inject_ask("income_level")
    assert ps.SENSITIVE_ASK_DISCIPLINE not in ps.inject_ask("family_status")
    phrase, key, _patch = ps.resolve_inject_gap({}, {}, include=["income_level", "age"])
    assert key == "income_level" and ps.SENSITIVE_ASK_DISCIPLINE in phrase
    assert "绝不直接问" in phrase and "绝不追问" in phrase
    phrase2, key2, _ = ps.resolve_inject_gap({}, {}, include=["age", "assets"])
    assert key2 == "age" and ps.SENSITIVE_ASK_DISCIPLINE not in phrase2
    # 陪伴域缺省缺口轨 = personal（不再问「业务痛点、预算档」）
    hint = ps.gap_hint({}, business_domain="companion")
    assert "家里" in hint and "头疼" not in hint
    hint_sales = ps.gap_hint({}, business_domain="sales")
    assert "头疼" in hint_sales
    # 敏感槽进 gap_hint 也带约束
    hint_sens = ps.gap_hint({}, include=["assets"], limit=1)
    assert ps.SENSITIVE_ASK_DISCIPLINE in hint_sens


def test_fill_rates_and_missing_by_domain_keep_all_track_keys():
    fields = {"family_status": {"v": "有孩子"}, "budget": {"v": "500刀"}}
    comp = ps.fill_rates(fields, business_domain="companion")
    assert comp["tracks"] == ["relation", "personal"] and comp["total"] == 10
    assert comp["personal"] == pytest.approx(2 / 7, abs=0.01)   # weight 2 / (2+2+1+1+1)
    assert comp["bant"] == pytest.approx(2 / 11, abs=0.01)      # 键恒在（消费方 .get 不炸）
    assert comp["filled"] == 1                                   # 只数陪伴表
    sales = ps.fill_rates(fields, business_domain="sales")
    assert sales["tracks"] == ["relation", "bant"] and sales["total"] == 11
    assert sales["filled"] == 1
    miss_c = [m["key"] for m in ps.missing_slots({}, track="", limit=99, business_domain="companion")]
    assert miss_c == RELATION + PERSONAL
    miss_s = [m["key"] for m in ps.missing_slots({}, track="", limit=99, business_domain="sales")]
    assert miss_s == RELATION + BANT
    assert [m["key"] for m in ps.missing_slots({}, track="personal", limit=99)] == PERSONAL


def test_parse_selected_accepts_all_domains_for_legacy_goals():
    # 陪伴机器上 1.0.76 建的 BANT 摸底目标：勾选仍解析、仍结算（存量不丢）
    assert ps.parse_selected_slots("age,budget,family_status,x_nope,churn_reason") == [
        "age", "budget", "family_status"]
    assert ps.selected_fill_rate({"budget": {"v": "1"}}, ["budget", "assets"]) == 0.5


def test_capture_marital_and_family_closed_sets():
    got = dict(ps.capture_from_text("我现在单身，我一个人住"))
    assert got["marital_status"] == "单身" and got["family_status"] == "独居"
    # 宁可漏采：没有第一人称锚的「一个人住」不采
    assert "family_status" not in dict(ps.capture_from_text("一个人住挺自在的"))
    assert dict(ps.capture_from_text("我有个女儿今年三岁")).get("family_status") == "有孩子"
    assert dict(ps.capture_from_text("I'm married and I have two kids"))["marital_status"] == "已婚"
    assert dict(ps.capture_from_text("我离婚了")).get("marital_status") == "离异"
    assert dict(ps.capture_from_text("我谈了个男朋友")).get("marital_status") == "恋爱中"
    # 否定 / 他人 / 泛谈不采；收入资产永不自动采
    for t in ("我不是单身", "我朋友单身很久了", "你单身吗", "我月薪两万", "我有两套房"):
        d = dict(ps.capture_from_text(t))
        assert "marital_status" not in d and "income_level" not in d and "assets" not in d, t


def test_custom_slots_registry_and_config_round_trip():
    k = ps.custom_slot_key("家乡")
    assert k.startswith("x_") and len(k) == 12 and k == ps.custom_slot_key(" 家乡 ")
    assert ps.custom_slot_key("") == ""
    slots = ps.register_custom_slots(["家乡", {"label": "宠物", "ask_zh": "养什么宠物", "sensitive": True}, "", "家乡"])
    assert [s["label_zh"] for s in slots] == ["家乡", "宠物"]
    assert ps.get_slot(k)["custom"] is True and ps.get_slot(k)["track"] == "custom"
    assert ps.slot_is_sensitive(ps.custom_slot_key("宠物"))
    assert ps.slot_ask(ps.custom_slot_key("宠物")) == "养什么宠物"
    assert ps.custom_slot_labels() == ["家乡", "宠物"]
    # 两域槽位表末尾都带自定义；parse / 缺口都认
    assert [s["key"] for s in ps.slots_for_domain("companion")][-2:] == [k, ps.custom_slot_key("宠物")]
    assert [s["key"] for s in ps.slots_for_domain("sales")][-2:] == [k, ps.custom_slot_key("宠物")]
    assert ps.parse_selected_slots(f"age,{k}") == ["age", k]
    assert [m["key"] for m in ps.missing_slots({}, track="custom", limit=9)] == [k, ps.custom_slot_key("宠物")]
    assert k in ps.facts_line({k: {"v": "潮汕", "src": "agent"}}) or "家乡:潮汕" in ps.facts_line({k: {"v": "潮汕"}})
    # 上限 12；坏配置 → 空表
    ps.register_custom_slots([f"标签{i}" for i in range(20)])
    assert len(ps.custom_slots()) == 12
    assert ps.load_custom_slots_from_config({"companion": {"goals": {"custom_slots": "junk"}}}) == []
    assert [s["label_zh"] for s in ps.load_custom_slots_from_config(
        {"companion": {"goals": {"custom_slots": ["家乡"]}}})] == ["家乡"]


def test_store_accepts_personal_and_custom_slot_values():
    ps.register_custom_slots(["家乡"])
    k = ps.custom_slot_key("家乡")
    store = get_goal_store(":memory:")
    row = store.upsert_customer_profile("telegram", "u9", {
        "family_status": "有孩子", "income_level": "中等", k: "潮汕", "bogus": "x"},
        source="agent", overwrite=True)
    assert row["fields"]["family_status"]["v"] == "有孩子"
    assert row["fields"]["income_level"]["v"] == "中等"
    assert row["fields"][k]["v"] == "潮汕"
    assert "bogus" not in row["fields"]


# ── B 路由：摸底 chips 与画像 schema 同源、按域、存量不丢 ─────────────────────

def _cm_client(cfg_extra=None, flavor="internal"):
    goals = {"enabled": True, "db_path": ":memory:"}
    cfg = {"companion": {"goals": goals}, "ui_visibility": {"flavor": flavor}}
    cfg.update(cfg_extra or {})
    cm = _CM(cfg)
    sess = {"role": "", "user": "tester", "username": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, cm)
    return TestClient(app), cm


def test_pickers_discovery_slots_follow_domain_with_sensitive_flags():
    client = _build_client({"business_domain": "companion"})
    pk = client.get("/api/goals/templates").json()["pickers"]["discovery_slots"]
    keys = [s["key"] for s in pk]
    assert keys == RELATION + PERSONAL
    flags = {s["key"]: s["sensitive"] for s in pk}
    assert flags["income_level"] is True and flags["assets"] is True and flags["age"] is False
    client_s = _build_client({"business_domain": "sales"})
    pk_s = client_s.get("/api/goals/templates").json()["pickers"]["discovery_slots"]
    assert [s["key"] for s in pk_s] == RELATION + BANT


def test_profile_view_companion_schema_same_as_chips_and_keeps_legacy_values():
    client = _build_client({"business_domain": "companion"})
    # 升级前（销售表）填过的预算档：不在陪伴表里，但有值 → extra 回显，不丢
    store = get_goal_store(":memory:")
    store.upsert_customer_profile("telegram", "u1", {"budget": "500刀", "age": "30岁"},
                                  source="agent", overwrite=True)
    d = client.get("/api/goals/profile?platform=telegram&chat_key=u1").json()
    assert d["business_domain"] == "companion"
    assert d["tracks"] == ["relation", "personal"] and d["secondary_track"] == "personal"
    keys = [s["key"] for s in d["slots"]]
    assert keys[:10] == RELATION + PERSONAL          # 与摸底 chips 同一份表
    assert "need" not in keys and "team_size" not in keys
    extra = [s for s in d["slots"] if s.get("extra")]
    assert [s["key"] for s in extra] == ["budget"] and extra[0]["value"] == "500刀"
    sens = {s["key"] for s in d["slots"] if s.get("sensitive")}
    assert sens == {"income_level", "assets"}
    # 缺口 chips 不含敏感槽（「拟稿去问」= 直接问；敏感项只走补录 / 注入链自然带出）
    assert d["missing"] == ["family_status", "marital_status", "residence"]
    assert d["missing_bant"] == []
    assert d["fill"]["tracks"] == ["relation", "personal"]
    # 保存陪伴槽 → 同形回包
    r = client.post("/api/goals/profile", json={
        "platform": "telegram", "chat_key": "u1", "fields": {"marital_status": "单身"}})
    assert r.status_code == 200
    vals = {s["key"]: s["value"] for s in r.json()["slots"]}
    assert vals["marital_status"] == "单身" and vals["budget"] == "500刀"
    assert "marital_status" not in r.json()["missing"]


def test_profile_view_sales_shape_unchanged():
    client = _build_client({"business_domain": "sales"})
    d = client.get("/api/goals/profile?platform=telegram&chat_key=u2").json()
    assert [s["key"] for s in d["slots"]] == RELATION + BANT + ["churn_reason"]
    assert len(d["missing_bant"]) == 6 and d["missing"] == d["missing_bant"]
    assert d["tracks"] == ["relation", "bant"]


def test_custom_slot_endpoints_persist_and_show_everywhere():
    client, cm = _cm_client({"business_domain": "companion"})
    r = client.post("/api/goals/custom-slots", json={"label": "家乡"})
    assert r.status_code == 200
    body = r.json()
    k = ps.custom_slot_key("家乡")
    assert body["added_key"] == k
    assert cm.writes == [("companion.goals.custom_slots", ["家乡"])]
    assert [s["key"] for s in body["discovery_slots"]][-1] == k
    # 幂等：同名不重复
    client.post("/api/goals/custom-slots", json={"label": "家乡"})
    assert cm.writes[-1][1] == ["家乡"]
    # 摸底 chips 与画像卡同步出现
    pk = client.get("/api/goals/templates").json()["pickers"]["discovery_slots"]
    assert pk[-1]["key"] == k and pk[-1]["custom"] is True and pk[-1]["label_zh"] == "家乡"
    d = client.get("/api/goals/profile?platform=telegram&chat_key=u5").json()
    assert d["slots"][-1]["key"] == k and d["slots"][-1].get("custom") is True
    # 摸底目标可勾自定义键并结算
    g = client.post("/api/goals", json={"template": "profile_discovery", "conversation_id": CONV,
                                         "params": {"slots": f"age,{k}"}}).json()["goal"]
    assert g["params"]["slots"] in (f"age,{k}", [f"age", k])
    # 删除
    r2 = client.post("/api/goals/custom-slots", json={"remove": k})
    assert r2.status_code == 200 and cm.writes[-1][1] == []
    assert client.get("/api/goals/custom-slots").json()["custom_slots"] == []
    assert client.post("/api/goals/custom-slots", json={}).status_code == 400


def test_custom_slots_loaded_from_config_at_register():
    client = _build_client({"business_domain": "companion",
                            "companion": {"goals": {"enabled": True, "db_path": ":memory:",
                                                    "custom_slots": ["宠物"]}}})
    pk = client.get("/api/goals/templates").json()["pickers"]["discovery_slots"]
    assert pk[-1]["label_zh"] == "宠物" and pk[-1]["key"] == ps.custom_slot_key("宠物")


def test_i18n_ask_keys_cover_all_slots_and_cp_goal_wires_custom_and_sensitive():
    from src.web.i18n_packs.goals import EN, ZH
    for s in ps.ALL_SLOTS:
        for lang in (ZH, EN):
            assert f"inbox.goal.profile.ask.{s['key']}" in lang, s["key"]
    for key in ("inbox.goal.profile.track.personal", "inbox.goal.profile.track.custom",
                "inbox.goal.profile.track.extra", "inbox.goal.profile.sensitive_t",
                "inbox.goal.form.slot_sensitive_t", "inbox.goal.form.slot_custom_add",
                "inbox.goal.form.slot_custom_prompt", "inbox.goal.form.slot_custom_fail",
                "err.goals.custom_slot_label_required", "err.goals.custom_slot_limit"):
        assert key in ZH and key in EN, key
    js = (REPO / "shared" / "copilot" / "components" / "cp-goal.js").read_text(encoding="utf-8")
    js2 = (REPO / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js"
           ).read_text(encoding="utf-8")
    assert js == js2, "cp-goal.js 双树不一致"
    for needle in ('data-act="slot_custom_add"', "_addCustomSlot", "/api/goals/custom-slots",
                   'this.t("inbox.goal.form.slot_sensitive_t")', "s.sensitive", ".gl-chip.sens"):
        assert needle in js, needle
    assert "/^[a-z][a-z0-9_]*$/" in js       # 自定义键 x_<hex> 放行


# ═══ C 画像 schema 同源 + 「标成交」字段收起（VAQGZY / TN736F）══════════════════

def test_card_and_report_payloads_carry_business_domain():
    client = _build_client({"business_domain": "companion"})
    store = get_goal_store(":memory:")
    d0 = client.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert d0 == {"goal": None, "last": None}        # 无目标：旧契约原样（卡片此时不渲染画像/达成表单）
    g = store.create_goal(conversation_id=CONV, platform="telegram", account_id="a1",
                          chat_key="100", template="profile_discovery",
                          params={"slots": "age,family_status"}, autonomy="auto", deadline_days=10)
    assert g is not None
    d = client.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert d["business_domain"] == "companion"
    assert [s["key"] for s in d["goal"]["slots_progress"]] == ["age", "family_status"]
    rep = client.get("/api/goals/report/accounts?days=30").json()
    assert rep["ok"] is True and rep["business_domain"] == "companion"


def test_mark_achieved_meta_outcome_and_note_persist_without_product_amount():
    from src.companion.goals.service import sanitize_won_meta
    assert sanitize_won_meta({"outcome": "关系升温", "note": "约了周末见", "junk": 1}) == {
        "outcome": "关系升温", "note": "约了周末见"}
    assert sanitize_won_meta({"outcome": "x" * 80})["outcome"] == "x" * 40
    assert sanitize_won_meta({"product": "p", "amount": "9.5"}) == {"product": "p", "amount": 9.5}
    client = _build_client({"business_domain": "companion"})
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram", account_id="a1",
                          chat_key="100", template="relationship_stage", params={},
                          autonomy="auto", deadline_days=10)
    r = client.post(f"/api/goals/{g['goal_id']}/status",
                    json={"action": "done", "meta": {"outcome": "见面", "note": "线下咖啡"}})
    assert r.status_code == 200 and r.json()["goal"]["status"] == "done"
    import json as _json
    metas = [e for e in store.list_events(g["goal_id"]) if e["kind"] == "won_meta"]
    assert metas and _json.loads(metas[0]["detail"]) == {"outcome": "见面", "note": "线下咖啡"}
    from src.companion.goals.notify import build_completion_payload
    p = build_completion_payload(store.get_goal(g["goal_id"]), won_meta={"outcome": "见面"})
    assert p["outcome"] == "见面" and p["amount"] is None and p["product"] == ""


def test_cp_goal_renders_by_domain_and_report_hides_amount():
    js = (REPO / "shared" / "copilot" / "components" / "cp-goal.js").read_text(encoding="utf-8")
    for needle in ("_isCompanion()", "_tDomain(", "_wonFieldsHtml()", "_wonChecklistHtml(g)",
                   'data-ref="won_outcome"', 'data-ref="won_note"', "meta.outcome", "p.tracks",
                   'trackLabel("extra")', "!s.sensitive", 'this._tDomain("inbox.goal.act.won")'):
        assert needle in js, needle
    from src.web.i18n_packs.goals import EN, ZH
    for key in ("inbox.goal.act.won_c", "inbox.goal.outcome.confirm_c", "inbox.goal.won_meta_title_c",
                "inbox.goal.won_meta_confirm_c", "inbox.goal.won_meta_outcome", "inbox.goal.won_meta_note",
                "inbox.goal.won_outcome.warm", "inbox.goal.won_outcome.meet", "inbox.goal.won_outcome.paid",
                "inbox.goal.won_outcome.other", "inbox.goal.won_checklist_t", "inbox.goal.done.kind.manual_c",
                "goal_rpt_kind_manual_companion", "goal_rpt_th_won_companion"):
        assert key in ZH and key in EN, key
    assert "成交" not in ZH["inbox.goal.act.won_c"] and "成交" not in ZH["inbox.goal.won_meta_title_c"]
    html = (REPO / "src" / "web" / "templates" / "goal_report.html").read_text(encoding="utf-8")
    assert "body.gr-companion .gr-amount{display:none;}" in html
    assert "classList.toggle('gr-companion'" in html
    assert html.count('class="gr-amount"') >= 2 and 'class="gr-kpi gr-amount"' in html
