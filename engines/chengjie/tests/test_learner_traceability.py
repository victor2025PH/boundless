# -*- coding: utf-8 -*-
"""融合 P2 门禁（2026-08-16）：学习溯源 + 效果回访 + 周报学习段。

钉住的决策（改语义请连同本文件一起改）：
1. **溯源迁移**：kb_drafts 幂等加列 source_ref（触发锚点 case:/conv:）+
   entry_id（审核通过后的正式条目 id，与 kb_query_log.matched_entry_id 对账钥匙）；
2. **透传链**：feed_and_learn(source_ref=) → 草稿落库；approve 写回 entry_id；
3. **效果回访零热路径**：effect_report 只读 KB 既有 kb_query_log（7 天滚动、
   每次检索都落 matched_entry_id），绝不给 KB 检索链加写入口；
4. **周报学习段**：value_report 与 goals/cases 同款「显式注入 → peek 单例 →
   两周全零且无积压不出段」纪律；
5. **前端三面**：cases 结案桥带 case: 锚点、cockpit 喂料带 conv: 锚点、
   learner 页展示来源徽章 + 效果回访行；base.html 徽标收敛 todo-summary 单请求。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.utils.daily_learner import DailyLearner, peek_daily_learner

_ROOT = Path(__file__).resolve().parents[1]
_TPL = _ROOT / "src" / "web" / "templates"

NOW = time.time()
DAY = 86400.0


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts))


def _mock_kb(tmp_path):
    kb = MagicMock()
    kb._db_path = str(tmp_path / "kb.db")
    kb.get_miss_stats = MagicMock(return_value=[])
    kb.list_feedback = MagicMock(return_value=[])
    kb.get_auto_suggestions = MagicMock(return_value=[])
    kb.search = MagicMock(return_value={"entries": []})
    kb.get_meta = MagicMock(return_value=None)
    kb.add_entry = MagicMock(return_value="e-77")
    return kb


def _fake_ai():
    ai = MagicMock()
    ai.generate_reply = AsyncMock(return_value=json.dumps([{
        "index": 1, "category": "常规咨询", "title": "VIP 充值",
        "triggers": ["充值", "VIP"], "example_reply": "您好，充值入口在……",
        "reasoning": "高频问题", "confidence": 80,
    }], ensure_ascii=False))
    return ai


# ── 1. 迁移 ──────────────────────────────────────────────────────────────────

def test_trace_columns_migrated(tmp_path):
    dl = DailyLearner(_mock_kb(tmp_path), None, db_path=tmp_path / "d.db")
    with dl._conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(kb_drafts)").fetchall()}
    assert "source_ref" in cols and "entry_id" in cols


def test_migration_idempotent_on_old_db(tmp_path):
    """旧库（无新列）→ 构造即补列且旧行可读；二次构造不炸（幂等）。"""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute(DailyLearner.DRAFTS_SCHEMA)
    conn.execute(
        "INSERT INTO kb_drafts (id,source,query,created_at) VALUES ('x','miss','q','t')")
    conn.commit()
    conn.close()
    dl = DailyLearner(_mock_kb(tmp_path), None, db_path=db)
    row = dl.get_draft("x")
    assert row is not None and row["source_ref"] == "" and row["entry_id"] == ""
    DailyLearner(_mock_kb(tmp_path), None, db_path=db)  # 幂等


def test_peek_returns_last_instance(tmp_path):
    dl = DailyLearner(_mock_kb(tmp_path), None, db_path=tmp_path / "p.db")
    assert peek_daily_learner() is dl


# ── 2. 透传链：feed → 落库 → approve 写回 entry_id ───────────────────────────

@pytest.mark.asyncio
async def test_feed_stores_source_ref(tmp_path):
    kb = _mock_kb(tmp_path)
    dl = DailyLearner(kb, _fake_ai(), db_path=tmp_path / "d.db")
    result = await dl.feed_and_learn("怎么充值VIP", source_ref="case:CASE-abc-1")
    assert result["queued"] is True and result["generated"] == 1
    rows = dl.list_drafts(status="pending")
    assert len(rows) == 1
    assert rows[0]["source_ref"] == "case:CASE-abc-1"
    assert rows[0]["source"] == "manual"


def test_approve_writes_entry_id(tmp_path):
    kb = _mock_kb(tmp_path)
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    dl.save_drafts([{"source": "manual", "query": "怎么充值VIP", "hit_count": 2,
                     "title": "充值", "triggers": ["充值"], "example_reply": "…",
                     "ai_reasoning": "", "confidence": 60,
                     "source_ref": "conv:tg:a:1"}])
    did = dl.list_drafts(status="pending")[0]["id"]
    entry_id = dl.approve_draft(did, operator="tester")
    assert entry_id == "e-77"
    row = dl.get_draft(did)
    assert row["entry_id"] == "e-77"
    assert row["source_ref"] == "conv:tg:a:1"


# ── 3. 效果回访：kb_query_log 只读对账 ───────────────────────────────────────

def test_effect_report_counts_hits(tmp_path):
    kb = _mock_kb(tmp_path)
    # KB 侧真 sqlite：建 kb_query_log 并落 3 次 e-77 命中 + 干扰行
    kbdb = tmp_path / "kb_real.db"
    kc = sqlite3.connect(str(kbdb))
    kc.execute(
        "CREATE TABLE kb_query_log (id TEXT PRIMARY KEY, query TEXT, hit INTEGER, "
        "score REAL, matched_entry_id TEXT, search_mode TEXT, category TEXT, "
        "lang TEXT, ts REAL)")
    rows = [
        ("q1", "充值", 1, 9.0, "e-77", NOW - 1 * DAY),
        ("q2", "怎么充值", 1, 8.0, "e-77", NOW - 0.5 * DAY),
        ("q3", "vip充值", 1, 7.0, "e-77", NOW - 0.2 * DAY),
        ("q4", "别的", 1, 9.0, "e-other", NOW - 1 * DAY),   # 其他条目
        ("q5", "没中", 0, 0.0, "", NOW - 1 * DAY),           # 未命中
    ]
    for qid, q, hit, score, eid, ts in rows:
        kc.execute("INSERT INTO kb_query_log VALUES (?,?,?,?,?,?,?,?,?)",
                   (qid, q, hit, score, eid, "bm25", "", "zh", ts))
    kc.commit()
    kc.close()
    kb._conn = lambda: sqlite3.connect(str(kbdb))

    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    dl.save_drafts([{"source": "manual", "query": "怎么充值VIP", "hit_count": 2,
                     "title": "充值", "triggers": [], "example_reply": "…",
                     "ai_reasoning": "", "confidence": 60,
                     "source_ref": "case:CASE-1"}])
    did = dl.list_drafts(status="pending")[0]["id"]
    dl.approve_draft(did, operator="t")

    rep = dl.effect_report(days=7)
    assert rep["approved_n"] == 1
    assert rep["total_hits"] == 3
    assert rep["entries"][0]["entry_id"] == "e-77"
    assert rep["entries"][0]["hits"] == 3
    assert rep["entries"][0]["source_ref"] == "case:CASE-1"


def test_effect_report_soft_fails_without_query_log(tmp_path):
    """KB 库不可查/旧库无表 → 命中记 0，报告结构完整，绝不抛。"""
    kb = _mock_kb(tmp_path)   # _conn 是 MagicMock，迭代即异常 → 软失败
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    dl.save_drafts([{"source": "miss", "query": "q1", "hit_count": 1,
                     "title": "t", "triggers": [], "example_reply": "",
                     "ai_reasoning": "", "confidence": 50}])
    did = dl.list_drafts(status="pending")[0]["id"]
    dl.approve_draft(did)
    rep = dl.effect_report()
    assert rep["approved_n"] == 1 and rep["total_hits"] == 0


# ── 4. 周报窗口 + value_report 学习段 ────────────────────────────────────────

def _seed_window_drafts(dl: DailyLearner):
    with dl._conn() as c:
        rows = [
            # 本周通过 ×2（hit_count 3+4=覆盖 7 次），上周通过 ×1，本周拒绝 ×1，待审 ×1
            ("a1", "approved", _iso(NOW - 1 * DAY), 3),
            ("a2", "approved", _iso(NOW - 2 * DAY), 4),
            ("a3", "approved", _iso(NOW - 9 * DAY), 5),
            ("r1", "rejected", _iso(NOW - 1 * DAY), 1),
            ("p1", "pending", "", 1),
        ]
        for did, status, reviewed, hits in rows:
            c.execute(
                "INSERT INTO kb_drafts (id,source,query,hit_count,status,"
                "created_at,reviewed_at) VALUES (?,?,?,?,?,?,?)",
                (did, "miss", f"q-{did}", hits, status, _iso(NOW - 3 * DAY),
                 reviewed))


def test_weekly_window_counts(tmp_path):
    dl = DailyLearner(_mock_kb(tmp_path), None, db_path=tmp_path / "d.db")
    _seed_window_drafts(dl)
    tw = dl.weekly_window(NOW - 7 * DAY, NOW)
    lw = dl.weekly_window(NOW - 14 * DAY, NOW - 7 * DAY)
    assert tw == {"approved": 2, "coverage_hits": 7, "rejected": 1, "pending": 1}
    assert lw["approved"] == 1 and lw["coverage_hits"] == 5


def test_value_report_learner_section(tmp_path):
    from src.inbox.store import InboxStore
    from src.ops.value_report import build_weekly_value

    store = InboxStore(tmp_path / "inbox.db")

    class _FakeLearner:
        def __init__(self):
            self.calls = []

        def weekly_window(self, lo, hi):
            self.calls.append((lo, hi))
            if len(self.calls) == 1:
                return {"approved": 2, "coverage_hits": 7, "rejected": 1,
                        "pending": 3}
            return {"approved": 1, "coverage_hits": 5, "rejected": 0,
                    "pending": 3}

    v = build_weekly_value(store, learner=_FakeLearner(), now=NOW)
    assert v["this_week"]["learner"]["approved"] == 2
    assert v["last_week"]["learner"]["approved"] == 1
    text = "\n".join(v["text_lines"])
    assert "学习入库 2 条新知识" in text
    assert "覆盖此前被问过 7 次的缺口" in text
    assert "待审草稿 3 条" in text


def test_value_report_learner_absent_when_inactive(tmp_path):
    from src.inbox.store import InboxStore
    from src.ops.value_report import build_weekly_value

    store = InboxStore(tmp_path / "inbox2.db")

    class _Zero:
        def weekly_window(self, lo, hi):
            return {"approved": 0, "coverage_hits": 0, "rejected": 0,
                    "pending": 0}

    v = build_weekly_value(store, learner=_Zero(), now=NOW)
    assert "learner" not in v["this_week"]
    assert not any("学习入库" in ln for ln in v["text_lines"])


# ── 5. 前端/路由接线（静态钉） ───────────────────────────────────────────────

def test_route_passes_source_ref_and_effect():
    src = (_ROOT / "src" / "web" / "routes" / "learner_routes.py").read_text(
        encoding="utf-8")
    assert "source_ref" in src, "feed 端点未透传 source_ref"
    assert "effect_report" in src, "stats 端点未接效果回访"


def test_cases_bridge_carries_case_anchor():
    src = (_TPL / "cases.html").read_text(encoding="utf-8")
    assert "'case:'+_csCloseCid" in src, "结案喂料桥未带案号溯源"
    assert "ctdo_feed" in src and "data-k" in src, "采纳度埋点未接"
    assert "/api/telemetry/ui-event" in src


def test_cockpit_feed_carries_conv_anchor():
    src = (_TPL / "cockpit.html").read_text(encoding="utf-8")
    assert "source_ref='conv:'+String(srcCid)" in src, "驾驶舱喂料未带会话溯源"


def test_learner_page_renders_trace_and_effect():
    src = (_TPL / "learner.html").read_text(encoding="utf-8")
    assert "_srcRefHtml" in src and "effect_7d" in src
    assert "lr2_effect_line" in src and "lr2_src_ref" in src
    assert "/api/learner/stats?effect=1" in src


def test_base_badges_consolidated_to_todo_summary():
    src = (_TPL / "base.html").read_text(encoding="utf-8")
    # 旧三请求路径必须消失（回归即口径分叉 + 三倍请求）
    assert "/api/learner/drafts?status=pending" not in src
    assert "/api/crisis-events?only_unhandled=true&limit=1" not in src
    # 新单请求路径在场且消费三徽标
    assert "cases_open" in src and "learner_pending" in src \
        and "crisis_unhandled" in src


def test_fusion_p2_keys_bilingual():
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    for k in ("lr2_src_ref", "lr2_effect_line"):
        assert zh.get(k), f"zh 缺键 {k}"
        assert en.get(k), f"en 缺键 {k}"
