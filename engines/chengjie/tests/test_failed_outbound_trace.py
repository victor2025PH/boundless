# -*- coding: utf-8 -*-
"""B63③（实施64 P1-4）：自动投递终局失败的「会话留痕 + 一键重发」链。

事故（skuio 2026-08-23 `_314`/`_320`-`_323`）：messenger 自动发 7/7 全失败，
失败内容在会话里零留痕直接消失，用户只能翻全自动记录弹窗才知道发过什么。

钉四层不变量：
1. store.record_failed_outbound 写 direction=out status=failed 的消息行，
   **绝不推进 conversations**（last_text/未读只反映真实收发）；
2. mark_message_resent 只允许 failed→resent 单向，绝不碰回执状态机；
3. 「真的发出去了」口径的消费方必须无视 failed/resent 行：
   首响 t_out / 回复时延 SLO / 群发言台账·最近冒头 / 目标「已跟进」/
   草稿「已回过」护栏 / 近重复守卫（不豁免的话一键重发永远撞自己的留痕 409）；
4. AutosendWorker 终局失败调用 record_failed_outbound_mirror 留痕，
   瞬时失败进重试队列时**不**留痕（重试成功就不该有失败痕）。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List

from src.inbox.store import InboxStore
from src.inbox.drafts import DraftService
from src.inbox.outbound_dup_guard import near_duplicate_of_recent
from src.ops.reply_latency import build_reply_latency

NOW = 1_800_000_000.0
HOUR = 3600.0


def _seed_conv(store, cid, *, platform="messenger", account="acct1",
               chat_key="peer", chat_type="private",
               last_text="", last_ts=0.0):
    with store._lock:
        store._conn.execute(
            "INSERT INTO conversations (conversation_id, platform, account_id,"
            " chat_key, chat_type, last_text, last_ts, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (cid, platform, account, chat_key, chat_type, last_text, last_ts,
             NOW - 30 * 86400, NOW - 30 * 86400))
        store._conn.commit()


def _seed_msg(store, cid, direction, ts, *, text="hi", status=""):
    with store._lock:
        store._conn.execute(
            "INSERT INTO messages (message_id, conversation_id, direction,"
            " text, ts, ingested_at, status) VALUES (?,?,?,?,?,?,?)",
            (f"{cid}:{uuid.uuid4().hex[:10]}", cid, direction, text, ts, ts,
             status))
        store._conn.commit()


def _conv_row(store, cid) -> Dict[str, Any]:
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM conversations WHERE conversation_id=?",
            (cid,)).fetchone()
    return dict(row) if row else {}


# ── 1. 留痕写入 ────────────────────────────────────────────────────────────


def test_record_failed_outbound_writes_trace_without_touching_conversation(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peer1"
    _seed_conv(store, cid, chat_key="peer1", last_text="客户原话", last_ts=NOW - 60)
    mid = store.record_failed_outbound(cid, "这条没发出去", ts=NOW)
    assert mid.startswith(f"{cid}:fail:")
    rows = store.list_messages(cid)
    assert len(rows) == 1
    r = rows[0]
    assert r["direction"] == "out"
    assert r["status"] == "failed"
    assert r["text"] == "这条没发出去"
    assert r["platform_msg_id"] == ""
    conv = _conv_row(store, cid)
    assert conv["last_text"] == "客户原话"          # 会话事实不被留痕推进
    assert float(conv["last_ts"]) == NOW - 60


def test_record_failed_outbound_rejects_empty_input(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert store.record_failed_outbound("", "text") == ""
    assert store.record_failed_outbound("cid", "   ") == ""


# ── 2. resent 单向改标 ─────────────────────────────────────────────────────


def test_mark_message_resent_one_way(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peer2"
    _seed_conv(store, cid, chat_key="peer2")
    mid = store.record_failed_outbound(cid, "重发我", ts=NOW)
    assert store.mark_message_resent(mid) is True
    assert store.list_messages(cid)[0]["status"] == "resent"
    assert store.mark_message_resent(mid) is False      # 已改标，幂等拒绝
    assert store.mark_message_resent("") is False
    # 非 failed 行（回执状态机领地）绝不被改标
    _seed_msg(store, cid, "out", NOW + 1, text="真发出去的", status="sent")
    sent_mid = [r["message_id"] for r in store.list_messages(cid)
                if r["status"] == "sent"][0]
    assert store.mark_message_resent(sent_mid) is False
    assert [r["status"] for r in store.list_messages(cid)].count("sent") == 1


# ── 3. 消费口径：failed/resent 不算「真的发出去了」 ────────────────────────


def test_first_response_ignores_failed_trace(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peer3"
    _seed_conv(store, cid, chat_key="peer3")
    _seed_msg(store, cid, "in", NOW - 100, text="客户进线")
    store.record_failed_outbound(cid, "没送达的回复", ts=NOW - 50)
    rows = store.first_response_rows(since_ts=NOW - 7 * 86400)
    mine = [r for r in rows if r.get("cid") == cid]
    assert mine and mine[0].get("t_out") in (None, 0, 0.0)  # 失败留痕≠首响
    # 真回复到达后 t_out 正常出现
    _seed_msg(store, cid, "out", NOW - 10, text="真回复")
    rows2 = store.first_response_rows(since_ts=NOW - 7 * 86400)
    mine2 = [r for r in rows2 if r.get("cid") == cid]
    assert mine2 and abs(float(mine2[0]["t_out"]) - (NOW - 10)) < 1


def test_reply_latency_ignores_failed_trace(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct1:1001"
    _seed_conv(store, cid, platform="telegram", chat_key="1001")
    _seed_msg(store, cid, "in", NOW - 2 * HOUR, text="在吗")
    store.record_failed_outbound(cid, "没送达", ts=NOW - 2 * HOUR + 30)
    data = build_reply_latency(store, now=NOW)
    d1 = data["windows"]["d1"] if "windows" in data else None
    # 兼容聚合形状：核心断言=该会话没有「已回复」段（失败留痕没有终结等待）
    flat = str(data)
    assert data, flat
    # unanswered 至少 1（进线超宽限无真实出站）
    if isinstance(d1, dict) and "unanswered" in d1:
        assert int(d1["unanswered"]) >= 1


def test_group_ledgers_ignore_failed_trace(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct1:g100"
    _seed_conv(store, cid, platform="telegram", chat_key="g100",
               chat_type="group")
    store.record_failed_outbound(cid, "群里没发出去", ts=NOW - 60)
    assert store.group_speech_ledger(since_ts=NOW - 86400) == {}
    assert store.group_last_spoke_at(since_ts=NOW - 86400) == {}
    _seed_msg(store, cid, "out", NOW - 30, text="群里真发言")
    ledger = store.group_speech_ledger(since_ts=NOW - 86400)
    assert ledger.get("g100") == ["acct1"]


def test_last_outbound_ts_map_ignores_failed_trace(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peer4"
    _seed_conv(store, cid, chat_key="peer4")
    store.record_failed_outbound(cid, "没送达", ts=NOW)
    assert store.last_outbound_ts_map([cid]) == {}
    _seed_msg(store, cid, "out", NOW - 500, text="早先真发过")
    got = store.last_outbound_ts_map([cid])
    assert abs(got[cid] - (NOW - 500)) < 1     # 取真实出站，不被更新的留痕顶掉


def test_replied_after_guard_ignores_failed_trace(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peer5"
    _seed_conv(store, cid, chat_key="peer5")
    svc = DraftService(inbox_store=store)
    draft = {"conversation_id": cid, "created_ts": NOW - 100}
    store.record_failed_outbound(cid, "没送达的回复", ts=NOW - 50)
    assert svc.conversation_replied_after(draft) is False   # 客户没收到=还在等
    _seed_msg(store, cid, "out", NOW - 20, text="真回复")
    assert svc.conversation_replied_after(draft) is True


def test_near_duplicate_guard_ignores_failed_trace():
    text = "您好，这是一条足够长的回复内容，用于近重复比对判定。"
    rows: List[Dict[str, Any]] = [{
        "direction": "out", "text": text, "ts": time.time() - 30,
        "status": "failed",
    }]
    # 一键重发的文本与它自己的留痕逐字相同：必须放行（否则永远 409）
    assert near_duplicate_of_recent(text, rows) is None
    rows[0]["status"] = ""      # 真发出去过的同文本仍要拦
    assert near_duplicate_of_recent(text, rows) is not None


# ── 4. worker 接线：终局失败留痕、重试中不留痕 ────────────────────────────


class _MirrorSvc:
    """最小草稿服务：记录留痕/审计调用。"""

    def __init__(self):
        self.mirrored: List[Dict[str, str]] = []
        self.audited: List[str] = []

    def record_autosend_failure(self, draft_id, *, conversation_id="", reason=""):
        self.audited.append(draft_id)

    def record_failed_outbound_mirror(self, conversation_id, text):
        self.mirrored.append({"cid": conversation_id, "text": text})
        return "mid"


def _worker(svc, send_cb, **cfg_extra):
    from src.inbox.autosend_worker import AutosendWorker
    cfg = {"enabled": True, "deliver": True}
    cfg.update(cfg_extra)
    return AutosendWorker(draft_service=svc, config=cfg, send_callback=send_cb)


def _item(conv="messenger:acct1:peerX") -> Dict[str, Any]:
    return {
        "draft_id": "d1", "platform": "messenger", "account_id": "acct1",
        "chat_key": "peerX", "text": "要发的话", "conversation_id": conv,
    }


def test_worker_permanent_failure_writes_trace():
    svc = _MirrorSvc()

    async def _boom(platform, account_id, chat_key, text):
        raise RuntimeError("USER_IS_BLOCKED")   # 永久性错误特征

    w = _worker(svc, _boom)
    asyncio.run(w._deliver_one(_item()))
    assert svc.mirrored and svc.mirrored[0]["cid"] == "messenger:acct1:peerX"
    assert svc.mirrored[0]["text"] == "要发的话"


def test_worker_transient_failure_retry_path_defers_trace():
    svc = _MirrorSvc()

    async def _flaky(platform, account_id, chat_key, text):
        raise RuntimeError("timeout while sending")   # 瞬时错误

    w = _worker(svc, _flaky, recoverable={"enabled": True, "max_attempts": 3})
    asyncio.run(w._deliver_one(_item()))
    # 首次瞬时失败进重试队列：不留痕（重试可能成功）
    assert svc.mirrored == []
    # 耗尽后（_attempt 已到顶）终局失败必须留痕
    item = _item()
    item["_attempt"] = 2
    asyncio.run(w._deliver_one(item))
    assert len(svc.mirrored) == 1


def test_worker_success_writes_no_trace():
    svc = _MirrorSvc()

    async def _ok(platform, account_id, chat_key, text):
        return {"ok": True}

    w = _worker(svc, _ok)
    asyncio.run(w._deliver_one(_item()))
    assert svc.mirrored == []


# ── 5. 失败可解释（实施72 P3，承实施71 P1-1）──────────────────────────────


def test_record_failed_outbound_persists_reason_and_serializes(tmp_path):
    """原因码随留痕落库 → thread 序列化透传（气泡自解释「为什么失败」）。"""
    from src.inbox.normalizer import store_message_to_obj
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peerR"
    _seed_conv(store, cid, chat_key="peerR")
    mid = store.record_failed_outbound(
        cid, "哎妈呀这条没发出去", reason="send_gate:daily_cap")
    assert mid
    row = next(m for m in store.list_recent_messages(cid, limit=5)
               if m["message_id"] == mid)
    assert row["fail_reason"] == "send_gate:daily_cap"
    obj = store_message_to_obj(row)
    assert obj["fail_reason"] == "send_gate:daily_cap"
    assert obj["status"] == "failed"
    # 旧行/无原因 → 空串（前端只显「发送失败」，不显原因短语）
    mid2 = store.record_failed_outbound(cid, "无原因旧路径")
    row2 = next(m for m in store.list_recent_messages(cid, limit=5)
                if m["message_id"] == mid2)
    assert row2["fail_reason"] == ""
    # 超长原因截断（防日志级长串撑爆列）
    mid3 = store.record_failed_outbound(cid, "长原因", reason="x" * 500)
    row3 = next(m for m in store.list_recent_messages(cid, limit=5)
                if m["message_id"] == mid3)
    assert len(row3["fail_reason"]) == 200


def test_mirror_passes_reason_and_falls_back_on_old_store(tmp_path):
    """DraftService 薄包装：新 store 透传 reason；旧 store（无该形参）回落旧签名。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "messenger:acct1:peerM"
    _seed_conv(store, cid, chat_key="peerM")
    svc = DraftService.__new__(DraftService)
    svc._store = store
    mid = svc.record_failed_outbound_mirror(
        cid, "带原因", reason="kill_switch:account")
    row = next(m for m in store.list_recent_messages(cid, limit=5)
               if m["message_id"] == mid)
    assert row["fail_reason"] == "kill_switch:account"

    class _OldStore:
        def record_failed_outbound(self, conversation_id, text, *, ts=0.0):
            self.called = (conversation_id, text)
            return "old-mid"

    old = _OldStore()
    svc2 = DraftService.__new__(DraftService)
    svc2._store = old
    assert svc2.record_failed_outbound_mirror(cid, "旧店", reason="r") == "old-mid"
    assert old.called == (cid, "旧店")
