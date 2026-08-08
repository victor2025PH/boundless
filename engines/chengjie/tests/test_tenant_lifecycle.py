"""托管租户生命周期门禁（src/ops/tenant_lifecycle.py + scripts/tenant_ops.py 的纯函数层）。

重点守两条安全不变量：
1. 生产双实例（zhiliao/tongyi）绝不落入租户生命周期管辖（guard_not_core / is_tenant_service）；
2. 退租导出默认不带平台登录态与厂商授权（export_manifest 排除语义）。
全部用例 tmp_path 隔离，零仓库/生产写入。
"""
from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest

from src.ops import tenant_lifecycle as tl


# ────────────────────────── 识别 / 硬闸 ──────────────────────────

def _svc(sid: str, ports=None, args: str = "") -> dict:
    return {"id": sid, "ports": ports or [], "up": {"args": args}}


def test_core_services_are_not_tenants():
    stack = {"services": [
        _svc("chengjie"), _svc("chengjie_zhiliao", [18799, 18787]),
        _svc("chengjie_tongyi", [18899, 18887]),
        _svc("chengjie_zhiliao_acme", [18999, 18987]),
        _svc("website"),
    ]}
    tenants = tl.tenant_services(stack)
    assert [s["id"] for s in tenants] == ["chengjie_zhiliao_acme"]


def test_guard_not_core_rejects_production_instances():
    for bad in ("zhiliao", "tongyi", "ZHILIAO", " tongyi "):
        with pytest.raises(ValueError):
            tl.guard_not_core(bad)
    tl.guard_not_core("zhiliao_acme")  # 租户不抛


def test_instance_id_and_port_extraction():
    svc = _svc("chengjie_zhiliao_acme", [18999, 18987])
    assert tl.instance_id_of(svc) == "zhiliao_acme"
    assert tl.web_port_of_service(svc) == 18999
    assert tl.web_port_of_service(_svc("chengjie_x")) is None


def test_data_dir_parsed_from_up_args_with_convention_fallback():
    svc = _svc("chengjie_zhiliao_acme", [18999],
               args=r'-InstanceId zhiliao_acme -Port 18999 -DataDir "E:\custom\acme\data"')
    assert tl.data_dir_of_service(svc) == r"E:\custom\acme\data"
    svc2 = _svc("chengjie_zhiliao_acme", [18999], args="-InstanceId zhiliao_acme")
    assert tl.data_dir_of_service(svc2) == r"D:\chengjie-instances\zhiliao_acme\data"


# ────────────────────────── materialize ──────────────────────────

def test_materialize_creates_skeleton_and_is_idempotent(tmp_path: Path):
    example = tmp_path / "config.example.yaml"
    example.write_text("web_admin:\n  port: 18787\n", encoding="utf-8")
    data = tmp_path / "inst" / "data"

    acts1 = tl.materialize(str(data), "web_admin:\n  port: 18999\n", example)
    for sub in ("config", "sessions", "logs", "events/spool", "ledger_outbox"):
        assert (data / sub).is_dir(), sub
    assert (data / "config" / "config.yaml").read_text(encoding="utf-8").startswith("web_admin")
    assert (data / "config" / "config.local.yaml").exists()
    assert any("config.yaml" in a for a in acts1)

    # 幂等重跑：内容不被覆盖（写入哨兵值验证）
    (data / "config" / "config.yaml").write_text("SENTINEL", encoding="utf-8")
    (data / "config" / "config.local.yaml").write_text("SENTINEL2", encoding="utf-8")
    acts2 = tl.materialize(str(data), "OVERWRITE?", example)
    assert (data / "config" / "config.yaml").read_text(encoding="utf-8") == "SENTINEL"
    assert (data / "config" / "config.local.yaml").read_text(encoding="utf-8") == "SENTINEL2"
    assert any("跳过" in a for a in acts2)


def test_junction_command_shape(tmp_path: Path):
    cmd = tl.junction_command(str(tmp_path / "data"), r"D:\eng\domains")
    assert cmd[:4] == ["cmd", "/c", "mklink", "/J"]
    assert cmd[4].endswith("domains") and cmd[5] == r"D:\eng\domains"


# ────────────────────────── 进程目标判定 ──────────────────────────

def test_select_engine_pids_requires_mainpy_and_matching_cwd():
    data = r"D:\chengjie-instances\zhiliao_acme\data"
    cands = [
        tl.ProcDesc(1, r"python D:\boundless\engines\chengjie\main.py", data),   # ✓
        tl.ProcDesc(2, r"python main.py", data + r"\sub"),                        # ✓ 子目录
        tl.ProcDesc(3, r"python main.py", r"D:\chengjie-instances\zhiliao\data"),  # ✗ 别家 cwd
        tl.ProcDesc(4, r"node server.js", data),                                  # ✗ 非引擎
        tl.ProcDesc(5, r"cmd /c set AITR=1 && python main.py", ""),               # ✓ cwd 不可读回落
    ]
    assert tl.select_engine_pids(cands, data) == [1, 2, 5]


# ────────────────────────── 导出清单 ──────────────────────────

