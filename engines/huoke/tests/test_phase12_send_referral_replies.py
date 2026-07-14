# -*- coding: utf-8 -*-
"""Phase 12 Alpha (2026-04-25): facebook_send_referral_replies 单测.

覆盖:
  * happy path: line_dispatch_planned event → send_message 成功 →
    wa_referral_sent event 写入 + mark_dispatch_outcome(sent)
  * strict_device_match 下 original_device_id 不匹配 → skipped_device
  * peer 24h 内已有 wa_referral_sent → skipped_dedup
  * send_message 返 False → failed 计数 + mark(failed)
  * send_message 抛异常 → failed (不崩)
  * dispatch_mode != 'messenger_text' → skipped_mode
  * limit 上限生效

mock fb.send_message, 不依赖真机.
"""
from __future__ import annotations

import json

import pytest
from unittest.mock import MagicMock, patch


@pytest.fixture(autouse=True)
def _fast_mode():
    """防 rate-limit sleep 拖慢测试. Executor 内部 import random/time 都被
    替换成零开销; 实际 sleep 被调的次数测试里用 _sleep_spy 单独检查."""
    with patch("random.uniform", return_value=0), \
         patch("time.sleep") as _slp:
        yield _slp


def _seed_planned_event(device_id: str, peer_name: str, *,
                         line_id: str = "along2026",
                         line_account_id: int = 1,
                         dispatch_mode: str = "messenger_text",
                         message_template: str = "LINE: along2026",
                         canonical_id: str = "",
                         original_device_id: str = "") -> int:
    from src.host.fb_store import record_contact_event
    return record_contact_event(
        device_id, peer_name,
        "line_dispatch_planned",
        preset_key=f"line_pool:{line_account_id}",
        meta={
            "line_id": line_id,
            "line_account_id": line_account_id,
            "dispatch_mode": dispatch_mode,
            "message_template": message_template,
            "canonical_id": canonical_id,
            "original_device_id": original_device_id or device_id,
        },
        skip_sanitize=True,  # Phase 16: 测试 fake peer 名 bypass
    )


def _make_fb_stub(send_return=True, send_raises=None):
    """构造只实现 send_message 的 fb mock."""
    fb = MagicMock()
    if send_raises:
        fb.send_message.side_effect = send_raises
    else:
        fb.send_message.return_value = send_return
    return fb


