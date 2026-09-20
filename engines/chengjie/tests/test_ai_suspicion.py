# -*- coding: utf-8 -*-
"""AI 质疑应对闭环门禁（2026-08-03）。

覆盖：检测器单一事实源（运行时 utils 与 eval re-export 同一对象）/ 应对提示
内容不变量（反翻车要点齐全 + 原话截短引用）/ 进程计数快照 /
apply_inbound_enrichments 接线（命中注入 _topic_switch_hint、不命中零污染）。
"""
from __future__ import annotations

import pytest

from src.utils.ai_suspicion import (
    build_suspicion_hint,
    detect_ai_suspicion,
    record_suspicion,
    reset_suspicion_state,
    suspicion_snapshot,
)


@pytest.fixture(autouse=True)
def _clean_state():
    reset_suspicion_state()
    yield
    reset_suspicion_state()


# ── 单一事实源 ────────────────────────────────────────────────────────────────


def test_eval_reexport_is_same_object():
    """eval 侧必须 re-export 运行时实现——两套词表必然漂移，一套不会。"""
    from src.eval.ai_flavor_eval import detect_ai_suspicion as eval_detect

    assert eval_detect is detect_ai_suspicion


# ── 应对提示内容不变量 ────────────────────────────────────────────────────────


def test_direct_hint_quotes_and_anti_patterns():
    hint = build_suspicion_hint("direct", "自然一点，一听就知道你是Ai，语气别这么冲")
    assert "质疑你是不是AI" in hint
    assert "自然一点，一听就知道你是Ai" in hint     # 原话引用（截短前 30 字内）
    for kw in ("连声否认", "长篇自证", "格外热情", "要短"):
        assert kw in hint
    # 截短：原话超 30 字带省略号
    long_hint = build_suspicion_hint("direct", "你是不是AI啊" + "真的假的" * 20)
    assert "…" in long_hint


def test_flavor_hint_self_deprecation_route():
    hint = build_suspicion_hint("flavor", "刚才那句一股机器味")
    assert "机器" in hint and "自嘲" in hint
    assert "不要解释技术原因" in hint


def test_unknown_kind_and_empty():
    assert build_suspicion_hint("", "x") == ""
    assert build_suspicion_hint("nope", "x") == ""


# ── 进程计数 ─────────────────────────────────────────────────────────────────


def test_record_and_snapshot():
    record_suspicion("direct", "你是不是AI")
    record_suspicion("flavor", "一股机器味")
    record_suspicion("bogus", "不该计数")
    snap = suspicion_snapshot()
    assert snap["direct"] == 1 and snap["flavor"] == 1
    assert len(snap["samples"]) == 2
    assert snap["last_ts"] > 0
    assert all(len(s["text"]) <= 31 for s in snap["samples"])


# ── apply_inbound_enrichments 接线 ───────────────────────────────────────────


def test_wiring_hint_injected_on_suspicion():
    from src.inbox.inbound_enrich import apply_inbound_enrichments

    ctx: dict = {}
    apply_inbound_enrichments(ctx, text="你是不是AI啊，说话这么整齐")
    hint = ctx.get("_topic_switch_hint") or ""
    assert "质疑你是不是AI" in hint
    snap = suspicion_snapshot()
    assert snap["direct"] == 1


def test_wiring_no_false_positive_on_ai_topic():
    from src.inbox.inbound_enrich import apply_inbound_enrichments

    ctx: dict = {}
    apply_inbound_enrichments(ctx, text="我今天用AI做了个图，你看看好不好看")
    assert "质疑" not in (ctx.get("_topic_switch_hint") or "")
    assert suspicion_snapshot()["direct"] == 0


def test_wiring_flavor_complaint():
    from src.inbox.inbound_enrich import apply_inbound_enrichments

    ctx: dict = {}
    apply_inbound_enrichments(ctx, text="你发的语音一股机器味，能不能自然点")
    hint = ctx.get("_topic_switch_hint") or ""
    assert "吐槽你说话/声音像机器" in hint
    assert suspicion_snapshot()["flavor"] == 1
