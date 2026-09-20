"""未读可信化 v2（last_in_ts 闸门）+ 平台已读回执决策层 + 标签全量摘除（2026-08-23）。

事故背景（内测实录「消息已读了，未读数字消失后还会再出现」）：
- 旧闸门 ``unread>0 AND last_ts > last_read_ts`` 的 last_ts 含**自己的出站**——
  坐席已读后回一句，last_ts 前进，手机端未读残值被整数复活（路径 B）；
- 工作台已读从不回传平台（只有 AI 自动回复链在发回执），协议号目录同步每轮
  带回未读残值，成为回弹的持续供体（路径 C→read_sync 决策层）。

本文件钉住：
1. 闸门矩阵：入站/出站 × 水位前后（自己出站绝不复活徽标＝核心回归钉）；
2. 目录同步「未读增长⇒近似推进 last_in_ts」语义（无消息回流也能点亮真新未读）；
3. 回填 SQL 正确性 + 幂等（可安全重跑不变量）；
4. 三处同口径：store.effective_unread / SQL 聚合 / normalizer 纯函数；
5. read_sync 配置解析（bool/dict/list）与同会话节流；
6. remove_tag_from_all_conversations（含归档会话 + 子串假阳性防线）。
"""

from __future__ import annotations

import pytest

from src.inbox.normalizer import _effective_unread_from_row
from src.inbox.store import (
    _LAST_IN_TS_BACKFILL_SQL,
    InboxConversation,
    InboxMessage,
    InboxStore,
)


def _conv(cid: str, *, unread: int = 0, last_ts: float = 0.0,
          platform: str = "telegram", account: str = "acc",
          chat_key: str = "") -> InboxConversation:
    ck = chat_key or cid.rsplit(":", 1)[-1]
    return InboxConversation(
        conversation_id=cid, platform=platform, account_id=account,
        chat_key=ck, display_name=f"客户{ck}", last_text="hi",
        last_ts=last_ts, unread=unread, chat_type="private",
    )


def _msg(cid: str, mid: str, direction: str, ts: float,
         text: str = "hello") -> InboxMessage:
    return InboxMessage(
        conversation_id=cid, platform_msg_id=mid, direction=direction,
        text=text, ts=ts,
    )


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    s.close()


# ── 1. 闸门矩阵：自己的出站绝不复活徽标 ─────────────────────────────


def test_own_outbound_does_not_resurrect_badge(store):
    cid = "telegram:acc:77"
    store.ingest_batch(_conv(cid, unread=1, last_ts=100), [_msg(cid, "m1", "in", 100)])
    row = store.get_conversation(cid)
    assert store.effective_unread(row) == 1          # 新入站未读如实点亮

    store.mark_conversation_read(cid)
    assert store.effective_unread(store.get_conversation(cid)) == 0

    # 坐席回复：出站镜像把 last_ts 顶到 200，手机端未读残值仍是 1（无回执场景）。
    # v1 闸门在这里返回 1（= 用户报障的「已读后又出现」）；v2 必须保持 0。
    store.ingest_batch(_conv(cid, unread=1, last_ts=200), [_msg(cid, "m2", "out", 200)])
    row = store.get_conversation(cid)
    assert float(row["last_ts"]) == 200
    assert store.effective_unread(row) == 0

    agg = store.sum_effective_unread_by_account()
    assert agg.get(("telegram", "acc"), 0) == 0      # SQL 聚合同口径

    # 真的又来一条 → 徽标回来（新未读绝不能被误吞）
    store.ingest_batch(_conv(cid, unread=2, last_ts=300), [_msg(cid, "m3", "in", 300)])
    row = store.get_conversation(cid)
    assert store.effective_unread(row) == 2
    assert store.sum_effective_unread_by_account()[("telegram", "acc")] == 2


