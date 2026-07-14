# -*- coding: utf-8 -*-
"""Phase 18 (2026-04-25): reject 持久化 + daily summary task + yaml schema 校验."""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _reset():
    from src.host.fb_store import reset_peer_name_reject_count
    from src.app_automation.facebook import FacebookAutomation
    reset_peer_name_reject_count()
    FacebookAutomation._BLACKLIST_YAML_CACHE = {
        "extra": frozenset(), "loaded_at": 0.0,
    }
    yield


# ═══════════════════════════════════════════════════════════════════
# A1: reject 持久化
# ═══════════════════════════════════════════════════════════════════

class TestRejectPersistence:
    def test_reject_writes_to_db(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          get_peer_name_reject_history)
        record_contact_event("D1", "查看翻译", "greeting_replied")
        record_contact_event("D2", "Reply", "message_received")
        h = get_peer_name_reject_history(hours_window=1)
        assert h["total"] == 2
        assert h["by_event_type"]["greeting_replied"] == 1
        assert h["by_event_type"]["message_received"] == 1
        assert len(h["samples"]) == 2

    def test_history_by_day_aggregation(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          get_peer_name_reject_history)
        for _ in range(3):
            record_contact_event("D", "查看翻译", "greeting_replied")
        h = get_peer_name_reject_history(hours_window=24, by_day=True)
        assert "by_day" in h
        # 1 个 date key, count = 3
        assert sum(h["by_day"].values()) == 3

    def test_history_window_exclusion(self, tmp_db):
        from src.host.fb_store import (record_contact_event,
                                          get_peer_name_reject_history)
        from src.host.database import _connect
        # 写一条 然后 fake 它的 at 为 200h ago
        record_contact_event("D", "Reply", "greeting_replied")
        with _connect() as c:
            c.execute("UPDATE peer_name_reject_log SET at = "
                       "datetime('now', '-200 hours')")
        # 168h window 应排除
        h = get_peer_name_reject_history(hours_window=168)
        assert h["total"] == 0


# ═══════════════════════════════════════════════════════════════════
# A2: daily summary task
# ═══════════════════════════════════════════════════════════════════

class TestDailySummaryTask:
    def test_summary_runs_no_data(self, tmp_db, tmp_path, monkeypatch):
        """空 DB 跑 summary, 不崩."""
        from src.host.executor import _fb_daily_referral_summary
        # patch logs dir
        monkeypatch.chdir(tmp_path)
        ok, _msg, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": True,
            "send_webhook": False})
        assert ok
        assert "summary" in stats
        s = stats["summary"]
        assert s["funnel"]["planned"] == 0
        assert s["top_5_accounts"] == []
        # 文件写了
        assert stats["written_to"]
        from pathlib import Path
        assert Path(stats["written_to"]).exists()

    def test_summary_with_seeded_data(self, tmp_db, tmp_path, monkeypatch):
        from src.host.executor import _fb_daily_referral_summary
        from src.host import line_pool as lp
        from src.host.fb_store import record_contact_event
        # seed
        aid = lp.add("@test", region="jp")
        # 模拟 1 planned
        record_contact_event("D1", "山田花子", "line_dispatch_planned",
                              meta={"line_id": "@test"})
        record_contact_event("D1", "山田花子", "wa_referral_sent",
                              meta={"line_id": "@test"})
        # 模拟 1 reject
        record_contact_event("D1", "查看翻译", "greeting_replied")

        monkeypatch.chdir(tmp_path)
        ok, _, stats = _fb_daily_referral_summary({
            "hours_window": 24, "write_file": True, "send_webhook": False})
        assert ok
        s = stats["summary"]
        assert s["funnel"]["planned"] == 1
        assert s["funnel"]["sent"] == 1
        assert s["peer_name_rejects"]["live_total"] >= 1


# ═══════════════════════════════════════════════════════════════════
# A3: yaml schema 校验
# ═══════════════════════════════════════════════════════════════════

