# -*- coding: utf-8 -*-
"""P13 `scripts/observe_messenger_health.py` meta 测试。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "observe_messenger_health.py"


# ─── CLI ─────────────────────────────────────────────────────────────────────

class TestCli:
    def test_one_shot_runs(self, tmp_db, tmp_path, monkeypatch):
        """一次性跑不应崩 + 输出包含核心 section 标题。"""
        # 让 logs 目录指向 tmp 避免污染真实 logs/
        monkeypatch.setenv("LOGS_DIR_TEST", str(tmp_path))
        # 2026-04-24 (Phase 10.3): 脚本输出 UTF-8 (含 🔴/🟢 emoji),
        # Windows 上 text=True 默认按 gbk 解码会崩 → 显式 utf-8.
        r = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--since-hours", "1", "--no-archive"],
            capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        )
        assert r.returncode == 0
        assert "Messenger Health Snapshot" in r.stdout
        assert "基础漏斗" in r.stdout
        assert "Intent Health" in r.stdout


# ─── HealthSnapshot ──────────────────────────────────────────────────────────

class TestHealthSnapshot:
    def test_default_empty(self):
        from scripts.observe_messenger_health import HealthSnapshot
        s = HealthSnapshot()
        assert s.funnel_stages == {}
        assert s.errors == []

    def test_to_dict(self):
        from scripts.observe_messenger_health import HealthSnapshot
        s = HealthSnapshot(
            timestamp_iso="2026-04-24T00:00:00Z",
            device_id="d1",
            funnel_stages={"stage_x": 5},
        )
        d = s.to_dict()
        assert d["device_id"] == "d1"
        assert d["funnel_stages"]["stage_x"] == 5


# ─── take_snapshot ───────────────────────────────────────────────────────────

class TestTakeSnapshot:
    def test_empty_db(self, tmp_db):
        from scripts.observe_messenger_health import take_snapshot
        s = take_snapshot(device_id="devA", since_hours=24)
        assert s.device_id == "devA"
        assert s.since_hours == 24
        # 漏斗 stages 都 0
        for k, v in s.funnel_stages.items():
            assert v == 0
        # intent_health no_data
        assert s.intent_health.get("health") in ("no_data", "healthy", "needs_rules")

    def test_with_seeded_data(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from scripts.observe_messenger_health import take_snapshot
        # seed 一条 incoming
        record_inbox_message("devA", "Alice",
                             direction="incoming",
                             peer_type="friend",
                             message_text="多少钱?")
        s = take_snapshot(device_id="devA", since_hours=24)
        # stage_inbox_incoming 应当 >= 1
        assert s.funnel_stages.get("stage_inbox_incoming", 0) >= 1
        # reply_rate_by_intent 应识别 buying (rule 命中)
        by_intent = s.reply_rate_by_intent.get("by_intent", {})
        # 至少有一个 intent 桶 (buying, 或者 opening 如果 history 空)
        assert len(by_intent) >= 1


# ─── render_markdown ─────────────────────────────────────────────────────────

class TestRender:
    def test_minimum_sections(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        s = HealthSnapshot(timestamp_iso="2026-04-24T00:00:00Z",
                           funnel_stages={"stage_greetings_sent": 3})
        md = render_markdown(s)
        assert "# Messenger Health Snapshot" in md
        assert "基础漏斗" in md
        assert "stage_greetings_sent" in md
        assert "Intent Health" in md
        assert "Contact Events" in md

    def test_diff_with_prev_shows_delta(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        prev = HealthSnapshot(timestamp_iso="2026-04-23T00:00:00Z",
                              funnel_stages={"stage_inbox_incoming": 5})
        cur = HealthSnapshot(timestamp_iso="2026-04-24T00:00:00Z",
                             funnel_stages={"stage_inbox_incoming": 8})
        md = render_markdown(cur, diff_with=prev)
        # 差异表格应显示 +3 ↑
        assert "+3" in md
        # 对比 section 存在
        assert "对比上次报告" in md

    def test_diff_shows_arrow_directions(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        prev = HealthSnapshot(
            timestamp_iso="2026-04-23T00:00:00Z",
            funnel_stages={"stage_wa_referrals": 10,
                            "stage_inbox_incoming": 5})
        cur = HealthSnapshot(
            timestamp_iso="2026-04-24T00:00:00Z",
            funnel_stages={"stage_wa_referrals": 7,   # 降
                            "stage_inbox_incoming": 5})  # 平
        md = render_markdown(cur, diff_with=prev)
        assert "-3 ↓" in md or "-3 " in md

    def test_intent_health_degraded_shows_emoji(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        s = HealthSnapshot(intent_health={
            "health": "degraded",
            "rule_coverage": 0.3,
            "recommendation": "扩词表",
        })
        md = render_markdown(s)
        assert "🔴" in md
        assert "degraded" in md
        assert "扩词表" in md

    def test_intent_health_healthy_shows_checkmark(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        s = HealthSnapshot(intent_health={
            "health": "healthy", "rule_coverage": 0.8,
            "recommendation": "OK"})
        md = render_markdown(s)
        assert "✅" in md
        assert "healthy" in md

    def test_contact_events_total_rendered(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        s = HealthSnapshot(contact_events_total={
            "greeting_replied": 5,
            "wa_referral_sent": 3,
        })
        md = render_markdown(s)
        assert "greeting_replied" in md
        assert "| 5 |" in md or "5 |" in md

    def test_errors_section_shown(self):
        from scripts.observe_messenger_health import (
            render_markdown, HealthSnapshot,
        )
        s = HealthSnapshot(errors=["DB offline"])
        md = render_markdown(s)
        assert "采样错误" in md or "⚠" in md
        assert "DB offline" in md


# ─── archive + load_last ─────────────────────────────────────────────────────

class TestArchive:
    def test_archive_and_load(self, tmp_db, tmp_path, monkeypatch):
        """archive 写入 logs/ 再 load 回来, 字段应相等。"""
        import scripts.observe_messenger_health as mod
        monkeypatch.setattr(mod, "LOGS_DIR", tmp_path)
        from scripts.observe_messenger_health import (
            HealthSnapshot, archive_report, load_last_snapshot,
            render_markdown,
        )
        s = HealthSnapshot(
            timestamp_iso="2026-04-24T12:00:00Z",
            device_id="d1",
            funnel_stages={"stage_greetings_sent": 7},
            intent_health={"health": "healthy", "rule_coverage": 0.7},
        )
        md = render_markdown(s)
        path = archive_report(s, md)
        assert path.exists()
        assert path.suffix == ".md"
        json_path = path.with_suffix(".json")
        assert json_path.exists()

        loaded = load_last_snapshot()
        assert loaded is not None
        assert loaded.device_id == "d1"
        assert loaded.funnel_stages["stage_greetings_sent"] == 7

    def test_load_last_none_when_empty(self, tmp_path, monkeypatch):
        import scripts.observe_messenger_health as mod
        monkeypatch.setattr(mod, "LOGS_DIR", tmp_path)
        from scripts.observe_messenger_health import load_last_snapshot
        assert load_last_snapshot() is None


# ─── CLI --diff 集成 ─────────────────────────────────────────────────────────

class TestCliDiff:
    def test_diff_against_existing_report(self, tmp_db, tmp_path, monkeypatch):
        """跑两次带 --diff, 第二次应在输出里看到对比节。"""
        import scripts.observe_messenger_health as mod
        monkeypatch.setattr(mod, "LOGS_DIR", tmp_path)
        from scripts.observe_messenger_health import (
            take_snapshot, archive_report, render_markdown, load_last_snapshot,
        )
        # 1st snapshot + archive
        s1 = take_snapshot(device_id="d1", since_hours=24)
        archive_report(s1, render_markdown(s1))
        # 2nd snapshot, load first as prev, render with diff
        prev = load_last_snapshot()
        assert prev is not None
        s2 = take_snapshot(device_id="d1", since_hours=24)
        md = render_markdown(s2, diff_with=prev)
        assert "对比上次报告" in md
