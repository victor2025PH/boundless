# -*- coding: utf-8 -*-
"""长传记「补齐向量」路由门禁（J1 线，2026-07-28）。

背景：句向量 2026-07-28 起改为**入库期**预计算（新表 persona_bio_sents）。早于该
改动入库的传记库整表没有句向量 → 检索静默退化回纯关键词排序，不报错不告警。
``POST /api/personas/{profile_id}/bio-doc/reembed`` 是运营在 Persona Studio 里的
自愈口：提交后台任务补嵌，轮询复用既有 ``/api/personas/import-doc/jobs/{job_id}``。

覆盖：
1. 补齐成功 → 任务 result 含 chunks/updated/sents_added
2. 该人设无传记库存 → 409（明确「无库存」语义，不是 500）
3. 嵌入端点不可用（``reembed_bio_doc`` 返 None）→ 任务 error=``embed_unavailable``，
   **绝不**报成功；与「无库存」是两个可区分的码
4. feature flag 关 → 403（与既有 bio-doc 路由同口径）
5. 鉴权：未登录 401；viewer 只读角色 403

app 构造沿用 tests/test_persona_doc_import.py / test_persona_bio_store.py 的
``_build_app`` + ``create_app`` + httpx AsyncClient 口径（viewer 角色一例走
tests/test_goal_routes.py 的可注入 session 轻量 app，create_app 的签名 cookie
不便伪造）。
"""

import asyncio
import sys
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.companion import persona_bio_store as pbs  # noqa: E402
from src.utils import persona_doc_import as pdi  # noqa: E402
from src.web.routes.persona_import_routes import (  # noqa: E402
    register_persona_import_routes,
)

_PID = "mizuki_sato"
_BIO = "\n\n".join([
    "美月1994年出生在北海道札幌，父亲是水产加工厂的技师，母亲在市立图书馆做管理员。",
    "2013年考入都灵大学读设计，第一年住在Porta Nuova附近的合租公寓，室友是个学建筑的希腊人。",
    "2019年和前夫Marco在都灵结婚，两年后和平分手，Marco现在在米兰做家具代理。",
    "现在在一家小型家居品牌做视觉，平时接一点独立插画的活儿补贴房租。",
])
_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}
_REEMBED = f"/api/personas/{_PID}/bio-doc/reembed"


@pytest.fixture(autouse=True)
def _bio_db(tmp_path):
    """模块级单例指向 tmp 库；钉死 embed_fn=None（禁懒取真嵌入端点，保离线）。"""
    pbs.reset_persona_bio_store()
    pbs.set_embed_fn(None)
    pbs.configure_persona_bio_store(str(tmp_path / "bio_reembed.db"), embed_fn=None)
    yield
    pbs.reset_persona_bio_store()


@pytest.fixture(autouse=True)
def _clean_jobs():
    pdi.reset_jobs()
    yield
    pdi.reset_jobs()


def _build_app(tmp_path, doc_import_enabled):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {"profiles": [],
                     "doc_import": {"enabled": bool(doc_import_enabled)}},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(cm.load())
    finally:
        loop.close()

    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture
def app_off(tmp_path):
    yield _build_app(tmp_path, False)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


@pytest.fixture
def app_on(tmp_path):
    yield _build_app(tmp_path, True)
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _await_job(c, job_id, tries=200):
    """轮询到终态（复用抽取线的 jobs 端点——同一注册表）。"""
    for _ in range(tries):
        r = await c.get(f"/api/personas/import-doc/jobs/{job_id}", headers=_HDRS)
        assert r.status_code == 200
        job = r.json()["job"]
        if job["status"] in ("done", "error"):
            return job
        await asyncio.sleep(0.02)
    raise AssertionError("补向量任务未在预期时间内到达终态")


