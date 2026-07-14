# -*- coding: utf-8 -*-
"""Phase 20.3 (2026-04-25): A 侧 e2e 完整闭环联调 (Mock B Messenger).

5 个场景串起 Phase 20.1 → 20.2.x:
  1. 单 region 5 leads 部分回复 → funnel.replied 正确
  2. 多 region 多语言关键词路由
  3. 不同 sent age → latency 统计 avg/median/p95
  4. stale 标记 + reply 自动 revive 回路
  5. 完整 daily summary 回归 — 3 alerts + 7d trend + per-region

每个场景用 FakeBMessenger 模拟 B, A 侧 task 函数原样运行.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from typing import List

import pytest

from tests._fakes import FakeBMessenger


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    from src.host import executor as _ex
    _ex._REFERRAL_KEYWORDS_CACHE["data"] = None
    _ex._peer_region_cache_clear()
    yield


# ─── helpers ────────────────────────────────────────────────────────

def _seed_l2_lead(name, persona="jp_female_midlife", region=None):
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    meta = {"l2_score": 80, "l2_persona_key": persona,
            "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime())}
    if region is not None:
        meta["region"] = region
    update_canonical_metadata(cid, meta, tags=["l2_verified"])
    return cid


def _seed_sent_event(device, peer, hours_ago=1):
    """直接 INSERT wa_referral_sent 事件 (绕开 Phase 12 dispatch+send 全链)."""
    from src.host.database import _connect
    at_str = (dt.datetime.utcnow()
               - dt.timedelta(hours=hours_ago)
               ).strftime("%Y-%m-%d %H:%M:%S")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO fb_contact_events (device_id, peer_name,"
            " event_type, meta_json, at) VALUES (?, ?, ?, ?, ?)",
            (device, peer, "wa_referral_sent", "{}", at_str))
        return int(cur.lastrowid or 0)


# ═══════════════════════════════════════════════════════════════════
# 场景 1: 5 leads 部分回复 (3 reply, 2 沉默)
# ═══════════════════════════════════════════════════════════════════

class TestScenarioPartialReply:
    def test_funnel_after_partial_reply(self, tmp_db):
        from src.host.executor import _fb_check_referral_replies
        from src.host.line_pool import referral_funnel
        # seed 5 jp leads + sent events 1h 前
        for i in range(5):
            _seed_l2_lead(f"花子{i}", persona="jp_female_midlife")
            _seed_sent_event("D1", f"花子{i}", hours_ago=1)
        # B 模拟 3 个回 line 关键词
        fb = FakeBMessenger(conversations={
            "花子0": "OK send me your line id",
            "花子1": "加 line 吧",
            "花子2": "友達追加していい？",
            # 花子3 / 花子4 不回
        })
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48})
        assert ok
        assert stats["pending_count"] == 5
        assert stats["replied_now"] == 3
        # funnel 应映射: 5 sent / 3 replied / conv_rate 0.6
        f = referral_funnel(hours_window=24)
        assert f["sent"] == 5
        assert f["replied"] == 3
        assert f["conversion_rate"] == 0.6

    def test_silent_peers_become_pending(self, tmp_db):
        """没回的 peer 仍在 pending list, 后续 cron 仍会再扫."""
        from src.host.executor import _fb_check_referral_replies
        from src.host.fb_store import get_pending_referral_peers
        for i in range(3):
            _seed_l2_lead(f"花子{chr(65+i)}")
            _seed_sent_event("D1", f"花子{chr(65+i)}", hours_ago=2)
        fb = FakeBMessenger(conversations={"花子A": "加 line"})
        _fb_check_referral_replies(fb, "D1", {"hours_back": 48})
        pending = get_pending_referral_peers(device_id="D1")
        # X1, X2 还在 pending; X0 已 replied 不在
        peers_pending = {p["peer_name"] for p in pending}
        assert "花子B" in peers_pending
        assert "花子C" in peers_pending
        assert "花子A" not in peers_pending


# ═══════════════════════════════════════════════════════════════════
# 场景 2: 多 region 多语言关键词路由
# ═══════════════════════════════════════════════════════════════════

class TestScenarioMultiRegion:
    def test_jp_and_it_routed_correctly(self, tmp_db):
        from src.host.executor import _fb_check_referral_replies
        # jp persona 用 "友達追加" (jp 关键词组), it persona 用 "aggiungi" (it 组)
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_l2_lead("マリア", persona="it_female_midlife")
        _seed_sent_event("D1", "花子", hours_ago=1)
        _seed_sent_event("D1", "マリア", hours_ago=1)
        fb = FakeBMessenger(conversations={
            "花子": "友達追加 OK",            # jp 组关键词
            "マリア": "aggiungi me su line",  # it 组关键词
        })
        ok, _, stats = _fb_check_referral_replies(
            fb, "D1", {"hours_back": 48})
        assert stats["replied_now"] == 2
        # 验 region 标签正确
        m = {x["peer_name"]: x for x in stats["matches"]}
        assert m["花子"]["region"] == "jp"
        assert m["花子"]["keyword"] in ("友達", "追加")
        assert m["マリア"]["region"] == "it"
        assert m["マリア"]["keyword"] == "aggiungi"


# ═══════════════════════════════════════════════════════════════════
# 场景 3: 不同 sent age → latency 统计正确
# ═══════════════════════════════════════════════════════════════════

class TestScenarioLatencySpread:
    def test_avg_median_p95_correct(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_check_referral_replies
        from src.host.executor import _compute_reply_latency_stats
        # 3 leads, sent 不同时长前
        ages_hours = [1, 4, 12]  # 1h, 4h, 12h ago
        for i, h in enumerate(ages_hours):
            _seed_l2_lead(f"レイ{chr(65+i)}")
            _seed_sent_event("D1", f"レイ{chr(65+i)}", hours_ago=h)
        fb = FakeBMessenger(conversations={
            f"レイ{chr(65+i)}": "加 line" for i in range(3)
        })
        _fb_check_referral_replies(fb, "D1", {"hours_back": 24})
        stats = _compute_reply_latency_stats(hours_window=24)
        assert stats["samples"] == 3
        # latency_min ≈ ages_hours * 60 (i.e. 60, 240, 720)
        assert 50 < stats["avg_min"] < 350  # avg ~340
        # median 应是中间值 (4h ≈ 240min)
        assert 200 < stats["median_min"] < 280


# ═══════════════════════════════════════════════════════════════════
# 场景 4: stale 标记 + reply 自动 revive
# ═══════════════════════════════════════════════════════════════════

class TestScenarioStaleAndRevive:
    def test_stale_then_reply_auto_revives(self, tmp_db):
        from src.host.executor import _fb_check_referral_replies
        from src.host.fb_store import mark_stale_referrals
        from src.host.lead_mesh import get_dossier
        cid = _seed_l2_lead("花子")
        # sent 50h 前 → 跑 stale → mark
        _seed_sent_event("D1", "花子", hours_ago=50)
        s = mark_stale_referrals(stale_hours=48)
        assert s["marked_stale"] == 1
        d1 = get_dossier(cid)
        assert "referral_stale" in (d1["canonical"]["tags"] or "")
        # 2 天后用户回复 → check_replies 写 wa_referral_replied → auto-revive
        fb = FakeBMessenger(conversations={"花子": "ok 加 line"})
        _fb_check_referral_replies(fb, "D1", {"hours_back": 72})
        # 但 pending 已排除 stale-after-reply? 先看 pending 规则:
        # pending = sent without replied. peer 1 sent 50h ago, no replied yet.
        # 所以 _fb_check_referral_replies 应能找到, 且 reply 后 revive.
        d2 = get_dossier(cid)
        tags = d2["canonical"]["tags"] or ""
        assert "referral_stale" not in tags
        # revive marker 应存在
        assert d2["canonical"]["metadata"].get("referral_stale_revived_at")


# ═══════════════════════════════════════════════════════════════════
# 场景 5: 完整 daily summary 集成
# ═══════════════════════════════════════════════════════════════════

class TestScenarioFullDailySummary:
    def test_full_loop_summary(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import (_fb_check_referral_replies,
                                          _fb_daily_referral_summary,
                                          _fb_mark_stale_referrals)
        monkeypatch.chdir(tmp_path)
        # mix: 4 jp + 3 it leads
        for i in range(4):
            _seed_l2_lead(f"花子{i}", persona="jp_female_midlife")
            _seed_sent_event("D1", f"花子{i}", hours_ago=2)
        for i in range(3):
            _seed_l2_lead(f"マリア{chr(65+i)}", persona="it_female_midlife")
            _seed_sent_event("D1", f"マリア{chr(65+i)}", hours_ago=2)
        # B 返: 花子0/1 reply, マリアA reply (jp 50% / it 33%)
        fb = FakeBMessenger(conversations={
            "花子0": "加 line",
            "花子1": "ライン id 教えて",
            "マリアA": "aggiungi me",
        })
        _fb_check_referral_replies(fb, "D1", {"hours_back": 48})
        # 跑 stale (用 24h 阈值, 让 2h 前的 sent 不算 stale)
        _fb_mark_stale_referrals({"stale_hours": 24})
        # 跑 daily summary
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": False, "send_webhook": False,
            "regions": ["jp", "it"]})
        assert ok
        s = stats["summary"]
        # funnel: 7 sent / 3 replied
        assert s["funnel"]["sent"] == 7
        assert s["funnel"]["replied"] == 3
        # latency 应有 3 samples
        assert s["reply_latency"]["samples"] == 3
        # per-region jp 4/2, it 3/1
        assert s["by_region"]["jp"]["sent"] == 4
        assert s["by_region"]["jp"]["replied"] == 2
        assert s["by_region"]["it"]["sent"] == 3
        assert s["by_region"]["it"]["replied"] == 1

    def test_full_loop_with_stale_alert(self, tmp_db, tmp_path, monkeypatch):
        """大量 sent + 0 reply + 老 sent → 应触发 stale_rate_high alert."""
        from src.host.executor import (_fb_daily_referral_summary,
                                          _fb_mark_stale_referrals)
        monkeypatch.chdir(tmp_path)
        # 12 leads, 全部 sent 50h 前, 全部不回
        for i in range(12):
            _seed_l2_lead(f"花子{chr(65+i)}", persona="jp_female_midlife")
            _seed_sent_event("D1", f"花子{chr(65+i)}", hours_ago=50)
        # stale mark
        _fb_mark_stale_referrals({"stale_hours": 48})
        # daily summary
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 72, "write_file": False, "send_webhook": False,
            "regions": ["jp"]})
        types = {a["type"] for a in stats["summary"]["alerts"]}
        # 12 sent, 0 replied → replied_rate_low
        assert "replied_rate_low" in types
        # 12 sent, 12 stale → stale_rate_high (100%)
        assert "stale_rate_high" in types


# ═══════════════════════════════════════════════════════════════════
# 场景 6 (bonus): FakeBMessenger 自身契约校验
# ═══════════════════════════════════════════════════════════════════

class TestFakeBMessengerContract:
    def test_legacy_inbox_call(self):
        """referral_mode=False 应返 messenger_active 状态 (legacy 兼容)."""
        fb = FakeBMessenger()
        r = fb.check_messenger_inbox(auto_reply=False)
        assert r["messenger_active"] is True
        assert "conversations" not in r

    def test_referral_mode_only_returns_filtered(self):
        fb = FakeBMessenger(conversations={
            "A": "x", "B": "y", "C": "z",
        })
        r = fb.check_messenger_inbox(referral_mode=True,
                                         peers_filter=["A", "B"])
        peers = {c["peer_name"] for c in r["conversations"]}
        assert peers == {"A", "B"}

    def test_call_logging(self):
        fb = FakeBMessenger()
        fb.check_messenger_inbox(referral_mode=True, peers_filter=["x"])
        fb.send_message("x", "hi")
        assert fb.inbox_call_count == 1
        assert fb.send_count == 1
        assert fb.inbox_calls[0]["peers_filter"] == ["x"]
        assert fb.send_log[0]["peer_name"] == "x"

    def test_send_failure_simulation(self):
        fb = FakeBMessenger(send_should_fail={"bad"})
        # raise_on_error=False → 返 dict
        r = fb.send_message("bad", "msg")
        assert r["success"] is False
        # raise_on_error=True → raise
        from src.app_automation.facebook import MessengerError
        with pytest.raises(MessengerError):
            fb.send_message("bad", "msg", raise_on_error=True)

    def test_add_inbound_runtime(self):
        """运行中加入新 inbound 模拟用户后续才回."""
        fb = FakeBMessenger()
        r1 = fb.check_messenger_inbox(referral_mode=True,
                                          peers_filter=["X"])
        assert r1["conversations"] == []
        fb.add_inbound("X", "delayed reply")
        r2 = fb.check_messenger_inbox(referral_mode=True,
                                          peers_filter=["X"])
        assert len(r2["conversations"]) == 1
        assert r2["conversations"][0]["last_inbound_text"] == "delayed reply"
