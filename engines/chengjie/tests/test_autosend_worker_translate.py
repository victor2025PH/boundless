"""AutosendWorker × 出站翻译回调集成单测（增量8）。

锁定：
  - 无 translate_callback → 发原文（向后兼容，旧行为不变）
  - 有 translate_callback → 投递前文本被替换为译文，total_translated 自增
  - translate_callback 抛异常 → 回落发原文，不阻塞投递（仍 total_delivered）
  - 翻译在 send 之前发生（顺序锁定）
"""

from __future__ import annotations

import pytest

from src.inbox.autosend_worker import AutosendWorker


class _FakeSvc:
    def __init__(self):
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d1", "autopilot_level": "L2",
            "final_text": "你好呀~", "platform": "telegram",
            "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


@pytest.mark.asyncio
async def test_no_translate_callback_sends_original():
    sent = []

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(draft_service=_FakeSvc(), send_callback=_send_cb)
    await w._tick()
    assert sent == ["你好呀~"]
    assert w.total_translated == 0
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_translate_callback_replaces_text():
    events = []

    async def _translate_cb(item):
        events.append(("translate", item["text"]))
        return "Hello~"

    async def _send_cb(p, a, c, text):
        events.append(("send", text))
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert ("send", "Hello~") in events
    assert events.index(("translate", "你好呀~")) < events.index(("send", "Hello~"))
    assert w.total_translated == 1
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_translate_same_text_no_counter_bump():
    async def _translate_cb(item):
        return item["text"]  # 回落原文（同文）

    sent = []

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert sent == ["你好呀~"]
    assert w.total_translated == 0   # 译文==原文不计


@pytest.mark.asyncio
async def test_translate_exception_falls_back_to_original():
    sent = []

    async def _translate_cb(item):
        raise RuntimeError("translate boom")

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert sent == ["你好呀~"]        # 异常回落原文
    assert w.total_delivered == 1     # 投递未被阻塞


def test_status_snapshot_exposes_translate_fields():
    w = AutosendWorker(draft_service=_FakeSvc())
    snap = w.status_snapshot()
    assert snap["translate_enabled"] is False
    assert snap["total_translated"] == 0
    assert snap["mark_read_enabled"] is False
    assert snap["total_marked_read"] == 0


@pytest.mark.asyncio
async def test_original_text_passed_to_callback_that_accepts_it():
    """出站翻译生效时：接受 original_text 的回调应同时拿到译文（text 位）与原文（kwarg）。"""
    got = {}

    async def _translate_cb(item):
        return "Hello~"

    async def _send_cb(p, a, c, text, original_text=None):
        got["text"] = text
        got["original_text"] = original_text
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert got == {"text": "Hello~", "original_text": "你好呀~"}


@pytest.mark.asyncio
async def test_legacy_four_arg_callback_still_works():
    """旧 4 参回调（无 original_text）：签名探测后不透传 kwarg，行为不变。"""
    sent = []

    async def _translate_cb(item):
        return "Hello~"

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert sent == ["Hello~"]
    assert w.total_delivered == 1


@pytest.mark.asyncio
async def test_mark_read_called_before_send():
    """拟人已读回执：投递前先 mark_read（顺序锁定：已读 → 发送），并计数。"""
    events = []

    async def _mark_read_cb(p, a, c):
        events.append(("mark_read", p, a, c))

    async def _send_cb(p, a, c, text):
        events.append(("send", text))
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        mark_read_callback=_mark_read_cb,
    )
    await w._tick()
    assert events[0] == ("mark_read", "telegram", "a1", "c1")
    assert events[1] == ("send", "你好呀~")
    assert w.total_marked_read == 1
    snap = w.status_snapshot()
    assert snap["mark_read_enabled"] is True
    assert snap["total_marked_read"] == 1


