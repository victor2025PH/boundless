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


def test_stats_snapshot_shape_and_zero_state(monkeypatch, tmp_path):
    monkeypatch.setenv(shadow_log.ENV_DIR, str(tmp_path / "s"))
    shadow_log.get_stats().reset()
    snap = shadow_log.stats_snapshot()
    for k in ("total", "today", "by_reason", "by_level", "by_stage", "top_hits",
              "stop_contact", "self_harm", "alerts", "write_errors", "last_ts", "dir"):
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


def test_ops_card_i18n_keys_bilingual():
    from src.web.i18n_packs.ops_overview_page import EN, ZH
    keys = ("ov2_s_ashadow", "ov2_ashadow_hint", "ov2_js_ash_total", "ov2_js_ash_today",
            "ov2_js_ash_stop", "ov2_js_ash_harm", "ov2_js_ash_last", "ov2_js_ash_by_reason",
            "ov2_js_ash_top_hits", "ov2_js_ash_col_reason", "ov2_js_ash_col_hit",
            "ov2_js_ash_col_count", "ov2_hidb_ashadow")
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
    # 渲染四段标题齐全
    out = rpt.render(s, days=30, dirs=[tmp_path], reason="")
    for tag in ("[1]", "[2]", "[3]", "[4]"):
        assert tag in out


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
