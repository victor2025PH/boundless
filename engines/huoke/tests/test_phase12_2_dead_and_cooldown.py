# -*- coding: utf-8 -*-
"""Phase 12.2 (2026-04-25): 失败去重 + 成功 metadata + cooldown 单测.

覆盖:
  * LINE 账号封号 auto cooldown: 最近 5 条 fail 比例 ≥ 80% 触发
  * cooldown 触发边界: < MIN_SAMPLES 不触发, 非 active 账号不触发
  * send 成功 → canonical.metadata.line_referred_at + tag line_referred
  * PERMANENT recipient_not_found 1 次即 referral_dead
  * PERMANENT send_blocked_by_content 1 次不到阈值, 2 次 dead
  * risk_detected / xspace_blocked 不计 peer (device/global 错)
  * dispatcher 过滤 referral_dead tag peer
  * list_l2_verified_leads include/exclude tags
"""
from __future__ import annotations

import json
import time

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def _fast_mode():
    with patch("random.uniform", return_value=0), \
         patch("time.sleep") as _slp:
        yield _slp


def _seed_l2_lead(name: str, *, age: str = "40s", gender: str = "female",
                   is_jp: bool = True, score: float = 85,
                   persona: str = "jp_female_midlife") -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
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


def _seed_planned_event(device_id: str, peer_name: str, *,
                         line_id: str = "along2026",
                         line_account_id: int = 1,
                         canonical_id: str = "",
                         original_device_id: str = "") -> int:
    from src.host.fb_store import record_contact_event
    # Phase 16: 测试 fake peer 名 (Alice/U0...) 走 skip_sanitize bypass
    return record_contact_event(
        device_id, peer_name, "line_dispatch_planned",
        preset_key=f"line_pool:{line_account_id}",
        meta={"line_id": line_id, "line_account_id": line_account_id,
              "dispatch_mode": "messenger_text",
              "message_template": f"LINE: {line_id}",
              "canonical_id": canonical_id,
              "original_device_id": original_device_id or device_id},
        skip_sanitize=True)


# ═══════════════════════════════════════════════════════════════════
# LINE 账号 auto cooldown
# ═══════════════════════════════════════════════════════════════════

class TestAutoCooldown:
    def test_4_of_5_failed_triggers_cooldown(self, tmp_db):
        """5 条里 4 条 failed (80%) → 账号自动 cooldown."""
        from src.host import line_pool as lp
        aid = lp.add("@tst", region="jp", daily_cap=20)
        # 凑 5 条: 4 failed + 1 sent
        for i in range(4):
            lp.allocate(region="jp", canonical_id=f"c_fail_{i}")
            lp.mark_dispatch_outcome(aid, status="failed", note="x")
        lp.allocate(region="jp", canonical_id="c_sent")
        lp.mark_dispatch_outcome(aid, status="sent", note="ok")
        # 最后再 1 条 failed, 总共近 5 条 = 4 failed + 1 sent = 80% → trigger
        lp.allocate(region="jp", canonical_id="c_trigger")
        lp.mark_dispatch_outcome(aid, status="failed", note="boom")
        fresh = lp.get_by_id(aid)
        assert fresh["status"] == "cooldown", \
            f"期望 cooldown, 实际: {fresh['status']}"
        assert "auto:fail_rate_high" in (fresh["notes"] or "")

    def test_below_min_samples_no_cooldown(self, tmp_db):
        """只有 2 条 failed (低于 MIN_SAMPLES=3) → 不触发."""
        from src.host import line_pool as lp
        aid = lp.add("@safe", region="jp")
        for i in range(2):
            lp.allocate(region="jp", canonical_id=f"c{i}")
            lp.mark_dispatch_outcome(aid, status="failed", note="x")
        fresh = lp.get_by_id(aid)
        assert fresh["status"] == "active"

    def test_below_ratio_no_cooldown(self, tmp_db):
        """5 条里 2 failed + 3 sent (40% < 80%) → 不触发."""
        from src.host import line_pool as lp
        aid = lp.add("@ok", region="jp")
        for _ in range(3):
            lp.allocate(region="jp", canonical_id="csent")
            lp.mark_dispatch_outcome(aid, status="sent", note="")
        for _ in range(2):
            lp.allocate(region="jp", canonical_id="cfail")
            lp.mark_dispatch_outcome(aid, status="failed", note="")
        fresh = lp.get_by_id(aid)
        assert fresh["status"] == "active"

    def test_non_active_account_not_affected(self, tmp_db):
        """账号已 disabled → failed 不改它."""
        from src.host import line_pool as lp
        aid = lp.add("@off", region="jp", status="disabled")
        for _ in range(5):
            # allocate 过滤 status=active, 这里手动插 log 模拟.
            from src.host.database import _connect
            with _connect() as c:
                c.execute(
                    "INSERT INTO line_dispatch_log (line_account_id, line_id,"
                    " status) VALUES (?,?,'planned')",
                    (aid, "@off"),
                )
            lp.mark_dispatch_outcome(aid, status="failed", note="x")
        fresh = lp.get_by_id(aid)
        assert fresh["status"] == "disabled", "non-active 不应改变"


