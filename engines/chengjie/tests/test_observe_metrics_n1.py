# -*- coding: utf-8 -*-
"""观测先行（2026-09-18 N1）：记忆 / 时间 / 一致性三条线的读数。

钉住：
- ``gap_bucket`` 分桶边界与 build_time_gap_hint 的 6h/48h 档对齐；
- ``apply_inbound_enrichments`` 出时间提示时同时记 ``time_hint_gap:<桶>``；
- ``probe_detail_from_store`` 回 (块, 类型, 证据数)，无证据 → n=0；``merge_evidence`` 去重保序；
- conv_route ``consistency_kept_by_layer`` 逐层计数、reset 清零；
- ``get_inbox_draft_metrics`` 派生读数：no_evidence 占比 / 断层分桶表 / 探针类型表，
  且新三类注入进 ``rates_vs_generated``；
- prompt_trace ``cache_stats().by_lane``：本地与云端命中率分开。
"""
from __future__ import annotations

import pytest

from src.ai import conv_route, prompt_trace
from src.inbox.time_context import GAP_BUCKETS, gap_bucket
from src.monitoring.metrics_store import get_metrics_store

D = 86400


@pytest.fixture(autouse=True)
def _reset():
    conv_route.reset_for_tests()
    prompt_trace.reset()
    yield
    conv_route.reset_for_tests()
    prompt_trace.reset()


# ── 分桶 ─────────────────────────────────────────────────────────────────────

def test_gap_bucket_boundaries():
    assert gap_bucket(0) == "<6h"
    assert gap_bucket(6 * 3600 - 1) == "<6h"
    assert gap_bucket(6 * 3600) == "6h-1d"
    assert gap_bucket(D - 1) == "6h-1d"
    assert gap_bucket(D) == "1-3d"
    assert gap_bucket(3 * D) == "3-7d"
    assert gap_bucket(7 * D) == "7-14d"
    assert gap_bucket(14 * D) == "14-30d"
    assert gap_bucket(30 * D) == "30d+"
    assert gap_bucket(400 * D) == "30d+"
    assert gap_bucket("garbage") == "<6h" and gap_bucket(None) == "<6h"
    assert all(gap_bucket(x) in GAP_BUCKETS for x in (0, 7 * 3600, 2 * D, 5 * D, 10 * D, 20 * D, 90 * D))


def test_enrich_records_gap_bucket_metric():
    from src.inbox.inbound_enrich import apply_inbound_enrichments
    ms = get_metrics_store()
    before = int(ms.get_inbox_draft_metrics()["total"].get("time_hint_gap:3-7d", 0))
    ctx = {"_turn_gap_sec": 5.4 * D}
    apply_inbound_enrichments(ctx, text="在吗", history=[], reply_lang="zh")
    assert "【时间提示——重要】" in str(ctx.get("_topic_switch_hint") or "")
    after = int(ms.get_inbox_draft_metrics()["total"].get("time_hint_gap:3-7d", 0))
    assert after == before + 1
    # 短间隔：不出提示、不记桶
    before2 = int(ms.get_inbox_draft_metrics()["total"].get("time_hint_gap:<6h", 0))
    ctx2 = {"_turn_gap_sec": 600}
    apply_inbound_enrichments(ctx2, text="在吗", history=[], reply_lang="zh")
    assert "【时间提示——重要】" not in str(ctx2.get("_topic_switch_hint") or "")
    assert int(ms.get_inbox_draft_metrics()["total"].get("time_hint_gap:<6h", 0)) == before2


# ── 记忆探针带观测 ────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, *, limit=50, include_deleted=True):
        return list(self.rows)[-limit:]

    def list_messages(self, cid, *, limit=50):
        return list(self.rows)[:limit]

    def count_messages(self, cid=""):
        return len(self.rows)


def _r(mid, ts, direction, text):
    return {"message_id": mid, "ts": ts, "direction": direction, "text": text}


def test_probe_detail_reports_kind_and_evidence_count():
    from src.inbox.memory_probe import probe_detail_from_store
    rows = [_r("m1", 1000, "in", "我叫阿明，在深圳做设计"), _r("m2", 1010, "out", "阿明你好"),
            _r("m3", 2000, "in", "上周吃火锅吃到撑"), _r("m4", 2010, "out", "哈哈注意肠胃"),
            _r("m5", 3000, "in", "还记得我上次说吃什么吃到撑吗")]
    st = _Store(rows)
    block, kind, n = probe_detail_from_store(st, "c1", "还记得我上次说吃什么吃到撑吗")
    assert kind == "recall" and n >= 1 and "火锅" in block
    block2, kind2, n2 = probe_detail_from_store(st, "c1", "还记得我说过我养了几只猫吗")
    assert kind2 == "recall" and n2 == 0 and "没有" in block2
    block3, kind3, n3 = probe_detail_from_store(st, "c1", "今天天气真好")
    assert (block3, kind3, n3) == ("", "", 0)


