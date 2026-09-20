# -*- coding: utf-8 -*-
"""双向全自动对级验收门禁（duplex_report P1，2026-08-07）。

守两件事：① 消息动力学纯函数的判定口径（连发段应答语义 / 复读尺与
peer_bot_guard 同一把 / 秒回风险 / 分位数）；② 判词矩阵——「单边哑火」
必须是 block 且指路 why_no_reply（验收工具给错方向比不给更糟）。
"""
from __future__ import annotations

from pathlib import Path

from src.inbox.duplex_report import (
    REPEAT_BRAKE_N,
    build_side_stats,
    consecutive_repeat_max,
    duplex_verdict,
    percentile,
    side_summary,
)

T0 = 1_700_000_000.0


def _m(direction: str, ts_off: float, text: str = "hi") -> dict:
    return {"direction": direction, "ts": T0 + ts_off, "text": text}


# ── 连发段应答语义 ──────────────────────────────────────────────────────

def test_burst_answer_semantics():
    """客户连发 3 条、AI 回 1 条＝答了 1 段（与 inbound_merge 产品语义一致）。"""
    msgs = [_m("in", 0, "a"), _m("in", 5, "b"), _m("in", 9, "c"),
            _m("out", 40, "reply")]
    s = build_side_stats(msgs)
    assert s.inbound == 3 and s.outbound == 1
    assert s.in_bursts == 1 and s.answered == 1
    assert s.answer_rate == 1.0
    # 段级延迟按「段末条 → 首条回复」计
    assert s.latencies == [31.0]


def test_unanswered_trailing_burst():
    msgs = [_m("in", 0), _m("out", 30), _m("in", 100), _m("in", 105)]
    s = build_side_stats(msgs)
    assert s.in_bursts == 2 and s.answered == 1
    assert s.answer_rate == 0.5


def test_outbound_only_no_bursts():
    s = build_side_stats([_m("out", 0), _m("out", 10)])
    assert s.in_bursts == 0 and s.answer_rate == 0.0 and s.outbound == 2


def test_fast_reply_counted():
    msgs = [_m("in", 0), _m("out", 1.0), _m("in", 60), _m("out", 120)]
    s = build_side_stats(msgs)
    assert s.fast_replies == 1


# ── 复读尺与守卫同口径 ──────────────────────────────────────────────────

def test_repeat_max_normalized_like_guard():
    # 标点/空白差异不算不同文本（normalize_text 口径）
    assert consecutive_repeat_max(["好呀!", "好呀！！", "好 呀"]) == 3
    assert consecutive_repeat_max(["a", "b", "a"]) == 1


def test_repeat_media_placeholder_immunity():
    """连发 33 条 [语音] 是真实客户行为——占位符中断计数，绝不算复读。"""
    texts = ["[语音]"] * 5 + ["在吗", "在吗"]
    assert consecutive_repeat_max(texts) == 2


def test_percentile_edges():
    assert percentile([], 0.5) == 0.0
    assert percentile([7.0], 0.95) == 7.0
    assert percentile([0.0, 10.0], 0.5) == 5.0


def test_side_summary_json_safe_keys():
    import json
    out = side_summary(build_side_stats([_m("in", 0), _m("out", 20)]))
    json.dumps(out)
    for k in ("answer_rate", "latency_p50_s", "latency_p95_s",
              "out_repeat_max", "fast_replies", "pending_in_age_s"):
        assert k in out


# ── 判词矩阵 ────────────────────────────────────────────────────────────

def _codes(verdicts, level=None):
    return [v["code"] for v in verdicts if level is None or v["level"] == level]


def test_verdict_silent_side_blocks_and_points_to_why_no_reply():
    a = build_side_stats([_m("in", 0), _m("out", 30)])          # A 正常
    b = build_side_stats([_m("in", 0), _m("in", 60)])           # B 收到不回
    vs = duplex_verdict(a, b, a_label="104", b_label="198")
    assert "198_silent" in _codes(vs, "block")
    blocked = [v for v in vs if v["code"] == "198_silent"][0]
    assert "why_no_reply" in blocked["msg"]


def test_verdict_duplex_alive_when_both_answer():
    a = build_side_stats([_m("in", 0), _m("out", 30),
                          _m("in", 100), _m("out", 140)])
    b = build_side_stats([_m("in", 10), _m("out", 50),
                          _m("in", 120), _m("out", 170)])
    vs = duplex_verdict(a, b)
    assert "duplex_alive" in _codes(vs, "ok")
    assert not _codes(vs, "block")


def test_verdict_pending_inbound_warns():
    """半哑火判据＝末段入站悬置时长（应答率在连发段语义下数学退化：
    未回应的入站合并成一段，率恒 ≥ (n-1)/n——首版踩过的设计缺陷，勿改回）。"""
    a = build_side_stats([_m("in", 0), _m("out", 30), _m("in", 100)],
                         now=T0 + 100 + 3600)   # 悬 1 小时
    b = build_side_stats([_m("in", 5), _m("out", 40)])
    vs = duplex_verdict(a, b, a_label="A", b_label="B")
    assert "A_pending" in _codes(vs, "warn")
    hit = [v for v in vs if v["code"] == "A_pending"][0]
    assert "60" in hit["msg"]          # 悬置分钟数进文案


def test_pending_zero_without_now_or_when_last_is_out():
    # 离线重放（无 now）→ 悬置恒 0；末段是 out → 无悬置
    assert build_side_stats([_m("in", 0), _m("in", 9)]).pending_in_age_s == 0.0
    assert build_side_stats([_m("in", 0), _m("out", 30)],
                            now=T0 + 9999).pending_in_age_s == 0.0


def test_verdict_repeat_risk_warns_at_guard_threshold():
    same = [_m("in", 0)] + [_m("out", 30 + i, "好的呢") for i in range(REPEAT_BRAKE_N)]
    a = build_side_stats(same)
    b = build_side_stats([_m("in", 0), _m("out", 25, "x"),
                          _m("in", 90), _m("out", 130, "y")])
    vs = duplex_verdict(a, b, a_label="A", b_label="B")
    assert "A_repeat_risk" in _codes(vs, "warn")
    hit = [v for v in vs if v["code"] == "A_repeat_risk"][0]
    assert "review" in hit["msg"]


def test_verdict_no_traffic_hint():
    vs = duplex_verdict(build_side_stats([]), build_side_stats([]))
    assert "no_traffic" in _codes(vs, "warn")


# ── CLI 静态门禁（只读纪律与分工契约） ──────────────────────────────────

def test_cli_read_only_and_wired():
    p = Path(__file__).resolve().parents[1] / "tools" / "duplex_report.py"
    text = p.read_text("utf-8")
    assert "mode=ro" in text, "验收 CLI 必须只读打开生产库"
    assert "from src.inbox.store import" not in text
    assert "InboxStore(" not in text
    assert "effective_automation" in text, "两侧封顶读数必须与护栏同源"
    assert "duplex_verdict" in text
    # 与 why_no_reply 的分工：发现单边哑火只指路，不重复实现闸门解释
    assert "why_no_reply" in text
