"""归档生命周期门禁（P0-198，2026-08-04）。

事故：198 测试机上一条 33 条消息的**活跃**会话在坐席聊天途中从工作台彻底消失。根因＝
归档在实现上是**永久**的——工作台所有默认视图都过滤 ``archived=1``，而入站链路从不复位
该标记，于是客户之后无论发多少条消息，会话都不会回来、也不产生任何未读提示。

本文件钉住三条不变量（重点在那些**不该**复活的边界，误复活会让归档功能整体失效）：
① 归档后客户再开口 → 自动复活；
② 归档之前的历史消息被重放（首次全量同步 / 坐席打开会话触发 ingest_thread）→ **不复活**；
③ 出站消息（坐席/AI 自己发的）不复活——归档是坐席的明示决定，自动发的话不该撤销它。
以及 ``archived_at`` 与 ``auto_archived_at`` 两列的职责分工。
"""

import time

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore

CID = "telegram:acctA:c1"


def _store():
    return InboxStore(":memory:")


def _conv(last_ts: float, unread: int = 0) -> InboxConversation:
    return InboxConversation(
        conversation_id=CID, platform="telegram", account_id="acctA",
        chat_key="c1", display_name="ds Lao", last_ts=last_ts, unread=unread,
    )


def _msg(ts: float, direction: str = "in", text: str = "在吗") -> InboxMessage:
    # platform_msg_id 唯一即可保证「真正新插入」（主键含 pmid）
    return InboxMessage(
        conversation_id=CID, platform_msg_id=f"m{int(ts)}{direction}",
        direction=direction, text=text, ts=ts,
    )


def _archived(store) -> int:
    row = store._conn.execute(
        "SELECT archived FROM conversation_meta WHERE conversation_id=?", (CID,),
    ).fetchone()
    return int(row["archived"]) if row else 0


def _archived_at(store) -> float:
    row = store._conn.execute(
        "SELECT archived_at FROM conversation_meta WHERE conversation_id=?", (CID,),
    ).fetchone()
    return float(row["archived_at"]) if row else 0.0


# ── ① 归档后客户再开口 → 复活 ─────────────────────────────────────────────

def test_inbound_after_archive_revives_conversation():
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now - 600), [_msg(now - 600)])
    store.set_conv_archived(CID, True, source="test")
    assert _archived(store) == 1

    # 客户在归档之后又说了一句
    store.ingest_batch(_conv(now + 5), [_msg(now + 5, text="你还在吗")])
    assert _archived(store) == 0, "归档后的新入站消息必须让会话回到默认视图"
    assert _archived_at(store) == 0.0, "复活后 archived_at 必须清零"


def test_revival_reflected_in_list_render_map():
    """复活必须体现在坐席列表**真正读的那个字段**上，不只是 DB 里一列。

    会话列表路由把 ``list_conv_tags_map`` 的 archived 原样下发（``c["archived"]``），
    前端据此隐藏——即归档的「消失」是纯客户端过滤，store 的 list_conversations
    本身并不过滤。所以契约点在这个 map，别改成断言 list_conversations。
    """
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now - 600), [_msg(now - 600)])
    store.set_conv_archived(CID, True, source="test")
    assert store.list_conv_tags_map([CID])[CID]["archived"] is True

    store.ingest_batch(_conv(now + 5), [_msg(now + 5, text="喂")])
    assert store.list_conv_tags_map([CID])[CID]["archived"] is False


# ── ② 归档之前的历史消息重放 → 不复活 ────────────────────────────────────

def test_replayed_history_before_archive_does_not_revive():
    """坐席打开已归档会话（ingest_thread 重放历史）不得把它顶回默认视图。

    判据用的是消息的**平台时间**而非入库时间，正是为了区分这一类：历史消息虽然此刻
    才落库，但它发生在归档之前。若误按入库时间判定，归档功能会当场失效——归档完只要
    有人点开看一眼就自动撤销。
    """
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now - 600), [_msg(now - 600)])
    store.set_conv_archived(CID, True, source="test")

    # 重放归档之前的 3 条历史（ts 全部早于归档时刻）
    store.ingest_batch(_conv(now - 600), [
        _msg(now - 900, text="历史1"), _msg(now - 800, text="历史2"),
        _msg(now - 700, text="历史3"),
    ])
    assert _archived(store) == 1, "归档之前的历史消息不得触发复活"


def test_outbound_after_archive_does_not_revive():
    """出站不复活：归档是人的明示决定，自动发出的话不该替他撤销。"""
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now - 600), [_msg(now - 600)])
    store.set_conv_archived(CID, True, source="test")

    store.ingest_batch(_conv(now + 5), [_msg(now + 5, direction="out", text="您好")])
    assert _archived(store) == 1


