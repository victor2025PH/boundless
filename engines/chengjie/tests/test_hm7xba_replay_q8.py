# -*- coding: utf-8 -*-
"""Q-8 H（#264 #263）：HM7XBA 时间线在 1.0.79 代码上整段回放——到期一拍**真发**（或明确原因）。

时间线（发版对账_v1.0.78_O3 §〇 / 本文件逐拍钉）：
- 9/7 14:45 建 3 天「客户摸底」自然档（auto）；
- 9/7 18:42 / 22:43 watchdog 两次喊 stalled 的时刻：day 0 → **not_due**，`stall_verdict` 必须 None
  （O-3 A 口径：全局 stall 按「首拍资格」起算、自然档 ≥24h 才响；不记 beat_blocked、不喊 stalled）；
- 9/8 00:11–00:43 九条入站：回复链每轮注入（当日拍 consumed），模型顺着聊零问句 → probe_missed；
- 9/8 02:10 报告时刻：窗外（10–20）→ not_due，仍 None；
- 9/8 10:05 窗口开 + 客户 6h 沉默：ticker 排入自然档主动拍（care 行 `goal:{gid}:d20260908`，source_text
  钉未填槽问法）→ 派发器 `run_once` **真发**（send_callback 收到文本）→ sent_hook 落 `beat_sent` → 当日拍
  行 `sent` → `collect_send_liveness.sent_24h=1` → 之后任何时刻都不再是 stalled。
若这一拍在 1.0.79 上发不出，本文件的断言会把「拦在哪一闸」（skips 键）打出来——这就是「明确原因」。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from types import SimpleNamespace

from src.companion.goals import service
from src.companion.goals.liveness import collect_send_liveness, stall_verdict
from src.companion.goals.service import build_block_for_chat
from src.companion.goals.sprint_ticker import (
    SprintGoalTicker,
    parse_goal_care_kind,
    parse_sprint_cfg,
    record_natural_beat_sent,
)
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.contacts.care_dispatcher import CareDispatcher
from src.contacts.care_schedule import CareScheduleStore

CONV = "whatsapp:12137839654:19096186612"
PLAT, ACCT, CK = "whatsapp", "12137839654", "19096186612"
CFG = parse_sprint_cfg({"sprint": {"enabled": True}})
GOALS_CFG = {"companion": {"goals": {"enabled": True, "db_path": ":memory:", "sprint": {"enabled": True}}}}


def _local(y, mo, d, h, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


T_CREATE = _local(2026, 9, 7, 14, 45)
WATCHDOG_MOMENTS = (_local(2026, 9, 7, 18, 42), _local(2026, 9, 7, 22, 43))
T_REPORT = _local(2026, 9, 8, 2, 10)
T_DUE = _local(2026, 9, 8, 10, 5)

INBOUND = [
    "good morning! it's early morning here", "just woke up, coffee first", "haha yes",
    "it rained here yesterday, everything is wet", "ok", "what about you", "nice", "i like that", "talk later",
]
AI_REPLIES = [
    "Early bird! Enjoy your coffee.", "Coffee is life haha.", "Right?", "Rainy days are cozy though.", "Yep.",
    "I'm good, just chilling.", "Glad you like it.", "Same here.", "Talk soon!",
]


class _Inbox:
    """会话消息 + 会话元数据（无停联 / 无 risk_hold）；ticker 与注入链共用。"""

    def __init__(self):
        self.msgs = []

    def add(self, direction, text, ts):
        self.msgs.append({"direction": direction, "text": text, "ts": ts})

    def list_recent_messages(self, conv, limit=30):
        return list(self.msgs)[-limit:]

    def get_conversation(self, conv):
        return {"last_ts": max((m["ts"] for m in self.msgs), default=0)}

    def get_conv_meta(self, conv):
        return {}

    def get_automation_mode(self, conv):
        return "auto_ai"


class _AI:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _fresh():
    reset_goal_store()
    service._inject_log_seen.clear()
    return get_goal_store(":memory:")


def _ticker(gs, care, inbox):
    return SprintGoalTicker(care_store=care, config_obj=GOALS_CFG, goals_store=gs,
                            inbox_store_getter=lambda: inbox, emotion_gate=lambda g, m: "")


def _inject(inbox, text, now):
    return build_block_for_chat(
        SimpleNamespace(config={"companion": {"goals": {"enabled": True, "db_path": ":memory:"}}}, config_path=None),
        platform=PLAT, chat_key=CK, account_id=ACCT, conversation_id=CONV,
        user_context={}, chain="draft", inbound_text=text, inbox_store=inbox, now=now)


def test_hm7xba_full_timeline_due_beat_really_sends_on_1_0_79(caplog):
    gs = _fresh()
    g = gs.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
                       template="profile_discovery", autonomy="auto", deadline_days=3,
                       params={"slots": "location,occupation,age,interests"}, now=T_CREATE) or {}
    gid = g["goal_id"]
    care = CareScheduleStore(":memory:")
    inbox = _Inbox()
    eng = {"sprint_effective": True}

    # ── ① 9/7 18:42 / 22:43：day 0 不排；watchdog 口径（O-3 A）不喊 stalled ─────────────
    for t in WATCHDOG_MOMENTS:
        out = _ticker(gs, care, inbox).run_once(now=t)
        assert out["scheduled"] == 0 and out["skips"] == {"not_due": 1}, (datetime.fromtimestamp(t), out)
        snap = collect_send_liveness(gs, now=t)
        assert snap["active_auto"] == 1 and snap["sent_24h"] == 0
        assert snap["oldest_eligible_sec"] == 0.0                      # 结构上还没到能出手的时刻
        assert stall_verdict(eng, snap, min_active=1, min_age_sec=4 * 3600) is None
        assert not snap["stalled_goals"]
    assert care.count(status="pending") == 0
    assert not gs.list_events(gid, kinds=("beat_blocked",))

    # ── ② 9/8 00:11–00:43 九条入站：回复链注入（当日拍 consumed）、模型零问句 → missed ──────
    base = _local(2026, 9, 8, 0, 11)
    hard = 0
    with caplog.at_level(logging.INFO, logger="src.companion.goals.service"):
        for i, (txt, reply) in enumerate(zip(INBOUND, AI_REPLIES)):
            t = base + i * 240
            inbox.add("in", txt, t)
            blk = _inject(inbox, txt, t)
            assert blk
            hard += 1 if "【本轮必问】" in blk else 0
            inbox.add("out", reply, t + 30)
    assert hard >= 2
    assert gs.list_events(gid, kinds=("probe_missed",))
    assert not gs.list_events(gid, kinds=("probe_asked",))
    day = "2026-09-08"
    act = gs.get_action(gid, day)
    assert act and act["status"] == "consumed" and not str(act.get("detail") or "").startswith("asked:")

    # ── ③ 9/8 02:10 报告时刻：窗外 → not_due；仍不是 stalled ─────────────────────────────
    out = _ticker(gs, care, inbox).run_once(now=T_REPORT)
    assert out["scheduled"] == 0 and out["skips"] == {"not_due": 1}, out
    snap = collect_send_liveness(gs, now=T_REPORT)
    assert stall_verdict(eng, snap, min_active=1, min_age_sec=4 * 3600) is None

    # ── ④ 9/8 10:05：窗口开 + 客户 6h 沉默 → 到期一拍排入 care ────────────────────────────
    with caplog.at_level(logging.INFO, logger="src.companion.goals.sprint_ticker"):
        out = _ticker(gs, care, inbox).run_once(now=T_DUE)
    assert out["scheduled"] == 1, ("到期一拍没排出去，拦在：", out["skips"])
    rows = care.list_pending()
    assert len(rows) == 1
    row = rows[0]
    assert parse_goal_care_kind(row["topic_norm"]) == ("daily", gid, day)
    assert "人在哪个城市" in row["source_text"] and row["chat_key"] == CK and row["platform"] == PLAT

    # ── ⑤ 派发器真发：send_callback 收到文本 → sent_hook 落 beat_sent → 当日拍 sent ──────────
    sent = []

    async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
        sent.append({"channel": channel, "account_id": account_id, "chat_name": chat_name, "reply": reply,
                     "extra": dict(extra or {})})
        return 501

    def _hook(item):
        kind, g_id, arg = parse_goal_care_kind(item.get("topic_norm"))
        assert kind == "daily" and g_id == gid
        record_natural_beat_sent(gs, g_id, str(arg), conversation_id=str(item.get("contact_key") or ""),
                                 care_id=item.get("id") or 0, now=T_DUE + 60)

    ai = _AI("Hey! Quick one — which city are you in these days? Sounds like it rains a lot there 🌧")
    disp = CareDispatcher(store=care, ai_client=ai, send_callback=_send,
                          context_provider=lambda ck: "客户昨天说那边下雨、早上刚起", default_lang="en",
                          quiet_start_hour=23, quiet_end_hour=8, sent_hook=_hook, dry_run=False,
                          send_jitter_sec=(0.0, 0.0))
    import asyncio
    n = asyncio.run(disp.run_once(now=T_DUE + 60))
    assert n == 1, ("派发器没发出去：", care.list_pending(), care.list_recent(limit=5))
    assert len(sent) == 1 and sent[0]["chat_name"] == CK and sent[0]["channel"] == PLAT
    assert "which city" in sent[0]["reply"].lower()
    assert "人在哪个城市" in ai.prompts[0]                              # 问法真进了拟稿 prompt
    assert care.count(status="sent") == 1 and care.count(status="pending") == 0
    act2 = gs.get_action(gid, day)
    # 回复链已把当日拍 consumed → record_natural_beat_sent 不覆盖行状态（既有口径），真发的真相在 beat_sent 事件
    assert act2 and act2["status"] in ("consumed", "sent")
    bs = gs.list_events(gid, kinds=("beat_sent",))
    assert len(bs) == 1 and bs[0]["detail"].startswith("daily:auto")

    # ── ⑥ 真发后 watchdog：sent_24h=1 → 任何时刻都不再 stalled（含 30h 后本会响的时刻）───────
    for t in (T_DUE + 3600, T_CREATE + 30 * 3600):
        snap = collect_send_liveness(gs, now=t)
        assert snap["sent_24h"] >= 1 or t > T_DUE + 24 * 3600
        assert stall_verdict(eng, snap, min_active=1, min_age_sec=4 * 3600) is None, datetime.fromtimestamp(t)
    # 同一天不重复排：当日拍行仍是 consumed（回复链留下的），due_daily 仍判到期，由 care 行同日
    # topic_norm 去重兜住（skips.dedup）——一天一拍成立，但靠的是第二道闸（落点表 §H 记为观察项）
    out2 = _ticker(gs, care, inbox).run_once(now=T_DUE + 3600)
    assert out2["scheduled"] == 0 and (out2["skips"].get("not_due") == 1 or out2["skips"].get("dedup") == 1), out2
    reset_goal_store()


def test_hm7xba_due_beat_blocked_reasons_are_explicit():
    """如果到期一拍发不出，原因必须是可读的闸名（不是静默 0）：停联冻结 → frozen + beat_blocked。"""
    gs = _fresh()
    g = gs.create_goal(conversation_id=CONV, platform=PLAT, account_id=ACCT, chat_key=CK,
                       template="profile_discovery", autonomy="auto", deadline_days=3,
                       params={"slots": "location"}, now=T_CREATE) or {}
    care = CareScheduleStore(":memory:")

    class _Frozen(_Inbox):
        def get_conv_meta(self, conv):
            return {"stop_contact": True}

    inbox = _Frozen()
    inbox.add("in", "ok", _local(2026, 9, 8, 2, 1))
    out = _ticker(gs, care, inbox).run_once(now=T_DUE)
    assert out["scheduled"] == 0 and out["skips"] == {"frozen": 1}, out
    ev = gs.list_events(g["goal_id"], kinds=("beat_blocked",))
    assert ev and ev[0]["detail"] == "frozen@d2026-09-08"
    # 客户 30 分钟前还在说话 → silence（交给回复链硬注入，不打断正聊着的对话）
    inbox2 = _Inbox()
    inbox2.add("in", "still here", T_DUE - 1800)
    out2 = _ticker(gs, CareScheduleStore(":memory:"), inbox2).run_once(now=T_DUE)
    assert out2["scheduled"] == 0 and out2["skips"] == {"not_due": 1}
    assert any(e["detail"].startswith("silence@") for e in gs.list_events(g["goal_id"], kinds=("beat_blocked",)))
    reset_goal_store()