# ── 1. 补齐成功 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reembed_success_reports_chunks_and_sents(app_on, monkeypatch):
    calls = []

    def _fake_reembed(pid):
        calls.append(pid)
        return {"chunks": 9, "updated": 4, "sents_added": 37}

    monkeypatch.setattr(pbs, "reembed_bio_doc", _fake_reembed)
    async with _client(app_on) as c:
        await c.post(f"/api/personas/{_PID}/bio-doc",
                     headers=_JSON_HDRS, json={"text": _BIO})
        r = await c.post(_REEMBED, headers=_HDRS)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["job_id"]
        assert body["chunks"] > 0          # 提交时即回库存单元数，前端可显示规模

        job = await _await_job(c, body["job_id"])
        assert job["status"] == "done" and job["progress"] == 100
        assert job["result"] == {
            "profile_id": _PID, "chunks": 9, "updated": 4, "sents_added": 37,
        }
    assert calls == [_PID]


@pytest.mark.asyncio
async def test_reembed_records_funnel_event(app_on, monkeypatch):
    """补向量要可观测。``record`` 对未登记事件名是**静默忽略**的，所以这里既钉
    「路由确实记了一发」，也钉「事件名在 _EVENTS 白名单里」——少任何一半都白记。"""
    from src.utils import persona_import_stats as pis

    monkeypatch.setattr(
        pbs, "reembed_bio_doc",
        lambda pid: {"chunks": 9, "updated": 4, "sents_added": 37})
    assert "bio_reembed" in pis._EVENTS
    before = pis.snapshot()["counts"]["bio_reembed"]
    async with _client(app_on) as c:
        await c.post(f"/api/personas/{_PID}/bio-doc",
                     headers=_JSON_HDRS, json={"text": _BIO})
        r = await c.post(_REEMBED, headers=_HDRS)
        assert r.status_code == 200
    assert pis.snapshot()["counts"]["bio_reembed"] == before + 1


@pytest.mark.asyncio
async def test_reembed_nothing_to_do_is_still_success(app_on, monkeypatch):
    """全都有向量（updated=0/sents_added=0）不是失败——前端据此显示「无需补齐」。"""
    monkeypatch.setattr(
        pbs, "reembed_bio_doc",
        lambda pid: {"chunks": 9, "updated": 0, "sents_added": 0})
    async with _client(app_on) as c:
        await c.post(f"/api/personas/{_PID}/bio-doc",
                     headers=_JSON_HDRS, json={"text": _BIO})
        r = await c.post(_REEMBED, headers=_HDRS)
        job = await _await_job(c, r.json()["job_id"])
        assert job["status"] == "done"
        assert job["result"]["updated"] == 0 and job["result"]["sents_added"] == 0


# ── 2. 无库存 vs 3. 嵌入端点不可用：两种「补不了」必须可区分 ─────────────────

@pytest.mark.asyncio
async def test_reembed_no_stock_is_409_not_500(app_on):
    async with _client(app_on) as c:
        r = await c.post("/api/personas/nobody/bio-doc/reembed", headers=_HDRS)
        assert r.status_code == 409
        assert r.json()["detail"]          # 走 tr()，非空且不是堆栈


@pytest.mark.asyncio
async def test_reembed_no_stock_never_submits_a_job(app_on, monkeypatch):
    """无库存在提交前判死：不该白占一个后台任务槽。"""
    submitted = []
    monkeypatch.setattr(pdi, "create_job_runner",
                        lambda runner: submitted.append(runner) or "j1")
    async with _client(app_on) as c:
        r = await c.post("/api/personas/nobody/bio-doc/reembed", headers=_HDRS)
    assert r.status_code == 409 and submitted == []


