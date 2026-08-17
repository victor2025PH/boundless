# -*- coding: utf-8 -*-
"""融合 P3 门禁（2026-08-16）：零命中淘汰建议 + 采纳度裁决 CLI + 两条页面引导。

钉住的决策：
1. **淘汰建议只出建议不动数据**：retirement_candidates 纯读；检索日志不可查时
   返回空——「不知道」绝不伪装成「零命中」；停用/改词是 /knowledge 页的显式操作。
2. **裁决 CLI 家法**（对齐 inbox_filter_usage_report）：埋点纪元日起算观测天数、
   零点击是合法证据、全零先怀疑埋点链、比值型结论小样本降置信、喂料桥采纳率
   分母＝同窗案例结案数（分母不可读如实降级为绝对量判词）。
3. **告警引导条窄触发**：只在 verdict=no_channel 现身，7 天 dismiss，端点缺席静默。
4. **空态学习引导零新请求**：复用待办条缓存的 todo-summary 快照。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock

from src.utils.daily_learner import DailyLearner
from tools.fusion_adoption_report import (
    aggregate,
    build_verdicts,
    observed_days,
    read_closes,
    read_rows,
)

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
    kb.add_entry = MagicMock(return_value="e-1")
    return kb


def _seed_approved(dl: DailyLearner, draft_id: str, entry_id: str,
                   reviewed_ts: float) -> None:
    with dl._conn() as c:
        c.execute(
            "INSERT INTO kb_drafts (id,source,query,title,status,created_at,"
            "reviewed_at,entry_id) VALUES (?,?,?,?,?,?,?,?)",
            (draft_id, "manual", f"q-{draft_id}", f"t-{draft_id}", "approved",
             _iso(reviewed_ts - DAY), _iso(reviewed_ts), entry_id))


def _kb_with_query_log(tmp_path, hit_entry_ids):
    kbdb = tmp_path / "kb_real.db"
    kc = sqlite3.connect(str(kbdb))
    kc.execute(
        "CREATE TABLE kb_query_log (id TEXT PRIMARY KEY, query TEXT, hit INTEGER, "
        "score REAL, matched_entry_id TEXT, search_mode TEXT, category TEXT, "
        "lang TEXT, ts REAL)")
    for i, eid in enumerate(hit_entry_ids):
        kc.execute("INSERT INTO kb_query_log VALUES (?,?,?,?,?,?,?,?,?)",
                   (f"q{i}", "x", 1, 9.0, eid, "bm25", "", "zh", NOW - 1 * DAY))
    kc.commit()
    kc.close()
    kb = _mock_kb(tmp_path)
    kb._conn = lambda: sqlite3.connect(str(kbdb))
    return kb


# ── 1. 零命中淘汰建议 ────────────────────────────────────────────────────────

def test_retirement_flags_zero_hit_aged_entries(tmp_path):
    kb = _kb_with_query_log(tmp_path, ["e-hit"])
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    _seed_approved(dl, "old-zero", "e-zero", NOW - 5 * DAY)   # 入库 5 天零命中 → 点名
    _seed_approved(dl, "old-hit", "e-hit", NOW - 5 * DAY)     # 有命中 → 不点名
    _seed_approved(dl, "fresh", "e-fresh", NOW - 1 * DAY)     # 太新 → 宽限期不点名
    cands = dl.retirement_candidates(min_age_days=3)
    ids = [c["entry_id"] for c in cands]
    assert ids == ["e-zero"]
    assert cands[0]["title"] == "t-old-zero"


def test_retirement_silent_when_log_unreadable(tmp_path):
    """日志不可查（KB 连接直接抛异常）→ 空建议，绝不冤枉条目（防冤枉闸①）。"""
    kb = _mock_kb(tmp_path)
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    _seed_approved(dl, "a", "e-a", NOW - 5 * DAY)
    kb._conn = MagicMock(side_effect=RuntimeError("kb down"))
    assert dl.retirement_candidates(min_age_days=3) == []


def test_retirement_silent_when_log_has_no_traffic(tmp_path):
    """日志表在但**零流量**：没人问 ≠ 条目没用——人人零命中时不出任何建议
    （防冤枉闸②；首版实现就栽在这里，本例是那次翻车的回归钉）。"""
    kb = _kb_with_query_log(tmp_path, [])   # 表存在、零行
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    _seed_approved(dl, "a", "e-a", NOW - 5 * DAY)
    assert dl.retirement_candidates(min_age_days=3) == []


def test_retirement_respects_limit(tmp_path):
    # 日志里放一条无关条目的命中——证明检索链活着，零命中结论才可下
    kb = _kb_with_query_log(tmp_path, ["e-unrelated"])
    dl = DailyLearner(kb, None, db_path=tmp_path / "d.db")
    for i in range(6):
        _seed_approved(dl, f"d{i}", f"e-{i}", NOW - 4 * DAY)
    assert len(dl.retirement_candidates(min_age_days=3, limit=4)) == 4


# ── 2. 裁决 CLI 纯函数 ──────────────────────────────────────────────────────

def _agg(**totals):
    rows = [{"day": "2026-08-16", "action": k, "n": v} for k, v in totals.items()]
    return aggregate(rows)


def test_observed_days_from_epoch():
    assert observed_days("2026-08-16", "2026-08-16") == 1
    assert observed_days("2026-08-30", "2026-08-16") == 15
    assert observed_days("bad", "2026-08-16") == 0


def test_verdicts_insufficient_before_min_days():
    agg = _agg(ctdo_learner=3)
    vs = build_verdicts(agg, now_day="2026-08-18", epoch_day="2026-08-16",
                        min_days=14, min_total=20)
    assert all(v["status"] == "insufficient" for v in vs)
    assert any("观测 3/14 天" in str(v.get("note")) for v in vs)


def test_verdicts_broken_chain_note_when_all_zero():
    vs = build_verdicts(_agg(), now_day="2026-09-01", epoch_day="2026-08-16",
                        min_days=14, min_total=20)
    assert all(v["status"] == "insufficient" for v in vs)
    assert all("埋点链" in str(v.get("note")) for v in vs)


def test_verdicts_dead_pills_named():
    agg = _agg(ctdo_learner=30, ctdo_drafts=5)
    vs = build_verdicts(agg, now_day="2026-09-01", epoch_day="2026-08-16",
                        min_days=14, min_total=20)
    q2 = next(v for v in vs if v["question"] == "各 pill 去留")
    assert q2["status"] == "ready"
    assert "ctdo_sla" in q2["recommendation"] and "ctdo_crisis" in q2["recommendation"]


def test_verdicts_feed_rate_with_closes_denominator():
    agg = _agg(ctdo_learner=1, ctdo_feed=10)
    vs = build_verdicts(agg, now_day="2026-09-01", epoch_day="2026-08-16",
                        closes=40, min_days=14, min_total=20)
    q3 = next(v for v in vs if v["question"] == "结案喂料桥采纳")
    assert q3["status"] == "ready"
    assert "25.0%" in q3["recommendation"]
    assert "采纳" in q3["recommendation"]


def test_verdicts_feed_absolute_without_denominator():
    agg = _agg(ctdo_learner=1, ctdo_feed=3)
    vs = build_verdicts(agg, now_day="2026-09-01", epoch_day="2026-08-16",
                        closes=None, min_days=14, min_total=20)
    q3 = next(v for v in vs if v["question"] == "结案喂料桥采纳")
    assert "分母不可读" in q3["recommendation"]


def test_verdicts_alertlink_background():
    agg = _agg(ctdo_learner=1, ctdo_alertlink_shown=9, ctdo_alertlink_dismiss=2)
    vs = build_verdicts(agg, now_day="2026-09-01", epoch_day="2026-08-16",
                        min_days=14, min_total=20)
    q4 = next(v for v in vs if "告警引导条" in v["question"])
    assert "现身 9 次" in q4["recommendation"] and "被关 2 次" in q4["recommendation"]


# ── 3. CLI 只读 IO ──────────────────────────────────────────────────────────

def test_read_rows_prefix_filter_and_missing(tmp_path):
    db = tmp_path / "ui_event_trend.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE ui_event_trend_daily (day TEXT, action TEXT, n INTEGER, "
                 "PRIMARY KEY (day, action))")
    conn.execute("INSERT INTO ui_event_trend_daily VALUES ('2026-08-16','ctdo_feed',2)")
    conn.execute("INSERT INTO ui_event_trend_daily VALUES ('2026-08-16','iflt_claimed',9)")
    conn.commit()
    conn.close()
    rows = read_rows(db, days=30, now=time.mktime((2026, 8, 17, 12, 0, 0, 0, 0, -1)))
    assert [r["action"] for r in rows] == ["ctdo_feed"]

    import pytest as _pytest
    with _pytest.raises(FileNotFoundError):
        read_rows(tmp_path / "nope.db", days=30)


def test_read_closes_sums_window_or_none(tmp_path):
    assert read_closes(tmp_path / "nope.db") is None
    db = tmp_path / "cases_trend.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE case_trend_daily (day TEXT PRIMARY KEY, opened INTEGER, "
                 "closed INTEGER, by_source TEXT)")
    conn.execute("INSERT INTO case_trend_daily VALUES ('2026-08-16', 3, 2, '{}')")
    conn.execute("INSERT INTO case_trend_daily VALUES ('2026-08-15', 1, 5, '{}')")
    conn.commit()
    conn.close()
    n = read_closes(db, days=30, now=time.mktime((2026, 8, 17, 12, 0, 0, 0, 0, -1)))
    assert n == 7


# ── 3.5 周批固化（P4）：效果快照 + 趋势行 ───────────────────────────────────

def _mk_kb_db(tmp_path, *, drafts=(), hits=(), extra_log_rows=0):
    """单文件 knowledge_base.db（生产同构：kb_drafts 与 kb_query_log 同库）。"""
    db = tmp_path / "knowledge_base.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE kb_drafts (id TEXT PRIMARY KEY, status TEXT, "
              "entry_id TEXT DEFAULT '', reviewed_at TEXT DEFAULT '')")
    c.execute("CREATE TABLE kb_query_log (id TEXT PRIMARY KEY, hit INTEGER, "
              "matched_entry_id TEXT, ts REAL)")
    for i, (eid, reviewed) in enumerate(drafts):
        c.execute("INSERT INTO kb_drafts VALUES (?,?,?,?)",
                  (f"d{i}", "approved", eid, reviewed))
    for i, eid in enumerate(hits):
        c.execute("INSERT INTO kb_query_log VALUES (?,?,?,?)",
                  (f"h{i}", 1, eid, NOW - 1 * DAY))
    for i in range(extra_log_rows):
        c.execute("INSERT INTO kb_query_log VALUES (?,?,?,?)",
                  (f"m{i}", 0, "", NOW - 1 * DAY))
    c.commit()
    c.close()
    return db


def test_read_effect_counts_and_zero_hit(tmp_path):
    from tools.fusion_adoption_report import read_effect
    db = _mk_kb_db(
        tmp_path,
        drafts=[("e-a", _iso(NOW - 1 * DAY)),    # 本周入库，有命中
                ("e-b", _iso(NOW - 5 * DAY)),    # 老条目，零命中 → zero_hit_aged
                ("e-c", _iso(NOW - 10 * DAY))],  # 更老，有命中
        hits=["e-a", "e-a", "e-c"])
    eff = read_effect(db, now=NOW)
    assert eff["learner_entries"] == 3
    assert eff["approved_7d"] == 2          # e-a + e-b（e-c 超 7 天窗）
    assert eff["hits_on_learner_7d"] == 3   # e-a×2 + e-c×1
    assert eff["zero_hit_aged"] == 1        # 只有 e-b（e-a 太新不计龄? e-a 1天<3天 且有命中）


def test_read_effect_zero_traffic_gives_null_not_zero(tmp_path):
    """日志零流量 → zero_hit_aged=None（不可证），绝不伪装成数字（防冤枉闸同源）。"""
    from tools.fusion_adoption_report import read_effect
    db = _mk_kb_db(tmp_path, drafts=[("e-a", _iso(NOW - 5 * DAY))], hits=[])
    eff = read_effect(db, now=NOW)
    assert eff["query_log_rows"] == 0
    assert eff["zero_hit_aged"] is None


def test_read_effect_missing_or_old_db_is_none(tmp_path):
    from tools.fusion_adoption_report import read_effect
    assert read_effect(tmp_path / "nope.db") is None
    old = tmp_path / "old.db"
    sqlite3.connect(str(old)).close()   # 空库无表 → 软 None
    assert read_effect(old) is None


def test_append_trend_lines_shape(tmp_path):
    import json as _json

    from tools.fusion_adoption_report import append_trend_lines
    out = tmp_path / "trend" / "fusion.jsonl"
    reports = [
        {"root": "r1", "grand_total": 5, "totals": {"ctdo_feed": 5},
         "closes_in_window": 12, "window_days": 30,
         "effect": {"learner_entries": 2}},
        {"root": "r2", "missing": True},
    ]
    n = append_trend_lines(reports, out, now=NOW)
    assert n == 2
    lines = [_json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["root"] == "r1" and lines[0]["totals"] == {"ctdo_feed": 5}
    assert lines[0]["effect"] == {"learner_entries": 2}
    assert lines[1]["missing"] is True
    # 追加语义：再写一轮变 4 行
    append_trend_lines(reports, out, now=NOW + 86400)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 4


# ── 4. 前端接线静态钉 ───────────────────────────────────────────────────────

def test_learner_page_renders_zero_hit_advice():
    src = (_TPL / "learner.html").read_text(encoding="utf-8")
    assert "zero_hit" in src and "lr2_effect_zero" in src
    assert "/knowledge#entry-" in src


def test_cases_alertlink_nudge_wired():
    src = (_TPL / "cases.html").read_text(encoding="utf-8")
    assert 'id="cs-alertlink-nudge"' in src
    assert "/api/admin/alert-link-status" in src
    assert "no_channel" in src, "引导条必须窄触发（仅 0 通道）"
    assert "csAlertNudgeDismiss" in src
    assert "ctdo_alertlink_shown" in src and "ctdo_alertlink_dismiss" in src


def test_cases_empty_state_learner_nudge_wired():
    src = (_TPL / "cases.html").read_text(encoding="utf-8")
    assert 'id="cs-empty-learn"' in src
    assert "_csPaintEmptyLearn" in src
    assert "window.__csTodo" in src, "空态引导必须复用待办条快照（零新请求）"
    assert "ck.empty.learner" in src, "与驾驶舱空态同款词条（不造同义新键）"


def test_zero_hit_keys_bilingual():
    from src.web.web_i18n import get_translations
    zh, en = get_translations("zh"), get_translations("en")
    for k in ("lr2_effect_zero", "cases.alertlink.body",
              "cases.alertlink.cta", "cases.alertlink.dismiss"):
        assert zh.get(k), f"zh 缺键 {k}"
        assert en.get(k), f"en 缺键 {k}"