def _mk_tenant_data(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    (data / "config" / "presets").mkdir(parents=True)
    (data / "sessions").mkdir()
    (data / "logs").mkdir()
    (data / "config" / "inbox.db").write_bytes(b"db")
    (data / "config" / "config.yaml").write_text("x", encoding="utf-8")
    (data / "config" / "presets" / "p.yaml").write_text("y", encoding="utf-8")
    (data / "config" / "license.key").write_text("SECRET", encoding="utf-8")
    (data / "config" / "license_quota.db").write_bytes(b"q")
    (data / "config" / "old.bak").write_text("z", encoding="utf-8")
    (data / "sessions" / "acct.session").write_bytes(b"s")
    (data / "logs" / "app.log").write_text("log", encoding="utf-8")
    return data


def test_export_manifest_excludes_credentials_by_default(tmp_path: Path):
    data = _mk_tenant_data(tmp_path)
    arcs = {arc for _, arc in tl.export_manifest(str(data))}
    assert "config/inbox.db" in arcs
    assert "config/config.yaml" in arcs
    assert "config/presets/p.yaml" in arcs
    # 安全不变量：授权/计量/备份/登录态/日志默认全不带
    assert not any("license.key" in a for a in arcs)
    assert not any("license_quota" in a for a in arcs)
    assert not any(a.endswith(".bak") for a in arcs)
    assert not any(a.startswith("sessions/") for a in arcs)
    assert not any(a.startswith("logs/") for a in arcs)


def test_export_manifest_opt_ins(tmp_path: Path):
    data = _mk_tenant_data(tmp_path)
    arcs = {arc for _, arc in tl.export_manifest(str(data), include_sessions=True,
                                                 include_logs=True)}
    assert "sessions/acct.session" in arcs
    assert "logs/app.log" in arcs
    # opt-in 不放宽授权排除
    assert not any("license.key" in a for a in arcs)


def test_write_export_zip_roundtrip(tmp_path: Path):
    data = _mk_tenant_data(tmp_path)
    manifest = tl.export_manifest(str(data))
    out = tmp_path / "out" / "exp.zip"
    n = tl.write_export_zip(manifest, out)
    assert n == len(manifest) and out.exists()
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {arc for _, arc in manifest}


# ────────────────────────── 旗 / 交付卡 ──────────────────────────

def test_suspend_flag_write_and_path(tmp_path: Path):
    flag = tl.write_suspend_flag("zhiliao_acme", "overdue", ops_base=str(tmp_path))
    assert flag == tl.suspended_flag_path("zhiliao_acme", str(tmp_path))
    body = json.loads(flag.read_text(encoding="utf-8"))
    assert body["instance_id"] == "zhiliao_acme"
    assert body["reason"] == "overdue"
    assert "watchdog" in body["note"]


# ────────────────────────── 受保护租户（2026-08-07 pilot 事故沉淀）──────────────────────────

def test_protected_flag_roundtrip(tmp_path: Path):
    ob = str(tmp_path)
    assert tl.is_protected("zhiliao_pilot", ob) is False
    flag = tl.write_protected_flag("zhiliao_pilot", "坐席生产入口", ops_base=ob)
    assert flag == tl.protected_flag_path("zhiliao_pilot", ob)
    assert tl.is_protected("zhiliao_pilot", ob) is True
    assert tl.protected_reason("zhiliao_pilot", ob) == "坐席生产入口"
    assert tl.clear_protected_flag("zhiliao_pilot", ob) is True
    assert tl.is_protected("zhiliao_pilot", ob) is False
    assert tl.clear_protected_flag("zhiliao_pilot", ob) is False  # 幂等


def test_protected_reason_survives_corrupt_flag(tmp_path: Path):
    ob = str(tmp_path)
    flag = tl.protected_flag_path("t1", ob)
    flag.parent.mkdir(parents=True)
    flag.write_text("not-json{{{", encoding="utf-8")
    assert tl.is_protected("t1", ob) is True        # 旗在=保护在（宁可多拦）
    assert tl.protected_reason("t1", ob) == ""      # 原因读不出但不抛


def test_guard_not_protected_blocks_and_overrides(tmp_path: Path):
    ob = str(tmp_path)
    tl.guard_not_protected("t1", "suspend", ops_base=ob)  # 未保护：放行
    tl.write_protected_flag("t1", "prod", ops_base=ob)
    with pytest.raises(ValueError) as ei:
        tl.guard_not_protected("t1", "suspend", ops_base=ob)
    msg = str(ei.value)
    assert "受保护" in msg and "--force-protected" in msg and "suspend" in msg
    # 显式破玻璃放行
    tl.guard_not_protected("t1", "suspend", override=True, ops_base=ob)
    # resume 语义由调用方保证不闸——纯函数层对任意 action 一视同仁，此处只验 override 通道


def test_is_drill_instance_namespace():
    # 演练件三种命名位任一命中即豁免（交付即保护的反向闸）
    assert tl.is_drill_instance("zhiliao_drill_e2e") is True
    assert tl.is_drill_instance("zhiliao_x", customer="drill e2e") is True
    assert tl.is_drill_instance("zhiliao_x", slug="drill-e2e") is True
    assert tl.is_drill_instance("zhiliao_x", customer="DRILL run") is True   # 大小写不敏感
    # 真客户不豁免
    assert tl.is_drill_instance("zhiliao_pilot", customer="pilot", slug="pilot") is False
    assert tl.is_drill_instance("zhiliao_acme", customer="Acme Ltd", slug="acme") is False


def test_tenant_card_fields_and_path():
    plan = {"instance_id": "zhiliao_acme", "service_id": "chengjie_zhiliao_acme",
            "product": "zhiliao", "customer": "Acme", "web_port": 18999,
            "data_dir": r"D:\chengjie-instances\zhiliao_acme\data"}
    card = tl.build_tenant_card(plan, "tok123", status="running")
    assert card["workspace_url"] == "http://127.0.0.1:18999/workspace/dash"
    assert card["start_here_url"] == "http://127.0.0.1:18999/workspace/golive"
    assert card["login_url"] == "http://127.0.0.1:18999/login?next=/workspace/dash"
    assert card["auth_token"] == "tok123"
    # 无 owner 时回落 admin+token 旧口径（向后兼容）
    assert card["username"] == "admin"
    assert card["initial_password"] == "tok123"
    assert card["status"] == "running"
    assert tl.tenant_card_path(plan["data_dir"]) == \
        Path(r"D:\chengjie-instances\zhiliao_acme") / "tenant_card.json"


def test_tenant_card_credential_separation():
    """凭据职责分离：交付卡给 owner 账号，auth_token 标为仅运维。"""
    plan = {"instance_id": "zhiliao_acme", "web_port": 18999}
    card = tl.build_tenant_card(plan, "OPS-TOKEN", status="running",
                                owner_user="owner", owner_password="cust-pw")
    assert card["username"] == "owner"
    assert card["initial_password"] == "cust-pw"
    assert card["auth_token"] == "OPS-TOKEN"        # 卡上留存（本机运维文件）
    assert "auth_token" in card["ops_only"]          # 但显式标记不外发
    assert "运维" in card["note"]


def test_delivery_code_never_ships_ops_token():
    """安全不变量：交付串只含客户账号凭据，运维令牌绝不出现。"""
    from src.ops import tenant_fulfillment as tf
    code = tf.build_delivery_code("https://acme.bd2026.cc", "cust-pw",
                                  "zhiliao_acme", username="owner")
    assert "owner" in code and "cust-pw" in code
    assert "OPS-TOKEN" not in code
    assert "上线自检" in code
    assert "login?next=/workspace/dash" in code  # 首登深链直达自检看板


def test_create_owner_account_idempotent_and_isolated(tmp_path: Path):
    data = tmp_path / "data"
    _seed_web_users_db(data, "ADMIN-TOKEN")          # 模拟引擎首启播种的 admin
    created, pw = tl.create_owner_account(str(data))
    assert created is True and len(pw) >= 8
    # owner 用新密码可验、admin 不受影响（两条凭据互不相干）
    assert tl.initial_password_unchanged(str(data), pw, username="owner") is True
    assert tl.initial_password_unchanged(str(data), "ADMIN-TOKEN", username="admin") is True
    assert tl.initial_password_unchanged(str(data), pw, username="admin") is False
    # 幂等：已存在不重建、不重置密码
    again, pw2 = tl.create_owner_account(str(data))
    assert again is False and pw2 == ""
    assert tl.initial_password_unchanged(str(data), pw, username="owner") is True


def test_create_owner_account_requires_started_instance(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        tl.create_owner_account(str(tmp_path / "never-started"))


def _seed_web_users_db(data_dir: Path, password: str) -> None:
    """照引擎 web_user_store 的口径建一个 admin（pbkdf2-sha256 100k，salt+hash 分列）。

    表结构必须与引擎真表一致（含 display_name/lang/created_at/last_login/enabled）——
    create_owner_account 复用的是引擎自己的 WebUserStore，简化 schema 会 OperationalError。
    """
    import hashlib
    import os
    import sqlite3
    cfg = data_dir / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    con = sqlite3.connect(cfg / "web_users.db")
    con.execute(
        "CREATE TABLE web_users ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " username TEXT UNIQUE NOT NULL,"
        " pw_salt BLOB NOT NULL,"
        " pw_hash BLOB NOT NULL,"
        " role TEXT NOT NULL,"
        " display_name TEXT DEFAULT '',"
        " lang TEXT DEFAULT '',"
        " created_at TEXT DEFAULT '',"
        " last_login TEXT DEFAULT '',"
        " enabled INTEGER DEFAULT 1)")
    con.execute("INSERT INTO web_users (username, pw_salt, pw_hash, role) VALUES"
                " ('admin', ?, ?, 'master')", (salt, dk))
    con.commit()
    con.close()


def test_initial_password_detection_readonly(tmp_path: Path):
    data = tmp_path / "data"
    # 仍是初始密码 → True
    _seed_web_users_db(data, "TOK-123")
    assert tl.initial_password_unchanged(str(data), "TOK-123") is True    # 客户改过 → False
    data2 = tmp_path / "data2"
    _seed_web_users_db(data2, "my-own-pw")
    assert tl.initial_password_unchanged(str(data2), "TOK-123") is False
    # 判不了的三种：无库 / 无 token
    assert tl.initial_password_unchanged(str(tmp_path / "nope"), "TOK-123") is None
    assert tl.initial_password_unchanged(str(data), "") is None


def test_initial_password_check_does_not_write(tmp_path: Path):
    """只读不变量：检测绝不能碰租户活库（verify() 会写 last_login，故刻意不用它）。"""
    data = tmp_path / "data"
    _seed_web_users_db(data, "TOK-123")
    db = data / "config" / "web_users.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    for _ in range(3):
        tl.initial_password_unchanged(str(data), "TOK-123")
    after = (db.stat().st_mtime_ns, db.stat().st_size)
    assert before == after
    assert not (data / "config" / "web_users.db-wal").exists()  # 未开写事务


# ────────────────────────── watch 决策 ──────────────────────────

def _fact(iid="t1", desired=True, suspended=False, ok=False):
    return tl.TenantFact(instance_id=iid, desired_running=desired,
                         suspended=suspended, http_ok=ok)


def test_watch_skips_suspended_and_not_desired():
    decisions, state = tl.plan_watch_actions(
        [_fact("a", suspended=True), _fact("b", desired=False)], {}, now=1000.0)
    assert [(d["action"], d["reason"]) for d in decisions] == [
        ("skip", "suspended"), ("skip", "not_desired_running")]
    assert state == {}  # 跳过态不留 streak


def test_watch_ok_resets_streak():
    decisions, state = tl.plan_watch_actions(
        [_fact(ok=True)], {"t1": {"fail_streak": 2, "last_heal": 900.0}}, now=1000.0)
    assert decisions[0]["action"] == "ok"
    assert state["t1"]["fail_streak"] == 0


def test_watch_down_heals_and_increments_streak():
    decisions, state = tl.plan_watch_actions([_fact()], {}, now=1000.0)
    assert decisions[0]["action"] == "heal"
    assert state["t1"] == {"fail_streak": 1, "last_heal": 1000.0}


def test_watch_cooldown_window_blocks_reheal():
    st = {"t1": {"fail_streak": 1, "last_heal": 950.0}}
    decisions, state = tl.plan_watch_actions([_fact()], st, now=1000.0,
                                             heal_cooldown_sec=600)
    assert decisions[0]["action"] == "cooldown"
    assert state["t1"]["fail_streak"] == 1  # 冷却期不涨计数
    # 冷却窗过了 → 再 heal
    decisions2, state2 = tl.plan_watch_actions([_fact()], st, now=2000.0,
                                               heal_cooldown_sec=600)
    assert decisions2[0]["action"] == "heal"
    assert state2["t1"]["fail_streak"] == 2


def test_watch_gives_up_after_max_streak():
    st = {"t1": {"fail_streak": 3, "last_heal": 0.0}}
    decisions, state = tl.plan_watch_actions([_fact()], st, now=9999.0,
                                             max_fail_streak=3)
    assert decisions[0]["action"] == "give_up"
    assert state["t1"]["fail_streak"] == 3  # 不再增长，等人工 reset


def test_watch_state_roundtrip(tmp_path: Path):
    p = tmp_path / "state.json"
    assert tl.load_watch_state(p) == {}  # 缺文件从零
    tl.save_watch_state(p, {"t1": {"fail_streak": 1, "last_heal": 5.0}})
    assert tl.load_watch_state(p)["t1"]["fail_streak"] == 1
    p.write_text("not json", encoding="utf-8")
    assert tl.load_watch_state(p) == {}  # 坏文件从零（watch 状态可再生）


# ────────────────────────── 边缘探针决策（公网入口整链）──────────────────────────

def _edge(iid="t1", ok=False):
    return tl.EdgeFact(instance_id=iid, edge_ok=ok)


def test_edge_ok_resets_streak():
    st = {tl.EDGE_STATE_KEY: {"t1": {"streak": 3}, "_last_kick": 100.0}}
    decisions, edge = tl.plan_edge_actions([_edge(ok=True)], st, now=1000.0)
    assert decisions[0]["action"] == "ok"
    assert edge["t1"]["streak"] == 0
    assert edge["_last_kick"] == 100.0  # 冷却戳保留


def test_edge_first_fail_is_strike_not_action():
    decisions, edge = tl.plan_edge_actions([_edge()], {}, now=1000.0, strike_limit=2)
    assert decisions[0]["action"] == "strike"
    assert edge["t1"]["streak"] == 1


def test_edge_strike_limit_kicks_tunnel_once_per_round():
    st = {tl.EDGE_STATE_KEY: {"t1": {"streak": 1}, "t2": {"streak": 1}}}
    decisions, edge = tl.plan_edge_actions(
        [_edge("t1"), _edge("t2")], st, now=1000.0, strike_limit=2)
    acts = [d["action"] for d in decisions]
    # 隧道全租户共享：同轮两家都到阈也只踢一次，另一家转 alert
    assert acts.count("kick_tunnel") == 1
    assert acts.count("alert") == 1
    assert edge["_last_kick"] == 1000.0


def test_edge_kick_cooldown_escalates_to_alert():
    st = {tl.EDGE_STATE_KEY: {"t1": {"streak": 5}, "_last_kick": 900.0}}
    decisions, edge = tl.plan_edge_actions(
        [_edge()], st, now=1000.0, strike_limit=2, kick_cooldown_sec=1200)
    assert decisions[0]["action"] == "alert"          # 冷却内不再踢，升级告警
    assert edge["_last_kick"] == 900.0
    # 冷却过了 → 允许再踢
    decisions2, edge2 = tl.plan_edge_actions(
        [_edge()], st, now=3000.0, strike_limit=2, kick_cooldown_sec=1200)
    assert decisions2[0]["action"] == "kick_tunnel"
    assert edge2["_last_kick"] == 3000.0


def test_edge_recovery_then_new_outage_recounts():
    # 恢复清零后再次失败：从 strike 重新数起（不是直接 kick）
    _, edge = tl.plan_edge_actions([_edge(ok=True)], {tl.EDGE_STATE_KEY: {"t1": {"streak": 4}}}, now=1000.0)
    decisions, _ = tl.plan_edge_actions([_edge()], {tl.EDGE_STATE_KEY: edge}, now=2000.0, strike_limit=2)
    assert decisions[0]["action"] == "strike"


# ────────────────────────── 备份 ──────────────────────────

def test_snapshot_sqlite_live_wal(tmp_path: Path):
    import sqlite3
    src = tmp_path / "live.db"
    con = sqlite3.connect(src)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (42)")
    con.commit()  # 连接保持打开=模拟活库
    snap = tmp_path / "snap" / "live.db"
    tl.snapshot_sqlite(src, snap)
    con.close()
    out = sqlite3.connect(snap)
    assert out.execute("SELECT x FROM t").fetchone() == (42,)
    out.close()


def test_backup_zip_includes_credentials_and_snapshots(tmp_path: Path):
    import sqlite3
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "sessions").mkdir()
    con = sqlite3.connect(data / "config" / "inbox.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE m(v)")
    con.execute("INSERT INTO m VALUES ('hi')")
    con.commit()
    con.close()
    (data / "config" / "license.key").write_text("LIC", encoding="utf-8")
    (data / "config" / "config.yaml").write_text("a: 1", encoding="utf-8")
    (data / "sessions" / "acct.session").write_bytes(b"s")

    out = tmp_path / "bk.zip"
    counts = tl.backup_tenant_zip(str(data), out)
    assert counts["db_snapshots"] == 1
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
    # 灾备口径与 export 相反：授权与登录态必须在内（恢复接待必需）
    assert {"config/inbox.db", "config/license.key", "config/config.yaml",
            "sessions/acct.session"} <= names
    assert not any(n.endswith((".db-wal", ".db-shm")) for n in names)


def test_prune_old_backups_keeps_newest(tmp_path: Path):
    for ts in ("20260801_010101", "20260802_010101", "20260803_010101"):
        (tmp_path / f"t1_{ts}.zip").write_bytes(b"z")
    doomed = tl.prune_old_backups(tmp_path, keep=2)
    assert [p.name for p in doomed] == ["t1_20260801_010101.zip"]
    assert tl.prune_old_backups(tmp_path, keep=0) == []  # keep=0 视为不清理


# ────────────────────────── 灾备恢复（DR 闭环）──────────────────────────

def _mk_backup_zip(path: Path, extra: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("config/inbox.db", b"DBDATA")
        zf.writestr("config/config.yaml", "a: 1")
        zf.writestr("sessions/acct.session", b"S")
        for arc, data in (extra or {}).items():
            zf.writestr(arc, data)


def test_validate_backup_zip_rejects_bad(tmp_path: Path):
    good = tmp_path / "good.zip"
    _mk_backup_zip(good)
    assert any(n.startswith("config/") for n in tl.validate_backup_zip(good))
    # 无 config/ → 拒
    nocfg = tmp_path / "nocfg.zip"
    with zipfile.ZipFile(nocfg, "w") as zf:
        zf.writestr("sessions/x", b"y")
    with pytest.raises(ValueError):
        tl.validate_backup_zip(nocfg)
    # zip-slip → 拒
    slip = tmp_path / "slip.zip"
    with zipfile.ZipFile(slip, "w") as zf:
        zf.writestr("config/ok", b"1")
        zf.writestr("../../evil.txt", b"pwn")
    with pytest.raises(ValueError):
        tl.validate_backup_zip(slip)
    with pytest.raises(FileNotFoundError):
        tl.validate_backup_zip(tmp_path / "nope.zip")


def test_restore_backup_roundtrip_and_overwrite(tmp_path: Path):
    zip_path = tmp_path / "bk.zip"
    _mk_backup_zip(zip_path)
    data = tmp_path / "data"
    # 现场存在损坏内容 → 恢复应覆盖成备份版本
    (data / "config").mkdir(parents=True)
    (data / "config" / "inbox.db").write_bytes(b"CORRUPT")
    counts = tl.restore_backup(zip_path, str(data))
    assert counts["config"] == 2 and counts["sessions"] == 1
    assert (data / "config" / "inbox.db").read_bytes() == b"DBDATA"  # 被备份覆盖
    assert (data / "sessions" / "acct.session").read_bytes() == b"S"


def test_latest_backup_picks_newest(tmp_path: Path):
    d = tl.backups_dir("t1", ops_base=str(tmp_path))
    assert tl.latest_backup("t1", ops_base=str(tmp_path)) is None
    for ts in ("20260801_000000", "20260803_000000", "20260802_000000"):
        _mk_backup_zip(d / f"t1_{ts}.zip")
    assert tl.latest_backup("t1", ops_base=str(tmp_path)).name == "t1_20260803_000000.zip"


# ────────────────────────── AI 预设注入 ──────────────────────────

def test_load_ai_preset_missing_or_bad_is_empty(tmp_path: Path):
    assert tl.load_ai_preset(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  - broken", encoding="utf-8")
    assert tl.load_ai_preset(bad) == {}
    no_ai = tmp_path / "noai.yaml"
    no_ai.write_text("other: 1", encoding="utf-8")
    assert tl.load_ai_preset(no_ai) == {}


def test_inject_ai_preset_preserves_comments(tmp_path: Path):
    overlay = tmp_path / "config.local.yaml"
    overlay.write_text(
        "# 运维注释：这行绝不能丢\n"
        "web_admin:\n"
        "  port: 18999   # 端口注释\n",
        encoding="utf-8")
    keys = tl.inject_ai_preset(overlay, {"api_key": "k123456789", "model": "deepseek-chat"})
    assert set(keys) == {"api_key", "model"}
    body = overlay.read_text(encoding="utf-8")
    assert "# 运维注释：这行绝不能丢" in body
    assert "# 端口注释" in body
    assert "api_key: k123456789" in body and "model: deepseek-chat" in body
    # 幂等改值不重复建块
    tl.inject_ai_preset(overlay, {"model": "qwen3"})
    body2 = overlay.read_text(encoding="utf-8")
    assert body2.count("ai:") == 1 and "model: qwen3" in body2


def test_mask_secret():
    assert tl.mask_secret("sk-abcdefg12345") == "***2345"
    assert tl.mask_secret("short") == "***"
    assert tl.mask_secret("") == "***"


# ────────────────── 多段租户预设（2026-08-08 托管能力供给）──────────────────

def test_load_tenant_preset_whitelists_sections(tmp_path: Path):
    p = tmp_path / "preset.yaml"
    p.write_text(
        "ai:\n  api_key: k1\n"
        "licensing:\n  hosted_ai:\n    enabled: true\n"
        "platform_login:\n  enabled: true\n"
        "web_admin:\n  auth_token: LEAK-ME\n"      # 白名单外段必须被丢弃
        "monitoring:\n  enabled: true\n",
        encoding="utf-8")
    preset = tl.load_tenant_preset(p)
    assert set(preset) == {"ai", "licensing", "platform_login"}
    assert "web_admin" not in preset and "monitoring" not in preset
    # 缺文件/坏文件 → {}
    assert tl.load_tenant_preset(tmp_path / "nope.yaml") == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text(":\n  - broken", encoding="utf-8")
    assert tl.load_tenant_preset(bad) == {}


def test_inject_tenant_preset_deep_write_preserves_comments(tmp_path: Path):
    overlay = tmp_path / "config.local.yaml"
    overlay.write_text(
        "# 运维注释：这行绝不能丢\n"
        "web_admin:\n"
        "  port: 18999   # 端口注释\n",
        encoding="utf-8")
    preset = {
        "licensing": {"hosted_ai": {"enabled": True}},
        "platform_login": {
            "enabled": True,
            "telegram": {"credpool": {"enabled": True, "license_key": "CHATX-X-1234"}},
        },
    }
    keys = tl.inject_tenant_preset(overlay, preset)
    assert "licensing.hosted_ai.enabled" in keys
    assert "platform_login.telegram.credpool.license_key" in keys
    body = overlay.read_text(encoding="utf-8")
    assert "# 运维注释：这行绝不能丢" in body and "# 端口注释" in body
    assert "license_key: CHATX-X-1234" in body
    # 幂等重写不重复建块
    tl.inject_tenant_preset(overlay, preset)
    assert overlay.read_text(encoding="utf-8").count("platform_login:") == 1


def test_ensure_pool_license_issue_reuse_and_softfail(tmp_path: Path):
    db = tmp_path / "matrixx.db"
    import sqlite3
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE NOT NULL,
        level TEXT, status TEXT, machine_id TEXT, expires_at TEXT)""")
    con.commit()
    con.close()
    k1 = tl.ensure_pool_license("zhiliao_acme_ltd", db_path=db)
    assert k1.startswith("CHATX-ZHILIAO-ACME-LTD-") and len(k1.split("-")[-1]) == 4
    # 幂等：同实例复用同一张卡
    assert tl.ensure_pool_license("zhiliao_acme_ltd", db_path=db) == k1
    # 池库缺失 → 软失败返回 ""
    assert tl.ensure_pool_license("zhiliao_x", db_path=tmp_path / "absent.db") == ""


def test_resolve_preset_license_auto_and_passthrough(tmp_path: Path):
    db = tmp_path / "matrixx.db"
    import sqlite3
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT, license_key TEXT UNIQUE NOT NULL,
        level TEXT, status TEXT, machine_id TEXT, expires_at TEXT)""")
    con.commit()
    con.close()

    def mk_preset(lic):
        return {"platform_login": {"telegram": {"credpool": {
            "enabled": True, "license_key": lic}}}}

    # AUTO → 签发并替换
    p = mk_preset("AUTO")
    key = tl.resolve_preset_license(p, "zhiliao_t1", db_path=db)
    assert key and p["platform_login"]["telegram"]["credpool"]["license_key"] == key
    # 显式卡密 → 原样保留
    p2 = mk_preset("CHATX-FIXED-0001")
    assert tl.resolve_preset_license(p2, "zhiliao_t1", db_path=db) == "CHATX-FIXED-0001"
    assert p2["platform_login"]["telegram"]["credpool"]["license_key"] == "CHATX-FIXED-0001"
    # AUTO 但池库缺失 → 键被删（free 档兜底），不留 AUTO 字面值
    p3 = mk_preset("AUTO")
    assert tl.resolve_preset_license(p3, "zhiliao_t1", db_path=tmp_path / "absent.db") == ""
    assert "license_key" not in p3["platform_login"]["telegram"]["credpool"]
    # 无 credpool 段 → 无操作
    assert tl.resolve_preset_license({"ai": {"api_key": "k"}}, "zhiliao_t1", db_path=db) == ""


# ────────────────────────── 公网暴露 ──────────────────────────

def test_validate_slug_rules():
    assert tl.validate_slug("Zhiliao-Pilot ") == "zhiliao-pilot"
    for bad in ("", "-lead", "trail-", "under_score", "a" * 41, "有中文"):
        with pytest.raises(ValueError):
            tl.validate_slug(bad)
    for reserved in ("www", "api", "console", "releases"):
        with pytest.raises(ValueError):
            tl.validate_slug(reserved)
    assert tl.default_slug("zhiliao_pilot") == "zhiliao-pilot"


def test_render_nginx_site_directives():
    conf = tl.render_nginx_site("acme", 18999)
    assert "server_name acme.bd2026.cc;" in conf
    assert "proxy_pass http://127.0.0.1:18999;" in conf
    assert "/etc/letsencrypt/live/acme.bd2026.cc/fullchain.pem" in conf
    # SSE/WebSocket/媒体三件套（工作台实时流的生死线）
    assert "proxy_buffering off;" in conf
    assert 'proxy_set_header Connection "upgrade";' in conf
    assert "client_max_body_size 50m;" in conf
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in conf
    # P5：后端不可达 → 友好页（到期被停的客户看到续费引导而非裸 502）
    assert "error_page 502 503 504 /__tenant_down.html;" in conf
    assert "location = /__tenant_down.html" in conf


def test_render_nginx_site_port_mode():
    conf = tl.render_nginx_site_port("acme", 18987, 18999)
    assert "listen 18987 ssl;" in conf
    assert "server_name bd2026.cc;" in conf
    # 端口形态复用主域证书（SAN 已含主域，零 DNS 依赖）
    assert "/etc/letsencrypt/live/bd2026.cc/fullchain.pem" in conf
    assert "proxy_pass http://127.0.0.1:18999;" in conf
    # 反代主体与子域形态同源（SSE/WS/媒体语义一致，友好页同享）
    assert "proxy_buffering off;" in conf and "client_max_body_size 50m;" in conf
    assert "error_page 502 503 504 /__tenant_down.html;" in conf


def test_ensure_tunnel_port_idempotent(tmp_path: Path):
    assert tl.ensure_tunnel_port(18999, ops_base=str(tmp_path)) is True
    assert tl.ensure_tunnel_port(18999, ops_base=str(tmp_path)) is False  # 幂等
    assert tl.ensure_tunnel_port(19099, ops_base=str(tmp_path)) is True
    body = tl.tunnel_ports_file(str(tmp_path)).read_text(encoding="utf-8")
    assert body.splitlines()[0].startswith("#")  # 头注释保留
    assert body.count("18999") == 1 and "19099" in body


# ────────────────────────── 托管开通守护（决策纯函数）──────────────────────────

def test_hosted_order_never_matches_installed_skus():
    """安全不变量：现有装机 SKU 一律不被认作托管单（否则会给装机客户误开实例）。"""
    from src.ops import tenant_fulfillment as tf
    for sku in ("chatx-entry", "chatx-team", "chatx-flagship",
                "lingox-team", "lingox-pro", "lingox-charpack"):
        assert tf.is_hosted_order({"sku_id": sku, "contact": "x@y.com"}) is False, sku


def test_hosted_order_recognizes_explicit_signals():
    from src.ops import tenant_fulfillment as tf
    assert tf.is_hosted_order({"sku_id": "chatx-team", "delivery": "hosted"}) is True
    assert tf.is_hosted_order({"sku_id": "chatx-hosted-team"}) is True
    assert tf.is_hosted_order({"sku_id": "hosted-pro"}) is True
    assert tf.is_hosted_order({"delivery": "HOSTED"}) is True  # 大小写不敏感
    assert tf.is_hosted_order({"sku_id": "unhosted-thing"}) is False  # 子串不算，要独立 token


def test_hosted_plan_args_and_product_routing():
    from src.ops import tenant_fulfillment as tf
    a = tf.hosted_plan_args_for_order(
        {"id": "O1", "sku_id": "chatx-hosted-team", "contact": "Acme Ltd"})
    assert a["product"] == "zhiliao"
    assert a["order_id"] == "O1" and a["customer"] == "Acme Ltd"
    assert a["slug"] == "acme-ltd" and a["instance_id"] == "zhiliao_acme_ltd"
    # lingox 路由
    b = tf.hosted_plan_args_for_order(
        {"id": "O2", "sku_id": "lingox-hosted-pro", "contact": "b@x.com", "slug": "BravoCo"})
    assert b["product"] == "tongyi" and b["slug"] == "bravoco"
    # 缺 contact → None（转人工，绝不裸开）
    assert tf.hosted_plan_args_for_order(
        {"id": "O3", "sku_id": "chatx-hosted-team", "contact": ""}) is None


def test_select_hostable_skips_done_and_installed():
    from src.ops import tenant_fulfillment as tf
    orders = [
        {"id": "A1", "sku_id": "chatx-team", "contact": "x@y.com"},              # 装机→跳
        {"id": "A2", "sku_id": "chatx-hosted-team", "contact": "a@y.com"},       # 托管→选
        {"id": "A3", "delivery": "hosted", "contact": "b@y.com", "code": "已发"},  # 已回填→跳
        {"id": "A4", "delivery": "hosted", "contact": "c@y.com"},                 # 托管→选
    ]
    picked = [o["id"] for o, _ in tf.select_hostable(orders, done_ids={"A2"})]
    assert picked == ["A4"]  # A1装机 A2done A3已回填 都被排除
    picked2 = [o["id"] for o, _ in tf.select_hostable(orders, done_ids=set())]
    assert picked2 == ["A2", "A4"]


def test_build_delivery_code_contains_url_and_token():
    from src.ops import tenant_fulfillment as tf
    code = tf.build_delivery_code("https://acme.bd2026.cc", "tok123", "zhiliao_acme")
    assert "https://acme.bd2026.cc" in code and "tok123" in code and "zhiliao_acme" in code


def test_resolve_catalog_sku_strips_hosted_marker():
    from src.ops import tenant_fulfillment as tf
    assert tf.resolve_catalog_sku("chatx-hosted-team") == "chatx-team"
    assert tf.resolve_catalog_sku("chatx-team") == "chatx-team"
    assert tf.resolve_catalog_sku("lingox-hosted-pro") == "lingox-pro"


def test_gw_daily_chars_by_sku_tier():
    from src.ops import tenant_fulfillment as tf
    # entry 3×25k=75k → basic floor 100k
    assert tf.gw_daily_chars_for_order(
        {"sku_id": "chatx-entry", "delivery": "hosted"}) == 100_000
    # team 10×25k=250k（= pro floor）
    assert tf.gw_daily_chars_for_order(
        {"sku_id": "chatx-hosted-team"}) == 250_000
    # flagship 50×25k=1.25M（夹在 800k..1.5M）
    assert tf.gw_daily_chars_for_order(
        {"sku_id": "chatx-flagship", "delivery": "hosted"}) == 1_250_000
    # lingox-team 月包 3M → 日 100k（= basic floor，3M/30=100k）
    assert tf.gw_daily_chars_for_order(
        {"sku_id": "lingox-team", "delivery": "hosted"}) == 100_000
    # 未知付费托管 → 默认 200k（高于试用 50k）
    assert tf.gw_daily_chars_for_order(
        {"sku_id": "chatx-hosted-ultra", "delivery": "hosted"}) == 200_000


def test_gw_budget_for_order_subject_and_none_paths():
    from src.ops import tenant_fulfillment as tf
    spec = tf.gw_budget_for_order(
        {"id": "O9", "sku_id": "chatx-team", "delivery": "hosted", "period": "annual"},
        "zhiliao_acme")
    assert spec["subject"] == "IID:zhiliao_acme"
    assert spec["budget"] == 250_000
    assert "order=O9" in spec["note"] and "annual" in spec["note"]
    assert tf.gw_budget_for_order(
        {"sku_id": "chatx-team"}, "zhiliao_acme") is None  # 装机不提托管额度
    assert tf.gw_budget_for_order(
        {"sku_id": "chatx-team", "delivery": "hosted"}, "") is None


def test_should_auto_suspend_rails():
    """到期自动停机的安全轨：默认关/宽限/保护/订单联动缺失/非运行态 全部拒停。"""
    now = 1_700_000_000.0
    exp_5d_ago = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 5 * 86400))
    card = {"status": "running", "expires_at": exp_5d_ago, "expiry_order": "O1"}

    # 默认关（grace_days=0）
    assert tl.should_auto_suspend(card, now, grace_days=0) == (False, "disabled")
    # 开启 + 逾期超宽限 → 停
    act, why = tl.should_auto_suspend(card, now, grace_days=3)
    assert act is True and "O1" in why
    # 宽限内不停（逾期 5 天 < 宽限 7 天）
    act, why = tl.should_auto_suspend(card, now, grace_days=7)
    assert act is False and why.startswith("in_grace")
    # 受保护绝不自动停
    assert tl.should_auto_suspend(card, now, grace_days=3, protected=True) == (
        False, "protected")
    # 已停不重复处置
    assert tl.should_auto_suspend(card, now, grace_days=3, suspended=True) == (
        False, "already_suspended")
    # 手工卡（无 expiry_order）没有续费→复机联动，只告警不自动停
    manual = dict(card)
    manual.pop("expiry_order")
    assert tl.should_auto_suspend(manual, now, grace_days=3) == (False, "no_order_link")
    # 非运行态（已停机/启动失败）不碰
    assert tl.should_auto_suspend(
        {**card, "status": "suspended"}, now, grace_days=3) == (False, "not_running")
    # 未到期不停
    future = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 30 * 86400))
    assert tl.should_auto_suspend(
        {**card, "expires_at": future}, now, grace_days=3)[0] is False
    # 无到期账本（试点/参考件）恒不停
    assert tl.should_auto_suspend(
        {"status": "running", "expiry_order": "O1"}, now, grace_days=3)[0] is False


