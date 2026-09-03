# -*- coding: utf-8 -*-
"""#159 账号栏幽灵未读——徽标口径与清单对齐（2026-09-03 证据包 G4BYWH）。

事故：skuio 机 steven 号（tg 7092595256）头像、侧栏、会话入口三处都显示 7 条
待处理，清单里一条都没有。三处数字同源 ``sum_effective_unread_by_account``，
清单却在取行之后还要过一串清单专属剔除（删除墓碑 / 消息全被软删 / 协议号纯
占位残值）——徽标少了这几道就成了幽灵数字。

本文件钉住的不变量只有一句：**账号栏的数字 == 点开能看到的条数**。任何一条
被清单藏起来的会话都不许进徽标；反过来，能出现在清单里的未读一条都不许漏。
"""
from __future__ import annotations

import pytest

from src.inbox.store import InboxConversation, InboxMessage, InboxStore
from src.inbox.unread_aggregate import (
    phantom_unread_report,
    unread_conversations,
    unread_maps,
)
from src.web.routes.unified_inbox_read_routes import _unread_aggregate_maps


def _conv(cid: str, *, unread: int = 0, last_ts: float = 0.0,
          platform: str = "telegram", account: str = "acc",
          chat_key: str = "", chat_type: str = "private") -> InboxConversation:
    ck = chat_key or cid.rsplit(":", 1)[-1]
    return InboxConversation(
        conversation_id=cid, platform=platform, account_id=account,
        chat_key=ck, display_name=f"客户{ck}", last_text="hi",
        last_ts=last_ts, unread=unread, chat_type=chat_type,
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


def _badge(store) -> int:
    maps = unread_maps(store)
    assert maps is not None
    return int(maps[0].get("telegram:acc") or 0)


def _listed(store) -> int:
    return sum(int(c["unread"]) for c in
               unread_conversations(store, "telegram", "acc"))


# ══ 1. G4BYWH 原形：构造 7 条残值会话 → 三处与清单一致 ═════════════════════

def test_g4bywh_seven_phantom_unread_shows_zero(store):
    """证据包形态：7 条未读全部来自「点开什么都看不到」的会话 → 徽标 0。

    两种残值凑够 7：协议号纯占位（同步了会话列表、消息没回流，G4BYWH 的
    主形态）×4，消息被对端全撤回 ×3。墓碑那条另有专测——它连 conversations
    行都被删了，本就不进任何聚合。
    """
    # ① 协议号手机端未读残值 ×4：upsert_protocol_chats 只建会话不带消息
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "ph1", "unread": 2, "ts": 100},
        {"chat_key": "ph2", "unread": 1, "ts": 101},
        {"chat_key": "ph3", "unread": 1, "ts": 102},
    ])
    # ② 消息全被软删（对端撤回）×3：库里有会话有消息行，但一条可见的都没有
    store.ingest_batch(_conv("telegram:acc:gone", unread=3, last_ts=110),
                       [_msg("telegram:acc:gone", "g1", "in", 110)])
    store.soft_delete_by_platform_msg_ids(
        "telegram", "acc", ["g1"], deleted_by="peer")

    # 旧口径正是坐席看到的那个 7
    legacy = store.sum_effective_unread_by_account()
    assert legacy.get(("telegram", "acc")) == 7

    # 新口径与清单一致：一条都点不开 → 徽标 0，桶整个消失
    maps = unread_maps(store)
    assert maps == ({}, {}), maps
    assert unread_conversations(store, "telegram", "acc") == []
    # 路由层同拍（三处徽标共同的数据源）
    assert _unread_aggregate_maps(store) == ({}, {})


def test_tombstoned_conversation_never_counts(store):
    """坐席删过的会话被目录占位复活 → 清单不显示，徽标也不许显示。"""
    store.ingest_batch(_conv("telegram:acc:dead", unread=2, last_ts=120),
                       [_msg("telegram:acc:dead", "d1", "in", 120)])
    assert _badge(store) == 2
    store.delete_conversation_data("telegram:acc:dead", deleted_by="agent")
    # 目录同步每 ~5min 全量推一次侧栏；墓碑挡住重建，聚合更不该凭空造数
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "dead", "unread": 2, "ts": 121}])
    assert unread_maps(store) == ({}, {})
    assert unread_conversations(store, "telegram", "acc") == []


