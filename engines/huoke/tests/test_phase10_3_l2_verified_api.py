# -*- coding: utf-8 -*-
"""Phase 10.3: /lead-mesh/leads/l2-verified API + list_l2_verified_leads 单测.

覆盖:
  * list_l2_verified_leads 默认 (无过滤) 返所有 l2_verified leads
  * age_band / gender / is_japanese 过滤
  * persona_key / platform / min_score 过滤
  * API 端点返 200 + 结构正确
  * l2-verified 路径不会被 /leads/{canonical_id} 捕获

依赖 fixture ``tmp_db`` (conftest).
"""
from __future__ import annotations

import json
import time

import pytest


def _seed_l2_lead(name: str, *, age: str, gender: str, is_jp: bool,
                   score: float, persona: str = "jp_female_midlife",
                   platform: str = "facebook") -> str:
    """工具函数: 入 1 条 l2_verified canonical + metadata."""
    from src.host.lead_mesh import resolve_identity, update_canonical_metadata
    cid = resolve_identity(platform=platform,
                            account_id=f"{platform}:{name}",
                            display_name=name)
    meta = {
        "age_band": age, "gender": gender,
        "is_japanese": is_jp, "l2_score": score,
        "l2_persona_key": persona,
        "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
    }
    tags = ["l2_verified", f"age:{age}", f"gender:{gender}"]
    if is_jp:
        tags.append("is_japanese")
    update_canonical_metadata(cid, meta, tags=tags)
    return cid


class TestListL2VerifiedLeads:
    def test_empty_no_l2_verified(self, tmp_db):
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads()
        assert rows == []

    def test_returns_all_when_no_filter(self, tmp_db):
        _seed_l2_lead("花子", age="40s", gender="female",
                       is_jp=True, score=85)
        _seed_l2_lead("美咲", age="50s", gender="female",
                       is_jp=True, score=78)
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads()
        assert len(rows) == 2
        # 按 l2_score 降序
        assert rows[0]["l2_score"] == 85
        assert rows[1]["l2_score"] == 78

    def test_filter_by_age_band(self, tmp_db):
        _seed_l2_lead("A", age="40s", gender="female",
                       is_jp=True, score=80)
        _seed_l2_lead("B", age="50s", gender="female",
                       is_jp=True, score=82)
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads(age_band="40s")
        names = [r["display_name"] for r in rows]
        assert names == ["A"], f"期望只返 40s, 实际: {names}"

    def test_filter_by_gender_and_is_japanese(self, tmp_db):
        _seed_l2_lead("JPF", age="40s", gender="female",
                       is_jp=True, score=80)
        _seed_l2_lead("USF", age="40s", gender="female",
                       is_jp=False, score=75)
        _seed_l2_lead("JPM", age="40s", gender="male",
                       is_jp=True, score=50)
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads(gender="female", is_japanese=True)
        names = [r["display_name"] for r in rows]
        assert names == ["JPF"], f"只留日文女性, 实际: {names}"

    def test_filter_by_min_score(self, tmp_db):
        _seed_l2_lead("hi", age="40s", gender="female",
                       is_jp=True, score=90)
        _seed_l2_lead("lo", age="40s", gender="female",
                       is_jp=True, score=60)
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads(min_score=70)
        assert [r["display_name"] for r in rows] == ["hi"]

    def test_filter_by_persona_key(self, tmp_db):
        _seed_l2_lead("JP", age="40s", gender="female",
                       is_jp=True, score=85,
                       persona="jp_female_midlife")
        _seed_l2_lead("IT", age="40s", gender="female",
                       is_jp=False, score=82,
                       persona="it_female_midlife")
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads(persona_key="it_female_midlife")
        assert [r["display_name"] for r in rows] == ["IT"]

    def test_limit_caps_results(self, tmp_db):
        for i in range(8):
            _seed_l2_lead(f"p{i}", age="40s", gender="female",
                          is_jp=True, score=70 + i)
        from src.host.lead_mesh import list_l2_verified_leads
        rows = list_l2_verified_leads(limit=3)
        assert len(rows) == 3

    def test_non_l2_verified_excluded(self, tmp_db):
        """没打 l2_verified tag 的普通 lead 不应返."""
        from src.host.lead_mesh import (resolve_identity,
                                          update_canonical_metadata,
                                          list_l2_verified_leads)
        cid = resolve_identity(platform="facebook",
                                account_id="fb:NoL2",
                                display_name="NoL2")
        update_canonical_metadata(cid, {"other_field": "x"},
                                    tags=["other_tag"])
        _seed_l2_lead("HasL2", age="40s", gender="female",
                       is_jp=True, score=80)
        rows = list_l2_verified_leads()
        names = [r["display_name"] for r in rows]
        assert names == ["HasL2"]


class TestL2VerifiedApi:
    @pytest.fixture
    def client(self, tmp_db):
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_api_empty(self, client):
        r = client.get("/lead-mesh/leads/l2-verified")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["count"] == 0
        assert data["results"] == []
        # Phase 12.4 起 API 多带 offset/limit echo (向后兼容增量字段)

    def test_api_returns_seeded(self, tmp_db, client):
        _seed_l2_lead("A花子", age="40s", gender="female",
                       is_jp=True, score=90)
        _seed_l2_lead("B美咲", age="50s", gender="female",
                       is_jp=True, score=70)
        r = client.get("/lead-mesh/leads/l2-verified")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        # 默认按 score 降序
        assert data["results"][0]["display_name"] == "A花子"
        assert data["results"][0]["l2_score"] == 90
        assert data["results"][0]["tags"], "tags 应非空"
        assert data["results"][0]["metadata"]["age_band"] == "40s"

    def test_api_filter_chain(self, tmp_db, client):
        _seed_l2_lead("JP_F40", age="40s", gender="female",
                       is_jp=True, score=88)
        _seed_l2_lead("JP_F50", age="50s", gender="female",
                       is_jp=True, score=75)
        _seed_l2_lead("JP_M40", age="40s", gender="male",
                       is_jp=True, score=40)
        r = client.get("/lead-mesh/leads/l2-verified",
                       params={"age_band": "40s", "gender": "female",
                               "is_japanese": True, "min_score": 80})
        assert r.status_code == 200
        data = r.json()
        names = [x["display_name"] for x in data["results"]]
        assert names == ["JP_F40"], f"filter chain 失效: {names}"

    def test_api_path_not_swallowed_as_canonical_id(self, tmp_db, client):
        """验证 /leads/l2-verified 不会被 /leads/{canonical_id} 当成 param 吃掉."""
        r = client.get("/lead-mesh/leads/l2-verified")
        assert r.status_code == 200
        # 200 + JSON schema 里有 count/results 即证明走对端点
        assert "count" in r.json()
        assert "results" in r.json()
