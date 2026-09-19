# -*- coding: utf-8 -*-
"""实施86 域B-1 门禁：Messenger 发送失败家族（工单 #21/#23/#34/#49）。

修复的三层契约：
① 结构化败因提取（``http_error_fields``）——边车 /send 的 429/423 带
   reason_code 与 retry_after_ms，此前死在 ``raise_for_status`` 的字符串里
   （#49 坐席看到的裸「Too Many Requests」正是我方退避 429 的状态行）；
② 人话分类（``classify_send_failure``）与 autosend 改期决策
   （``plan_failure_retry``）——限频/冻结按边车提示的恢复时刻改期重投，
   不再当场终局失败白丢草稿，也不盲撞退避窗；
③ 手发失败留痕——B63③ 的 status=failed 留痕行此前只有自动链在写，
   #21「无法发送的消息也没有记录」就是手发半边断链。
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from src.inbox.send_failure_class import (
    DEFER_MARGIN_SEC,
    FAILURE_CLASS_I18N,
    classify_send_failure,
    plan_failure_retry,
)


# ── ① 结构化败因提取 ─────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body, status_code=429):
        self._body = body
        self.status_code = status_code

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeHttpError(Exception):
    def __init__(self, msg, resp=None):
        super().__init__(msg)
        if resp is not None:
            self.response = resp


def test_http_error_fields_extracts_reason_and_retry_after():
    from src.integrations.messenger_web_login import http_error_fields
    ex = _FakeHttpError(
        "Client error '429 Too Many Requests' for url 'http://127.0.0.1:8791/...'",
        _FakeResp({"ok": False, "delivered": False, "reason_code": "send_backoff",
                   "retry_after_ms": 42000,
                   "error": "send backoff active after consecutive failures"}))
    f = http_error_fields(ex)
    assert f["reason_code"] == "send_backoff"
    assert f["retry_after_ms"] == 42000
    assert f["status"] == 429
    assert "send backoff active" in f["detail"]
    assert "[send_backoff]" in f["detail"]


def test_http_error_fields_no_response_falls_back():
    from src.integrations.messenger_web_login import http_error_fields
    f = http_error_fields(_FakeHttpError("connection refused"))
    # Q-24 #298：新增 code / retries / sidecar_detail 三个零值键（七码契约），旧四键不变
    assert f == {"detail": "connection refused", "reason_code": "",
                 "retry_after_ms": 0, "status": 0,
                 "code": "", "retries": 0, "sidecar_detail": ""}


def test_http_error_fields_bad_body_keeps_base_detail():
    from src.integrations.messenger_web_login import http_error_fields
    ex = _FakeHttpError("Server error '500'", _FakeResp(ValueError("not json"),
                                                        status_code=500))
    f = http_error_fields(ex)
    assert f["detail"] == "Server error '500'"
    assert f["status"] == 500
    # 非 dict body 同样只回落，不炸
    ex2 = _FakeHttpError("boom", _FakeResp(["not", "a", "dict"]))
    assert http_error_fields(ex2)["reason_code"] == ""


def test_http_error_detail_delegates_to_fields():
    from src.integrations.messenger_web_login import (
        http_error_detail, http_error_fields)
    ex = _FakeHttpError(
        "Server error '503'",
        _FakeResp({"error": "e2ee recovery pin prompt on screen",
                   "reason_code": "e2ee_pin_pending"}, status_code=503))
    assert http_error_detail(ex) == http_error_fields(ex)["detail"]
    assert "[e2ee_pin_pending]" in http_error_detail(ex)


# ── ② 人话分类 + 改期决策 ────────────────────────────────────────────────────

@pytest.mark.parametrize("text,reason,expect", [
    # #49 实录：我方退避 429 的裸状态行
    ("Client error '429 Too Many Requests' for url ...", "", "rate_limited"),
    ("send backoff active after consecutive failures", "send_backoff",
     "rate_limited"),
    # #34 实录：平台临时封锁（边车冻结文案）
    ("account temporarily blocked by platform (auto-frozen)", "",
     "platform_block"),
    ("", "account_blocked", "platform_block"),
    # PIN 未解锁（B99 ②）
    ("e2ee recovery pin prompt on screen (session locked)",
     "e2ee_pin_pending", "e2ee_pin"),
    # 会话掉线族
    ("messenger session unhealthy (needs manual re-login)", "", "session"),
    ("session listed but not logged in (cookie expired?)", "", "session"),
    # #23 实录：我方 worker 异常族
    ("composer not found (render_timeout)", "", "channel"),
    ("Server error '500 Internal Server Error' for url ...", "", "channel"),
    ("messenger marked the message as failed to send", "bubble_fail_marker",
     "channel"),
    # 认不出 → 空串（调用方保留原文，不错贴标签）
    ("weird unknown platform hiccup", "", ""),
    ("", "", ""),
])
def test_classify_send_failure(text, reason, expect):
    assert classify_send_failure(text, reason) == expect


def test_failure_class_i18n_keys_exist_bilingual():
    """分类代号的 i18n 键必须 zh+en 双语齐备（tr 回落裸键＝比原文更糟）。"""
    from src.web.i18n_packs.errors import EN, ZH
    for key in FAILURE_CLASS_I18N.values():
        assert key in ZH, f"缺简体键 {key}"
        assert key in EN, f"缺英文键 {key}"


def test_bubble_failr_keys_exist_bilingual():
    """失败留痕气泡的三个新短文案键同样双语齐备（前端 _failReasonText 消费）。"""
    from src.web.i18n_packs.inbox_workspace import EN, ZH
    for key in ("inbox.failr.rate_limited", "inbox.failr.platform_block",
                "inbox.failr.channel", "inbox.failr.voice_mic_busy",
                "inbox.failr.voice_daily_cap", "inbox.failr.voice_per_peer"):
        assert key in ZH, f"缺简体键 {key}"
        assert key in EN, f"缺英文键 {key}"


def test_plan_failure_retry_semantics():
    # 边车说 42s 后恢复 → 按提示改期（加确定性余量）
    action, delay = plan_failure_retry(hint_ms=42000, deferrals_used=0)
    assert action == "defer"
    assert delay == pytest.approx(42.0 + DEFER_MARGIN_SEC)
    # 无提示 / 永久错误 / 改期次数耗尽 / 超长冻结（2h 风控）→ 全部终局
    assert plan_failure_retry(hint_ms=0)[0] == "fail"
    assert plan_failure_retry(hint_ms=42000, permanent=True)[0] == "fail"
    assert plan_failure_retry(hint_ms=42000, deferrals_used=3)[0] == "fail"
    assert plan_failure_retry(hint_ms=7_200_000)[0] == "fail"


# ── ③ autosend：限频改期不终局、超长冻结终局留痕 ─────────────────────────────

def _svc(drafts):
    s = MagicMock()
    s.list_drafts.return_value = drafts
    s.resolve_with_audit.return_value = {"ok": True}
    return s


def _l2(cid="messenger:a:c1", draft_id="d1"):
    return {"draft_id": draft_id, "autopilot_level": "L2", "status": "pending",
            "platform": "messenger", "account_id": "a", "chat_key": "c1",
            "conversation_id": cid, "draft_text": "hi"}


def test_autosend_defers_on_retry_after_hint_even_without_recoverable():
    """429 带 retry_after_ms：改期入队（独立于 recoverable），不记 failed 不留痕。"""
    from src.inbox.autosend_worker import AutosendWorker
    failed, traced = [], []

    async def _cb(*a, **k):
        return {"ok": False, "delivered": False,
                "error": "messenger send failed: ... [send_backoff]",
                "error_kind": "send_backoff", "retry_after_ms": 42000}

    svc = _svc([_l2()])
    svc.record_autosend_failure.side_effect = lambda *a, **k: failed.append(1)
    svc.record_failed_outbound_mirror.side_effect = \
        lambda *a, **k: traced.append(1)
    w = AutosendWorker(draft_service=svc, config={}, send_callback=_cb,
                       sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())
    assert w.total_deferred == 1
    assert len(w._retry_queue) == 1
    assert w._retry_queue[0]["item"]["_deferrals"] == 1
    assert failed == [] and traced == [], "改期中不得记 failed/留痕"


def test_autosend_long_freeze_fails_with_trace_no_defer():
    """2h 风控冻结（retry_after 远超改期上限）：终局失败 + 留痕，不改期。"""
    from src.inbox.autosend_worker import AutosendWorker
    failed, traced = [], []

    async def _cb(*a, **k):
        return {"ok": False, "delivered": False,
                "error": "account temporarily blocked by platform [account_blocked]",
                "error_kind": "account_blocked", "retry_after_ms": 7_200_000}

    svc = _svc([_l2()])
    svc.record_autosend_failure.side_effect = lambda *a, **k: failed.append(1)
    svc.record_failed_outbound_mirror.side_effect = \
        lambda *a, **k: traced.append(1)
    w = AutosendWorker(draft_service=svc, config={}, send_callback=_cb,
                       sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())
    assert w.total_deferred == 0
    assert len(w._retry_queue) == 0
    assert failed == [1] and traced == [1], "超长冻结应终局并留痕"


def test_autosend_deferred_item_retried_when_due_without_recoverable():
    """改期项到期后（recoverable 仍关）必须被重投——排空不再闸 recoverable。"""
    from src.inbox import send_failure_class as sfc
    from src.inbox.autosend_worker import AutosendWorker
    attempts = {"n": 0}

    async def _cb(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return {"ok": False, "delivered": False, "error": "backoff",
                    "error_kind": "send_backoff", "retry_after_ms": 1}
        return {"ok": True, "delivered": True}

    svc = _svc([_l2()])
    w = AutosendWorker(draft_service=svc, config={}, send_callback=_cb,
                       sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())
    assert len(w._retry_queue) == 1
    # 让改期项立即到期（余量固定 5s，直接改 next_ts 而不是睡等）
    w._retry_queue[0]["next_ts"] = 0.0
    svc.list_drafts.return_value = []
    # M-2 C（#233）：账号级退避水位（≥5s）未过时到点项不重投——这里模拟水位已过
    # （真实成功 / 登录成功 / 健康探测通过会 reset），再排空才该重投
    from src.inbox.account_channel_gate import reset_backoff
    reset_backoff("messenger", "a")
    asyncio.run(w._tick())
    assert attempts["n"] == 2, "到期改期项应被重投"
    assert len(w._retry_queue) == 0
    assert svc.resolve_with_audit.call_count == 1, "改期重投不得二次 resolve"
    assert sfc  # 引用防未用导入告警


def test_autosend_defer_cap_then_terminal():
    """连续改期超过上限（3 次）后第 4 次失败终局留痕，防「边车一直给提示」死循环。"""
    from src.inbox.autosend_worker import AutosendWorker
    traced = []

    async def _cb(*a, **k):
        return {"ok": False, "delivered": False, "error": "backoff",
                "error_kind": "send_backoff", "retry_after_ms": 1}

    svc = _svc([_l2()])
    svc.record_failed_outbound_mirror.side_effect = \
        lambda *a, **k: traced.append(1)
    w = AutosendWorker(draft_service=svc, config={}, send_callback=_cb,
                       sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())          # 第 1 次失败 → 改期#1
    from src.inbox.account_channel_gate import reset_backoff
    for _ in range(3):              # 到期重投 → 继续失败 → 改期#2/#3 → 终局
        if not w._retry_queue:
            break
        w._retry_queue[0]["next_ts"] = 0.0
        svc.list_drafts.return_value = []
        reset_backoff("messenger", "a")   # M-2 C（#233）：每轮模拟账号退避水位已过
        asyncio.run(w._tick())
    assert w.total_deferred == 3
    assert len(w._retry_queue) == 0
    assert traced == [1], "改期耗尽后应终局留痕一次"


# ── ③ 手发失败留痕 helper ────────────────────────────────────────────────────

class _Req:
    """最小 request 假件：只带 app.state.inbox_store。"""

    def __init__(self, store):
        import types
        self.app = types.SimpleNamespace(
            state=types.SimpleNamespace(inbox_store=store))


def test_trace_failed_manual_send_writes_b63_trace():
    from src.web.routes.unified_inbox_send_routes import (
        _trace_failed_manual_send)
    store = MagicMock()
    store.record_failed_outbound.return_value = "cid:fail:abc"
    mid = _trace_failed_manual_send(
        _Req(store), "messenger", "acct1", "peer9",
        "hello there", "send_backoff")
    assert mid == "cid:fail:abc"
    args, kwargs = store.record_failed_outbound.call_args
    assert args[0] == "messenger:acct1:peer9"
    assert args[1] == "hello there"
    assert kwargs["reason"] == "send_backoff"


def test_trace_failed_manual_send_soft_fails():
    from src.web.routes.unified_inbox_send_routes import (
        _trace_failed_manual_send)
    # store 缺席 / 旧 store 无方法 / 抛异常 / 空文本，一律回空串不炸
    assert _trace_failed_manual_send(
        _Req(None), "messenger", "a", "c", "x", "r") == ""
    legacy = object()   # 无 record_failed_outbound 属性
    assert _trace_failed_manual_send(
        _Req(legacy), "messenger", "a", "c", "x", "r") == ""
    boom = MagicMock()
    boom.record_failed_outbound.side_effect = RuntimeError("db locked")
    assert _trace_failed_manual_send(
        _Req(boom), "messenger", "a", "c", "x", "r") == ""
    ok = MagicMock()
    assert _trace_failed_manual_send(
        _Req(ok), "messenger", "a", "c", "   ", "r") == ""
    ok.record_failed_outbound.assert_not_called()


def test_humanize_send_failure_maps_and_falls_back():
    """能归类出人话（附原始码）；归不了类原文返回。用假 request 走真 tr()。"""
    from src.web.routes.unified_inbox_send_routes import _humanize_send_failure

    class _R:
        class state:  # noqa: N801 - 模拟 request.state
            ui_lang = "zh"

    human = _humanize_send_failure(
        _R(), "Client error '429 Too Many Requests' ...", "send_backoff")
    assert "限频" in human and "send_backoff" in human
    raw = _humanize_send_failure(_R(), "weird unknown hiccup", "")
    assert raw == "weird unknown hiccup"
