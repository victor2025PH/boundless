# -*- coding: utf-8 -*-
"""WP-2 首启向导 /welcome 门禁（2026-08-16）。

覆盖四条不变量：
1. 状态模块纯语义：round-trip / 未知步骤拒绝 / 坏文件按全新 / welcome_pending 三态；
2. **flag 关 = 零可见变化**：页面与全部 /api/onboarding/* 一律 404（老实例零打扰）；
3. flag 开：页面渲染 / 聚合读面形状 / 进度持久化 / 自动化档位护栏写（overlay 真写）；
4. 首登直达链：建号 → 登录 → 303 落 /welcome；完成后登录恢复旧落地。

隔离：状态文件经 AITR_CONFIG_PATH 指向 tmp（data_paths.config_dir 单一事实源）；
app 用真 ConfigManager + create_app（overlay 写落 tmp config.local.yaml，零仓库写）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── 1. 状态模块纯语义 ─────────────────────────────────────────────────────

@pytest.fixture
def state_tmp(tmp_path, monkeypatch):
    """把 data_paths.config_dir() 钉到 tmp（AITR_CONFIG_PATH 优先级最高）。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    return tmp_path / "config"


def test_state_default_and_roundtrip(state_tmp):
    from src.utils.onboarding_state import (
        STATE_FILENAME, load_state, mark_step, set_completed)

    st = load_state()
    assert st == {"steps": {}, "completed": False, "updated_at": 0}

    st = mark_step("license", done=True)
    assert st["steps"]["license"]["done"] is True
    assert (state_tmp / STATE_FILENAME).exists()

    st = mark_step("channel", skipped=True, data={"note": "later"})
    assert st["steps"]["channel"]["skipped"] is True
    assert st["steps"]["channel"]["data"] == {"note": "later"}
    # license 步不被 channel 写覆盖
    assert st["steps"]["license"]["done"] is True

    st = set_completed(True)
    assert st["completed"] is True
    # 重新加载与内存一致
    from src.utils.onboarding_state import load_state as reload
    assert reload()["completed"] is True


def test_state_rejects_unknown_step(state_tmp):
    from src.utils.onboarding_state import mark_step

    with pytest.raises(ValueError):
        mark_step("not_a_step", done=True)


def test_state_corrupt_file_treated_as_fresh(state_tmp):
    from src.utils.onboarding_state import STATE_FILENAME, load_state

    state_tmp.mkdir(parents=True, exist_ok=True)
    (state_tmp / STATE_FILENAME).write_text("{oops", encoding="utf-8")
    assert load_state() == {"steps": {}, "completed": False, "updated_at": 0}


def test_state_data_size_cap(state_tmp):
    from src.utils.onboarding_state import mark_step

    st = mark_step("persona", done=True, data={"big": "x" * 10000})
    assert "data" not in st["steps"]["persona"]  # 超限静默丢弃，步骤本身照记


def test_welcome_pending_semantics(state_tmp):
    from src.utils.onboarding_state import set_completed, welcome_pending

    assert welcome_pending({}) is False                      # flag 缺省关
    assert welcome_pending({"onboarding": {"enabled": False}}) is False
    assert welcome_pending({"onboarding": {"enabled": True}}) is True
    set_completed(True)
    assert welcome_pending({"onboarding": {"enabled": True}}) is False


# ── 2/3/4. 路由级（真 ConfigManager + create_app）───────────────────────────

_HDRS = {"Authorization": "Bearer test-token"}
_JSON = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}


