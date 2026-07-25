"""账号作用域键迁移门禁：归属判定保守性 / 键分类 / episodic+context 迁移幂等。"""
from __future__ import annotations

import sqlite3
import time

import pytest

from src.utils.account_scope_migration import (
    apply_context_account_scope,
    apply_episodic_account_scope,
    build_attribution,
    classify_key,
    plan_context_account_scope,
    plan_episodic_account_scope,
    _sole_nondefault_account,
)
from src.utils.episodic_memory_store import EpisodicMemoryStore


def test_classify_key_forms():
    assert classify_key("5433982810")["form"] == "bare"
    assert classify_key("telegram:5433982810") == {
        "form": "legacy_platform", "platform": "telegram", "peer": "5433982810"}
    assert classify_key("telegram:8244899900:5433982810")["form"] == "scoped"
    # 组合键（群 cid_uid）与未知形态不动
    assert classify_key("123_456")["form"] == "other"
    assert classify_key("wa:acct:peer:extra")["form"] == "other"
    assert classify_key("8244899900:5433982810")["form"] == "other"  # 无平台段不猜


def test_sole_nondefault_rules():
    assert _sole_nondefault_account({("telegram", "8244899900")}) == (
        "telegram", "8244899900")
    # default 在场 = 不迁（default 现行键就是旧格式）
    assert _sole_nondefault_account(
        {("telegram", "default"), ("telegram", "8244899900")}) is None
    # 多账号歧义 = 不迁
    assert _sole_nondefault_account(
        {("telegram", "a1"), ("telegram", "a2")}) is None
    # 平台过滤
    assert _sole_nondefault_account(
        {("whatsapp", "w1")}, platform="telegram") is None


def _mk_attribution_db(tmp_path, rows):
    db = str(tmp_path / "inbox.db")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conversations ("
        "conversation_id TEXT PRIMARY KEY, platform TEXT, account_id TEXT,"
        "chat_key TEXT)")
    for cid, plat, acct, ck in rows:
        conn.execute("INSERT INTO conversations VALUES (?,?,?,?)",
                     (cid, plat, acct, ck))
    conn.commit()
    conn.close()
    return db


def test_build_attribution(tmp_path):
    db = _mk_attribution_db(tmp_path, [
        ("telegram:8244899900:u1", "telegram", "8244899900", "u1"),
        ("telegram:default:u2", "telegram", "default", "u2"),
    ])
    att = build_attribution(db)
    assert att["u1"] == {("telegram", "8244899900")}
    assert att["u2"] == {("telegram", "default")}


def test_episodic_plan_and_apply_idempotent(tmp_path):
    store = EpisodicMemoryStore(str(tmp_path / "epi.db"))
    store.add_fact("u1", "用户喜欢旅行", "heuristic", source="user_stated")
    store.add_fact("telegram:u1", "用户是混血儿", "heuristic", source="user_stated")
    store.add_fact("telegram:u2", "用户在上海", "heuristic", source="user_stated")
    store.add_fact("u3", "多号歧义者", "heuristic")
    att = {
        "u1": {("telegram", "8244899900")},
        "u2": {("telegram", "default")},
        "u3": {("telegram", "a1"), ("telegram", "a2")},
    }
    plan = plan_episodic_account_scope(store, att)
    by_old = {p["old_key"]: p for p in plan}
    assert by_old["u1"]["new_key"] == "telegram:8244899900:u1"
    assert by_old["telegram:u1"]["new_key"] == "telegram:8244899900:u1"
    assert by_old["telegram:u2"]["action"] == "skip"      # default 现行键不动
    assert by_old["u3"]["action"] == "skip"               # 歧义不迁

    rep = apply_episodic_account_scope(store, att)
    assert rep["migrated_keys"] == 2 and rep["moved_rows"] == 2
    keys = {k for k, _ in store.list_key_stats()}
    assert "telegram:8244899900:u1" in keys
    assert "u1" not in keys and "telegram:u1" not in keys
    assert "telegram:u2" in keys and "u3" in keys
    # 幂等复跑：无候选、零移动
    rep2 = apply_episodic_account_scope(store, att)
    assert rep2["moved_rows"] == 0


