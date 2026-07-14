# -*- coding: utf-8 -*-
"""Phase 13 (2026-04-25): referral funnel + account ranking + per-event 决策树."""
from __future__ import annotations

import time

import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def _fast_mode():
    with patch("random.uniform", return_value=0), \
         patch("time.sleep"):
        yield


def _seed_event(device_id, peer_name, event_type, preset_key=""):
    from src.host.fb_store import record_contact_event
    # Phase 16: skip_sanitize for fake peer names (Alice/Bob/A/B/C)
    return record_contact_event(device_id, peer_name, event_type,
                                  preset_key=preset_key,
                                  meta={"via": "test"},
                                  skip_sanitize=True)


def _seed_l2_lead(name: str, *, extra_tags=None, extra_meta=None) -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    meta = {
        "age_band": "40s", "gender": "female", "is_japanese": True,
        "l2_score": 80, "l2_persona_key": "jp_female_midlife",
        "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime()),
    }
    if extra_meta: meta.update(extra_meta)
    tags = ["l2_verified"]
    if extra_tags: tags += extra_tags
    update_canonical_metadata(cid, meta, tags=tags)
    return cid


# ═══════════════════════════════════════════════════════════════════
# referral_funnel
# ═══════════════════════════════════════════════════════════════════

class TestReferralFunnel:
    def test_empty_funnel_zeros(self, tmp_db):
        from src.host.line_pool import referral_funnel
        r = referral_funnel(hours_window=24)
        assert r["planned"] == 0
        assert r["sent"] == 0
        assert r["replied"] == 0
        assert r["send_rate"] == 0.0
        assert r["conversion_rate"] == 0.0

    def test_funnel_dedup_by_peer(self, tmp_db):
        """同一 peer 多次 planned 只算 1 个 planned peer."""
        from src.host.line_pool import referral_funnel
        _seed_event("DEV1", "Alice", "line_dispatch_planned")
        _seed_event("DEV1", "Alice", "line_dispatch_planned")  # dup
        _seed_event("DEV1", "Bob", "line_dispatch_planned")
        r = referral_funnel(hours_window=24)
        assert r["planned"] == 2  # Alice + Bob 去重后 2 个 peer
        assert r["raw_events"]["planned_events"] == 3  # 但原 events 3

    def test_funnel_4_stage_rates(self, tmp_db):
        from src.host.line_pool import referral_funnel
        # 3 planned, 2 sent, 1 replied
        for name in ["A", "B", "C"]:
            _seed_event("DEV1", name, "line_dispatch_planned")
        for name in ["A", "B"]:
            _seed_event("DEV1", name, "wa_referral_sent")
        _seed_event("DEV1", "A", "wa_referral_replied")
        r = referral_funnel(hours_window=24)
        assert r["planned"] == 3
        assert r["sent"] == 2
        assert r["replied"] == 1
        assert r["send_rate"] == round(2 / 3, 4)
        assert r["conversion_rate"] == round(1 / 2, 4)


# ═══════════════════════════════════════════════════════════════════
# account_ranking
# ═══════════════════════════════════════════════════════════════════

