# -*- coding: utf-8 -*-
"""运维群告警卡文案契约（2026-09-10 运维群降噪 P0.5）。

09-10 逐张读运维群 33.5h 里的 70 张卡得到的问题清单，每条钉一个断言：
  - 反引号原样露出（`inbox.sla_watcher.auto_expire_hours`）；
  - 空字段行（「详情: 」后面什么都没有）；
  - LAN GPU 卡把跑 vLLM 的 176 说成「Ollama」、让人「到现场检查电源」；
  - 会话 id 被 [-12:] 砍成认不出的尾巴；
  - 「最老 884.2 小时」这种没人会换算的时长；
  - 重提卡不告诉人「和上次一模一样」。
"""
from __future__ import annotations

import re

import pytest

from src.inbox.webhook_notifier import (_build_card, _build_message, _hours_txt, _minutes_txt, _plainify,
                                        card_body_lines)


def _card(etype, data):
    title, text = _build_message(etype, data)
    return _build_card(etype, data, title, text, "https://katie.example.cc", "login")


def _assert_clean(card: str):
    assert "`" not in card, card
    for line in card.splitlines():
        assert not re.match(r"^\s*[^:：]{1,12}[:：]\s*$", line), f"空字段行: {line!r}\n{card}"
    assert "](/" not in card and "**" not in card


def test_plainify_strips_backticks_and_empty_field_lines():
    out = _plainify("**处置**: 开 `inbox.sla_watcher.auto_expire_hours`\n**详情**: \n下一行")
    assert "`" not in out and "inbox.sla_watcher.auto_expire_hours" in out
    assert "详情" not in out and "下一行" in out


def test_duration_humanized():
    assert _hours_txt(884.2) == "37 天"
    assert _hours_txt(30.4) == "30 小时"
    assert _hours_txt(5.04) == "5.0 小时"
    assert _hours_txt(0.5) == "30 分钟"
    assert _minutes_txt(31) == "31 分钟"
    assert _minutes_txt(90) == "1 小时 30 分钟"
    assert _minutes_txt(16 * 60) == "16 小时"
    assert _minutes_txt(3 * 24 * 60) == "3.0 天"


def test_lan_gpu_card_no_ollama_no_onsite_no_empty_detail():
    card = _card("lan_gpu_alert", {
        "host": "192.168.0.176:11434", "url": "http://192.168.0.176:11434",
        "error": "", "down_minutes": 16 * 60 + 5, "reminder": True, "unchanged": True,
    })
    _assert_clean(card)
    assert "Ollama" not in card and "现场" not in card and "电源" not in card
    assert "16 小时 5 分钟" in card
    assert "/v1/models" in card


def test_avatar_card_no_empty_detail_and_no_shell_command():
    card = _card("avatar_voice_alert", {
        "reachable": False, "models_loaded": False, "url": "http://127.0.0.1:7852",
        "error": "", "hang": False, "down_minutes": 30, "reminder": False,
        "rescue_broken": ["EmotionTTS_Boot", "EmotionTTS_Watchdog"],
    })
    _assert_clean(card)
    assert "schtasks" not in card
    assert "EmotionTTS_Boot" in card   # 任务名要给出（运维要知道启哪个），但不带命令行


def test_unanswered_card_keeps_full_conversation_id_and_marks_unchanged():
    cid = "messenger:100012345678901:obe-20260908-test"
    card = _card("unanswered_inbound_alert", {
        "count": 1, "oldest_hours": 30.4, "reminder": True, "unchanged": True,
        "samples": [{"platform": "messenger", "conversation_id": cid,
                     "account_id": "msg_nxl3iqxn", "age_hours": 30.4}],
    })
    _assert_clean(card)
    assert cid in card and "msg_nxl3iqxn" in card
    assert "与上次相同" in card and "30 小时" in card


def test_case_backlog_card_humanizes_days():
    card = _card("case_backlog_alert", {
        "urgent_count": 0, "stale_count": 3, "oldest_hours": 884.2, "by_source": {"ai_doubt": 3},
        "min_age_hours": 4, "media_stale_count": 0, "reminder": True, "unchanged": True,
    })
    _assert_clean(card)
    assert "37 天" in card and "884" not in card
    assert "与上次相同" in card


def test_draft_backlog_card_no_backticks_marks_unchanged():
    card = _card("draft_backlog_alert", {
        "stale_count": 5, "oldest_hours": 712.6, "min_age_hours": 24, "sla_uncovered": 5,
        "by_level": {"L1": 5}, "already_replied": 0, "reminder": True, "unchanged": True,
    })
    _assert_clean(card)
    assert "inbox.sla_watcher.auto_expire_hours" in card
    assert "30 天" in card and "712" not in card and "与上次相同" in card


def test_ops_digest_card_is_short_and_plain():
    """每日运维摘要（P1.2）：一张卡说清「未处理什么 / 开多久 / 探针 / 昨日花费」，≤ 8 行正文。"""
    card = _card("ops_digest_report", {
        "day": "2026-09-10",
        "open": [
            {"key": "draft_backlog", "label": "待审草稿",
             "summary": "待审草稿 5 条无人处理（最久 30 天）", "hours": 712.6},
            {"key": "lan_gpu:http://192.168.0.173:8001", "label": "LAN GPU",
             "summary": "192.168.0.173:8001 探测失败", "hours": 16.5},
        ],
        "probes": {"total": 7, "ok": 6, "bad": ["vision"]},
        "cost": {"provider": "siliconflow", "yesterday_cost": 3.21, "yesterday_calls": 412,
                 "yesterday_truth": None, "balance": 88.5, "runway_days": 27},
    })
    _assert_clean(card)
    assert "每日运维摘要 · 2026-09-10" in card
    assert "未处理: 2 项（最久 30 天）" in card
    # 摘要行以标签开头时不重复「待审草稿：待审草稿…」；摘要自带「最久 30 天」时不再追加「已开」
    assert "- 待审草稿 5 条无人处理（最久 30 天）\n" in card
    assert "- LAN GPU：192.168.0.173:8001 探测失败（已开 16 小时）" in card
    assert "探针: 1 项异常（vision），其余 6 项正常" in card
    assert "昨日花费: ¥3.21（412 次调用），账单未导入；余额约 ¥88.50，够用 27 天" in card
    assert len(card_body_lines(card)) <= 8, card


def test_ops_digest_card_empty_day_says_so():
    card = _card("ops_digest_report", {"day": "2026-09-11", "open": [], "probes": {"total": 7, "ok": 7, "bad": []},
                                       "cost": None})
    _assert_clean(card)
    assert "未处理: 无 ✅" in card and "7 项全部正常" in card and "昨日花费" not in card


@pytest.mark.parametrize("etype,data", [
    ("lan_gpu_alert", {"recovered": True, "host": "h", "url": "u"}),
    ("compute_lane_alert", {"recovered": True, "lane": "cloud", "label": "DeepSeek 官方"}),
    ("avatar_voice_alert", {"recovered": True}),
    ("draft_backlog_alert", {"recovered": True}),
    ("case_backlog_alert", {"recovered": True}),
    ("unanswered_inbound_alert", {"recovered": True}),
    ("host_alert", {"title": "真活探针失败｜vision", "message": "vision 连续 3 次、持续 20 分钟真活探针失败（TimeoutError）。"}),
])
def test_recovery_and_host_cards_are_clean(etype, data):
    _assert_clean(_card(etype, data))