def test_120_badge_counts_only_visible_private_unarchived(store):
    """#120（0831 钧原图 921「幽灵未读」）：徽标聚合只数用户可见真未读。

    列表默认视图只显示私聊未归档会话，而旧聚合把群/频道/归档/legacy 群
    （chat_type 迁移列没回填的负 id TG 群）全数了进去——「全部账号⑩/TG⑦」
    列表却无一条未读行。矩阵：私聊未读计入；群/频道不计；归档不计；
    legacy 负 id TG 群（chat_type 仍 'private'）不计；LINE group/room 键不计。
    """
    # 私聊未读 → 计入
    store.ingest_batch(_conv("telegram:acc:11", unread=2, last_ts=100),
                       [_msg("telegram:acc:11", "p1", "in", 100)])
    # 真群（chat_type=group）→ 不计
    g = _conv("telegram:acc:22", unread=5, last_ts=100)
    g.chat_type = "group"
    store.ingest_batch(g, [_msg("telegram:acc:22", "g1", "in", 100)])
    # legacy TG 群：负 id、chat_type 还顶着 'private' → 启发式排除
    store.ingest_batch(
        _conv("telegram:acc:-100777", unread=7, last_ts=100, chat_key="-100777"),
        [_msg("telegram:acc:-100777", "lg1", "in", 100)])
    # LINE group 键 → 不计；LINE 私聊 → 计入
    store.ingest_batch(
        _conv("line:acc:line:group:abc", unread=3, last_ts=100,
              platform="line", chat_key="line:group:abc"),
        [_msg("line:acc:line:group:abc", "lgr", "in", 100)])
    store.ingest_batch(
        _conv("line:acc:U123", unread=1, last_ts=100, platform="line",
              chat_key="U123"),
        [_msg("line:acc:U123", "lp1", "in", 100)])
    # 归档的私聊未读 → 不计
    store.ingest_batch(_conv("telegram:acc:33", unread=4, last_ts=100),
                       [_msg("telegram:acc:33", "a1", "in", 100)])
    store.set_conv_archived("telegram:acc:33", True)

    agg = store.sum_effective_unread_by_account()
    assert agg.get(("telegram", "acc"), 0) == 2, \
        f"TG 应只剩私聊 2 条（群5/legacy群7/归档4 全不计），got {agg}"
    assert agg.get(("line", "acc"), 0) == 1
    # 取消归档 → 回到可见集合，重新计入
    store.set_conv_archived("telegram:acc:33", False)
    assert store.sum_effective_unread_by_account()[("telegram", "acc")] == 6


def test_dialog_sync_unread_increase_lights_badge_without_messages(store):
    """目录同步（无消息回流）：未读增长⇒近似推进 last_in_ts，真新未读能点亮。"""
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "88", "name": "X", "ts": 400, "unread": 3},
    ])
    cid = "telegram:acc:88"
    assert store.effective_unread(store.get_conversation(cid)) == 3

    store.mark_conversation_read(cid)
    assert store.effective_unread(store.get_conversation(cid)) == 0

    # 同未读数 + 更新 ts（≈手机端自己发了消息 / 会话被顶起）→ 不复活
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "88", "name": "X", "ts": 500, "unread": 3},
    ])
    assert store.effective_unread(store.get_conversation(cid)) == 0

    # 未读增长（3→4）→ 必有新入站 → 点亮
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "88", "name": "X", "ts": 600, "unread": 4},
    ])
    row = store.get_conversation(cid)
    assert store.effective_unread(row) == 4
    assert float(row["last_in_ts"]) == 600


def test_legacy_row_without_last_in_falls_back_to_last_ts(store):
    """last_in_ts=0（存量行回填不到入站）→ 回落旧 last_ts 闸门，宁多报不漏报。"""
    cid = "telegram:acc:99"
    store.upsert_conversation(_conv(cid, unread=2, last_ts=100))
    with store._lock:
        store._conn.execute(
            "UPDATE conversations SET last_in_ts=0 WHERE conversation_id=?", (cid,))
        store._conn.commit()
    row = store.get_conversation(cid)
    assert store.effective_unread(row) == 2          # 旧行为：last_ts(100) > read(0)
    store.mark_conversation_read(cid)
    assert store.effective_unread(store.get_conversation(cid)) == 0


# ── 2. 回填 SQL：正确 + 幂等（可安全重跑不变量）────────────────────


def test_backfill_sql_correct_and_idempotent(store):
    cid = "telegram:acc:55"
    store.ingest_batch(_conv(cid, unread=1, last_ts=300), [
        _msg(cid, "a1", "in", 100), _msg(cid, "a2", "out", 200),
        _msg(cid, "a3", "in", 250),
    ])
    other = "telegram:acc:56"
    store.ingest_batch(_conv(other, unread=0, last_ts=500),
                       [_msg(other, "b1", "in", 500)])

    with store._lock:
        # 模拟升级前存量：全体归零 → 跑回填
        store._conn.execute("UPDATE conversations SET last_in_ts=0")
        store._conn.execute(_LAST_IN_TS_BACKFILL_SQL)
        store._conn.commit()
    assert float(store.get_conversation(cid)["last_in_ts"]) == 250   # 最后一条入站
    assert float(store.get_conversation(other)["last_in_ts"]) == 500

    # 幂等：非零值不被重算/冲掉（WHERE last_in_ts=0 守卫）
    with store._lock:
        store._conn.execute(
            "UPDATE conversations SET last_in_ts=9999 WHERE conversation_id=?",
            (cid,))
        store._conn.execute(_LAST_IN_TS_BACKFILL_SQL)
        store._conn.commit()
    assert float(store.get_conversation(cid)["last_in_ts"]) == 9999


