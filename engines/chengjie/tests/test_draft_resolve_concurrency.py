"""多开/双坐席并发防线门禁（P0 2026-07-29）。

锁定四条不变量：
  1. ``update_draft_status`` 原子状态闸门——终态草稿不可被二次处置（双窗口双击、
     人工×AutosendWorker 竞态都只有一方成功）；enriching 仍可被系统作废（stale_peer）。
  2. resolve 撞闸门返回 409 + already_resolved（前端据此提示「已被其他窗口处理」；
     AutosendWorker 据此计 skip 不计 error，不喂熔断器）。
  3. 「通过≠发送」断链修复——人工 approve/edit_send 的 inbox 草稿在处置成功后经注入
     回调真投递，且**恰好一次**；autosend 动作不走该钩子（worker 自己投递，防双发）。
  4. 发送幂等键 ``send_dedup``——同 (会话,id) 窗口内至多成功一次；失败释放可重试。
另锁可见性修复：``list_drafts(platform=X)`` 必须包含该平台的 inbox 草稿
（此前按平台过滤永远看不到 → 生产 pending 积压 199h 无人可处置的根因）。
"""

from __future__ import annotations

import asyncio

import pytest

from src.inbox.autosend_worker import AutosendWorker
from src.inbox.drafts import DraftService
from src.inbox.send_dedup import SendDedup
from src.inbox.store import InboxStore


def _mk_inbox_draft(store, draft_id="inbox:t1", platform="telegram",
                    chat_key="peer1", status="pending", text="草稿正文"):
    store.upsert_draft({
        # source_id 必须逐条唯一：reply_drafts 以 (source_kind, source_id) 为冲突键，
        # 同 source_id 的第二次 upsert 是 UPDATE 而非新行（与生产 auto_generate_draft 同口径）
        "source_kind": "inbox", "source_id": draft_id.split(":", 1)[1],
        "draft_id": draft_id, "platform": platform,
        "account_id": "default", "chat_key": chat_key,
        "conversation_id": f"{platform}:default:{chat_key}",
        "draft_text": text, "status": status,
        "autopilot_level": "L3", "risk_level": "low",
    })
    return draft_id


# ── 1. store 层原子闸门 ─────────────────────────────────────────