class TestSendReferralReplies:
    def test_happy_path_sends_and_records(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host.fb_store import count_contact_events
        from src.host import line_pool as lp

        # 建一个 account + planned event
        aid = lp.add("along2026", region="jp", daily_cap=20)
        # 先 allocate 一次模拟真实 dispatcher 写的 planned log
        lp.allocate(region="jp", canonical_id="c1",
                     peer_name="花子", source_device_id="DEV1")
        _seed_planned_event("DEV1", "花子",
                             line_account_id=aid,
                             canonical_id="c1",
                             original_device_id="DEV1")

        fb = _make_fb_stub(send_return=True)
        ok, _msg, stats = _fb_send_referral_replies(fb, "DEV1", {
            "hours_window": 2, "strict_device_match": True,
        })
        assert ok
        assert stats["sent"] == 1
        assert stats["failed"] == 0
        # fb.send_message 被调了, 参数含 message_template
        args, kwargs = fb.send_message.call_args
        assert kwargs.get("recipient") == "花子"
        assert "along2026" in kwargs.get("message", "")

        # wa_referral_sent event 写了
        n = count_contact_events(device_id="DEV1", peer_name="花子",
                                   event_type="wa_referral_sent", hours=2)
        assert n == 1

        # dispatch_log outcome=sent
        log = lp.recent_dispatch_log(limit=10)
        assert any(r["line_account_id"] == aid and r["status"] == "sent"
                   for r in log)

    def test_strict_device_match_skips_other_device(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        _seed_planned_event("DEV_B", "美咲",
                             original_device_id="DEV_B",  # 不匹配 resolved
                             line_account_id=1)
        fb = _make_fb_stub()
        ok, _, stats = _fb_send_referral_replies(fb, "DEV_A", {
            "hours_window": 2, "strict_device_match": True,
        })
        assert ok and stats["sent"] == 0
        assert stats["skipped_device"] == 1
        fb.send_message.assert_not_called()

    def test_strict_false_sends_across_device(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV_B", "恵",
                             line_account_id=aid,
                             original_device_id="DEV_B")
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV_A", {
            "hours_window": 2, "strict_device_match": False,
        })
        assert stats["sent"] == 1

    def test_dedupe_when_wa_already_sent(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host.fb_store import record_contact_event
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "由美",
                             line_account_id=aid,
                             original_device_id="DEV1")
        # 已经有人工发过一条 wa_referral_sent
        record_contact_event("DEV1", "由美", "wa_referral_sent",
                              meta={"line_id": "prev"})
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert stats["skipped_dedup"] == 1
        assert stats["sent"] == 0
        fb.send_message.assert_not_called()

    def test_send_failed_marks_outcome(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        lp.allocate(region="jp", canonical_id="c1", peer_name="裕子",
                     source_device_id="DEV1")
        _seed_planned_event("DEV1", "裕子",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = _make_fb_stub(send_return=False)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert ok and stats["failed"] == 1
        log = lp.recent_dispatch_log()
        assert any(r["line_account_id"] == aid and r["status"] == "failed"
                   for r in log)

    def test_send_exception_caught_and_counted(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "香織",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = _make_fb_stub(send_raises=RuntimeError("messenger frozen"))
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert ok and stats["failed"] == 1
        # outcome note 带异常类名
        out = stats["outcomes"][0]
        assert "send_message_exception" in (out.get("note") or "")

    def test_line_direct_send_mode_fallback_to_messenger(self, tmp_db):
        """Phase 12.0.1: line_direct_send 无 LINE auto 时默认 fallback 为
        messenger_text 直发 (防死信)."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "真理子",
                             dispatch_mode="line_direct_send",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1", {})
        assert stats["sent"] == 1, "fallback_line_direct_send=True (默认) 应发出"
        assert stats["outcomes"][0]["effective_mode"] == "messenger_text"

    def test_line_direct_send_mode_strict_skip(self, tmp_db):
        """fallback_line_direct_send=False → line_direct_send skip (旧行为)."""
        from src.host.executor import _fb_send_referral_replies
        _seed_planned_event("DEV1", "真理子",
                             dispatch_mode="line_direct_send",
                             line_account_id=1,
                             original_device_id="DEV1")
        fb = _make_fb_stub()
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1",
            {"fallback_line_direct_send": False})
        assert stats["skipped_mode"] == 1
        assert stats["sent"] == 0
        fb.send_message.assert_not_called()

    def test_transient_error_retried(self, tmp_db):
        """Phase 12.0.1: 瞬时错误 (messenger_unavailable) 重试 max_retry 次."""
        from src.host.executor import _fb_send_referral_replies
        from src.app_automation.facebook import MessengerError
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "花子",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = MagicMock()
        # 第 1-2 次抛瞬时错误, 第 3 次成功
        fb.send_message.side_effect = [
            MessengerError("messenger_unavailable", "启动中"),
            MessengerError("search_ui_missing", "UI 加载慢"),
            True,
        ]
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1",
            {"max_retry": 2, "retry_interval_sec": 0})
        assert stats["sent"] == 1
        assert fb.send_message.call_count == 3  # 2 retry + 1 成功

    def test_permanent_error_no_retry(self, tmp_db):
        """永久错误 (risk_detected) 不 retry 直接 failed."""
        from src.host.executor import _fb_send_referral_replies
        from src.app_automation.facebook import MessengerError
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "美咲",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = MagicMock()
        fb.send_message.side_effect = MessengerError("risk_detected", "风控")
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1",
            {"max_retry": 3, "retry_interval_sec": 0})
        assert stats["failed"] == 1
        assert fb.send_message.call_count == 1, \
            "永久错误只应调 1 次, 不 retry"
        assert "risk_detected" in stats["outcomes"][0]["err_code"]

    def test_retry_exhausted_marks_failed(self, tmp_db):
        """瞬时错误连续失败 → retry 耗尽 failed."""
        from src.host.executor import _fb_send_referral_replies
        from src.app_automation.facebook import MessengerError
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp")
        _seed_planned_event("DEV1", "裕子",
                             line_account_id=aid,
                             original_device_id="DEV1")
        fb = MagicMock()
        fb.send_message.side_effect = MessengerError("send_button_missing", "UI")
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1",
            {"max_retry": 2, "retry_interval_sec": 0})
        assert stats["failed"] == 1
        assert fb.send_message.call_count == 3  # 初次 + 2 retry

    def test_rate_limit_sleep_between_sends(self, tmp_db, _fast_mode):
        """Phase 12.0.1: 第 2 条起 time.sleep(random.uniform(min, max))."""
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp", daily_cap=100)
        for i in range(3):
            _seed_planned_event("DEV1", f"P{i}",
                                 line_account_id=aid,
                                 original_device_id="DEV1")
        fb = _make_fb_stub(send_return=True)
        _fb_send_referral_replies(fb, "DEV1",
            {"min_interval_sec": 30, "max_interval_sec": 90,
             "retry_interval_sec": 0, "max_retry": 0})
        # 第 1 条不 sleep, 第 2/3 条各 sleep 一次 → time.sleep 被调 >= 2 次
        # _fast_mode 已把 sleep patch 成 MagicMock, 检查 call_count
        assert _fast_mode.call_count >= 2

    def test_limit_caps_send_count(self, tmp_db):
        from src.host.executor import _fb_send_referral_replies
        from src.host import line_pool as lp
        aid = lp.add("along2026", region="jp", daily_cap=100)
        for i in range(5):
            _seed_planned_event("DEV1", f"U{i}",
                                 line_account_id=aid,
                                 original_device_id="DEV1")
        fb = _make_fb_stub(send_return=True)
        ok, _, stats = _fb_send_referral_replies(fb, "DEV1",
                                                    {"limit": 2})
        assert stats["sent"] == 2
        assert fb.send_message.call_count == 2