def test_badge_equals_drilldown_when_real_unread_present(store):
    """真未读与残值混在一起：徽标只数真的，且与点开的明细逐条恒等。"""
    store.ingest_batch(_conv("telegram:acc:real1", unread=2, last_ts=200),
                       [_msg("telegram:acc:real1", "r1", "in", 200)])
    store.ingest_batch(_conv("telegram:acc:real2", unread=1, last_ts=190),
                       [_msg("telegram:acc:real2", "r2", "in", 190)])
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "ghost", "unread": 4, "ts": 180}])   # 残值，不该计

    assert _badge(store) == 3
    convs = unread_conversations(store, "telegram", "acc")
    assert [c["chat_key"] for c in convs] == ["real1", "real2"]  # 未读降序
    assert _listed(store) == _badge(store) == 3
    # 数字可点直达：明细带够前端跳转所需的定位键
    assert convs[0]["conversation_id"] == "telegram:acc:real1"
    assert convs[0]["platform"] == "telegram" and convs[0]["account_id"] == "acc"


# ══ 2. 与既有口径的一致性（#120 归档 / 私聊白名单 / 已读水位）═════════════

def test_archived_excluded_by_default_and_knob_restores(store):
    store.ingest_batch(_conv("telegram:acc:a1", unread=2, last_ts=100),
                       [_msg("telegram:acc:a1", "m1", "in", 100)])
    store.ingest_batch(_conv("telegram:acc:a2", unread=3, last_ts=90),
                       [_msg("telegram:acc:a2", "m2", "in", 90)])
    store.set_conv_archived("telegram:acc:a2", True)

    assert _badge(store) == 2
    maps = unread_maps(store, include_archived=True)
    assert maps[0] == {"telegram:acc": 5}
    # 明细同一开关（点开归档入口时看到的与那枚数字一致）
    assert len(unread_conversations(store, "telegram", "acc")) == 1
    assert len(unread_conversations(
        store, "telegram", "acc", include_archived=True)) == 2


def test_groups_and_read_watermark_excluded(store):
    """群/legacy 群不进主徽标；已读水位覆盖的残值也不进（口径与 store 同源）。"""
    g = _conv("telegram:acc:g1", unread=5, last_ts=100, chat_type="group")
    store.ingest_batch(g, [_msg("telegram:acc:g1", "gm", "in", 100)])
    store.ingest_batch(
        _conv("telegram:acc:-100888", unread=7, last_ts=100,
              chat_key="-100888"),
        [_msg("telegram:acc:-100888", "lg", "in", 100)])
    # 读过之后自己又回了一句：last_ts 前进但 last_in_ts 没有 → 不复活
    store.ingest_batch(_conv("telegram:acc:read", unread=3, last_ts=100),
                       [_msg("telegram:acc:read", "i1", "in", 100)])
    store.mark_conversation_read("telegram:acc:read")
    store.ingest_batch(_conv("telegram:acc:read", unread=3, last_ts=200),
                       [_msg("telegram:acc:read", "o1", "out", 200)])

    assert unread_maps(store) == ({}, {})


def test_partial_soft_delete_still_counts(store):
    """只删掉一条、还剩可见消息 → 会话仍在清单里，未读照常计（不误杀）。"""
    store.ingest_batch(_conv("telegram:acc:p1", unread=2, last_ts=100), [
        _msg("telegram:acc:p1", "k1", "in", 99),
        _msg("telegram:acc:p1", "k2", "in", 100),
    ])
    store.soft_delete_by_platform_msg_ids(
        "telegram", "acc", ["k2"], deleted_by="peer")
    assert _badge(store) == 2
    assert _listed(store) == 2


def test_tombstone_release_on_new_message_restores_badge(store):
    """客户再开口 → 墓碑自动解除、会话回清单 → 徽标同拍回来（绝不丢客户消息）。"""
    import time as _t

    t0 = _t.time()
    store.ingest_batch(_conv("telegram:acc:t1", unread=1, last_ts=t0),
                       [_msg("telegram:acc:t1", "x1", "in", t0)])
    store.delete_conversation_data("telegram:acc:t1", deleted_by="agent")
    assert unread_maps(store) == ({}, {})

    # 墓碑闸按「消息 ts 晚于删除时刻」判真实新消息 → 用真实时钟之后的 ts
    t1 = _t.time() + 60
    store.ingest_batch(_conv("telegram:acc:t1", unread=1, last_ts=t1),
                       [_msg("telegram:acc:t1", "x2", "in", t1)])
    assert _badge(store) == 1
    assert _listed(store) == 1


# ══ 3. 平台/账号维度与容错 ═════════════════════════════════════════════════

def test_by_platform_sums_accounts(store):
    for acct, n in (("acc", 2), ("acc2", 3)):
        cid = f"telegram:{acct}:c"
        store.ingest_batch(
            _conv(cid, unread=n, last_ts=100, account=acct),
            [_msg(cid, f"m-{acct}", "in", 100)])
    cid_w = "whatsapp:wa1:c"
    store.ingest_batch(
        _conv(cid_w, unread=4, last_ts=100, platform="whatsapp", account="wa1"),
        [_msg(cid_w, "mw", "in", 100)])

    by_acct, by_plat = unread_maps(store)
    assert by_acct == {"telegram:acc": 2, "telegram:acc2": 3, "whatsapp:wa1": 4}
    assert by_plat == {"telegram": 5, "whatsapp": 4}
    # account_id 留空＝该平台全部账号（账号栏「全部」视角）
    assert len(unread_conversations(store, "telegram", "")) == 2


