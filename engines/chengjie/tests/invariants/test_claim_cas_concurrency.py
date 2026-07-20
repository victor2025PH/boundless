"""消息不变量 · 会话租约锁并发正确性（CAS）。

修复前 `set_conversation_claim` 的「检查在锁外、写入无条件覆盖」构成 TOCTOU：
多坐席并发抢同一会话会双双 ok:True → 重复回复/串号。本测试固定：
  1) N 线程并发抢同一 fresh 会话 → 恰好 1 人 ok:True；
  2) 同一坐席重复抢 → 续租成功；
  3) 已被他人认领（未过期、非 force）→ already_claimed；
  4) force=True → 抢占成功；
  5) 租约过期 → 他人可接管。
"""
import threading

import pytest

from src.inbox.store import InboxStore


@pytest.fixture()
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    yield s


def test_concurrent_claim_exactly_one_winner(store):
    cid = "tg:acc:conv-concurrency"
    n = 24
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()  # 尽量对齐起跑线，制造真并发
        results[i] = store.set_conversation_claim(cid, f"agent-{i}", agent_name=f"A{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [r for r in results if r and r.get("ok") is True]
    losers = [r for r in results if r and r.get("ok") is False]
    assert len(winners) == 1, f"并发抢占应恰好 1 人成功，实得 {len(winners)}"
    assert all(r.get("reason") == "already_claimed" for r in losers), losers
    # DB 里最终 owner 必须就是那个唯一赢家
    final = store.get_conversation_claim(cid)
    assert final and final.get("agent_id") == winners[0]["claim"]["agent_id"]


def test_same_agent_renew_ok(store):
    cid = "tg:acc:renew"
    r1 = store.set_conversation_claim(cid, "agent-1", agent_name="A1", ttl_sec=120)
    r2 = store.set_conversation_claim(cid, "agent-1", agent_name="A1", ttl_sec=120)
    assert r1["ok"] is True and r2["ok"] is True
    assert store.get_conversation_claim(cid)["agent_id"] == "agent-1"


def test_other_agent_rejected_without_force(store):
    cid = "tg:acc:contended"
    store.set_conversation_claim(cid, "agent-1", agent_name="A1", ttl_sec=600)
    r = store.set_conversation_claim(cid, "agent-2", agent_name="A2", ttl_sec=600)
    assert r["ok"] is False and r["reason"] == "already_claimed"
    assert store.get_conversation_claim(cid)["agent_id"] == "agent-1"


def test_force_takes_over(store):
    cid = "tg:acc:forced"
    store.set_conversation_claim(cid, "agent-1", agent_name="A1", ttl_sec=600)
    r = store.set_conversation_claim(cid, "agent-2", agent_name="A2", ttl_sec=600, force=True)
    assert r["ok"] is True
    assert store.get_conversation_claim(cid)["agent_id"] == "agent-2"


def test_expired_claim_can_be_taken_over(store):
    cid = "tg:acc:expired"
    store.set_conversation_claim(cid, "agent-1", agent_name="A1", ttl_sec=600)
    # 白盒：把 agent-1 的租约到期时间改到过去，模拟过期。
    with store._lock:
        store._conn.execute(
            "UPDATE conversation_claims SET expires_at = 1 WHERE conversation_id = ?",
            (cid,),
        )
        store._conn.commit()
    r = store.set_conversation_claim(cid, "agent-2", agent_name="A2", ttl_sec=600)
    assert r["ok"] is True, "过期租约应允许他人接管"
    assert store.get_conversation_claim(cid)["agent_id"] == "agent-2"
