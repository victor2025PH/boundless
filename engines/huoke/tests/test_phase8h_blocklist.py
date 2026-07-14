# -*- coding: utf-8 -*-
"""Phase 8h: Lead Blocklist 单测 (2026-04-24).

覆盖:
  * Library: add / remove / is_blocklisted / get_entry / list / count
  * Upsert: 加已存在 peer 只更新 reason/note, 不 duplicate
  * FB 前置检查: _check_peer_blocklist 写 journey + skip
  * API: POST / DELETE / GET endpoints
"""
from __future__ import annotations

import os

import pytest

os.environ["OPENCLAW_API_KEY"] = ""

from fastapi.testclient import TestClient
from src.host.api import app
import src.host.database as db_mod


@pytest.fixture(autouse=True)
def _use_tmp_db(tmp_path):
    original = db_mod.DB_PATH
    db_mod.DB_PATH = tmp_path / "blocklist_test.db"
    db_mod.init_db()
    yield
    db_mod.DB_PATH = original


@pytest.fixture
def api_client():
    return TestClient(app, raise_server_exceptions=False)


# ─── Library 层 ─────────────────────────────────────────────────────
class TestBlocklistLibrary:
    def test_add_returns_true_first_time(self):
        from src.host.lead_mesh.blocklist import add_to_blocklist, is_blocklisted
        created = add_to_blocklist("cid-1", reason="spammer", created_by="op1")
        assert created is True
        assert is_blocklisted("cid-1") is True

    def test_add_twice_returns_false_second_time(self):
        """第 2 次加同 cid, 视为 upsert — 返回 False 表示 '未新建'."""
        from src.host.lead_mesh.blocklist import add_to_blocklist, count_blocklist
        add_to_blocklist("cid-up", reason="A")
        created = add_to_blocklist("cid-up", reason="B")
        assert created is False
        # 但 count 仍是 1
        assert count_blocklist() == 1

    def test_remove_returns_true_when_existed(self):
        from src.host.lead_mesh.blocklist import (
            add_to_blocklist, remove_from_blocklist, is_blocklisted)
        add_to_blocklist("cid-r")
        assert is_blocklisted("cid-r")
        assert remove_from_blocklist("cid-r") is True
        assert is_blocklisted("cid-r") is False

    def test_remove_returns_false_when_not_existed(self):
        from src.host.lead_mesh.blocklist import remove_from_blocklist
        assert remove_from_blocklist("nonexistent") is False

    def test_get_entry_fields(self):
        from src.host.lead_mesh.blocklist import add_to_blocklist, get_blocklist_entry
        add_to_blocklist("cid-e", reason="harassment", note="reported",
                          created_by="ops-jane")
        entry = get_blocklist_entry("cid-e")
        assert entry["canonical_id"] == "cid-e"
        assert entry["reason"] == "harassment"
        assert entry["note"] == "reported"
        assert entry["created_by"] == "ops-jane"
        assert entry["created_at"]

    def test_list_blocklist_order_by_created_desc(self):
        import time
        from src.host.lead_mesh.blocklist import add_to_blocklist, list_blocklist
        add_to_blocklist("cid-old")
        time.sleep(1.1)
        add_to_blocklist("cid-new")
        items = list_blocklist()
        assert items[0]["canonical_id"] == "cid-new"
        assert items[1]["canonical_id"] == "cid-old"

    def test_empty_cid_no_op(self):
        from src.host.lead_mesh.blocklist import (
            add_to_blocklist, remove_from_blocklist, is_blocklisted)
        assert add_to_blocklist("") is False
        assert remove_from_blocklist("") is False
        assert is_blocklisted("") is False


# ─── FB 前置检查 ─────────────────────────────────────────────────────
class TestFbBlocklistGate:
    def test_check_gate_writes_journey_on_hit(self):
        """blocklist 命中时写 journey event `greeting_blocked{reason=peer_blocklisted}`."""
        from src.app_automation.facebook import FacebookAutomation
        from src.host.lead_mesh import (resolve_identity, add_to_blocklist,
                                          get_journey)

        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb._current_device = "D_blk"
        fb._last_greet_skip_reason = ""
        fb._current_lead_cid = ""
        fb._current_lead_persona = ""
        fb._current_greet_template_id = ""

        cid = resolve_identity(platform="facebook",
                                 account_id="fb:BlkPeer",
                                 display_name="BlkPeer")
        add_to_blocklist(cid, reason="ops_flagged")

        hit = fb._check_peer_blocklist("BlkPeer", did="D_blk",
                                         persona_key="jp_female_midlife")
        assert hit is True
        # journey 写入
        events = get_journey(cid)
        blocked = [e for e in events if e["action"] == "greeting_blocked"]
        assert any(e["data"]["reason"] == "peer_blocklisted" for e in blocked)

    def test_check_gate_miss_returns_false(self):
        from src.app_automation.facebook import FacebookAutomation
        from src.host.lead_mesh import resolve_identity

        fb = FacebookAutomation.__new__(FacebookAutomation)
        fb._current_device = "D_blk"
        fb._last_greet_skip_reason = ""
        fb._current_lead_cid = ""

        resolve_identity(platform="facebook",
                          account_id="fb:CleanPeer",
                          display_name="CleanPeer")
        # 不加 blocklist
        hit = fb._check_peer_blocklist("CleanPeer", did="D_blk")
        assert hit is False


# ─── API ────────────────────────────────────────────────────────────
class TestBlocklistApi:
    def test_post_adds_to_blocklist(self, api_client):
        from src.host.lead_mesh.blocklist import is_blocklisted
        r = api_client.post(
            "/lead-mesh/peers/cid-a/blocklist",
            json={"reason": "abusive", "note": "reported 3x"})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["created"] is True
        assert is_blocklisted("cid-a") is True

    def test_post_idempotent(self, api_client):
        api_client.post("/lead-mesh/peers/cid-dup/blocklist", json={})
        r2 = api_client.post("/lead-mesh/peers/cid-dup/blocklist", json={})
        body = r2.json()
        assert body["created"] is False
        assert body["was_already_blocklisted"] is True

    def test_delete_removes(self, api_client):
        from src.host.lead_mesh.blocklist import is_blocklisted
        api_client.post("/lead-mesh/peers/cid-d/blocklist", json={})
        assert is_blocklisted("cid-d") is True
        r = api_client.delete("/lead-mesh/peers/cid-d/blocklist")
        assert r.status_code == 200
        body = r.json()
        assert body["removed"] is True
        assert is_blocklisted("cid-d") is False

    def test_list_returns_items(self, api_client):
        api_client.post("/lead-mesh/peers/cid-l1/blocklist",
                         json={"reason": "A"})
        api_client.post("/lead-mesh/peers/cid-l2/blocklist",
                         json={"reason": "B"})
        r = api_client.get("/lead-mesh/blocklist")
        body = r.json()
        assert body["total"] == 2
        assert body["count"] == 2
        # latest first
        cids = [it["canonical_id"] for it in body["items"]]
        assert set(cids) == {"cid-l1", "cid-l2"}
