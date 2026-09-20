"""M-1 C（#219，2026-09-06）：己方在手机端删掉的消息 → 工作台灰显「已撤回」+ AI 上下文/记忆剔除。

实录：steven 号 14:03 发出的关怀 mid=88808 被用户在 Telegram 原生端删除，工作台重载
仍显 ✓，后端 14:03–14:16 零删除事件。两处根因：
① pyrogram 同 group 内只跑第一个命中的 handler——删除同步 RawUpdateHandler 与已读回执
   handler 同在 group 0，从未被调用过；
② ``report_deleted_messages`` 不分方向一律按「对端删除」软删（前台消失），没有「己方撤回」语义。

覆盖：
- store：``revoke_own_by_platform_msg_ids`` 只标出站行 revoked=1（入站不动 / 本账号 /
  chat_key 收窄 / 幂等）；``list_recent_messages`` AI 口径剔除 revoked，UI 口径仍给（灰显）；
- bridge：裸 id 命中出站行 → 撤回、入站行 → 软删；SSE 带 own_revoked；purger 收到带方向的行；
- purge：A 线 assistant 历史条换占位、last_reply/recent_replies/_human_said_log 剔除；己方账本
  ``own_quotes_for``；``build_withdrawn_hint(own_quotes=)`` / ``redact_history`` / ``apply_to_reply``
  三个注入/出站口都认己方撤回；
- 轮询对账 ``find_deleted_by_top``：本地更新且够老的行 → 判已删；刚发的 / 失败留痕 / 非数字 id 不算；
- 静态钉：删除 handler 注册在独立 group；删除更新落 INFO；对账挂在轮询循环。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore

_ROOT = Path(__file__).resolve().parents[1]
CID = "telegram:7092595256:6088992099"
CARE = "How are you finding life in the Philippines? Is the food there as good as what I cook?"


def _seed(store: InboxStore, cid: str, pmid: str, text: str, ts: float, *,
          direction="in", platform="telegram", account="7092595256", chat_key=""):
    conv = InboxConversation(
        conversation_id=cid, platform=platform, account_id=account,
        chat_key=chat_key or cid.rsplit(":", 1)[-1], display_name="Kxhm")
    store.ingest_batch(conv, [InboxMessage(
        conversation_id=cid, platform_msg_id=pmid, text=text, ts=ts, direction=direction)])


def _store(tmp_path: Path) -> InboxStore:
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, CID, "88800", "kim? are you at cebu now?", 1.0, direction="in")
    _seed(store, CID, "88808", CARE, 2.0, direction="out")
    _seed(store, CID, "88810", "客户后来说的", 3.0, direction="in")
    return store


# ── ① store ───────────────────────────────────────────────────────────────────
def test_revoke_own_marks_only_outbound_rows(tmp_path: Path):
    store = _store(tmp_path)
    assert store.revoke_own_by_platform_msg_ids("telegram", "7092595256", ["88808", "88810"]) == 1
    raw = {m["platform_msg_id"]: m for m in store.list_messages(CID, limit=10)}
    assert int(raw["88808"]["revoked"]) == 1
    assert int(raw["88810"]["revoked"]) == 0          # 入站行不动（那是对端删除的语义）
    assert float(raw["88808"]["deleted_at"] or 0) == 0  # 不软删：UI 要灰显不是消失
    # 幂等 / 本账号限定 / chat_key 收窄
    assert store.revoke_own_by_platform_msg_ids("telegram", "7092595256", ["88808"]) == 0
    _seed(store, "telegram:other:1", "88808", "别的账号", 1.0, direction="out", account="other")
    assert store.revoke_own_by_platform_msg_ids("telegram", "7092595256", ["88808"]) == 0
    assert store.revoke_own_by_platform_msg_ids("telegram", "other", ["88808"], chat_key="2") == 0
    assert store.revoke_own_by_platform_msg_ids("telegram", "other", ["88808"], chat_key="1") == 1
    assert store.revoke_own_by_platform_msg_ids("telegram", "other", []) == 0


def test_revoked_rows_hidden_from_ai_history_but_shown_to_ui(tmp_path: Path):
    store = _store(tmp_path)
    store.revoke_own_by_platform_msg_ids("telegram", "7092595256", ["88808"])
    ai = [m["text"] for m in store.list_recent_messages(CID, limit=10)]
    assert ai == ["kim? are you at cebu now?", "客户后来说的"]        # AI 口径：撤回的不喂
    ai2 = [m["text"] for m in store.list_recent_messages(CID, limit=10, before_ts=3.0)]
    assert ai2 == ["kim? are you at cebu now?"]
    ui = store.list_recent_messages(CID, limit=10, include_deleted=False)
    assert [m["text"] for m in ui] == ["kim? are you at cebu now?", CARE, "客户后来说的"]
    assert int([m for m in ui if m["text"] == CARE][0]["revoked"]) == 1   # 前端据此灰显
    # worker 上报的撤回（mark_message_revoked）同列同口径
    store.mark_message_revoked(CID, "88810")
    assert [m["text"] for m in store.list_recent_messages(CID, limit=10)] == ["kim? are you at cebu now?"]


# ── ② bridge ──────────────────────────────────────────────────────────────────
def test_bridge_splits_own_revoke_and_peer_delete(tmp_path: Path, monkeypatch):
    import src.integrations.protocol_bridge as pb
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    store = _store(tmp_path)
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    got = {}

    def _purger(rows):
        got["rows"] = list(rows)
        return {"memory": 2, "context": 1}

    monkeypatch.setattr(pb, "_deleted_memory_purger", _purger)
    n = pb.report_deleted_messages("telegram", "7092595256", ["88808", "88810", "99999"])
    assert n == 2
    raw = {m["platform_msg_id"]: m for m in store.list_messages(CID, limit=10)}
    assert int(raw["88808"]["revoked"]) == 1 and float(raw["88808"]["deleted_at"] or 0) == 0
    assert float(raw["88810"]["deleted_at"] or 0) > 0 and raw["88810"]["deleted_by"] == "peer"
    dirs = {r["platform_msg_id"]: r["direction"] for r in got["rows"]}
    assert dirs == {"88808": "out", "88810": "in"}
    from src.integrations.shared.event_bus import get_event_bus
    ev = [e for e in get_event_bus().recent_events(50) if e["type"] == "messages_deleted"][-1]
    assert ev["data"]["op"] == "delete_sync"
    assert ev["data"]["own_revoked"] == 1 and ev["data"]["peer_deleted"] == 1
    assert ev["data"]["memory_purged"] == 2


def test_bridge_own_only_event_op(tmp_path: Path, monkeypatch):
    import src.integrations.protocol_bridge as pb
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    store = _store(tmp_path)
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    monkeypatch.setattr(pb, "_deleted_memory_purger", None)
    assert pb.report_deleted_messages("telegram", "7092595256", ["88808"]) == 1
    from src.integrations.shared.event_bus import get_event_bus
    ev = [e for e in get_event_bus().recent_events(50) if e["type"] == "messages_deleted"][-1]
    assert ev["data"]["op"] == "own_revoke" and ev["data"]["count"] == 1
    # 全无命中 → 0，不发事件
    before = len(get_event_bus().recent_events(50))
    assert pb.report_deleted_messages("telegram", "7092595256", ["123456"]) == 0
    assert len(get_event_bus().recent_events(50)) == before


# ── ③ 记忆 / 上下文 ────────────────────────────────────────────────────────────
class _Ctx:
    def __init__(self, data):
        self.data = data
        self.flushed = []

    def peek(self, key):
        return self.data.get(key)

    def mark_dirty(self, key):
        pass

    def flush(self, key):
        self.flushed.append(key)


class _SM:
    def __init__(self, ctx):
        self._context_store = ctx
        self._episodic_store = None


def test_purge_own_row_redacts_assistant_context_and_memory_sources():
    from src.inbox.peer_delete_purge import purge_for_deleted_rows
    from src.inbox.withdrawn_cite import (
        OWN_PLACEHOLDER, own_quotes_for, quotes_for, reset_ledger,
    )
    from src.utils.context_store import make_context_key
    reset_ledger()
    key = make_context_key("6088992099", "7092595256")
    ctx = _Ctx({key: {
        "_conversation_history": [
            {"role": "user", "content": "kim? are you at cebu now?"},
            {"role": "assistant", "content": CARE},
            {"role": "user", "content": "客户后来说的"},
        ],
        "last_reply": CARE,
        "recent_replies": [{"text": CARE, "ts": 2.0}, {"text": "别的", "ts": 1.0}],
        "_human_said_log": [{"text": CARE, "author": "human"}, {"text": "我有个女儿", "author": "human"}],
        "last_message": "客户后来说的",
    }})
    rows = [{"conversation_id": CID, "platform_msg_id": "88808", "direction": "out",
             "text": CARE, "ts": 2.0}]
    out = purge_for_deleted_rows(_SM(ctx), rows)
    assert out == {"memory": 1, "context": 1}
    c = ctx.data[key]
    assert c["_conversation_history"][1]["content"] == OWN_PLACEHOLDER
    assert c["_conversation_history"][1]["_withdrawn_own"] is True
    assert c["_conversation_history"][0]["content"] == "kim? are you at cebu now?"  # user 条不动
    assert c["last_reply"] == OWN_PLACEHOLDER
    assert [x["text"] for x in c["recent_replies"]] == ["别的"]
    assert [x["text"] for x in c["_human_said_log"]] == ["我有个女儿"]
    assert c["last_message"] == "客户后来说的"
    assert ctx.flushed == [key]
    assert own_quotes_for(CID) == [CARE] and quotes_for(CID) == []   # 两本账本分开
    reset_ledger()


def test_withdrawn_cite_own_hint_redact_and_outbound_sanitize():
    from src.inbox.withdrawn_cite import (
        OWN_PLACEHOLDER, PLACEHOLDER, apply_to_reply, build_withdrawn_hint,
        record_withdrawn, redact_history, reset_ledger,
    )
    reset_ledger()
    assert record_withdrawn(CID, CARE, own=True) is True
    assert record_withdrawn(CID, CARE, own=True) is False          # 幂等
    assert record_withdrawn(CID, "对方删的那句话", own=False) is True
    hint = build_withdrawn_hint(["对方删的那句话"], inbound="hi", own_quotes=[CARE])
    assert "【已撤回·不得主动引用】" in hint and "【你已撤回·不得再提】" in hint
    assert "Philippines" in hint
    # 只有己方撤回也出提示
    assert "你已撤回" in build_withdrawn_hint([], own_quotes=[CARE])
    hist = redact_history(
        [{"role": "user", "content": "对方删的那句话"}, {"role": "assistant", "content": CARE},
         {"role": "assistant", "content": "别的回复"}],
        ["对方删的那句话"], [CARE])
    assert hist[0]["content"] == PLACEHOLDER and hist[1]["content"] == OWN_PLACEHOLDER
    assert hist[2]["content"] == "别的回复"
    # 出站再复述己方删掉的话 → 剥离
    cleaned, hits = apply_to_reply(
        "Anyway, how are you finding life in the Philippines? Have a good night!", CID)
    assert hits and "Philippines" not in cleaned and "good night" in cleaned
    reset_ledger()


def test_inbound_enrich_consumes_own_quotes():
    src = (_ROOT / "src" / "inbox" / "inbound_enrich.py").read_text(encoding="utf-8")
    assert "own_quotes_for(_cid)" in src
    assert "build_withdrawn_hint(_qs, inbound=t, own_quotes=_oqs)" in src
    assert "redact_history(history, _qs, _oqs)" in src


# ── ④ 轮询对账兜底 ────────────────────────────────────────────────────────────
def test_find_deleted_by_top_rules():
    from src.client.telegram_client import find_deleted_by_top
    now = 10_000.0
    rows = [
        {"platform_msg_id": "88800", "direction": "in", "ts": now - 900},
        {"platform_msg_id": "88808", "direction": "out", "ts": now - 600},   # 比 top 新且够老 → 已删
        {"platform_msg_id": "88809", "direction": "out", "ts": now - 30},    # 刚发的 → 竞态窗，不算
        {"platform_msg_id": "88811", "direction": "out", "ts": now - 600, "status": "failed"},
        {"platform_msg_id": "h:abcd", "direction": "out", "ts": now - 600},  # 兜底键不算
        {"platform_msg_id": "88812", "direction": "out", "ts": now - 600, "revoked": 1},
        {"platform_msg_id": "88813", "direction": "in", "ts": now - 600},    # 对端行也算（服务端没了）
    ]
    assert find_deleted_by_top(rows, 88805, now=now) == ["88808", "88813"]
    assert find_deleted_by_top(rows, 88813, now=now) == []
    assert find_deleted_by_top(rows, 0, now=now) == [] and find_deleted_by_top(rows, "x", now=now) == []


def test_reconcile_by_top_marks_revoked_via_bridge(tmp_path: Path, monkeypatch):
    """服务端 top=88800（客户那条），本地却还有 88808 出站行（够老）→ 标撤回；同 top 二次调用不重复。"""
    import time as _t
    import src.integrations.protocol_bridge as pb
    from src.client.telegram_client import TelegramClient
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "_bus", None, raising=False)
    store = InboxStore(tmp_path / "inbox.db")
    now = _t.time()
    _seed(store, CID, "88800", "kim? are you at cebu now?", now - 900, direction="in")
    _seed(store, CID, "88808", CARE, now - 600, direction="out")
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    monkeypatch.setattr(pb, "_deleted_memory_purger", None)
    tc = TelegramClient.__new__(TelegramClient)   # LoggerMixin.logger 是只读属性，按类名取
    tc._mirror_inbox = True
    tc.account_id = "7092595256"
    assert tc._reconcile_deleted_by_top(6088992099, 88800) == 1
    raw = {m["platform_msg_id"]: m for m in store.list_messages(CID, limit=10)}
    assert int(raw["88808"]["revoked"]) == 1
    assert tc._reconcile_deleted_by_top(6088992099, 88800) == 0     # top 未变不重扫
    tc2 = TelegramClient.__new__(TelegramClient)
    tc2._mirror_inbox = False
    tc2.account_id = "7092595256"
    assert tc2._reconcile_deleted_by_top(6088992099, 1) == 0        # 镜像关 → 不动


# ── ⑤ 静态钉 ────────────────────────────────────────────────────────────────
def test_delete_handler_registered_in_own_group_and_logs():
    src = (_ROOT / "src" / "client" / "telegram_client.py").read_text(encoding="utf-8")
    assert re.search(r"add_handler\(_RUH\(_on_deleted\),\s*group=_DELETE_SYNC_HANDLER_GROUP\)", src)
    m = re.search(r"^_DELETE_SYNC_HANDLER_GROUP\s*=\s*(-?\d+)", src, flags=re.M)
    assert m and int(m.group(1)) != 0, "删除同步 handler 不得与已读回执同在 group 0（同组只跑第一个）"
    assert "add_handler(RawUpdateHandler(_on_read_receipt))" in src   # 已读回执仍在默认组
    assert "收到删除更新 ids=" in src
    assert "self._reconcile_deleted_by_top(" in src


def test_message_op_revoke_route_calls_purger():
    """WhatsApp / Messenger 边车上报的撤回（含己方删自己的消息）→ 同一记忆清理钩子。"""
    src = (_ROOT / "src" / "web" / "routes" / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    seg = src[src.index("async def api_protocol_message_op"):src.index("async def api_protocol_presence")]
    assert "select_live_by_platform_msg_ids(" in seg
    assert "get_deleted_memory_purger" in seg
    assert seg.index("select_live_by_platform_msg_ids(") < seg.index("store.mark_message_revoked(")


def test_bridge_never_soft_deletes_outbound_rows_as_peer():
    src = (_ROOT / "src" / "integrations" / "protocol_bridge.py").read_text(encoding="utf-8")
    seg = src[src.index("def report_deleted_messages"):src.index("def tg_peer_to_chat_key")]
    assert "revoke_own_by_platform_msg_ids" in seg
    assert "peer_ids = [i for i in ids if i not in set(own_ids)]" in seg