def test_none_means_failure_not_zero():
    """空 dict（真的没未读）与 None（聚合没跑成）必须可区分——把前者当失败
    会让路由回落旧口径，#159 的幽灵数字原样复活。"""
    assert unread_maps(None) is None

    class _Broken:
        _lock = __import__("threading").Lock()

        class _Conn:
            @staticmethod
            def execute(*a, **k):
                raise RuntimeError("no such table")

        _conn = _Conn()

    assert unread_maps(_Broken()) is None
    assert unread_conversations(_Broken(), "telegram", "acc") == []
    # 路由层据此回落旧聚合（而非把徽标抹成 0）
    class _Legacy(_Broken):
        @staticmethod
        def sum_effective_unread_by_account(**k):
            return {("telegram", "acc"): 9}

    assert _unread_aggregate_maps(_Legacy()) == (
        {"telegram:acc": 9}, {"telegram": 9})


def test_phantom_report_quantifies_the_gap(store):
    """值守可观测：差额＝被新闸门剔掉的幽灵数，不必等客户截图。"""
    store.ingest_batch(_conv("telegram:acc:ok", unread=2, last_ts=100),
                       [_msg("telegram:acc:ok", "m", "in", 100)])
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "ghost", "unread": 7, "ts": 100}])

    rep = phantom_unread_report(store)
    assert rep["badge"] == 2 and rep["store"] == 9
    assert rep["phantom"] == 7
    assert rep["by_account"] == {"telegram:acc": 7}
    assert phantom_unread_report(None)["phantom"] == 0


# ══ 4. 路由层：数字可点直达 + 一键清零 ═════════════════════════════════════

def _client(store):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.unified_inbox_read_routes import register_read_routes

    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_read_routes(app, api_auth=api_auth, config_manager=None)
    app.state.inbox_store = store
    return TestClient(app)


def test_account_unread_endpoint_matches_badge(store):
    """点开的条数与徽标恒等——两者共用同一份 WHERE，对不上就是真 bug。"""
    store.ingest_batch(_conv("telegram:acc:real", unread=3, last_ts=200),
                       [_msg("telegram:acc:real", "r", "in", 200)])
    store.upsert_protocol_chats("telegram", "acc", [
        {"chat_key": "ghost", "unread": 7, "ts": 190}])

    c = _client(store)
    d = c.get("/api/unified-inbox/account-unread",
              params={"platform": "telegram", "account_id": "acc"}).json()
    assert d["ok"] is True
    assert d["count"] == 1 and d["unread_total"] == 3
    assert d["conversations"][0]["chat_key"] == "real"
    assert d["unread_total"] == _badge(store)
    # 幽灵那条既不进徽标，也不该在直达列表里冒出来
    assert all(x["chat_key"] != "ghost" for x in d["conversations"])


def test_account_unread_requires_platform(store):
    c = _client(store)
    assert c.get("/api/unified-inbox/account-unread",
                 params={"platform": ""}).status_code == 400


def test_mark_account_read_clears_badge_and_drilldown(store):
    """一键清零：水位推到位后徽标与直达列表同拍归零（永不回弹）。"""
    store.ingest_batch(_conv("telegram:acc:c1", unread=2, last_ts=100),
                       [_msg("telegram:acc:c1", "m1", "in", 100)])
    store.ingest_batch(_conv("telegram:acc:c2", unread=1, last_ts=90),
                       [_msg("telegram:acc:c2", "m2", "in", 90)])
    c = _client(store)
    assert c.get("/api/unified-inbox/account-unread",
                 params={"platform": "telegram",
                         "account_id": "acc"}).json()["unread_total"] == 3

    r = c.post("/api/unified-inbox/mark-account-read",
               json={"platform": "telegram", "account_id": "acc"}).json()
    assert r["ok"] is True and r["marked"] == 2
    assert unread_maps(store) == ({}, {})
    assert c.get("/api/unified-inbox/account-unread",
                 params={"platform": "telegram",
                         "account_id": "acc"}).json()["count"] == 0


def test_read_only_never_writes(store):
    """聚合是只读的：跑一轮不得改动任何未读/水位（徽标绝不能偷偷清账）。"""
    store.ingest_batch(_conv("telegram:acc:w1", unread=3, last_ts=100),
                       [_msg("telegram:acc:w1", "m", "in", 100)])
    before = dict(store.get_conversation("telegram:acc:w1") or {})
    unread_maps(store)
    unread_conversations(store, "telegram", "acc")
    phantom_unread_report(store)
    after = dict(store.get_conversation("telegram:acc:w1") or {})
    assert before == after
