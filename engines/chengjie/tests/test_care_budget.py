"""P3：每联系人主动预算门禁——纯判定 + 派发器接线 + outreach_log 读写侧。

真发开闸前的防打扰闸：同一联系人今天已被主动摸够 / 距上次太近 → care 让路。
读写共用既有 outreach_log 账本（proactive_topic 真发已落账，care 真发也落账）。
"""
from __future__ import annotations

from datetime import datetime

from src.contacts.care_budget import (
    ContactBudgetCfg, budget_allows, local_midnight_ts, parse_contact_budget_cfg,
)
from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import CareDispatcher
from src.contacts.care_schedule import CRISIS_CARE_TOPIC, CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()  # 周三 10:00（非安静时段）


# ── 配置解析 ─────────────────────────────────────────────────────────────
def test_parse_defaults_on():
    cfg = parse_contact_budget_cfg({})
    assert cfg.enabled is True
    assert cfg.min_gap_hours == 4.0
    assert cfg.max_daily_touches == 2


def test_parse_explicit_and_bad_values():
    cfg = parse_contact_budget_cfg({"contact_budget": {
        "enabled": False, "min_gap_hours": "bad", "max_daily_touches": -3}})
    assert cfg.enabled is False
    assert cfg.min_gap_hours == 4.0     # 坏值回默认
    assert cfg.max_daily_touches == 0   # 负数夹为 0（=不启用该规则）
    cfg2 = parse_contact_budget_cfg({"contact_budget": {"min_gap_hours": 8,
                                                        "max_daily_touches": 1}})
    assert cfg2.min_gap_hours == 8.0 and cfg2.max_daily_touches == 1


def test_parse_non_dict_budget():
    assert parse_contact_budget_cfg({"contact_budget": "yes"}).enabled is True


# ── local_midnight_ts ────────────────────────────────────────────────────
def test_local_midnight():
    mid = local_midnight_ts(NOW)
    d = datetime.fromtimestamp(mid)
    assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 6, 17, 0, 0)
    assert mid <= NOW


# ── budget_allows 判定矩阵 ───────────────────────────────────────────────
def test_allows_never_touched():
    cfg = ContactBudgetCfg()
    assert budget_allows(cfg=cfg, last_touch_ts=0, touches_today=0, now=NOW)


def test_blocks_within_min_gap():
    cfg = ContactBudgetCfg(min_gap_hours=4)
    assert not budget_allows(cfg=cfg, last_touch_ts=NOW - 3600, touches_today=1, now=NOW)
    # 刚好超过间隔 → 放行（今日 1 次 + 本次 = 2 = 上限内）
    assert budget_allows(cfg=cfg, last_touch_ts=NOW - 5 * 3600, touches_today=1, now=NOW)


def test_blocks_daily_cap():
    cfg = ContactBudgetCfg(min_gap_hours=0, max_daily_touches=2)
    assert not budget_allows(cfg=cfg, last_touch_ts=NOW - 9 * 3600,
                             touches_today=2, now=NOW)
    assert budget_allows(cfg=cfg, last_touch_ts=NOW - 9 * 3600,
                         touches_today=1, now=NOW)


def test_disabled_or_zero_thresholds_allow_all():
    off = ContactBudgetCfg(enabled=False)
    assert budget_allows(cfg=off, last_touch_ts=NOW - 60, touches_today=99, now=NOW)
    loose = ContactBudgetCfg(min_gap_hours=0, max_daily_touches=0)
    assert budget_allows(cfg=loose, last_touch_ts=NOW - 60, touches_today=99, now=NOW)


