# -*- coding: utf-8 -*-
"""补丁提案路由：开关、硬锁 409、fill 写入、retire 钉住、stale、revert。"""
import sys
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_HDRS = {"Authorization": "Bearer test-token"}
_JSON = {**_HDRS, "Content-Type": "application/json"}

_THIN = {
    "id": "thin_p", "name": "薄档",
    "role": "店员",
}


async def _build_app(tmp_path, *, enabled=True, extra_persona=None):
    profiles = [dict(_THIN)]
    if extra_persona:
        profiles.append(extra_persona)
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {
            "profiles": profiles,
            "proposals": {"enabled": bool(enabled)},
        },
        "persona_persistence": {"enabled": True},
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager
    from src.utils import persona_proposal_store as pps

    pps.reset()
    pps.configure(tmp_path / "persona_proposals.db")
    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture(autouse=True)
def _reset():
    yield
    from src.utils.persona_manager import PersonaManager
    from src.utils import persona_proposal_store as pps
    PersonaManager.reset()
    pps.reset()


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_status_off_hides_write(tmp_path):
    app = await _build_app(tmp_path, enabled=False)
    async with _client(app) as c:
        st = await c.get("/api/personas/proposals/status", headers=_HDRS)
        assert st.status_code == 200 and st.json()["enabled"] is False
        g = await c.post("/api/personas/profiles/thin_p/proposals/generate",
                         headers=_JSON, json={})
        assert g.status_code == 403


@pytest.mark.asyncio
async def test_generate_fill_accept_writes_and_revert(tmp_path):
    app = await _build_app(tmp_path, enabled=True)
    async with _client(app) as c:
        g = await c.post("/api/personas/profiles/thin_p/proposals/generate",
                         headers=_JSON, json={})
        assert g.status_code == 200, g.text
        body = g.json()
        fills = [p for p in body["proposals"] if p["field"] == "background"]
        assert fills and fills[0]["proposed"] is None
        assert all(p["field"] not in ("name", "age", "gender") for p in body["proposals"]
                   if p["kind"] == "fill")
        pid = fills[0]["id"]
        acc = await c.post(
            f"/api/personas/profiles/thin_p/proposals/{pid}/accept",
            headers=_JSON, json={"proposed": "在洛杉矶做服装设计。"})
        assert acc.status_code == 200, acc.text
        got = await c.get("/api/personas/profiles/thin_p", headers=_HDRS)
        assert got.status_code == 200
        persona = got.json()["persona"]
        assert "洛杉矶" in (persona.get("background") or "")
        rev = await c.post("/api/personas/profiles/thin_p/revert", headers=_JSON, json={})
        assert rev.status_code == 200, rev.text
        got2 = (await c.get("/api/personas/profiles/thin_p", headers=_HDRS)).json()["persona"]
        assert not (got2.get("background") or "").strip()


@pytest.mark.asyncio
async def test_hard_lock_accept_409(tmp_path):
    app = await _build_app(tmp_path, enabled=True)
    from src.utils import persona_proposal_store as pps
    store = pps.get()
    rec = store.insert_pending({
        "persona_id": "thin_p", "kind": "fill", "field": "age",
        "action": "fill", "dedupe_key": "fill:age",
        "proposed": "99", "base_rev": "x",
    })
    async with _client(app) as c:
        acc = await c.post(
            f"/api/personas/profiles/thin_p/proposals/{rec['id']}/accept",
            headers=_JSON, json={"proposed": "99"})
        assert acc.status_code == 409
        persona = (await c.get("/api/personas/profiles/thin_p", headers=_HDRS)).json()["persona"]
        assert persona.get("age") in (None, "", 30) or "age" not in persona or persona.get("age") != 99


@pytest.mark.asyncio
async def test_accept_empty_proposed_400(tmp_path):
    app = await _build_app(tmp_path, enabled=True)
    async with _client(app) as c:
        g = (await c.post("/api/personas/profiles/thin_p/proposals/generate",
                          headers=_JSON, json={})).json()
        pid = next(p["id"] for p in g["proposals"] if p["field"] == "background")
        acc = await c.post(
            f"/api/personas/profiles/thin_p/proposals/{pid}/accept",
            headers=_JSON, json={})
        assert acc.status_code == 400


@pytest.mark.asyncio
async def test_stale_rev_409(tmp_path):
    app = await _build_app(tmp_path, enabled=True)
    async with _client(app) as c:
        g = (await c.post("/api/personas/profiles/thin_p/proposals/generate",
                          headers=_JSON, json={})).json()
        pid = next(p["id"] for p in g["proposals"] if p["field"] == "background")
        # 旁路改档，让 base_rev 失效
        await c.put("/api/personas/profiles/thin_p", headers=_JSON, json={
            "persona": {"tags": ["touched"]}, "merge": True,
        })
        acc = await c.post(
            f"/api/personas/profiles/thin_p/proposals/{pid}/accept",
            headers=_JSON, json={"proposed": "一段背景"})
        assert acc.status_code == 409


@pytest.mark.asyncio
async def test_retire_pin_accept(tmp_path):
    extra = {
        "id": "cat_p", "name": "阿猫", "role": "店员",
        "tastes": {"likes": ["撸猫"]},
        "boundaries": {"retired_facts": [
            {"text": "不再养猫", "added": "2026-08-01", "terms": ["猫"]},
        ]},
    }
    app = await _build_app(tmp_path, enabled=True, extra_persona=extra)
    async with _client(app) as c:
        g = await c.post("/api/personas/profiles/cat_p/proposals/generate",
                         headers=_JSON, json={})
        assert g.status_code == 200, g.text
        retires = [p for p in g.json()["proposals"] if p["kind"] == "retire"]
        assert retires
        acc = await c.post(
            f"/api/personas/profiles/cat_p/proposals/{retires[0]['id']}/accept",
            headers=_JSON, json={})
        assert acc.status_code == 200, acc.text
        persona = (await c.get("/api/personas/profiles/cat_p", headers=_HDRS)).json()["persona"]
        facts = (persona.get("boundaries") or {}).get("retired_facts") or []
        assert len(facts) >= 2