@pytest.mark.asyncio
async def test_mark_read_failure_does_not_block_delivery():
    """已读回执异常 → 只记 debug，不阻断投递、不计入 marked_read。"""
    sent = []

    async def _mark_read_cb(p, a, c):
        raise RuntimeError("read boom")

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        mark_read_callback=_mark_read_cb,
    )
    await w._tick()
    assert sent == ["你好呀~"]
    assert w.total_delivered == 1
    assert w.total_marked_read == 0


@pytest.mark.asyncio
async def test_typing_indicator_kept_during_deliver_delay():
    """打字状态：deliver_delay 期间按 4s 分片周期挂「正在输入」，且在发送前。"""
    events = []

    async def _typing_cb(p, a, c, action):
        events.append(("typing", action))

    async def _send_cb(p, a, c, text):
        events.append(("send", text))
        return {"ok": True}

    async def _fast_sleep(_s):
        return None

    # deliver_delay 10s → 需 4s/4s/2s 三次续挂
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 10, "max_sec": 10}},
        send_callback=_send_cb,
        typing_callback=_typing_cb,
        sleep=_fast_sleep,
    )
    await w._tick()
    typings = [e for e in events if e[0] == "typing"]
    assert len(typings) == 3
    assert all(a == "typing" for _, a in typings)
    # 所有 typing 都在 send 之前
    assert events.index(("send", "你好呀~")) == len(events) - 1
    snap = w.status_snapshot()
    assert snap["typing_enabled"] is True


@pytest.mark.asyncio
async def test_adaptive_deliver_delay_scales_with_text_length():
    """deliver_delay.adaptive=true → 长回复的投递延迟长于短回复（按内容估时）。"""
    slept = {"total": 0.0}

    async def _sleep(s):
        slept["total"] += s

    async def _send_cb(p, a, c, text):
        return {"ok": True}

    class _Svc:
        def __init__(self, reply):
            self._reply = reply

        def list_drafts(self, status="pending", limit=200):
            return [{
                "draft_id": "d1", "autopilot_level": "L2",
                "final_text": self._reply, "platform": "telegram",
                "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
            }]

        def resolve_with_audit(self, draft_id, action, by=""):
            return {"ok": True}

    cfg = {"deliver_delay": {"min_sec": 0.0, "max_sec": 60.0, "adaptive": True,
                             "per_char_sec": 0.1, "jitter": 0}}

    w_short = AutosendWorker(draft_service=_Svc("嗨"), config=cfg,
                             send_callback=_send_cb, sleep=_sleep)
    await w_short._tick()
    short_total = slept["total"]

    slept["total"] = 0.0
    w_long = AutosendWorker(draft_service=_Svc("这是一段明显更长的回复内容" * 4),
                            config=cfg, send_callback=_send_cb, sleep=_sleep)
    await w_long._tick()
    long_total = slept["total"]

    assert long_total > short_total   # 长回复等更久


@pytest.mark.asyncio
async def test_adaptive_delay_deducts_elapsed_from_created_ts():
    """adaptive=true 且草稿 created_at 久远 → 已耗时扣满 → 投递延迟≈0（不再叠加等待）。"""
    import time as _t
    slept = {"total": 0.0}

    async def _sleep(s):
        slept["total"] += s

    async def _send_cb(p, a, c, text):
        return {"ok": True}

    class _Svc:
        def list_drafts(self, status="pending", limit=200):
            return [{
                "draft_id": "d1", "autopilot_level": "L2",
                "final_text": "内容内容内容内容", "platform": "telegram",
                "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
                # 创建于 300s 前 → 已耗时远超任何目标
                "created_at": _t.time() - 300.0,
            }]

        def resolve_with_audit(self, draft_id, action, by=""):
            return {"ok": True}

    cfg = {"deliver_delay": {"min_sec": 0.0, "max_sec": 30.0, "adaptive": True,
                             "per_char_sec": 0.2, "jitter": 0}}
    w = AutosendWorker(draft_service=_Svc(), config=cfg,
                       send_callback=_send_cb, sleep=_sleep)
    await w._tick()
    assert slept["total"] == 0.0    # 已等够 → 立即发


