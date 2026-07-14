# -*- coding: utf-8 -*-
"""Phase 12.5 (2026-04-25): batch revive prefix 展开 + with_total 单测."""
from __future__ import annotations

import time

import pytest


def _seed_dead(name: str) -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    update_canonical_metadata(cid, {
        "age_band": "40s", "gender": "female", "is_japanese": True,
        "l2_score": 80, "l2_persona_key": "jp_female_midlife",
        "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
        "referral_dead_reason": "recipient_not_found",
        "referral_dead_at": "2026-04-20T00:00:00Z",
        "referral_fail_count_recipient_not_found": 1,
    }, tags=["l2_verified", "referral_dead"])
    return cid


# ═══════════════════════════════════════════════════════════════════
# batch revive prefix expansion
# ═══════════════════════════════════════════════════════════════════

class TestBatchPrefixExpansion:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_unique_prefix_resolves_to_full_id(self, tmp_db, client):
        cid = _seed_dead("UniquePrefix")
        prefix = cid[:8]  # 8 字符肯定唯一
        r = client.post("/lead-mesh/leads/revive-referral-batch",
                         json={"canonical_ids": [prefix]})
        data = r.json()
        assert data["revived"] == 1
        assert data["revived_ids"] == [cid]
        assert data["expanded_count"] == 1

    def test_too_short_prefix_errors(self, tmp_db, client):
        _seed_dead("Short")
        r = client.post("/lead-mesh/leads/revive-referral-batch",
                         json={"canonical_ids": ["ab"]})
        data = r.json()
        assert data["revived"] == 0
        assert any("prefix 太短" in (e.get("reason") or "")
                   for e in data["errors"])

    def test_no_match_prefix_errors(self, tmp_db, client):
        _seed_dead("NoMatch")
        r = client.post("/lead-mesh/leads/revive-referral-batch",
                         json={"canonical_ids": ["zzzzzzzz"]})
        data = r.json()
        assert data["revived"] == 0
        assert any("无匹配" in (e.get("reason") or "")
                   for e in data["errors"])

    def test_ambiguous_prefix_errors(self, tmp_db, client):
        """如果两个 canonical_id 共前缀, 报歧义."""
        # 这个测试要造出共前缀的 cid 比较难 (UUID 随机). 改成直接 raw 写
        # 两个共前缀的 fake cid 到 leads_canonical, 然后试 prefix.
        from src.host.lead_mesh.canonical import _connect
        with _connect() as c:
            c.execute(
                "INSERT INTO leads_canonical (canonical_id, primary_name,"
                " tags, metadata_json) VALUES (?,?,?,?)",
                ("aaaa-1111-aaaa-1111-aaaaaaaaaaaa", "X1",
                 "l2_verified,referral_dead", "{}"))
            c.execute(
                "INSERT INTO leads_canonical (canonical_id, primary_name,"
                " tags, metadata_json) VALUES (?,?,?,?)",
                ("aaaa-2222-bbbb-2222-bbbbbbbbbbbb", "X2",
                 "l2_verified,referral_dead", "{}"))
        r = client.post("/lead-mesh/leads/revive-referral-batch",
                         json={"canonical_ids": ["aaaa"]})
        data = r.json()
        assert data["revived"] == 0
        assert any("歧义" in (e.get("reason") or "")
                   for e in data["errors"])

    def test_full_id_passthrough_no_lookup(self, tmp_db, client):
        """完整 36 字符 cid 直传, 不走 prefix 查询."""
        cid = _seed_dead("FullId")
        assert len(cid) >= 36
        r = client.post("/lead-mesh/leads/revive-referral-batch",
                         json={"canonical_ids": [cid]})
        assert r.json()["revived"] == 1


# ═══════════════════════════════════════════════════════════════════
# with_total query param
# ═══════════════════════════════════════════════════════════════════

class TestWithTotal:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_with_total_default_false_no_total(self, tmp_db, client):
        r = client.get("/lead-mesh/leads/l2-verified")
        data = r.json()
        assert "total" not in data

    def test_with_total_true_returns_total(self, tmp_db, client):
        # 造 5 个 l2_verified
        for i in range(5):
            from src.host.lead_mesh import (resolve_identity,
                                              update_canonical_metadata)
            cid = resolve_identity(platform="facebook",
                                    account_id=f"fb:T{i}",
                                    display_name=f"T{i}")
            update_canonical_metadata(cid,
                {"l2_score": 70 + i,
                 "l2_persona_key": "jp_female_midlife",
                 "l2_verified_at": "now"},
                tags=["l2_verified"])
        r = client.get("/lead-mesh/leads/l2-verified?with_total=true&limit=2")
        data = r.json()
        assert data["count"] == 2  # limit=2 切片
        assert data["total"] == 5  # 总数 5

    def test_with_total_respects_include_tags(self, tmp_db, client):
        from src.host.lead_mesh import (resolve_identity,
                                          update_canonical_metadata)
        # 3 个普通 l2 + 2 个 dead
        for i in range(3):
            cid = resolve_identity(platform="facebook",
                                    account_id=f"fb:N{i}",
                                    display_name=f"N{i}")
            update_canonical_metadata(cid,
                {"l2_score": 70 + i, "l2_verified_at": "x"},
                tags=["l2_verified"])
        for i in range(2):
            cid = resolve_identity(platform="facebook",
                                    account_id=f"fb:D{i}",
                                    display_name=f"D{i}")
            update_canonical_metadata(cid,
                {"l2_score": 70 + i, "l2_verified_at": "x"},
                tags=["l2_verified", "referral_dead"])
        # 总 5, dead 2, 非 dead 3
        r1 = client.get("/lead-mesh/leads/l2-verified"
                         "?with_total=true&include_tags=referral_dead")
        assert r1.json()["total"] == 2
        r2 = client.get("/lead-mesh/leads/l2-verified"
                         "?with_total=true&exclude_tags=referral_dead")
        assert r2.json()["total"] == 3


# ═══════════════════════════════════════════════════════════════════
# count_l2_verified_leads SQL injection 防护
# ═══════════════════════════════════════════════════════════════════

class TestCountSecurity:
    def test_malicious_tag_not_injected(self, tmp_db):
        """非 alnum/_-: 字符的 tag 应被 silently skip (不参与 SQL)."""
        from src.host.lead_mesh import count_l2_verified_leads
        # 造 1 个普通 l2_verified
        from src.host.lead_mesh import (resolve_identity,
                                          update_canonical_metadata)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:Inj",
                                display_name="Inj")
        update_canonical_metadata(cid, {"l2_score": 80,
                                          "l2_verified_at": "x"},
                                    tags=["l2_verified"])
        # 包含恶意片段, 但应被 sanitize
        n = count_l2_verified_leads(
            include_tags=["l2_verified", "x' OR '1'='1"])
        # 'x\' OR \'1\'=\'1' 因含 ' 被 skip, 仅剩 l2_verified 过滤
        assert n == 1