def test_probe_detail_merges_semantic_rows_for_recall():
    from src.inbox.memory_probe import probe_detail_from_store
    rows = [_r("m1", 1000, "in", "我叫阿明"), _r("m3", 2000, "in", "上周吃火锅吃到撑"),
            _r("m5", 3000, "in", "还记得我说的那个涮肉店吗")]
    st = _Store(rows)
    # 轻量同义（涮肉→火锅）即可命中；语义行再补也不该冲掉原文
    sem = [rows[1]]
    block, kind, n = probe_detail_from_store(st, "c1", "还记得我说的那个涮肉店吗", semantic_rows=sem)
    assert kind == "recall" and n >= 1 and "火锅" in block
    block0, kind0, n0 = probe_detail_from_store(st, "c1", "还记得我说的那个涮肉店吗")
    assert kind0 == "recall" and n0 >= 1 and "火锅" in block0


def test_merge_evidence_dedupes_and_orders():
    from src.inbox.memory_probe import merge_evidence
    a = [_r("m3", 2000, "in", "上周吃火锅"), _r("m4", 2010, "out", "注意肠胃")]
    b = [_r("m3", 2000, "in", "上周吃火锅"), _r("m1", 1000, "in", "我叫阿明"),
         _r("m9", 9000, "in", "当前问题"), {"ts": 500, "direction": "in", "text": "[图片]"}]
    out = merge_evidence(a, b, current_text="当前问题")
    ids = [r["message_id"] for r in out]
    assert ids == ["m1", "m3", "m4"]


# ── conv_route 一致性层按层计数 ───────────────────────────────────────────────

def test_consistency_kept_by_layer_counts_and_resets():
    ctx = {"_unrestricted": True}
    conv_route.skip_guard(ctx, "fabrication_guard")
    conv_route.skip_guard(ctx, "fabrication_guard")
    conv_route.skip_guard(ctx, "world_clock_guard")
    conv_route.skip_guard(ctx, "persona_guard")          # 普通质量层：跳，不计入 kept
    snap = conv_route.stats_snapshot()
    assert snap["consistency_kept"] == 3
    assert snap["consistency_kept_by_layer"] == {"fabrication_guard": 2, "world_clock_guard": 1}
    assert snap["skipped_total"] == 1
    conv_route.reset_for_tests()
    snap2 = conv_route.stats_snapshot()
    assert snap2["consistency_kept"] == 0 and snap2["consistency_kept_by_layer"] == {}


# ── metrics_store 派生读数 ───────────────────────────────────────────────────

def test_inbox_draft_metrics_derived_block():
    ms = get_metrics_store()
    snap0 = ms.get_inbox_draft_metrics()
    p0 = int(snap0["total"].get("memory_probe_hint", 0))
    n0 = int(snap0["total"].get("memory_probe_no_evidence", 0))
    ms.record_inbox_draft_event("generated")
    ms.record_inbox_draft_event("memory_probe_hint", 4)
    ms.record_inbox_draft_event("memory_probe_no_evidence", 1)
    ms.record_inbox_draft_event("memory_probe:recall", 3)
    ms.record_inbox_draft_event("memory_probe:name", 1)
    ms.record_inbox_draft_event("time_hint_gap:7-14d", 2)
    ms.record_inbox_draft_event("time_hint_active", 2)
    snap = ms.get_inbox_draft_metrics()
    d = snap["derived"]
    assert d["memory_probe_no_evidence_ratio"] == round((n0 + 1) / (p0 + 4), 4)
    assert d["time_hint_gap"]["7-14d"] >= 2
    assert d["memory_probe_kinds"]["recall"] >= 3 and d["memory_probe_kinds"]["name"] >= 1
    for k in ("time_hint_active", "memory_probe_hint", "media_ledger_hint"):
        assert k in snap["rates_vs_generated"]


# ── prompt_trace 分 lane 缓存统计 ────────────────────────────────────────────

def _usage(prompt, cached):
    return {"prompt_tokens": prompt, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": cached}}


def test_prompt_trace_cache_stats_by_lane():
    msgs = [{"role": "system", "content": "【后台人设定位 · 须遵守】x"}, {"role": "user", "content": "hi"}]
    prompt_trace.record(messages=msgs, model="deepseek-chat", usage=_usage(1000, 800), ok=True)
    prompt_trace.record(messages=msgs, model="chatx", usage=_usage(2000, 500), ok=True,
                        purpose="local_primary")
    prompt_trace.record(messages=msgs, model="chatx", usage=_usage(1000, 0), ok=False,
                        purpose="local_primary")        # 失败不计
    cs = prompt_trace.cache_stats()
    assert cs["calls"] == 2
    assert cs["by_lane"]["cloud"]["hit_ratio"] == 0.8
    assert cs["by_lane"]["local_primary"]["calls"] == 1
    assert cs["by_lane"]["local_primary"]["hit_ratio"] == 0.25
    assert cs["hit_ratio"] == round(1300 / 3000, 4)
    ents = prompt_trace.list_entries(limit=5)
    assert any(e.get("purpose") == "local_primary" for e in ents)
