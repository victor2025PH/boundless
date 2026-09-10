"""工作时间闸门在自动回复各出口的接线（P0-ws，2026-08-04）。

钉住四条安全语义：
- B 线 AutosendWorker：休息中 L2 草稿**留 pending 不处置**（不 resolve 不取消），
  复班自动接续；危机消息穿透照发；provider 未注入=零行为变更；
- 人工通道不受闸：``deliver_human_approved`` 休息中照发（人已明示）；
- 复班补觉：稿龄超阈值 → 作废 + 经原拟稿产线重拟；**重拟回调未注入绝不作废**；
  每批预算封顶；
- 拟稿节流 / companion 双轨互斥 / 协议号直发与 A 线同一 should_hold 口径。

班表窗口按「当前时刻 ±偏移」动态构造（UTC 显式时区、抖动 0、边距 ≥2h），
与跑测试的机器时区/时刻无关。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.inbox.autosend_worker import AutosendWorker


def _window(offset_start_h: float, offset_end_h: float):
    now = datetime.now(timezone.utc)
    s = (now + timedelta(hours=offset_start_h)).strftime("%H:%M")
    e = (now + timedelta(hours=offset_end_h)).strftime("%H:%M")
    return s, e


def _ws_cfg(in_hours: bool, **over):
    """此刻恒在班（窗口 now±2h）/ 恒休息（窗口 now+2h..now+4h）的班表。"""
    s, e = _window(-2, 2) if in_hours else _window(2, 4)
    cfg = {
        "enabled": True,
        "timezone": "UTC",
        "edge_jitter_min": 0,
        "default": {"start": s, "end": e},
    }
    cfg.update(over)
    return cfg


class _FakeStore:
    def __init__(self):
        self.cancelled = []  # (draft_id, status, decided_by)

    def update_draft_status(self, draft_id, *, status, final_text="",
                            decided_by=""):
        self.cancelled.append((draft_id, status, decided_by))
        return True


class _Svc:
    def __init__(self, drafts):
        self._drafts = drafts
        self._store = _FakeStore()
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [dict(d) for d in self._drafts]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def _draft(draft_id="d1", peer_text="在吗？", age_sec=30):
    return {
        "draft_id": draft_id, "autopilot_level": "L2",
        "final_text": "在的呀~", "platform": "telegram",
        "account_id": "acct1", "chat_key": "c1",
        "conversation_id": f"telegram:acct1:{draft_id}",
        "peer_text": peer_text,
        "created_ts": time.time() - age_sec,
    }


async def _noop_send(platform, account_id, chat_key, text):
    return {"delivered": True}


# ── B 线：扣留语义 ──────────────────────────────────────────


def test_off_hours_holds_pending_without_resolve():
    svc = _Svc([_draft()])
    ws = _ws_cfg(in_hours=False)
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws)
    sent, errors, to_deliver = w._process_batch()
    assert to_deliver == [] and sent == 0 and errors == 0
    assert svc.resolved == []            # 不 resolve
    assert svc._store.cancelled == []    # 不取消 → 留 pending 等复班
    assert w.total_skipped_off_hours == 1
    snap = w.status_snapshot()
    assert snap["work_schedule_enabled"] is True
    assert snap["total_skipped_off_hours"] == 1


def test_in_hours_delivers_normally():
    svc = _Svc([_draft()])
    ws = _ws_cfg(in_hours=True)
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert w.total_skipped_off_hours == 0


def test_crisis_peer_text_bypasses_hold():
    svc = _Svc([_draft(peer_text="我真的不想活了")])
    ws = _ws_cfg(in_hours=False)
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1  # 危机穿透照发
    assert w.total_skipped_off_hours == 0


def test_no_provider_means_legacy_behavior():
    svc = _Svc([_draft()])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send, config={})
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert w.status_snapshot()["work_schedule_enabled"] is False


def test_disabled_schedule_does_not_hold():
    svc = _Svc([_draft()])
    ws = _ws_cfg(in_hours=False)
    ws["enabled"] = False
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1


def test_broken_provider_fails_open():
    svc = _Svc([_draft()])

    def _boom():
        raise RuntimeError("provider down")

    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_boom)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1  # 闸门故障绝不闸死自动回复


# ── 下班收尾宽限 ────────────────────────────────────────────


def _ws_just_closed():
    """窗口 [now-4h, now-6min]：此刻刚下班 6 分钟。"""
    s, e = _window(-4, -0.1)
    return {"enabled": True, "timezone": "UTC", "edge_jitter_min": 0,
            "default": {"start": s, "end": e}}


def test_wrapup_grace_delivers_drafts_born_in_hours():
    # 稿在班内拟出（10 分钟前）、刚过下班边界 → 放行把最后一句说完
    svc = _Svc([_draft(age_sec=600)])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_ws_just_closed)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert w.total_skipped_off_hours == 0


def test_wrapup_grace_capped_at_15min():
    # 同样在班内拟出、但已过去 40 分钟 → 超宽限硬顶，正常扣留
    svc = _Svc([_draft(age_sec=2400)])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_ws_just_closed)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 0 and to_deliver == []
    assert w.total_skipped_off_hours == 1


def test_wrapup_grace_not_for_off_hours_drafts():
    # 稿本身就是休息期拟的（深夜入站）→ 不适用宽限，照常扣留
    svc = _Svc([_draft(age_sec=120)])
    ws = _ws_cfg(in_hours=False)
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 0 and to_deliver == []
    assert w.total_skipped_off_hours == 1


# ── 人工通道不受闸 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_human_deliver_not_gated_off_hours():
    sent_calls = []

    async def _cb(platform, account_id, chat_key, text):
        sent_calls.append((platform, chat_key, text))
        return {"delivered": True}

    ws = _ws_cfg(in_hours=False)
    w = AutosendWorker(draft_service=_Svc([]), send_callback=_cb,
                       config={}, work_schedule_provider=lambda: ws)
    res = await w.deliver_human_approved({
        "draft_id": "h1", "conversation_id": "telegram:acct1:h1",
        "platform": "telegram", "account_id": "acct1",
        "chat_key": "c9", "final_text": "人工通过的回复",
    })
    assert res.get("ok") is True
    assert sent_calls and sent_calls[0][2] == "人工通过的回复"
    assert w.total_human_delivered == 1


# ── 复班补觉 ────────────────────────────────────────────────


def _catchup_ws():
    ws = _ws_cfg(in_hours=True)
    ws["off_hours"] = {"catch_up": True, "catch_up_regenerate_hours": 2}
    return ws


def test_catchup_regenerates_stale_draft():
    svc = _Svc([_draft(age_sec=4 * 3600)])  # 稿龄 4h > 2h
    regen_calls = []

    def _regen(conv, text):
        regen_calls.append((conv, text))
        return True

    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_catchup_ws,
                       catchup_regenerate_cb=_regen)
    sent, _errors, to_deliver = w._process_batch()
    assert to_deliver == [] and sent == 0
    assert svc._store.cancelled == [("d1", "cancelled", "work_schedule_regen")]
    assert regen_calls and regen_calls[0][0]["conversation_id"] == \
        "telegram:acct1:d1"
    assert regen_calls[0][1] == "在吗？"
    assert w.total_catchup_regenerated == 1
    assert w.status_snapshot()["catchup_regen_wired"] is True


def test_catchup_without_cb_never_cancels():
    # 铁律：重拟回调未注入 → 绝不作废（宁发陈稿不丢回复）
    svc = _Svc([_draft(age_sec=4 * 3600)])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_catchup_ws)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1  # 原样投递
    assert svc._store.cancelled == []
    assert w.status_snapshot()["catchup_regen_wired"] is False


def test_catchup_fresh_draft_delivers_normally():
    svc = _Svc([_draft(age_sec=600)])  # 10 分钟 < 2h
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_catchup_ws,
                       catchup_regenerate_cb=lambda conv, text: True)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert svc._store.cancelled == []


def test_catchup_budget_caps_per_batch():
    drafts = [_draft(draft_id=f"d{i}", age_sec=4 * 3600) for i in range(8)]
    svc = _Svc(drafts)
    regen_calls = []
    w = AutosendWorker(
        draft_service=svc, send_callback=_noop_send, config={},
        work_schedule_provider=_catchup_ws,
        catchup_regenerate_cb=lambda conv, text: regen_calls.append(conv) or True)
    sent, _errors, to_deliver = w._process_batch()
    # 每批最多重拟 5 条；预算外的留 pending（不投陈稿、不取消），下 tick 继续
    assert len(regen_calls) == 5
    assert len(svc._store.cancelled) == 5
    assert sent == 0 and to_deliver == []
    assert w.total_catchup_regenerated == 5


def test_catchup_disabled_delivers_stale_as_is():
    ws = _ws_cfg(in_hours=True)
    ws["off_hours"] = {"catch_up": False}
    svc = _Svc([_draft(age_sec=4 * 3600)])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws,
                       catchup_regenerate_cb=lambda conv, text: True)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert svc._store.cancelled == []


# ── Q-4 #267：阈值 0（默认）= 过夜积压全部重拟（R79 代补接线）──────────


def _catchup_ws_default_zero():
    ws = _ws_cfg(in_hours=True)          # 本班次 now-2h 开班
    ws["off_hours"] = {"catch_up": True}  # 不写 hours → 默认 0 = regenerate_all
    return ws


def test_catchup_zero_threshold_regenerates_pre_shift_draft():
    # 稿拟于开班前（稿龄 3h > 班龄 2h）→ 过夜积压 → 作废重拟
    svc = _Svc([_draft(age_sec=3 * 3600)])
    regen_calls = []
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_catchup_ws_default_zero,
                       catchup_regenerate_cb=lambda conv, text: regen_calls.append(conv) or True)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 0 and to_deliver == []
    assert svc._store.cancelled == [("d1", "cancelled", "work_schedule_regen")]
    assert len(regen_calls) == 1
    assert w.total_catchup_regenerated == 1


def test_catchup_zero_threshold_keeps_in_shift_draft():
    # 稿拟于本班次内（稿龄 30min < 班龄 2h）→ 不是过夜稿 → 原样投递
    svc = _Svc([_draft(age_sec=1800)])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=_catchup_ws_default_zero,
                       catchup_regenerate_cb=lambda conv, text: True)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert svc._store.cancelled == []


def test_off_hours_hold_logs_once_per_conv(caplog):
    # Q-4 D-Q1：扣留必留痕 [work_schedule] hold=off_hours conv=… until=… tz=…；
    # 同会话同一「到点」只打一条（第二 tick 不重复刷屏）
    import logging
    from src.inbox import work_hours_gate as whg
    whg._hold_logged.clear()
    svc = _Svc([_draft()])
    ws = _ws_cfg(in_hours=False)
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, work_schedule_provider=lambda: ws)
    with caplog.at_level(logging.INFO, logger=whg.logger.name):
        w._process_batch()
        w._process_batch()
    hold_lines = [r.getMessage() for r in caplog.records
                  if "[work_schedule] hold=off_hours" in r.getMessage()]
    assert len(hold_lines) == 1
    assert "conv=telegram:acct1:d1" in hold_lines[0]
    assert "until=" in hold_lines[0] and "tz=UTC" in hold_lines[0]
    assert w.total_skipped_off_hours == 2


# ── 拟稿节流 + companion 双轨互斥（autodraft_helpers）────────


class _DraftSvc:
    def __init__(self):
        self.calls = []

    def auto_generate_draft(self, conv, text, *, automation_mode="review",
                            enrich=False):
        self.calls.append((conv, text, automation_mode))
        return "new-draft"


def _make_cb(app_config, mode="auto_ai"):
    import logging

    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    return make_auto_draft_cb(
        AutoDraftConfig(mode=mode, min_len=0, skip=set(),
                        platform_ceilings={}, skip_groups=False, enrich=False),
        _DraftSvc(), None, None, None, logging.getLogger("t"),
        app_config=app_config,
    )


def _conv(platform="telegram", account_id="acct1"):
    return {"conversation_id": f"{platform}:{account_id}:c1",
            "platform": platform, "account_id": account_id, "chat_key": "c1"}


def test_autodraft_generates_by_default_off_hours():
    # 休息期默认仍拟稿（generate_drafts 缺省 true）——稿由 B 线闸扣住
    import logging

    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    svc = _DraftSvc()
    app_cfg = {"inbox": {"work_schedule": _ws_cfg(in_hours=False)}}
    cb = make_auto_draft_cb(
        AutoDraftConfig(mode="review", min_len=0, skip=set(),
                        platform_ceilings={}, skip_groups=False, enrich=False),
        svc, None, None, None, logging.getLogger("t"), app_config=app_cfg)
    cb(_conv(platform="line"), "在吗？")
    assert len(svc.calls) == 1


def test_autodraft_throttled_when_generate_drafts_false():
    import logging

    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    svc = _DraftSvc()
    ws = _ws_cfg(in_hours=False)
    ws["off_hours"] = {"generate_drafts": False}
    app_cfg = {"inbox": {"work_schedule": ws}}
    cb = make_auto_draft_cb(
        AutoDraftConfig(mode="review", min_len=0, skip=set(),
                        platform_ceilings={}, skip_groups=False, enrich=False),
        svc, None, None, None, logging.getLogger("t"), app_config=app_cfg)
    cb(_conv(platform="line"), "在吗？")
    assert svc.calls == []               # 休息期不拟稿
    cb(_conv(platform="line"), "我不想活了")
    assert len(svc.calls) == 1           # 危机照拟
    # 在班照常拟稿
    ws2 = _ws_cfg(in_hours=True)
    ws2["off_hours"] = {"generate_drafts": False}
    app_cfg2 = {"inbox": {"work_schedule": ws2}}
    svc2 = _DraftSvc()
    cb2 = make_auto_draft_cb(
        AutoDraftConfig(mode="review", min_len=0, skip=set(),
                        platform_ceilings={}, skip_groups=False, enrich=False),
        svc2, None, None, None, logging.getLogger("t"), app_config=app_cfg2)
    cb2(_conv(platform="line"), "在吗？")
    assert len(svc2.calls) == 1


def _patch_companion_owns(monkeypatch):
    import src.integrations.account_orchestrator as ao
    import src.integrations.telegram_companion_worker as tcw

    class _Orch:
        def owns(self, platform, account_id):
            return True

    monkeypatch.setattr(tcw, "companion_runtime_enabled", lambda cfg: True)
    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: _Orch())


def test_companion_yield_respects_schedule(monkeypatch):
    """双轨互斥班表感知：A 线在班才让位；休息中必须接住拟稿。"""
    import logging

    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    _patch_companion_owns(monkeypatch)

    def _cb_with(ws):
        svc = _DraftSvc()
        cb = make_auto_draft_cb(
            AutoDraftConfig(mode="auto_ai", min_len=0, skip=set(),
                            platform_ceilings={}, skip_groups=False,
                            enrich=False),
            svc, None, None, None, logging.getLogger("t"),
            app_config={"inbox": {"work_schedule": ws}})
        return svc, cb

    # 在班：A 线会直发 → 让位（不拟稿，旧行为）
    svc, cb = _cb_with(_ws_cfg(in_hours=True))
    cb(_conv(), "在吗？")
    assert svc.calls == []
    # 休息：A 线被扣 → 不让位，照常拟稿（稿由 B 线闸扣到复班）
    svc, cb = _cb_with(_ws_cfg(in_hours=False))
    cb(_conv(), "在吗？")
    assert len(svc.calls) == 1
    # 休息 + 危机：A 线穿透直发 → 照旧让位防双发
    svc, cb = _cb_with(_ws_cfg(in_hours=False))
    cb(_conv(), "我不想活了")
    assert svc.calls == []
    # 班表未启用：恒让位（零行为变更）
    ws_off = _ws_cfg(in_hours=False)
    ws_off["enabled"] = False
    svc, cb = _cb_with(ws_off)
    cb(_conv(), "在吗？")
    assert svc.calls == []


def test_companion_yield_bypassed_for_catchup_regen(monkeypatch):
    """复班补觉重拟：skip_companion_yield=True 旁路让位（在班也要拟）。"""
    import logging

    from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb
    _patch_companion_owns(monkeypatch)
    svc = _DraftSvc()
    cb = make_auto_draft_cb(
        AutoDraftConfig(mode="auto_ai", min_len=0, skip=set(),
                        platform_ceilings={}, skip_groups=False, enrich=False),
        svc, None, None, None, logging.getLogger("t"),
        app_config={"inbox": {"work_schedule": _ws_cfg(in_hours=True)}})
    cb(_conv(), "在吗？", skip_companion_yield=True)
    assert len(svc.calls) == 1


# ── 协议号直发（run_autoreply）──────────────────────────────


class _Registry:
    def get(self, platform, account_id):
        return {"meta": {"auto_reply": True}}


def _proto_cfg(in_hours: bool):
    return {
        "protocol_autoreply": {"enabled": True},
        "inbox": {"work_schedule": _ws_cfg(in_hours=in_hours)},
    }


@pytest.mark.asyncio
async def test_protocol_autoreply_off_hours():
    from src.integrations.protocol_autoreply import run_autoreply

    async def _gen(**kw):
        return "好呀"

    async def _send(**kw):
        return None

    res = await run_autoreply(
        {"direction": "in", "platform": "whatsapp", "account_id": "wa1",
         "chat_key": "ws-proto-off-1", "text": "在吗？"},
        registry=_Registry(), cfg=_proto_cfg(False),
        generate=_gen, send=_send)
    assert res["reason"] == "off_hours"
    assert res["decision"] == "skipped"


@pytest.mark.asyncio
async def test_protocol_autoreply_crisis_bypass_and_in_hours():
    from src.integrations.protocol_autoreply import run_autoreply

    sent = []

    async def _gen(**kw):
        return "我在，别怕"

    async def _send(**kw):
        sent.append(kw)
        return None

    # 休息中 + 危机 → 穿透照发
    res = await run_autoreply(
        {"direction": "in", "platform": "whatsapp", "account_id": "wa1",
         "chat_key": "ws-proto-crisis-1", "text": "我真的不想活了"},
        registry=_Registry(), cfg=_proto_cfg(False),
        generate=_gen, send=_send)
    assert res["reason"] == "ok" and sent
    # 在班 → 正常发
    res2 = await run_autoreply(
        {"direction": "in", "platform": "whatsapp", "account_id": "wa1",
         "chat_key": "ws-proto-in-1", "text": "在吗？"},
        registry=_Registry(), cfg=_proto_cfg(True),
        generate=_gen, send=_send)
    assert res2["reason"] == "ok"


# ── 主动触达一致性（proactive/ritual 与班表求交）────────────


def test_proactive_account_on_duty():
    from src.companion.proactive_topic import account_on_duty
    root_off = {"inbox": {"work_schedule": _ws_cfg(in_hours=False)}}
    root_on = {"inbox": {"work_schedule": _ws_cfg(in_hours=True)}}
    assert account_on_duty(root_off, "telegram", "acct1") is False
    assert account_on_duty(root_on, "telegram", "acct1") is True
    # 未启用 / 空配置 / 异常输入 → 在班（旧行为）
    assert account_on_duty({}, "telegram", "acct1") is True
    assert account_on_duty(None, "telegram", "acct1") is True
    ws_off = _ws_cfg(in_hours=False)
    ws_off["enabled"] = False
    assert account_on_duty(
        {"inbox": {"work_schedule": ws_off}}, "telegram", "a") is True


def test_proactive_candidate_gate_wired():
    # 接线契约：_account_can_send 必须消费 account_on_duty（候选单一咽喉，
    # topic/ritual/milestone/沉默回访共用——漏接=休息账号仍主动搭讪）
    src = (Path(__file__).resolve().parents[1]
           / "src" / "companion" / "proactive_topic.py").read_text(
        encoding="utf-8")
    i_fn = src.index("def _account_can_send")
    i_next = src.index("def _conversations")
    assert "account_on_duty(" in src[i_fn:i_next]


# ── A 线静态接线（telegram_client 直发口）───────────────────


def test_telegram_a_line_gate_wired_before_llm():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "client" / "telegram_client.py").read_text(
        encoding="utf-8")
    i_auto = src.index("[automation] A线让位")
    i_ws = src.index("[work-schedule] A线让位")
    # 班表闸之后才是回复逻辑闸（进而 LLM）——reply_logic_gates 在文件更早处
    # 另有群白名单等 import，必须从闸门位置向后找本段的那次 import
    i_rl = src.index("from src.client.reply_logic_gates import", i_ws)
    assert i_auto < i_ws < i_rl
    # 用的是共享判定（三口同一口径）
    assert "should_hold_auto_reply" in src[i_auto:i_rl]
