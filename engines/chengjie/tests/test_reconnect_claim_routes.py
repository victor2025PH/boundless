# -*- coding: utf-8 -*-
"""回连认领路由端到端门禁（假 store / 假 CPI，零生产依赖）。

覆盖：inventory 覆盖率读数 / candidates 分层匹配 / claim 真链路（走真
``link_and_merge_memory``，CPI 用最小契约桩）/ auto-claim dry_run 与实跑 /
台账读数 / 参数校验（400/404/同账号拒绝）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.contacts import reconnect_claim as rc
from src.web.routes.reconnect_claim_routes import register_reconnect_claim_routes


# ── 桩件 ─────────────────────────────────────────────────────────────────────

def _row(cid, ck, *, name="", username="", phone="", last_ts=0.0,
         chat_type="private", bot=0):
    return {
        "conversation_id": cid, "chat_key": ck, "display_name": name,
        "username": username, "phone": phone, "last_ts": last_ts,
        "chat_type": chat_type, "peer_is_bot": bot, "language": "zh",
        "first_seen": 1.0,
    }


class _FakeInbox:
    def __init__(self, rows):
        self._rows = rows   # list of row dicts (all accounts mixed)

    def list_conversations(self, *, platform, account_id, limit, before_ts=None):
        got = [r for r in self._rows
               if r["conversation_id"].startswith(f"{platform}:{account_id}:")]
        got.sort(key=lambda r: -float(r.get("last_ts") or 0))
        if before_ts is not None:
            got = [r for r in got if float(r.get("last_ts") or 0) < before_ts]
        return got[:limit]

    def get_conversation(self, cid):
        for r in self._rows:
            if r["conversation_id"] == cid:
                return r
        return None


class _MiniCpi:
    """link_and_merge_memory 所需最小契约：resolve / link / get_by_canonical。"""

    def __init__(self):
        self._map = {}   # "p:u" -> canonical

    def resolve(self, platform, uid):
        return self._map.get(f"{platform}:{uid}", f"{platform}:{uid}")

    def link(self, pa, ua, pb, ub):
        canon = self.resolve(pa, ua)
        self._map[f"{pa}:{ua}"] = canon
        self._map[f"{pb}:{ub}"] = canon
        return canon

    def get_by_canonical(self, canonical_id):
        return [tuple(k.split(":", 1)) for k, v in self._map.items()
                if v == canonical_id]


class _MiniEpisodic:
    def __init__(self):
        self.merges = []

    def merge_key(self, old, new):
        self.merges.append((old, new))
        return 3


class _StubSm:
    def __init__(self, cpi, epi):
        self._cpi = cpi
        self._episodic_store = epi


@pytest.fixture()
def app_env(monkeypatch, tmp_path):
    rows = [
        _row("telegram:dead:111", "111", name="Ann", username="ann_l",
             phone="+86 13800138000", last_ts=100),
        _row("telegram:dead:222", "222", name="Bob", last_ts=90),
        _row("telegram:dead:333", "333", name="Cara", username="cara9",
             last_ts=80),
        # 新账号：111 同 chat_key（exact），cara9 同 username，999 无信号
        _row("telegram:fresh:111", "111", name="Ann New", last_ts=10),
        _row("telegram:fresh:555", "555", name="c", username="cara9", last_ts=9),
        _row("telegram:fresh:999", "999", name="Stranger", last_ts=8),
    ]
    cpi = _MiniCpi()
    epi = _MiniEpisodic()
    monkeypatch.setattr(
        "src.web.web_context.resolve_skill_manager",
        lambda client, app=None: _StubSm(cpi, epi))
    # 台账落 tmp（同时验证 default_ledger_path 跟随 AITR_DATA_DIR）
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(rc, "_ledger_singleton", None)

    app = FastAPI()
    app.state.inbox_store = _FakeInbox(rows)
    app.state.telegram_client = None
    register_reconnect_claim_routes(app, api_auth=lambda r: None)
    client = TestClient(app)
    return client, cpi, epi


# ── 用例 ─────────────────────────────────────────────────────────────────────

def test_inventory_coverage_and_validation(app_env):
    client, *_ = app_env
    r = client.get("/api/admin/asset/reconnect/inventory",
                   params={"platform": "telegram", "account_id": "dead"})
    assert r.status_code == 200
    data = r.json()
    assert data["scanned"] == 3
    cov = data["coverage"]
    assert cov["total"] == 3 and cov["with_username"] == 2
    assert cov["with_phone"] == 1 and cov["with_any_handle"] == 2
    assert {row["conversation_id"] for row in data["rows"]} == {
        "telegram:dead:111", "telegram:dead:222", "telegram:dead:333"}
    # 缺参 400
    assert client.get("/api/admin/asset/reconnect/inventory",
                      params={"platform": "telegram"}).status_code == 400


def test_candidates_tiers(app_env):
    client, *_ = app_env
    r = client.get("/api/admin/asset/reconnect/candidates", params={
        "platform": "telegram", "new_account_id": "fresh",
        "old_account_id": "dead"})
    assert r.status_code == 200
    cands = r.json()["candidates"]
    by_new = {c["new_conversation_id"]: c for c in cands}
    assert by_new["telegram:fresh:111"]["confidence"] == 1.0
    assert by_new["telegram:fresh:111"]["matched_on"][0] == "exact_peer"
    assert by_new["telegram:fresh:555"]["confidence"] == 0.9
    assert "telegram:fresh:999" not in by_new
    # 同账号拒绝
    assert client.get("/api/admin/asset/reconnect/candidates", params={
        "platform": "telegram", "new_account_id": "dead",
        "old_account_id": "dead"}).status_code == 400


def test_claim_end_to_end_and_404(app_env):
    client, cpi, epi = app_env
    r = client.post("/api/admin/asset/reconnect/claim", json={
        "old_conversation_id": "telegram:dead:111",
        "new_conversation_id": "telegram:fresh:111",
        "matched_on": "exact_peer"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] and not data["already_linked"]
    assert data["rows_merged"] == 3            # 真 link_and_merge_memory 搬行
    assert epi.merges                          # episodic merge_key 被调用
    # 两键已同 canonical
    assert cpi.resolve("telegram", "dead:111") == cpi.resolve(
        "telegram", "fresh:111")
    # 幂等：二次认领 already_linked，不再搬行
    r2 = client.post("/api/admin/asset/reconnect/claim", json={
        "old_conversation_id": "telegram:dead:111",
        "new_conversation_id": "telegram:fresh:111"})
    assert r2.status_code == 200 and r2.json()["already_linked"]
    assert len(epi.merges) == 1
    # 不存在的会话 404
    r3 = client.post("/api/admin/asset/reconnect/claim", json={
        "old_conversation_id": "telegram:dead:nope",
        "new_conversation_id": "telegram:fresh:111"})
    assert r3.status_code == 404
    # 台账可读
    claims = client.get("/api/admin/asset/reconnect/claims").json()["claims"]
    assert len(claims) == 2
    assert claims[0]["old_conversation_id"] == "telegram:dead:111"


def test_auto_claim_dry_run_then_execute(app_env):
    client, cpi, epi = app_env
    body = {"platform": "telegram", "new_account_id": "fresh",
            "old_account_id": "dead"}
    r = client.post("/api/admin/asset/reconnect/auto-claim",
                    json=dict(body, dry_run=True))
    assert r.status_code == 200
    data = r.json()
    assert data["dry_run"] is True
    # exact_peer(1.0) + username(0.9) 达标；999 无信号不入
    assert {c["new_conversation_id"] for c in data["eligible"]} == {
        "telegram:fresh:111", "telegram:fresh:555"}
    assert epi.merges == []                    # dry_run 零副作用

    r2 = client.post("/api/admin/asset/reconnect/auto-claim", json=body)
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["claimed"] == 2 and d2["attempted"] == 2
    # 再跑一遍：已链对子不再入 eligible
    r3 = client.post("/api/admin/asset/reconnect/auto-claim", json=body)
    assert r3.json()["claimed"] == 0
    assert r3.json()["attempted"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
