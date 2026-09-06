# -*- coding: utf-8 -*-
"""内部功能界面显隐（ui_visibility，2026-08-14）门禁。

覆盖：
- 单源判定 resolve_ui_visibility：缺省全隐藏 / 解析 / 坏输入回落；
- 导航过滤：matrix_nav 关 → 真机矩阵五项从 nav_groups / nav_cmd_items /
  nav_matrix_items 全面剔除，开 → 原样保留（其余导航项不受影响）；
  group_show 关（2026-08-16 第五键）→ 群脉导播台同面剔除，两键全关时
  「真机矩阵」组整组消失（组标题不得带单项残留——上线实录就是这个残留）；
  ai_settings 关（2026-08-16 第六键）→ 简洁模式「人工转接」项（深链
  /settings#escalation）剔除，而完整模式「系统设置」项必须留下——两者共用
  key="settings"，按 key 剔会连坐；
- 路由：GET /api/desktop/ui-flags 免鉴权；GET/POST /api/developer/ui-visibility
  须 dev 解锁；POST 写 overlay 键名契约 + 未知键 404 + 写失败 500。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.web.ui_visibility import DEFAULTS, UI_VISIBILITY_KEYS, resolve_ui_visibility
from src.web.routes.ui_visibility_routes import register_ui_visibility_routes


# ── 单源判定 ─────────────────────────────────────────────────────────

def test_defaults_all_hidden():
    # 产品契约：内部界面缺省对坐席隐藏
    assert set(DEFAULTS) == {k for k, _ in UI_VISIBILITY_KEYS}
    assert "group_show" in DEFAULTS, "群脉导播台显隐键丢失"
    assert "ai_settings" in DEFAULTS, "AI 与转接设置显隐键丢失"
    assert all(v is False for v in DEFAULTS.values())


def test_resolve_none_and_garbage_fall_back_to_defaults():
    assert resolve_ui_visibility(None) == DEFAULTS
    assert resolve_ui_visibility("not a dict") == DEFAULTS
    assert resolve_ui_visibility({"ui_visibility": "oops"}) == DEFAULTS
    assert resolve_ui_visibility({"ui_visibility": None}) == DEFAULTS


def test_resolve_parses_section_and_coerces_bool():
    cfg = {"ui_visibility": {"manual_console": True, "matrix_nav": 1,
                             "group_extract": 0, "unknown_key": True}}
    out = resolve_ui_visibility(cfg)
    assert out["manual_console"] is True
    assert out["matrix_nav"] is True
    assert out["group_extract"] is False
    assert out["team_collab"] is False        # 未提及 → 缺省
    assert out["group_show"] is False         # 未提及 → 缺省
    assert "unknown_key" not in out           # 未知键不透传


# ── 导航过滤（matrix_nav）─────────────────────────────────────────────

def _nav_keys(ctx):
    keys = set()
    for g in ctx["nav_groups"]:
        for it in g["items"]:
            if isinstance(it, dict):
                keys.add(it.get("key"))
    return keys


def test_nav_matrix_hidden_by_default():
    from src.web.nav_schema import get_nav_context, MATRIX_ITEM_IDS

    ctx = get_nav_context({})   # 无 ui_visibility 段 = 缺省全隐藏
    keys = _nav_keys(ctx)
    for mid in MATRIX_ITEM_IDS:
        assert mid not in keys, f"matrix 项 {mid} 应从侧栏剔除"
    assert ctx["nav_matrix_items"] == []
    cmd_keys = {it.get("key") for it in ctx["nav_cmd_items"] if isinstance(it, dict)}
    assert not (cmd_keys & set(MATRIX_ITEM_IDS)), "命令面板不应残留矩阵项"
    # 其余导航仍在（抽查非矩阵项）
    assert "cases" in keys


def test_nav_matrix_visible_when_enabled():
    from src.web.nav_schema import get_nav_context, MATRIX_ITEM_IDS

    ctx = get_nav_context({"ui_visibility": {"matrix_nav": True}})
    keys = _nav_keys(ctx)
    present = keys & set(MATRIX_ITEM_IDS)
    assert present, "开启 matrix_nav 后矩阵项应回归侧栏"
    assert ctx["nav_matrix_items"], "简洁模式矩阵组应非空"


def test_nav_group_show_hidden_by_default():
    from src.web.nav_schema import get_nav_context

    ctx = get_nav_context({})   # 缺省 = 隐藏
    keys = _nav_keys(ctx)
    assert "group_show" not in keys, "群脉导播台应从侧栏剔除"
    cmd_keys = {it.get("key") for it in ctx["nav_cmd_items"] if isinstance(it, dict)}
    assert "group_show" not in cmd_keys, "命令面板不应残留导播台项"
    # matrix_nav 与 group_show 全关 → 「真机矩阵」组整组消失（此前导播台留守
    # 组内，组标题带着单项一直显示——本键的由来）
    labels = {g.get("label_key") for g in ctx["nav_groups"]}
    assert "section_channels" not in labels, "真机矩阵组应整组消失，不得剩组标题"


def test_nav_group_show_visible_when_enabled():
    from src.web.nav_schema import get_nav_context, MATRIX_ITEM_IDS

    ctx = get_nav_context({"ui_visibility": {"group_show": True}})
    keys = _nav_keys(ctx)
    assert "group_show" in keys, "开启 group_show 后导播台应回归侧栏"
    # 两键独立：matrix_nav 仍关，矩阵五项不得搭车回流
    assert not (keys & set(MATRIX_ITEM_IDS)), "矩阵五项不应随 group_show 回流"
    labels = {g.get("label_key") for g in ctx["nav_groups"]}
    assert "section_channels" in labels, "导播台回归后真机矩阵组应重新出现"


# ── 导航过滤（ai_settings：简洁模式「人工转接」项）─────────────────────

def _paths(items):
    return {it.get("path") for it in items if isinstance(it, dict)}


def test_nav_escalation_hidden_by_default():
    from src.web.nav_schema import get_nav_context, NAV_ITEMS

    esc_path = NAV_ITEMS["escalation"]["path"]
    ctx = get_nav_context({})   # 缺省 = 隐藏
    assert esc_path not in _paths(ctx["nav_simple_core"]), \
        "「人工转接」项应从简洁主区剔除（卡片藏了入口还在＝点进去空页）"
    cmd_paths = _paths(ctx["nav_cmd_items"])
    assert esc_path not in cmd_paths, "命令面板不应残留人工转接项"
    # 简洁主区其余项不受影响
    assert "/workspace" in _paths(ctx["nav_simple_core"])


def test_nav_settings_page_survives_escalation_hiding():
    """escalation 与「系统设置」共用 key="settings"，只能按 path 剔除。

    按 key 剔会把完整模式的系统设置项一起干掉（用户从此无法进 /settings，
    连开发者工具入口都要绕路）——这条钉住那个连坐。
    """
    from src.web.nav_schema import get_nav_context

    ctx = get_nav_context({})
    group_paths = set()
    for g in ctx["nav_groups"]:
        group_paths |= _paths(g["items"])
    assert "/settings" in group_paths, "完整模式「系统设置」项被连坐剔除"
    assert "/settings" in _paths(ctx["nav_cmd_items"]), "命令面板系统设置项被连坐"


def test_nav_escalation_visible_when_enabled():
    from src.web.nav_schema import get_nav_context, NAV_ITEMS

    esc_path = NAV_ITEMS["escalation"]["path"]
    ctx = get_nav_context({"ui_visibility": {"ai_settings": True}})
    assert esc_path in _paths(ctx["nav_simple_core"]), \
        "开启 ai_settings 后「人工转接」应回归简洁主区"
    assert esc_path in _paths(ctx["nav_cmd_items"])


# ── 设置页模板：两张卡的显隐接线（静态断言，模板热更新直上生产）───────

def _settings_tpl():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
            / "settings.html").read_text(encoding="utf-8")


def test_settings_cards_wired_to_ai_settings_flag():
    tpl = _settings_tpl()
    # 缺键（旧后端 / 模板热更先于重启）同样按藏处理——藏就是本批目的，热更即生效；
    # 那段窗口的逃生门是下面断言的深链揭示，不是「先照旧显示」。
    assert "{% set _uiv_ai = (ui_vis or {}).get('ai_settings') %}" in tpl, \
        "ai_settings 取值口径变了（缺键必须落 falsy＝藏）"
    # 两张卡各挂一次条件 class（AI 提示词卡 + 人工转接卡）
    assert tpl.count("{% if not _uiv_ai %} set-uiv-hide{% endif %}") >= 2
    assert ".set-uiv-hide{display:none!important}" in tpl
    # 保 DOM 只加 class：卡内 JS 按 id 取元素，删 DOM 会炸
    assert 'id="body-prompt"' in tpl and 'id="body-he"' in tpl
    # 深链就地揭示（藏而不废：/settings#escalation 仍要真能用）
    assert "closest('.set-uiv-hide')" in tpl


def test_settings_empty_state_present_for_simple_mode():
    tpl = _settings_tpl()
    assert "set_uiv_empty_t" in tpl, "简洁模式空态缺失（整页空白＝坐席以为坏了）"
    assert "{% if not _uiv_ai and ui_mode == 'simple' %}" in tpl


def test_settings_empty_state_i18n_bilingual():
    from src.web.i18n_packs.settings_page import EN, ZH
    from src.web.i18n_packs.developer_page import EN as DEV_EN, ZH as DEV_ZH

    for k in ("set_uiv_empty_t", "set_uiv_empty_d", "set_uiv_empty_d2"):
        assert ZH.get(k) and EN.get(k), f"{k} 缺双语"
    for k in ("dv_uiv_k_ai_settings", "dv_uiv_k_ai_settings_d"):
        assert DEV_ZH.get(k) and DEV_EN.get(k), f"{k} 缺双语"


def test_developer_page_renders_ai_settings_toggle():
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
           / "developer.html").read_text(encoding="utf-8")
    assert "'team_collab', 'ai_settings'" in tpl, \
        "开发者页显隐开关清单未收录 ai_settings（藏了却无处可开）"


# ── 部署形态（flavor：客户包剔运维页）─────────────────────────────────

def test_flavor_defaults_to_internal_on_server(monkeypatch):
    from src.web.ui_visibility import (FLAVOR_CLIENT, FLAVOR_INTERNAL,
                                       is_client_flavor, resolve_ui_flavor)

    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    # 服务器部署（无桌面信号）零变化——回落方向刻意与布尔键相反：
    # 读不到配置时宁可多显示一个入口，也不让内部机器突然缺入口。
    assert resolve_ui_flavor({}) == FLAVOR_INTERNAL
    assert resolve_ui_flavor(None) == FLAVOR_INTERNAL
    assert resolve_ui_flavor("not a dict") == FLAVOR_INTERNAL
    assert is_client_flavor({}) is False
    # 桌面信号（env 或 app.desktop_mode）→ 客户形态
    assert resolve_ui_flavor({"app": {"desktop_mode": True}}) == FLAVOR_CLIENT
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert resolve_ui_flavor({}) == FLAVOR_CLIENT


def test_flavor_explicit_config_overrides_desktop_detection(monkeypatch):
    from src.web.ui_visibility import (FLAVOR_CLIENT, FLAVOR_INTERNAL,
                                       resolve_ui_flavor)

    # 桌面包给内部人员用（117 上的 dogfood 壳）→ 写死 internal 保住运维页
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert resolve_ui_flavor(
        {"ui_visibility": {"flavor": "internal"}}) == FLAVOR_INTERNAL
    # 反向：服务器部署也能显式声明客户形态
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    assert resolve_ui_flavor(
        {"ui_visibility": {"flavor": "CLIENT"}}) == FLAVOR_CLIENT
    # 乱值不认，回落自动判定
    assert resolve_ui_flavor(
        {"ui_visibility": {"flavor": "oops"}}) == FLAVOR_INTERNAL


def test_flavor_key_not_in_boolean_family():
    """flavor 是字符串维度，不得混进布尔键家族。

    混进去就会被 resolve_ui_visibility 强转 bool 塞进 /api/desktop/ui-flags，
    桌面壳按 flags 判显隐会把 'internal' 当 True 用。
    """
    from src.web.ui_visibility import FLAVOR_KEY, UI_VISIBILITY_KEYS

    assert FLAVOR_KEY not in {k for k, _ in UI_VISIBILITY_KEYS}
    out = resolve_ui_visibility({"ui_visibility": {"flavor": "client"}})
    assert "flavor" not in out
    assert out == DEFAULTS


def test_nav_ops_pages_hidden_for_client_flavor():
    from src.web.nav_schema import CLIENT_HIDDEN_ITEM_IDS, get_nav_context

    cfg = {"ui_visibility": {"flavor": "client"}}
    ctx = get_nav_context(cfg)
    keys = _nav_keys(ctx)
    for cid in CLIENT_HIDDEN_ITEM_IDS:
        assert cid not in keys, f"客户形态侧栏不应出现运维项 {cid}"
    cmd_keys = {it.get("key") for it in ctx["nav_cmd_items"] if isinstance(it, dict)}
    assert not (cmd_keys & set(CLIENT_HIDDEN_ITEM_IDS)), \
        "命令面板同样是发现面，客户形态不应残留运维项"
    # 「系统管理」组不得整组消失（用户管理 / 系统设置仍在）
    assert "/settings" in {it.get("path")
                           for g in ctx["nav_groups"] for it in g["items"]
                           if isinstance(it, dict)}


def test_nav_ops_pages_survive_internal_flavor(monkeypatch):
    from src.web.nav_schema import CLIENT_HIDDEN_ITEM_IDS, get_nav_context

    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    keys = _nav_keys(get_nav_context({}))
    for cid in CLIENT_HIDDEN_ITEM_IDS:
        assert cid in keys, f"内部部署必须原样保留 {cid}（117 双实例天天用）"


def test_nav_ops_overview_stays_visible_for_client():
    """运营总览刻意不随形态隐藏——那是老板每天看的经营读数面。"""
    from src.web.nav_schema import get_nav_context

    ctx = get_nav_context({"ui_visibility": {"flavor": "client"}})
    assert "ops" in _nav_keys(ctx)


def test_nav_no_empty_groups_after_filter():
    from src.web.nav_schema import get_nav_context, DOMAIN_SENTINEL

    ctx = get_nav_context({})
    for g in ctx["nav_groups"]:
        real = [i for i in g["items"] if i != DOMAIN_SENTINEL]
        assert real, f"过滤后出现空组：{g.get('key') or g.get('label')}"


def test_nav_monetization_follows_product_switch_not_ui_visibility():
    """客户营收跟 monetization.enabled，不另造 ui_visibility 键。"""
    from src.web.nav_schema import get_nav_context
    from src.web.ui_visibility import UI_VISIBILITY_KEYS

    assert "monetization" not in UI_VISIBILITY_KEYS

    def _has_mo(ctx):
        for g in ctx["nav_groups"]:
            for it in g["items"]:
                if isinstance(it, dict) and it.get("path") == "/monetization":
                    return True
        return False

    assert not _has_mo(get_nav_context({}))
    assert _has_mo(get_nav_context({"monetization": {"enabled": True}}))


# ── 形态台账（新页面必须表态：客户端要不要看见）────────────────────────
# 2026-08-20 实施49 P1-6：客户端形态藏运维页的机制建好之后，真正的风险变成
# 「以后新加的页面默认漏给客户」——B6 那类反馈（这页不该对用户开放）会重新长出来。
# 故用棘轮：任何新导航项/命令面板项必须在这张表里表态一次，两种表态法二选一——
#   ① 客户也该看见 → 把 id 加进本表；
#   ② 纯运维/开发面 → 加进 nav_schema.CLIENT_HIDDEN_ITEM_IDS（同时也加进本表）。
# 表本身不判对错，它只保证「有人想过这个问题」，且 review 时一眼能看出新页归属。
_AUDIENCE_DECLARED = frozenset({
    "analytics", "asset_center", "audit", "bug_tickets", "care", "cases",
    "crisis_audit", "dash",
    "developer", "diff", "episodic", "escalation", "funnel", "group_show", "help",
    "import", "knowledge", "learner", "line_rpa", "logs", "membership",
    "messenger_rpa", "monetization", "ops", "personal_settings", "personas",
    "relations_health", "reply_settings", "rpa_overview", "settings",
    # 代理商面深链（L-4 A 2026-09-06）：PARTNER_ONLY_ITEM_IDS，client 形态剔除
    "settings_brand", "settings_demo", "settings_license",
    # singing / voice_eval / help / strategies：2026-09-06 L-4 A（D-L6）改判客户
    # 形态隐藏（空壳 / 研发调参面），已加进 nav_schema.CLIENT_HIDDEN_ITEM_IDS。
    "singing", "strategies",
    "strategy_analytics", "telegram", "templates", "usage_center", "users",
    "voice_eval", "whatsapp_rpa", "work_goal", "workspace", "ws_aiq", "ws_boards",
    "ws_perf", "ws_queue", "ws_roi",
})


def test_new_nav_items_declare_client_audience():
    from src.web.nav_schema import CLIENT_HIDDEN_ITEM_IDS, CMD_EXTRA_ITEMS, NAV_ITEMS

    known = set(NAV_ITEMS) | set(CMD_EXTRA_ITEMS)
    undeclared = sorted(known - _AUDIENCE_DECLARED)
    assert not undeclared, (
        f"新页面未表态：{undeclared}。客户端形态（桌面包/最终用户）要不要看见这些页？\n"
        f"  · 该看见 → 把 id 加进 tests/test_ui_visibility.py::_AUDIENCE_DECLARED；\n"
        f"  · 纯运维/开发面 → 同时加进 nav_schema.CLIENT_HIDDEN_ITEM_IDS。")
    stale = sorted(_AUDIENCE_DECLARED - known)
    assert not stale, f"台账过期（页面已删）：{stale}"
    assert set(CLIENT_HIDDEN_ITEM_IDS) <= known, "CLIENT_HIDDEN_ITEM_IDS 指向了不存在的导航项"


# ── 坐席规模（B13：单人部署藏认领/释放）──────────────────────────────
def _users(*roles, enabled=True):
    return [{"username": f"u{i}", "role": r, "enabled": 1 if enabled else 0}
            for i, r in enumerate(roles)]


def test_seat_mode_single_when_only_one_workspace_account():
    from src.web.ui_visibility import is_multi_seat, resolve_seat_mode

    assert resolve_seat_mode({}, _users("master")) == "single"
    assert is_multi_seat({}, _users("master")) is False


def test_seat_mode_multi_with_two_workspace_accounts():
    from src.web.ui_visibility import is_multi_seat

    assert is_multi_seat({}, _users("master", "agent")) is True


def test_seat_mode_viewer_and_disabled_are_not_seats():
    """viewer 只读不接管、停用账号不是坐席——两者都不该把单人部署算成团队。"""
    from src.web.ui_visibility import count_seat_accounts, is_multi_seat

    assert is_multi_seat({}, _users("master") + _users("viewer")) is False
    disabled = [{"username": "x", "role": "agent", "enabled": 0}]
    assert is_multi_seat({}, _users("master") + disabled) is False
    assert count_seat_accounts(_users("master", "admin", "supervisor", "agent")) == 4


def test_seat_mode_fails_open_to_multi():
    """取不到用户表/坏数据一律 multi：藏错方向＝真团队退回「两人回同一个客户」。"""
    from src.web.ui_visibility import is_multi_seat, resolve_seat_mode

    assert is_multi_seat({}, None) is True          # 用户表不可用
    assert is_multi_seat(None, None) is True
    assert resolve_seat_mode({}, ["not-a-dict"]) == "single"  # 坏行跳过，仍如实判定
    assert is_multi_seat({}, [{"role": "agent"}, {"role": "agent"}]) is True  # 缺 enabled 列按启用


def test_seat_mode_explicit_config_overrides_account_count():
    from src.web.ui_visibility import is_multi_seat

    many = _users("master", "agent", "agent")
    assert is_multi_seat({"ui_visibility": {"seat_mode": "single"}}, many) is False
    assert is_multi_seat({"ui_visibility": {"seat_mode": "multi"}}, _users("master")) is True
    # 无法识别的值＝按数据判（不因写错一个字符把协作功能藏了）
    assert is_multi_seat({"ui_visibility": {"seat_mode": "solo"}}, many) is True


def test_seat_mode_roles_track_workspace_page_permission():
    """坐席角色集必须跟 web_user_store 的 workspace 页权限同源，别各写一份。"""
    from src.utils.web_user_store import PAGE_PERMISSIONS
    from src.web.ui_visibility import _seat_roles

    assert _seat_roles() == frozenset(PAGE_PERMISSIONS["workspace"])


def test_seat_mode_key_not_in_boolean_family():
    from src.web.ui_visibility import UI_VISIBILITY_KEYS, resolve_ui_visibility

    assert "seat_mode" not in dict(UI_VISIBILITY_KEYS)
    flags = resolve_ui_visibility({"ui_visibility": {"seat_mode": "single"}})
    assert "seat_mode" not in flags


def test_inbox_template_gates_claim_surfaces_on_seat_mode():
    """收件箱四处认领面必须挂在 ws_multi_seat / MULTI_SEAT 上（静态接线）。"""
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
            / "unified_inbox.html").read_text(encoding="utf-8")
    assert "const MULTI_SEAT =" in html
    # 三处静态 DOM：主行「我的」chip / 面板镜像项 / 会话卡认领按钮
    assert html.count("ws_multi_seat is not defined or ws_multi_seat") >= 4
    assert 'data-f="claimed"' in html and 'id="claim-hdr-btn"' in html
    # 两处轮询/写入必须早退（否则单坐席仍在写认领行 + 每 8s 打一次 claims）
    for fn in ("async function renewClaim(c){", "async function _refreshClaimsBulk(){"):
        i = html.index(fn)
        assert "if(!MULTI_SEAT) return;" in html[i:i + 260], f"{fn} 缺单坐席早退"
    # 粘性筛选降级：旧 localStorage/深链带 claimed 进来不得筛出恒空列表
    j = html.index("function setFilter(f,el){")
    assert "f==='claimed' && !MULTI_SEAT" in html[j:j + 400]


# ── 路由 ─────────────────────────────────────────────────────────────

class _FakeCfgMgr:
    def __init__(self, cfg=None, write_ok=True):
        self.config = cfg if cfg is not None else {}
        self.write_ok = write_ok
        self.writes = []

    def set_overlay_flag(self, key, value):
        self.writes.append((key, value))
        if not self.write_ok:
            return False, "disk full"
        # 模拟热合并生效
        section = self.config.setdefault("ui_visibility", {})
        section[key.split(".", 1)[1]] = value
        return True, ""


def _mk_client(cfg_mgr):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test")

    async def auth_dep():
        return "tester"

    register_ui_visibility_routes(app, auth_dep, config_manager=cfg_mgr)

    @app.post("/_test/unlock2")
    async def _unlock2(request: Request):
        request.session["dev_unlocked"] = True
        return {"ok": True}

    return TestClient(app)


def test_desktop_ui_flags_unauthenticated_defaults():
    client = _mk_client(_FakeCfgMgr({}))
    r = client.get("/api/desktop/ui-flags")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["flags"] == DEFAULTS


def test_desktop_ui_flags_reflects_config():
    client = _mk_client(_FakeCfgMgr({"ui_visibility": {"team_collab": True}}))
    d = client.get("/api/desktop/ui-flags").json()
    assert d["flags"]["team_collab"] is True
    assert d["flags"]["manual_console"] is False


def test_developer_endpoints_require_dev_unlock():
    client = _mk_client(_FakeCfgMgr({}))
    assert client.get("/api/developer/ui-visibility").status_code == 403
    r = client.post("/api/developer/ui-visibility",
                    json={"key": "team_collab", "visible": True})
    assert r.status_code == 403


def test_post_writes_overlay_and_returns_flags():
    mgr = _FakeCfgMgr({})
    client = _mk_client(mgr)
    client.post("/_test/unlock2")
    r = client.post("/api/developer/ui-visibility",
                    json={"key": "group_extract", "visible": True})
    assert r.status_code == 200
    assert mgr.writes == [("ui_visibility.group_extract", True)]
    assert r.json()["flags"]["group_extract"] is True
    # 关回去
    r2 = client.post("/api/developer/ui-visibility",
                     json={"key": "group_extract", "visible": False})
    assert r2.json()["flags"]["group_extract"] is False


def test_post_unknown_key_404():
    mgr = _FakeCfgMgr({})
    client = _mk_client(mgr)
    client.post("/_test/unlock2")
    r = client.post("/api/developer/ui-visibility",
                    json={"key": "evil_key", "visible": True})
    assert r.status_code == 404
    assert not mgr.writes


def test_post_write_failure_500():
    mgr = _FakeCfgMgr({}, write_ok=False)
    client = _mk_client(mgr)
    client.post("/_test/unlock2")
    r = client.post("/api/developer/ui-visibility",
                    json={"key": "matrix_nav", "visible": True})
    assert r.status_code == 500


def test_get_developer_flags_after_unlock():
    client = _mk_client(_FakeCfgMgr({"ui_visibility": {"matrix_nav": True}}))
    client.post("/_test/unlock2")
    d = client.get("/api/developer/ui-visibility").json()
    assert d["flags"]["matrix_nav"] is True