def test_update_draft_status_only_from_active_states(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    did = _mk_inbox_draft(store)

    assert store.update_draft_status(did, status="approved", decided_by="a") is True
    # 终态后任何二次处置都被闸门拒绝
    assert store.update_draft_status(did, status="cancelled", decided_by="b") is False
    assert store.update_draft_status(did, status="rejected", decided_by="b") is False
    row = store.get_draft(did)
    assert row["status"] == "approved"
    assert row["decided_by"] == "a"

    # enriching 属活跃态：系统作废（stale_peer）仍可转换
    did2 = _mk_inbox_draft(store, draft_id="inbox:t2", status="enriching")
    assert store.update_draft_status(
        did2, status="cancelled", decided_by="stale_peer") is True
    store.close()


def test_update_draft_status_missing_row_returns_false(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert store.update_draft_status("inbox:nope", status="approved") is False
    store.close()


# ── 2. resolve 409 语义 ────────────────────────────────────────


def test_double_resolve_second_gets_409(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store)
    did = _mk_inbox_draft(store)

    r1 = svc.resolve(did, "approve", by="win-a")
    assert r1["ok"] is True

    r2 = svc.resolve(did, "approve", by="win-b")
    assert r2["ok"] is False
    assert r2["code"] == 409
    assert r2["already_resolved"] is True
    assert r2["current_status"] == "approved"

    # 不存在的草稿仍是 404（与 409 可区分）
    r3 = svc.resolve("inbox:ghost", "approve", by="x")
    assert r3["code"] == 404
    store.close()


# ── 可见性：平台过滤必须包含该平台的 inbox 草稿 ─────────────────


def test_platform_filtered_list_includes_inbox_drafts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store)
    _mk_inbox_draft(store, draft_id="inbox:tg1", platform="telegram", chat_key="p1")
    _mk_inbox_draft(store, draft_id="inbox:wa1", platform="whatsapp", chat_key="p2")

    tg = {d["draft_id"] for d in svc.list_drafts(platform="telegram")}
    assert "inbox:tg1" in tg and "inbox:wa1" not in tg
    # 旧语义保留：platform="" 全列 / platform="inbox" 全列 inbox 源
    allp = {d["draft_id"] for d in svc.list_drafts(platform="")}
    assert {"inbox:tg1", "inbox:wa1"} <= allp
    inbox_only = {d["draft_id"] for d in svc.list_drafts(platform="inbox")}
    assert {"inbox:tg1", "inbox:wa1"} <= inbox_only
    store.close()


# ── 3. 人工通过 → 真投递（恰好一次） ────────────────────────────


async def test_human_approve_schedules_delivery_exactly_once(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store)
    did = _mk_inbox_draft(store)

    delivered = []

    async def _cb(row):
        delivered.append(row)

    svc.set_inbox_deliver_callback(_cb)

    r1 = svc.resolve_with_audit(did, "approve", by="win-a")
    assert r1["ok"] is True
    assert r1["delivery"] == "scheduled"

    r2 = svc.resolve_with_audit(did, "approve", by="win-b")   # 另一窗口竞态
    assert r2["ok"] is False and r2["code"] == 409

    await asyncio.sleep(0.05)
    assert len(delivered) == 1
    assert delivered[0]["draft_id"] == did
    assert delivered[0]["chat_key"] == "peer1"
    store.close()


async def test_autosend_action_skips_human_delivery_hook(tmp_path):
    """worker 的 autosend 动作不走人工投递钩子（worker 自己投递，走钩子=双发）。"""
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store)
    did = _mk_inbox_draft(store)

    delivered = []

    async def _cb(row):
        delivered.append(row)

    svc.set_inbox_deliver_callback(_cb)
    r = svc.resolve_with_audit(did, "autosend", by="autosend_worker")
    assert r["ok"] is True
    assert "delivery" not in r
    await asyncio.sleep(0.05)
    assert delivered == []
    store.close()


async def test_autosend_with_deliver_flag_forces_delivery(tmp_path):
    """bulk-autosend 路由：显式 deliver=True 的 autosend 要投递（worker 不管这批）。"""
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store)
    did = _mk_inbox_draft(store)

    delivered = []

    async def _cb(row):
        delivered.append(row)

    svc.set_inbox_deliver_callback(_cb)
    r = svc.resolve_with_audit(did, "autosend", by="op", deliver=True)
    assert r["ok"] is True and r["delivery"] == "scheduled"
    await asyncio.sleep(0.05)
    assert len(delivered) == 1
    store.close()


def test_no_callback_or_no_loop_keeps_mark_only(tmp_path):
    """未注入回调（deliver 关部署）/ 无事件循环（同步上下文）→ 保持仅标记旧语义。"""
    store = InboxStore(tmp_path / "inbox.db")
    svc = DraftService(inbox_store=store)
    did = _mk_inbox_draft(store)
    r = svc.resolve_with_audit(did, "approve", by="a")   # 同步调用且无回调
    assert r["ok"] is True
    assert r.get("delivery") in (None, "skipped")
    store.close()


# ── worker.deliver_human_approved ──────────────────────────────


