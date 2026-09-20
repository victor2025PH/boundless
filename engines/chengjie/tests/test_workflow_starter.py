# -*- coding: utf-8 -*-
"""B1/B2 门禁：工作链出厂模板（种子包）+ 链效果漏斗。

种子包不变量：
- 结构合法（id 唯一、步骤类型合法、话术步骤必有 note）；
- **安全默认**：种子链一律手动启动（trigger_conditions={}）——导入动作绝不能
  立刻对沉默会话批量开链；
- 幂等且不覆盖：按 chain_id 判重，运营改过的同 id 链再导入必须原样保留。

漏斗口径不变量：
- started/completed/failed/cancelled/running 按窗口内 started_at 聚合；
- 回复率分母只算已满 72h 窗口期的执行（刚启动的不计，防人为压低）；
- 回复须落在启动后 72h 窗口内（窗口外的入站不算）。
"""

import time

from src.inbox.store import InboxStore
from src.inbox.workflow_monitor import chain_funnel
from src.inbox.workflow_starter import STARTER_CHAINS, ensure_starter_chains

_VALID_TYPES = {"template", "task", "tag", "note", "escalate"}


def _store(tmp_path):
    return InboxStore(tmp_path / "wf_starter.db")


def _insert_inbound(store, conversation_id: str, ts: float):
    store._conn.execute(
        """INSERT INTO messages (message_id, conversation_id, direction, text, ts, ingested_at)
           VALUES (?, ?, 'in', 'hi', ?, ?)""",
        (f"m-{conversation_id}-{ts}", conversation_id, ts, ts),
    )
    store._conn.commit()


def _set_started_at(store, exec_id: str, ts: float):
    store._conn.execute(
        "UPDATE workflow_executions SET started_at = ? WHERE exec_id = ?", (ts, exec_id))
    store._conn.commit()


# ── 种子包结构 ───────────────────────────────────────────────────────────────

def test_starter_chains_wellformed():
    ids = [c["chain_id"] for c in STARTER_CHAINS]
    assert len(ids) == len(set(ids)), "chain_id 必须唯一"
    assert all(i.startswith("starter_") for i in ids), "种子链 id 须带 starter_ 前缀（幂等判重锚点）"
    for chain in STARTER_CHAINS:
        assert chain["name"].strip(), f"{chain['chain_id']} 缺名称"
        steps = chain["steps"]
        assert steps, f"{chain['chain_id']} 步骤为空"
        for s in steps:
            assert s["action_type"] in _VALID_TYPES, f"非法步骤类型: {s}"
            assert float(s.get("delay_hours") or 0) >= 0
            if s["action_type"] == "template":
                assert str(s.get("note") or "").strip(), f"话术步骤必须有 note: {chain['chain_id']}"
            if s["action_type"] == "tag":
                assert str(s.get("tag") or "").strip(), f"tag 步骤必须有 tag 值: {chain['chain_id']}"


def test_starter_chains_never_auto_trigger():
    """安全默认：种子链导入后绝不能被 auto_start_chains 扫中（惊吓默认值防线）。"""
    for chain in STARTER_CHAINS:
        assert not chain.get("trigger_conditions"), (
            f"{chain['chain_id']} 带了自动触发条件——种子链必须手动启动，"
            "自动触发是运营看过内容后自己开的决定")


# ── 幂等导入 ─────────────────────────────────────────────────────────────────

def test_seed_idempotent_and_never_overwrites(tmp_path):
    store = _store(tmp_path)
    r1 = ensure_starter_chains(store)
    assert sorted(r1["imported"]) == sorted(c["chain_id"] for c in STARTER_CHAINS)
    assert r1["skipped"] == []

    # 二次导入：全部跳过
    r2 = ensure_starter_chains(store)
    assert r2["imported"] == []
    assert sorted(r2["skipped"]) == sorted(c["chain_id"] for c in STARTER_CHAINS)

    # 运营改过的链再导入必须原样保留（不覆盖）
    cid = STARTER_CHAINS[0]["chain_id"]
    store.upsert_workflow_chain({
        "chain_id": cid, "name": "运营改过的名字",
        "steps": [{"action_type": "template", "note": "自定义", "delay_hours": 0}],
        "trigger_conditions": {"silence_days": 3}, "enabled": False,
    })
    ensure_starter_chains(store)
    kept = store.get_workflow_chain(cid)
    assert kept["name"] == "运营改过的名字"
    assert not kept["enabled"]


def test_seeded_chains_visible_and_startable(tmp_path):
    """种子链在链列表可见、可对会话启动（会话内选择器的数据源路径）。"""
    store = _store(tmp_path)
    ensure_starter_chains(store)
    chains = store.list_workflow_chains()
    assert len(chains) == len(STARTER_CHAINS)
    assert all(c["enabled"] for c in chains)
    eid = store.start_chain_execution(
        STARTER_CHAINS[0]["chain_id"], "tg:acc:peer1", {}, schedule_first_step=True)
    ex = store.get_workflow_execution(eid)
    assert ex["status"] == "running"


