# -*- coding: utf-8 -*-
"""十期：会话列表游标分页（store 层 before_ts + has_more 计数）。"""
import time

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore


def _mk_store(tmp_path, n=8):
    store = InboxStore(tmp_path / "inbox.db")
    base = time.time()
    for i in range(n):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"tg:acct:{i}",
            platform="telegram",
            account_id="acct",
            chat_key=str(i),
            display_name=f"客户{i}",
            last_ts=base - i * 60,   # i 越大越旧
            last_text=f"msg {i}",
        ))
    return store, base


def test_list_conversations_before_ts_cursor(tmp_path):
    store, base = _mk_store(tmp_path)
    page1 = store.list_conversations(limit=3)
    assert [c["chat_key"] for c in page1] == ["0", "1", "2"]

    oldest = min(float(c["last_ts"]) for c in page1)
    page2 = store.list_conversations(limit=3, before_ts=oldest)
    assert [c["chat_key"] for c in page2] == ["3", "4", "5"]
    # 不重不漏：两页无交集
    assert not {c["conversation_id"] for c in page1} & {c["conversation_id"] for c in page2}

    oldest2 = min(float(c["last_ts"]) for c in page2)
    page3 = store.list_conversations(limit=3, before_ts=oldest2)
    assert [c["chat_key"] for c in page3] == ["6", "7"]


def test_count_conversations_older_than(tmp_path):
    store, base = _mk_store(tmp_path)
    # 第 3 条（i=2）的 last_ts 之前还有 5 条更旧（i=3..7）
    third_ts = base - 2 * 60
    assert store.count_conversations_older_than(third_ts) == 5
    # 最旧一条之前没有更旧
    oldest_ts = base - 7 * 60
    assert store.count_conversations_older_than(oldest_ts) == 0
    # 平台过滤
    assert store.count_conversations_older_than(third_ts, platform="telegram") == 5
    assert store.count_conversations_older_than(third_ts, platform="line") == 0


def test_before_ts_none_keeps_legacy_behavior(tmp_path):
    store, _ = _mk_store(tmp_path, n=4)
    legacy = store.list_conversations(limit=10)
    explicit = store.list_conversations(limit=10, before_ts=None)
    assert [c["conversation_id"] for c in legacy] == [c["conversation_id"] for c in explicit]
    assert len(legacy) == 4
