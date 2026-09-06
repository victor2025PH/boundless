# -*- coding: utf-8 -*-
"""#170（L-3 B 修法 2）核实：归档会话收到**协议路径**新入站 → 自动浮回（archived=0）。

skuio 机被埋数 7→8 被当作「浮回不生效」的反证。本探针走真实入站落库口
``protocol_bridge.ingest_incoming``（WA/TG worker push 与进程内 sink 都经它）→
``ingest_collected_chats`` → ``InboxStore.ingest_batch`` → ``_unarchive_on_inbound``，
用真 SQLite 验证：归档后客户再开口 → 会话回默认视图；归档前的历史重放 → 保持归档。
结论若为绿，7→8 只能是「坐席把带未读的会话人工归档」（无新入站），与 J-4 A 结论一致。
"""
from __future__ import annotations

import time

from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import ingest_incoming
from src.inbox.normalizer import conv_id

_PLAT, _ACCT, _CK = "whatsapp", "17345893506", "8613800000001"
_CID = conv_id(_PLAT, _ACCT, _CK)


def _push(store: InboxStore, text: str, ts: float, msg_id: str) -> str:
    cid = ingest_incoming(
        store, platform=_PLAT, account_id=_ACCT, chat_key=_CK, name="Loki",
        text=text, ts=ts, msg_id=msg_id, direction="in",
    )
    assert cid == _CID
    return cid


def _archived(store: InboxStore) -> bool:
    return bool((store.list_conv_tags_map([_CID]).get(_CID) or {}).get("archived"))


def test_archived_conversation_revives_on_new_inbound(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    t0 = time.time() - 3600
    _push(st, "hi", t0, "m1")
    assert st.set_conv_archived(_CID, True, source="test", actor="agent")
    assert _archived(st)
    # 归档之后客户又开口（平台时间晚于 archived_at）→ 复活
    _push(st, "are you there?", time.time(), "m2")
    assert not _archived(st), "协议路径新入站没有让归档会话浮回——浮回接线断了"
    row = st.get_conversation(_CID) or {}
    assert int(row.get("unread") or 0) >= 1
    st.close()


def test_history_replay_before_archive_does_not_revive(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    t0 = time.time() - 7200
    _push(st, "old 1", t0, "h1")
    assert st.set_conv_archived(_CID, True, source="test", actor="agent")
    # 首次全量同步 / thread 重放把**归档之前**的历史灌进来 → 不能把刚归档的会话顶回来
    _push(st, "old 2", t0 + 60, "h2")
    assert _archived(st), "归档前的历史消息不该触发浮回（否则归档等于失效）"
    st.close()


def test_buried_list_reflects_revival(tmp_path):
    """浮回后 list_buried_archived 不再列出它（看门狗被埋数与横幅同源归零）。"""
    st = InboxStore(tmp_path / "inbox.db")
    _push(st, "hi", time.time() - 3600, "m1")
    st.set_conv_archived(_CID, True, source="test", actor="agent")
    assert any(r.get("conversation_id") == _CID for r in st.list_buried_archived())
    _push(st, "hello?", time.time(), "m2")
    assert not any(r.get("conversation_id") == _CID for r in st.list_buried_archived())
    st.close()