async def test_worker_deliver_human_approved_success_and_failure():
    sent = []

    async def _ok_cb(platform, account_id, chat_key, text, original_text=None):
        sent.append((platform, account_id, chat_key, text, original_text))
        return {"ok": True}

    class _Svc:
        def __init__(self):
            self.failures = []

        def record_autosend_failure(self, draft_id, *, conversation_id="", reason="",
                                    autopilot_level="L2"):
            self.failures.append((draft_id, reason))

    svc = _Svc()
    w = AutosendWorker(draft_service=svc, config={"enabled": False},
                       send_callback=_ok_cb)
    draft = {"draft_id": "inbox:x", "conversation_id": "c1",
             "platform": "telegram", "account_id": "default",
             "chat_key": "peer", "draft_text": "hello", "final_text": ""}
    res = await w.deliver_human_approved(draft)
    assert res["ok"] is True
    assert sent == [("telegram", "default", "peer", "hello", "hello")]
    assert w.total_human_delivered == 1

    async def _bad_cb(platform, account_id, chat_key, text, original_text=None):
        raise RuntimeError("boom")

    w2 = AutosendWorker(draft_service=svc, config={"enabled": False},
                        send_callback=_bad_cb)
    res2 = await w2.deliver_human_approved(draft)
    assert res2["ok"] is False
    assert w2.total_human_deliver_errors == 1
    assert svc.failures and "boom" in svc.failures[-1][1]

    # 空正文 / 无发送通道：不投递不炸
    w3 = AutosendWorker(draft_service=svc, config={"enabled": False})
    res3 = await w3.deliver_human_approved(draft)
    assert res3["ok"] is False


def test_worker_batch_409_counts_skip_not_error():
    class _Svc:
        _store = None

        def list_drafts(self, *, status="pending", limit=200):
            return [{
                "draft_id": "inbox:r1", "autopilot_level": "L2",
                "conversation_id": "c1", "platform": "telegram",
                "account_id": "default", "chat_key": "p",
                "draft_text": "hi", "final_text": "", "created_at": 0,
            }]

        def resolve_with_audit(self, draft_id, action, by=""):
            return {"ok": False, "code": 409, "already_resolved": True}

    async def _cb(platform, account_id, chat_key, text):
        return {"ok": True}

    w = AutosendWorker(draft_service=_Svc(), config={"enabled": False},
                       send_callback=_cb)
    sent, errors, to_deliver = w._process_batch()
    assert sent == 0
    assert errors == 0                 # 竞态不算故障，不喂熔断器
    assert to_deliver == []
    assert w.total_skipped_raced == 1


# ── 4. 发送幂等键 ──────────────────────────────────────────────


def test_send_dedup_reserve_duplicate_release_and_cap():
    d = SendDedup(window_sec=600, max_entries=32)
    assert d.reserve("tg:default:p1", "id-1") is True
    assert d.reserve("tg:default:p1", "id-1") is False     # 窗口内重复
    assert d.reserve("tg:default:p2", "id-1") is True      # 不同会话不互扰
    assert d.reserve("tg:default:p1", "") is True          # 无 id=旧行为恒放行
    assert d.reserve("tg:default:p1", "") is True

    # 失败释放 → 同 id 重试可再发
    d.release("tg:default:p1", "id-1")
    assert d.reserve("tg:default:p1", "id-1") is True

    snap = d.snapshot()
    assert snap["total_duplicates"] == 1
    assert snap["total_released"] == 1

    # 容量上限：淘汰最旧不炸
    small = SendDedup(window_sec=600, max_entries=16)
    for i in range(64):
        assert small.reserve("s", f"id-{i}") is True
    assert small.snapshot()["entries"] <= 16


def test_send_dedup_window_expiry(monkeypatch):
    import src.inbox.send_dedup as sd
    d = SendDedup(window_sec=10)
    t = [1000.0]
    monkeypatch.setattr(sd.time, "time", lambda: t[0])
    assert d.reserve("k", "a") is True
    assert d.reserve("k", "a") is False
    t[0] += 11.0                       # 过窗
    assert d.reserve("k", "a") is True


# ── 端到端：路由 409 detail 走 i18n 键 ─────────────────────────


def test_already_resolved_i18n_key_exists():
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en"):
        tr_map = get_translations(lang)
        assert "err.draft.already_resolved" in tr_map, f"missing key for {lang}"
