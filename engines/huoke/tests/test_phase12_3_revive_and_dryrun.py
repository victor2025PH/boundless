# -*- coding: utf-8 -*-
"""Phase 12.3 (2026-04-25): revive/untag + tags filter + dry_run + recycle 单测.

覆盖:
  * remove_canonical_tags 单项去 tag
  * revive_referral 清 dead tag + counter + reason
  * dispatcher include_tags / exclude_tags / dry_run (不 allocate 不写 log)
  * send_referral_replies dry_run (不调 send_message)
  * facebook_recycle_dead_peers 按 days 阈值复活
"""
from __future__ import annotations

import datetime as _dt
import json
import time

import pytest
from unittest.mock import MagicMock, patch


def _iso_days_ago(days: int) -> str:
    """生成 N 天前的 UTC ISO 时间戳, 替代硬编码日期防 date-rot."""
    return (_dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _fast_mode():
    with patch("random.uniform", return_value=0), \
         patch("time.sleep"):
        yield


def _seed_l2_lead(name: str, *, persona: str = "jp_female_midlife",
                   extra_tags=None, extra_meta=None) -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    meta = {
        "age_band": "40s", "gender": "female",
        "is_japanese": True, "l2_score": 85,
        "l2_persona_key": persona,
        "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
    }
    if extra_meta:
        meta.update(extra_meta)
    tags = ["l2_verified", "age:40s", "gender:female", "is_japanese"]
    if extra_tags:
        tags += extra_tags
    update_canonical_metadata(cid, meta, tags=tags)
    return cid


def _seed_reply_event(device_id, peer_name, event_type="greeting_replied"):
    from src.host.fb_store import record_contact_event
    # Phase 16: skip_sanitize for fake peer names in tests
    return record_contact_event(device_id, peer_name, event_type,
                                  meta={"via": "test"},
                                  skip_sanitize=True)


# ═══════════════════════════════════════════════════════════════════
# remove_canonical_tags + revive_referral
# ═══════════════════════════════════════════════════════════════════

class TestRemoveTagsAndRevive:
    def test_remove_tag_basic(self, tmp_db):
        from src.host.lead_mesh import remove_canonical_tags
        cid = _seed_l2_lead("X", extra_tags=["custom_a", "custom_b"])
        ok = remove_canonical_tags(cid, ["custom_a"])
        assert ok is True
        from src.host.lead_mesh.canonical import _connect
        with _connect() as c:
            row = c.execute(
                "SELECT tags FROM leads_canonical WHERE canonical_id=?",
                (cid,)).fetchone()
        tags = {t.strip() for t in row["tags"].split(",") if t.strip()}
        assert "custom_a" not in tags
        assert "custom_b" in tags
        assert "l2_verified" in tags

    def test_remove_tag_noop_when_not_present(self, tmp_db):
        from src.host.lead_mesh import remove_canonical_tags
        cid = _seed_l2_lead("Y")
        assert remove_canonical_tags(cid, ["nonexistent"]) is False

    def test_revive_referral_cleans_metadata_and_tag(self, tmp_db):
        from src.host.lead_mesh import revive_referral
        from src.host.lead_mesh.canonical import _connect
        cid = _seed_l2_lead("Dead",
            extra_tags=["referral_dead"],
            extra_meta={
                "referral_dead_reason": "recipient_not_found",
                "referral_dead_at": "2026-04-20T00:00:00Z",
                "referral_dead_peer_name": "Dead",
                "referral_fail_count_recipient_not_found": 1,
                "referral_fail_count_send_blocked_by_content": 2,
                "age_band": "40s",  # 非 referral 字段应保留
            })
        ok = revive_referral(cid)
        assert ok is True
        with _connect() as c:
            row = c.execute(
                "SELECT tags, metadata_json FROM leads_canonical"
                " WHERE canonical_id=?", (cid,)).fetchone()
        tags = row["tags"] or ""
        meta = json.loads(row["metadata_json"])
        assert "referral_dead" not in tags
        assert "referral_dead_reason" not in meta
        assert "referral_dead_at" not in meta
        assert "referral_fail_count_recipient_not_found" not in meta
        assert "referral_fail_count_send_blocked_by_content" not in meta
        # 其它字段保留
        assert meta.get("age_band") == "40s"
        assert "l2_verified" in tags

    def test_revive_noop_when_not_dead(self, tmp_db):
        from src.host.lead_mesh import revive_referral
        cid = _seed_l2_lead("Alive")
        assert revive_referral(cid) is False


# ═══════════════════════════════════════════════════════════════════
# dispatcher include/exclude_tags + dry_run
# ═══════════════════════════════════════════════════════════════════

class TestDispatcherTagsAndDryRun:
    def test_include_tags_and_filter(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host import line_pool as lp
        lp.add("along2026", region="jp", persona_key="jp_female_midlife")
        _seed_l2_lead("带 VIP", extra_tags=["vip"])
        _seed_l2_lead("普通")
        _seed_reply_event("DEV1", "带 VIP")
        _seed_reply_event("DEV1", "普通")

        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "region": "jp",
            "persona_key": "jp_female_midlife",
            "include_tags": ["vip"],  # 只要含 vip 的
        })
        names = [d["peer_name"] for d in stats["dispatches"]]
        assert names == ["带 VIP"]

    def test_exclude_tags_any_hit(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host import line_pool as lp
        lp.add("along2026", region="jp", persona_key="jp_female_midlife")
        _seed_l2_lead("有 flag", extra_tags=["skip_me"])
        _seed_l2_lead("干净")
        _seed_reply_event("DEV1", "有 flag")
        _seed_reply_event("DEV1", "干净")
        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "region": "jp",
            "persona_key": "jp_female_midlife",
            "exclude_tags": ["skip_me"],
        })
        names = [d["peer_name"] for d in stats["dispatches"]]
        assert names == ["干净"]

    def test_dry_run_does_not_allocate_or_log(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host import line_pool as lp
        lp.add("along2026", region="jp", persona_key="jp_female_midlife",
                daily_cap=10)
        _seed_l2_lead("预览")
        _seed_reply_event("DEV1", "预览")

        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "region": "jp",
            "persona_key": "jp_female_midlife",
            "dry_run": True,
        })
        assert stats["dry_run"] is True
        assert stats["dispatched"] == 1
        # account times_used 未被改 (dry_run 不 allocate)
        acc = lp.get_by_id(1)
        assert acc["times_used"] == 0
        # log 应为空 (或只有 skipped 因 no_match 但本 case 有 match)
        log = lp.recent_dispatch_log()
        assert len(log) == 0


