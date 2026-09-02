"""B87（实施68）：对端手机删消息 → 工作台镜像同步软删契约。

钧 _419：手机端（TG 官方客户端）删消息后工作台仍显示。口径（钧 04:03 定稿）＝
界面同步删 + AI 记忆保留但不主动提已删内容。
**#146（2026-09-02）改口径**：残留错误内容会进 AI 记忆污染后续回复 → 对端删的消息
不再进 AI 历史（``list_recent_messages`` 默认口径也剔 peer 软删行；坐席「仅工作台
删除」仍按旧业务口径可见），关联情景记忆另清（见 test_peer_delete_memory_purge）。

链路：TG UpdateDeleteMessages（只带裸 message id）→ ``report_deleted_messages``
→ ``store.soft_delete_by_platform_msg_ids``（按 platform_msg_id 跨会话软删，本账号
限定）→ UI 读路径（include_deleted=False）不可见 + messages_deleted SSE 同步。
"""

from __future__ import annotations

from pathlib import Path

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore


def _seed(store: InboxStore, cid: str, pmid: str, text: str, ts: float,
          *, platform="telegram", account="acct", chat_key=""):
    conv = InboxConversation(
        conversation_id=cid, platform=platform, account_id=account,
        chat_key=chat_key or cid.rsplit(":", 1)[-1], display_name="客户")
    store.ingest_batch(conv, [InboxMessage(
        conversation_id=cid, platform_msg_id=pmid, text=text, ts=ts,
        direction="in")])