def test_backfill_amnesty_clears_stale_residue(store):
    """回填即存量清账：坐席早已读完、手机残留未读的老会话在新闸门下熄灭。"""
    cid = "telegram:acc:44"
    store.ingest_batch(_conv(cid, unread=5, last_ts=100), [_msg(cid, "m1", "in", 100)])
    store.mark_conversation_read(cid)                 # 水位=100
    # 坐席其后又回了两句（出站把 last_ts 顶到 300），手机未读残值 5 一直没清
    store.ingest_batch(_conv(cid, unread=5, last_ts=300), [
        _msg(cid, "m2", "out", 200), _msg(cid, "m3", "out", 300),
    ])
    row = store.get_conversation(cid)
    assert store.effective_unread(row) == 0           # v2 闸门：不再回弹
    # v1 口径复算（拿 last_ts 当闸门）会是 5 —— 这就是修掉的病灶
    assert float(row["last_ts"]) > float(row["last_read_ts"])


# ── 3. 三处同口径：normalizer 纯函数 ────────────────────────────────


def test_normalizer_gate_matches_store_semantics():
    base = {"unread": 5, "last_ts": 200, "last_read_ts": 150}
    assert _effective_unread_from_row({**base, "last_in_ts": 100}) == 0   # 出站顶起
    assert _effective_unread_from_row({**base, "last_in_ts": 180}) == 5   # 新入站
    assert _effective_unread_from_row({**base, "last_in_ts": 0}) == 5     # 回落 last_ts
    assert _effective_unread_from_row(
        {"unread": 0, "last_ts": 200, "last_in_ts": 180, "last_read_ts": 0}) == 0
    # ts 字段脏值 → 回落「原样返回同步未读」（与 v1 同语义的兜底路径）
    assert _effective_unread_from_row(
        {"unread": 5, "last_ts": "x", "last_in_ts": None,
         "last_read_ts": None}) == 5


# ── 4. read_sync 决策层 ─────────────────────────────────────────────


def test_read_sync_push_enabled_parsing():
    from src.inbox import read_sync as rs
    assert rs.push_enabled(
        {"inbox": {"read_sync": {"push_to_platform": True}}}, "telegram")
    assert rs.push_enabled(
        {"inbox": {"read_sync": {"push_to_platform": {"telegram": True}}}},
        "Telegram")
    assert not rs.push_enabled(
        {"inbox": {"read_sync": {"push_to_platform": {"telegram": False}}}},
        "telegram")
    assert rs.push_enabled(
        {"inbox": {"read_sync": {"push_to_platform": ["telegram", "whatsapp"]}}},
        "whatsapp")
    assert not rs.push_enabled(
        {"inbox": {"read_sync": {"push_to_platform": ["telegram"]}}}, "line")
    assert not rs.push_enabled({}, "telegram")            # 缺省=关（产品开关）
    assert not rs.push_enabled(None, "telegram")
    assert not rs.push_enabled(
        {"inbox": {"read_sync": {"push_to_platform": True}}}, "")


def test_read_sync_throttle_and_stats():
    from src.inbox import read_sync as rs
    rs._reset_for_tests()
    assert rs.should_push("c1", now=1000.0)
    rs.record_push("c1", now=1000.0)
    assert not rs.should_push("c1", now=1010.0)            # 20s 窗内节流
    assert rs.should_push("c1", now=1020.5)
    assert rs.should_push("c2", now=1001.0)                # 不同会话互不影响
    rs.note_result(True)
    rs.note_result(False)
    rs.note_no_unread()
    snap = rs.stats_snapshot()
    assert snap["pushed"] == 1
    assert snap["skipped_throttle"] == 1
    assert snap["push_ok"] == 1 and snap["push_fail"] == 1
    assert snap["skipped_no_unread"] == 1
    rs._reset_for_tests()


# ── 5. 标签全量摘除 ─────────────────────────────────────────────────


def test_remove_tag_from_all_conversations(store):
    c1, c2, c3 = "telegram:acc:1", "telegram:acc:2", "telegram:acc:3"
    for c in (c1, c2, c3):
        store.upsert_conversation(_conv(c, last_ts=100))
    store.set_conv_tags(c1, ["333", "vip"])
    store.set_conv_tags(c2, ["333"])
    store.set_conv_tags(c3, ["3330"])                     # 子串近邻，不得误伤
    store.set_conv_archived(c2, True)                     # 归档会话也要摘干净

    removed = store.remove_tag_from_all_conversations("333")
    assert removed == 2
    assert store.get_conv_tags(c1) == ["vip"]
    assert store.get_conv_tags(c2) == []
    assert store.get_conv_tags(c3) == ["3330"]

    assert store.remove_tag_from_all_conversations("333") == 0   # 幂等
    assert store.remove_tag_from_all_conversations("") == 0
    assert store.remove_tag_from_all_conversations("不存在") == 0
