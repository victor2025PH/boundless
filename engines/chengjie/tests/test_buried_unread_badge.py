"""#142「没有未读却一直显示未读」——徽标口径对齐 + 归档未读入口（2026-09-02）。

事故背景（诊断包 WEXX7E，9/1 health_watchdog 实录）：16 个会话已归档却有未读
入站（共 19 条未读）——未读挂在已归档会话上，默认视图把会话藏了，顶部徽标却按
全库口径照算，用户对着空列表永远清不掉。

本文件钉住：
1. 徽标聚合默认剔除已归档会话的未读（与列表默认视图同一口径，#120 行为的回归钉）；
2. ``include_archived=True`` = ``inbox.badge.include_archived`` 显式回退旧口径；
3. ``sum_archived_unread_by_account``：被埋会话的界面化取数口——归档中的有效未读
   （convs/unread），与主徽标恰为同一全集按 archived 二分，读掉/解档即消退；
4. 路由层：``_badge_include_archived`` 配置解析、``_archived_unread_map`` 键形状、
   ``_unread_aggregate_maps`` 对旧 store（无 include_archived 形参）的兼容回落；
5. 验收链：归档一条有未读的会话 → 主徽标即时减掉、归档聚合即时出现；
   读掉 → 归档聚合消失。非归档未读计数全程与现状一致（回归）。
"""

from __future__ import annotations

import pytest

