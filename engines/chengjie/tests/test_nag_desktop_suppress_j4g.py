# -*- coding: utf-8 -*-
"""J-4 G（2026-09-05）：桌面包不出「告警通道未接通 / 达成推送未接通」nag。

两条 nag 都是只有内部运维（有 bot token、有 notify_webhooks 写权限）才能处置的
配置提示；桌面包（client 形态）坐席机用户与非管理角色看到它只是常驻的无能为力。
钉住的不变量：
1. ``_alertlink_connect.html`` 在 client 形态下 bannerMode=false（胶囊/卡片/5min
   轮询全不启动），internal 形态与旧行为逐字一致；弹窗本体两态都保留（ops CTA 按需开）。
2. ``admin.py`` 上下文注入 ``ui_client_flavor``，判定单源 ``ui_visibility.is_client_flavor``。
3. ``/api/goals/notify-status`` 回 ``nag_suppressed``：client 形态或 agent/viewer 角色为
   True；internal + master 为 False（旧后端缺字段＝cp-goal 旧行为）。
4. cp-goal 的 warn 分支消费 ``nag_suppressed``（返回空串＝行保持 hidden），双树镜像同步。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from jinja2 import Environment, FileSystemLoader
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
TPL = REPO / "src" / "web" / "templates"
PARTIAL = TPL / "_alertlink_connect.html"
CP_GOAL = REPO / "shared" / "copilot" / "components" / "cp-goal.js"
CP_GOAL_MIRROR = REPO / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-goal.js"


def _render_partial(**ctx) -> str:
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=False)
    base = {"user_role": "master", "i18n": {}, "alertlink_banner": True}
    base.update(ctx)
    return env.get_template("_alertlink_connect.html").render(**base)


def _banner_mode(html: str) -> str:
    m = re.search(r"var bannerMode=(true|false);", html)
    assert m, "partial 丢了 bannerMode 变量"
    return m.group(1)


def test_partial_banner_off_in_client_flavor():
    html = _render_partial(ui_client_flavor=True)
    assert _banner_mode(html) == "false"
    # 弹窗本体保留（ops 页 CTA / 显式打开仍可用）
    assert 'id="al-connect-mask"' in html


def test_partial_banner_on_in_internal_flavor_and_when_flag_missing():
    assert _banner_mode(_render_partial(ui_client_flavor=False)) == "true"
    assert _banner_mode(_render_partial()) == "true"          # 旗标缺失＝不隐藏
    # 纯弹窗模式（ops 页）不受形态影响：本来就 false
    assert _banner_mode(_render_partial(alertlink_banner=False,
                                        ui_client_flavor=True)) == "false"


def test_partial_still_admin_gated_in_client_flavor():
    html = _render_partial(user_role="agent", ui_client_flavor=True)
    assert 'id="al-connect-mask"' not in html


def test_admin_context_injects_ui_client_flavor():
    src = (REPO / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert 'context.setdefault("ui_client_flavor"' in src
    assert "is_client_flavor" in src


def _client(role: str, flavor: str):
    from src.companion.goals.store import reset_goal_store
    from src.web.routes.goal_routes import register_goal_routes
    reset_goal_store()
    cfg = {"companion": {"goals": {"enabled": True, "db_path": ":memory:"}},
           "ui_visibility": {"flavor": flavor}}
    sess = {"role": role, "username": "tester"}
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app)


def test_notify_status_nag_suppressed_matrix(monkeypatch):
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    from src.companion.goals.store import reset_goal_store
    try:
        assert _client("master", "internal").get("/api/goals/notify-status").json()["nag_suppressed"] is False
        assert _client("master", "client").get("/api/goals/notify-status").json()["nag_suppressed"] is True
        assert _client("agent", "internal").get("/api/goals/notify-status").json()["nag_suppressed"] is True
        assert _client("viewer", "internal").get("/api/goals/notify-status").json()["nag_suppressed"] is True
    finally:
        reset_goal_store()


def test_notify_status_desktop_env_counts_as_client(monkeypatch):
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    from src.companion.goals.store import reset_goal_store
    try:
        cfg_flavor_unset = _client("master", "")   # flavor 未显式配 → 随桌面模式
        assert cfg_flavor_unset.get("/api/goals/notify-status").json()["nag_suppressed"] is True
    finally:
        reset_goal_store()


def test_cp_goal_consumes_nag_suppressed_and_mirror_synced():
    src = CP_GOAL.read_text(encoding="utf-8")
    assert "nag_suppressed" in src
    start = src.find("_notifyRowInner(st) {")
    assert start > 0
    seg = src[start:src.find("_notifyRowHtml() {", start)]
    # 两个 warn 分支（scan_off / push_off）都在抑制旗标下返回空串
    assert seg.count('if (!nag) return "";') == 2, "scan_off / push_off 两分支都必须受 nag 闸"
    assert CP_GOAL_MIRROR.read_bytes() == CP_GOAL.read_bytes(), "桌面镜像未同步"
