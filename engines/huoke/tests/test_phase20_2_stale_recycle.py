# -*- coding: utf-8 -*-
"""Phase 20.2 (2026-04-25): SLA 死信回收 — mark stale + escalate dead."""
from __future__ import annotations

import datetime as dt
import time

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    from src.host import executor as _ex
    _ex._REFERRAL_KEYWORDS_CACHE["data"] = None
    _ex._peer_region_cache_clear()
    yield


def _seed_l2_lead(name, persona="jp_female_midlife"):
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    update_canonical_metadata(cid, {
        "l2_score": 80, "l2_persona_key": persona,
    }, tags=["l2_verified"])
    return cid


def _seed_event_at(device, peer, etype, sent_at_str):
    """直接 INSERT 一个指定 at 的事件 (绕开 datetime('now'))."""
    from src.host.database import _connect
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fb_contact_events (device_id, peer_name,"
            " event_type, meta_json, at) VALUES (?, ?, ?, ?, ?)",
            (device, peer, etype, "{}", sent_at_str))
        return int(cur.lastrowid or 0)


# ═══════════════════════════════════════════════════════════════════
# 20.2.1: mark_stale_referrals
# ═══════════════════════════════════════════════════════════════════

class TestMarkStaleReferrals:
    def test_no_pending_returns_zero(self, tmp_db):
        from src.host.fb_store import mark_stale_referrals
        s = mark_stale_referrals(stale_hours=48)
        assert s["scanned"] == 0
        assert s["marked_stale"] == 0

    def test_recent_sent_not_stale(self, tmp_db):
        """sent 1h 前不超过 48h 阈值, 不该 mark."""
        from src.host.fb_store import mark_stale_referrals
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=1)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48)
        assert s["marked_stale"] == 0
        assert s["candidates"] == 0  # age 没到阈值不算 candidate

    def test_old_sent_marked_stale(self, tmp_db):
        """sent 50h 前 > 48h 阈值 → mark stale."""
        from src.host.fb_store import (mark_stale_referrals,
                                          count_contact_events)
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48,
                                    escalate_to_dead_days=7)
        assert s["marked_stale"] == 1
        assert s["escalated_dead"] == 0  # 50h < 7天
        # 应该写了 referral_stale event
        n = count_contact_events(event_type="referral_stale", hours=24)
        assert n == 1

    def test_replied_excluded(self, tmp_db):
        """已 replied 的不应被标 stale."""
        from src.host.fb_store import mark_stale_referrals
        _seed_l2_lead("花子")
        old_at = (dt.datetime.utcnow()
                   - dt.timedelta(hours=50)
                   ).strftime("%Y-%m-%d %H:%M:%S")
        _seed_event_at("D1", "花子", "wa_referral_sent", old_at)
        _seed_event_at("D1", "花子", "wa_referral_replied", old_at)
        s = mark_stale_referrals(stale_hours=48)
        assert s["marked_stale"] == 0

    def test_old_sent_escalates_to_dead(self, tmp_db):
        """sent 8 天前 → 同时打 stale + dead."""
        from src.host.fb_store import mark_stale_referrals
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(days=8)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48,
                                    escalate_to_dead_days=7)
        assert s["marked_stale"] == 1
        assert s["escalated_dead"] == 1
        # 验 canonical tag
        d = get_dossier(cid)
        tags_csv = (d.get("canonical", {}).get("tags") or "")
        tags = {t.strip() for t in tags_csv.split(",") if t.strip()}
        assert "referral_stale" in tags
        assert "referral_dead" in tags

    def test_dry_run_no_writes(self, tmp_db):
        from src.host.fb_store import (mark_stale_referrals,
                                          count_contact_events)
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s = mark_stale_referrals(stale_hours=48, dry_run=True)
        # 报告说 marked_stale=1 (统计目的) 但无实际写入
        assert s["dry_run"] is True
        assert s["marked_stale"] == 1
        # 没写 event
        n = count_contact_events(event_type="referral_stale", hours=24)
        assert n == 0
        # 没打 tag
        d = get_dossier(cid)
        assert "referral_stale" not in (d.get("tags") or [])

    def test_already_stale_skipped_for_remarking(self, tmp_db):
        """重复运行不应多次写 event 或重复 mark."""
        from src.host.fb_store import (mark_stale_referrals,
                                          count_contact_events)
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        s1 = mark_stale_referrals(stale_hours=48)
        assert s1["marked_stale"] == 1
        s2 = mark_stale_referrals(stale_hours=48)
        # 第 2 次跑: 已 tag stale, 走 already_stale 分支
        assert s2["marked_stale"] == 0
        assert s2["already_stale"] >= 1
        # 仅 1 个 stale event
        n = count_contact_events(event_type="referral_stale", hours=24)
        assert n == 1

    def test_already_stale_can_still_escalate(self, tmp_db):
        """已 stale 的 peer 后续达 7d → 升级 dead."""
        from src.host.fb_store import mark_stale_referrals
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(days=8)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        # 第 1 次跑 escalate_days=10 不会 dead
        s1 = mark_stale_referrals(stale_hours=48,
                                     escalate_to_dead_days=10)
        assert s1["marked_stale"] == 1
        assert s1["escalated_dead"] == 0
        # 第 2 次 escalate_days=7 → 应升级 dead (already_stale + escalate)
        s2 = mark_stale_referrals(stale_hours=48,
                                     escalate_to_dead_days=7)
        assert s2["already_stale"] >= 1
        assert s2["escalated_dead"] == 1
        d = get_dossier(cid)
        tags_csv = (d.get("canonical", {}).get("tags") or "")
        assert "referral_dead" in tags_csv

    def test_per_device_filter(self, tmp_db):
        from src.host.fb_store import mark_stale_referrals
        _seed_l2_lead("花子")
        _seed_l2_lead("Maria")
        old = (dt.datetime.utcnow()
                - dt.timedelta(hours=50)
                ).strftime("%Y-%m-%d %H:%M:%S")
        _seed_event_at("D1", "花子", "wa_referral_sent", old)
        _seed_event_at("D2", "Maria", "wa_referral_sent", old)
        s = mark_stale_referrals(stale_hours=48, device_id="D1")
        assert s["marked_stale"] == 1
        # D2 没动
        s2 = mark_stale_referrals(stale_hours=48, device_id="D2")
        assert s2["marked_stale"] == 1