from src.inbox.store import InboxConversation, InboxMessage, InboxStore
from src.web.routes.unified_inbox_read_routes import (
    _archived_unread_map,
    _badge_include_archived,
    _unread_aggregate_maps,
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


def _msg(cid: str, mid: str, direction: str, ts: float) -> InboxMessage:
    return InboxMessage(
        conversation_id=cid, platform_msg_id=mid, direction=direction,
        text="hello", ts=ts,
    )


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    s.close()


# ── 1/2/3. store 聚合口径 ─────────────────────────────────────────────


def test_badge_excludes_archived_and_knob_restores_legacy(store):
    store.ingest_batch(_conv("telegram:acc:11", unread=2, last_ts=100),
                       [_msg("telegram:acc:11", "m1", "in", 100)])
    store.ingest_batch(_conv("telegram:acc:22", unread=3, last_ts=90),
                       [_msg("telegram:acc:22", "m2", "in", 90)])
    store.set_conv_archived("telegram:acc:22", True)

    # 默认口径：归档未读不进主徽标（列表默认视图同口径）
    assert store.sum_effective_unread_by_account()[("telegram", "acc")] == 2
    # 显式回退：inbox.badge.include_archived=true = #120 之前的全库口径
    assert store.sum_effective_unread_by_account(
        include_archived=True)[("telegram", "acc")] == 5


def test_archived_unread_aggregate_lifecycle(store):
    """验收链：归档 → 出现在归档聚合；读掉 → 消失；解档 → 回主徽标。"""
    store.ingest_batch(_conv("telegram:acc:31", unread=2, last_ts=100),
                       [_msg("telegram:acc:31", "a1", "in", 100)])
    store.ingest_batch(_conv("telegram:acc:32", unread=1, last_ts=95),
                       [_msg("telegram:acc:32", "a2", "in", 95)])
    assert store.sum_archived_unread_by_account() == {}

    store.set_conv_archived("telegram:acc:31", True)
    agg = store.sum_archived_unread_by_account()
    assert agg[("telegram", "acc")] == {"convs": 1, "unread": 2}
    # 主徽标同拍减掉（两本账互补，合计恒等于全集）
    assert store.sum_effective_unread_by_account()[("telegram", "acc")] == 1

    # 在归档视图里读掉 → 归档聚合消退（HAVING n>0 桶整个消失）
    store.mark_conversation_read("telegram:acc:31")
    assert store.sum_archived_unread_by_account() == {}
    # 回归：非归档未读计数不受影响
    assert store.sum_effective_unread_by_account()[("telegram", "acc")] == 1

    # 解档一条仍有未读的 → 从归档聚合迁回主徽标
    store.ingest_batch(_conv("telegram:acc:33", unread=4, last_ts=80),
                       [_msg("telegram:acc:33", "a3", "in", 80)])
    store.set_conv_archived("telegram:acc:33", True)
    assert store.sum_archived_unread_by_account()[
        ("telegram", "acc")] == {"convs": 1, "unread": 4}
    store.set_conv_archived("telegram:acc:33", False)
    assert store.sum_archived_unread_by_account() == {}
    assert store.sum_effective_unread_by_account()[("telegram", "acc")] == 5


def test_archived_aggregate_visible_private_caliber(store):
    """归档聚合与主徽标同一「用户可见真未读」白名单：群/legacy 群不计；
    已读水位覆盖（有效未读=0）的归档会话不计（不是显示问题的不报）。"""
    # 归档的真群 → 不计（群未读有群组动态区自己的归宿）
    g = _conv("telegram:acc:41", unread=5, last_ts=100)
    g.chat_type = "group"
    store.ingest_batch(g, [_msg("telegram:acc:41", "g1", "in", 100)])
    store.set_conv_archived("telegram:acc:41", True)
    # 归档的 legacy 负 id TG 群（chat_type 仍 'private'）→ 启发式排除
    store.ingest_batch(
        _conv("telegram:acc:-100888", unread=7, last_ts=100,
              chat_key="-100888"),
        [_msg("telegram:acc:-100888", "lg1", "in", 100)])
    store.set_conv_archived("telegram:acc:-100888", True)
    # 归档但已读净（unread>0 残值、水位已覆盖）→ 有效未读 0，不计
    store.ingest_batch(_conv("telegram:acc:42", unread=3, last_ts=100),
                       [_msg("telegram:acc:42", "r1", "in", 100)])
    store.mark_conversation_read("telegram:acc:42")
    store.ingest_batch(_conv("telegram:acc:42", unread=3, last_ts=200),
                       [_msg("telegram:acc:42", "r2", "out", 200)])
    store.set_conv_archived("telegram:acc:42", True)

    assert store.sum_archived_unread_by_account() == {}


# ── 4. 路由层 ────────────────────────────────────────────────────────


class _CfgMgr:
    def __init__(self, config):
        self.config = config


def test_badge_include_archived_config_parse():
    assert _badge_include_archived(None) is False
    assert _badge_include_archived(_CfgMgr({})) is False
    assert _badge_include_archived(
        _CfgMgr({"inbox": {"badge": {}}})) is False
    assert _badge_include_archived(
        _CfgMgr({"inbox": {"badge": {"include_archived": True}}})) is True


def test_archived_unread_map_shape(store):
    store.ingest_batch(_conv("telegram:acc:51", unread=2, last_ts=100),
                       [_msg("telegram:acc:51", "m1", "in", 100)])
    store.set_conv_archived("telegram:acc:51", True)
    assert _archived_unread_map(store) == {
        "telegram:acc": {"convs": 1, "unread": 2}}
    assert _archived_unread_map(None) == {}
    # 旧 store（无该方法）→ {}（前端回落窗口推导）
    assert _archived_unread_map(object()) == {}


def test_unread_aggregate_maps_knob_and_legacy_store(store):
    store.ingest_batch(_conv("telegram:acc:61", unread=2, last_ts=100),
                       [_msg("telegram:acc:61", "m1", "in", 100)])
    store.ingest_batch(_conv("telegram:acc:62", unread=3, last_ts=90),
                       [_msg("telegram:acc:62", "m2", "in", 90)])
    store.set_conv_archived("telegram:acc:62", True)

    by_acct, by_plat = _unread_aggregate_maps(store)
    assert by_acct == {"telegram:acc": 2}
    assert by_plat == {"telegram": 2}
    by_acct, by_plat = _unread_aggregate_maps(store, include_archived=True)
    assert by_acct == {"telegram:acc": 5}
    assert by_plat == {"telegram": 5}

    class _LegacyStore:
        """旧版 store：sum_effective_unread_by_account 无 include_archived 形参。"""

        def sum_effective_unread_by_account(self):
            return {("telegram", "acc"): 9}

    by_acct, by_plat = _unread_aggregate_maps(
        _LegacyStore(), include_archived=True)
    assert by_acct == {"telegram:acc": 9}
