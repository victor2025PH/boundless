# -*- coding: utf-8 -*-
"""新手引导退役门禁（2026-08-31，接替 test_onboarding_welcome.py）。

背景：首启五步向导（WP-2 /welcome）+ 后台 4 步 Tour + 简洁模式欢迎弹窗 +
工作台 cp-tour + 坐席新手任务卡（WP-7）于 2026-08-31 整体退役——新手登录后
直接使用，任何角色任何配置下 /workspace 无条件可达。

事故背书（勿复活闸门）：cloud_light 预设曾播种 onboarding.enabled=true，
向导未完成时 /workspace 被 303 扣在 /welcome——新手从侧栏点「聊天坐席工作台」
永远进不了工作台，体感即「点了没反应」。核心页面不得被 feature flag 挡路。

本文件钉四层不复活：
1. 路由层：即使 onboarding.enabled=true，/welcome 与 /api/onboarding/* 与
   /api/agent-tasks* 一律 404（路由不再注册）；
2. 导航层：/workspace 恒 200；登录落地不再指向 /welcome；
3. 配置层：cloud_light.yaml 不再播种 onboarding 键；
4. 模板层：base.html / unified_inbox.html / unified-inbox.css 无引导残留。
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

ENGINE_ROOT = Path(__file__).resolve().parents[1]

_HDRS = {"Authorization": "Bearer test-token"}


async def _build_app(tmp_path, *, onboarding_enabled: bool):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "onboarding": {"enabled": onboarding_enabled, "agent_tasks": True},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    from src.web.admin import create_app

    return create_app(cm), cm


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_onboarding_routes_gone_even_when_flag_on(tmp_path, monkeypatch):
    """flag 开也全 404：路由已不注册，「配置考古」不可能复活向导。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        await c.post("/login", data={"auth_token": "test-token"})
        assert (await c.get("/welcome")).status_code == 404
        assert (await c.get("/api/onboarding/status", headers=_HDRS)).status_code == 404
        assert (await c.get("/api/agent-tasks", headers=_HDRS)).status_code == 404


@pytest.mark.asyncio
async def test_workspace_reachable_regardless_of_flag(tmp_path, monkeypatch):
    """核心页面不被 flag 挡路：onboarding.enabled=true 时 /workspace 也 200。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        await c.post("/login", data={"auth_token": "test-token"})
        r = await c.get("/workspace")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_login_never_lands_on_welcome(tmp_path, monkeypatch):
    """master 首登落角色默认页（/workspace/dash），不再被向导劫持。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    app.state.user_store.create_user("boss", "secret6", "master",
                                     display_name="boss")
    async with _client(app) as c:
        r = await c.post("/login", data={"username": "boss",
                                         "password": "secret6"})
        assert r.status_code == 303
        assert r.headers["location"] == "/workspace/dash"


def test_cloud_light_no_longer_seeds_onboarding():
    data = yaml.safe_load(
        (ENGINE_ROOT / "config" / "profiles" / "cloud_light.yaml").read_text(
            encoding="utf-8"))
    assert "onboarding" not in (data or {}), \
        "cloud_light 不得再播种 onboarding（新装机曾因此被扣在向导里）"


def test_admin_no_longer_registers_onboarding_routes():
    src = (ENGINE_ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert "register_welcome_routes(" not in src
    assert "register_agent_tasks_routes(" not in src


def test_base_template_has_no_tour_or_welcome_modal():
    tpl = (ENGINE_ROOT / "src" / "web" / "templates" / "base.html").read_text(
        encoding="utf-8")
    for marker in ("startTour", "endTour", "onboard-modal", "dismissOnboard",
                   "tour_done", "tour_auto", "#tour"):
        assert marker not in tpl, f"base.html 引导残留复活: {marker}"


def test_inbox_template_has_no_cp_tour():
    tpl = (ENGINE_ROOT / "src" / "web" / "templates"
           / "unified_inbox.html").read_text(encoding="utf-8")
    for marker in ("_cpTourStart", "_cpTourMaybeAuto", "_CP_TOUR_DONE_KEY",
                   "ws-cp-tour-btn"):
        assert marker not in tpl, f"unified_inbox.html cp-tour 残留复活: {marker}"
    css = (ENGINE_ROOT / "src" / "web" / "static" / "workspace"
           / "unified-inbox.css").read_text(encoding="utf-8")
    for sel in (".cp-tour-veil{", ".cp-tour-hl{", ".cp-tour-pop{"):
        assert sel not in css, f"unified-inbox.css cp-tour 样式复活: {sel}"
