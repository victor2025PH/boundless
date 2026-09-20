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
async def test_translate_hold_none_blocks_delivery():
    """P0-198：回调返回 None（文本含 CJK 而客户语言非 CJK 且翻译不可用的 HOLD 信号）
    → 绝不发原文（发中文给外语客户=人设穿帮），按投递失败走审计/重试链。"""
    sent = []

    async def _translate_cb(item):
        return None

    async def _send_cb(p, a, c, text):
        sent.append(text)
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=_send_cb,
        translate_callback=_translate_cb,
    )
    await w._tick()
    assert sent == []                       # 一个字都没发出去
    assert w.total_delivered == 0
    assert w.total_deliver_errors == 1
    assert "translate_hold" in str(w.last_error)


@pytest.mark.asyncio
async def test_human_deliver_translate_hold_returns_error():
    """人工通过链同口径：HOLD → 返回失败（坐席铃铛可见），绝不静默发原文。"""
    async def _translate_cb(item):
        return None

    async def _send_cb(p, a, c, text):
        return {"ok": True}

    w = AutosendWorker(
        draft_service=_FakeSvc(),
        send_callback=None,
        human_send_callback=_send_cb,
        translate_callback=_translate_cb,
        deliver_only=True,
    )
    res = await w.deliver_human_approved({
        "draft_id": "d1", "conversation_id": "x1", "platform": "telegram",
        "account_id": "a1", "chat_key": "c1", "final_text": "你好呀~",
    })
    assert res["ok"] is False
    assert "translate_hold" in str(res.get("error") or "")
    assert w.total_human_deliver_errors == 1


@pytest.mark.asyncio
async def test_translate_exception_holds_no_original_send():
    """无兜底纪律（2026-08-17）：翻译回调异常＝HOLD 不发——旧「异常发原文」拆除。"""
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
    assert sent == []                 # 一个字都没发出（不发原文）
    assert w.total_delivered == 0     # 走投递失败链（重试/审计），不算成功


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
    """打字状态两段式（2026-08-04/09）：延迟前段静默（真人在想，无输入状态），
    临发前 typing_lead（按文本长度×手速估，短文本下限 1.2s）才挂「正在输入」，
    且在发送之前——全程挂打字＝「打了 10 秒字只打出一句话」比不挂更假。"""
    events = []

    async def _typing_cb(p, a, c, action):
        events.append(("typing", action))

    async def _send_cb(p, a, c, text):
        events.append(("send", text))
        return {"ok": True}

    async def _fast_sleep(_s):
        return None

    # deliver_delay 10s：静默 ~8.8s + 尾部 typing_lead ~1.2s（短文本下限）
    # → 恰好一次挂「正在输入」，紧邻发送
    w = AutosendWorker(
        draft_service=_FakeSvc(),
        config={"deliver_delay": {"min_sec": 10, "max_sec": 10}},
        send_callback=_send_cb,
        typing_callback=_typing_cb,
        sleep=_fast_sleep,
    )
    await w._tick()
    typings = [e for e in events if e[0] == "typing"]
    assert len(typings) == 1, (
        f"两段式应只在临发前挂一次打字（短文本 lead≈1.2s），实得 {len(typings)}")
    assert all(a == "typing" for _, a in typings)
    _send_idx = next(i for i, e in enumerate(events) if e[0] == "send")
    assert events.index(typings[0]) < _send_idx, "打字必须发生在发送之前"
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
    # 2026-08-09 观测路径升级为 autosend/{platform|-}/{persona|-}（平台/人设
    # 双分维，见 _pick_deliver_delay docstring；旧单段格式前端已兼容两代）
    assert "autosend/telegram/slow" in snap_slow  # 观测按 平台/人设 分维
    slow_total = slept_slow["total"]

    w_fast, slept_fast = _run_with_persona("fast", 1.0)
    await w_fast._tick()
    assert "autosend/telegram/fast" in hm.pacing_snapshot()
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


# ── B125（2026-08-28）：译文出口的混语守卫 ──────────────────────────────────
#
# 事故：「You know I'm here, same 我」「im 我」直发客户。B121 的守卫挂在出稿口，
# 而出站翻译在它之后——译文自此再没有任何语种检查，MT 漏译的代词就这么出站了。


class _FakeAssistant:
    """最小 assistant 替身：守卫只用 .config.config 与 .logger。"""

    class _Cfg:
        def __init__(self, d):
            self.config = d

    class _Log:
        def __init__(self):
            self.warnings = []

        def warning(self, *a, **k):
            self.warnings.append(a[0] if a else "")

        def debug(self, *a, **k):
            pass

        def info(self, *a, **k):
            pass

    def __init__(self, cfg=None):
        self.config = self._Cfg(cfg if cfg is not None else {})
        self.logger = self._Log()


def test_translated_lang_mix_stripped():
    """生产实录样本：英文主体夹单个汉字 → 剥除后才投递。"""
    from src.inbox.autosend_helpers import _guard_translated_lang_mix
    a = _FakeAssistant()
    out = _guard_translated_lang_mix(a, "你知道我在这儿", "You know I'm here, same 我")
    assert "我" not in out
    assert "You know" in out
    assert a.logger.warnings, "剥除必须留痕（否则 MT 质量问题再次无声）"


def test_translated_hold_passes_through():
    """翻译 HOLD（None）语义必须原样透传——守卫绝不能把不发改成放行。"""
    from src.inbox.autosend_helpers import _guard_translated_lang_mix
    assert _guard_translated_lang_mix(_FakeAssistant(), "你好", None) is None


def test_clean_translation_untouched():
    """正常译文零改动（中文译文、含品牌词的英文译文都不许误伤）。"""
    from src.inbox.autosend_helpers import _guard_translated_lang_mix
    a = _FakeAssistant()
    for txt in ("Hello, how are you today?",
                "你好呀，今天过得怎么样？",
                "I use iPhone and WhatsApp every day."):
        assert _guard_translated_lang_mix(a, "src", txt) == txt
    assert not a.logger.warnings


def test_guard_respects_operator_switch():
    """运营显式关掉 lang_mix → 守卫不动手（与出稿口同口径）。"""
    from src.inbox.autosend_helpers import _guard_translated_lang_mix
    a = _FakeAssistant(
        {"companion": {"outbound_text_guard": {"lang_mix": False}}})
    bad = "You know I'm here, same 我"
    assert _guard_translated_lang_mix(a, "src", bad) == bad


def test_short_latin_fragment_not_over_stripped():
    """「im 我」拉丁不足阈值 → 刻意不动（宁可漏拦不误伤，与 B121 同哲学）。

    留此条是让「阈值该不该下调」成为一次显式决策，而不是被顺手改掉。
    """
    from src.inbox.autosend_helpers import _guard_translated_lang_mix
    assert _guard_translated_lang_mix(_FakeAssistant(), "src", "im 我") == "im 我"


def test_translate_cb_wires_guard():
    """接线锚点：守卫必须长在翻译回调出口（两条投递链共用它）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "src" / "inbox" / "autosend_helpers.py").read_text(encoding="utf-8")
    assert "return _guard_translated_lang_mix(" in src, "翻译回调未接守卫"