# ── 效果漏斗 ─────────────────────────────────────────────────────────────────

def test_chain_funnel_counts_and_reply_rate(tmp_path):
    store = _store(tmp_path)
    ensure_starter_chains(store)
    now = time.time()
    chain_a = STARTER_CHAINS[0]["chain_id"]
    chain_b = STARTER_CHAINS[1]["chain_id"]

    # e1: 成熟 + 窗口内回复 + completed；带 goal_id（归因组样本）
    e1 = store.start_chain_execution(chain_a, "tg:a:c1", {"goal_id": "g-1"})
    _set_started_at(store, e1, now - 100 * 3600)
    _insert_inbound(store, "tg:a:c1", now - 90 * 3600)   # 启动后 10h 回复
    store.complete_workflow_execution(e1, status="completed")

    # e2: 成熟 + 无回复 + running
    e2 = store.start_chain_execution(chain_a, "tg:a:c2", {})
    _set_started_at(store, e2, now - 100 * 3600)

    # e3: 未成熟（1h 前启动）——不进回复率分母
    store.start_chain_execution(chain_a, "tg:a:c3", {})

    # e4: 成熟 + 回复落在 72h 窗口外（不算回复）+ failed
    e4 = store.start_chain_execution(chain_b, "tg:a:c4", {})
    _set_started_at(store, e4, now - 200 * 3600)
    _insert_inbound(store, "tg:a:c4", now - 100 * 3600)  # 启动后 100h，窗口外
    store.complete_workflow_execution(e4, status="failed")

    d = chain_funnel(store, days=14, now=now)
    t = d["total"]
    assert t["started"] == 4
    assert t["completed"] == 1
    assert t["failed"] == 1
    assert t["running"] == 2
    assert t["mature_n"] == 3
    assert t["replied_n"] == 1
    assert t["reply_rate"] == round(1 / 3, 3)
    # 目标归因分段：只有 e1 带 goal_id → 归因组 1/1 回复，散链组不计入
    assert t["attributed"] == 1
    assert t["attr_mature_n"] == 1
    assert t["attr_replied_n"] == 1
    assert t["attr_reply_rate"] == 1.0

    by_id = {c["chain_id"]: c for c in d["chains"]}
    assert by_id[chain_a]["started"] == 3
    assert by_id[chain_a]["attributed"] == 1
    assert by_id[chain_b]["started"] == 1
    assert by_id[chain_b]["attributed"] == 0
    assert by_id[chain_b]["replied_n"] == 0
    # 链名随行富化（监控页直接可读）
    assert by_id[chain_a]["chain_name"] == STARTER_CHAINS[0]["name"]


def test_chain_funnel_window_excludes_old(tmp_path):
    store = _store(tmp_path)
    ensure_starter_chains(store)
    now = time.time()
    e_old = store.start_chain_execution(STARTER_CHAINS[0]["chain_id"], "tg:a:old", {})
    _set_started_at(store, e_old, now - 20 * 86400)   # 20 天前，14 天窗外
    e_new = store.start_chain_execution(STARTER_CHAINS[0]["chain_id"], "tg:a:new", {})
    _set_started_at(store, e_new, now - 2 * 86400)
    d = chain_funnel(store, days=14, now=now)
    assert d["total"]["started"] == 1


def test_chain_funnel_empty_store_soft(tmp_path):
    store = _store(tmp_path)
    d = chain_funnel(store, days=14)
    assert d["ok"] is True
    assert d["total"]["started"] == 0
    assert d["chains"] == []
    assert d["goal_chain_starts"] == {}


def test_chain_funnel_collects_goal_chain_starts(tmp_path):
    """J 推荐跟随原料：goal_id → {chain_id: 启动数}（纯 store 数据，无归因的
    执行不进 map；模板解析/推荐比对属路由层职责，本层零跨域）。"""
    store = _store(tmp_path)
    ensure_starter_chains(store)
    a = STARTER_CHAINS[0]["chain_id"]
    b = STARTER_CHAINS[1]["chain_id"]
    store.start_chain_execution(a, "tg:a:g1c1", {"goal_id": "g1"})
    store.start_chain_execution(b, "tg:a:g1c2", {"goal_id": "g1"})
    store.start_chain_execution(a, "tg:a:g1c3", {"goal_id": "g1"})
    store.start_chain_execution(a, "tg:a:g2c1", {"goal_id": "g2"})
    store.start_chain_execution(a, "tg:a:none", {})            # 无归因不进 map
    d = chain_funnel(store, days=14)
    assert d["goal_chain_starts"] == {
        "g1": {a: 2, b: 1},
        "g2": {a: 1},
    }
    assert d["total"]["attributed"] == 4