def test_soft_delete_by_platform_msg_id(tmp_path: Path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:555"
    _seed(store, cid, "1001", "第一条", 1.0)
    _seed(store, cid, "1002", "要删的", 2.0)
    _seed(store, cid, "1003", "第三条", 3.0)

    n = store.soft_delete_by_platform_msg_ids("telegram", "acct", ["1002"])
    assert n == 1

    # UI 读路径（include_deleted=False）看不到；#146 起业务默认口径（LLM 历史消费方）
    # 也看不到 peer 软删行——客户侧都不存在的消息不该再喂给 AI
    visible = store.list_recent_messages(cid, limit=50, include_deleted=False)
    assert [m["text"] for m in visible] == ["第一条", "第三条"]
    allrows = store.list_recent_messages(cid, limit=50, include_deleted=True)
    assert [m["text"] for m in allrows] == ["第一条", "第三条"]
    # 数据保留 + deleted_by=peer 留痕（库里仍在，只是读路径不给）
    raw = store.list_messages(cid, limit=50)
    deleted = [m for m in raw if float(m.get("deleted_at") or 0) > 0]
    assert len(deleted) == 1 and deleted[0]["deleted_by"] == "peer"

    # 幂等：再删同 id 不重复计
    assert store.soft_delete_by_platform_msg_ids("telegram", "acct", ["1002"]) == 0

    # 会话预览重算：末条已是「第三条」（删的是中间条，末条不变仍正确）
    conv = store.get_conversation(cid)
    assert conv["last_text"] == "第三条"


def test_local_delete_keeps_business_view_but_peer_delete_does_not(tmp_path: Path):
    """#146 两种软删的口径分岔：坐席「仅工作台删除」＝本地视图动作，业务口径
    （include_deleted=True）必须仍看到（回复时延/replied-after 护栏的事实没变）；
    对端删除＝客户侧也没了，任何口径都不给。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:acct:555"
    _seed(store, cid, "1", "坐席本地删的", 1.0)
    _seed(store, cid, "2", "客户手机删的", 2.0)
    _seed(store, cid, "3", "都在", 3.0)
    mid_local = [m for m in store.list_messages(cid, limit=10) if m["text"] == "坐席本地删的"][0]["message_id"]
    assert store.delete_messages_local(cid, [mid_local], deleted_by="agent:a1") == 1
    assert store.soft_delete_by_platform_msg_ids("telegram", "acct", ["2"]) == 1

    ui = [m["text"] for m in store.list_recent_messages(cid, limit=10, include_deleted=False)]
    biz = [m["text"] for m in store.list_recent_messages(cid, limit=10, include_deleted=True)]
    assert ui == ["都在"]
    assert biz == ["坐席本地删的", "都在"]
    # before_ts 游标分支同口径
    older = [m["text"] for m in store.list_recent_messages(
        cid, limit=10, before_ts=3.0, include_deleted=True)]
    assert older == ["坐席本地删的"]


def test_select_live_by_platform_msg_ids(tmp_path: Path):
    """#146 前置读：只回尚未软删的行、本账号限定、chat_key 可收窄。"""
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acct:100", "42", "会话100", 1.0, chat_key="100")
    _seed(store, "telegram:acct:200", "42", "会话200", 1.0, chat_key="200")
    _seed(store, "telegram:other:300", "42", "别的账号", 1.0, account="other", chat_key="300")
    rows = store.select_live_by_platform_msg_ids("telegram", "acct", ["42"])
    assert sorted(r["text"] for r in rows) == ["会话100", "会话200"]
    assert {r["direction"] for r in rows} == {"in"}
    rows_n = store.select_live_by_platform_msg_ids("telegram", "acct", ["42"], chat_key="100")
    assert [r["text"] for r in rows_n] == ["会话100"]
    store.soft_delete_by_platform_msg_ids("telegram", "acct", ["42"], chat_key="100")
    assert [r["text"] for r in store.select_live_by_platform_msg_ids(
        "telegram", "acct", ["42"])] == ["会话200"]
    assert store.select_live_by_platform_msg_ids("telegram", "acct", []) == []


def test_soft_delete_scoped_to_account(tmp_path: Path):
    """同 message id 在不同账号并存 → 只删本账号那条（绝不跨账号误删）。"""
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acctA:555", "9", "A 的消息", 1.0, account="acctA")
    _seed(store, "telegram:acctB:777", "9", "B 的消息", 1.0, account="acctB")
    n = store.soft_delete_by_platform_msg_ids("telegram", "acctA", ["9"])
    assert n == 1
    a = store.list_recent_messages(
        "telegram:acctA:555", limit=10, include_deleted=False)
    b = store.list_recent_messages(
        "telegram:acctB:777", limit=10, include_deleted=False)
    assert a == []                       # A 那条被删
    assert [m["text"] for m in b] == ["B 的消息"]   # B 那条不动


def test_soft_delete_chat_key_narrowing(tmp_path: Path):
    """频道版带 channel_id → chat_key 收窄，同账号跨会话同 id 只删指定会话。"""
    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acct:100", "42", "会话100", 1.0, chat_key="100")
    _seed(store, "telegram:acct:200", "42", "会话200", 1.0, chat_key="200")
    n = store.soft_delete_by_platform_msg_ids(
        "telegram", "acct", ["42"], chat_key="100")
    assert n == 1
    assert store.list_recent_messages(
        "telegram:acct:100", limit=10, include_deleted=False) == []
    assert [m["text"] for m in store.list_recent_messages(
        "telegram:acct:200", limit=10, include_deleted=False)] == ["会话200"]


def test_report_deleted_messages_bridge_and_sse(tmp_path: Path, monkeypatch):
    """bridge：软删 + 发 messages_deleted(op=peer_delete) SSE。"""
    import src.integrations.protocol_bridge as pb
    from src.integrations.shared import event_bus as eb
    monkeypatch.setattr(eb, "_bus", None, raising=False)

    store = InboxStore(tmp_path / "inbox.db")
    _seed(store, "telegram:acct:555", "1002", "要删的", 2.0)
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)

    n = pb.report_deleted_messages("telegram", "acct", ["1002"])
    assert n == 1
    assert store.list_recent_messages(
        "telegram:acct:555", limit=10, include_deleted=False) == []

    from src.integrations.shared.event_bus import get_event_bus
    evts = [e for e in get_event_bus().recent_events(50)
            if e["type"] == "messages_deleted"]
    assert evts, "对端删除同步必须发 messages_deleted SSE 给工作台"
    assert evts[-1]["data"]["op"] == "peer_delete"
    assert evts[-1]["data"]["count"] == 1


def test_report_deleted_messages_empty_noop(tmp_path: Path, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "get_inbox_store", lambda: InboxStore(tmp_path / "x.db"))
    assert pb.report_deleted_messages("telegram", "acct", []) == 0
    assert pb.report_deleted_messages("telegram", "acct", ["  "]) == 0
