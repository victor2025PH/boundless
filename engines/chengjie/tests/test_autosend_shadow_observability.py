# -*- coding: utf-8 -*-
"""#160 I-1-C 影子台账观测面门禁（2026-09-04）：metrics 段 / ops 卡三件套 / 告警别名链 / 读数 CLI。

台账本体（JSONL 落盘、去重、不记原文）在 ``test_drafts_risk_policy.py`` 第二层；本文件钉的是
「一个月后有没有地方读到它」：metrics 键在、卡片注册在、别名能订阅、CLI 能算出四段。
三件套少任何一件都是静默缺陷（有 section 没注册＝永远 loading；有 loader 没 section＝白请求）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.inbox import autosend_shadow_log as shadow_log

_ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── metrics 段 ─────────────────────────────────────────────────────────

def test_workspace_metrics_wires_autosend_shadow():
    src = _read("src/web/routes/drafts_routes.py")
    assert 'metrics["autosend_shadow"]' in src
    assert "autosend_shadow_log import stats_snapshot" in src


@pytest.fixture
def clean_ledger(monkeypatch, tmp_path):
    d = tmp_path / "s"
    monkeypatch.setenv(shadow_log.ENV_DIR, str(d))
    shadow_log.get_stats().reset()
    shadow_log._reset_pending_for_tests()
    return d


def test_stats_snapshot_shape_and_zero_state(clean_ledger):
    snap = shadow_log.stats_snapshot()
    for k in ("total", "today", "by_reason", "by_level", "by_stage", "top_hits",
              "stop_contact", "self_harm", "alerts", "write_errors", "last_ts", "dir",
              "outcomes", "settled", "sent", "sent_rate", "pending_outcomes", "warmed_from"):
        assert k in snap, k
    assert snap["total"] == 0 and snap["today"] == 0        # 零命中 → 卡片隐藏口径
    rec = shadow_log.build_record(
        platform="telegram", account_id="a", conv_key="c", draft_id="d",
        would_hold_level="L4", hold_reason="money", peer_risk="high",
        peer_reasons=["money"], reply_risk="low", reply_reasons=[],
        risk_hits=["bank card"], text="hello", stage="peer")
    assert shadow_log.record(rec) is True
    snap = shadow_log.stats_snapshot()
    assert snap["total"] == 1 and snap["today"] == 1
    assert snap["by_reason"] == {"money": 1} and snap["by_level"] == {"L4": 1}
    assert snap["top_hits"] == [{"hit": "bank card", "n": 1}]
    assert snap["stop_contact"] == 0 and snap["self_harm"] == 0
    assert snap["pending_outcomes"] == 1 and snap["settled"] == 0 and snap["sent_rate"] is None


# ── 放行后的去向（outcome）─────────────────────────────────────────────

_NOW = 1_800_000_000.0


def test_classify_outcome_matrix():
    c = shadow_log.classify_outcome
    hold = _NOW - 600
    assert c({"sent_at": _NOW - 10, "status": "approved", "decided_by": "autosend_worker"}, hold, now=_NOW) == ("sent", "autosend_worker")
    assert c({"sent_at": 0, "status": "cancelled", "decided_by": "send_blocked"}, hold, now=_NOW) == ("cancelled", "send_blocked")
    assert c({"sent_at": 0, "status": "cancelled", "decided_by": "channel_mutex"}, hold, now=_NOW) == ("cancelled", "channel_mutex")
    assert c({"sent_at": 0, "status": "rejected", "decided_by": "agent7"}, hold, now=_NOW) == ("rejected", "agent7")
    # approved 但没 sent_at：宽限内在途（拟人延迟窗），超宽限 → approved_unsent
    row = {"sent_at": 0, "status": "approved", "decided_by": "autosend_worker", "decided_at": _NOW - 60}
    assert c(row, hold, now=_NOW) == ("", "")
    row_old = {"sent_at": 0, "status": "approved", "decided_by": "autosend_worker",
               "decided_at": _NOW - 3600, "error": "UndeliveredError: send not ok"}
    assert c(row_old, hold, now=_NOW) == ("approved_unsent", "UndeliveredError: send not ok")
    # 仍 pending：不到 max_age 继续等；超龄 → missing
    assert c({"sent_at": 0, "status": "pending"}, hold, now=_NOW) == ("", "")
    assert c({"sent_at": 0, "status": "pending"}, _NOW - 8 * 86400, now=_NOW) == ("missing", "stale_pending")
    # 行没了（被 cleanup 清理）→ missing；刚写完那一瞬读不到不算
    assert c(None, hold, now=_NOW) == ("missing", "row_gone")
    assert c(None, _NOW - 5, now=_NOW) == ("", "")


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get_draft(self, did):
        self.calls += 1
        return self.rows.get(did)


def _hold(did, reason="stop_contact", ts=None):
    return shadow_log.build_record(
        platform="telegram", account_id="a1", conv_key="c-" + did, draft_id=did,
        would_hold_level="L4", hold_reason=reason, peer_risk="high", peer_reasons=[reason],
        reply_risk="low", reply_reasons=[], risk_hits=["x"], text="t", stage="peer",
        ts=ts if ts is not None else _NOW - 600)


def test_reconcile_writes_outcomes_and_settles_pending(clean_ledger):
    for did in ("d-sent", "d-cancel", "d-wait", "d-gone"):
        shadow_log.record(_hold(did))
    assert shadow_log.pending_count() == 4
    store = _FakeStore({
        "d-sent": {"sent_at": _NOW - 5, "status": "approved", "decided_by": "autosend_worker"},
        "d-cancel": {"sent_at": 0, "status": "cancelled", "decided_by": "mode_downgraded"},
        "d-wait": {"sent_at": 0, "status": "approved", "decided_by": "autosend_worker", "decided_at": _NOW - 30},
        # d-gone → None
    })
    n = shadow_log.reconcile_outcomes(store, now=_NOW)
    assert n == 3                                   # sent / cancelled / missing；d-wait 在途继续等
    assert shadow_log.pending_count() == 1
    snap = shadow_log.stats_snapshot()
    assert snap["outcomes"] == {"sent": 1, "cancelled": 1, "missing": 1}
    assert snap["settled"] == 3 and snap["sent"] == 1 and snap["sent_rate"] == 0.333
    rows = [json.loads(l) for p in clean_ledger.glob("*.jsonl")
            for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    outs = [r for r in rows if r.get("kind") == "outcome"]
    assert len(outs) == 3
    for o in outs:
        for k in shadow_log.OUTCOME_FIELDS:
            assert k in o, k
    by = {o["draft_id"]: o for o in outs}
    assert by["d-cancel"]["reason"] == "mode_downgraded" and by["d-cancel"]["hold_reason"] == "stop_contact"
    assert by["d-sent"]["latency_sec"] == 600.0
    # 第二轮：d-wait 超宽限 → approved_unsent，注销
    n2 = shadow_log.reconcile_outcomes(store, now=_NOW + 3600)
    assert n2 == 1 and shadow_log.pending_count() == 0
    assert shadow_log.stats_snapshot()["outcomes"]["approved_unsent"] == 1
    # 幂等：没有待终局时不查库
    calls = store.calls
    assert shadow_log.reconcile_outcomes(store, now=_NOW + 7200) == 0
    assert store.calls == calls


def test_reconcile_tolerates_no_store_and_store_errors(clean_ledger):
    shadow_log.record(_hold("d1"))
    assert shadow_log.reconcile_outcomes(None) == 0
    assert shadow_log.reconcile_outcomes(object()) == 0          # 没有 get_draft

    class _Boom:
        def get_draft(self, did):
            raise RuntimeError("db locked")
    assert shadow_log.reconcile_outcomes(_Boom(), now=_NOW) == 0
    assert shadow_log.pending_count() == 1                       # 异常不注销，下轮再试


def test_warm_from_disk_restores_counters_and_pending(clean_ledger):
    """重启后：计数器与待终局登记从 JSONL 回填；已有 outcome 的不再 pending。"""
    now = time.time()
    shadow_log.record(_hold("d-a", ts=now - 100))
    shadow_log.record(_hold("d-b", reason="self_harm", ts=now - 90))
    store = _FakeStore({"d-a": {"sent_at": now - 50, "status": "approved", "decided_by": "autosend_worker"},
                        "d-b": {"sent_at": 0, "status": "pending"}})      # 还在队列里
    assert shadow_log.reconcile_outcomes(store, now=now) == 1
    # 模拟重启：清进程态，再首次读 snapshot
    shadow_log.get_stats().reset()
    shadow_log._reset_pending_for_tests()
    snap = shadow_log.stats_snapshot()
    assert snap["warmed_from"] == "3d"
    assert snap["total"] == 2 and snap["today"] == 2
    assert snap["by_reason"] == {"stop_contact": 1, "self_harm": 1}
    assert snap["outcomes"] == {"sent": 1} and snap["pending_outcomes"] == 1
    assert shadow_log.pending_count() == 1


def test_reconcile_end_to_end_with_real_store(clean_ledger, tmp_path, monkeypatch):
    """真库走一遍：放行 → worker 同款 resolve(autosend)+mark_draft_sent → sent；
    另一稿被 worker 守卫取消（update_draft_status cancelled/send_blocked）→ cancelled。
    钉的是 get_draft 真实返回的键名（sent_at/status/decided_by/decided_at），假件测不出。"""
    from src.ai.chat_assistant_service import quick_risk
    from src.inbox.drafts import DraftService
    from src.inbox.store import InboxStore

    monkeypatch.delenv("AITR_AUTOSEND_POLICY_MODE", raising=False)
    store = InboxStore(tmp_path / "e2e.db")
    try:
        svc = DraftService(inbox_store=store, risk_fn=quick_risk)

        def conv(k):
            return {"conversation_id": f"telegram:acct1:{k}", "platform": "telegram",
                    "account_id": "acct1", "chat_key": k, "display_name": "T"}
        d_sent = svc.auto_generate_draft(conv("u1"), "please stop messaging me", automation_mode="auto_ai")
        d_cancel = svc.auto_generate_draft(conv("u2"), "I want to kill myself", automation_mode="auto_ai")
        d_wait = svc.auto_generate_draft(conv("u3"), "send me your bank card number", automation_mode="auto_ai")
        assert shadow_log.pending_count() == 3
        # 首轮：三稿都还 pending → 无终局
        assert svc.reconcile_shadow_outcomes() == 0
        # worker 同款：resolve(autosend) → 投递成功 mark_draft_sent
        r = svc.resolve_with_audit(d_sent, "autosend", by="autosend_worker")
        assert r.get("ok"), r
        assert store.mark_draft_sent(d_sent)
        # worker 守卫取消（send_blocked 路径写法）
        store.update_draft_status(d_cancel, status="cancelled", decided_by="send_blocked")
        n = svc.reconcile_shadow_outcomes()
        assert n == 2
        assert shadow_log.pending_count() == 1                 # d_wait 仍 pending
        snap = shadow_log.stats_snapshot()
        assert snap["outcomes"] == {"sent": 1, "cancelled": 1}
        assert snap["sent_rate"] == 0.5
        rows = [json.loads(l) for p in clean_ledger.glob("*.jsonl")
                for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        outs = {r["draft_id"]: r for r in rows if r.get("kind") == "outcome"}
        assert outs[d_sent]["outcome"] == "sent" and outs[d_sent]["hold_reason"] == "stop_contact"
        assert outs[d_cancel]["outcome"] == "cancelled" and outs[d_cancel]["reason"] == "send_blocked"
        assert d_wait not in outs
        # hold 行带二批字段（真链路口径）
        holds = {r["draft_id"]: r for r in rows if r.get("kind", "hold") == "hold"}
        assert holds[d_sent]["lang"] == "en" and holds[d_sent]["intent"] == "停止联系"
        assert holds[d_sent]["peer_text_fp"] == shadow_log.text_fingerprint("please stop messaging me")
    finally:
        store.close()


def test_worker_and_service_wired_to_reconcile():
    worker = _read("src/inbox/autosend_worker.py")
    assert 'getattr(self._svc, "reconcile_shadow_outcomes", None)' in worker
    assert "run_in_executor(None, _recon)" in worker
    drafts = _read("src/inbox/drafts.py")
    assert "def reconcile_shadow_outcomes(self" in drafts
    assert "_shadow_log.reconcile_outcomes(self._store" in drafts
    from src.inbox.drafts import DraftService
    svc = DraftService(inbox_store=None)
    assert svc.reconcile_shadow_outcomes() == 0                  # 无 store → 0，不抛


# ── ops-overview 卡三件套 ──────────────────────────────────────────────

def test_ops_card_renders_and_registered():
    src = _read("src/web/templates/ops_overview.html")
    assert 'id="autosendShadowSection"' in src
    assert "function renderAutosendShadow(d)" in src
    assert "d.autosend_shadow" in src                      # loader 数据源（metrics 段）
    assert "anchor:'autosendShadowKpis'" in src            # 卡片注册表登记
    assert "renderAutosendShadow(d);" in src               # 正常分支派发
    assert "renderAutosendShadow(null);" in src            # 403 分支派发
    # 零命中整卡隐藏（结构化原因 healthy）+ 命中词经转义再拼 HTML
    assert "opsHideCardEl(sec, 'ashadow', 'healthy')" in src
    assert "ashadow:  {reason:'healthy'}" in src
    assert "_esc(h && h.hit)" in src
    # 放行后的去向：已送达/已终局 KPI + 去向表 + 回填提示
    assert "sh.settled" in src and "sh.pending_outcomes" in src and "sh.outcomes" in src
    assert "sh.warmed_from" in src


def test_ops_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH
    keys = ("ov2_s_ashadow", "ov2_ashadow_hint", "ov2_js_ash_total", "ov2_js_ash_today",
            "ov2_js_ash_stop", "ov2_js_ash_harm", "ov2_js_ash_last", "ov2_js_ash_by_reason",
            "ov2_js_ash_top_hits", "ov2_js_ash_col_reason", "ov2_js_ash_col_hit",
            "ov2_js_ash_col_count", "ov2_hidb_ashadow",
            "ov2_js_ash_sent", "ov2_js_ash_pending", "ov2_js_ash_outcomes",
            "ov2_js_ash_col_outcome", "ov2_js_ash_warm_note")
    for k in keys:
        assert ZH.get(k), k
        assert EN.get(k), k


# ── 告警别名链（不拦只报） ─────────────────────────────────────────────

def test_alert_alias_chain_registered():
    from src.inbox.webhook_notifier import (
        _BUSINESS_ALERTS, _CARD_META, _EVENT_ALIASES, _build_message, alert_audience,
    )
    assert _EVENT_ALIASES["autosend_shadow"]["types"] == {"autosend_shadow_alert"}
    assert alert_audience("autosend_shadow") == "business"
    assert _BUSINESS_ALERTS["autosend_shadow"] == "cp.alert.autosend_shadow"
    assert "autosend_shadow_alert" in _CARD_META
    title, text = _build_message("autosend_shadow_alert", {
        "reason": "stop_contact", "platform": "telegram", "account_id": "a1",
        "conv_key": "6834964252", "draft_id": "inbox:x", "would_hold_level": "L4",
        "risk_hits": ["stop messaging me"], "rate_key": "a1:6834964252",
    })
    assert title != "[autosend_shadow_alert] 事件"          # 有专属分支，不落通用兜底
    assert "6834964252" in text and "stop messaging me" in text
    assert "已放行" in text and "人工介入" in text
    title2, text2 = _build_message("autosend_shadow_alert", {"reason": "self_harm", "risk_hits": []})
    assert "自伤" in title2 and "safety net" in text2


def test_alert_label_in_cp_i18n_both_trees_bilingual():
    for rel in ("shared/copilot/i18n/cp-i18n.js",
                "desktop/renderer/shared/copilot/i18n/cp-i18n.js"):
        src = _read(rel)
        assert src.count('"cp.alert.autosend_shadow"') == 2, rel   # reg(zh, en) 两侧
    assert '"cp.alert.autosend_shadow"' in _read("shared/copilot/i18n/cp-i18n-ext.zh_hant.js")


def test_alert_publish_point_is_in_ledger_module_only():
    """发布点只在台账模块（单点），且 rate_key 按 account:conv。"""
    src = _read("src/inbox/autosend_shadow_log.py")
    assert 'publish("autosend_shadow_alert"' in src
    assert '"rate_key": f"{acct}:{conv}"' in src
    assert 'publish("autosend_shadow_alert"' not in _read("src/inbox/drafts.py")


# ── 读数 CLI ──────────────────────────────────────────────────────────

def _write_ledger(d: Path, rows):
    d.mkdir(parents=True, exist_ok=True)
    by_day = {}
    for r in rows:
        day = time.strftime("%Y%m%d", time.localtime(r["ts"]))
        by_day.setdefault(day, []).append(r)
    for day, rs in by_day.items():
        with open(d / f"shadow_{day}.jsonl", "a", encoding="utf-8") as fh:
            for r in rs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _rec(ts, reason, hits, acct="a1", level="L4", stage="peer", conv="c1", did="d"):
    return shadow_log.build_record(
        platform="telegram", account_id=acct, conv_key=conv, draft_id=did,
        would_hold_level=level, hold_reason=reason, peer_risk="high",
        peer_reasons=[reason], reply_risk="low", reply_reasons=[],
        risk_hits=hits, text="x", stage=stage, ts=ts)


def test_cli_summarize_four_sections(tmp_path):
    import tools.autosend_shadow_report as rpt
    now = time.time()
    rows = [
        _rec(now - 60, "stop_contact", ["stop messaging me"], conv="c1", did="d1"),
        _rec(now - 120, "stop_contact", ["stop messaging me"], acct="a2", conv="c2", did="d2"),
        _rec(now - 180, "self_harm", ["kill myself"], conv="c3", did="d3"),
        _rec(now - 240, "reply_risk", ["transfer to"], level="L4", stage="reply", conv="c4", did="d4"),
        _rec(now - 86400 * 40, "money", ["bank card"], conv="old", did="old"),   # 超窗，不该算
    ]
    _write_ledger(tmp_path, rows)
    recs = rpt.collect(30, dirs=[tmp_path], now=now)
    assert len(recs) == 4
    s = rpt.summarize(recs, samples=20)
    assert s["total"] == 4
    assert s["by_reason"] == {"stop_contact": 2, "self_harm": 1, "reply_risk": 1}
    assert s["stop_contact"] == 2 and s["self_harm"] == 1
    assert s["by_account"] == {"telegram:a1": 3, "telegram:a2": 1}
    assert s["top_hits"][0] == {"hit": "stop messaging me", "n": 2}   # 哪个正则在响
    assert {x["draft_id"] for x in s["samples"]} == {"d1", "d2", "d3", "d4"}
    # --reason 过滤
    only = rpt.collect(30, dirs=[tmp_path], reason="self_harm", now=now)
    assert [r["draft_id"] for r in only] == ["d3"]
    # 渲染五段标题齐全（无 outcome 行时 1b 给出解释而非空白）
    out = rpt.render(s, days=30, dirs=[tmp_path], reason="")
    for tag in ("[1]", "[1b]", "[2]", "[3]", "[4]"):
        assert tag in out
    assert "无 outcome 行" in out


def test_cli_joins_outcomes_by_draft(tmp_path):
    """放行后的去向：outcome 行按 draft_id 关联 hold 行，算送达率/未送达明细/待终局。"""
    import tools.autosend_shadow_report as rpt
    now = time.time()
    holds = [
        _rec(now - 300, "stop_contact", ["stop messaging me"], conv="c1", did="d1"),
        _rec(now - 280, "stop_contact", ["stop messaging me"], conv="c2", did="d2"),
        _rec(now - 260, "money", ["bank card"], conv="c3", did="d3"),
        _rec(now - 240, "money", ["bank card"], conv="c4", did="d4"),   # 无 outcome → pending
    ]
    outs = [
        {"kind": "outcome", "ts": now - 200, "draft_id": "d1", "outcome": "sent", "reason": "autosend_worker",
         "hold_reason": "stop_contact", "hold_ts": now - 300, "latency_sec": 100.0,
         "platform": "telegram", "account_id": "a1", "conv_key": "c1"},
        {"kind": "outcome", "ts": now - 190, "draft_id": "d2", "outcome": "cancelled", "reason": "send_blocked",
         "hold_reason": "stop_contact", "hold_ts": now - 280, "latency_sec": 90.0,
         "platform": "telegram", "account_id": "a1", "conv_key": "c2"},
        {"kind": "outcome", "ts": now - 180, "draft_id": "d3", "outcome": "sent", "reason": "autosend_worker",
         "hold_reason": "money", "hold_ts": now - 260, "latency_sec": 80.0,
         "platform": "telegram", "account_id": "a1", "conv_key": "c3"},
    ]
    _write_ledger(tmp_path, holds + outs)
    recs = rpt.collect(30, dirs=[tmp_path], now=now)
    assert len(recs) == 7
    s = rpt.summarize(recs)
    assert s["total"] == 4                                   # hold 行数不被 outcome 行污染
    assert s["outcomes"] == {"sent": 2, "cancelled": 1}
    assert s["settled"] == 3 and s["pending_outcome"] == 1 and s["sent_rate"] == 0.667
    assert s["outcomes_by_reason"]["stop_contact"] == {"sent": 1, "cancelled": 1}
    assert s["outcomes_by_reason"]["money"] == {"sent": 1, "pending": 1}
    assert s["cancel_reasons"] == {"cancelled:send_blocked": 1}
    assert s["outcome_latency_p50_sec"] == 90.0
    out = rpt.render(s, days=30, dirs=[tmp_path], reason="")
    assert "送达率 0.667" in out and "cancelled:send_blocked" in out
    # --reason 过滤对 outcome 行同样生效（outcome 行带 hold_reason）
    only = rpt.collect(30, dirs=[tmp_path], reason="money", now=now)
    assert sorted(r["draft_id"] for r in only) == ["d3", "d3", "d4"]


def test_cli_main_json_and_empty(tmp_path, capsys):
    import tools.autosend_shadow_report as rpt
    rc = rpt.main(["--dir", str(tmp_path / "nothing"), "--json", "--days", "1"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 0 and data["days"] == 1
    rc = rpt.main(["--dir", str(tmp_path / "nothing"), "--days", "1"])
    assert rc == 0
    assert "零命中" in capsys.readouterr().out


def test_cli_reads_via_iter_records_skips_bad_lines(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    day = time.strftime("%Y%m%d", time.localtime())
    (d / f"shadow_{day}.jsonl").write_text(
        json.dumps(_rec(time.time(), "money", ["bank card"])) + "\nnot json\n\n", encoding="utf-8")
    recs = list(shadow_log.iter_records(1, base=d))
    assert len(recs) == 1 and recs[0]["hold_reason"] == "money"