def test_duplicate_inbound_does_not_revive():
    """去重跳过的重复消息不算「新入站」——否则任何 re-ingest 都能撤销归档。"""
    now = time.time()
    store = _store()
    dup = _msg(now - 600)
    store.ingest_batch(_conv(now - 600), [dup])
    store.set_conv_archived(CID, True, source="test")

    store.ingest_batch(_conv(now - 600), [dup])   # 同一条，主键相同 → INSERT OR IGNORE
    assert _archived(store) == 1


def test_never_archived_conversation_gets_no_meta_row():
    """未归档会话走入站路径不得凭空建 conversation_meta 行（与 clear_snooze 同护栏）。"""
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now), [_msg(now)])
    row = store._conn.execute(
        "SELECT 1 FROM conversation_meta WHERE conversation_id=?", (CID,),
    ).fetchone()
    assert row is None


# ── ③ archived_at / auto_archived_at 职责分工 ────────────────────────────

def test_archived_at_written_on_archive_and_cleared_on_unarchive():
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now), [])
    store.set_conv_archived(CID, True, source="test")
    assert _archived_at(store) >= now, "归档必须留下归档时刻（入站复活的唯一判据）"

    store.set_conv_archived(CID, False, source="test")
    assert _archived_at(store) == 0.0


def test_manual_archive_does_not_set_auto_archived_at():
    """人工归档不得写 auto_archived_at——那一列是自动归档的幂等闸。

    198 排查时正是靠「archived=1 而 auto_archived_at=0」断定该会话是被人工归档的，
    而非自动归档任务所为。两列混用会毁掉这个可归因性。
    """
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now), [])
    store.set_conv_archived(CID, True, source="api:conv_archive", actor="alice")
    row = store._conn.execute(
        "SELECT auto_archived_at FROM conversation_meta WHERE conversation_id=?", (CID,),
    ).fetchone()
    assert float(row["auto_archived_at"]) == 0.0


def test_auto_archive_candidate_excluded_after_unarchive():
    """自动归档过的会话被坐席解档后，不得被下一轮 tick 立刻归档回去。"""
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now - 72 * 3600), [_msg(now - 72 * 3600)])
    assert [c["conversation_id"] for c in store._auto_archive_candidates(24)] == [CID]

    store.set_conv_archived(CID, True, source="auto:idle", actor="system")
    store.mark_auto_archived(CID, now)
    store.set_conv_archived(CID, False, source="test")   # 坐席捞回来
    assert store._auto_archive_candidates(24) == [], \
        "auto_archived_at 非 0 应永久排除该会话，避免与坐席拉锯"


# ── ④ 存量「被埋会话」体检（与 archived_at 无关的信号）────────────────────────

def test_buried_audit_flags_archived_with_unread():
    """归档 + 有未读＝客户在等而没人看得见，必须能被点名。

    这条信号刻意**不看** archived_at：存量已归档行的真实归档时刻不可追溯（只能回填升级
    时刻），入站复活判据够不着它们，只有「未读」能把历史损失捞出来。
    """
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now, unread=3), [_msg(now)])
    store.set_conv_archived(CID, True, source="test")
    hits = store.list_buried_archived()
    assert [h["conversation_id"] for h in hits] == [CID]
    assert hits[0]["unread"] == 3


def test_buried_audit_ignores_read_and_unarchived():
    """两类**不该**报的：读完才归档（正常收尾）／没归档但有未读（列表里看得见）。"""
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now, unread=0), [_msg(now)])
    store.set_conv_archived(CID, True, source="test")
    assert store.list_buried_archived() == [], "读完再归档是正常收尾，不该报"

    store.set_conv_archived(CID, False, source="test")
    store.ingest_batch(_conv(now + 1, unread=5), [_msg(now + 1, text="喂")])
    assert store.list_buried_archived() == [], "没归档的会话有未读＝列表里看得见，不该报"


def test_buried_audit_clears_after_auto_revive():
    """自动复活之后不得继续报——否则体检清单永远清不空、失去可信度。"""
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now - 600, unread=1), [_msg(now - 600)])
    store.set_conv_archived(CID, True, source="test")
    assert len(store.list_buried_archived()) == 1

    store.ingest_batch(_conv(now + 5, unread=2), [_msg(now + 5, text="你还在吗")])
    assert store.list_buried_archived() == [], "入站已让它复活，体检不该再点名"


def test_buried_audit_is_read_only():
    """体检绝不能顺手解档：存量缺可信归档时刻，批量唤回＝拿猜测覆盖运营的明示决定。"""
    now = time.time()
    store = _store()
    store.ingest_batch(_conv(now, unread=2), [_msg(now)])
    store.set_conv_archived(CID, True, source="test")
    before = _archived_at(store)
    store.list_buried_archived()
    assert _archived(store) == 1 and _archived_at(store) == before