class TestYamlSchemaValidation:
    def _make_yaml_load_returning(self, monkeypatch, data):
        """patch _load_extra_blacklist 让它从内联 data 读."""
        from src.app_automation.facebook import FacebookAutomation
        import yaml as _yaml

        # 强制 cache miss
        FacebookAutomation._BLACKLIST_YAML_CACHE = {
            "extra": frozenset(), "loaded_at": 0.0,
        }

        # patch yaml.safe_load 返指定 data
        def _fake_load(_f):
            return data
        monkeypatch.setattr(_yaml, "safe_load", _fake_load)

        # patch Path.exists 返 True
        from pathlib import Path
        monkeypatch.setattr(Path, "exists", lambda self: True)

        # patch yaml_path.open
        import io
        def _fake_open(self, *a, **kw):
            return io.StringIO("")
        monkeypatch.setattr(Path, "open", _fake_open)

    def test_top_level_not_dict_logs_error(self, monkeypatch, caplog):
        """data = list (非 dict) → error log + 返空."""
        from src.app_automation.facebook import FacebookAutomation
        self._make_yaml_load_returning(monkeypatch, ["not", "a", "dict"])
        import logging
        with caplog.at_level(logging.ERROR):
            r = FacebookAutomation._load_extra_blacklist()
        assert r == frozenset()
        assert any("顶层必须是 dict" in (rec.message or "")
                   for rec in caplog.records)

    def test_extra_blacklist_not_list_logs_error(self, monkeypatch, caplog):
        """extra_blacklist = dict (非 list) → error log + 返空."""
        from src.app_automation.facebook import FacebookAutomation
        self._make_yaml_load_returning(monkeypatch,
                                          {"extra_blacklist": {"x": 1}})
        import logging
        with caplog.at_level(logging.ERROR):
            r = FacebookAutomation._load_extra_blacklist()
        assert r == frozenset()
        assert any("必须是 list" in (rec.message or "")
                   for rec in caplog.records)

    def test_non_str_item_skipped_with_warning(self, monkeypatch, caplog):
        """list 含非 str → warning + skip 那项, 其他正常."""
        from src.app_automation.facebook import FacebookAutomation
        self._make_yaml_load_returning(monkeypatch, {
            "extra_blacklist": ["valid_word", 123, None, "other_word"],
        })
        import logging
        with caplog.at_level(logging.WARNING):
            r = FacebookAutomation._load_extra_blacklist()
        assert "valid_word" in r
        assert "other_word" in r
        # 123 / None 被 skip
        assert 123 not in r
        # 至少 2 条 warning (123 / None 各一)
        warnings_count = sum(1 for rec in caplog.records
                              if "不是 str" in (rec.message or ""))
        assert warnings_count >= 2

    def test_empty_yaml_returns_empty_no_error(self, monkeypatch):
        """完全空文件 (data=None) — 合法, 返空, 无 error."""
        from src.app_automation.facebook import FacebookAutomation
        self._make_yaml_load_returning(monkeypatch, None)
        r = FacebookAutomation._load_extra_blacklist()
        assert r == frozenset()

    def test_missing_key_returns_empty_no_error(self, monkeypatch):
        """yaml 没 extra_blacklist key — 合法, 返空."""
        from src.app_automation.facebook import FacebookAutomation
        self._make_yaml_load_returning(monkeypatch, {
            "other_key": "anything",
        })
        r = FacebookAutomation._load_extra_blacklist()
        assert r == frozenset()


# ═══════════════════════════════════════════════════════════════════
# API
# ═══════════════════════════════════════════════════════════════════

class TestApi:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_history_api_returns_persisted(self, tmp_db, client):
        from src.host.fb_store import record_contact_event
        record_contact_event("D1", "Reply", "greeting_replied")
        r = client.get("/line-pool/stats/peer-name-rejects/history"
                        "?hours_window=24&by_day=true")
        assert r.status_code == 200
        d = r.json()
        assert d["total"] == 1
        assert "by_day" in d
