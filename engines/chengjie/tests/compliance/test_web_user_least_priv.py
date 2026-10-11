# -*- coding: utf-8 -*-
"""坐席最小权限 + 会话过期清理（智安 P1-6，2026-10-08）。

钉住：① 会话绝对寿命 SESSION_MAX_AGE_DAYS（天天 touch 也到期）；② 节流清扫返回条数并写审计；
③ cleanup_old_sessions 用本地时间口径；④ 新增 / 提升 admin 受 web_admin.max_admins 约束，
存量不动；⑤ 最小权限清单只读、不含密码 / 哈希；⑥ CLI 工具只读打开库。
"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from src.utils.web_user_store import (
    DEFAULT_MAX_ADMINS,
    ROLE_ADMIN,
    ROLE_AGENT,
    ROLE_MASTER,
    ROLE_VIEWER,
    WebUserStore,
    admin_cap_reached,
    least_privilege_report,
    max_admins_from_config,
)

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def store(tmp_path):
    return WebUserStore(tmp_path / "users.db")


def _ts(days_ago: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days_ago * 86400))


def _set(store, jti, *, created_days=None, seen_days=None):
    with store._lock:
        if created_days is not None:
            store._conn.execute("UPDATE web_sessions SET created_at=? WHERE jti=?", (_ts(created_days), jti))
        if seen_days is not None:
            store._conn.execute("UPDATE web_sessions SET last_seen=? WHERE jti=?", (_ts(seen_days), jti))
        store._conn.commit()


def test_session_absolute_max_age_rejects_even_if_active(store):
    j = store.create_session("admin", ROLE_MASTER, "127.0.0.1", "ua")
    store._last_sweep = time.time()            # 关掉清扫，单测 touch 路径
    _set(store, j, created_days=WebUserStore.SESSION_MAX_AGE_DAYS + 1, seen_days=0)
    assert store.touch_session(j) is False, "创建超 30 天的会话即使天天活跃也要求重登"
    with store._lock:
        assert store._conn.execute("SELECT revoked FROM web_sessions WHERE jti=?", (j,)).fetchone()[0] == 1


def test_fresh_session_still_valid(store):
    j = store.create_session("lily", ROLE_AGENT, "1.2.3.4", "ua")
    assert store.touch_session(j) is True


def test_expire_sweep_counts_and_audits(store):
    calls = []
    store.audit_fn = lambda actor, action, detail: calls.append((actor, action, detail))
    keep = store.create_session("a", ROLE_AGENT, "ip", "ua1")
    idle = store.create_session("a", ROLE_AGENT, "ip", "ua2")
    old = store.create_session("a", ROLE_AGENT, "ip", "ua3")
    gone = store.create_session("a", ROLE_AGENT, "ip", "ua4")
    _set(store, idle, seen_days=WebUserStore.SESSION_IDLE_DAYS + 1)
    _set(store, old, created_days=WebUserStore.SESSION_MAX_AGE_DAYS + 2, seen_days=1)
    _set(store, gone, created_days=60, seen_days=WebUserStore.SESSION_PRUNE_DAYS + 1)
    out = store.expire_sessions()
    assert out["idle"] >= 1 and out["max_age"] >= 1 and out["pruned"] == 1
    assert {s["jti"] for s in store.list_sessions()} == {keep}
    assert calls and calls[-1][1] == "session_expire_sweep" and calls[-1][0] == "system"
    assert "pruned=1" in calls[-1][2]
    # 无事可做 → 不写审计
    n = len(calls)
    assert store.expire_sessions() == {"idle": 0, "max_age": 0, "pruned": 0}
    assert len(calls) == n


def test_sweep_is_throttled_on_touch(store, monkeypatch):
    hits = []
    monkeypatch.setattr(store, "expire_sessions", lambda **k: hits.append(1) or {})
    j = store.create_session("a", ROLE_AGENT, "ip", "ua")
    store._last_sweep = 0.0
    store.touch_session(j)
    assert hits == [1]
    store._last_sweep = time.time()
    store.touch_session(j)
    store.touch_session(j)
    assert hits == [1], "SESSION_SWEEP_SEC 内不重复清扫"


def test_cleanup_old_sessions_uses_local_time(store):
    j = store.create_session("a", ROLE_AGENT, "ip", "ua")
    _set(store, j, seen_days=0.1)   # 2.4 小时前：UTC 口径在 UTC+8 机器上会被误删（days=0.2）
    store.cleanup_old_sessions(days=0.2)
    with store._lock:
        assert store._conn.execute("SELECT COUNT(*) FROM web_sessions").fetchone()[0] == 1
    src = (_ROOT / "src" / "utils" / "web_user_store.py").read_text(encoding="utf-8")
    body = src[src.index("def cleanup_old_sessions"):src.index("def count_admins")]
    assert "datetime('now'" not in body


def test_admin_cap_counts_enabled_admins_only(store):
    assert max_admins_from_config({}) == DEFAULT_MAX_ADMINS == 2
    assert max_admins_from_config({"web_admin": {"max_admins": 0}}) == 0
    assert max_admins_from_config({"web_admin": {"max_admins": "x"}}) == DEFAULT_MAX_ADMINS
    a1 = store.create_user("ad1", "secret123", ROLE_ADMIN)
    assert not admin_cap_reached(store, 2)
    a2 = store.create_user("ad2", "secret123", ROLE_ADMIN)
    assert admin_cap_reached(store, 2)
    assert not admin_cap_reached(store, 2, exclude_user_id=a2["id"]), "自己原本就是 admin 时不算新增"
    assert not admin_cap_reached(store, 0), "0=不限"
    store.update_user(a1["id"], enabled=False)
    assert not admin_cap_reached(store, 2), "禁用的 admin 不占名额"
    store.create_user("boss", "secret123", ROLE_MASTER)
    assert store.count_admins() == 1


def test_route_blocks_third_admin_but_allows_others(auth_client):
    def _create(name, role):
        return auth_client.post("/users/create",
                                data={"username": name, "password": "secure123", "role": role},
                                headers={"Accept": "application/json"})
    assert _create("adm_a", "admin").json().get("ok") is True
    assert _create("adm_b", "admin").json().get("ok") is True
    r = _create("adm_c", "admin")
    assert r.status_code == 403
    # 非 admin 角色不受影响；把普通账号提升为 admin 同样被拦
    v = _create("view_a", "viewer").json()
    assert v.get("ok") is True
    r2 = auth_client.post(f"/users/update/{v['id']}", data={"role": "admin"},
                          headers={"Accept": "application/json"})
    assert r2.status_code == 403
    r3 = auth_client.post(f"/users/update/{v['id']}", data={"role": "agent"},
                          headers={"Accept": "application/json"})
    assert r3.status_code == 200 and r3.json().get("role") == "agent"


def test_least_privilege_report_rules_and_no_secrets(store):
    for i, name in enumerate(("ad1", "ad2", "ad3", "ad4")):
        store.create_user(name, "secret123", ROLE_ADMIN)
    store.create_user("boss", "secret123", ROLE_MASTER)
    store.create_user("idle_agent", "secret123", ROLE_AGENT)
    store.create_user("viewer_x", "secret123", ROLE_VIEWER)
    # ad1/ad2 最近活跃，ad3/ad4 从未登录
    for n in ("ad1", "ad2", "viewer_x"):
        store.create_session(n, ROLE_ADMIN, "ip", "ua")
    for i in range(7):
        store.create_session("boss", ROLE_MASTER, "ip", f"ua{i}")
    uid = store.get_user("viewer_x")["id"]
    assert store.set_user_perms(uid, allow=["chat.send_text"], deny=[])
    rep = least_privilege_report(store, max_admins=2, stale_days=30)
    issues = {(f["username"], f["issue"]) for f in rep["findings"]}
    assert ("ad3", "admin_over_cap") in issues and ("ad4", "admin_over_cap") in issues
    assert ("ad1", "admin_over_cap") not in issues and ("ad2", "admin_over_cap") not in issues
    assert ("idle_agent", "stale_account") in issues
    assert ("boss", "too_many_sessions") in issues
    assert ("viewer_x", "perm_override_allow") in issues
    assert not any(f["username"] == "boss" and f["issue"] == "stale_account" for f in rep["findings"])
    assert rep["admins"] == 4 and rep["roles"][ROLE_ADMIN] == 4
    blob = json.dumps(rep, ensure_ascii=False)
    for bad in ("secret123", "password", "pw_hash", "salt", "jti"):
        assert bad not in blob


def test_report_is_read_only(store):
    store.create_user("ad1", "secret123", ROLE_ADMIN)
    store.create_user("ad2", "secret123", ROLE_ADMIN)
    store.create_user("ad3", "secret123", ROLE_ADMIN)
    before = [dict(u) for u in store.list_users()]
    least_privilege_report(store, max_admins=1)
    assert [dict(u) for u in store.list_users()] == before, "清单不得改动任何账号"


def test_cli_tool_opens_db_read_only(tmp_path):
    import importlib.util
    db = tmp_path / "web_users.db"
    s = WebUserStore(db)
    for n in ("ad1", "ad2", "ad3"):
        s.create_user(n, "secret123", ROLE_ADMIN)
    s._conn.close()
    mtime = db.stat().st_mtime_ns
    spec = importlib.util.spec_from_file_location("web_user_role_audit", _ROOT / "tools" / "web_user_role_audit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rep = mod.build_report(db, max_admins=2)
    assert rep["admins"] == 3
    assert any(f["issue"] == "admin_over_cap" for f in rep["findings"])
    assert db.stat().st_mtime_ns == mtime
    src = (_ROOT / "tools" / "web_user_role_audit.py").read_text(encoding="utf-8")
    assert "mode=ro" in src
    for w in ("UPDATE ", "DELETE ", "INSERT ", "DROP "):
        assert w not in src
    assert mod.main(["--db", str(db)]) == 0


def test_audit_labels_and_wiring():
    from src.web.i18n_packs import audit_actions
    for k in ("aud_act_session_expire_sweep", "aud_act_admin_cap_blocked"):
        assert k in audit_actions.ZH and k in audit_actions.EN
    admin_src = (_ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert "user_store.audit_fn = audit_store.log" in admin_src
