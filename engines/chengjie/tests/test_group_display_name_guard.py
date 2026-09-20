# -*- coding: utf-8 -*-
"""群会话名护栏 + 灰度白名单拦截提示（2026-09-18 P6 社群舞台两条沉淀）。

① ``ingest_incoming``：群入站若 ``name`` 就是发言人名 → 不作会话名（退成裸 chat_key，store 的
   upsert CASE 不会用裸 chat_key 冲掉已存真群名）。实锤：A 线 @本账号 触发路径把「2026社群聊天」
   改名成 Katie。私聊不受影响（私聊显示名本来就是对端本人）。
② ``TelegramClient._note_allowlist_skip``：同群 1h 内只 INFO 提示一次；群 handler 白名单分支
   真的在用它（静态钉）。
"""
from __future__ import annotations

import time
from pathlib import Path

from src.client.telegram_client import TelegramClient
from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import ingest_incoming
from tests._source_block import source_block

_TC_PATH = Path(__file__).resolve().parents[1] / "src" / "client" / "telegram_client.py"
_ACCT, _GROUP = "6834964252", "-1004309455763"
_GCID = f"telegram:{_ACCT}:{_GROUP}"


def _seed_group(st: InboxStore) -> None:
    ingest_incoming(st, platform="telegram", account_id=_ACCT, chat_key=_GROUP,
                    name="2026社群聊天", text="hello group", ts=time.time() - 60, msg_id="g1",
                    direction="in", chat_type="group",
                    sender_id="1", sender_name="Someone")


def test_group_title_not_overwritten_by_speaker_name(tmp_path):
    st = InboxStore(tmp_path / "inbox.db")
    _seed_group(st)
    assert (st.get_conversation(_GCID) or {}).get("display_name") == "2026社群聊天"
    # 有 bug 的调用方：显示名传了发言人名
    ingest_incoming(st, platform="telegram", account_id=_ACCT, chat_key=_GROUP,
                    name="Katie", text="Pak, harga GaN 20W?", ts=time.time(), msg_id="g2",
                    direction="in", chat_type="group",
                    sender_id="8244899900", sender_name="Katie")
    conv = st.get_conversation(_GCID) or {}
    assert conv.get("display_name") == "2026社群聊天", "群会话名被发言人名覆盖"
    rows = st.list_recent_messages(_GCID, limit=5)
    assert len(rows) == 2, "护栏不许丢消息"
    st.close()


def test_group_guard_also_fires_via_source_chat_type_and_negative_id(tmp_path):
    # source 派调用方（tg_message_payload / A 线镜像）：chat_type 塞 source，sender_name 亦在 source
    st = InboxStore(tmp_path / "inbox.db")
    _seed_group(st)
    ingest_incoming(st, platform="telegram", account_id=_ACCT, chat_key=_GROUP,
                    name="Katie", text="x", ts=time.time(), msg_id="g3", direction="in",
                    source={"chat_type": "supergroup", "sender_id": "8244899900",
                            "sender_name": "Katie"})
    assert (st.get_conversation(_GCID) or {}).get("display_name") == "2026社群聊天"
    # 连 chat_type 都没带：TG 负数 chat_id 启发式判群，护栏仍生效
    ingest_incoming(st, platform="telegram", account_id=_ACCT, chat_key=_GROUP,
                    name="Katie", text="y", ts=time.time(), msg_id="g4", direction="in",
                    sender_id="8244899900", sender_name="Katie")
    assert (st.get_conversation(_GCID) or {}).get("display_name") == "2026社群聊天"
    st.close()


def test_group_real_title_change_still_applies(tmp_path):
    # 群改名是合法的：name ≠ 发言人名 → 照常覆盖
    st = InboxStore(tmp_path / "inbox.db")
    _seed_group(st)
    ingest_incoming(st, platform="telegram", account_id=_ACCT, chat_key=_GROUP,
                    name="2026社群聊天（新）", text="z", ts=time.time(), msg_id="g5",
                    direction="in", chat_type="group", sender_id="2", sender_name="Katie")
    assert (st.get_conversation(_GCID) or {}).get("display_name") == "2026社群聊天（新）"
    st.close()


def test_private_chat_name_unaffected(tmp_path):
    # 私聊：显示名就是对端本人，即便调用方把 sender_name 也填成同名，不得被护栏抹掉
    st = InboxStore(tmp_path / "inbox.db")
    cid = f"telegram:{_ACCT}:8244899900"
    ingest_incoming(st, platform="telegram", account_id=_ACCT, chat_key="8244899900",
                    name="Katie", text="hi", ts=time.time(), msg_id="p1", direction="in",
                    sender_name="Katie")
    assert (st.get_conversation(cid) or {}).get("display_name") == "Katie"
    st.close()


# ── 灰度白名单拦截提示节流 ─────────────────────────────────────────────────────

def _client():
    c = TelegramClient.__new__(TelegramClient)
    return c


def test_allowlist_skip_note_throttles_per_chat():
    c = _client()
    t0 = 1_000_000.0
    assert c._note_allowlist_skip(-100123, now=t0) is True
    assert c._note_allowlist_skip(-100123, now=t0 + 10) is False
    assert c._note_allowlist_skip(-100123, now=t0 + 3599) is False
    assert c._note_allowlist_skip(-100123, now=t0 + 3601) is True
    # 不同群互不影响；int/str 同键
    assert c._note_allowlist_skip("-100456", now=t0 + 20) is True
    assert c._note_allowlist_skip(-100456, now=t0 + 30) is False


def test_allowlist_skip_note_book_capped():
    c = _client()
    for i in range(500):
        assert c._note_allowlist_skip(-i - 1, now=1.0) is True
    assert len(c._allowlist_skip_noted) == 500
    # 溢出整体清空后重新记账（下一轮再提示，不无限增长）
    assert c._note_allowlist_skip(-9999, now=2.0) is True
    assert len(c._allowlist_skip_noted) == 1


def test_group_handler_allowlist_branch_uses_note_and_hint():
    block = source_block(_TC_PATH, "async def handle_group_message(")
    assert "group_allowlist_blocked(" in block
    assert "self._note_allowlist_skip(chat_id)" in block, "白名单分支未接节流提示"
    assert "allowlist_chat_ids" in block, "提示里应告诉运维往哪个键加群"
    # 提示必须在白名单判定之后、其它闸之前（它是第一道闸）
    assert block.index("group_allowlist_blocked(") < block.index("self._note_allowlist_skip(chat_id)") \
        < block.index("_msg_dedup.claim(")