@pytest.mark.asyncio
async def test_persona_resolver_drives_persona_scoped_pacing():
    """注入 persona_resolver → 用对应人设的 persona_overrides 估延迟，观测按人设分维。

    同一文本（arousal 缩放相同）下，比较 base_sec 大的人设 vs 小的人设：前者延迟更长，
    隔离掉自动 arousal 的影响。"""
    import src.integrations.humanize_metrics as hm

    def _run_with_persona(pid, base_sec):
        hm.reset()
        slept = {"total": 0.0}

        async def _sleep(s):
            slept["total"] += s

        async def _send_cb(p, a, c, text):
            return {"ok": True}

        class _Svc:
            def list_drafts(self, status="pending", limit=200):
                return [{
                    "draft_id": "d1", "autopilot_level": "L2",
                    "final_text": "内容内容内容内容内容", "platform": "telegram",
                    "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
                }]

            def resolve_with_audit(self, draft_id, action, by=""):
                return {"ok": True}

        cfg = {"deliver_delay": {
            "min_sec": 0.0, "max_sec": 60.0, "adaptive": True, "per_char_sec": 0.0,
            "jitter": 0,
            "persona_overrides": {"slow": {"base_sec": 8.0}, "fast": {"base_sec": 1.0}}}}
        w = AutosendWorker(
            draft_service=_Svc(), config=cfg, send_callback=_send_cb, sleep=_sleep,
            persona_resolver=lambda p, a: pid)
        return w, slept

    w_slow, slept_slow = _run_with_persona("slow", 8.0)
    await w_slow._tick()
    snap_slow = hm.pacing_snapshot()
    assert "autosend/slow" in snap_slow           # 观测按人设分维
    slow_total = slept_slow["total"]

    w_fast, slept_fast = _run_with_persona("fast", 1.0)
    await w_fast._tick()
    assert "autosend/fast" in hm.pacing_snapshot()
    fast_total = slept_fast["total"]

    assert slow_total > fast_total                # base_sec 大的人设延迟更长
    hm.reset()


@pytest.mark.asyncio
async def test_typing_failure_does_not_block_delivery():
    """打字状态异常 → 吞掉，照常睡完延迟并投递。"""
    sent = []

    async def _typing_cb(p, a, c, action):
        raise RuntimeError("typing boom")

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    async def _fast_sleep(_s):
        return None

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 5, "max_sec": 5}},
        send_callback=_send_cb,
        typing_callback=_typing_cb,
        sleep=_fast_sleep,
    )
    await w._tick()
    assert sent == ["你好呀~"]
    assert w.total_delivered == 1


class _EmptyDraftSvc:
    """L2 草稿正文为空（回填失败/竞态）——投递模式下必须被跳过，绝不标记已发。"""

    def __init__(self):
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [{
            "draft_id": "d_empty", "autopilot_level": "L2",
            "final_text": "", "draft_text": "", "platform": "telegram",
            "account_id": "a1", "chat_key": "c1", "conversation_id": "x1",
        }]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


@pytest.mark.asyncio
async def test_empty_draft_skipped_not_marked_sent():
    """投递模式：空正文 L2 草稿不 resolve、不投递、不计入 sent（防『只标记不真发』）。"""
    svc = _EmptyDraftSvc()
    sent = []

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(draft_service=svc, send_callback=_send_cb)
    await w._tick()
    assert svc.resolved == []          # 空草稿没有被 resolve（不会被标记 approved/已发）
    assert sent == []                  # 没有任何投递
    assert w.total_sent == 0
    assert w.total_delivered == 0


@pytest.mark.asyncio
async def test_empty_draft_still_resolved_when_no_delivery():
    """非投递模式（send_callback=None，旧『仅 DB 标记』行为）：保持向后兼容，仍 resolve。"""
    svc = _EmptyDraftSvc()
    w = AutosendWorker(draft_service=svc)  # 无 send_callback
    await w._tick()
    assert svc.resolved == ["d_empty"]
    assert w.total_sent == 1
