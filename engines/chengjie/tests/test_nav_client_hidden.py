# -*- coding: utf-8 -*-
"""后台可见性分层门禁（L-4 A 2026-09-06，D-L2 / D-L6 / D-L7；#197 #198 #212 #199 #211）。

三层形态 client ⊂ partner ⊂ internal × 开发者模式开关：
- client：CLIENT_HIDDEN_ITEM_IDS（运维/研发面）与 PARTNER_ONLY_ITEM_IDS（代理商面）
  从侧栏 / 简洁清单 / 命令面板全面剔除；URL 不封（藏而不废）；
- partner / internal：两表原样保留（代理商配白标、铺演示是正当业务）；
- 开发者模式（session 级，须 dev_unlocked）：client 形态下两表回归并带 tier 注解
  （侧栏「研发 / 代理商」角标）；partner / internal 形态下无注解（本就全显）；
- 路由：POST /api/developer/developer-mode 须 dev 解锁；只写 session 不落 overlay；
  /developer/logout 清 dev_unlocked 后开发者模式随之失效。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.web.nav_schema import (CLIENT_HIDDEN_ITEM_IDS, CMD_EXTRA_ITEMS,
                                NAV_ITEMS, PARTNER_ONLY_ITEM_IDS,
                                get_nav_context)
from src.web.ui_visibility import (FLAVOR_TIERS, client_hide_active,
                                   resolve_developer_mode, resolve_ui_flavor)

CLIENT = {"ui_visibility": {"flavor": "client"}}
PARTNER = {"ui_visibility": {"flavor": "partner"}}
INTERNAL = {"ui_visibility": {"flavor": "internal"}}
REPO = Path(__file__).resolve().parents[1]


def _all_items(ctx):
    out = []
    for g in ctx["nav_groups"]:
        out += [it for it in g["items"] if isinstance(it, dict)]
    out += [it for it in ctx["nav_simple_core"] if isinstance(it, dict)]
    out += [it for it in ctx["nav_simple_more"] if isinstance(it, dict)]
    out += [it for it in ctx["nav_cmd_items"] if isinstance(it, dict)]
    return out


def _keys(ctx):
    return {it.get("key") for it in _all_items(ctx)}


def _paths(ctx):
    return {it.get("path") for it in _all_items(ctx)}


def _partner_paths():
    return {CMD_EXTRA_ITEMS[i]["path"] for i in PARTNER_ONLY_ITEM_IDS}


# ── 清单本身（棘轮：七张单点名的页必须在表里） ──────────────────────────────

def test_client_hidden_list_covers_l4_pages():
    for cid in ("logs", "developer", "bug_tickets",
                "help", "strategies", "singing", "voice_eval"):
        assert cid in CLIENT_HIDDEN_ITEM_IDS, f"{cid} 应对用户版隐藏（D-L2 / D-L6）"
    # 学习队列按 D-L5 止血不隐藏；运营总览是老板读数面
    assert "learner" not in CLIENT_HIDDEN_ITEM_IDS
    assert "ops" not in CLIENT_HIDDEN_ITEM_IDS
    assert set(CLIENT_HIDDEN_ITEM_IDS) <= set(NAV_ITEMS)


def test_partner_only_list_points_to_settings_deep_links():
    assert set(PARTNER_ONLY_ITEM_IDS) == {"settings_brand", "settings_license", "settings_demo"}
    for i in PARTNER_ONLY_ITEM_IDS:
        it = CMD_EXTRA_ITEMS[i]
        assert it["path"].startswith("/settings#"), "代理商面入口是系统设置页三张卡的深链"
        assert it["key"] == "settings", "与系统设置同 key，剔除必须按 path"


# ── 形态三态 ─────────────────────────────────────────────────────────────────

def test_flavor_partner_is_recognised():
    assert FLAVOR_TIERS == ("client", "partner", "internal")
    assert resolve_ui_flavor(PARTNER) == "partner"
    assert resolve_ui_flavor({"ui_visibility": {"flavor": "PARTNER"}}) == "partner"
    # 桌面信号只推出 client，代理商版只能显式声明
    assert resolve_ui_flavor({"app": {"desktop_mode": True}}) == "client"


def test_client_flavor_hides_both_lists_everywhere():
    ctx = get_nav_context(CLIENT)
    keys, paths = _keys(ctx), _paths(ctx)
    for cid in CLIENT_HIDDEN_ITEM_IDS:
        assert cid not in keys, f"用户版侧栏/命令面板不应出现 {cid}"
    for p in _partner_paths():
        assert p not in paths, f"用户版不应出现代理商深链 {p}"
    # 系统设置本体（同 key=settings）不得被连坐
    assert "/settings" in paths
    # 「支持」组只剩个人设置，不得留空组；「AI 与知识」组仍有人设等项
    labels = {g.get("label_key") for g in ctx["nav_groups"]}
    assert "section_support" in labels and "section_ai_kb" in labels
    assert "/personal-settings" in paths and "/personas" in paths


def test_client_flavor_hides_strategies_and_help_in_simple_lists_too():
    ctx = get_nav_context(CLIENT)
    simple = {it.get("key") for it in ctx["nav_simple_core"] + ctx["nav_simple_more"]
              if isinstance(it, dict)}
    assert "help" not in simple
    # 学习队列保留在「更多」（D-L5：止血而不隐藏）
    assert "learner" in simple


@pytest.mark.parametrize("cfg", [PARTNER, INTERNAL])
def test_partner_and_internal_keep_everything(monkeypatch, cfg):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    ctx = get_nav_context(cfg)
    keys, paths = _keys(ctx), _paths(ctx)
    for cid in CLIENT_HIDDEN_ITEM_IDS:
        assert cid in keys, f"{cfg['ui_visibility']['flavor']} 形态必须保留 {cid}"
    for p in _partner_paths():
        assert p in paths, f"{cfg['ui_visibility']['flavor']} 形态必须保留代理商深链 {p}"
    # 没有开发者模式注解（本就全显，角标是噪音）
    assert not any(it.get("tier") for it in _all_items(ctx))


# ── 开发者模式 ───────────────────────────────────────────────────────────────

def test_developer_mode_reveals_hidden_items_with_tier_badges():
    ctx = get_nav_context(CLIENT, developer_mode=True)
    items = _all_items(ctx)
    by_key = {it.get("key"): it for it in items if it.get("key") not in ("settings", "")}
    for cid in CLIENT_HIDDEN_ITEM_IDS:
        assert cid in by_key, f"开发者模式下 {cid} 应回归"
        assert by_key[cid].get("tier") == "internal", f"{cid} 应带「研发」角标"
    by_path = {it.get("path"): it for it in items}
    for p in _partner_paths():
        assert p in by_path, f"开发者模式下代理商深链 {p} 应回归"
        assert by_path[p].get("tier") == "partner", f"{p} 应带「代理商」角标"
    # 未被藏的项不打注解；系统设置本体（同 key）不受深链注解连坐
    assert by_key["cases"].get("tier") is None
    assert by_path["/settings"].get("tier") is None


def test_developer_mode_does_not_touch_other_visibility_keys():
    """开发者模式只放回形态隐藏项：matrix_nav / group_show / 客户营收等布尔键照旧。"""
    ctx = get_nav_context(CLIENT, developer_mode=True)
    keys, paths = _keys(ctx), _paths(ctx)
    assert "rpa_overview" not in keys and "group_show" not in keys
    assert "/monetization" not in paths
    assert NAV_ITEMS["escalation"]["path"] not in paths


def test_developer_mode_ignored_on_partner_and_internal(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    for cfg in (PARTNER, INTERNAL):
        assert get_nav_context(cfg, developer_mode=True) == get_nav_context(cfg)


def test_nav_singleton_not_mutated_by_tier_annotation():
    get_nav_context(CLIENT, developer_mode=True)
    assert "tier" not in NAV_ITEMS["help"]
    assert "tier" not in CMD_EXTRA_ITEMS["settings_brand"]
    assert not any(isinstance(it, dict) and it.get("tier")
                   for it in get_nav_context(None)["nav_cmd_items"])


# ── 判定口径（模板全局 ui_client_hide 与导航同源） ────────────────────────────

def test_client_hide_active_follows_flavor_and_developer_mode():
    assert client_hide_active(CLIENT, False) is True
    assert client_hide_active(CLIENT, True) is False
    assert client_hide_active(PARTNER, False) is False
    assert client_hide_active(INTERNAL, False) is False


def test_resolve_developer_mode_requires_dev_unlock():
    assert resolve_developer_mode({"developer_mode": True, "dev_unlocked": True}) is True
    assert resolve_developer_mode({"developer_mode": True}) is False, \
        "没过 /developer 密码闸的 session 不得开开发者模式"
    assert resolve_developer_mode({"dev_unlocked": True}) is False
    assert resolve_developer_mode(None) is False
    assert resolve_developer_mode("garbage") is False


# ── 路由：POST /api/developer/developer-mode ─────────────────────────────────

class _FakeCfgMgr:
    def __init__(self, cfg=None):
        self.config = cfg if cfg is not None else {}
        self.writes = []

    def set_overlay_flag(self, key, value):
        self.writes.append((key, value))
        return True, ""


def _mk_client(cfg_mgr):
    from src.web.routes.ui_visibility_routes import register_ui_visibility_routes

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test")

    async def auth_dep():
        return "tester"

    register_ui_visibility_routes(app, auth_dep, config_manager=cfg_mgr)

    @app.post("/_test/unlock")
    async def _unlock(request: Request):
        request.session["dev_unlocked"] = True
        return {"ok": True}

    @app.post("/_test/lock")
    async def _lock(request: Request):
        request.session.pop("dev_unlocked", None)
        return {"ok": True}

    @app.get("/_test/devmode")
    async def _devmode(request: Request):
        return {"on": resolve_developer_mode(request.session)}

    return TestClient(app)


def test_developer_mode_route_requires_dev_unlock():
    c = _mk_client(_FakeCfgMgr(dict(CLIENT)))
    r = c.post("/api/developer/developer-mode", json={"on": True})
    assert r.status_code == 403
    assert c.get("/_test/devmode").json()["on"] is False


def test_developer_mode_route_writes_session_only_and_dies_with_unlock():
    mgr = _FakeCfgMgr(dict(CLIENT))
    c = _mk_client(mgr)
    c.post("/_test/unlock")
    r = c.post("/api/developer/developer-mode", json={"on": True})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["developer_mode"] is True and d["flavor"] == "client"
    assert mgr.writes == [], "开发者模式不得落 overlay（客户机会永久变研发面）"
    assert c.get("/_test/devmode").json()["on"] is True
    # 密码闸一锁，开发者模式随之失效（退出登录同理）
    c.post("/_test/lock")
    assert c.get("/_test/devmode").json()["on"] is False
    # 关回去
    c.post("/_test/unlock")
    r2 = c.post("/api/developer/developer-mode", json={"on": False})
    assert r2.json()["developer_mode"] is False


# ── 模板接线（静态断言：模板热更新直上生产） ─────────────────────────────────

def _tpl(name):
    return (REPO / "src" / "web" / "templates" / name).read_text(encoding="utf-8")


def test_sidebars_render_tier_badge():
    base = _tpl("base.html")
    assert "{%- if it.tier %}" in base and "nav-tier-{{ it.tier }}" in base
    assert ".nav-tier-internal{" in base and ".nav-tier-partner{" in base
    wsb = _tpl("_ws_sidebar.html")
    assert "{%- if it.tier %}" in wsb and "wsb-tier-{{ it.tier }}" in wsb


def test_developer_page_has_mode_toggle_wired_to_route():
    dev = _tpl("developer.html")
    assert 'id="dv-devmode-cb"' in dev
    assert "/api/developer/developer-mode" in dev
    assert "{% if ui_developer_mode %}checked{% endif %}" in dev


def test_admin_enrich_context_exposes_flavor_flags():
    src = (REPO / "src" / "web" / "admin.py").read_text(encoding="utf-8", errors="replace")
    for k in ('"ui_flavor"', '"ui_developer_mode"', '"ui_client_hide"'):
        assert f"context.setdefault({k}" in src, f"模板全局 {k} 未注入"
    assert "developer_mode=_ui_dev_mode" in src, "导航上下文未接开发者模式"


def test_i18n_bilingual_for_new_keys():
    from src.web.i18n_packs.developer_page import EN as DEV_EN, ZH as DEV_ZH
    from src.web.i18n_packs.nav import EN, ZH

    for k in ("nav_tier_internal", "nav_tier_partner", "nav_tier_tip",
              "nav_settings_brand", "nav_settings_license", "nav_settings_demo"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"
    for k in ("dv_devmode_title", "dv_devmode_sub", "dv_devmode_cb", "dv_devmode_na",
              "dv_devmode_flavor_client", "dv_devmode_flavor_partner",
              "dv_devmode_flavor_internal"):
        assert DEV_ZH.get(k) and DEV_EN.get(k), f"{k} 缺双语"
