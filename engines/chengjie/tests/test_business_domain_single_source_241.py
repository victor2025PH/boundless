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

from src.companion.goals.store import reset_goal_store
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
    reset_goal_store()
    yield
    bdm.reset_active_business_domain()
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
