# -*- coding: utf-8 -*-
"""P3 (2026-08-15) 召回体检: 判词/渲染纯函数 + record/summary 落库聚合。"""
from __future__ import annotations

from src.host.fb_store import _RECALL_INT_FIELDS
from src.host.messenger_recall_report import (
    format_text_report,
    recall_verdicts,
)


def _mk_summary(runs=50, rescued=0, rates=None, **totals_over):
    totals = {k: 0 for k in _RECALL_INT_FIELDS}
    totals.update(totals_over)
    return {
        "runs": runs,
        "days": 7,
        "device_id": "",
        "totals": totals,
        "rescued": rescued,
        "rates": rates or {
            "rescue_rate": 0.0, "search_mismatch_rate": 0.0,
            "title_mismatch_rate": 0.0, "empty_rate": 0.0,
        },
    }


# ─── 判词 ────────────────────────────────────────────────────────────
class TestVerdicts:
    def test_insufficient_sample_single_verdict(self):
        v = recall_verdicts(_mk_summary(runs=10))
        assert len(v) == 1
        assert v[0]["level"] == "info"
        assert "样本不足" in v[0]["text"]

    def test_high_rescue_warns(self):
        s = _mk_summary(runs=50, unread_processed=100, rescued=40,
                        notif_forced=25, preview_forced=10, search_opened=5,
                        rates={"rescue_rate": 0.40, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.0})
        texts = " ".join(x["text"] for x in v_warn(s))
        assert "召回不足" in texts

    def test_low_rescue_ok(self):
        s = _mk_summary(runs=50, unread_processed=100, rescued=8,
                        notif_forced=8,
                        rates={"rescue_rate": 0.08, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.0})
        levels = {x["level"] for x in recall_verdicts(s)}
        texts = " ".join(x["text"] for x in recall_verdicts(s))
        assert "召回健康" in texts
        assert "ok" in levels

    def test_high_search_mismatch_warns(self):
        s = _mk_summary(runs=50, search_opened=20, search_mismatch=10,
                        rates={"rescue_rate": 0.0, "search_mismatch_rate": 0.33,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.0})
        assert any("误点" in x["text"] and x["level"] == "warn"
                   for x in recall_verdicts(s))

    def test_high_title_mismatch_warns(self):
        s = _mk_summary(runs=50, unread_processed=100, title_mismatch=15,
                        rates={"rescue_rate": 0.0, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.15, "empty_rate": 0.0})
        assert any("身份错乱" in x["text"] and x["level"] == "warn"
                   for x in recall_verdicts(s))

    def test_high_empty_warns(self):
        s = _mk_summary(runs=50, unread_processed=100, empty_extract=30,
                        rates={"rescue_rate": 0.0, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.30})
        assert any("空读" in x["text"] and x["level"] == "warn"
                   for x in recall_verdicts(s))

    def test_offscreen_info(self):
        s = _mk_summary(runs=50, notif_offscreen=5, search_opened=2,
                        rates={"rescue_rate": 0.0, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.0})
        assert any("屏外" in x["text"] for x in recall_verdicts(s))

    def test_all_healthy_has_ok(self):
        s = _mk_summary(runs=50, unread_processed=100,
                        rates={"rescue_rate": 0.05, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.0})
        assert any(x["level"] == "ok" for x in recall_verdicts(s))


def v_warn(s):
    return [x for x in recall_verdicts(s) if x["level"] == "warn"]


# ─── 渲染 ────────────────────────────────────────────────────────────
class TestFormat:
    def test_contains_key_sections(self):
        s = _mk_summary(runs=30, unread_processed=50, conversations_listed=80,
                        notif_forced=5, search_opened=3, rescued=8,
                        rates={"rescue_rate": 0.16, "search_mismatch_rate": 0.0,
                               "title_mismatch_rate": 0.0, "empty_rate": 0.0})
        txt = format_text_report(s)
        assert "召回体检" in txt
        assert "救回合计" in txt
        assert "结论" in txt
        assert "80" in txt  # conversations_listed

    def test_insufficient_sample_render(self):
        txt = format_text_report(_mk_summary(runs=3))
        assert "样本不足" in txt


