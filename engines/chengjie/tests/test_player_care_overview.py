"""B6 看板：/player-care/overview（HTML）+ /api/player-care/overview（JSON）与 build_overview 聚合。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from domains.player_care import sync as psync
from domains.player_care.commandbus import CommandOutbox, send_reengage, send_stop, set_outbox
from domains.player_care.overview import (
    GW_DEGRADED, GW_DOWN, GW_IDLE, GW_OK, GW_UNCONFIGURED, build_overview, gateway_status,
)
from domains.player_care.profile import STAGES, PlayerProfileService, day_key, set_profile_service
from domains.player_care.web.routes import register_routes
from src.contacts.store import ContactStore

T0 = 1_800_000_000.0
DAY = 86400


@pytest.fixture
def store(tmp_path):
    s = ContactStore(tmp_path / "contacts.db")
    yield s
    s.close()


@pytest.fixture
def svc(store):
    s = PlayerProfileService(store, dormant_after_days=7, active_min_deposit_days=2)
    set_profile_service(s)
    yield s
    set_profile_service(None)


@pytest.fixture
def ob(tmp_path):
    o = CommandOutbox(tmp_path / "cb.db")
    set_outbox(o)
    yield o
    set_outbox(None)
    o.close()


@pytest.fixture(autouse=True)
def _reset_last_sync():
    psync._last_summary.clear()
    yield
    psync._last_summary.clear()


def _inbound(svc, key, text, *, now=T0, facts=None, looked_up=False, round_kind="", phone="", acct="wa-01",
             platform="whatsapp"):
    return svc.record_inbound(key=key, text=text, platform=platform, account_id=acct, external_id=key,
                              phone=phone, facts=facts, looked_up=looked_up, round_kind=round_kind, now=now)


def _cfg(**gw):
    g = {"enabled": True, "url": "http://gw.local", "key": "k"}
    g.update(gw)
    return SimpleNamespace(config={"player_gateway": g, "player_care": {}})


# ── 纯函数：网关健康判定 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("configured,recent,exp", [
    (False, {"total": 9, "errors": {"timeout": 9}}, GW_UNCONFIGURED),
    (True, {"total": 0, "errors": {}}, GW_IDLE),
    (True, {"total": 4, "found": 2, "not_found": 2, "errors": {}}, GW_OK),
    (True, {"total": 4, "found": 3, "errors": {"timeout": 1}}, GW_DEGRADED),
    (True, {"total": 4, "found": 2, "errors": {"timeout": 1, "http_401": 1}}, GW_DOWN),
])
def test_gateway_status(configured, recent, exp):
    assert gateway_status(configured, recent) == exp


# ── store 聚合 ──────────────────────────────────────────────────────────────

def test_store_overview_counts(store):
    store.upsert_player_profile("639171234567", phone_e164="639171234567", account_id="wa-01",
                                last_seen=int(T0), lookups=3, last_lookup_at=int(T0), last_found=True)
    store.upsert_player_profile("639170000001", phone_e164="639170000001", account_id="wa-01",
                                last_seen=int(T0 - 10 * DAY), lookups=1, last_lookup_at=int(T0 - 60),
                                last_found=False, last_error="not_found", handoff_at=int(T0))
    store.upsert_player_profile("telegram:555", account_id="tg-01", last_seen=int(T0),
                                lookups=1, last_lookup_at=int(T0 - 2 * DAY), last_found=False, last_error="timeout")
    store.upsert_player_profile("telegram:556", account_id="tg-01", last_seen=int(T0))  # 从没查过

    c = store.player_overview_counts(active_since=int(T0 - 7 * DAY), lookup_since=int(T0 - DAY))
    assert c["total"] == 4 and c["with_phone"] == 2 and c["handoff"] == 1 and c["active"] == 3
    assert c["lookups_total"] == 5 and c["last_lookup_at"] == int(T0)
    assert c["by_account"] == {"wa-01": 2, "tg-01": 2}
    # 窗口只含 24h 内最后一次查询：2 条（timeout 那条在 2 天前，没查过的不算）
    assert c["recent_lookups"] == {"total": 2, "found": 1, "not_found": 1, "errors": {}}
    c2 = store.player_overview_counts(lookup_since=0)
    assert c2["recent_lookups"] == {"total": 3, "found": 1, "not_found": 1, "errors": {"timeout": 1}}


def test_store_overview_counts_empty(store):
    c = store.player_overview_counts(active_since=1, lookup_since=1)
    assert c["total"] == 0 and c["by_account"] == {} and c["last_lookup_at"] == 0
    assert c["recent_lookups"] == {"total": 0, "found": 0, "not_found": 0, "errors": {}}


# ── build_overview ─────────────────────────────────────────────────────────

def test_build_overview_shape_and_numbers(svc):
    facts = {"found": True, "text": "platform: JILI, game: Super Ace"}
    _inbound(svc, "639171234567", "hi", phone="639171234567", now=T0 - 60)
    _inbound(svc, "639171234567", "I play JILI Super Ace", phone="639171234567", now=T0 - 30,
             facts=facts, looked_up=True, round_kind="visible")
    svc.record_gate_hit("639171234567", "wa-01", now=T0 - 20)
    _inbound(svc, "telegram:555", "hello", acct="tg-01", platform="telegram", now=T0 - 10,
             facts={"found": False, "error": "timeout"}, looked_up=True)

    d = build_overview(_cfg(), now=T0)
    assert d["ok"] is True and d["day"] == day_key(T0) and d["notes"] == []
    assert d["contacts"]["total"] == 2 and d["contacts"]["with_phone"] == 1 and d["contacts"]["active_7d"] == 2
    assert d["contacts"]["by_account"] == {"wa-01": 1, "tg-01": 1}
    assert d["stages"]["order"] == STAGES
    assert d["stages"]["totals"]["registered"] == 1 and d["stages"]["totals"]["new_friend"] == 1  # 查到事实 → registered
    assert sum(d["stages"]["totals"].values()) == 2
    assert d["stages"]["by_account"]["wa-01"] == {"registered": 1}
    t = d["today"]
    assert t["inbound"] == 3 and t["lookups"] == 2 and t["found"] == 1
    assert t["visible"] == 1 and t["gate_hits"] == 1 and t["new_profiles"] == 2
    assert {r["account_id"] for r in t["accounts"]} == {"wa-01", "tg-01"}
    gw = d["gateway"]
    assert gw["enabled"] and gw["key_set"] and gw["url"] == "http://gw.local"
    assert gw["lookups_total"] == 2 and gw["last_lookup_at"] == int(T0 - 10)
    assert gw["recent_24h"] == {"total": 2, "found": 1, "not_found": 0, "errors": {"timeout": 1}}
    assert gw["status"] == GW_DOWN  # 2 次里 1 次超时 → ≥50%
    assert d["sync"]["enabled"] is True and d["sync"]["last"] == {}
    assert d["commandbus"] == {"enabled": False, "stats": None}


def test_build_overview_unconfigured_gateway_and_no_key(svc, monkeypatch):
    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    d = build_overview(_cfg(key=""), now=T0)
    assert d["gateway"]["status"] == GW_UNCONFIGURED and d["gateway"]["key_set"] is False
    assert d["gateway"]["key_env"] == "GATEWAY_KEY"
    d2 = build_overview(_cfg(), now=T0)
    assert d2["gateway"]["status"] == GW_IDLE  # 配好了但没人查过


def test_build_overview_without_profile_store_is_fail_soft():
    set_profile_service(None)
    d = build_overview(SimpleNamespace(config={"player_gateway": {}}), now=T0)  # 无 config_path → 不落盘
    assert d["ok"] is True and d["contacts"]["total"] == 0
    assert d["notes"] == ["profile_store_unavailable"]
    d2 = build_overview(SimpleNamespace(config={"player_care": {"profile": {"enabled": False}}}), now=T0)
    assert d2["notes"] == ["profile_disabled"]


def test_build_overview_includes_last_sync_and_outbox(svc, ob):
    psync.run_player_sync(_cfg(), now=T0, profile=svc, goal_store=None)
    send_reengage(ob, account="wa-01", phone="639171234567", messages=["hoy"])
    send_stop(ob, account="wa-01", phone="639170000001")
    cfg = SimpleNamespace(config={"player_gateway": {}, "player_care": {"commandbus": {"enabled": True}}})
    d = build_overview(cfg, now=T0, outbox=ob)
    assert d["sync"]["last"]["ts"] == int(T0) and d["sync"]["last"]["scanned"] == 0
    assert d["commandbus"]["enabled"] is True
    st = d["commandbus"]["stats"]
    assert st["by_status"]["queued"] == 2 and st["stopped"] == 1


def test_last_sync_summary_recorded_even_when_disabled():
    assert psync.last_sync_summary() == {}
    psync.run_player_sync(SimpleNamespace(config={"player_care": {"sync": {"enabled": False}}}), now=T0)
    s = psync.last_sync_summary()
    assert s["skipped"] == "disabled" and s["ts"] == int(T0)


# ── web 路由 ────────────────────────────────────────────────────────────────

class _Tpl:
    def __init__(self):
        self.calls = []

    def TemplateResponse(self, request, name, context):
        from fastapi.responses import HTMLResponse
        self.calls.append((name, context))
        return HTMLResponse(f"<html>{name}</html>")


def _client(cfg, *, page_auth_ok=True):
    from fastapi import HTTPException
    app = FastAPI()
    tpl = _Tpl()

    def _auth(request: Request):
        return None

    def _page(request: Request):
        if not page_auth_ok:
            raise HTTPException(status_code=403, detail="nope")

    ctx = SimpleNamespace(config_manager=SimpleNamespace(config=cfg), api_auth=_auth,
                          api_write_factory=lambda perm: _auth, page_auth=_page, templates=tpl)
    register_routes(app, ctx)
    return TestClient(app), tpl


def test_route_api_overview_json(svc):
    _inbound(svc, "639171234567", "hi", phone="639171234567", now=T0)
    c, _ = _client({"player_gateway": {"enabled": True, "url": "http://gw", "key": "k"}})
    r = c.get("/api/player-care/overview")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["contacts"]["total"] == 1
    assert set(d) >= {"contacts", "stages", "today", "gateway", "sync", "commandbus", "notes"}


def test_route_overview_page_renders_domain_template_and_respects_page_auth():
    c, tpl = _client({})
    r = c.get("/player-care/overview")
    assert r.status_code == 200 and "player_care_overview.html" in r.text
    assert tpl.calls[0][0] == "player_care_overview.html"
    c2, _ = _client({}, page_auth_ok=False)
    assert c2.get("/player-care/overview").status_code == 403


def test_route_api_overview_fail_soft(monkeypatch):
    import domains.player_care.web.routes as routes

    def _boom(cfg):
        raise RuntimeError("x")

    monkeypatch.setattr(routes, "build_overview", _boom)
    c, _ = _client({})
    r = c.get("/api/player-care/overview")
    assert r.status_code == 200 and r.json() == {"ok": False, "error": "overview_failed"}


def test_manifest_declares_overview_page():
    import yaml
    from pathlib import Path
    mf = yaml.safe_load((Path(__file__).resolve().parents[1] / "domains" / "player_care" / "manifest.yaml").read_text(encoding="utf-8"))
    web = mf["web"]
    assert web["routes"] is True
    pages = {p["path"]: p for p in web["pages"]}
    assert pages["/player-care/overview"]["key"] == "player_care_overview"
    assert (Path(__file__).resolve().parents[1] / "domains" / "player_care" / "web" / "templates"
            / "player_care_overview.html").is_file()


def test_manifest_roles_registered_into_page_permissions():
    """web.pages[].roles → 核心 PAGE_PERMISSIONS（admin / supervisor / viewer 能看看板，agent 不能）；
    核心已有的键域不能改；没写 roles 的域页仍仅 master。"""
    import yaml
    from pathlib import Path
    from src.utils import web_user_store as wus

    mf = yaml.safe_load((Path(__file__).resolve().parents[1] / "domains" / "player_care" / "manifest.yaml").read_text(encoding="utf-8"))
    pages = mf["web"]["pages"]
    assert set(pages[0]["roles"]) >= {"master", "admin", "viewer"}

    saved, saved_owner = dict(wus.PAGE_PERMISSIONS), dict(wus._DOMAIN_PAGE_OWNER)
    try:
        reg = wus.register_domain_page_permissions(pages + [
            {"key": "settings", "roles": ["viewer"]},            # 核心键：不得放宽
            {"key": "pc_no_roles", "path": "/x"},                 # 无 roles：不注册 → 仅 master
            {"key": "pc_bad_roles", "roles": ["god", "root"]},    # 全是未知角色：不注册
            {"key": "pc_admin_only", "roles": ["ADMIN"]},         # 大小写归一 + master 自动带上
        ], "player_care")
        assert set(reg) == {"player_care_overview", "pc_admin_only"}
        assert wus.PAGE_PERMISSIONS["settings"] == {wus.ROLE_MASTER}
        assert wus.PAGE_PERMISSIONS["pc_admin_only"] == {wus.ROLE_MASTER, wus.ROLE_ADMIN}
        chk = wus.WebUserStore.can_access_page
        for role, ok in ((wus.ROLE_MASTER, True), (wus.ROLE_ADMIN, True), (wus.ROLE_SUPERVISOR, True),
                         (wus.ROLE_VIEWER, True), (wus.ROLE_AGENT, False)):
            assert chk(None, role, "player_care_overview") is ok, role
        assert chk(None, wus.ROLE_ADMIN, "pc_no_roles") is False and chk(None, wus.ROLE_MASTER, "pc_no_roles") is True
        # 别的域不能改 player_care 的键（先到先得，不串域）
        assert wus.register_domain_page_permissions([{"key": "pc_admin_only", "roles": ["agent"]}], "other") == {}
        assert wus.PAGE_PERMISSIONS["pc_admin_only"] == {wus.ROLE_MASTER, wus.ROLE_ADMIN}
        # 同一域重注册以最新清单为准：改角色生效、清单里没了的旧键退回仅 master
        wus.register_domain_page_permissions([{"key": "pc_admin_only", "roles": ["viewer"]}], "player_care")
        assert wus.PAGE_PERMISSIONS["pc_admin_only"] == {wus.ROLE_MASTER, wus.ROLE_VIEWER}
        assert "player_care_overview" not in wus.PAGE_PERMISSIONS
        assert chk(None, wus.ROLE_ADMIN, "player_care_overview") is False
        assert wus.PAGE_PERMISSIONS["settings"] == {wus.ROLE_MASTER}
    finally:
        wus.PAGE_PERMISSIONS.clear()
        wus.PAGE_PERMISSIONS.update(saved)
        wus._DOMAIN_PAGE_OWNER.clear()
        wus._DOMAIN_PAGE_OWNER.update(saved_owner)