# ═══════════════════════════════════════════════════════════════════
# send_referral_replies dry_run
# ═══════════════════════════════════════════════════════════════════

class TestSendReferralDryRun:
    def test_dry_run_does_not_call_send_message(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        from src.host.fb_store import record_contact_event

        aid = lp.add("along2026", region="jp")
        cid = _seed_l2_lead("XY")
        # 手动 seed 一条 line_dispatch_planned event
        record_contact_event("DEV1", "XY", "line_dispatch_planned",
            preset_key=f"line_pool:{aid}",
            meta={"line_id": "along2026", "line_account_id": aid,
                  "dispatch_mode": "messenger_text",
                  "message_template": "LINE: along2026",
                  "canonical_id": cid,
                  "original_device_id": "DEV1"})

        fb = MagicMock()
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "dry_run": True,
            "min_interval_sec": 0, "max_interval_sec": 0})
        fb.send_message.assert_not_called()
        assert stats["dry_run"] is True
        assert len(stats["outcomes"]) == 1
        o = stats["outcomes"][0]
        assert o["err_code"] == "dry_run"
        assert "along2026" in (o["would_send_template"] or "")


# ═══════════════════════════════════════════════════════════════════
# facebook_recycle_dead_peers
# ═══════════════════════════════════════════════════════════════════

class TestRecycleDeadPeers:
    def test_recycle_old_dead_peer(self, tmp_db):
        from src.host.executor import _line_pool_recycle_dead_peers
        # 老 dead (11 天前) — 应被复活 (>= 7 天阈值)
        _seed_l2_lead("OldDead",
            extra_tags=["referral_dead"],
            extra_meta={
                "referral_dead_reason": "recipient_not_found",
                "referral_dead_at": _iso_days_ago(11),
                "referral_fail_count_recipient_not_found": 1,
            })
        # 新 dead (1 天前) — 不该动 (< 7 天阈值)
        _seed_l2_lead("NewDead",
            extra_tags=["referral_dead"],
            extra_meta={
                "referral_dead_reason": "recipient_not_found",
                "referral_dead_at": _iso_days_ago(1),
                "referral_fail_count_recipient_not_found": 1,
            })
        ok, _m, stats = _line_pool_recycle_dead_peers({"days": 7})
        assert ok
        assert stats["revived"] == 1
        assert stats["skipped_young"] == 1

    def test_recycle_dry_run_does_not_revive(self, tmp_db):
        from src.host.executor import _line_pool_recycle_dead_peers
        cid = _seed_l2_lead("DryDead",
            extra_tags=["referral_dead"],
            extra_meta={
                "referral_dead_reason": "recipient_not_found",
                "referral_dead_at": _iso_days_ago(11),  # >= 7 天阈值, 应被 revive
            })
        _ok, _m, stats = _line_pool_recycle_dead_peers(
            {"days": 7, "dry_run": True})
        assert stats["revived"] == 1
        # 但实际 tags 应该没动
        from src.host.lead_mesh.canonical import _connect
        with _connect() as c:
            row = c.execute(
                "SELECT tags FROM leads_canonical WHERE canonical_id=?",
                (cid,)).fetchone()
        assert "referral_dead" in (row["tags"] or "")

    def test_recycle_no_dead_at_meta_still_revives(self, tmp_db):
        """老数据没 dead_at 字段 → 保守复活 (避免死掉的旧数据永远排除)."""
        from src.host.executor import _line_pool_recycle_dead_peers
        _seed_l2_lead("NoDate",
            extra_tags=["referral_dead"],
            extra_meta={"referral_dead_reason": "recipient_not_found"},
        )
        _ok, _m, stats = _line_pool_recycle_dead_peers({"days": 7})
        assert stats["revived"] == 1