# ═══════════════════════════════════════════════════════════════════
# 20.2.2: facebook_mark_stale_referrals task
# ═══════════════════════════════════════════════════════════════════

class TestStaleTask:
    def test_dispatch_through_executor(self, tmp_db):
        from src.host.executor import _fb_mark_stale_referrals
        _seed_l2_lead("花子")
        _seed_event_at("D1", "花子", "wa_referral_sent",
                          (dt.datetime.utcnow()
                            - dt.timedelta(hours=50)
                            ).strftime("%Y-%m-%d %H:%M:%S"))
        ok, _, stats = _fb_mark_stale_referrals({"stale_hours": 48})
        assert ok
        assert stats["marked_stale"] == 1


# ═══════════════════════════════════════════════════════════════════
# 20.2.3: funnel + daily summary 集成
# ═══════════════════════════════════════════════════════════════════

class TestFunnelStale:
    def test_funnel_includes_stale_field(self, tmp_db):
        from src.host.line_pool import referral_funnel
        f = referral_funnel(hours_window=24)
        assert "stale" in f
        assert f["stale"] == 0
        assert "stale_rate" in f

    def test_funnel_counts_stale_events(self, tmp_db):
        from src.host.line_pool import referral_funnel
        from src.host.fb_store import record_contact_event
        _seed_l2_lead("花子")
        record_contact_event("D1", "花子", "wa_referral_sent",
                               skip_sanitize=True)
        record_contact_event("D1", "花子", "referral_stale",
                               skip_sanitize=True)
        f = referral_funnel(hours_window=24)
        assert f["sent"] == 1
        assert f["stale"] == 1
        assert f["stale_rate"] == 1.0  # 100% sent 也 stale

    def test_daily_summary_funnel_has_stale(self, tmp_db, tmp_path,
                                                 monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        from src.host.fb_store import record_contact_event
        _seed_l2_lead("花子")
        record_contact_event("D1", "花子", "wa_referral_sent",
                               skip_sanitize=True)
        record_contact_event("D1", "花子", "referral_stale",
                               skip_sanitize=True)
        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": []})
        assert stats["summary"]["funnel"]["stale"] == 1


# ═══════════════════════════════════════════════════════════════════
# 20.2.x.1: 自动复活 referral_stale on wa_referral_replied
# ═══════════════════════════════════════════════════════════════════

class TestAutoReviveOnReply:
    def test_replied_clears_stale_tag(self, tmp_db):
        """seed stale tagged peer → 写 wa_referral_replied → tag 应被移除."""
        from src.host.lead_mesh import (update_canonical_metadata,
                                          get_dossier)
        from src.host.fb_store import record_contact_event
        cid = _seed_l2_lead("花子")
        update_canonical_metadata(cid, {"referral_stale_at": "2026-04-01T0:0:0Z"},
                                    tags=["referral_stale"])
        # confirm tag exists
        d = get_dossier(cid)
        assert "referral_stale" in (d["canonical"]["tags"] or "")
        # 写 wa_referral_replied (auto-revive trigger)
        record_contact_event("D1", "花子", "wa_referral_replied",
                               meta={"keyword_matched": "line"},
                               skip_sanitize=True)
        d2 = get_dossier(cid)
        assert "referral_stale" not in (d2["canonical"]["tags"] or "")
        # meta 应有 revived_at marker
        assert d2["canonical"]["metadata"].get("referral_stale_revived_at")

    def test_no_op_when_no_stale_tag(self, tmp_db):
        """非 stale 的 peer 写 replied 不应出错."""
        from src.host.fb_store import record_contact_event
        _seed_l2_lead("花子")
        # 没有 referral_stale tag
        eid = record_contact_event("D1", "花子", "wa_referral_replied",
                                      meta={"k": "v"},
                                      skip_sanitize=True)
        assert eid > 0  # 事件正常写入

    def test_unknown_peer_silent(self, tmp_db):
        """peer 不在 lead_identities 时 hook 应 silent 不报错."""
        from src.host.fb_store import record_contact_event
        eid = record_contact_event("D1", "ghost-xyz", "wa_referral_replied",
                                      skip_sanitize=True)
        assert eid > 0

    def test_other_event_types_dont_trigger_hook(self, tmp_db):
        """非 wa_referral_replied 不该调用 revive hook (省 SQL)."""
        from src.host.lead_mesh import (update_canonical_metadata,
                                          get_dossier)
        from src.host.fb_store import record_contact_event
        cid = _seed_l2_lead("花子")
        update_canonical_metadata(cid, {"referral_stale_at": "x"},
                                    tags=["referral_stale"])
        # 写其他类型, tag 应保持
        record_contact_event("D1", "花子", "wa_referral_sent",
                               skip_sanitize=True)
        d = get_dossier(cid)
        assert "referral_stale" in (d["canonical"]["tags"] or "")


# ═══════════════════════════════════════════════════════════════════
# 20.2.x.2: stale_rate_high alert
# ═══════════════════════════════════════════════════════════════════

class TestStaleRateHighAlert:
    def test_high_stale_triggers(self):
        from src.host.executor import _detect_referral_alerts
        # sent=12, stale=8 → 67% >= 50%
        funnel = {"planned": 12, "sent": 12, "replied": 0, "stale": 8,
                   "send_rate": 1.0}
        alerts = _detect_referral_alerts(funnel, 0)
        types = {a["type"] for a in alerts}
        assert "stale_rate_high" in types

    def test_low_stale_no_alert(self):
        from src.host.executor import _detect_referral_alerts
        # sent=12, stale=2 → 17% < 50%
        funnel = {"planned": 12, "sent": 12, "replied": 5, "stale": 2,
                   "send_rate": 1.0}
        alerts = _detect_referral_alerts(funnel, 0)
        types = {a["type"] for a in alerts}
        assert "stale_rate_high" not in types

    def test_too_few_sent_skipped(self):
        from src.host.executor import _detect_referral_alerts
        # sent=5 < min_sent=10
        funnel = {"planned": 5, "sent": 5, "replied": 0, "stale": 5,
                   "send_rate": 1.0}
        alerts = _detect_referral_alerts(funnel, 0)
        types = {a["type"] for a in alerts}
        assert "stale_rate_high" not in types

    def test_threshold_overridable(self):
        from src.host.executor import _detect_referral_alerts
        # 30% stale, default 50% 不触发, 阈值降到 20% 应触发
        funnel = {"planned": 10, "sent": 10, "replied": 0, "stale": 3,
                   "send_rate": 1.0}
        alerts = _detect_referral_alerts(
            funnel, 0, alert_stale_threshold=0.2)
        types = {a["type"] for a in alerts}
        assert "stale_rate_high" in types


# ═══════════════════════════════════════════════════════════════════
# 20.2.x.3: GET /line-pool/stats/stale-leads
# ═══════════════════════════════════════════════════════════════════

class TestStaleLeadsEndpoint:
    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from src.host.routers.line_pool import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_empty_returns_empty_list(self, tmp_db):
        c = self._client()
        r = c.get("/line-pool/stats/stale-leads")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["results"] == []

    def test_stale_lead_listed(self, tmp_db):
        from src.host.lead_mesh import update_canonical_metadata
        cid = _seed_l2_lead("花子")
        update_canonical_metadata(cid, {
            "referral_stale_at": "2026-04-23T05:00:00Z",
            "referral_stale_peer_name": "花子",
            "referral_stale_age_hours_when_marked": 50,
        }, tags=["referral_stale"])
        c = self._client()
        r = c.get("/line-pool/stats/stale-leads")
        body = r.json()
        assert body["total"] == 1
        assert body["results"][0]["peer_name"] == "花子"
        assert body["results"][0]["is_dead"] is False
        assert body["results"][0]["hours_since_stale"] is not None

    def test_include_dead_flag(self, tmp_db):
        from src.host.lead_mesh import update_canonical_metadata
        cid = _seed_l2_lead("花子")
        update_canonical_metadata(cid, {
            "referral_stale_at": "2026-04-23T05:00:00Z",
            "referral_dead_reason": "stale_no_reply",
        }, tags=["referral_stale", "referral_dead"])
        c = self._client()
        # default include_dead=true → 看到 dead 的
        r1 = c.get("/line-pool/stats/stale-leads")
        assert r1.json()["total"] == 1
        # include_dead=false → 看不到
        r2 = c.get("/line-pool/stats/stale-leads?include_dead=false")
        assert r2.json()["total"] == 0

    def test_pagination(self, tmp_db):
        from src.host.lead_mesh import update_canonical_metadata
        for i in range(5):
            cid = _seed_l2_lead(f"P{i}")
            update_canonical_metadata(cid, {"referral_stale_at": "x"},
                                        tags=["referral_stale"])
        c = self._client()
        r1 = c.get("/line-pool/stats/stale-leads?limit=2&offset=0")
        assert r1.json()["total"] == 5
        assert len(r1.json()["results"]) == 2
        r2 = c.get("/line-pool/stats/stale-leads?limit=2&offset=2")
        assert len(r2.json()["results"]) == 2