@pytest.mark.asyncio
async def test_reembed_embed_endpoint_down_fails_honestly(app_on, monkeypatch):
    """嵌入端点不可用（reembed 返 None，库存还在）→ error=embed_unavailable，不装成功。"""
    monkeypatch.setattr(pbs, "reembed_bio_doc", lambda pid: None)
    async with _client(app_on) as c:
        await c.post(f"/api/personas/{_PID}/bio-doc",
                     headers=_JSON_HDRS, json={"text": _BIO})
        r = await c.post(_REEMBED, headers=_HDRS)
        assert r.status_code == 200        # 提交本身成功（库存在）
        job = await _await_job(c, r.json()["job_id"])
        assert job["status"] == "error"
        assert job["error"] == "embed_unavailable"
        assert not job["result"]


@pytest.mark.asyncio
async def test_reembed_stock_vanished_midflight_says_bio_missing(app_on, monkeypatch):
    """提交后库存被删的竞态 → bio_missing，与端点挂了是两个码（前端两套文案）。"""
    monkeypatch.setattr(pbs, "reembed_bio_doc", lambda pid: None)
    monkeypatch.setattr(pbs, "get_bio_meta", _meta_then_gone())
    async with _client(app_on) as c:
        r = await c.post(_REEMBED, headers=_HDRS)
        assert r.status_code == 200
        job = await _await_job(c, r.json()["job_id"])
        assert job["status"] == "error" and job["error"] == "bio_missing"


def _meta_then_gone():
    """首次（路由预检）报有库存，之后（runner 复核）报没了。"""
    state = {"n": 0}

    def _get_bio_meta(pid):
        state["n"] += 1
        return {"chunks": 9, "chars": 400} if state["n"] == 1 else None

    return _get_bio_meta


# ── 4. feature flag ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reembed_flag_off_403_like_other_bio_routes(app_off):
    async with _client(app_off) as c:
        r = await c.post(_REEMBED, headers=_HDRS)
        assert r.status_code == 403
        # 与既有 bio-doc 路由完全同口径
        assert (await c.get(f"/api/personas/{_PID}/bio-doc",
                            headers=_HDRS)).status_code == 403
        assert (await c.delete(f"/api/personas/{_PID}/bio-doc",
                               headers=_HDRS)).status_code == 403


# ── 5. 满载退让 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reembed_jobs_busy_429(app_on, monkeypatch):
    monkeypatch.setattr(pdi, "create_job_runner", lambda runner: None)
    async with _client(app_on) as c:
        await c.post(f"/api/personas/{_PID}/bio-doc",
                     headers=_JSON_HDRS, json={"text": _BIO})
        r = await c.post(_REEMBED, headers=_HDRS)
        assert r.status_code == 429


# ── 6. 鉴权 ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reembed_unauthenticated_rejected_like_sibling_bio_route(app_on):
    """无凭证一律拒（实际由 CSRF 中间件先于 auth 拦下无 header 的 POST，403）——
    断言与既有 ``POST /bio-doc`` **同码**，防日后本路由被漏挂在鉴权链之外。"""
    async with _client(app_on) as c:
        r = await c.post(_REEMBED)
        sibling = await c.post(f"/api/personas/{_PID}/bio-doc", json={"text": _BIO})
        assert r.status_code in (401, 403)
        assert r.status_code == sibling.status_code
        # 未鉴权绝不落地成任务
        assert "job_id" not in (r.json() if r.headers.get(
            "content-type", "").startswith("application/json") else {})


def _role_app(role, enabled=True):
    """可注入 session 的轻量 app（仿 tests/test_goal_routes.py），只为验角色闸门。"""
    app = FastAPI()
    app.state.config_manager = type(
        "_CM", (), {"config": {"personas": {"doc_import": {"enabled": enabled}}}})()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = {"role": role, "user": "tester"}

    register_persona_import_routes(app, auth_dep)
    return app


@pytest.mark.asyncio
async def test_reembed_viewer_role_403():
    pbs.replace_bio_doc(_PID, _BIO)
    async with _client(_role_app("viewer")) as c:
        assert (await c.post(_REEMBED)).status_code == 403
    async with _client(_role_app("master")) as c:
        assert (await c.post(_REEMBED)).status_code == 200
