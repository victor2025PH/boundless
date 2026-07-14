"""付费实验发放路径端到端门禁：grant 路由 → EntitlementStore → resolve_entitlement
→ 命理详批门控放行。这是开付费实验前的「运营配方」验证——照此操作必然生效：

  1. overlay 开 monetization.enabled + gate.enabled（bootstrap 注册 entitlement resolver）
  2. POST /api/monetize/grant {contact_key, kind:"unlock", item_id:"bazi_reading"}
  3. 该 contact 求详批 → 放行完整深读；未购者 → 免费大方向 + 软引导
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.utils.companion_context import (
    reset_relationship_providers,
    resolve_entitlement,
    set_relationship_providers,
)
from src.utils.entitlement_store import EntitlementStore
from src.web.routes.monetization_routes import register_monetization_routes

_SMcls = __import__(
    "src.skills.skill_manager", fromlist=["SkillManager"]
).SkillManager


@pytest.fixture(autouse=True)
def _providers_clean():
    reset_relationship_providers()
    yield
    reset_relationship_providers()


def _client():
    app = FastAPI()

    def _auth(r: Request):
        return True

    cm = SimpleNamespace(config={"monetization": {"enabled": True}}, config_path="")
    app.state.config_manager = cm
    register_monetization_routes(app, api_auth=_auth, config_manager=cm)
    app.state.entitlement_store = EntitlementStore(":memory:")
    return TestClient(app), app.state.entitlement_store


class _SM:
    """轻量绑定：真实的 _bazi_deep_allowed / _bazi_entitlement 权益链。"""
    _bazi_cfg = _SMcls._bazi_cfg
    _bazi_entitlement = _SMcls._bazi_entitlement
    _bazi_deep_allowed = _SMcls._bazi_deep_allowed
    _monetization_gate_enabled = _SMcls._monetization_gate_enabled

    def __init__(self):
        self.config = SimpleNamespace(config={
            "companion": {"bazi": {"enabled": True}},
            "monetization": {"enabled": True, "gate": {"enabled": True}},
        })
        self.logger = logging.getLogger("test_paid")


def test_grant_route_unlocks_and_gate_opens():
    """完整配方：grant 路由发放 → resolver 注册 → 详批门控对购买者放行。"""
    client, store = _client()
    # ① 运营经 API 手动发放（照生产 curl 同 body）
    r = client.post("/api/monetize/grant", json={
        "contact_key": "cust-1", "kind": "unlock", "item_id": "bazi_reading"})
    body = r.json()
    assert body["ok"] is True and body["newly_unlocked"] is True
    assert "bazi_reading" in body["entitlement"]["unlocked"]
    # ② bootstrap 同款 resolver 注册（monetization.enabled=true 时生产自动做）
    set_relationship_providers(
        entitlement_resolver=lambda ck: store.get_entitlement(ck))
    assert "bazi_reading" in (resolve_entitlement("cust-1") or {}).get("unlocked", [])
    # ③ 详批门控：购买者放行、未购者拦下（gate 全开）
    sm = _SM()
    assert sm._bazi_deep_allowed({}, "cust-1") is True
    assert sm._bazi_deep_allowed({}, "cust-2") is False


def test_grant_idempotent_and_ledger():
    client, store = _client()
    r1 = client.post("/api/monetize/grant", json={
        "contact_key": "c9", "kind": "unlock", "item_id": "bazi_reading",
        "ref": "manual-001"})
    assert r1.json()["newly_unlocked"] is True
    # 同 ref 重复发放 → 不重复解锁（幂等）
    r2 = client.post("/api/monetize/grant", json={
        "contact_key": "c9", "kind": "unlock", "item_id": "bazi_reading",
        "ref": "manual-001"})
    assert r2.json()["newly_unlocked"] is False
    ent = store.get_entitlement("c9")
    assert ent["unlocked"].count("bazi_reading") == 1


def test_vip_grant_does_not_cover_bazi_but_custom_tier_can():
    """默认目录 vip/svip 的 grants 不含 bazi_reading（详批只走单点解锁）；
    运营若想会员含详批，catalog overlay 给 tier 加 grant 即可——两条路都验证。"""
    client, store = _client()
    client.post("/api/monetize/grant", json={
        "contact_key": "v1", "kind": "subscribe", "item_id": "vip"})
    set_relationship_providers(
        entitlement_resolver=lambda ck: store.get_entitlement(ck))
    sm = _SM()
    assert sm._bazi_deep_allowed({}, "v1") is False  # vip 默认不含
    # catalog 覆盖：自定义 tier 含 bazi_reading grant
    from src.utils.monetization import feature_allowed, merge_catalog, tier_grants
    cat = merge_catalog({"tiers": {"fortune_vip": {
        "monthly": 6.9, "label": "命理VIP", "grants": ["bazi_reading"]}}})
    assert "bazi_reading" in tier_grants("fortune_vip", cat)
    assert feature_allowed(
        {"grants": ["bazi_reading"], "unlocked": []}, "bazi_reading",
        gate_enabled=True) is True


def test_subscription_expiry_closes_gate():
    """订阅到期 → 权益随之失效（详批重新落回软引导）。"""
    client, store = _client()
    now = time.time()
    store.grant_subscription(
        "e1", "vip", now - 60, source="manual", now=now - 86400)  # 已过期
    ent = store.get_entitlement("e1")
    assert ent["tier"] == "free" and ent["active"] is False