def test_merge_preserved_card_fields():
    """provision 幂等重入的交付卡合并：账本/公网字段绝不被整卡重写抹除（P6 实锤）。"""
    old = {
        "expires_at": "2026-09-09 20:00:00", "expires_days": 32,
        "expiry_order": "AH-1", "last_fulfill_kind": "activate",
        "last_order_sku": "chatx-team", "ai_daily_chars": 250000,
        "ai_budget_subject": "IID:zhiliao_x", "slug": "acme",
        "exposed": True, "expose_mode": "subdomain", "protected": True,
        "public_url": "https://acme.bd2026.cc",
        "workspace_url": "https://acme.bd2026.cc/workspace/dash",
        "login_url": "https://acme.bd2026.cc/login?next=/workspace/dash",
        "start_here_url": "https://acme.bd2026.cc/workspace/golive",
        "initial_password": "old-pw",
    }
    new = {
        "instance_id": "zhiliao_x", "status": "running", "username": "owner",
        "initial_password": "new-pw", "auth_token": "tok",
        "workspace_url": "http://127.0.0.1:18999/workspace/dash",
        "login_url": "http://127.0.0.1:18999/login?next=/workspace/dash",
    }
    m = tl.merge_preserved_card_fields(new, old)
    # 账本/暴露状态全保留
    assert m["expires_at"] == "2026-09-09 20:00:00" and m["expiry_order"] == "AH-1"
    assert m["last_order_sku"] == "chatx-team" and m["ai_daily_chars"] == 250000
    assert m["exposed"] is True and m["protected"] is True and m["slug"] == "acme"
    # 公网 URL 族整组回填（不许「public 是公网、login 是 127.0.0.1」的分裂卡）
    assert m["public_url"] == "https://acme.bd2026.cc"
    assert m["login_url"].startswith("https://acme.bd2026.cc/")
    # 本次 provision 的凭据/状态以新卡为准
    assert m["initial_password"] == "new-pw" and m["status"] == "running"
    # 首开（无旧卡）＝新卡原样
    assert tl.merge_preserved_card_fields(new, {}) == new
    # 旧卡未暴露过 → 不回填 URL（保留 provision 的本地 URL）
    m2 = tl.merge_preserved_card_fields(new, {"expires_at": "2026-01-01 00:00:00"})
    assert m2["login_url"].startswith("http://127.0.0.1")
    assert m2["expires_at"] == "2026-01-01 00:00:00"


