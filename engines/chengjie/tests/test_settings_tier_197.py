# -*- coding: utf-8 -*-
"""系统设置口径（L-4 B，#197，D-L7，2026-09-06）门禁。

skuio 机实录（D736EJ）：用户版露「品牌/白标」「试用/演示数据」「授权/激活」三卡；
「授权：社区模式（未检测到授权文件）」与右上「旗舰版」互矛盾；功能总览满屏
「缺依赖 / 未开放」研发术语。钉住：
1. 三张代理商卡在 ui_client_hide 时整卡带 set-tier-hide（hash 深链不揭示）；
2. 徽标 / 授权卡 / /api/admin/license 同源：plan_override 显「旗舰版（厂商自营）」；
   授权卡不再有「已并入会员中心」空壳描述；
3. 演示数据只对「无真实账号的空工作区」开放（状态回 seed_allowed；seed 409）；
4. 功能总览：状态文案人话；client 形态不显依赖码/配置键。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

REPO = Path(__file__).resolve().parents[1]


def _tpl(name):
    return (REPO / "src" / "web" / "templates" / name).read_text(encoding="utf-8")


# ── 1. 三张代理商卡 ─────────────────────────────────────────────────────────

def test_partner_cards_hidden_on_client_edition():
    tpl = _tpl("settings.html")
    assert "{% set _tier_hide = ' set-tier-hide' if ui_client_hide else '' %}" in tpl
    for cid in ("card-brand", "card-license", "card-demo"):
        assert f'{{{{ _tier_hide }}}}" id="{cid}"' in tpl, f"{cid} 未挂 tier 隐藏"
    assert ".set-tier-hide{display:none!important}" in tpl
    # hash 深链只揭示 ai_settings 的 set-uiv-hide，不揭示商业结构面
    assert "closest('.set-tier-hide')" not in tpl


def test_license_card_no_hollow_membership_text_and_same_source_wording():
    tpl = _tpl("settings.html")
    assert "mb_lic_moved_hint" not in tpl, "「已并入会员中心」空壳描述应删"
    assert "mb_lic_settings_sub" not in tpl
    assert "set_lic_sub_l4" in tpl
    assert "d.plan_source" in tpl and "effective_plan" in tpl, "授权卡须读同源的 plan_source"
    assert "mb_plan_src_override" in tpl and "set_lic_no_file" in tpl


def test_settings_renders_without_tier_flag(jinja_env=None):
    """ui_client_hide 缺席（渲染类测试 / 旧后端）→ 不藏；client 形态 → 三卡带 class。"""
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(REPO / "src" / "web" / "templates")))
    # 只渲染卡片开头片段：从模板抠出 set 行 + 三个 div 起始，避免整页依赖 base.html 上下文
    src = _tpl("settings.html")
    snippet = "\n".join(l for l in src.splitlines()
                        if "_tier_hide" in l and ("{% set" in l or 'id="card-' in l))
    t = env.from_string(snippet)
    out_hide = t.render(ui_client_hide=True, ui_mode="full")
    out_show = t.render(ui_mode="full")
    assert out_hide.count("set-tier-hide") == 3
    assert "set-tier-hide" not in out_show


# ── 2. 授权口径同源 ─────────────────────────────────────────────────────────

def test_badge_label_key_marks_override_explicitly():
    from src.licensing.feature_gate import badge_label_key
    assert badge_label_key("flagship", "override") == "mb_plan_flagship_override"
    assert badge_label_key("flagship", "license") == "mb_plan_flagship"
    assert badge_label_key("community", "override") == "mb_plan_community"
    assert badge_label_key("", "") == "mb_plan_community"


def test_admin_badge_uses_shared_snapshot():
    src = (REPO / "src" / "web" / "admin.py").read_text(encoding="utf-8", errors="replace")
    assert "badge_label_key as _fg_lk" in src and "gate_snapshot as _fg_snap" in src
    assert "effective_plan as _fg_plan" not in src, "徽标不得再单独走 effective_plan 口径"


def test_license_api_exposes_plan_source(monkeypatch):
    from src.web.routes import license_routes as lr

    class _St:
        licensed = False
        state = "unlicensed"
        plan = "community"

        def to_dict(self):
            return {"state": "unlicensed", "licensed": False, "plan": "community"}

    class _Mgr:
        def status(self):
            return _St()

    import src.licensing as lic_pkg
    monkeypatch.setattr(lic_pkg, "get_license_manager", lambda: _Mgr(), raising=False)
    monkeypatch.setattr(lr, "_quota_snapshot", lambda: {}, raising=False)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    cfg_mgr = SimpleNamespace(config={"licensing": {"feature_gate": {
        "enabled": True, "plan_override": "flagship"}}})
    lr.register_license_routes(app, api_auth=lambda request: None, config_manager=cfg_mgr)
    c = TestClient(app)
    d = c.get("/api/admin/license").json()
    assert d["ok"] is True
    assert d["effective_plan"] == "flagship" and d["plan_source"] == "override"
    assert d["plan"] == "community", "授权文件口径原样保留，页面据两者措辞"


def test_membership_i18n_has_override_labels():
    from src.web.i18n_packs.membership import EN, ZH
    for p in ("basic", "pro", "flagship"):
        assert ZH.get(f"mb_plan_{p}_override") and EN.get(f"mb_plan_{p}_override")
    assert "厂商自营" in ZH["mb_plan_flagship_override"]


# ── 3. 演示数据仅空工作区 ───────────────────────────────────────────────────

class _Inbox:
    def __init__(self, total, demo):
        self._total, self._demo = total, demo

    def count_conversations_older_than(self, ts, **kw):
        return self._total

    def count_demo(self, prefix="demo:"):
        return {"conversations": self._demo, "messages": 0, "draft_audits": 0}


class _Reg:
    def __init__(self, ids):
        self._ids = ids

    def list(self, platform=None, include_removed=False):
        return [{"platform": "telegram", "account_id": i} for i in self._ids]


def test_seed_allowed_only_on_empty_workspace():
    from src.utils.demo_seeder import demo_status, real_workspace_footprint, seed_allowed
    assert seed_allowed(_Inbox(0, 0), _Reg([])) is True
    assert seed_allowed(_Inbox(6, 6), _Reg([])) is True, "只有 demo 会话＝仍是空工作区"
    assert seed_allowed(_Inbox(7, 6), _Reg([])) is False
    assert seed_allowed(_Inbox(0, 0), _Reg(["+63917"])) is False
    fp = real_workspace_footprint(_Inbox(9, 6), _Reg(["a", "b"]))
    assert fp == {"real_conversations": 3, "real_accounts": 2}
    st = demo_status(_Inbox(9, 6), account_registry=_Reg(["a"]))
    assert st["seed_allowed"] is False and st["real_conversations"] == 3
    # 读不到一律不拦（inbox 缺方法 / 无账号表）
    assert seed_allowed(SimpleNamespace(count_demo=lambda p: {"conversations": 0}), None) is True


def test_demo_seed_route_refuses_non_empty_workspace(monkeypatch):
    from src.web.routes import demo_routes as dr
    monkeypatch.setattr(dr, "_account_registry", lambda: _Reg([]))
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = _Inbox(3, 0)
    called = {"seed": 0}

    import src.utils.demo_seeder as ds
    monkeypatch.setattr(ds, "seed_demo", lambda *a, **k: called.__setitem__("seed", called["seed"] + 1) or {"ok": True})
    dr.register_demo_routes(app, api_auth=lambda request: None, config_manager=None)
    c = TestClient(app)
    r = c.post("/api/admin/demo/seed", json={})
    assert r.status_code == 409 and called["seed"] == 0
    assert "3" in r.json()["detail"]
    st = c.get("/api/admin/demo").json()
    assert st["seed_allowed"] is False and st["real_conversations"] == 3
    # 空工作区放行
    app.state.inbox_store = _Inbox(0, 0)
    assert c.post("/api/admin/demo/seed", json={}).status_code == 200 and called["seed"] == 1


def test_settings_demo_card_greys_seed_button_when_blocked():
    tpl = _tpl("settings.html")
    assert "d.seed_allowed===false" in tpl and "set_demo_blocked" in tpl


# ── 4. 功能总览人话 + client 不显研发细节 ─────────────────────────────────

def test_feature_center_wording_is_customer_facing():
    from src.web.i18n_packs.feature_center import EN, ZH
    assert ZH["fc_state_needs_dep"] == "需要配套服务" and ZH["fc_state_locked"] == "即将开放"
    assert "缺依赖" not in ZH["fc_sub"] and "未开放" not in ZH["fc_sub"]
    assert "config.local.yaml" not in ZH["fc_hint"]
    for k in ("fc_dep_client_generic", "fc_state_needs_dep", "fc_state_locked", "fc_rsn_lan"):
        assert ZH.get(k) and EN.get(k)


def _fc_client(cfg, unlock_dev=False):
    from src.web.routes.feature_center_routes import register_feature_center_routes
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    async def auth_dep():
        return True

    register_feature_center_routes(app, auth_dep, config_manager=SimpleNamespace(config=cfg))

    @app.post("/_t/dev")
    async def _dev(request: Request):
        request.session["dev_unlocked"] = True
        request.session["developer_mode"] = True
        return {"ok": True}

    c = TestClient(app)
    if unlock_dev:
        c.post("/_t/dev")
    return c


def _memvec(items):
    return next(i for i in items if i["slug"] == "memvec")


def test_feature_center_hides_dependency_keys_on_client(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    base = {"ai": {}, "memory": {"vector": {"enabled": False}}}
    client_cfg = {**base, "ui_visibility": {"flavor": "client"}}
    internal_cfg = {**base, "ui_visibility": {"flavor": "internal"}}
    d_client = _fc_client(client_cfg).get("/api/setup/features").json()
    d_internal = _fc_client(internal_cfg).get("/api/setup/features").json()
    mv_c, mv_i = _memvec(d_client["features"]), _memvec(d_internal["features"])
    assert mv_c["state"] == mv_i["state"] == "needs_dep"
    assert mv_c["state_label"] == "需要配套服务"
    assert "embedding_base_url" not in mv_c["extra"], "用户版不显配置键名"
    assert "embedding_base_url" in mv_i["extra"], "内部形态保留研发细节"
    # 开发者模式在客户机上恢复研发细节
    d_dev = _fc_client(client_cfg, unlock_dev=True).get("/api/setup/features").json()
    assert "embedding_base_url" in _memvec(d_dev["features"])["extra"]


def test_settings_i18n_bilingual_new_keys():
    from src.web.i18n_packs.settings_page import EN, ZH
    for k in ("set_lic_sub_l4", "set_lic_no_file", "set_demo_blocked",
              "err.demo.workspace_not_empty"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"