# ═══════════════════════════════════════════════════════════════════
# send 成功 → canonical metadata + line_referred tag
# ═══════════════════════════════════════════════════════════════════

class TestSuccessMetadataWriteback:
    def test_success_writes_line_referred_tag(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        from src.host.lead_mesh.canonical import _connect as _lm
        aid = lp.add("along2026", region="jp")
        cid = _seed_l2_lead("花子")
        _seed_planned_event("DEV1", "花子", line_account_id=aid,
                             canonical_id=cid, original_device_id="DEV1")
        fb = MagicMock(); fb.send_message.return_value = True
        ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "min_interval_sec": 0,
            "max_interval_sec": 0, "max_retry": 0})
        assert stats["sent"] == 1
        # 验证 canonical metadata
        with _lm() as conn:
            row = conn.execute(
                "SELECT tags, metadata_json FROM leads_canonical"
                " WHERE canonical_id=?", (cid,)).fetchone()
        assert row is not None
        tags = [t.strip() for t in row["tags"].split(",") if t.strip()]
        assert "line_referred" in tags
        meta = json.loads(row["metadata_json"])
        assert meta.get("line_id") == "along2026"
        assert meta.get("line_referred_at")
        assert meta.get("line_account_id") == aid


# ═══════════════════════════════════════════════════════════════════
# peer 失败 → referral_dead tag
# ═══════════════════════════════════════════════════════════════════

