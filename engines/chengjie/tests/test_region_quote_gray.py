"""I-4 灰度账本（src/ops/region_quote_gray）：落盘/关闭/遍历/汇总判词 + 两个生产者接线。"""
from __future__ import annotations

import json
import time

import pytest

from src.ops import region_quote_gray as g


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv(g.ENV_DIR, str(tmp_path / "gray"))
    return tmp_path / "gray"


def test_append_writes_jsonl_and_iter_reads_back(ledger):
    assert g.append("quote", {"quote": True, "score": 0.5})
    assert g.append("region", {"region": "zh-HK", "banned": ["咱"], "script": []})
    assert not g.append("nope", {"x": 1})            # 未知 kind 拒绝
    files = sorted(p.name for p in ledger.glob("*.jsonl"))
    assert len(files) == 2 and files[0].startswith("quote_") and files[1].startswith("region_")
    recs = list(g.iter_records("quote", 3))
    assert recs and recs[0]["quote"] is True and "ts" in recs[0]
    # 坏行跳过
    with open(next(ledger.glob("quote_*.jsonl")), "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert len(list(g.iter_records("quote", 3))) == 1


def test_env_off_disables_everything(tmp_path, monkeypatch):
    monkeypatch.setenv(g.ENV_DIR, "off")
    assert g.gray_dir() is None
    assert not g.append("quote", {"quote": True})
    assert list(g.iter_records("quote", 3)) == []


def test_iter_records_respects_day_window(ledger):
    now = time.time()
    old = now - 10 * 86400
    g.append("quote", {"quote": False, "reason": "single_inbound", "ts": old})
    g.append("quote", {"quote": True, "ts": now})
    assert len(list(g.iter_records("quote", 3, now=now))) == 1
    assert len(list(g.iter_records("quote", 30, now=now))) == 2


def test_summarize_quote_gate_and_suggestion():
    # 样本不足：不给建议
    recs = [{"quote": False, "reason": "low_relevance", "score": 0.2}] * 5
    s = g.summarize_quote(recs, cur_min_relevance=0.35)
    assert s["decided"] == 5 and s["suggest_min_relevance"] is None
    assert s["sample_gate_ok"] is False and s["skipped"] == {"low_relevance": 5}
    assert s["low_relevance_hist"] == {"0.20": 5}
    # 样本足：25 条 low_relevance 分布 0.10..0.34 + 5 条已引用 0.4 → 建议下调到落 25% 分位
    recs = [{"quote": False, "reason": "low_relevance", "score": 0.10 + i * 0.01} for i in range(25)]
    recs += [{"quote": True, "score": 0.4}] * 5
    recs += [{"quote": False, "reason": "single_inbound"}] * 10
    recs += [{"applied": True}, {"fallback_plain": True}]     # 回执行不计 decided
    s = g.summarize_quote(recs, cur_min_relevance=0.35)
    assert s["decided"] == 40 and s["quoted"] == 5
    assert s["applied"] == 1 and s["fallback_plain"] == 1
    assert s["sample_gate_ok"] is True
    assert s["suggest_min_relevance"] is not None
    assert 0.1 <= s["suggest_min_relevance"] < 0.35
    # 只建议下调：若当前阈值已低于分位值则不建议
    s2 = g.summarize_quote(recs, cur_min_relevance=0.10)
    assert s2["suggest_min_relevance"] is None


def test_summarize_region_l4_verdict():
    recs = [{"region": "zh-HK", "banned": [], "script": []}] * 27
    recs += [{"region": "zh-HK", "banned": ["咱"], "script": ["这"]}] * 3
    recs += [{"region": "zh-TW", "banned": [], "script": []}] * 5
    s = g.summarize_region(recs)
    hk, tw = s["by_region"]["zh-HK"], s["by_region"]["zh-TW"]
    assert hk["observed"] == 30 and hk["banned_hit"] == 3 and hk["script_hit"] == 3
    assert hk["hit_rate"] == 0.2 and hk["l4_verdict"] == "recommend_l4_rewrite"
    assert hk["top_banned"] == {"咱": 3} and hk["top_script"] == {"这": 3}
    assert tw["l4_verdict"] == "insufficient_sample"
    assert len(s["verdicts"]) == 1 and "zh-HK" in s["verdicts"][0]
    # 30 条只 1 条命中 → 观测即可
    recs = [{"region": "zh-HK", "banned": [], "script": []}] * 29 + [{"region": "zh-HK", "banned": ["咱"], "script": []}]
    assert g.summarize_region(recs)["by_region"]["zh-HK"]["l4_verdict"] == "observe_only"


def test_producers_write_ledger(ledger):
    from src.ai import persona_region
    from src.inbox import reply_quote_policy as rq

    persona_region.note_banned_hits("咱们这样吧", "zh-HK")
    rec = list(g.iter_records("region", 3))[-1]
    assert rec["region"] == "zh-HK" and "咱" in rec["banned"] and "这" in rec["script"]
    assert "text" not in rec and "reply" not in rec           # 不落原文

    rq.record_decision(rq.QuoteDecision(False, "single_inbound", burst_len=1))
    rq.record_applied({"quote_applied": True})
    rq.record_fallback_plain()
    recs = list(g.iter_records("quote", 3))
    assert [r.get("reason") for r in recs if "quote" in r] == ["single_inbound"]
    assert any(r.get("applied") for r in recs) and any(r.get("fallback_plain") for r in recs)


def test_report_cli_renders_without_ledger(tmp_path, capsys):
    from tools import region_quote_gray_report as cli
    rep = cli.report_root(tmp_path, 7)
    assert rep["ledger_exists"] is False
    txt = cli.render(rep)
    assert "无账本目录" in txt


def test_report_cli_renders_with_ledger(tmp_path):
    from tools import region_quote_gray_report as cli
    base = tmp_path / "logs" / "i4_gray"
    base.mkdir(parents=True)
    day = time.strftime("%Y%m%d")
    with open(base / f"quote_{day}.jsonl", "w", encoding="utf-8") as fh:
        for i in range(25):
            fh.write(json.dumps({"ts": time.time(), "quote": False, "reason": "low_relevance",
                                 "score": 0.1 + i * 0.01}) + "\n")
        fh.write(json.dumps({"ts": time.time(), "quote": True, "score": 0.5}) + "\n")
    with open(base / f"region_{day}.jsonl", "w", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write(json.dumps({"ts": time.time(), "region": "zh-TW", "banned": [], "script": []}) + "\n")
    rep = cli.report_root(tmp_path, 7)
    assert rep["quote"]["decided"] == 26
    assert rep["region"]["by_region"]["zh-TW"]["observed"] == 3
    txt = cli.render(rep)
    assert "自动链引用回复" in txt and "zh-TW" in txt and "insufficient_sample" in txt
