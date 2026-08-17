"""inbox.ingest 映射测试（Phase A）。"""

from unittest.mock import patch

from src.inbox.store import InboxStore
from src.inbox.ingest import ingest_collected_chats, ingest_thread


def _chat(**kw):
    base = {
        "platform": "whatsapp",
        "account_id": "wa-a",
        "chat_key": "room",
        "conversation_id": "whatsapp:wa-a:room",
        "name": "WA User",
        "last_msg": "hello",
        "last_ts": 110,
        "unread": 1,
        "language": "en",
        "last_message": {"text": "hello", "ts": 110, "direction": "in", "language": "en"},
    }
    base.update(kw)
    return base


def test_ingest_collected_chats_populates_store(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    n = ingest_collected_chats(store, [_chat()])
    assert n == 1
    convs = store.list_conversations()
    assert convs[0]["conversation_id"] == "whatsapp:wa-a:room"
    assert convs[0]["display_name"] == "WA User"
    assert store.count_messages("whatsapp:wa-a:room") == 1
    store.close()


def test_ingest_skips_invalid_rows(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    # 缺 conversation_id / platform 的行被跳过
    n = ingest_collected_chats(store, [{"name": "x"}, _chat(conversation_id="", platform="")])
    assert n == 0
    assert store.list_conversations() == []
    store.close()


def test_ingest_is_idempotent(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    ingest_collected_chats(store, [_chat()])
    # 同一轮聚合重放 → 消息不重复
    ingest_collected_chats(store, [_chat()])
    assert store.count_messages("whatsapp:wa-a:room") == 1
    store.close()


def test_ingest_skips_store_backed_chats_no_hash_dup(tmp_path):
    """from_store 会话不再 re-ingest：防同一消息被 hash 兜底键复制成 ``:h:`` 重复行。

    复刻真实 bug：协议 worker 经带 platform_msg_id 的权威路径落库 ``:<pmid>`` 行；随后
    聚合把 store 读出的同一会话（from_store=True、last_message 无 pmid）再写回——旧行为会
    多出一条 ``:h:<hash>`` 重复行并重复触发 auto-draft。跳过后消息数恒为 1。
    """
    store = InboxStore(tmp_path / "inbox.db")
    # 1) 权威路径：带 source.id（telegram msg_id）→ 落 ``:<pmid>`` 行
    authoritative = _chat(
        platform="telegram", account_id="8244899900", chat_key="555",
        conversation_id="telegram:8244899900:555",
        last_message={"text": "hi", "ts": 110, "direction": "in",
                      "language": "en", "source": {"id": "778"}},
    )
    assert ingest_collected_chats(store, [authoritative]) == 1
    assert store.count_messages("telegram:8244899900:555") == 1
    # 2) 聚合 re-ingest store 读出的同一会话（from_store=True，last_message 丢了 pmid）
    store_backed = _chat(
        platform="telegram", account_id="8244899900", chat_key="555",
        conversation_id="telegram:8244899900:555",
        last_message={"text": "hi", "ts": 110, "direction": "in", "language": "en"},
        from_store=True,
    )
    assert ingest_collected_chats(store, [store_backed]) == 0   # 被跳过，无新插入
    assert store.count_messages("telegram:8244899900:555") == 1  # 仍只有 1 条（无 :h: 重复）
    store.close()


def test_ingest_thread_persists_history(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    chat = _chat()
    messages = [
        {"text": "m1", "ts": 1, "message_id": "1", "language": "en"},
        {"text": "m2", "ts": 2, "message_id": "2", "language": "en"},
        {"text": "m3", "ts": 3, "message_id": "3", "language": "en"},
    ]
    assert ingest_thread(store, chat, messages) == 3
    rows = store.list_messages("whatsapp:wa-a:room")
    assert [r["text"] for r in rows] == ["m1", "m2", "m3"]
    store.close()


def test_ingest_writes_contact_id_when_resolver_registered(tmp_path):
    """Q 延伸：register_contact_resolver → conversations + conv_meta 回写 contact_id。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.register_contact_resolver(
        lambda platform, account_id, chat_key: (
            "contact-xyz" if chat_key == "messenger_rpa:Bob" else ""
        ),
    )
    chat = _chat(
        platform="messenger",
        account_id="a",
        chat_key="messenger_rpa:Bob",
        conversation_id="messenger:a:messenger_rpa:Bob",
        last_message={"text": "hi", "ts": 110, "direction": "in", "language": "en"},
    )
    with patch(
        "src.ai.chat_assistant_service.quick_analyze",
        return_value={"intent": "chitchat", "emotion": "neutral", "risk_level": "low"},
    ):
        ingest_collected_chats(store, [chat])
    conv = store.get_conversation("messenger:a:messenger_rpa:Bob")
    assert conv is not None
    assert conv["contact_id"] == "contact-xyz"
    meta = store.get_conv_meta("messenger:a:messenger_rpa:Bob")
    assert meta is not None
    assert meta["contact_id"] == "contact-xyz"
    store.close()


# ── 客户语言列的方向卫生（2026-08-15，messenger Wisley 实锤） ──────────────────

def test_outbound_mirror_does_not_pollute_conversation_language(tmp_path):
    """出站镜像绝不覆盖客户语言列。

    事故机制：客户全程中文（language=zh），AI 英文译文经出站镜像回流 ingest，
    normalize_chat 把 chat.language 取自末条消息（=我们自己的 en）→ 客户语言列
    被钉成 en → 出站翻译永远瞄准英文（自锁）。修复＝direction=out 时降级
    unknown，store 的 CASE 护栏保住已存真值。
    """
    store = InboxStore(tmp_path / "inbox.db")
    # 1) 客户中文入站 → 会话语言 zh
    ingest_collected_chats(store, [_chat(
        language="zh",
        last_message={"text": "你好呀", "ts": 110, "direction": "in",
                      "language": "zh", "source": {"id": "m1"}},
    )])
    assert store.get_conversation("whatsapp:wa-a:room")["language"] == "zh"
    # 2) 我们的英文回复镜像（旧行为会把列改成 en）
    ingest_collected_chats(store, [_chat(
        language="en", last_ts=120,
        last_message={"text": "Hello there my friend", "ts": 120,
                      "direction": "out", "language": "en", "source": {"id": "m2"}},
    )])
    assert store.get_conversation("whatsapp:wa-a:room")["language"] == "zh"
    # 3) 客户真的切换语言（入站英文）→ 照常更新
    ingest_collected_chats(store, [_chat(
        language="en", last_ts=130,
        last_message={"text": "let's speak english", "ts": 130,
                      "direction": "in", "language": "en", "source": {"id": "m3"}},
    )])
    assert store.get_conversation("whatsapp:wa-a:room")["language"] == "en"
    store.close()
