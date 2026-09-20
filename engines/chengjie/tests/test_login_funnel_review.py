"""账号接入漏斗周报 CLI 门禁（纯函数 + 只读库 + 模板接线）。"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from scripts.login_funnel_review import (
    collect_review,
    load_daily,
    render_review,
    render_trend,
    summarize,
    verdicts,
)


def _seed_db(path: Path, rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE login_funnel_trend_daily (
            day TEXT PRIMARY KEY,
            started INTEGER NOT NULL DEFAULT 0,
            authorized INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE login_funnel_reason_daily (
            day TEXT NOT NULL,
            reason TEXT NOT NULL,
            n INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, reason)
        );
        """
    )
    for day, started, authorized, failed, reasons in rows:
        conn.execute(
            "INSERT INTO login_funnel_trend_daily VALUES (?,?,?,?)",
            (day, started, authorized, failed),
        )
        for reason, n in (reasons or {}).items():
            conn.execute(
                "INSERT INTO login_funnel_reason_daily VALUES (?,?,?)",
                (day, reason, n),
            )
    conn.commit()
    conn.close()


def test_summarize_and_verdicts_checkpoint_heavy():
    rows = [
        {"day": "d1", "started": 10, "authorized": 1, "failed": 8,
         "by_reason": {"checkpoint": 6, "session_timeout": 2}},
    ]
    s = summarize(rows)
    assert s["started"] == 10
    assert s["success_rate"] == 0.1
    assert s["top_reasons"][0]["reason"] == "checkpoint"
    vs = verdicts(s, None)
    assert any("checkpoint" in v for v in vs)
    assert any("偏低" in v for v in vs)


def test_verdicts_rate_recovery():
    prev = {"started": 10, "authorized": 2, "failed": 8, "success_rate": 0.2,
            "reasons": {"checkpoint": 5}}
    cur = {"started": 10, "authorized": 7, "failed": 3, "success_rate": 0.7,
           "reasons": {"checkpoint": 1}}
    vs = verdicts(cur, prev)
    assert any("回升" in v for v in vs)


def test_load_daily_pads_and_collect(tmp_path):
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    root = tmp_path / "data"
    _seed_db(root / "config" / "login_funnel_trend.db", [
        (day, 3, 1, 1, {"two_factor": 1}),
    ])
    rows = load_daily(root / "config" / "login_funnel_trend.db", days=3, now=now)
    assert len(rows) == 3
    assert rows[-1]["started"] == 3
    assert rows[-1]["by_reason"].get("two_factor") == 1
    rep = collect_review(root, days=3, now=now)
    assert rep["enabled"] is True
    assert rep["summary"]["started"] == 3
    text = render_review(rep)
    assert "成功率" in text
    assert "two_factor" in text


def test_collect_disabled_when_no_db(tmp_path):
    rep = collect_review(tmp_path / "empty", days=7)
    assert rep["enabled"] is False
    assert "未启用" in render_review(rep)


def test_render_trend_empty_and_with_rows():
    assert "为空" in render_trend([])
    rows = [
        {"ts": 1, "summary": {"started": 5, "authorized": 1, "failed": 3,
                              "success_rate": 0.2,
                              "top_reasons": [{"reason": "checkpoint", "n": 2}],
                              "reasons": {"checkpoint": 2}}},
        {"ts": 2, "summary": {"started": 5, "authorized": 4, "failed": 1,
                              "success_rate": 0.8,
                              "top_reasons": [],
                              "reasons": {}}},
    ]
    text = render_trend(rows)
    assert "回升" in text


def test_messenger_default_instruction_not_qr_or_device_cast():
    """Messenger 默认指引不得再写投屏/扫码——那是 hosted 事故复现位。"""
    from src.integrations import platform_login as pl
    from src.web.web_i18n import get_translations

    instr = pl.PLATFORM_INSTRUCTIONS["messenger"]
    assert "扫码" not in instr
    assert "投屏" not in instr
    assert "服务器" in instr
    for lang in ("zh", "en"):
        t = get_translations(lang)["inbox.connect.instr_messenger"]
        low = t.lower()
        assert "扫码" not in t
        assert "投屏" not in t
        assert "qr" not in low or "no qr" in low or "not" in low
        # EN 允许出现 “No QR code…” 否定句
        if lang == "zh":
            assert "服务器" in t


def test_messenger_desc_does_not_claim_page_inbox():
    from src.web.web_i18n import get_translations
    zh = get_translations("zh")["inbox.plat.messenger_desc"]
    en = get_translations("en")["inbox.plat.messenger_desc"]
    assert "非主页私信" in zh
    assert "page inbox" in en.lower()


def test_ops_template_wires_alert_outlet_hint():
    html = (Path(__file__).resolve().parents[1]
            / "src" / "web" / "templates" / "ops_overview.html").read_text(
                encoding="utf-8")
    assert "ov2_js_lf_alert_outlet" in html
    assert "ov2_js_lf_alert_uncovered" in html
    assert "login_funnel" in html
