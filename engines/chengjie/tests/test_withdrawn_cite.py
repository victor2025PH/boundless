# -*- coding: utf-8 -*-
"""#32② / #145⑤：撤回内容记得但不主动提。

金标：被删内容是「我下周去日本」，之后 5 轮 AI 都不该主动提日本；
对方本轮自己又说到同一件事则不剥。上下文槽位不因撤回而塌。
"""
from __future__ import annotations

from src.inbox.inbound_enrich import apply_inbound_enrichments
from src.inbox.withdrawn_cite import (
    PLACEHOLDER,
    build_withdrawn_hint,
    quotes_for,
    record_withdrawn,
    redact_history,
    reset_ledger,
    sanitize_outbound,
)


QUOTE = "我下周去日本"
CID = "telegram:acct:555"

_FIVE_DRAFTS = [
    "日本那边天气怎么样？要不要我帮你看看机票。",
    "你不是说下周去日本嘛，准备得怎么样了。",
    "去日本玩记得带充电器哦。",
    "日本的拉面我超喜欢，你到了跟我说一声。",
    "那你日本行程定了没？我可以陪你规划。",
]


def setup_function():
    reset_ledger()


def test_record_and_quotes_for_suffix():
    assert record_withdrawn(CID, QUOTE) is True
    assert record_withdrawn(CID, QUOTE) is False  # 幂等
    assert quotes_for(CID) == [QUOTE]
    assert quotes_for("555") == [QUOTE]
    assert quotes_for("telegram:other:999") == []
    assert record_withdrawn("", QUOTE) is False
    assert record_withdrawn(CID, "嗯") is False


def test_sanitize_five_rounds_must_not_cite_japan():
    record_withdrawn(CID, QUOTE)
    qs = quotes_for(CID)
    for draft in _FIVE_DRAFTS:
        cleaned, hits = sanitize_outbound(draft, qs)
        assert "日本" not in cleaned, (draft, cleaned)
        assert hits
        assert cleaned  # 剥空有中性兜底，不许回退原文
        assert cleaned != draft


def test_sanitize_empty_falls_to_neutral():
    record_withdrawn(CID, QUOTE)
    cleaned, hits = sanitize_outbound("日本真不错。", quotes_for(CID))
    assert cleaned == "嗯。"
    assert hits


def test_inbound_mentions_japan_not_stripped():
    record_withdrawn(CID, QUOTE)
    draft = "日本那边我帮你看看天气。"
    cleaned, hits = sanitize_outbound(draft, quotes_for(CID), inbound="日本好玩吗")
    assert cleaned == draft
    assert hits == []


def test_unrelated_outbound_untouched():
    record_withdrawn(CID, QUOTE)
    draft = "今晚想吃火锅吗？"
    cleaned, hits = sanitize_outbound(draft, quotes_for(CID))
    assert cleaned == draft
    assert hits == []


def test_redact_history_keeps_slots():
    hist = [
        {"role": "user", "content": QUOTE},
        {"role": "assistant", "content": "好呀到时候注意安全"},
        {"role": "user", "content": "今天好累"},
    ]
    out = redact_history(hist, [QUOTE])
    assert len(out) == 3
    assert out[0]["content"] == PLACEHOLDER
    assert out[0]["_withdrawn"] is True
    assert out[1]["content"] == "好呀到时候注意安全"
    assert out[2]["content"] == "今天好累"


def test_hint_suppressed_when_inbound_covers():
    record_withdrawn(CID, QUOTE)
    qs = quotes_for(CID)
    hint = build_withdrawn_hint(qs, inbound="")
    assert "不得主动引用" in hint
    assert "日本" in hint
    # 对方本轮把整句又说了一遍 → 不注入
    assert build_withdrawn_hint(qs, inbound=QUOTE) == ""


def test_inbound_enrich_injects_hint_and_redacts_history():
    record_withdrawn(CID, QUOTE)
    hist = [
        {"role": "user", "content": QUOTE},
        {"role": "assistant", "content": "好的"},
    ]
    uc = {"conversation_id": CID}
    apply_inbound_enrichments(uc, text="今天好累", history=hist, platform="telegram")
    assert "不得主动引用" in (uc.get("_topic_switch_hint") or "")
    assert hist[0]["content"] == PLACEHOLDER
    assert len(hist) == 2