# ─── record / summary 落库聚合 (tmp_db) ──────────────────────────────
class TestRecordSummary:
    def test_record_and_summary_totals(self, tmp_db):
        from src.host.fb_store import (messenger_recall_summary,
                                       record_messenger_recall_run)
        stats = {"conversations_listed": 10, "unread_detected": 4,
                 "unread_processed": 5, "notif_forced": 2, "preview_forced": 1,
                 "search_opened": 1, "search_mismatch": 1, "title_mismatch": 1,
                 "empty_extract": 1, "notif_offscreen": 3, "phase": "growth"}
        record_messenger_recall_run("devA", stats, preset_key="jp")
        s = messenger_recall_summary(days=7)
        assert s["runs"] == 1
        assert s["totals"]["conversations_listed"] == 10
        assert s["totals"]["unread_processed"] == 5
        assert s["rescued"] == 4  # 2+1+1

    def test_summary_sums_multiple_runs(self, tmp_db):
        from src.host.fb_store import (messenger_recall_summary,
                                       record_messenger_recall_run)
        for _ in range(3):
            record_messenger_recall_run(
                "devA", {"unread_processed": 10, "notif_forced": 2})
        s = messenger_recall_summary(days=7)
        assert s["runs"] == 3
        assert s["totals"]["unread_processed"] == 30
        assert s["totals"]["notif_forced"] == 6

    def test_rates_computed(self, tmp_db):
        from src.host.fb_store import (messenger_recall_summary,
                                       record_messenger_recall_run)
        record_messenger_recall_run(
            "devA", {"unread_processed": 100, "notif_forced": 20,
                     "preview_forced": 10, "search_opened": 10,
                     "search_mismatch": 10, "title_mismatch": 5,
                     "empty_extract": 25})
        s = messenger_recall_summary(days=7)
        assert s["rates"]["rescue_rate"] == 0.4          # 40/100
        assert s["rates"]["search_mismatch_rate"] == 0.5  # 10/(10+10)
        assert s["rates"]["title_mismatch_rate"] == 0.05  # 5/100
        assert s["rates"]["empty_rate"] == 0.25          # 25/100

    def test_device_filter(self, tmp_db):
        from src.host.fb_store import (messenger_recall_summary,
                                       record_messenger_recall_run)
        record_messenger_recall_run("devA", {"unread_processed": 5})
        record_messenger_recall_run("devB", {"unread_processed": 7})
        assert messenger_recall_summary(days=7, device_id="devA")[
            "totals"]["unread_processed"] == 5
        assert messenger_recall_summary(days=7, device_id="devB")[
            "totals"]["unread_processed"] == 7
        assert messenger_recall_summary(days=7)[
            "totals"]["unread_processed"] == 12

    def test_window_excludes_old(self, tmp_db):
        import datetime as _dt
        from src.host.database import _connect
        from src.host.fb_store import (messenger_recall_summary,
                                       record_messenger_recall_run)
        record_messenger_recall_run("devA", {"unread_processed": 5})
        # 手工把该行 run_at 倒退 30 天 → 落在 7 天窗外
        old = (_dt.datetime.utcnow() - _dt.timedelta(days=30)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with _connect() as conn:
            conn.execute("UPDATE messenger_recall_runs SET run_at=?", (old,))
        s = messenger_recall_summary(days=7)
        assert s["runs"] == 0
        assert s["totals"]["unread_processed"] == 0

    def test_missing_fields_default_zero(self, tmp_db):
        from src.host.fb_store import (messenger_recall_summary,
                                       record_messenger_recall_run)
        record_messenger_recall_run("devA", {})  # 空 stats
        s = messenger_recall_summary(days=7)
        assert s["runs"] == 1
        assert all(v == 0 for v in s["totals"].values())

    def test_empty_device_noop(self, tmp_db):
        from src.host.fb_store import record_messenger_recall_run
        assert record_messenger_recall_run("", {"unread_processed": 5}) == 0
