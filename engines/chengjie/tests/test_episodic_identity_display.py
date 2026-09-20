# -*- coding: utf-8 -*-
"""「AI 记忆」身份化（P0）门禁：memory_key → 昵称/头像解析 + 人类可读搜索。

不变量重点：
- 键形态解析纯函数四态（canonical / canonical+群成员 / acct_peer / bare）；
- **错认比不认伤害大**：bare/acct_peer 只有唯一命中才 approx 认领，歧义一律
  unresolved；
- 富化/搜索全程软失败（inbox 库缺席 → 旧响应形状），绝不阻断记忆列表；
- 路由 q 联合搜索 = 昵称/用户名/手机号（经会话表译键集）∪ 记忆键 LIKE ∪ 内容
  LIKE；q 为空时对 skill_manager 保持旧三参调用形状（既有 MagicMock 断言依赖）;
- offset 分页排序带 id 次键（created_at 秒级粒度同秒多行翻页不重不漏）；
- 删除落审计（与 confirm 对称）：记「谁删了谁的哪条记忆」，404 不落。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.utils.episodic_identity_display import (
    _clear_cache,
    find_conversation_keys,
    parse_memory_key,
    resolve_identities,
)
from src.utils.episodic_memory_store import EpisodicMemoryStore


@pytest.fixture(autouse=True)
def _fresh_cache():
    _clear_cache()
    yield
    _clear_cache()


# ── fixtures ─────────────────────────────────────────────────────────────

def _mk_inbox_db(tmp_path: Path, *, full_cols: bool = True, name: str = "inbox.db") -> Path:
    db = tmp_path / name
    conn = sqlite3.connect(db)
    cols = (
        "conversation_id TEXT PRIMARY KEY, platform TEXT NOT NULL, "
        "account_id TEXT NOT NULL DEFAULT 'default', "
        "chat_key TEXT NOT NULL DEFAULT '', "
        "display_name TEXT NOT NULL DEFAULT '', "
        "chat_type TEXT NOT NULL DEFAULT 'private', "
        "last_ts REAL NOT NULL DEFAULT 0"
    )
    if full_cols:
        cols += (", username TEXT NOT NULL DEFAULT '', "
                 "phone TEXT NOT NULL DEFAULT '', "
                 "avatar_url TEXT NOT NULL DEFAULT ''")
    conn.execute(f"CREATE TABLE conversations ({cols})")

    def ins(plat, acct, ck, name_, ctype="private", username="", phone="",
            avatar="", last_ts=0.0):
        base = [f"{plat}:{acct}:{ck}", plat, acct, ck, name_, ctype, last_ts]
        if full_cols:
            conn.execute(
                "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?)",
                base + [username, phone, avatar])
        else:
            conn.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?,?)", base)

    ins("telegram", "8244899900", "8921664288", "张小明",
        username="zhang_xm", avatar="/static/avatars/1.jpg", last_ts=100)
    ins("telegram", "8244899900", "-100777", "产品交流群", ctype="group", last_ts=90)
    ins("telegram", "8244899900", "555", "李四", phone="+8613800138000", last_ts=80)
    ins("whatsapp", "63917", "999", "Maria", phone="+639171234567", last_ts=70)
    # bare 歧义对：同 chat_key 两平台
    ins("telegram", "8244899900", "777", "甲", last_ts=60)
    ins("whatsapp", "63917", "777", "乙", last_ts=50)
    if full_cols:
        # 通讯录兜底数据（P3）：30001/667 只有联系人、没有会话行；
        # old.db（full_cols=False）刻意不建此表——覆盖「老库无表软跳过」路径
        conn.execute(
            "CREATE TABLE protocol_contacts ("
            "platform TEXT NOT NULL, account_id TEXT NOT NULL, "
            "chat_key TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', "
            "notify_name TEXT NOT NULL DEFAULT '', "
            "PRIMARY KEY (platform, account_id, chat_key))")
        conn.execute(
            "INSERT INTO protocol_contacts VALUES "
            "('telegram','8244899900','30001','王五','')")
        conn.execute(
            "INSERT INTO protocol_contacts VALUES "
            "('telegram','8244899900','667','','陈七')")
    conn.commit()
    conn.close()
    return db


# ── parse_memory_key 纯函数 ──────────────────────────────────────────────

def test_parse_canonical_private():
    p = parse_memory_key("telegram:8244899900:8921664288")
    assert p["form"] == "canonical"
    assert p["platform"] == "telegram"
    assert p["account_id"] == "8244899900"
    assert p["peer"] == "8921664288"
    assert p["group_id"] == "" and p["member_id"] == ""


def test_parse_canonical_group_member():
    p = parse_memory_key("telegram:8244899900:-100777_555")
    assert p["form"] == "canonical"
    assert p["group_id"] == "-100777" and p["member_id"] == "555"


def test_parse_acct_peer_and_bare():
    p = parse_memory_key("8244899900:8921664288")
    assert p["form"] == "acct_peer"
    assert p["account_id"] == "8244899900" and p["peer"] == "8921664288"
    b = parse_memory_key("8921664288")
    assert b["form"] == "bare" and b["peer"] == "8921664288"
    assert parse_memory_key("")["form"] == "empty"


def test_parse_line_uid_not_mistaken_as_group():
    # LINE U-hex / WA jid 不含「数字_数字」形态，不得误拆成群成员
    p = parse_memory_key("line:acct1:U4af4980629abcdef")
    assert p["form"] == "canonical" and p["group_id"] == ""


# ── resolve_identities ───────────────────────────────────────────────────

def test_resolve_canonical_hit(tmp_path):
    db = _mk_inbox_db(tmp_path)
    out = resolve_identities(db, ["telegram:8244899900:8921664288"])
    idn = out["telegram:8244899900:8921664288"]
    assert idn["resolved"] is True and idn["approx"] is False
    assert idn["kind"] == "private"
    assert idn["name"] == "张小明"
    assert idn["username"] == "zhang_xm"
    assert idn["avatar_url"] == "/static/avatars/1.jpg"
    assert idn["platform"] == "telegram"
    assert idn["conversation_id"] == "telegram:8244899900:8921664288"


def test_resolve_miss_keeps_parsed_platform(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "telegram:8244899900:404404"
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is False
    assert idn["platform"] == "telegram"  # 前端仍可出平台徽标
    assert idn["chat_key"] == "404404"


def test_resolve_bare_unique_is_approx(tmp_path):
    db = _mk_inbox_db(tmp_path)
    idn = resolve_identities(db, ["8921664288"])["8921664288"]
    assert idn["resolved"] is True and idn["approx"] is True
    assert idn["name"] == "张小明"


def test_resolve_bare_ambiguous_unresolved(tmp_path):
    db = _mk_inbox_db(tmp_path)
    idn = resolve_identities(db, ["777"])["777"]
    assert idn["resolved"] is False  # 两平台同 chat_key → 宁可不认


def test_resolve_group_member_with_private_row(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "telegram:8244899900:-100777_555"
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is True and idn["kind"] == "group"
    assert idn["name"] == "李四"          # 成员私聊会话的昵称
    assert idn["group_name"] == "产品交流群"
    assert idn["member_id"] == "555"


def test_resolve_group_member_without_private_row(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "telegram:8244899900:-100777_666"
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is True and idn["kind"] == "group"
    assert idn["name"] == ""              # 不冒充群名当成员名
    assert idn["group_name"] == "产品交流群"
    assert idn["member_id"] == "666"
    assert idn["avatar_url"] == ""        # 群头像不冒充成员头像


def test_resolve_acct_peer_unique_platform_variant(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "8244899900:8921664288"  # 无平台段：仅 telegram 变体命中
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is True and idn["approx"] is True
    assert idn["name"] == "张小明" and idn["platform"] == "telegram"


# ── 通讯录名兜底（P3：加了好友没开口 → 有联系人无会话行）────────────────

def test_resolve_contact_fallback_private(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "telegram:8244899900:30001"  # 会话表无此行，通讯录有「王五」
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is True and idn["kind"] == "private"
    assert idn["name"] == "王五"
    assert idn["conversation_id"] == ""   # 无会话 → 不给跳转
    assert idn["avatar_url"] == ""
    assert idn["platform"] == "telegram"


def test_resolve_contact_fallback_group_member(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "telegram:8244899900:-100777_667"  # 成员无私聊行，通讯录备注「陈七」
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is True and idn["kind"] == "group"
    assert idn["name"] == "陈七"             # notify_name 兜底
    assert idn["group_name"] == "产品交流群"
    assert idn["member_id"] == "667"


def test_resolve_contact_fallback_absent_table_soft_skip(tmp_path):
    db = _mk_inbox_db(tmp_path, full_cols=False, name="old3.db")
    key = "telegram:8244899900:30001"  # 老库无 protocol_contacts 表 → 保持 unresolved
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is False


def test_resolve_cache_and_soft_fail(tmp_path):
    db = _mk_inbox_db(tmp_path)
    key = "telegram:8244899900:8921664288"
    assert resolve_identities(db, [key])[key]["resolved"] is True
    db.unlink()  # 库消失：TTL 缓存内仍可读
    assert resolve_identities(db, [key])[key]["resolved"] is True
    _clear_cache()  # 缓存清空后软失败 → 空映射，绝不抛
    assert resolve_identities(db, [key]) == {}


# ── find_conversation_keys ───────────────────────────────────────────────

def test_find_keys_by_name_username_phone(tmp_path):
    db = _mk_inbox_db(tmp_path)
    assert find_conversation_keys(db, "张小明") == ["telegram:8244899900:8921664288"]
    assert find_conversation_keys(db, "zhang_xm") == ["telegram:8244899900:8921664288"]
    assert "telegram:8244899900:555" in find_conversation_keys(db, "13800138000")
    assert find_conversation_keys(db, "不存在的人") == []
    assert find_conversation_keys(db, "") == []


def test_find_keys_legacy_columns_fallback(tmp_path):
    db = _mk_inbox_db(tmp_path, full_cols=False, name="old.db")
    assert find_conversation_keys(db, "张小明") == ["telegram:8244899900:8921664288"]
    # 老库缺 username 列：searching username 不崩、按昵称列回落（查不到即空）
    assert find_conversation_keys(db, "zhang_xm") == []


def test_resolve_legacy_columns_no_avatar(tmp_path):
    db = _mk_inbox_db(tmp_path, full_cols=False, name="old2.db")
    key = "telegram:8244899900:8921664288"
    idn = resolve_identities(db, [key])[key]
    assert idn["resolved"] is True and idn["name"] == "张小明"
    assert idn["avatar_url"] == "" and idn["username"] == ""


# ── store.list_rows q 联合搜索 ───────────────────────────────────────────

def _mk_store(tmp_path: Path) -> EpisodicMemoryStore:
    store = EpisodicMemoryStore(tmp_path / "bot.db")
    store.add_fact("telegram:8244899900:8921664288", "用户每天电脑办公约12小时",
                   "habit", source="ai_inferred")
    store.add_fact("telegram:8244899900:555", "用户在做跨境电商", "work")
    store.add_fact("999888", "用户喜欢喝美式咖啡", "preference")
    return store


def test_list_rows_q_matches_content(tmp_path):
    store = _mk_store(tmp_path)
    rows = store.list_rows(q="跨境")
    assert [r["memory_key"] for r in rows] == ["telegram:8244899900:555"]


def test_list_rows_q_matches_key_like(tmp_path):
    store = _mk_store(tmp_path)
    rows = store.list_rows(q="8921664288")
    assert [r["memory_key"] for r in rows] == ["telegram:8244899900:8921664288"]


def test_list_rows_q_keys_union(tmp_path):
    store = _mk_store(tmp_path)
    # q 本身无字面命中，但键集（昵称翻译产物）命中 → 仍返回
    rows = store.list_rows(q="张小明", q_keys=["telegram:8244899900:8921664288"])
    assert [r["memory_key"] for r in rows] == ["telegram:8244899900:8921664288"]


def test_list_rows_q_and_source_are_and(tmp_path):
    store = _mk_store(tmp_path)
    rows = store.list_rows(q="用户", source="ai_inferred")
    assert [r["memory_key"] for r in rows] == ["telegram:8244899900:8921664288"]


def test_list_rows_old_signature_unchanged(tmp_path):
    store = _mk_store(tmp_path)
    assert len(store.list_rows(prefix="", limit=100, source="")) == 3
    rows = store.list_rows(prefix="555")
    assert [r["memory_key"] for r in rows] == ["telegram:8244899900:555"]


# ── 路由端到端（q 搜索 + identity 富化 + 软降级）─────────────────────────

def _build_app(tmp_path: Path, inbox_db, audit=None):
    from src.web.routes.episodic_identity_routes import (
        register_episodic_identity_routes,
    )

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.yaml").write_text("{}", encoding="utf-8")
    app = FastAPI()
    store = _mk_store(tmp_path)

    class _SM:
        _episodic_store = store

        def episodic_list_for_admin(self, prefix="", limit=100, source="",
                                    q="", q_keys=None, offset=0):
            extra = {}
            if q:
                extra.update(q=q, q_keys=q_keys or [])
            if offset:
                extra["offset"] = offset
            if extra:
                return store.list_rows(prefix=prefix, limit=limit,
                                       source=source, **extra)
            return store.list_rows(prefix=prefix, limit=limit, source=source)

        def episodic_delete_for_admin(self, row_id):
            return store.delete_by_id(int(row_id))

    app.state.skill_manager = _SM()

    def _auth(request: Request) -> None:
        request.scope.setdefault("session", {"username": "tester"})

    def _api_write(_perm: str):
        def dep(request: Request) -> None:
            request.scope.setdefault("session", {"username": "tester"})
        return dep

    cfg = {"inbox": {"db_path": str(inbox_db)}} if inbox_db else {}
    ctx = SimpleNamespace(
        telegram_client=None, api_auth=_auth, api_write=_api_write,
        audit_store=audit,
        config_manager=SimpleNamespace(
            config=cfg, config_path=cfg_dir / "config.yaml"),
    )
    register_episodic_identity_routes(app, ctx)
    return TestClient(app)


def test_route_q_by_nickname_and_identity_enrich(tmp_path):
    db = _mk_inbox_db(tmp_path)
    client = _build_app(tmp_path, db)
    r = client.get("/api/episodic-memory?q=张小明")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [it["memory_key"] for it in items] == ["telegram:8244899900:8921664288"]
    idn = items[0]["identity"]
    assert idn["resolved"] is True and idn["name"] == "张小明"
    assert idn["username"] == "zhang_xm"


def test_route_q_prefix_dual_send_uses_q_semantics(tmp_path):
    """过渡期前端 q+prefix 双发同值：不得 AND 缩窄（昵称按 prefix LIKE 必空）。"""
    db = _mk_inbox_db(tmp_path)
    client = _build_app(tmp_path, db)
    r = client.get("/api/episodic-memory?q=张小明&prefix=张小明")
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


def test_route_no_q_returns_all_with_identity(tmp_path):
    db = _mk_inbox_db(tmp_path)
    client = _build_app(tmp_path, db)
    r = client.get("/api/episodic-memory")
    items = r.json()["items"]
    assert len(items) == 3
    by_key = {it["memory_key"]: it for it in items}
    assert by_key["telegram:8244899900:555"]["identity"]["name"] == "李四"
    # bare 且唯一命中不存在 → unresolved（999888 无会话）
    assert by_key["999888"]["identity"]["resolved"] is False


def test_route_identity_flag_off(tmp_path):
    db = _mk_inbox_db(tmp_path)
    client = _build_app(tmp_path, db)
    items = client.get("/api/episodic-memory?identity=0").json()["items"]
    assert items and all("identity" not in it for it in items)


def test_route_missing_inbox_db_degrades_to_old_shape(tmp_path):
    client = _build_app(tmp_path, None)  # cfg 无 inbox.db_path → cfg_dir/inbox.db 不存在
    r = client.get("/api/episodic-memory?q=咖啡")
    assert r.status_code == 200
    items = r.json()["items"]
    # 内容 LIKE 仍可用（键集翻译降级为空），且不附 identity
    assert [it["memory_key"] for it in items] == ["999888"]
    assert all("identity" not in it for it in items)


# ── offset 分页 + 删除审计（P1 后端攒批，随 P0 同一重启窗装载）──────────

def test_list_rows_offset_pagination_no_dup_no_gap(tmp_path):
    """同秒多行（created_at 同值）靠 id 次键稳定排序：翻页不重不漏。"""
    store = EpisodicMemoryStore(tmp_path / "bot.db")
    for i in range(5):
        store.add_fact(f"k{i}", f"内容{i}", "general")
    all_ids = [r["id"] for r in store.list_rows(limit=10)]
    assert len(all_ids) == 5
    paged = (store.list_rows(limit=2)
             + store.list_rows(limit=2, offset=2)
             + store.list_rows(limit=2, offset=4))
    assert [r["id"] for r in paged] == all_ids


def test_get_row_brief(tmp_path):
    store = _mk_store(tmp_path)
    row = store.list_rows(limit=1)[0]
    b = store.get_row_brief(row["id"])
    assert b == {
        "id": row["id"], "memory_key": row["memory_key"],
        "content": row["content"], "source": row["source"],
    }
    assert store.get_row_brief(999999) is None
    assert store.get_row_brief("abc") is None


def test_route_offset_pagination(tmp_path):
    db = _mk_inbox_db(tmp_path)
    client = _build_app(tmp_path, db)
    p1 = client.get("/api/episodic-memory?limit=2").json()["items"]
    p2 = client.get("/api/episodic-memory?limit=2&offset=2").json()["items"]
    assert len(p1) == 2 and len(p2) == 1
    assert {x["id"] for x in p1} & {x["id"] for x in p2} == set()


def test_route_delete_writes_audit_with_content(tmp_path):
    """删除审计留痕：actor / action / row_id / 「谁的哪条记忆」摘要。"""
    db = _mk_inbox_db(tmp_path)
    calls = []

    class _Audit:
        def log(self, actor, action, target="", old_val="", new_val=""):
            calls.append({"actor": actor, "action": action,
                          "target": target, "old_val": old_val})

    client = _build_app(tmp_path, db, audit=_Audit())
    victim = client.get("/api/episodic-memory").json()["items"][0]
    r = client.delete(f"/api/episodic-memory/{victim['id']}")
    assert r.status_code == 200
    assert calls and calls[-1]["action"] == "episodic_delete"
    assert calls[-1]["actor"] == "tester"
    assert calls[-1]["target"] == str(victim["id"])
    assert victim["memory_key"] in calls[-1]["old_val"]
    assert victim["content"][:20] in calls[-1]["old_val"]
    # 行确实被删了
    left = {x["id"] for x in client.get("/api/episodic-memory").json()["items"]}
    assert victim["id"] not in left


def test_route_delete_404_writes_no_audit(tmp_path):
    db = _mk_inbox_db(tmp_path)
    calls = []

    class _Audit:
        def log(self, *a, **k):
            calls.append((a, k))

    client = _build_app(tmp_path, db, audit=_Audit())
    r = client.delete("/api/episodic-memory/999999")
    assert r.status_code == 404
    assert calls == []


# ── P3：管理者摘要（近 N 天新增/覆盖用户/Top-N + 身份富化）──────────────
# （并行线收敛记录 2026-08-02：摘要曾出现两套实现——列表路由 summary= 参数
#   vs 独立 /summary 端点；按「独立端点 + new_count/new_users/count 命名」
#   收敛，前端以「端点是否存在」做能力探测。）

def test_admin_summary_counts_and_top(tmp_path):
    store = _mk_store(tmp_path)
    store.add_fact("telegram:8244899900:8921664288", "用户养了一只猫", "pet")
    s = store.admin_summary(days=7, top_n=2)
    assert s["window_days"] == 7
    assert s["new_count"] == 4 and s["new_users"] == 3
    assert s["top"][0] == {
        "memory_key": "telegram:8244899900:8921664288", "count": 2,
    }
    assert len(s["top"]) == 2


def test_admin_summary_window_excludes_old(tmp_path):
    """时间窗按 epoch 数值比较的回归钉——created_at 是 REAL（生产实测），
    若谁改成字符串时间戳比较（TEXT 恒大于 REAL），老行会全被算成新增。"""
    store = _mk_store(tmp_path)
    store._conn.execute(
        "UPDATE episodic_memory SET created_at = ? WHERE user_id = ?",
        (time.time() - 30 * 86400, "999888"),
    )
    store._conn.commit()
    s = store.admin_summary(days=7)
    assert s["new_count"] == 2 and s["new_users"] == 2
    # 全库 Top 不受时间窗影响
    assert len(s["top"]) == 3


def test_route_summary_with_identity(tmp_path):
    db = _mk_inbox_db(tmp_path)
    client = _build_app(tmp_path, db)
    r = client.get("/api/episodic-memory/summary?days=7&top=3")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["new_count"] == 3 and d["new_users"] == 3
    by_key = {t["memory_key"]: t for t in d["top"]}
    assert by_key["telegram:8244899900:8921664288"]["identity"]["name"] == "张小明"


def test_route_summary_missing_inbox_db_degrades(tmp_path):
    client = _build_app(tmp_path, None)
    d = client.get("/api/episodic-memory/summary").json()
    assert d["ok"] is True and d["new_count"] == 3
    assert d["top"] and all("identity" not in t for t in d["top"])
