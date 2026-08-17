"""AutosendWorker 投递拟人延迟（Phase 4）单测。

锁定：
  - deliver_delay 未配置 → 0（向后兼容，不延迟）
  - 配置后 _pick_deliver_delay 落在 [min, max]
  - _tick 投递时「先延迟后发送」，且 sleep 可注入（不真等）
"""

from __future__ import annotations

import pytest

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    """最小草稿服务：恒返回一条 L2 待发草稿，resolve 恒成功。"""

    def __init__(self):
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d1", "autopilot_level": "L2",
            "final_text": "您好呀~", "platform": "telegram",
            "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def test_no_delay_by_default():
    w = AutosendWorker(draft_service=_FakeSvc())
    assert w._pick_deliver_delay() == 0.0


def test_pick_delay_within_range():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 1.0, "max_sec": 2.0}},
    )
    for _ in range(20):
        d = w._pick_deliver_delay()
        assert 1.0 <= d <= 2.0


def test_invalid_range_yields_zero():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 5.0, "max_sec": 1.0}},
    )
    assert w._pick_deliver_delay() == 0.0


@pytest.mark.asyncio
async def test_tick_delays_before_send():
    events = []

    async def _fake_sleep(d):
        events.append(("sleep", d))

    async def _send_cb(platform, account_id, chat_key, text):
        events.append(("send", text))
        return {"ok": True}

    svc = _FakeSvc()
    w = AutosendWorker(
        draft_service=svc,
        config={"deliver_delay": {"min_sec": 0.5, "max_sec": 0.5}},
        send_callback=_send_cb,
        sleep=_fake_sleep,
    )
    await w._tick()
    # 先 sleep 再 send，且 sleep 时长在配置区间
    assert ("sleep", 0.5) in events
    assert ("send", "您好呀~") in events
    assert events.index(("sleep", 0.5)) < events.index(("send", "您好呀~"))
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_tick_no_sleep_when_unconfigured():
    events = []

    async def _fake_sleep(d):
        events.append(("sleep", d))

    async def _send_cb(platform, account_id, chat_key, text):
        events.append(("send", text))
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        sleep=_fake_sleep,
    )
    await w._tick()
    assert not any(e[0] == "sleep" for e in events)
    assert ("send", "您好呀~") in events


# ── P1 同会话连发最小间隔地板（min_gap_sec，2026-08-12）────────────────────────
# 修 198 实录「第 2/3 条秒回」：串行队列里后续草稿的 adaptive 抵扣把排队等待
# 算成已耗时 → 延迟归零 → 同一客户背靠背收到机关枪。地板只看「距本会话上一条
# 出站多久」，与抵扣正交；账本由自动/人工两条投递链共写。


def test_gap_floor_first_send_not_padded():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {
            "min_sec": 0, "max_sec": 30, "adaptive": True, "min_gap_sec": 15}},
    )
    # 进程内首条出站：地板不介入（elapsed 远超目标 → adaptive 归零）
    assert w._pick_deliver_delay(
        "短", elapsed_sec=100.0, conversation_id="x1") == 0.0
    assert w.total_gap_floored == 0


def test_gap_floor_pads_consecutive_send():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {
            "min_sec": 0, "max_sec": 30, "adaptive": True, "min_gap_sec": 15}},
    )
    w._note_conv_sent("x1")   # 刚发过一条
    d = w._pick_deliver_delay("短", elapsed_sec=100.0, conversation_id="x1")
    # 目标间隔带 ±15% 抖动 → 需再等 ∈ [15*0.85, 15*1.15]（since≈0）
    assert 15 * 0.85 - 0.5 <= d <= 15 * 1.15
    assert w.total_gap_floored == 1


def test_gap_floor_scoped_to_conversation():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {
            "min_sec": 0, "max_sec": 30, "adaptive": True, "min_gap_sec": 15}},
    )
    w._note_conv_sent("x1")
    # 另一个会话不受 x1 的账本影响（地板是「同一客户」语义，不是全局限速）
    assert w._pick_deliver_delay(
        "短", elapsed_sec=100.0, conversation_id="other") == 0.0
    assert w.total_gap_floored == 0


def test_gap_floor_disabled_by_default():
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 0, "max_sec": 30, "adaptive": True}},
    )
    w._note_conv_sent("x1")
    assert w._pick_deliver_delay(
        "短", elapsed_sec=100.0, conversation_id="x1") == 0.0


@pytest.mark.asyncio
async def test_gap_padded_delay_flows_through_typing_indicator():
    """P2-1 契约：地板垫出的等待必须走同一条拟人序列（静默思考 → 尾段挂
    「正在输入」→ 投递），而不是裸 sleep——否则客户视角是「已读后长时间
    无声无息突然弹消息」，垫得越久越像机器人。钉住：垫付延迟期间 typing
    回调被调用，且总 sleep ≈ 垫付后的延迟值。"""
    sleeps, typed = [], []

    async def _fake_sleep(d):
        sleeps.append(float(d))

    async def _send_cb(platform, account_id, chat_key, text):
        return {"ok": True}

    async def _typing_cb(platform, account_id, chat_key, action):
        typed.append(action)

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {
            "min_sec": 0, "max_sec": 30, "adaptive": True, "min_gap_sec": 15}},
        send_callback=_send_cb,
        typing_callback=_typing_cb,
        sleep=_fake_sleep,
    )
    w._note_conv_sent("x1")   # 刚发过一条 → 本次 _tick 的草稿(x1)吃地板
    await w._tick()
    assert w.total_gap_floored == 1
    # 垫付的等待走了拟人序列：有 sleep 分段，且尾段挂了「正在输入」
    total = sum(sleeps)
    assert 15 * 0.85 - 0.5 <= total <= 15 * 1.15 + 0.5
    assert "typing" in typed


@pytest.mark.asyncio
async def test_tick_records_conv_sent_and_snapshot_field():
    async def _send_cb(platform, account_id, chat_key, text):
        return {"ok": True}

    async def _fake_sleep(d):
        pass

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        sleep=_fake_sleep,
    )
    await w._tick()
    # 投递成功 → 会话账本有记录（下一条同会话即可垫地板）
    assert w._since_conv_sent("x1") is not None
    assert w._since_conv_sent("never-sent") is None
    assert w.status_snapshot().get("total_gap_floored") == 0