# ── 派发器接线 ───────────────────────────────────────────────────────────
class _AI:
    def __init__(self, reply="你之前说的面试怎么样啦？"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(record, row_id=123):
    async def _send(channel, account_id, chat_name, reply, defer_until,
                    reason, staleness, extra):
        record.append({"channel": channel, "reason": reason, "reply": reply})
        return row_id
    return _send


def _store_with(topic="面试", contact="tg:u0"):
    s = CareScheduleStore(":memory:")
    due = NOW - 60
    s.add_commitment(
        CareCommitment(due_at=due, event_at=due, topic=topic, sentiment="neutral",
                       anchor_text="x", source_text="明天面试", confidence=0.9),
        contact_key=contact, platform="telegram", account_id="default",
        chat_key="u0", min_confidence=0.0, dedup_window_days=0.0)
    return s


async def test_budget_gate_blocks_real_send():
    s = _store_with()
    rec = []
    ai = _AI()
    d = CareDispatcher(store=s, ai_client=ai, send_callback=_sender(rec),
                       context_provider=lambda ck: "上下文",
                       budget_gate=lambda ck: False)
    n = await d.run_once(now=NOW)
    assert n == 0
    assert s.count(status="skipped") == 1
    assert s.list_recent(status="skipped")[0]["note"] == "contact_budget"
    assert ai.prompts == []          # 拦在 LLM 之前，零 token
    assert rec == []


async def test_budget_gate_ignored_in_dry_run():
    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "上下文",
                       dry_run=True, budget_gate=lambda ck: False)
    n = await d.run_once(now=NOW)
    assert n == 1                    # dry_run 不受预算拦（样本要流动）
    assert s.list_recent(status="sent")[0]["note"] == "dry_run"


async def test_budget_gate_crisis_exempt():
    s = CareScheduleStore(":memory:")
    s.add_commitment(
        CareCommitment(due_at=NOW - 60, event_at=NOW - 60, topic=CRISIS_CARE_TOPIC,
                       sentiment="negative", anchor_text="", source_text="",
                       confidence=1.0),
        contact_key="tg:u9", platform="telegram", account_id="default",
        chat_key="u9", min_confidence=0.0, dedup_window_days=0.0)
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(reply="我在，不用急着回我"),
                       send_callback=_sender(rec),
                       budget_gate=lambda ck: False)     # 预算说不行也要发
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1


async def test_budget_gate_exception_fail_open():
    def _boom(ck):
        raise RuntimeError("ledger down")

    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "上下文", budget_gate=_boom)
    n = await d.run_once(now=NOW)
    assert n == 1 and len(rec) == 1  # gate 崩了按放行


async def test_sent_hook_called_on_real_send_only():
    s = _store_with()
    rec, touched = [], []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "上下文",
                       sent_hook=lambda item: touched.append(item))
    await d.run_once(now=NOW)
    assert len(touched) == 1
    assert touched[0]["contact_key"] == "tg:u0"
    assert touched[0]["topic"] == "面试"

    # dry_run 不落账
    s2 = _store_with()
    touched2 = []
    d2 = CareDispatcher(store=s2, ai_client=_AI(), send_callback=_sender([]),
                        context_provider=lambda ck: "上下文", dry_run=True,
                        sent_hook=lambda item: touched2.append(item))
    await d2.run_once(now=NOW)
    assert touched2 == []


async def test_sent_hook_exception_does_not_break_send():
    def _boom(item):
        raise RuntimeError("ledger write failed")

    s = _store_with()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "上下文", sent_hook=_boom)
    n = await d.run_once(now=NOW)
    assert n == 1
    assert s.count(status="sent") == 1   # 发送与 mark_sent 不受 hook 异常影响


# ── outreach_log 读侧（真 InboxStore） ──────────────────────────────────
def test_count_outreach_since(tmp_path):
    from src.inbox.store import InboxStore
    st = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:a:1"
    st.record_outreach(cid, batch_id="proactive_topic:x", note="gentle_checkin",
                       ts=NOW - 3600)
    st.record_outreach(cid, batch_id="care:面试", note="care", ts=NOW - 60)
    st.record_outreach("telegram:a:2", batch_id="care:x", note="care", ts=NOW - 60)
    st.record_outreach(cid, batch_id="old", note="x", ts=NOW - 3 * 86400)
    assert st.count_outreach_since(cid, NOW - 2 * 3600) == 2
    assert st.count_outreach_since(cid, NOW - 7 * 86400) == 3
    assert st.count_outreach_since("nope", 0) == 0
    assert st.count_outreach_since("", 0) == 0
