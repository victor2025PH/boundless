# -*- coding: utf-8 -*-
"""M-7 A（#236 = #166 族第五次复报）：目标「每一拍」可追溯。

skuio 机 1.0.75：看门狗 14:01 ``sent_24h=4``、卡片「已推进 2 拍」，而 BABY BEAR
会话里一条目标驱动消息都看不见；原目标 09-07 静默到期。取证结论：
- ``beat_sent`` 事件只有相位、没有会话/正文/消息 id（入队即记）；
- 卡片数 goal_actions（consumed=回复链顺势带方向），看门狗数 goal_events 且
  care 钩子每次真发写 beat_sent+care_sent 两条 → 一次计 2；
- ticker 被闸拦下只在内存 last_skips 计数，不落事件。

本文件钉：事件追溯列 / 拦下也记事件（按槽位去重）/ ``build_beats_trace`` 清单
与摘要 / 看门狗与卡片同口径 / ``/api/goals/{id}/beats`` 路由登记。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from src.companion.goals.liveness import (
    collect_send_liveness,
    goal_stall_verdict,
)
from src.companion.goals.service import build_beats_trace
from src.companion.goals.sprint_ticker import (
    SprintGoalTicker,
    record_beat_blocked,
    record_natural_beat_sent,
    record_sprint_beat_sent,
)
from src.companion.goals.store import GoalStore
from src.contacts.care_schedule import CareScheduleStore

NOW = time.time()
CONV = "whatsapp:17345893506:13308422244"


def _goal(gs: GoalStore, **kw):
    base = dict(
        conversation_id=CONV, platform="whatsapp", account_id="17345893506",
        chat_key="13308422244", template="custom", autonomy="auto",
        deadline_days=3, params={"note": "把关系推进到愿意视频"},
        now=NOW - 2 * 86400,
    )
    base.update(kw)
    return gs.create_goal(**base)


# ── 事件表追溯列 ───────────────────────────────────────────────────────────
def test_store_event_trace_columns_migrate_and_roundtrip():
    gs = GoalStore(":memory:")
    g = _goal(gs)
    gid = g["goal_id"]
    gs.add_event(gid, "beat_sent", "daily:auto care#12",
                 conversation_id=CONV, message_id="wamid.X", text_head="嗨，今天…")
    gs.add_event(gid, "note", "plain three-arg call still works")
    evs = gs.list_events(gid, limit=10)
    sent = [e for e in evs if e["kind"] == "beat_sent"][0]
    assert sent["conversation_id"] == CONV
    assert sent["message_id"] == "wamid.X"
    assert sent["text_head"] == "嗨，今天…"
    plain = [e for e in evs if e["kind"] == "note"][0]
    assert plain["conversation_id"] == "" and plain["message_id"] == ""
    # kinds 过滤 + since_ts
    only = gs.list_events(gid, kinds=("beat_sent",))
    assert [e["kind"] for e in only] == ["beat_sent"]
    assert gs.list_events(gid, kinds=("beat_sent",), since_ts=NOW + 10) == []
    # message_id 回填只填空的
    assert gs.set_event_message_id(int(plain["id"]), "m1") is True
    assert gs.set_event_message_id(int(plain["id"]), "m2") is False
    assert gs.last_event(gid, "beat_sent", detail_prefix="daily:auto") is not None
    assert gs.last_event(gid, "beat_sent", detail_prefix="sprint:") is None


def test_existing_db_without_trace_columns_gets_migrated(tmp_path):
    """存量库（旧 DDL 无三列）打开即补列，不丢既有事件。"""
    import sqlite3
    p = tmp_path / "marketing_goals.db"
    con = sqlite3.connect(str(p))
    con.executescript(
        "CREATE TABLE goal_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " goal_id TEXT NOT NULL, kind TEXT NOT NULL,"
        " detail TEXT NOT NULL DEFAULT '', ts REAL NOT NULL);"
        "INSERT INTO goal_events (goal_id, kind, detail, ts)"
        " VALUES ('g1','beat_sent','sprint:p1', 1.0);")
    con.commit()
    con.close()
    gs = GoalStore(str(p))
    evs = gs.list_events("g1")
    assert len(evs) == 1 and evs[0]["conversation_id"] == ""
    gs.add_event("g1", "beat_sent", "sprint:p2", conversation_id=CONV)
    assert gs.list_events("g1", kinds=("beat_sent",))[0]["conversation_id"] == CONV


# ── 真发回执带会话 / care 行 ───────────────────────────────────────────────
def test_record_beat_sent_carries_conversation_and_care_id():
    gs = GoalStore(":memory:")
    g = _goal(gs, params={"pace": "today", "note": "x"}, deadline_days=3 / 24.0,
              now=NOW - 600)
    gid = g["goal_id"]
    assert record_sprint_beat_sent(gs, gid, 1, now=NOW, care_id=42,
                                   text_head="要不要今晚视频？")
    ev = gs.list_events(gid, kinds=("beat_sent",))[0]
    assert ev["detail"] == "sprint:p1 care#42"
    assert ev["conversation_id"] == CONV            # 回落目标自身会话
    assert ev["text_head"] == "要不要今晚视频？"
    g2 = _goal(gs, chat_key="other", conversation_id="whatsapp:1:other")
    assert record_natural_beat_sent(gs, g2["goal_id"], "2026-09-07", now=NOW,
                                    conversation_id="whatsapp:1:other",
                                    care_id="7")
    ev2 = gs.list_events(g2["goal_id"], kinds=("beat_sent",))[0]
    assert ev2["detail"] == "daily:auto care#7"
    assert ev2["conversation_id"] == "whatsapp:1:other"


# ── 拦下也记事件，按目标×原因×槽位去重 ─────────────────────────────────────
def test_record_beat_blocked_dedups_per_slot_and_reason():
    gs = GoalStore(":memory:")
    gid = _goal(gs)["goal_id"]
    assert record_beat_blocked(gs, gid, "silence", slot="d2026-09-07", now=NOW)
    assert not record_beat_blocked(gs, gid, "silence", slot="d2026-09-07",
                                   now=NOW + 120)          # 同槽同因不重复
    assert record_beat_blocked(gs, gid, "min_gap", slot="d2026-09-07", now=NOW)
    assert record_beat_blocked(gs, gid, "silence", slot="d2026-09-08", now=NOW)
    evs = gs.list_events(gid, kinds=("beat_blocked",))
    assert sorted(e["detail"] for e in evs) == [
        "min_gap@d2026-09-07", "silence@d2026-09-07", "silence@d2026-09-08"]
    assert all(e["conversation_id"] == CONV for e in evs)


class _Inbox:
    def __init__(self, mode="auto_ai", msgs=None):
        self.mode = mode
        self.msgs = msgs or []

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_meta(self, cid):
        return {}

    def list_recent_messages(self, cid, limit=30):
        return list(self.msgs)


def _ticker(gs, cs, inbox, **sprint_cfg):
    scfg = {"enabled": True, "natural_daily": True,
            "natural_window": [0, 24]}
    scfg.update(sprint_cfg)
    cfg = {"companion": {"goals": {"enabled": True, "sprint": scfg}}}
    return SprintGoalTicker(
        care_store=cs, config_obj=SimpleNamespace(config=cfg, config_path=None),
        goals_store=gs, inbox_store_getter=(lambda: inbox))


def test_ticker_records_silence_block_for_chatty_customer():
    """BABY BEAR 画像：自然档、客户白天一直在聊 → 6h 沉默闸每 tick 拦，此前只在
    内存计数；现在到点的那一天记一条 beat_blocked(silence@dYYYY-MM-DD)，再扫不重复。"""
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    gid = _goal(gs)["goal_id"]
    inbox = _Inbox(msgs=[{"direction": "in", "content": "hi", "ts": NOW - 300}])
    t = _ticker(gs, cs, inbox)
    out = t.run_once(now=NOW)
    assert out["scheduled"] == 0 and out["skips"].get("not_due") == 1
    blocked = gs.list_events(gid, kinds=("beat_blocked",))
    assert len(blocked) == 1 and blocked[0]["detail"].startswith("silence@d")
    t.run_once(now=NOW + 120)
    assert len(gs.list_events(gid, kinds=("beat_blocked",))) == 1


def test_ticker_records_safety_gate_block_only_when_due():
    """人审档会话：到点才记 automation_mode 拦下；建目标当天（自然档不排）不记。"""
    gs, cs = GoalStore(":memory:"), CareScheduleStore(":memory:")
    gid = _goal(gs)["goal_id"]
    fresh = _goal(gs, chat_key="new", conversation_id="whatsapp:1:new", now=NOW - 60)
    t = _ticker(gs, cs, _Inbox(mode="review"))
    out = t.run_once(now=NOW)
    assert out["skips"].get("automation_mode") == 2
    assert [e["detail"].split("@")[0]
            for e in gs.list_events(gid, kinds=("beat_blocked",))] == ["automation_mode"]
    assert gs.list_events(fresh["goal_id"], kinds=("beat_blocked",)) == []


# ── 拍清单 + 摘要（卡片/端点共用）──────────────────────────────────────────
class _CareRows:
    def __init__(self, rows):
        self.rows = {int(r["id"]): r for r in rows}

    def get(self, sid):
        return self.rows.get(int(sid))


class _Outbox:
    def __init__(self, rows):
        self.rows = {int(r["id"]): r for r in rows}

    def get_by_ids(self, ids):
        return {int(i): self.rows[int(i)] for i in ids if int(i) in self.rows}


class _MsgInbox:
    def __init__(self, msgs):
        self.msgs = msgs

    def list_recent_messages(self, cid, limit=60):
        return list(self.msgs)


def test_build_beats_trace_lists_sent_injected_blocked_with_delivery_truth():
    gs = GoalStore(":memory:")
    gid = _goal(gs)["goal_id"]
    gs.add_event(gid, "created", "custom", now=NOW - 7200)
    record_natural_beat_sent(gs, gid, "2026-09-06", now=NOW - 3600,
                             conversation_id=CONV, care_id=11)
    gs.add_event(gid, "beat_injected", "draft:2026-09-07", conversation_id=CONV,
                 text_head="顺势提一下周末", now=NOW - 1800)
    record_beat_blocked(gs, gid, "silence", slot="d2026-09-07",
                        conversation_id=CONV, now=NOW - 600)
    gs.add_event(gid, "goal_no_product", "目标未绑定产品，已拦下含报价话术", now=NOW - 300)
    care = _CareRows([{"id": 11, "sent_text": "Hey, thinking of you today ☀️",
                       "status": "sent", "note": "deferred:501"}])
    outbox = _Outbox([{"id": 501, "status": "sent", "sent_at": NOW - 3500,
                       "reply_text": "Hey, thinking of you today ☀️", "error": ""}])
    inbox = _MsgInbox([
        {"direction": "in", "text": "hi", "ts": NOW - 4000, "message_id": "in1"},
        {"direction": "out", "text": "Hey, thinking of you today ☀️",
         "ts": NOW - 3490, "message_id": "out77"},
    ])
    tr = build_beats_trace(gs, gs.get_goal(gid), care_store=care,
                           outbox_store=outbox, inbox_store=inbox, now=NOW)
    kinds = [b["kind"] for b in tr["beats"]]
    assert kinds == ["sent", "injected", "blocked", "blocked"]      # 时间正序
    sent = tr["beats"][0]
    assert sent["n"] == 1 and sent["phase"] == "daily:auto" and sent["care_id"] == 11
    assert sent["status"] == "sent"                                  # 投递真相
    assert sent["text_head"].startswith("Hey, thinking")
    assert sent["message_id"] == "out77"                             # 可跳转
    assert sent["conversation_id"] == CONV
    # 回填进事件列，下次直读
    assert gs.list_events(gid, kinds=("beat_sent",))[0]["message_id"] == "out77"
    inj = tr["beats"][1]
    assert inj["status"] == "injected" and inj["text_head"] == "顺势提一下周末"
    blk = tr["beats"][2]
    assert blk["reason"] == "silence" and blk["phase"] == "d2026-09-07"
    assert tr["beats"][3]["reason"] == "goal_no_product"
    s = tr["summary"]
    assert (s["sent"], s["delivered"], s["injected"], s["blocked"]) == (1, 1, 1, 2)
    assert s["blocked_by"] == {"silence": 1, "goal_no_product": 1}
    assert s["today"]["blocked"] >= 1


def test_build_beats_trace_marks_queued_failed_and_skipped():
    gs = GoalStore(":memory:")
    gid = _goal(gs)["goal_id"]
    record_sprint_beat_sent(gs, gid, 0, now=NOW - 900, care_id=1)
    record_sprint_beat_sent(gs, gid, 1, now=NOW - 600, care_id=2)
    record_sprint_beat_sent(gs, gid, 2, now=NOW - 300, care_id=3)
    care = _CareRows([
        {"id": 1, "sent_text": "a", "status": "sent", "note": "deferred:1"},
        {"id": 2, "sent_text": "b", "status": "sent", "note": "deferred:2"},
        {"id": 3, "sent_text": "c", "status": "skipped",
         "note": "preview:draft:abc"},
    ])
    outbox = _Outbox([
        {"id": 1, "status": "pending", "sent_at": 0, "reply_text": "a", "error": ""},
        {"id": 2, "status": "failed", "sent_at": 0, "reply_text": "b",
         "error": "channel_disconnected"},
    ])
    tr = build_beats_trace(gs, gs.get_goal(gid), care_store=care,
                           outbox_store=outbox, now=NOW)
    st = [(b["n"], b["status"]) for b in tr["beats"]]
    assert st == [(1, "queued"), (2, "failed"), (3, "blocked")]
    assert tr["beats"][1]["reason"] == "channel_disconnected"
    assert tr["beats"][2]["reason"].startswith("preview:draft:")
    assert tr["summary"]["sent"] == 3 and tr["summary"]["delivered"] == 0


def test_build_beats_trace_without_side_stores_is_pure_event_read():
    gs = GoalStore(":memory:")
    gid = _goal(gs)["goal_id"]
    record_natural_beat_sent(gs, gid, "2026-09-06", now=NOW - 60, care_id=5)
    tr = build_beats_trace(gs, gs.get_goal(gid), now=NOW)
    assert tr["summary"]["sent"] == 1
    assert tr["beats"][0]["status"] == "queued" and tr["beats"][0]["care_id"] == 5
    assert build_beats_trace(gs, {}, now=NOW)["beats"] == []


# ── 看门狗与卡片同口径：只数 beat_sent，一次真发计 1 ───────────────────────
def test_liveness_counts_beat_sent_once_and_names_stalled_goal():
    gs = GoalStore(":memory:")
    gid = _goal(gs)["goal_id"]
    # 钩子形态：一次真发 = beat_sent + care_sent 两条
    gs.add_event(gid, "beat_sent", "daily:auto care#1", conversation_id=CONV)
    gs.add_event(gid, "care_sent", "每日主动拍已发出", conversation_id=CONV)
    # 大量注入/拦下事件也不把真发挤出窗口
    for i in range(80):
        gs.add_event(gid, "beat_injected", f"draft:{i}")
    snap = collect_send_liveness(gs, now=NOW)
    assert snap["sent_24h"] == 1 and snap["stalled_goals"] == []
    tr = build_beats_trace(gs, gs.get_goal(gid), now=NOW)
    assert tr["summary"]["sent"] == snap["sent_24h"] == 1

    quiet = _goal(gs, chat_key="q", conversation_id="whatsapp:1:q", title="Q")
    snap2 = collect_send_liveness(gs, now=NOW)
    assert [s["goal_id"] for s in snap2["stalled_goals"]] == [quiet["goal_id"]]
    assert snap2["stalled_goals"][0]["conversation_id"] == "whatsapp:1:q"


def test_goal_stall_verdict_single_goal_rules():
    base = {"autonomy": "auto", "status": "active", "created_at": NOW - 2 * 86400,
            "deadline_ts": NOW + 86400}
    assert goal_stall_verdict(base, sent_in_window=0, now=NOW) == "stalled"
    assert goal_stall_verdict(base, sent_in_window=1, now=NOW) is None
    assert goal_stall_verdict(dict(base, autonomy="suggest"), sent_in_window=0,
                              now=NOW) is None
    assert goal_stall_verdict(dict(base, created_at=NOW - 3600), sent_in_window=0,
                              now=NOW) is None                       # 新目标宽限
    assert goal_stall_verdict(dict(base, deadline_ts=NOW - 10), sent_in_window=0,
                              now=NOW) is None                       # 已过期不判
    assert goal_stall_verdict(dict(base, status="expired"), sent_in_window=0,
                              now=NOW) is None
    assert goal_stall_verdict(None, sent_in_window=0, now=NOW) is None


# ── 路由登记 ───────────────────────────────────────────────────────────────
def test_beats_route_registered_and_inventoried():
    import pathlib
    src = pathlib.Path("src/web/routes/goal_routes.py").read_text(encoding="utf-8")
    assert '@app.get("/api/goals/{goal_id}/beats")' in src
    inv = pathlib.Path("tests/test_admin_route_inventory.py").read_text(encoding="utf-8")
    assert "/api/goals/{goal_id}/beats\tGET" in inv
    # 卡片 N 拍口径：sprint_live.beats_used 读事件摘要，不再 len(actions)
    assert '"beats_used": int(tsum.get("sent") or 0)' in src