def test_build_tenant_notice_levels():
    """到期提醒载荷：expiring/expired 给载荷，ok/none 恒 None（=清文件）。"""
    now = 1_700_000_000.0
    fmt = lambda off: time.strftime(  # noqa: E731
        "%Y-%m-%d %H:%M:%S", time.localtime(now + off * 86400))
    # 剩 2 天 → expiring
    n = tl.build_tenant_notice({"expires_at": fmt(2)}, now)
    assert n["kind"] == "expiry" and n["level"] == "expiring"
    assert n["days_left"] == 2.0 and n["expires_at"] == fmt(2)
    # 逾期 → expired
    n2 = tl.build_tenant_notice({"expires_at": fmt(-1)}, now)
    assert n2["level"] == "expired" and n2["days_left"] == -1.0
    # 还早 / 无账本 / 坏格式 → None
    assert tl.build_tenant_notice({"expires_at": fmt(30)}, now) is None
    assert tl.build_tenant_notice({}, now) is None
    assert tl.build_tenant_notice({"expires_at": "garbage"}, now) is None


def test_renew_order_url_mapping():
    from src.ops import tenant_fulfillment as tf
    assert tf.renew_order_url("chatx-hosted-team") == (
        "https://bd2026.cc/order?plan=autochat-team&delivery=hosted")
    assert tf.renew_order_url("chatx-flagship") == (
        "https://bd2026.cc/order?plan=autochat-flagship&delivery=hosted")
    assert tf.renew_order_url("lingox-pro") == (
        "https://bd2026.cc/order?plan=translate-pro&delivery=hosted")
    # 未知/缺 SKU → 裸下单页（绝不拼不存在的 plan）
    assert tf.renew_order_url("") == "https://bd2026.cc/order?delivery=hosted"
    assert tf.renew_order_url("mystery-sku") == "https://bd2026.cc/order?delivery=hosted"


def test_stack_expires_at_and_renewal_code():
    from src.ops import tenant_fulfillment as tf
    # 未过期：从旧到期叠
    now = 1_700_000_000.0  # 固定锚
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 10 * 86400))
    stacked = tf.stack_expires_at(old, 32, now=now)
    expect = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 42 * 86400))
    assert stacked == expect
    # 已过期：从 now 起算
    past = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now - 5 * 86400))
    stacked2 = tf.stack_expires_at(past, 32, now=now)
    expect2 = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now + 32 * 86400))
    assert stacked2 == expect2
    # 续费串：有延期、无密码
    assert tf.is_existing_hosted_tenant(
        {"public_url": "https://a.bd2026.cc", "expires_at": old}) is True
    assert tf.is_existing_hosted_tenant(
        {"public_url": "https://a.bd2026.cc"}) is False  # 持单无账本≠续费
    code = tf.build_renewal_code("https://a.bd2026.cc", "zhiliao_a", stacked, "owner")
    assert "续费已到账" in code and stacked in code and "初始密码" not in code
