"""P3-198：WhatsApp 设备后缀会话身份收口门禁。

事故背景（2026-07-31 实锤，198 桌面节点）：Baileys 历史同步/回执携带设备后缀 jid
（``639531765880:0@s.whatsapp.net``），旧链路把 ``num:0`` 原样当 chat_key 落库 →
同一客户裂成两个会话；旧版边车 ``toJid`` 又把后缀并进号码（``6395317658800``）→
主动触达发到不存在的 13 位号。三层收口：
  ① 边车全推送路径 normalizeUserJid（本文件静态门禁）；
  ② 服务端内桥 handler 统一 ``normalize_chat_key``（静态门禁，防陈旧边车回放）；
  ③ store 一次性合并迁移（marker=_WA_KEY_MERGE_MIG_ID，本文件 e2e）。
"""

from pathlib import Path

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.normalizer import normalize_chat_key
from src.inbox.store import InboxStore, _WA_KEY_MERGE_MIG_ID

_ENGINE_ROOT = Path(__file__).resolve().parents[1]

BARE = "whatsapp:639602326257:639531765880"
PHANTOM = "whatsapp:639602326257:639531765880:0"


# ── ① 纯函数：chat_key 归一 ─────────────────────────────────────────────

def test_normalize_strips_wa_device_suffix():
    assert normalize_chat_key("whatsapp", "639531765880:0") == "639531765880"
    assert normalize_chat_key("whatsapp", "639531765880:12") == "639531765880"


def test_normalize_leaves_canonical_and_group_keys():
    assert normalize_chat_key("whatsapp", "639531765880") == "639531765880"
    # 群 id 纯数字无冒号，不受影响
    assert normalize_chat_key("whatsapp", "120363048758123456") == "120363048758123456"
    assert normalize_chat_key("whatsapp", "") == ""


def test_normalize_is_platform_gated():
    # LINE 官方键 / Telegram 数字键即便含冒号形态也绝不动（平台闸门）
    assert normalize_chat_key("line", "line:group:abc123") == "line:group:abc123"
    assert normalize_chat_key("telegram", "123:0") == "123:0"
    assert normalize_chat_key("", "1:2") == "1:2"


# ── ③ store 合并迁移 e2e ────────────────────────────────────────────────

def _conv(cid, key, last_ts=100, last_text="hi"):
    return InboxConversation(
        conversation_id=cid, platform="whatsapp", account_id="639602326257",
        chat_key=key, display_name="客户A", language="en",
        last_text=last_text, last_ts=last_ts, unread=0,
    )


def _seed_split_brain(store: InboxStore) -> None:
    """真身 + 幻影双会话：一条消息两边镜像（撞键）+ 幻影独有一条。"""
    store.ingest_batch(
        _conv(BARE, "639531765880", last_ts=100),
        [InboxMessage(conversation_id=BARE, platform_msg_id="AAA",
                      text="shared mirror", ts=100)])
    store.ingest_batch(
        _conv(PHANTOM, "639531765880:0", last_ts=200, last_text="unique phantom"),
        [InboxMessage(conversation_id=PHANTOM, platform_msg_id="AAA",
                      text="shared mirror", ts=100),
         InboxMessage(conversation_id=PHANTOM, platform_msg_id="BBB",
                      text="unique phantom", ts=200)])
    store.set_automation_mode(PHANTOM, "auto_ai")


def _reopen_as_legacy(store: InboxStore, path) -> InboxStore:
    """抹掉 marker 后重开＝模拟「旧库第一次被新构建启动」。"""
    store._conn.execute(
        "DELETE FROM schema_migrations WHERE mig_id = ?", (_WA_KEY_MERGE_MIG_ID,))
    store._conn.commit()
    store.close()
    return InboxStore(path)


def test_merge_into_existing_bare_conv(tmp_path):
    p = tmp_path / "inbox.db"
    store = InboxStore(p)
    _seed_split_brain(store)
    store2 = _reopen_as_legacy(store, p)
    try:
        ids = [r["conversation_id"] for r in store2.list_conversations()
               if r["platform"] == "whatsapp"]
        assert PHANTOM not in ids
        assert ids.count(BARE) == 1
        # 消息合流：镜像去重（AAA 只留一条）+ 独有消息迁入且 message_id 前缀改写
        assert store2.count_messages(BARE) == 2
        assert store2.count_messages(PHANTOM) == 0
        rows = store2._conn.execute(
            "SELECT message_id FROM messages WHERE conversation_id = ?",
            (BARE,)).fetchall()
        assert all(str(r["message_id"]).startswith(BARE + ":") for r in rows)
        # 幻影更新（ts=200 > 100）→ 会话预览取新者
        conv = [r for r in store2.list_conversations()
                if r["conversation_id"] == BARE][0]
        assert float(conv["last_ts"]) == 200
        # conversation_meta 随迁：幻影上设的档位在真身可读
        assert store2.get_automation_mode_if_set(BARE) == "auto_ai"
        # FTS 同步改写：检索独有消息命中真身会话
        if getattr(store2, "_fts5_available", False):
            hits = store2.search_messages("phantom")
            assert hits and all(h["conversation_id"] == BARE for h in hits)
    finally:
        store2.close()


def test_rename_when_no_bare_conv(tmp_path):
    """只有幻影（真身从未建）→ 原行改名转正，消息保留。"""
    p = tmp_path / "inbox.db"
    store = InboxStore(p)
    store.ingest_batch(
        _conv(PHANTOM, "639531765880:0", last_ts=50, last_text="only phantom"),
        [InboxMessage(conversation_id=PHANTOM, platform_msg_id="CCC",
                      text="only phantom", ts=50)])
    store2 = _reopen_as_legacy(store, p)
    try:
        rows = {r["conversation_id"]: r for r in store2.list_conversations()}
        assert PHANTOM not in rows
        assert BARE in rows
        assert rows[BARE]["chat_key"] == "639531765880"
        assert store2.count_messages(BARE) == 1
    finally:
        store2.close()


