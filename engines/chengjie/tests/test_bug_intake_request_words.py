# -*- coding: utf-8 -*-
"""L-7 C-1（2026-09-06）：报障群里「向值守要清单/进度/答复」不再被判闲聊静默。

0906 00:45 skuio「先列出41张单以及修复的结果」→ other → smalltalk_suppressed（ev#753）无痕，
他随后自己做了 44 项总表；04:07 钧「那个表情在客户手机上会动吗」（无问号）同样静默（ev#810）。
契约：request_for_info 事件 + 值守告警（每用户 10 分钟去抖）；AI 仍不接（不许编台账）；
真闲聊照旧 smalltalk_suppressed；bug / usage / feedback 分类优先级不变；webhook 认识新 kind。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CFG = {
    "bug_intake": {
        "enabled": True,
        "groups": [-100123],
        "support_accounts": ["777"],
        "max_replies_per_user_hour": 5,
        "max_replies_per_group_hour": 20,
        "collect_window_min": 30,
    }
}


class _BusStub:
    def __init__(self):
        self.published = []

    def publish(self, etype, payload):
        self.published.append((etype, payload))


@pytest.fixture()
def bi(tmp_path, monkeypatch):
    from src.ops import bug_intake
    from src.ops import bug_intake_backfill as bfm
    bug_intake.reset_state_for_tests()
    monkeypatch.setattr(bug_intake, "_db_path", lambda: tmp_path / "bug_intake.db")
    monkeypatch.setattr(bfm, "_SEEN_PATH_OVERRIDE", tmp_path / "bug_intake_seen.json")
    yield bug_intake
    bug_intake.reset_state_for_tests()


@pytest.fixture()
def bus(monkeypatch):
    from src.integrations.shared import event_bus as eb
    stub = _BusStub()
    monkeypatch.setattr(eb, "get_event_bus", lambda: stub)
    return stub


@pytest.mark.parametrize("text", [
    "先列出41张单以及修复的结果",
    "给我一份修复清单",
    "现在进度怎么样",
    "哪些修了哪些没修",
    "1.0.74 修复了什么",
    "changelog 发一下",
    "这个什么时候修",
    "#170 修好了吗",
    "那个表情在客户手机上会动吗",      # ev#810：无问号的「吗」问句
    "语音这样设置对么",
])
def test_info_request_words_hit(bi, text):
    assert bi.is_info_request(text) is True


@pytest.mark.parametrize("text", [
    "嗯这个", "好的", "哈哈哈", "晚安", "吃了吗",          # 「吃了吗」< 6 字不算
    "这个列表好看",                                        # 「列表」刻意不进表
    "", "   ",
])
def test_info_request_words_miss(bi, text):
    assert bi.is_info_request(text) is False


def test_verdict_request_records_event_alerts_and_does_not_engage(bi, bus):
    out = bi.trigger_verdict(CFG, -100123, "u1", "先列出41张单以及修复的结果",
                             now=1000.0, msg_id=753)
    assert out is False                                   # AI 不接：不许编清单
    evs = bi.list_events(["request_for_info"])
    assert len(evs) == 1 and "列出41张单" in evs[0]["detail"]
    assert bi.list_events(["smalltalk_suppressed"]) == []  # 不再当闲聊
    assert len(bus.published) == 1
    etype, payload = bus.published[0]
    assert etype == "bug_intake_alert" and payload["kind"] == "request_for_info"
    assert payload["reporter"] == "u1" and payload["severity"] == "P2"
    assert payload["rate_key"] == "bug_intake:request_for_info:-100123:u1"
    st = bi.dump_stats()
    assert st["request_for_info"] == 1 and st["smalltalk_suppressed"] == 0


def test_verdict_request_alert_debounced_but_events_always_recorded(bi, bus):
    bi.trigger_verdict(CFG, -100123, "u1", "先列出41张单", now=1000.0)
    bi.trigger_verdict(CFG, -100123, "u1", "还有修复进度呢", now=1100.0)   # 10 分钟内
    assert len(bi.list_events(["request_for_info"])) == 2
    assert len(bus.published) == 1
    bi.trigger_verdict(CFG, -100123, "u1", "对账表发我", now=1000.0 + 601)
    assert len(bus.published) == 2
    # 别的用户不共享去抖桶
    bi.trigger_verdict(CFG, -100123, "u2", "哪些修了", now=1000.0)
    assert len(bus.published) == 3


def test_verdict_smalltalk_still_suppressed_and_bug_words_still_win(bi, bus):
    assert bi.trigger_verdict(CFG, -100123, "u1", "嗯这个", now=1000.0) is False
    assert len(bi.list_events(["smalltalk_suppressed"])) == 1
    assert bi.list_events(["request_for_info"]) == []
    # 请求词与 bug 词同现 → bug 分类先命中（引燃），不落 request_for_info
    assert bi.trigger_verdict(CFG, -100123, "u3", "列出为什么消息发不出去", now=1000.0) is True
    assert bi.list_events(["request_for_info"]) == []
    assert bus.published == []


def test_webhook_formatter_knows_request_for_info():
    src = (ROOT / "src" / "inbox" / "webhook_notifier.py").read_text(encoding="utf-8")
    assert '== "request_for_info"' in src, "告警 formatter 必须认识 request_for_info（否则渲染成假「新工单」）"
    from src.inbox.webhook_notifier import _build_message
    title, text = _build_message("bug_intake_alert", {
        "kind": "request_for_info", "chat_id": "-100123", "reporter": "u1",
        "text": "先列出41张单以及修复的结果", "severity": "P2"})
    assert "清单" in title and "新工单" not in title
    assert "列出41张单" in text
