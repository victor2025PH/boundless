# -*- coding: utf-8 -*-
"""P0-4 入站结局台账（#279 / #44 / #68「消息发了但收件箱没有」）。

不变量：
  ① 每条走 ``ingest_incoming`` 的消息都留下一个结局（inserted / duplicate / tombstone /
     no_content / no_store / no_chat_key / store_error），可按会话或 chat_key 追查；
  ② 台账纯观测——落库行为（去重、墓碑、回填静默、Bad MAC 占位）一律不变；
  ③ 落库异常不再只有 debug 日志：台账记 store_error，返回值不再伪装成功；
  ④ 实时入站缺 ts 补当前时间（避免 1970 沉底 + 被墓碑闸当历史丢弃），回填路径不补。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from src.inbox import inbound_ledger as L
from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import ingest_incoming

_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _reset_ledger():
    L.reset_for_tests()
    yield
    L.reset_for_tests()


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "ledger.db")
    try:
        yield s
    finally:
        try:
            s.close()
        except Exception:
            pass


def _in(store, text="hi", msg_id="m1", ts=None, chat_key="777", **kw):
    return ingest_incoming(
        store, platform="telegram", account_id="acct1", chat_key=chat_key,
        name="Bob", text=text, ts=(time.time() if ts is None else ts),
        msg_id=msg_id, **kw,
    )


def _outcomes(**flt):
    return [r["outcome"] for r in L.recent(200, **flt)]


def test_inserted_then_duplicate_are_distinguished(store):
    cid = _in(store, msg_id="m1")
    assert cid == "telegram:acct1:777"
    cid2 = _in(store, msg_id="m1")
    assert cid2 == cid
    oc = _outcomes(conversation_id=cid)
    assert oc == ["duplicate", "inserted"], oc  # 新→旧
    st = L.dump_stats()
    assert st["inserted"] == 1 and st["duplicate"] == 1
    assert st["dropped_total"] == 0  # 重投去重不是丢
    # 落库行为不变：仅一条消息
    assert len(store.list_messages(cid, limit=10)) == 1


def test_no_store_and_no_chat_key_recorded():
    assert ingest_incoming(None, platform="telegram", account_id="a", chat_key="1", text="x") is None
    assert _outcomes() == ["no_store"]


def test_no_chat_key_recorded(store):
    assert _in(store, chat_key="") is None
    assert _outcomes() == ["no_chat_key"]


def test_empty_payload_recorded_as_no_content(store):
    cid = _in(store, text="", msg_id="m-empty")
    assert cid == "telegram:acct1:777"
    assert _outcomes(conversation_id=cid) == ["no_content"]
    assert store.list_messages(cid, limit=10) == []
    assert L.dump_stats()["dropped_total"] == 1


def test_tombstone_drop_is_named_not_silent(store):
    cid = _in(store, msg_id="m1", ts=1000.0)
    store.delete_conversation_data(cid, deleted_by="boss")
    L.reset_for_tests()
    # 历史重放（ts 不晚于删除时刻）→ 按墓碑丢弃，且台账明说
    _in(store, msg_id="m0", ts=999.0, backfill=True, backfill_source="history_sync")
    assert _outcomes(conversation_id=cid) == ["tombstone"]
    assert "deleted_at=" in L.recent(1)[0]["detail"]
    # 真实新消息解除墓碑并落库
    _in(store, msg_id="m2", ts=time.time() + 5)
    assert _outcomes(conversation_id=cid)[0] == "inserted"
    assert not store.is_conversation_tombstoned(cid)


def test_realtime_inbound_without_ts_is_defaulted_backfill_is_not(store):
    before = time.time()
    cid = _in(store, msg_id="m-nots", ts=0)
    rows = store.list_messages(cid, limit=10)
    assert len(rows) == 1
    ts = float(rows[0].get("ts") if isinstance(rows[0], dict) else rows[0].ts)
    assert ts >= before - 1
    assert "ts_defaulted" in _outcomes(chat_key="777")
    # 回填缺 ts 保持原样（历史同步语义由上游决定）
    _in(store, msg_id="m-bf", ts=0, text="old", backfill=True, backfill_source="history_sync")
    rows = store.list_messages(cid, limit=10)
    assert any(float(r.get("ts") if isinstance(r, dict) else r.ts) == 0 for r in rows)


def test_store_error_recorded_and_not_masked(store, caplog, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(store, "ingest_batch", _boom)
    with caplog.at_level(logging.WARNING, logger="src.inbox.inbound_ledger"):
        cid = _in(store, msg_id="m-err")
    assert cid is not None  # 返回口径不变（调用方靠 conv id 关联）
    assert _outcomes() == ["store_error"]
    assert "RuntimeError: disk full" in L.recent(1)[0]["detail"]
    assert any("outcome=store_error" in r.getMessage() for r in caplog.records)


def test_ledger_log_line_carries_conv_account_chat_key(store, caplog):
    with caplog.at_level(logging.INFO, logger="src.inbox.inbound_ledger"):
        _in(store, text="", msg_id="m-empty")
    lines = [r.getMessage() for r in caplog.records if "[inbound] outcome=" in r.getMessage()]
    assert lines, caplog.records
    ln = lines[0]
    assert "outcome=no_content" in ln
    assert "conv=telegram:acct1:777" in ln and "account=acct1" in ln and "chat_key=777" in ln
    assert "msg_id=m-empty" in ln


def test_recent_filters_and_drops_only(store):
    _in(store, msg_id="a1", chat_key="1")
    _in(store, msg_id="a1", chat_key="1")          # duplicate
    _in(store, text="", msg_id="b1", chat_key="2")  # no_content
    assert _outcomes(chat_key="1") == ["duplicate", "inserted"]
    assert _outcomes(chat_key="2") == ["no_content"]
    assert _outcomes(drops_only=True) == ["no_content"]
    assert len(L.recent(1)) == 1


def test_ledger_never_raises_on_bad_input():
    L.record("weird", ts="not-a-number")
    L.record("", platform=None)
    assert L.dump_stats().get("weird") == 1
    assert L.dump_stats().get("unknown") == 1


def test_ops_route_registered_and_wired():
    admin = (_ROOT / "src/web/admin.py").read_text(encoding="utf-8")
    assert "register_ops_inbound_ledger_routes(app, _admin_ctx)" in admin
    route = (_ROOT / "src/web/routes/ops_inbound_ledger_routes.py").read_text(encoding="utf-8")
    assert '"/api/ops/inbound-ledger"' in route
    bridge = (_ROOT / "src/integrations/protocol_bridge.py").read_text(encoding="utf-8")
    assert 'logger.debug("[protocol_bridge] ingest_collected_chats 失败"' in bridge
    assert '_ledger("store_error"' in bridge