def test_fresh_db_records_marker_and_skips(tmp_path):
    """新库初始化即记 marker（无存量可迁）；二次开库不重跑（幂等）。"""
    p = tmp_path / "inbox.db"
    store = InboxStore(p)
    row = store._conn.execute(
        "SELECT 1 FROM schema_migrations WHERE mig_id = ?",
        (_WA_KEY_MERGE_MIG_ID,)).fetchone()
    assert row is not None
    store.close()
    store2 = InboxStore(p)  # 重开不抛、不重复迁移
    store2.close()


def test_zero_key_placeholder_purged_but_with_messages_kept(tmp_path):
    """900002：chat_key='0' 空占位删除；带消息的同键会话保守保留（不删数据）。"""
    from src.inbox.store import _WA_ZERO_KEY_MIG_ID

    p = tmp_path / "inbox.db"
    store = InboxStore(p)
    # 空占位（零消息）→ 该删
    store.upsert_conversation(InboxConversation(
        conversation_id="whatsapp:639602326257:0", platform="whatsapp",
        account_id="639602326257", chat_key="0",
        display_name="0", language="unknown", last_text="", last_ts=1, unread=0))
    # 同键但有消息 → 保守保留（另一账号）
    store.ingest_batch(
        InboxConversation(
            conversation_id="whatsapp:acct2:0", platform="whatsapp",
            account_id="acct2", chat_key="0",
            display_name="0", language="unknown", last_text="x", last_ts=2, unread=0),
        [InboxMessage(conversation_id="whatsapp:acct2:0",
                      platform_msg_id="Z1", text="x", ts=2)])
    store._conn.execute(
        "DELETE FROM schema_migrations WHERE mig_id = ?", (_WA_ZERO_KEY_MIG_ID,))
    store._conn.commit()
    store.close()
    store2 = InboxStore(p)
    try:
        ids = [r["conversation_id"] for r in store2.list_conversations()]
        assert "whatsapp:639602326257:0" not in ids
        assert "whatsapp:acct2:0" in ids
    finally:
        store2.close()


def test_non_wa_colon_keys_untouched_by_migration(tmp_path):
    """LINE 官方键含冒号但平台≠whatsapp → 迁移绝不动（平台闸门在 SQL WHERE）。"""
    p = tmp_path / "inbox.db"
    store = InboxStore(p)
    store.upsert_conversation(InboxConversation(
        conversation_id="line:acct:line:group:abc", platform="line",
        account_id="acct", chat_key="line:group:abc",
        display_name="群", language="ja", last_text="x", last_ts=1, unread=0))
    store2 = _reopen_as_legacy(store, p)
    try:
        ids = [r["conversation_id"] for r in store2.list_conversations()]
        assert "line:acct:line:group:abc" in ids
    finally:
        store2.close()


# ── ①② 静态接线门禁（防回退） ───────────────────────────────────────────

def test_internal_bridge_handlers_all_normalize():
    """六个内桥 handler（ingest/chats/reaction/receipt/message-op/presence）都必须
    过 normalize_chat_key——少一个＝陈旧边车经那条路仍能裂出幻影会话。"""
    src = (_ENGINE_ROOT / "src/web/routes/unified_inbox_account_routes.py").read_text(
        encoding="utf-8")
    assert src.count("normalize_chat_key(") >= 6, (
        "内桥 handler 的 chat_key 归一接线被删/漏（应 ≥6 处）")


def test_sidecar_push_paths_all_normalize():
    """边车推送路径（消息/会话表/通讯录/回应/回执/在线态/编辑撤回）统一走
    normalizeUserJid；personalNumber 内建归一覆盖 chats/contacts 两路。"""
    js = (_ENGINE_ROOT / "services/whatsapp-baileys/server.js").read_text(
        encoding="utf-8", errors="ignore")
    assert js.count("normalizeUserJid(jid).split") >= 4, (
        "reaction/receipt/presence/message-op 的设备后缀归一被删")
    # personalNumber 必须先归一再取号（chats/contacts 同步共用）
    import re
    m = re.search(r"function personalNumber\(jid\) \{([\s\S]*?)\n\}", js)
    assert m and "normalizeUserJid" in m.group(1), (
        "personalNumber 不再归一设备后缀（chats/contacts 同步会裂会话）")
    # '0@s.whatsapp.net'（WhatsApp 系统伪 jid）必须被当非客户过滤——
    # 否则每次 chats 同步/系统通知都会再造 chat_key='0' 幽灵会话
    # （生产实录：zhiliao 三个账号各一条，最新 2026-07-20 仍在新增）
    assert 'num === "0"' in m.group(1), "personalNumber 丢失系统伪 jid 过滤"
    assert js.count('=== "0"') >= 2, "pushWaMessage 消息路径丢失系统伪 jid 过滤"
    # toJid 出站防线（上一轮热修）仍在
    assert "s.split(\":\")[0]" in js or 's.split(":")[0]' in js


def test_server_chats_handler_drops_system_pseudo_jid():
    """服务端 chats handler 必须丢弃 jid='0' 的私聊行（陈旧边车防御）。"""
    src = (_ENGINE_ROOT / "src/web/routes/unified_inbox_account_routes.py").read_text(
        encoding="utf-8")
    assert '_r["jid"] == "0"' in src, "chats handler 丢失系统伪 jid 行过滤"
