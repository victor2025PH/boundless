"""身份/记忆路由在「协议号实例」形态下可用（2026-07-27 生产 503 事故回归）。

事故：zhiliao 走 protocol 账号编排（telegram.phone_number 为空）→
assistant.telegram_client=None → 路由闭包只认 telegram_client.skill_manager
一条通路 → ops 卡「确认同一人」/全部 /api/identity* 硬 503
「CrossPlatformIdentity 未就绪」。修复＝resolve_skill_manager 双通路
（主客户端 → app.state.skill_manager，bootstrap P1-2 起就挂了后者）。

本门禁按 zhiliao 真实布线自建 app：ctx.telegram_client=None +
app.state.skill_manager 存在 → confirm-link / identity list / link 必须工作；
两条通路都缺 → 仍应 503（fail-closed 不装好）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.utils.cross_platform_identity import CrossPlatformIdentity
from src.utils.episodic_memory_store import EpisodicMemoryStore
from src.web.routes.episodic_identity_routes import (
    register_episodic_identity_routes,
)
from src.web.web_context import resolve_skill_manager


def _build_app(tmp_path: Path, *, with_state_sm: bool = True):
    """zhiliao 形态：telegram_client=None；SkillManager 只在 app.state。"""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("{}", encoding="utf-8")

    app = FastAPI()
    sm = None
    if with_state_sm:
        db = cfg_dir / "bot.db"
        sm = SimpleNamespace(
            _cpi=CrossPlatformIdentity(db),
            _episodic_store=EpisodicMemoryStore(db),
        )
        app.state.skill_manager = sm

    def _auth(request: Request) -> None:
        request.scope.setdefault("session", {"username": "tester"})

    def _api_write(_perm: str):
        def dep(request: Request) -> None:
            request.scope.setdefault("session", {"username": "tester"})
        return dep

    ctx = SimpleNamespace(
        telegram_client=None,
        api_auth=_auth,
        api_write=_api_write,
        audit_store=None,
        config_manager=SimpleNamespace(
            config={}, config_path=cfg_dir / "config.yaml"),
    )
    register_episodic_identity_routes(app, ctx)
    return TestClient(app), sm, cfg_dir


def test_resolve_skill_manager_two_paths():
    sm = object()
    tc = SimpleNamespace(skill_manager=sm)
    assert resolve_skill_manager(tc, None) is sm
    app = SimpleNamespace(state=SimpleNamespace(skill_manager=sm))
    assert resolve_skill_manager(None, app) is sm
    # 主客户端优先于 state
    other = object()
    assert resolve_skill_manager(tc, SimpleNamespace(
        state=SimpleNamespace(skill_manager=other))) is sm
    assert resolve_skill_manager(None, None) is None


def test_shadow_confirm_link_works_via_state_sm(tmp_path):
    client, sm, cfg_dir = _build_app(tmp_path)
    r = client.post("/api/identity/shadow/confirm-link", json={
        "platform_a": "telegram", "chat_a": "6107037825",
        "platform_b": "whatsapp", "chat_b": "639649321471",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    canon = body["canonical_id"]
    assert canon
    # map 真落库：两侧 resolve 到同一 canonical
    assert sm._cpi.resolve("telegram", "6107037825") == canon
    assert sm._cpi.resolve("whatsapp", "639649321471") == canon
    # 合流观测 totals 落盘
    totals = json.loads(
        (cfg_dir / "identity_shadow_totals.json").read_text("utf-8"))
    assert int(totals.get("confirm_pairs") or 0) == 1


def test_reconfirm_already_linked_does_not_inflate_totals(tmp_path):
    """幂等重确认（验证 ping / 双击）不虚胀 confirm_pairs（生产踩过：1→2）。"""
    client, sm, cfg_dir = _build_app(tmp_path)
    payload = {
        "platform_a": "telegram", "chat_a": "6107037825",
        "platform_b": "whatsapp", "chat_b": "639649321471",
    }
    r1 = client.post("/api/identity/shadow/confirm-link", json=payload)
    assert r1.status_code == 200
    assert r1.json().get("already_linked") is not True
    r2 = client.post("/api/identity/shadow/confirm-link", json=payload)
    assert r2.status_code == 200
    body2 = r2.json()
    # 重确认仍返回同一 canonical（幂等成功，不报错）
    assert body2["canonical_id"] == r1.json()["canonical_id"]
    assert body2.get("already_linked") is True
    totals = json.loads(
        (cfg_dir / "identity_shadow_totals.json").read_text("utf-8"))
    assert int(totals.get("confirm_pairs") or 0) == 1


def test_identity_list_and_link_work_via_state_sm(tmp_path):
    client, sm, cfg_dir = _build_app(tmp_path)
    assert client.get("/api/identity").status_code == 200
    payload = {
        "platform_a": "telegram", "uid_a": "111",
        "platform_b": "line", "uid_b": "U222",
    }
    r = client.post("/api/identity/link", json=payload)
    assert r.status_code == 200, r.text
    assert sm._cpi.resolve("line", "U222") == sm._cpi.resolve(
        "telegram", "111")
    # 幂等重关联不虚胀 manual_links（与 confirm 同口径）
    assert client.post("/api/identity/link", json=payload).status_code == 200
    totals = json.loads(
        (cfg_dir / "identity_shadow_totals.json").read_text("utf-8"))
    assert int(totals.get("manual_links") or 0) == 1


def test_still_503_when_no_skill_manager_anywhere(tmp_path):
    client, _, _ = _build_app(tmp_path, with_state_sm=False)
    r = client.post("/api/identity/shadow/confirm-link", json={
        "platform_a": "telegram", "chat_a": "1",
        "platform_b": "whatsapp", "chat_b": "2",
    })
    assert r.status_code == 503
    assert client.get("/api/identity").status_code == 503
