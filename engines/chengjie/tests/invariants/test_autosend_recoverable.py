"""消息不变量 · autosend 投递可恢复（Sprint2）。

固定：
  - 默认 recoverable 关 → 保持「宁丢不重发」：瞬时失败即 record_autosend_failure、不重排。
  - recoverable 开 → 瞬时失败进重试队列（不立即记 failed）；重试成功计入 recovered；
    永久失败不重试、直接记 failed + 发提醒事件。
"""
import asyncio

from unittest.mock import MagicMock

from src.inbox.autosend_worker import AutosendWorker


def _svc(drafts):
    s = MagicMock()
    s.list_drafts.return_value = drafts
    s.resolve_with_audit.return_value = {"ok": True}
    return s


def _l2(cid="line:a:c1", draft_id="d1"):
    return {"draft_id": draft_id, "autopilot_level": "L2", "status": "pending",
            "platform": "line", "account_id": "a", "chat_key": "c1",
            "conversation_id": cid, "draft_text": "hi"}


def test_default_not_recoverable_records_failure_no_retry():
    """recoverable 默认关：瞬时失败 → 记一次 failed，不进重试队列（旧行为）。"""
    calls = []

    async def _cb(*a, **k):
        raise RuntimeError("temporary network glitch")

    svc = _svc([_l2()])
    svc.record_autosend_failure.side_effect = lambda *a, **k: calls.append(1)
    w = AutosendWorker(draft_service=svc, config={}, send_callback=_cb, sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())
    assert w.total_deliver_errors == 1
    assert len(w._retry_queue) == 0
    assert w.total_retry_scheduled == 0
    assert calls == [1], "默认应记一次 autosend_failure"


def test_recoverable_transient_enqueues_retry_not_failed():
    """recoverable 开：瞬时失败 → 进重试队列、暂不记 failed。"""
    calls = []

    async def _cb(*a, **k):
        raise RuntimeError("temporary network glitch")

    svc = _svc([_l2()])
    svc.record_autosend_failure.side_effect = lambda *a, **k: calls.append(1)
    cfg = {"recoverable": {"enabled": True, "max_attempts": 3, "backoff_base_sec": 10}}
    w = AutosendWorker(draft_service=svc, config=cfg, send_callback=_cb, sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())
    assert w.total_deliver_errors == 1
    assert w.total_retry_scheduled == 1
    assert len(w._retry_queue) == 1
    assert calls == [], "重试中不应立即记 failed"


def test_recoverable_permanent_no_retry_records_failed():
    """recoverable 开：永久失败（被拉黑）→ 不重试，直接记 failed。"""
    calls = []

    async def _cb(*a, **k):
        raise RuntimeError("USER_IS_BLOCKED")

    svc = _svc([_l2()])
    svc.record_autosend_failure.side_effect = lambda *a, **k: calls.append(1)
    cfg = {"recoverable": {"enabled": True, "max_attempts": 3}}
    w = AutosendWorker(draft_service=svc, config=cfg, send_callback=_cb, sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())
    assert w.total_retry_scheduled == 0, "永久错误不重试"
    assert len(w._retry_queue) == 0
    assert calls == [1], "永久失败应记 failed"


def test_recoverable_retry_recovers_on_next_tick():
    """重试到期后再投递成功 → total_retry_recovered 增加，不二次 resolve。"""
    attempts = {"n": 0}

    async def _cb(*a, **k):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("temporary")
        return {"ok": True}

    svc = _svc([_l2()])
    cfg = {"recoverable": {"enabled": True, "max_attempts": 3, "backoff_base_sec": 0}}
    w = AutosendWorker(draft_service=svc, config=cfg, send_callback=_cb, sleep=lambda s: asyncio.sleep(0))
    asyncio.run(w._tick())  # 首投失败 → 入队（backoff 0 → 立即到期）
    assert w.total_retry_scheduled == 1
    # 第二轮：list_drafts 返回空（草稿已 resolve），仅重试队列投递
    svc.list_drafts.return_value = []
    asyncio.run(w._tick())
    assert w.total_retry_recovered == 1
    assert svc.resolve_with_audit.call_count == 1, "重试不应二次 resolve"