class TestReferralDeadTag:
    def _make_fb_raising(self, code: str):
        from src.app_automation.facebook import MessengerError
        fb = MagicMock()
        fb.send_message.side_effect = MessengerError(code, f"{code} error")
        return fb

    def test_recipient_not_found_one_strike_dead(self, tmp_db):
        """recipient_not_found 1 次即 referral_dead (阈值=1)."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        from src.host.lead_mesh.canonical import _connect as _lm
        aid = lp.add("along2026", region="jp")
        cid = _seed_l2_lead("失联")
        _seed_planned_event("DEV1", "失联", line_account_id=aid,
                             canonical_id=cid, original_device_id="DEV1")
        fb = self._make_fb_raising("recipient_not_found")
        _ok, _m, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 24, "max_retry": 0,
            "min_interval_sec": 0, "max_interval_sec": 0})
        assert stats["failed"] == 1
        assert stats["outcomes"][0]["became_dead"] is True
        # metadata 验证
        with _lm() as conn:
            row = conn.execute(
                "SELECT tags, metadata_json FROM leads_canonical"
                " WHERE canonical_id=?", (cid,)).fetchone()
        tags = [t.strip() for t in row["tags"].split(",") if t.strip()]
        assert "referral_dead" in tags
        meta = json.loads(row["metadata_json"])
        assert meta.get("referral_dead_reason") == "recipient_not_found"
        assert meta.get("referral_fail_count_recipient_not_found") == 1

    def test_send_blocked_2_strikes_dead(self, tmp_db):
        """send_blocked_by_content 阈值=2, 1 次不到, 2 次 dead."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        from src.host.lead_mesh.canonical import _connect as _lm
        aid = lp.add("along2026", region="jp")
        cid = _seed_l2_lead("内容锁")
        _seed_planned_event("DEV1", "内容锁", line_account_id=aid,
                             canonical_id=cid, original_device_id="DEV1")
        fb = self._make_fb_raising("send_blocked_by_content")
        # 第 1 次
        _fb_send_referral_replies(fb, "DEV1", {"hours_window": 24,
            "max_retry": 0, "min_interval_sec": 0, "max_interval_sec": 0})
        # 第 1 次后不应 dead (阈值 2)
        with _lm() as conn:
            tags1 = (conn.execute(
                "SELECT tags FROM leads_canonical WHERE canonical_id=?",
                (cid,)).fetchone() or {"tags": ""})["tags"] or ""
        assert "referral_dead" not in tags1

        # 第 2 次 planned + send → 触发 dead
        _seed_planned_event("DEV1", "内容锁", line_account_id=aid,
                             canonical_id=cid, original_device_id="DEV1")
        _fb_send_referral_replies(fb, "DEV1", {"hours_window": 24,
            "max_retry": 0, "min_interval_sec": 0, "max_interval_sec": 0,
            "dedupe_hours": 0})  # 关去重让第 2 条能跑
        with _lm() as conn:
            row = conn.execute(
                "SELECT tags, metadata_json FROM leads_canonical"
                " WHERE canonical_id=?", (cid,)).fetchone()
        tags2 = [t.strip() for t in row["tags"].split(",") if t.strip()]
        assert "referral_dead" in tags2
        meta = json.loads(row["metadata_json"])
        assert meta.get("referral_fail_count_send_blocked_by_content") == 2

    def test_risk_detected_not_counted_as_peer_fail(self, tmp_db):
        """risk_detected 是全局/设备问题, 不算 peer 账."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        from src.host.lead_mesh.canonical import _connect as _lm
        aid = lp.add("along2026", region="jp")
        cid = _seed_l2_lead("Lucky")
        _seed_planned_event("DEV1", "Lucky", line_account_id=aid,
                             canonical_id=cid, original_device_id="DEV1")
        fb = self._make_fb_raising("risk_detected")
        _fb_send_referral_replies(fb, "DEV1", {"hours_window": 24,
            "max_retry": 0, "min_interval_sec": 0, "max_interval_sec": 0})
        # 不应 dead
        with _lm() as conn:
            row = conn.execute(
                "SELECT tags, metadata_json FROM leads_canonical"
                " WHERE canonical_id=?", (cid,)).fetchone()
        tags = row["tags"] or ""
        meta = json.loads(row["metadata_json"])
        assert "referral_dead" not in tags
        # 也不应有 recipient_not_found/send_blocked 计数
        assert "referral_fail_count_risk_detected" not in meta


# ═══════════════════════════════════════════════════════════════════
# dispatcher 过滤 referral_dead
# ═══════════════════════════════════════════════════════════════════

class TestDispatcherSkipsReferralDead:
    def test_dead_peer_filtered_out(self, tmp_db):
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host import line_pool as lp
        from src.host.lead_mesh import (update_canonical_metadata,
                                         resolve_identity)
        from src.host.fb_store import record_contact_event
        lp.add("along2026", region="jp", persona_key="jp_female_midlife")
        # lead A 正常
        _seed_l2_lead("正常")
        record_contact_event("DEV1", "正常", "greeting_replied",
                              meta={"via": "test"})
        # lead B 标 dead
        cid_dead = _seed_l2_lead("已死")
        update_canonical_metadata(cid_dead,
            {"referral_dead_reason": "recipient_not_found"},
            tags=["referral_dead"])
        record_contact_event("DEV1", "已死", "greeting_replied",
                              meta={"via": "test"})
        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 24, "region": "jp",
            "persona_key": "jp_female_midlife"})
        names = [d["peer_name"] for d in stats["dispatches"]]
        assert "正常" in names
        assert "已死" not in names
        assert stats["filtered_out"] >= 1


# ═══════════════════════════════════════════════════════════════════
# list_l2_verified_leads API tags filter
# ═══════════════════════════════════════════════════════════════════

class TestL2VerifiedTagsFilter:
    def test_include_line_referred(self, tmp_db):
        from src.host.lead_mesh import (list_l2_verified_leads,
                                         update_canonical_metadata)
        cid_r = _seed_l2_lead("已引流")
        update_canonical_metadata(cid_r, {"line_referred_at": "now"},
                                    tags=["line_referred"])
        _seed_l2_lead("未引流")
        rows = list_l2_verified_leads(include_tags=["line_referred"])
        names = [r["display_name"] for r in rows]
        assert names == ["已引流"]

    def test_exclude_referral_dead(self, tmp_db):
        from src.host.lead_mesh import (list_l2_verified_leads,
                                         update_canonical_metadata)
        cid_d = _seed_l2_lead("DeadOne")
        update_canonical_metadata(cid_d, {"referral_dead_reason": "x"},
                                    tags=["referral_dead"])
        _seed_l2_lead("AliveOne")
        rows = list_l2_verified_leads(exclude_tags=["referral_dead"])
        names = [r["display_name"] for r in rows]
        assert names == ["AliveOne"]