async def _build_app(tmp_path, *, onboarding_enabled: bool):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "onboarding": {"enabled": onboarding_enabled},
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
async def test_flag_off_everything_404(tmp_path, monkeypatch):
    """onboarding.enabled=false（含缺省）→ 页面与 API 全 404 = 零可见变化。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=False)
    async with _client(app) as c:
        assert (await c.get("/api/onboarding/status", headers=_HDRS)).status_code == 404
        r = await c.post("/api/onboarding/state", headers=_JSON,
                         json={"step": "license", "done": True})
        assert r.status_code == 404
        r = await c.post("/api/onboarding/automation-tier", headers=_JSON,
                         json={"tier": "review"})
        assert r.status_code == 404
        # 页面：先令牌登录拿 session，再访问 → 404（不是 303/200）
        await c.post("/login", data={"auth_token": "test-token"})
        assert (await c.get("/welcome")).status_code == 404


@pytest.mark.asyncio
async def test_flag_on_page_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        await c.post("/login", data={"auth_token": "test-token"})
        r = await c.get("/welcome")
        assert r.status_code == 200
        assert "onb-steps" in r.text
        assert "persona_create_wizard.js" in r.text

        r = await c.get("/api/onboarding/status", headers=_HDRS)
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] and d["enabled"]
        assert d["steps"] == ["license", "channel", "persona",
                              "automation", "test_message"]
        assert set(d["automation"]) == {"mode", "worker_enabled", "deliver_enabled"}
        assert "accounts" in d and "links" in d
        assert d["links"]["setup"] == "/workspace/setup"


@pytest.mark.asyncio
async def test_flag_on_state_roundtrip_and_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        r = await c.post("/api/onboarding/state", headers=_JSON,
                         json={"step": "license", "done": True})
        assert r.status_code == 200
        assert r.json()["state"]["steps"]["license"]["done"] is True

        r = await c.post("/api/onboarding/state", headers=_JSON,
                         json={"step": "bogus", "done": True})
        assert r.status_code == 400

        r = await c.post("/api/onboarding/state", headers=_JSON,
                         json={"completed": True})
        assert r.json()["state"]["completed"] is True
        # 状态文件真的落在 tmp（config_dir=AITR_CONFIG_PATH 父目录）
        assert (tmp_path / "welcome_onboarding.json").exists()


@pytest.mark.asyncio
async def test_automation_tier_writes_overlay(tmp_path, monkeypatch):
    """review 档：automation_mode 真写 overlay（保注释写入器）+ deliver 关方向必放行。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        r = await c.post("/api/onboarding/automation-tier", headers=_JSON,
                         json={"tier": "review"})
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True, d
        assert "inbox.auto_draft.automation_mode" in d["applied"]
        # overlay 落盘可回读
        overlay = tmp_path / "config.local.yaml"
        assert overlay.exists()
        data = yaml.safe_load(overlay.read_text(encoding="utf-8")) or {}
        assert (data.get("inbox") or {}).get("auto_draft", {}).get(
            "automation_mode") == "review"

        r = await c.post("/api/onboarding/automation-tier", headers=_JSON,
                         json={"tier": "nope"})
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_automation_tier_auto_ai_honest_blocking(tmp_path, monkeypatch):
    """auto_ai 档：要么全应用（ok），要么如实回报 blocked——绝不静默半应用装成功。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        r = await c.post("/api/onboarding/automation-tier", headers=_JSON,
                         json={"tier": "auto_ai"})
        assert r.status_code == 200
        d = r.json()
        if d["ok"]:
            assert "inbox.auto_draft.automation_mode" in d["applied"]
        else:
            assert d["blocked"], "ok=False 必须带被拦明细"
            for b in d["blocked"]:
                assert b.get("reason")


@pytest.mark.asyncio
async def test_first_login_lands_on_welcome_then_stops(tmp_path, monkeypatch):
    """首登（用户名密码）→ 303 落 /welcome；显式 next 优先；完成后恢复旧落地。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    app.state.user_store.create_user("boss", "secret6", "master",
                                     display_name="boss")
    async with _client(app) as c:
        r = await c.post("/login", data={"username": "boss",
                                         "password": "secret6"})
        assert r.status_code == 303
        assert r.headers["location"] == "/welcome"
        # 显式 next 深链仍最优先（交付串语义不被向导劫持）
        r = await c.post("/login", data={"username": "boss",
                                         "password": "secret6",
                                         "next": "/workspace/dash"})
        assert r.headers["location"] == "/workspace/dash"
        # 完成向导 → 登录恢复角色默认落地
        from src.utils.onboarding_state import set_completed
        set_completed(True)
        r = await c.post("/login", data={"username": "boss",
                                         "password": "secret6"})
        assert r.status_code == 303
        assert r.headers["location"] == "/workspace/dash"


