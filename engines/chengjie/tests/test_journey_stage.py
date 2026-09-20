# -*- coding: utf-8 -*-
"""实施92 P0-2/P0-3 门禁：客户旅程阶段脊柱 + 成交事件。

不变量：
- 推导是**前进棘轮**：自动只升不降；deal/repeat 绝不由推导给出；
- 人工设置优先：src=manual 后自动不覆盖同级/降级；且 manual 之后的报价
  关键词只认设置时刻**之后**的新消息（防「刚降级就被旧消息顶回」）；
- 成交事件：首单 deal、再单 repeat；撤销按事件 prev_stage 还原；
- enabled 只闸扫描，成交记账/手动设置不受闸。
"""

import time

from src.inbox.journey_stage import (
    DEFAULT_QUOTE_KEYWORDS,
    STAGE_ORDER,
    backfill_stages,
    derive_stage,
    record_deal,
    resolve_journey_cfg,
    revoke_deal,
    scan_and_update,
    set_stage_manual,
    stage_rank,
)
from src.inbox.store import InboxStore

_CFG_ON = {"inbox": {"workflows": {"journey": {"enabled": True}}}}


def _store(tmp_path):
    return InboxStore(tmp_path / "inbox.db")


def _seed_conv(store, cid, *, chat_type="private", last_ts=None):
    with store._lock:
        store._conn.execute(
            """INSERT OR IGNORE INTO conversations
               (conversation_id, platform, account_id, chat_key, chat_type,
                last_ts, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (cid, "telegram", "a1", cid.split(":")[-1], chat_type,
             float(last_ts or time.time()), time.time(), time.time()),
        )
        store._conn.commit()


def _add_msg(store, cid, direction, text, ts):
    with store._lock:
        store._conn.execute(
            """INSERT INTO messages
               (message_id, conversation_id, direction, text, ts, ingested_at)
               VALUES (?,?,?,?,?,?)""",
            (f"{cid}:{direction}:{ts}", cid, direction, text, float(ts),
             float(ts)),
        )
        store._conn.execute(
            "UPDATE conversations SET last_ts = ? WHERE conversation_id = ?",
            (float(ts), cid))
        store._conn.commit()


# ── 配置解析 ────────────────────────────────────────────────────────────────

def test_cfg_defaults_off_and_conservative():
    cfg = resolve_journey_cfg({})
    assert cfg["enabled"] is False
    assert cfg["nurturing_min_days"] == 2
    assert cfg["quote_keywords"] == list(DEFAULT_QUOTE_KEYWORDS)


def test_cfg_overrides_and_bad_input():
    cfg = resolve_journey_cfg({"inbox": {"workflows": {"journey": {
        "enabled": True, "quote_keywords": ["套餐A", " "], "scan_limit": 9999,
    }}}})
    assert cfg["enabled"] is True
    assert cfg["quote_keywords"] == ["套餐A"]
    assert cfg["scan_limit"] == 500          # 上限夹紧
    assert resolve_journey_cfg(None)["enabled"] is False
    assert resolve_journey_cfg({"inbox": {"workflows": {"journey": 3}}})[
        "enabled"] is False


# ── derive_stage 纯规则 ─────────────────────────────────────────────────────

def _cfg():
    return resolve_journey_cfg(_CFG_ON)


def test_derive_new_when_one_sided():
    assert derive_stage("", {"in_n": 3, "out_n": 0, "active_days": 2},
                        quote_hit=False, cfg=_cfg()) == "new"


def test_derive_contacted_on_mutual():
    assert derive_stage("", {"in_n": 1, "out_n": 1, "active_days": 1},
                        quote_hit=False, cfg=_cfg()) == "contacted"


def test_derive_nurturing_needs_days_and_msgs():
    st = {"in_n": 3, "out_n": 3, "active_days": 2}
    assert derive_stage("", st, quote_hit=False, cfg=_cfg()) == "nurturing"
    # 条数不够 → 仍 contacted
    st2 = {"in_n": 2, "out_n": 2, "active_days": 2}
    assert derive_stage("", st2, quote_hit=False, cfg=_cfg()) == "contacted"


def test_derive_quoting_requires_mutual():
    assert derive_stage("", {"in_n": 2, "out_n": 2, "active_days": 1},
                        quote_hit=True, cfg=_cfg()) == "quoting"
    # 单向会话就算命中关键词也不进报价（还没建联谈什么报价）
    assert derive_stage("", {"in_n": 2, "out_n": 0, "active_days": 1},
                        quote_hit=True, cfg=_cfg()) == "new"


def test_derive_ratchet_never_downgrades():
    assert derive_stage("quoting", {"in_n": 1, "out_n": 1, "active_days": 1},
                        quote_hit=False, cfg=_cfg()) == "quoting"


def test_derive_never_yields_deal():
    got = derive_stage("deal", {"in_n": 99, "out_n": 99, "active_days": 30},
                       quote_hit=True, cfg=_cfg())
    assert got == "deal"
    assert stage_rank("repeat") > stage_rank("deal") > stage_rank("quoting")


# ── 扫描与落库 ──────────────────────────────────────────────────────────────

def test_scan_disabled_is_noop(tmp_path):
    store = _store(tmp_path)
    _seed_conv(store, "tg:a1:u1")
    _add_msg(store, "tg:a1:u1", "in", "hi", time.time())
    assert scan_and_update(store, {}, {}) == []
    assert store.get_journey_stage("tg:a1:u1")["stage"] == ""


def test_scan_advances_contacted_and_quoting(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    _seed_conv(store, "tg:a1:u2")
    _add_msg(store, "tg:a1:u2", "in", "你好", now - 100)
    _add_msg(store, "tg:a1:u2", "out", "你好呀", now - 90)
    state = {}
    trans = scan_and_update(store, _CFG_ON, state, now=now)
    assert [t["to"] for t in trans] == ["contacted"]
    assert store.get_journey_stage("tg:a1:u2")["stage"] == "contacted"
    # 出现报价关键词 → 下轮扫描升 quoting（增量扫描按新消息驱动）
    _add_msg(store, "tg:a1:u2", "in", "这个套餐多少钱？", now + 5)
    trans2 = scan_and_update(store, _CFG_ON, state, now=now + 10)
    assert [t["to"] for t in trans2] == ["quoting"]
    src = store.get_journey_stage("tg:a1:u2")
    assert src["stage"] == "quoting" and src["src"] == "auto"


def test_scan_skips_group_chats(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    _seed_conv(store, "tg:a1:g1", chat_type="group")
    _add_msg(store, "tg:a1:g1", "in", "hi", now - 10)
    _add_msg(store, "tg:a1:g1", "out", "hey", now - 5)
    assert scan_and_update(store, _CFG_ON, {}, now=now) == []


def test_manual_downgrade_not_repromoted_by_old_quote(tmp_path):
    """坐席把误判的 quoting 降回 nurturing 后，旧报价消息不得把它顶回去；
    但**新**报价消息可以再升。"""
    store = _store(tmp_path)
    now = time.time()
    cid = "tg:a1:u3"
    _seed_conv(store, cid)
    for i in range(4):
        _add_msg(store, cid, "in", f"msg{i}", now - 86400 * 2 - i)
        _add_msg(store, cid, "out", f"re{i}", now - 86400 * 2 - i + 0.5)
    _add_msg(store, cid, "in", "报价发我看看", now - 3600)
    state = {}
    scan_and_update(store, _CFG_ON, state, now=now)
    assert store.get_journey_stage(cid)["stage"] == "quoting"
    # 人工降级
    res = set_stage_manual(store, cid, "nurturing", by="agent1")
    assert res["ok"] and store.get_journey_stage(cid)["src"] == "manual"
    # 旧报价消息仍在窗内，但 manual 之后只认新证据 → 不回升
    _add_msg(store, cid, "in", "哈哈好的", now + 5)
    scan_and_update(store, _CFG_ON, state, now=now + 10)
    assert store.get_journey_stage(cid)["stage"] == "nurturing"
    # 新报价消息 → 再升
    _add_msg(store, cid, "in", "那价格能优惠吗", now + 20)
    scan_and_update(store, _CFG_ON, state, now=now + 30)
    assert store.get_journey_stage(cid)["stage"] == "quoting"


def test_set_stage_manual_rejects_bad_stage(tmp_path):
    store = _store(tmp_path)
    assert set_stage_manual(store, "tg:a1:x", "vip")["ok"] is False
    assert set_stage_manual(store, "tg:a1:x", "QUOTING")["ok"] is True


def test_backfill_covers_stock_conversations(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    old = now - 30 * 86400   # 超出增量扫描 bootstrap 窗的存量会话
    _seed_conv(store, "tg:a1:old1", last_ts=old)
    _add_msg(store, "tg:a1:old1", "in", "hello", old - 100)
    _add_msg(store, "tg:a1:old1", "out", "hi", old - 90)
    assert backfill_stages(store, _CFG_ON) == 1
    assert store.get_journey_stage("tg:a1:old1")["stage"] == "contacted"


# ── 成交事件 ────────────────────────────────────────────────────────────────

def test_deal_first_then_repeat_and_ledger(tmp_path):
    store = _store(tmp_path)
    cid = "tg:a1:d1"
    _seed_conv(store, cid)
    store.set_journey_stage(cid, "quoting", src="auto")
    r1 = record_deal(store, cid, amount=99.5, currency="USD",
                     note="首单", recorded_by="agent1")
    assert r1["ok"] and r1["stage"] == "deal"
    meta = store.get_journey_stage(cid)
    assert meta["stage"] == "deal" and meta["src"] == "deal"
    r2 = record_deal(store, cid, amount=50)
    assert r2["stage"] == "repeat"
    deals = store.list_deal_events(cid)
    assert len(deals) == 2
    assert deals[-1]["prev_stage"] == "quoting"   # 首单记录了成交前阶段


def test_deal_works_even_when_journey_disabled(tmp_path):
    store = _store(tmp_path)
    cid = "tg:a1:d2"
    _seed_conv(store, cid)
    assert record_deal(store, cid, amount=10)["ok"] is True
    assert store.get_journey_stage(cid)["stage"] == "deal"


def test_revoke_restores_prev_stage(tmp_path):
    store = _store(tmp_path)
    cid = "tg:a1:d3"
    _seed_conv(store, cid)
    store.set_journey_stage(cid, "quoting", src="auto")
    r1 = record_deal(store, cid, amount=100)
    res = revoke_deal(store, cid, r1["deal_id"])
    assert res["ok"] and res["remaining"] == 0
    assert store.get_journey_stage(cid)["stage"] == "quoting"


def test_revoke_one_of_two_downgrades_repeat_to_deal(tmp_path):
    store = _store(tmp_path)
    cid = "tg:a1:d4"
    _seed_conv(store, cid)
    record_deal(store, cid, amount=100)
    r2 = record_deal(store, cid, amount=200)
    res = revoke_deal(store, cid, r2["deal_id"])
    assert res["ok"] and res["remaining"] == 1
    assert store.get_journey_stage(cid)["stage"] == "deal"


def test_revoke_guards(tmp_path):
    store = _store(tmp_path)
    cid = "tg:a1:d5"
    _seed_conv(store, cid)
    r1 = record_deal(store, cid, amount=10)
    assert revoke_deal(store, "tg:a1:other", r1["deal_id"])["error"] == "not_found"
    assert revoke_deal(store, cid, 99999)["error"] == "not_found"
    assert revoke_deal(store, cid, r1["deal_id"])["ok"] is True
    assert revoke_deal(store, cid, r1["deal_id"])["error"] == "already_revoked"


def test_stage_order_contract():
    """阶段全集与顺序被多处消费（前端下拉/推导/挂链），钉住防漂移。"""
    assert STAGE_ORDER == ["new", "contacted", "nurturing", "quoting",
                           "deal", "repeat"]
