"""托管租户生命周期门禁（src/ops/tenant_lifecycle.py + scripts/tenant_ops.py 的纯函数层）。

重点守两条安全不变量：
1. 生产双实例（zhiliao/tongyi）绝不落入租户生命周期管辖（guard_not_core / is_tenant_service）；
2. 退租导出默认不带平台登录态与厂商授权（export_manifest 排除语义）。
全部用例 tmp_path 隔离，零仓库/生产写入。
"""
from __future__ import annotations

import json
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


def test_render_nginx_site_port_mode():
    conf = tl.render_nginx_site_port("acme", 18987, 18999)
    assert "listen 18987 ssl;" in conf
    assert "server_name bd2026.cc;" in conf
    # 端口形态复用主域证书（SAN 已含主域，零 DNS 依赖）
    assert "/etc/letsencrypt/live/bd2026.cc/fullchain.pem" in conf
    assert "proxy_pass http://127.0.0.1:18999;" in conf
    # 反代主体与子域形态同源（SSE/WS/媒体语义一致）
    assert "proxy_buffering off;" in conf and "client_max_body_size 50m;" in conf


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