def test_context_plan_and_apply(tmp_path):
    db = str(tmp_path / "ctx.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE user_context (user_id TEXT PRIMARY KEY,"
                 " data TEXT NOT NULL DEFAULT '{}', updated_at REAL)")
    for k in ("u1", "u2", "a9:u9"):
        conn.execute("INSERT INTO user_context VALUES (?,?,?)",
                     (k, "{}", time.time()))
    conn.commit()
    conn.close()
    att = {"u1": {("telegram", "8244899900")},
           "u2": {("telegram", "default")}}
    plan = plan_context_account_scope(db, att)
    assert [p["old_key"] for p in plan] == ["u1"]
    assert plan[0]["new_key"] == "8244899900:u1"

    rep = apply_context_account_scope(db, att)
    assert rep["renamed"] == 1
    conn = sqlite3.connect(db)
    keys = {r[0] for r in conn.execute("SELECT user_id FROM user_context")}
    conn.close()
    assert "8244899900:u1" in keys and "u1" not in keys and "u2" in keys


def _mk_ctx_db(tmp_path, rows):
    db = str(tmp_path / "ctx2.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE user_context (user_id TEXT PRIMARY KEY,"
                 " data TEXT NOT NULL DEFAULT '{}', updated_at REAL)")
    for k, ts in rows:
        conn.execute("INSERT INTO user_context VALUES (?,?,?)", (k, "{}", ts))
    conn.commit()
    conn.close()
    return db


def test_context_platform_keys_and_newest_wins(tmp_path):
    """三段平台键纳入候选；同目标多来源（裸+平台键）按 updated_at 最新择优。"""
    db = _mk_ctx_db(tmp_path, [
        ("p1", 100.0),                    # 裸键（旧）
        ("whatsapp:w1:p1", 200.0),        # 平台键（新）→ 应胜出
        ("whatsapp:w1:p2", 150.0),        # 仅平台键
        ("whatsapp:p4", 120.0),           # 两段平台键
    ])
    att = {"p1": {("whatsapp", "w1")}, "p4": {("whatsapp", "w1")}}
    plan = plan_context_account_scope(db, att)
    by_old = {p["old_key"]: p for p in plan}
    assert by_old["whatsapp:w1:p1"]["action"] == "rename"
    assert by_old["whatsapp:w1:p1"]["new_key"] == "w1:p1"
    assert by_old["p1"]["action"] == "skip_older_duplicate"
    assert by_old["whatsapp:w1:p2"]["action"] == "rename"
    assert by_old["whatsapp:p4"]["new_key"] == "w1:p4"

    rep = apply_context_account_scope(db, att)
    assert rep["renamed"] == 3
    conn = sqlite3.connect(db)
    keys = {r[0] for r in conn.execute("SELECT user_id FROM user_context")}
    conn.close()
    assert {"w1:p1", "w1:p2", "w1:p4"} <= keys
    assert "p1" in keys  # 落选旧行保留为死数据（不删不丢）
    assert "whatsapp:w1:p1" not in keys


def test_context_target_exists_keeps_new(tmp_path):
    """目标现行键已有数据 → 全部跳过保新（绝不覆盖运行中数据）。"""
    db = _mk_ctx_db(tmp_path, [
        ("w1:p9", 300.0),                 # 现行键（新数据）
        ("whatsapp:w1:p9", 100.0),        # 孤儿旧键
        ("p9", 90.0),
    ])
    att = {"p9": {("whatsapp", "w1")}}
    plan = plan_context_account_scope(db, att)
    assert {p["action"] for p in plan} == {"skip_target_exists"}
    rep = apply_context_account_scope(db, att)
    assert rep["renamed"] == 0


def test_online_account_gate(tmp_path):
    """在线闸门：目标账号不在注册表在线集 → 拒迁（A 线裸键/removed 号保护）。"""
    store = EpisodicMemoryStore(str(tmp_path / "epi2.db"))
    store.add_fact("u1", "老客户事实", "heuristic", source="user_stated")
    att = {"u1": {("telegram", "8127518232")}}
    online = {("telegram", "8244899900")}  # 8127518232 已 removed

    plan = plan_episodic_account_scope(store, att, online)
    assert plan[0]["action"] == "skip"
    assert plan[0]["reason"] == "target_account_not_online"
    # 不给 online 集合 = 旧行为（可迁）
    assert plan_episodic_account_scope(store, att)[0]["action"] == "rename"

    db = _mk_ctx_db(tmp_path, [("u1", 100.0)])
    ctx_plan = plan_context_account_scope(db, att, online)
    assert [p["action"] for p in ctx_plan] == ["skip_account_not_online"]
    rep = apply_context_account_scope(db, att, online)
    assert rep["renamed"] == 0