@pytest.mark.asyncio
async def test_flag_off_login_dest_unchanged(tmp_path, monkeypatch):
    """flag 关：登录落地与向导前完全一致（零可见变化的登录侧半边）。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=False)
    app.state.user_store.create_user("boss", "secret6", "master",
                                     display_name="boss")
    async with _client(app) as c:
        r = await c.post("/login", data={"username": "boss",
                                         "password": "secret6"})
        assert r.status_code == 303
        assert r.headers["location"] == "/workspace/dash"


# ── 首启直达：/workspace 303 → /welcome（WP-5 收口批，2026-08-17）──────────

@pytest.mark.asyncio
async def test_workspace_redirects_to_welcome_when_pending(tmp_path, monkeypatch):
    """向导启用且未完成：非坐席打开 /workspace → 303 /welcome（桌面壳 token 会话
    不经 /login，此闸是「装完自动拉起→直达向导」的后端语义）；完成后恢复 200。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    async with _client(app) as c:
        await c.post("/login", data={"auth_token": "test-token"})   # master 会话
        r = await c.get("/workspace")
        assert r.status_code == 303
        assert r.headers["location"] == "/welcome"
        # 完成向导 → /workspace 恢复直达
        from src.utils.onboarding_state import set_completed
        set_completed(True)
        r = await c.get("/workspace")
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_workspace_no_redirect_for_agent_role(tmp_path, monkeypatch):
    """坐席角色豁免：agent 页面白名单只有 /workspace*，重定向它=往返死循环。"""
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=True)
    app.state.user_store.create_user("seat1", "secret6", "agent",
                                     display_name="seat1")
    async with _client(app) as c:
        r = await c.post("/login", data={"username": "seat1",
                                         "password": "secret6"})
        assert r.status_code == 303          # 登录成功 → 角色默认落地
        r = await c.get("/workspace")
        assert r.status_code == 200          # 不被重定向（向导 pending 也不）


@pytest.mark.asyncio
async def test_workspace_unchanged_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config.yaml"))
    app, _cm = await _build_app(tmp_path, onboarding_enabled=False)
    async with _client(app) as c:
        await c.post("/login", data={"auth_token": "test-token"})
        assert (await c.get("/workspace")).status_code == 200


# ── 配置面契约（cloud_light 播种 / example 基线默认关）─────────────────────

def test_example_baseline_default_off():
    data = yaml.safe_load(
        (ENGINE_ROOT / "config" / "config.example.yaml").read_text(
            encoding="utf-8"))
    assert (data.get("onboarding") or {}).get("enabled") is False


def test_cloud_light_seeds_onboarding_on():
    data = yaml.safe_load(
        (ENGINE_ROOT / "config" / "profiles" / "cloud_light.yaml").read_text(
            encoding="utf-8"))
    assert (data.get("onboarding") or {}).get("enabled") is True


# ── 模板 × i18n pack 契约（本地快反馈；全站门禁另有兜底）───────────────────

def test_template_t_keys_exist_in_pack():
    """welcome.html 里全部静态 window.T/Tf('onb_wz_*') 键必须在 pack 双语齐备。"""
    from src.web.i18n_packs.onboarding import EN, ZH

    tpl = (ENGINE_ROOT / "src" / "web" / "templates" / "welcome.html").read_text(
        encoding="utf-8")
    keys = set(re.findall(r"""(?:window\.)?Tf?\(\s*['"](onb_wz_[a-z0-9_]+)['"]""",
                          tpl))
    keys |= set(re.findall(r"""\(i18n or \{\}\)\.get\('(onb_wz_[a-z0-9_]+)'""",
                           tpl))
    assert keys, "模板里应至少引用一个 onb_wz_* 键"
    missing_zh = sorted(k for k in keys if k not in ZH)
    missing_en = sorted(k for k in keys if k not in EN)
    assert not missing_zh, f"ZH 缺键: {missing_zh}"
    assert not missing_en, f"EN 缺键: {missing_en}"
    # pack 自身 zh/en 键集一致（全局 i18n 门禁也会抓，这里本地快反馈）
    assert set(ZH) == set(EN)


def test_step_ids_match_template_and_pack():
    """STEP_IDS 与模板 data-step、pack 步骤标签键三方一致（改一处必改三处）。"""
    from src.utils.onboarding_state import STEP_IDS

    tpl = (ENGINE_ROOT / "src" / "web" / "templates" / "welcome.html").read_text(
        encoding="utf-8")
    tpl_steps = re.findall(r'data-step="([a-z_]+)"', tpl)
    assert tuple(tpl_steps) == STEP_IDS
    js_steps = re.search(r"var STEPS = \[([^\]]+)\]", tpl)
    assert js_steps
    js_list = tuple(s.strip().strip("'\"") for s in js_steps.group(1).split(","))
    assert js_list == STEP_IDS
