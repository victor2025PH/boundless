# -*- coding: utf-8 -*-
"""Phase 12.4 (2026-04-25): journey audit + 批量 revive + 分页."""
from __future__ import annotations

import time

import pytest


def _seed_dead(name: str, *, days_ago: int = 0) -> str:
    """造一个 referral_dead canonical 用于测试. days_ago 控制 dead_at."""
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    import datetime as _dt
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    dead_at = (_dt.datetime.utcnow() - _dt.timedelta(days=days_ago)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    update_canonical_metadata(cid, {
        "age_band": "40s", "gender": "female",
        "is_japanese": True, "l2_score": 80,
        "l2_persona_key": "jp_female_midlife",
        "l2_verified_at": dead_at,
        "referral_dead_reason": "recipient_not_found",
        "referral_dead_at": dead_at,
        "referral_fail_count_recipient_not_found": 1,
    }, tags=["l2_verified", "referral_dead"])
    return cid


# ═══════════════════════════════════════════════════════════════════
# revive_referral journey audit
# ═══════════════════════════════════════════════════════════════════

class TestReviveAudit:
    def test_revive_writes_journey(self, tmp_db):
        from src.host.lead_mesh import revive_referral, get_journey
        cid = _seed_dead("CleanMe")
        ok = revive_referral(cid, actor="operator_ui")
        assert ok is True
        events = [e for e in get_journey(cid)
                   if e["action"] == "referral_revived"]
        assert len(events) == 1
        e = events[0]
        assert e["actor"] == "operator_ui"
        assert e["platform"] == "system"
        # snapshot 字段验证
        data = e.get("data") or {}
        assert data.get("had_dead_tag") is True
        assert data.get("dead_reason") == "recipient_not_found"
        assert "fail_counts" in data
        assert data["fail_counts"].get(
            "referral_fail_count_recipient_not_found") == 1

    def test_revive_default_actor_operator(self, tmp_db):
        from src.host.lead_mesh import revive_referral, get_journey
        cid = _seed_dead("Default")
        revive_referral(cid)
        events = [e for e in get_journey(cid)
                   if e["action"] == "referral_revived"]
        assert events[0]["actor"] == "operator"

    def test_revive_noop_no_journey(self, tmp_db):
        """非 dead 的 peer 调 revive → 无 journey 写."""
        from src.host.lead_mesh import (resolve_identity,
                                         update_canonical_metadata,
                                         revive_referral, get_journey)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:Alive",
                                display_name="Alive")
        update_canonical_metadata(cid, {"age_band": "40s"},
                                    tags=["l2_verified"])
        revive_referral(cid)  # noop
        events = [e for e in get_journey(cid)
                   if e["action"] == "referral_revived"]
        assert events == []

    def test_scheduled_recycle_writes_journey_with_actor(self, tmp_db):
        """scheduled task 调 revive 时 actor='scheduled_7d_auto'."""
        from src.host.executor import _line_pool_recycle_dead_peers
        from src.host.lead_mesh import get_journey
        cid = _seed_dead("OldDead", days_ago=10)
        _ok, _m, stats = _line_pool_recycle_dead_peers({"days": 7})
        assert stats["revived"] == 1
        events = [e for e in get_journey(cid)
                   if e["action"] == "referral_revived"]
        assert events[0]["actor"] == "scheduled_7d_auto"


# ═══════════════════════════════════════════════════════════════════
# 批量 revive 端点
# ═══════════════════════════════════════════════════════════════════

class TestBatchReviveEndpoint:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_batch_revive_partial_success(self, tmp_db, client):
        cid_a = _seed_dead("PeerA")
        cid_b = _seed_dead("PeerB")
        # 用完整 36 字符 fake UUID 触发"找到但不是 dead" → skipped 路径
        # (Phase 12.5 后短串走 prefix 查询, 找不到归 errors; 这里测 skipped)
        full_fake = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        r = client.post("/lead-mesh/leads/revive-referral-batch", json={
            "canonical_ids": [cid_a, cid_b, full_fake],
            "actor": "operator_ui_batch",
        })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["revived"] == 2
        # full_fake 在 DB 不存在, revive_referral 返 False → skipped++
        assert data["skipped"] == 1
        assert sorted(data["revived_ids"]) == sorted([cid_a, cid_b])

    def test_batch_revive_400_on_non_list(self, tmp_db, client):
        r = client.post("/lead-mesh/leads/revive-referral-batch",
                         json={"canonical_ids": "not_a_list"})
        assert r.status_code == 400

    def test_batch_revive_dedupe_input(self, tmp_db, client):
        """重复 / 空字符串 / 非 string 在批量端点里被去重去脏."""
        cid_a = _seed_dead("Dup")
        r = client.post("/lead-mesh/leads/revive-referral-batch", json={
            "canonical_ids": [cid_a, cid_a, "", None, cid_a],
        })
        data = r.json()
        assert data["revived"] == 1


# ═══════════════════════════════════════════════════════════════════
# offset 分页
# ═══════════════════════════════════════════════════════════════════

class TestL2VerifiedOffset:
    def test_offset_pagination(self, tmp_db):
        from src.host.lead_mesh import (resolve_identity,
                                         update_canonical_metadata,
                                         list_l2_verified_leads)
        # 造 7 个 l2_verified
        for i in range(7):
            cid = resolve_identity(platform="facebook",
                                    account_id=f"fb:U{i}",
                                    display_name=f"U{i}")
            update_canonical_metadata(cid,
                {"age_band": "40s", "gender": "female",
                 "is_japanese": True, "l2_score": 70 + i,
                 "l2_persona_key": "jp_female_midlife",
                 "l2_verified_at": time.strftime(
                     "%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                tags=["l2_verified"])
        page1 = list_l2_verified_leads(limit=3, offset=0)
        page2 = list_l2_verified_leads(limit=3, offset=3)
        page3 = list_l2_verified_leads(limit=3, offset=6)
        assert len(page1) == 3
        assert len(page2) == 3
        assert len(page3) == 1
        # 三页的 canonical_id 互不重叠
        ids1 = {r["canonical_id"] for r in page1}
        ids2 = {r["canonical_id"] for r in page2}
        ids3 = {r["canonical_id"] for r in page3}
        assert not (ids1 & ids2)
        assert not (ids2 & ids3)
