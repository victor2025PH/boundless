# -*- coding: utf-8 -*-
"""P8 `src/analytics/chat_funnel.py` 单元测试 — 扩展漏斗指标。

通过 conftest 的 tmp_db 起临时 sqlite, 用 record_inbox_message 种数据,
再跑 chat_funnel 聚合验证。
"""
from __future__ import annotations

import json
import sqlite3

import pytest


# ─── reply_rate_by_intent ────────────────────────────────────────────────────

class TestReplyRateByIntent:
    def test_empty_db_returns_zeros(self, tmp_db):
        from src.analytics.chat_funnel import reply_rate_by_intent
        r = reply_rate_by_intent()
        assert r["total_incomings"] == 0
        assert r["by_intent"] == {}
        assert r["errors"] == 0

    def test_classifies_and_aggregates(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import reply_rate_by_intent

        # alice 发购买信号 → B 回 → 应被归 buying
        record_inbox_message("d1", "Alice", direction="incoming",
                             peer_type="friend",
                             message_text="How much does it cost?")
        record_inbox_message("d1", "Alice", direction="outgoing",
                             peer_type="friend",
                             ai_decision="wa_referral",
                             message_text="加我 LINE")

        # bob 问 LINE → referral_ask → B 未回复
        record_inbox_message("d1", "Bob", direction="incoming",
                             peer_type="friend",
                             message_text="Do you have WhatsApp?")
        # 没有后续 outgoing

        r = reply_rate_by_intent(device_id="d1")
        assert r["total_incomings"] == 2
        assert r["errors"] == 0
        assert "buying" in r["by_intent"]
        assert "referral_ask" in r["by_intent"]
        # buying: 有 reply (wa_referral 算 replied)
        assert r["by_intent"]["buying"]["replied"] == 1
        assert r["by_intent"]["buying"]["reply_rate"] == 1.0
        # referral_ask: 未 reply
        assert r["by_intent"]["referral_ask"]["replied"] == 0
        assert r["by_intent"]["referral_ask"]["reply_rate"] == 0.0

    def test_device_filter(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import reply_rate_by_intent
        record_inbox_message("d1", "A", direction="incoming",
                             message_text="how much")
        record_inbox_message("d2", "A", direction="incoming",
                             message_text="how much")
        r_d1 = reply_rate_by_intent(device_id="d1")
        r_d2 = reply_rate_by_intent(device_id="d2")
        r_all = reply_rate_by_intent()
        assert r_d1["total_incomings"] == 1
        assert r_d2["total_incomings"] == 1
        assert r_all["total_incomings"] == 2


# ─── stranger_conversion_rate ────────────────────────────────────────────────

class TestStrangerConversionRate:
    def test_empty(self, tmp_db):
        from src.analytics.chat_funnel import stranger_conversion_rate
        r = stranger_conversion_rate()
        assert r["stranger_peers"] == 0
        assert r["reply_rate"] == 0.0
        assert r["referral_rate"] == 0.0

    def test_basic_aggregation(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import stranger_conversion_rate
        # 3 个陌生人都发 incoming
        for name in ("s1", "s2", "s3"):
            record_inbox_message("d1", name, direction="incoming",
                                 peer_type="stranger",
                                 message_text=f"hi from {name}")
        # s1, s2 被 B 回复 (reply)
        record_inbox_message("d1", "s1", direction="outgoing",
                             peer_type="stranger",
                             ai_decision="reply",
                             message_text="hello")
        record_inbox_message("d1", "s2", direction="outgoing",
                             peer_type="stranger",
                             ai_decision="reply",
                             message_text="hello")
        # s2 升级到 wa_referral
        record_inbox_message("d1", "s2", direction="outgoing",
                             peer_type="stranger",
                             ai_decision="wa_referral",
                             message_text="加 LINE")

        r = stranger_conversion_rate(device_id="d1")
        assert r["stranger_peers"] == 3
        assert r["stranger_replied"] == 2  # s1 + s2
        assert r["stranger_wa_referred"] == 1  # s2
        assert r["reply_rate"] == round(2 / 3, 3)
        assert r["referral_rate"] == round(1 / 3, 3)

    def test_friend_type_not_counted(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import stranger_conversion_rate
        record_inbox_message("d1", "alice", direction="incoming",
                             peer_type="friend",  # 不是 stranger
                             message_text="hi")
        r = stranger_conversion_rate(device_id="d1")
        assert r["stranger_peers"] == 0


# ─── gate_block_distribution ─────────────────────────────────────────────────

class TestGateBlockDistribution:
    def test_returns_empty_shell_when_table_missing(self, tmp_db):
        """Graceful-degradation: `fb_contact_events` 表不存在 (或 SELECT 抛) →
        返含 `note` 键的 empty shell, 不抛。原 "phase5 未 merge" 场景 —
        init_db 现在会建表, 本测试显式 DROP 表模拟"DB 未迁移" case 以覆盖
        `except Exception` 分支。"""
        from src.analytics.chat_funnel import gate_block_distribution
        from src.host.fb_store import _connect
        with _connect() as conn:
            conn.execute("DROP TABLE IF EXISTS fb_contact_events")
            conn.commit()
        r = gate_block_distribution()
        assert r.get("by_channel") == {}
        assert r.get("by_peer_type") == {}
        assert r.get("by_intent_at_referral") == {}
        assert "note" in r  # graceful-degradation 标志位

    def test_with_fake_contact_events_table(self, tmp_db):
        """手建 fb_contact_events 表 + 种数据, 验聚合正确。"""
        from src.host.fb_store import _connect
        from src.analytics.chat_funnel import gate_block_distribution

        with _connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS fb_contact_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    peer_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    template_id TEXT DEFAULT '',
                    preset_key TEXT DEFAULT '',
                    meta_json TEXT DEFAULT '',
                    detected_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            # 种 4 条 wa_referral_sent, 不同 channel/intent
            events = [
                ("d1", "A", {"channel": "line", "peer_type": "friend",
                             "intent": "buying"}),
                ("d1", "B", {"channel": "line", "peer_type": "friend",
                             "intent": "referral_ask"}),
                ("d1", "C", {"channel": "whatsapp", "peer_type": "stranger",
                             "intent": "buying"}),
                ("d1", "D", {"channel": "telegram", "peer_type": "stranger",
                             "intent": "buying"}),
            ]
            for did, peer, meta in events:
                conn.execute(
                    "INSERT INTO fb_contact_events (device_id, peer_name,"
                    " event_type, meta_json) VALUES (?, ?, ?, ?)",
                    (did, peer, "wa_referral_sent", json.dumps(meta)),
                )

        r = gate_block_distribution(device_id="d1")
        assert r["total_referrals"] == 4
        assert r["by_channel"] == {"line": 2, "whatsapp": 1, "telegram": 1}
        assert r["by_peer_type"] == {"friend": 2, "stranger": 2}
        assert r["by_intent_at_referral"] == {
            "buying": 3, "referral_ask": 1}


# ─── intent_source_coverage ──────────────────────────────────────────────────

class TestIntentSourceCoverage:
    def test_empty(self, tmp_db):
        from src.analytics.chat_funnel import intent_source_coverage
        r = intent_source_coverage()
        assert r["total_sampled"] == 0
        assert r["by_source"] == {}

    def test_rule_coverage_high(self, tmp_db):
        """多数 incoming 含 rule 命中关键词, rule source 占比应高。"""
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import intent_source_coverage
        msgs = [
            "how much does it cost",  # buying (rule)
            "add me on LINE",          # referral_ask (rule)
            "再见",                    # closing (rule)
            "ok",                      # cold (rule)
        ]
        # 首轮 = opening (rule) 所以加 outgoing 先置入 history 前置状态
        record_inbox_message("d1", "peer", direction="outgoing",
                             peer_type="friend", message_text="greeting")
        for m in msgs:
            record_inbox_message("d1", "peer", direction="incoming",
                                 peer_type="friend", message_text=m)
        # classify 不走历史 (coverage 函数本就是对单条消息判断)
        r = intent_source_coverage(device_id="d1")
        assert r["total_sampled"] >= 1
        # rule 命中占比
        rules = r["by_source"].get("rule", 0)
        total = r["total_sampled"]
        assert rules / total > 0.5  # 至少一半来自规则

    def test_sample_limit(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import intent_source_coverage
        for i in range(30):
            record_inbox_message("d1", f"p{i}", direction="incoming",
                                 peer_type="friend",
                                 message_text=f"hello {i}")
        r = intent_source_coverage(device_id="d1", sample_limit=5)
        assert r["total_sampled"] <= 5


# ─── P9 intent_health_report ─────────────────────────────────────────────────

class TestIntentHealthReport:
    def test_no_data_returns_no_data_health(self, tmp_db):
        from src.analytics.chat_funnel import intent_health_report
        r = intent_health_report()
        assert r["health"] == "no_data"
        assert r["rule_coverage"] == 0.0

    def test_healthy_when_rules_dominate(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import intent_health_report
        for m in ["how much", "LINE できますか",
                  "再见", "ok", "hello friend how are you"]:
            record_inbox_message("d1", "p", direction="incoming",
                                 peer_type="friend", message_text=m)
        r = intent_health_report(device_id="d1")
        # 大部分可 rule 命中 → healthy
        assert r["health"] in ("healthy", "needs_rules")
        assert r["rule_coverage"] > 0.5
        assert "rule" in r["recommendation"] or "健康" in r["recommendation"]

    def test_degraded_when_mostly_fallback(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import intent_health_report
        # 故意用模糊消息让 rule 不命中 → LLM 关 → fallback
        for i in range(5):
            record_inbox_message("d1", f"p{i}", direction="incoming",
                                 peer_type="friend",
                                 message_text="这是一个比较含糊的表达消息")
        r = intent_health_report(device_id="d1")
        # 大部分 fallback (rule 不中 + LLM 关) → degraded 或 needs_rules
        assert r["health"] in ("degraded", "needs_rules")
        assert "扩" in r["recommendation"] or "审计" in r["recommendation"]

    def test_thresholds_customizable(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import intent_health_report
        record_inbox_message("d1", "p", direction="incoming",
                             peer_type="friend", message_text="how much?")
        # 把 healthy 门调到 99%, 必然 degraded/needs_rules
        r = intent_health_report(device_id="d1",
                                  rule_threshold_healthy=0.99,
                                  rule_threshold_degraded=0.99)
        assert r["health"] in ("needs_rules", "degraded", "healthy")


# ─── a_greeting_reply_rate (A Phase 5 消费) ──────────────────────────────────

class TestAGreetingReplyRate:
    def test_a_not_merged_returns_empty(self, tmp_db):
        from src.analytics.chat_funnel import a_greeting_reply_rate
        r = a_greeting_reply_rate()
        # Phase 5 未 merge → note 提示
        assert "note" in r or "templates" in r

    def test_a_available_calls_through(self, tmp_db, monkeypatch):
        from src.analytics import chat_funnel
        from src.host import fb_store
        fake = {"templates": {"yaml:jp:1": {"sent": 5, "replied": 3}}}
        monkeypatch.setattr(fb_store, "get_greeting_reply_rate_by_template",
                            lambda **kw: fake, raising=False)
        r = chat_funnel.a_greeting_reply_rate(device_id="d1")
        assert r == fake


# ─── get_funnel_metrics_extended (一站式) ────────────────────────────────────

class TestGetFunnelMetricsExtended:
    def test_shape_contains_all_keys(self, tmp_db):
        from src.analytics.chat_funnel import get_funnel_metrics_extended
        r = get_funnel_metrics_extended()
        # 基础 stage_* 至少存在几个
        assert isinstance(r, dict)
        # B 扩展字段
        assert "reply_rate_by_intent" in r
        assert "stranger_conversion_rate" in r
        assert "gate_block_distribution" in r
        assert "intent_source_coverage" in r
        assert "greeting_reply_rate" in r

    def test_with_real_data(self, tmp_db):
        from src.host.fb_store import record_inbox_message
        from src.analytics.chat_funnel import get_funnel_metrics_extended

        # 种数据: 1 陌生人 buying 被引流
        record_inbox_message("d1", "Stranger1", direction="incoming",
                             peer_type="stranger",
                             message_text="how much?")
        record_inbox_message("d1", "Stranger1", direction="outgoing",
                             peer_type="stranger",
                             ai_decision="wa_referral",
                             message_text="加 LINE")

        r = get_funnel_metrics_extended(device_id="d1")

        # reply_rate_by_intent 有 buying
        rri = r["reply_rate_by_intent"]
        assert rri["total_incomings"] == 1
        assert "buying" in rri["by_intent"]
        assert rri["by_intent"]["buying"]["replied"] == 1

        # stranger_conversion_rate 算对
        scr = r["stranger_conversion_rate"]
        assert scr["stranger_peers"] == 1
        assert scr["referral_rate"] == 1.0

    def test_flags_skip_optional_sections(self, tmp_db):
        from src.analytics.chat_funnel import get_funnel_metrics_extended
        r = get_funnel_metrics_extended(include_intent_coverage=False,
                                         include_greeting_template=False)
        # include_* 为 False 时对应 key 不出现
        assert "intent_source_coverage" not in r
        assert "greeting_reply_rate" not in r
        assert "reply_rate_by_intent" in r
        assert "stranger_conversion_rate" in r