class TestAccountRanking:
    def test_empty_ranking(self, tmp_db):
        from src.host.line_pool import account_ranking
        assert account_ranking() == []

    def test_ranking_sorted_by_success_rate_desc(self, tmp_db):
        from src.host import line_pool as lp
        # 3 个账号, 人工构造 dispatch_log
        ids = [
            lp.add("@good", region="jp"),   # 10 sent / 0 failed → 100%
            lp.add("@mid", region="jp"),    # 2 sent / 3 failed → 40%
            lp.add("@bad", region="jp"),    # 1 sent / 9 failed → 10%
        ]
        from src.host.database import _connect
        with _connect() as c:
            for _ in range(10):
                c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                           " line_id, status) VALUES (?,?,'sent')",
                           (ids[0], "@good"))
            for _ in range(2):
                c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                           " line_id, status) VALUES (?,?,'sent')",
                           (ids[1], "@mid"))
            for _ in range(3):
                c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                           " line_id, status) VALUES (?,?,'failed')",
                           (ids[1], "@mid"))
            for _ in range(1):
                c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                           " line_id, status) VALUES (?,?,'sent')",
                           (ids[2], "@bad"))
            for _ in range(9):
                c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                           " line_id, status) VALUES (?,?,'failed')",
                           (ids[2], "@bad"))

        from src.host.line_pool import account_ranking
        rows = account_ranking(hours_window=24)
        line_ids = [r["line_id"] for r in rows]
        assert line_ids == ["@good", "@mid", "@bad"]
        assert rows[0]["success_rate"] == 1.0
        assert rows[1]["success_rate"] == 0.4
        assert rows[2]["success_rate"] == 0.1

    def test_ranking_skips_account_id_zero(self, tmp_db):
        """skipped log 占位 (account_id=0) 不纳入排名."""
        from src.host import line_pool as lp
        from src.host.database import _connect
        aid = lp.add("@x", region="jp")
        with _connect() as c:
            c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                       " line_id, status) VALUES (0,'','skipped')")
            c.execute("INSERT INTO line_dispatch_log (line_account_id,"
                       " line_id, status) VALUES (?,?,'sent')",
                       (aid, "@x"))
        from src.host.line_pool import account_ranking
        rows = account_ranking()
        assert len(rows) == 1
        assert rows[0]["line_id"] == "@x"


# ═══════════════════════════════════════════════════════════════════
# dispatcher verbose_dry_run per_event_decisions
# ═══════════════════════════════════════════════════════════════════

class TestDispatcherPerEventDecisions:
    def test_verbose_off_no_decisions_key(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        _seed_event("DEV1", "X", "greeting_replied")
        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "dry_run": True})
        assert "per_event_decisions" not in stats

    def test_verbose_on_decisions_listed(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host import line_pool as lp
        lp.add("along2026", region="jp", persona_key="jp_female_midlife")
        # 1 个合格 + 1 个非 l2
        _seed_l2_lead("合格")
        _seed_event("DEV1", "合格", "greeting_replied")
        _seed_event("DEV1", "非_l2_peer", "greeting_replied")

        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "region": "jp",
            "persona_key": "jp_female_midlife",
            "dry_run": True, "verbose_dry_run": True,
        })
        pe = stats.get("per_event_decisions")
        assert pe is not None
        assert len(pe) == 2
        decisions = {d["peer_name"]: d["decision"] for d in pe}
        assert decisions["合格"] == "dispatched"
        assert decisions["非_l2_peer"] == "skipped_not_l2_verified"

    def test_decision_skipped_referral_dead(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host import line_pool as lp
        from src.host.lead_mesh import update_canonical_metadata
        lp.add("along2026", region="jp", persona_key="jp_female_midlife")
        cid = _seed_l2_lead("Dead")
        update_canonical_metadata(cid,
            {"referral_dead_reason": "recipient_not_found"},
            tags=["referral_dead"])
        _seed_event("DEV1", "Dead", "greeting_replied")
        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "region": "jp",
            "persona_key": "jp_female_midlife",
            "dry_run": True, "verbose_dry_run": True,
        })
        pe = stats.get("per_event_decisions", [])
        assert any(d["decision"] == "skipped_referral_dead" for d in pe)


# ═══════════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════════

class TestPhase13Apis:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_funnel_api_empty(self, client):
        r = client.get("/line-pool/stats/referral-funnel?hours_window=24")
        assert r.status_code == 200
        d = r.json()
        assert d["planned"] == 0
        assert d["sent"] == 0
        assert "send_rate" in d

    def test_account_ranking_api_empty(self, client):
        r = client.get("/line-pool/stats/account-ranking")
        assert r.status_code == 200
        assert r.json()["results"] == []
