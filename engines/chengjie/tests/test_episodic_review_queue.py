"""J-10 A2（#183 · 决策 D8）：「待确认」改例外队列 + 自动转正 + 软删 + 冲突并列。

覆盖：五类打标（conflict / high_impact / low_confidence / self_fact / commitment）、
第二条晋升路径（7 天 + 被召回 ≥1 + 无冲突）、冲突并列不覆盖 stable、软删不召回可恢复、
confirm 语义（转正 + 清 review_reason）、四条新路由。全部 tmp_path。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.utils.episodic_memory_store import EpisodicMemoryStore
from src.utils.memory_review import (
    REVIEW_COMMITMENT,
    REVIEW_CONFLICT,
    REVIEW_HIGH_IMPACT,
    REVIEW_LOW_CONFIDENCE,
    REVIEW_SELF_FACT,
    classify_fact,
)


@pytest.fixture
def store(tmp_path):
    s = EpisodicMemoryStore(tmp_path / "t.db")
    yield s
    s.close()


def _row(store: EpisodicMemoryStore, rid: int):
    r = store._conn.execute(
        "SELECT COALESCE(review_reason,''), COALESCE(impact,'normal'), COALESCE(status,'active'),"
        " COALESCE(tier,'raw'), COALESCE(conflict_group,''), COALESCE(source,'user_stated')"
        " FROM episodic_memory WHERE id = ?", (rid,)).fetchone()
    return {"review_reason": r[0], "impact": r[1], "status": r[2], "tier": r[3],
            "conflict_group": r[4], "source": r[5]}


# ── 五类打标（纯函数）──────────────────────────────────────────────────────
def test_classify_five_classes_and_normal():
    assert classify_fact("客户有一个女儿") == (REVIEW_HIGH_IMPACT, "high")
    assert classify_fact("客户的地址是幸福小区3号楼") == (REVIEW_HIGH_IMPACT, "high")
    assert classify_fact("客户最近在住院") == (REVIEW_HIGH_IMPACT, "high")
    assert classify_fact("客户答应下周五转账 2000 元") == (REVIEW_COMMITMENT, "high")
    assert classify_fact("客户说下周来见我") == (REVIEW_COMMITMENT, "high")
    assert classify_fact("客户可能是护士", source="ai_inferred") == (REVIEW_LOW_CONFIDENCE, "normal")
    assert classify_fact("客户是护士", source="ai_inferred", confidence=0.4) == (
        REVIEW_LOW_CONFIDENCE, "normal")
    assert classify_fact("客户是护士", source="ai_inferred", confidence=0.9) == ("", "normal")
    assert classify_fact("我有个女儿") == (REVIEW_SELF_FACT, "high")
    assert classify_fact("I have a daughter") == (REVIEW_SELF_FACT, "high")
    assert classify_fact("客户喜欢吃披萨", author="human") == (REVIEW_SELF_FACT, "normal")
    # 日常事实不进队列（D8：客户说的事实自动记）
    for t in ("客户喜欢喝美式咖啡", "客户住在上海", "用户自称：小明", "客户 25 岁",
              "客户每周三晚上有空", "客户结过一次婚", "客户想去大阪玩"):
        assert classify_fact(t) == ("", "normal"), t
    # 客户主语的推断句不是 self_fact
    assert classify_fact("客户可能喜欢猫", source="ai_inferred")[0] == REVIEW_LOW_CONFIDENCE


# ── 入库打标 + 冲突并列 ─────────────────────────────────────────────────────
def test_add_fact_tags_and_explicit_override(store: EpisodicMemoryStore):
    uid = "k1"
    hi = store.add_fact(uid, "客户有一个女儿", source="ai_inferred")
    assert _row(store, hi)["review_reason"] == REVIEW_HIGH_IMPACT
    assert _row(store, hi)["impact"] == "high"
    plain = store.add_fact(uid, "客户喜欢喝美式咖啡")
    assert _row(store, plain) == {"review_reason": "", "impact": "normal", "status": "active",
                                  "tier": "raw", "conflict_group": "", "source": "user_stated"}
    low = store.add_fact(uid, "客户是护士", source="ai_inferred", confidence=0.3)
    assert _row(store, low)["review_reason"] == REVIEW_LOW_CONFIDENCE
    # 显式传入不再自算（导入/人工核过）
    forced = store.add_fact(uid, "客户的地址是幸福小区3号楼", review_reason="", impact="normal")
    assert _row(store, forced)["review_reason"] == "" and _row(store, forced)["impact"] == "normal"
    counts = store.review_counts()
    assert counts["pending"] == 2
    assert counts["by_reason"][REVIEW_HIGH_IMPACT] == 1
    assert counts["by_reason"][REVIEW_LOW_CONFIDENCE] == 1
    assert counts["high_impact_pending"] == 1


def test_conflict_with_stable_is_paired_not_overwritten(store: EpisodicMemoryStore):
    uid = "k2"
    old = store.add_fact(uid, "用户住在北京")
    store._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (old,))
    store._conn.commit()
    new = store.add_fact(uid, "用户住在上海")
    r_new, r_old = _row(store, new), _row(store, old)
    assert r_new["review_reason"] == REVIEW_CONFLICT
    assert r_new["conflict_group"] == f"g{old}" == r_old["conflict_group"]
    # stable 不被自动覆盖；新条不进 prompt（不让 prompt 里出现两个矛盾事实）
    assert r_old["tier"] == "stable" and r_old["review_reason"] == ""
    bullets = store.get_bullets_for_prompt(uid)
    assert "北京" in bullets and "上海" not in bullets
    # 队列里并列展示
    q = store.review_queue(user_id=uid)
    assert [i["id"] for i in q] == [new]
    assert q[0]["conflict_with"]["id"] == old
    # 复发 ≥2 也不能把冲突条晋升 stable
    store.add_fact(uid, "用户住在上海")   # hits → 2
    res = store.consolidate(uid, min_hits=2)
    assert res["promoted"] == 0 and _row(store, new)["tier"] == "raw"
    # 人工择一：保留新条 → 新 stable、旧 stale、组键清空
    out = store.resolve_conflict(new)
    assert out["kept"] == new and out["staled"] == [old]
    assert _row(store, new)["tier"] == "stable" and _row(store, new)["review_reason"] == ""
    assert _row(store, old)["tier"] == "stale"
    assert _row(store, new)["conflict_group"] == "" == _row(store, old)["conflict_group"]
    assert "上海" in store.get_bullets_for_prompt(uid)
    assert store.review_queue(user_id=uid) == []
    # 不在冲突组的 id → kept None
    assert store.resolve_conflict(new)["kept"] is None


def test_resolve_conflict_keep_stable_side(store: EpisodicMemoryStore):
    uid = "k3"
    old = store.add_fact(uid, "用户住在北京")
    store._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (old,))
    store._conn.commit()
    new = store.add_fact(uid, "用户住在上海")
    out = store.resolve_conflict(old)
    assert out["kept"] == old and out["staled"] == [new]
    assert _row(store, old)["tier"] == "stable" and _row(store, new)["tier"] == "stale"


# ── 自动转正：7 天 + 被召回 ≥1 + 无冲突 ────────────────────────────────────
def test_auto_promote_path(store: EpisodicMemoryStore):
    uid = "k4"
    old_ts = time.time() - 8 * 86400
    a = store.add_fact(uid, "客户喜欢喝美式咖啡")           # 老 + 被召回 → 转正
    b = store.add_fact(uid, "客户喜欢看科幻电影")           # 老 + 未召回 → 不转
    c = store.add_fact(uid, "客户每周三晚上有空")           # 新 + 被召回 → 不转
    store._conn.execute("UPDATE episodic_memory SET created_at=?, recall_count=3 WHERE id=?",
                        (old_ts, a))
    store._conn.execute("UPDATE episodic_memory SET created_at=? WHERE id=?", (old_ts, b))
    store._conn.execute("UPDATE episodic_memory SET recall_count=1 WHERE id=?", (c,))
    store._conn.commit()
    res = store.consolidate(uid, min_hits=2)
    assert res["auto_promoted"] == 1 and res["promoted"] == 1
    assert _row(store, a)["tier"] == "stable"
    assert _row(store, b)["tier"] == "raw" and _row(store, c)["tier"] == "raw"
    # 关掉第二路径 → 不再转
    d = store.add_fact(uid, "客户养了一只猫")
    store._conn.execute("UPDATE episodic_memory SET created_at=?, recall_count=1 WHERE id=?",
                        (old_ts, d))
    store._conn.commit()
    assert store.consolidate(uid, min_hits=2, auto_promote_days=None)["auto_promoted"] == 0
    assert store.consolidate(uid, min_hits=2, auto_promote_min_recalls=2)["auto_promoted"] == 0
    assert store.consolidate(uid, min_hits=2)["auto_promoted"] == 1
    assert _row(store, d)["tier"] == "stable"


def test_auto_promote_skips_conflict_and_ignored(store: EpisodicMemoryStore):
    uid = "k5"
    old_ts = time.time() - 30 * 86400
    stable = store.add_fact(uid, "用户住在北京")
    store._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (stable,))
    store._conn.commit()
    conflict = store.add_fact(uid, "用户住在上海")
    ignored = store.add_fact(uid, "客户喜欢喝美式咖啡")
    store.ignore_fact(ignored)
    store._conn.execute("UPDATE episodic_memory SET created_at=?, recall_count=5 WHERE id IN (?, ?)",
                        (old_ts, conflict, ignored))
    store._conn.commit()
    assert store.consolidate(uid, min_hits=2)["auto_promoted"] == 0
    assert _row(store, conflict)["tier"] == "raw" and _row(store, ignored)["tier"] == "raw"


# ── 软删：不召回 / 不算画像 / 可恢复 / 先被裁 ─────────────────────────────
def test_ignore_restore_semantics(store: EpisodicMemoryStore):
    uid = "k6"
    rid = store.add_fact(uid, "客户喜欢喝美式咖啡")
    keep = store.add_fact(uid, "客户喜欢看科幻电影")
    assert store.ignore_fact(rid) == "客户喜欢喝美式咖啡"
    assert _row(store, rid)["status"] == "ignored"
    assert "美式咖啡" not in store.get_bullets_for_prompt(uid)
    assert store.profile_summary(uid)["total"] == 1
    assert [r["id"] for r in store.list_rows(prefix=uid)] == [keep]
    assert [r["id"] for r in store.list_rows(prefix=uid, status="ignored")] == [rid]
    assert len(store.list_rows(prefix=uid, status="all")) == 2
    assert store.list_rows(prefix=uid, status="ignored")[0]["status"] == "ignored"
    # 恢复
    assert store.restore_fact(rid) == "客户喜欢喝美式咖啡"
    assert store.restore_fact(rid) is None          # 本就 active → 不命中
    assert "美式咖啡" in store.get_bullets_for_prompt(uid)
    assert store.ignore_fact(999999) is None and store.ignore_fact("x") is None


def test_ignored_pruned_first(store: EpisodicMemoryStore):
    uid = "k7"
    ids = [store.add_fact(uid, f"客户事实 {i} 独立 {i}") for i in range(5)]
    store.ignore_fact(ids[4])                        # 最新的一条被软删
    store.prune_oldest(uid, keep=4)
    left = {r["id"] for r in store.list_rows(prefix=uid, status="all")}
    assert ids[4] not in left and ids[0] in left    # 软删先裁，最旧的 active 仍在


# ── confirm：转正 + 清 review_reason；不在队列的 user_stated 仍不命中 ──────
def test_confirm_clears_review_and_keeps_legacy_gate(store: EpisodicMemoryStore):
    uid = "k8"
    hi = store.add_fact(uid, "客户有一个女儿")         # user_stated + high_impact
    assert store.confirm_inferred_fact(hi) == "客户有一个女儿"
    r = _row(store, hi)
    assert r["tier"] == "stable" and r["review_reason"] == "" and r["source"] == "user_stated"
    plain = store.add_fact(uid, "客户喜欢喝美式咖啡")
    assert store.confirm_inferred_fact(plain) is None   # 旧口径：不在队列的明说事实不动
    inf = store.add_fact(uid, "客户喜欢看科幻电影", source="ai_inferred")
    assert store.confirm_inferred_fact(inf) == "客户喜欢看科幻电影"
    assert store.review_queue(user_id=uid) == []


def test_review_queue_ordering_filter_and_summary(store: EpisodicMemoryStore):
    uid = "k9"
    low = store.add_fact(uid, "客户可能是护士", source="ai_inferred")
    hi = store.add_fact(uid, "客户最近在住院")
    store.add_fact("other", "客户答应下周转账 500 元")
    q = store.review_queue()
    assert [i["id"] for i in q][:1] == [hi] or q[0]["impact"] == "high"   # high 先
    assert len(q) == 3
    assert [i["id"] for i in store.review_queue(user_id=uid, reason=REVIEW_LOW_CONFIDENCE)] == [low]
    assert store.review_queue(reason="nope") == []
    s = store.admin_summary(days=7)
    assert s["review"]["pending"] == 3 and s["total_count"] == 3 and s["total_users"] == 2
    assert s["review"]["by_reason"][REVIEW_COMMITMENT] == 1
    store.ignore_fact(low)
    assert store.admin_summary(days=7)["total_count"] == 2
    assert store.review_counts()["pending"] == 2


# ── 路由 ───────────────────────────────────────────────────────────────────
def test_review_routes_end_to_end(tmp_path):
    from starlette.testclient import TestClient
    from src.utils.audit_store import AuditStore
    from src.web.admin import create_app
    from tests.test_web_episodic_memory_api import _load_cm, _run_async

    cm = _run_async(_load_cm(tmp_path))
    audit = AuditStore(db_path=tmp_path / "audit.db")
    st = EpisodicMemoryStore(tmp_path / "mem.db")
    uid = "telegram:acct:900"
    old = st.add_fact(uid, "用户住在北京")
    st._conn.execute("UPDATE episodic_memory SET tier='stable' WHERE id=?", (old,))
    st._conn.commit()
    new = st.add_fact(uid, "用户住在上海")
    hi = st.add_fact(uid, "客户有一个女儿")
    plain = st.add_fact(uid, "客户喜欢喝美式咖啡")
    from src.skills.skill_manager import SkillManager
    sm = MagicMock()
    sm._episodic_store = st
    # 走真实 wrapper（status / review 透传必须真到 store——A2 首版曾只提交了签名没提交透传体）
    sm.episodic_list_for_admin = lambda **kw: SkillManager.episodic_list_for_admin(sm, **kw)
    tc = MagicMock()
    tc.skill_manager = sm
    app = create_app(cm, audit_store=audit, boot_ts=0, telegram_client=tc)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.headers.update({"Authorization": "Bearer test-token-123"})
        r = client.get("/api/episodic-memory/review-queue", params={"memory_key": uid})
        assert r.status_code == 200
        d = r.json()
        assert d["counts"]["pending"] == 2
        ids = {i["id"]: i for i in d["items"]}
        assert set(ids) == {new, hi}
        assert ids[new]["conflict_with"]["id"] == old
        # 择一保留新条
        r = client.post("/api/episodic-memory/resolve-conflict", json={"keep_id": new})
        assert r.status_code == 200 and r.json()["kept"] == new and r.json()["staled"] == [old]
        assert client.post("/api/episodic-memory/resolve-conflict", json={"keep_id": plain}).status_code == 404
        assert client.post("/api/episodic-memory/resolve-conflict", json={}).status_code == 400
        # 软删 / 恢复
        r = client.post(f"/api/episodic-memory/{plain}/ignore")
        assert r.status_code == 200 and r.json()["ignored"] == plain
        # 默认只看 active（old 已 stale 但 status 仍 active，列表不按 tier 过滤）：old/new/hi
        assert client.get("/api/episodic-memory", params={"prefix": uid}).json()["count"] == 3
        items_all = client.get("/api/episodic-memory",
                               params={"prefix": uid, "status": "all"}).json()["items"]
        assert plain in {i["id"] for i in items_all}
        items_ign = client.get("/api/episodic-memory",
                               params={"prefix": uid, "status": "ignored"}).json()["items"]
        assert [i["id"] for i in items_ign] == [plain] and items_ign[0]["status"] == "ignored"
        assert client.post(f"/api/episodic-memory/{plain}/restore").status_code == 200
        assert client.post(f"/api/episodic-memory/{plain}/restore").status_code == 404
        assert client.post("/api/episodic-memory/999999/ignore").status_code == 404
        # review 筛选
        pend = client.get("/api/episodic-memory",
                          params={"prefix": uid, "review": "pending"}).json()["items"]
        assert [i["id"] for i in pend] == [hi]
        # 审计留痕
        assert audit.query(limit=10, action="episodic_resolve_conflict")
        assert audit.query(limit=10, action="episodic_ignore")
        assert audit.query(limit=10, action="episodic_restore")
    st.close()
