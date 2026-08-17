# -*- coding: utf-8 -*-
"""WP-8 实例备份/恢复门禁（2026-08-17）。

钉住的不变量：
1. 排除面 denylist 语义：logs/temp/tmp_*/src/__pycache__/备份产物/-wal/-shm 不进包，
   config 全家（db/yaml/key/媒体）与 sessions 进包；
2. **活库安全**：WAL 模式下有未 checkpoint 写入时备份，快照必须包含已提交行
   （sqlite online backup 合并 WAL，裸 copy 做不到）；
3. round-trip：备份 → 恢复 → 逐文件 sha256 全过 + 每 .db integrity_check=ok +
   行级内容一致；
4. 安全边界：目标非空拒绝（--force 才覆盖）；包内文件被篡改 → 恢复端逐文件点名
   且 ok=False；更新工具/更新引擎写的包 → warn 不拦。
"""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from scripts.instance_backup import (
    MANIFEST_NAME,
    build_backup,
    restore_backup,
    should_exclude,
)


# ── 1. 排除面纯函数 ──────────────────────────────────────────────────────────

def test_should_exclude_semantics():
    keep = [
        ("config/inbox.db", False),
        ("config/config.local.yaml", False),
        ("config/license.key", False),
        ("config/voice_refs/a.wav", False),
        ("sessions/main.session", False),
        ("ledger_outbox/x.json", False),
        ("assets/voices/p/clip.ogg", False),
        ("unknown_errors.txt", False),
    ]
    drop = [
        ("logs/app.log", False),
        ("temp/x.bin", False),
        ("tmp_selfies/a.jpg", False),
        ("tmp_tts_preview/b.ogg", False),
        ("src/web/static/x.png", False),
        ("config/__pycache__/m.pyc", False),
        ("config/inbox.db-wal", False),
        ("config/inbox.db-shm", False),
        ("backups/instance-backup-old.zip", False),
        ("config/instance-backup-x-20260101-000000.zip", False),
        ("logs", True),
        ("tmp_voice_review", True),
        ("src", True),
    ]
    for rel, is_dir in keep:
        assert not should_exclude(rel, is_dir=is_dir), f"误剔 {rel}"
    for rel, is_dir in drop:
        assert should_exclude(rel, is_dir=is_dir), f"漏剔 {rel}"


# ── 夹具：仿真数据根 ─────────────────────────────────────────────────────────

def _mk_root(tmp_path: Path) -> Path:
    root = tmp_path / "inst" / "data"
    (root / "config" / "voice_refs").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "tmp_selfies").mkdir()
    (root / "src").mkdir()
    (root / "sessions").mkdir()
    con = sqlite3.connect(root / "config" / "inbox.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY, peer TEXT)")
    con.execute("CREATE TABLE messages (id TEXT, conversation_id TEXT, body TEXT)")
    for i in range(3):
        con.execute("INSERT INTO conversations VALUES (?, ?)",
                    (f"conv:{i}", f"peer{i}"))
        con.execute("INSERT INTO messages VALUES (?, ?, ?)",
                    (f"m{i}", f"conv:{i}", f"hello {i}"))
    con.commit()
    con.close()
    (root / "config" / "config.local.yaml").write_text(
        "ai:\n  primary: cloud\n", encoding="utf-8")
    (root / "config" / "license.key").write_text("LIC-XYZ\n", encoding="utf-8")
    (root / "config" / "voice_refs" / "a.wav").write_bytes(b"RIFFfakewav")
    (root / "sessions" / "main.session").write_bytes(b"\x01\x02session")
    (root / "logs" / "app.log").write_text("noise", encoding="utf-8")
    (root / "tmp_selfies" / "x.jpg").write_bytes(b"\xff\xd8junk")
    (root / "src" / "stray.py").write_text("# misdrop", encoding="utf-8")
    (root / "unknown_errors.txt").write_text("diag", encoding="utf-8")
    return root


# ── 2/3. 活库快照 + round-trip ───────────────────────────────────────────────

def test_backup_restore_roundtrip_with_live_wal(tmp_path):
    root = _mk_root(tmp_path)
    # 活库语义：WAL 下再写一行且**不关连接不 checkpoint**——快照必须看得见它
    live = sqlite3.connect(root / "config" / "inbox.db")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("INSERT INTO messages VALUES ('m_live', 'conv:0', 'wal row')")
    live.commit()          # 已提交但停留在 -wal（未 checkpoint）
    try:
        zp = build_backup(str(root), out_dir=str(tmp_path / "bak"))
        assert zp.exists()
        names = set(zipfile.ZipFile(zp).namelist())
        assert "config/inbox.db" in names
        assert "config/config.local.yaml" in names
        assert "config/license.key" in names
        assert "config/voice_refs/a.wav" in names
        assert "sessions/main.session" in names
        assert "unknown_errors.txt" in names
        assert not any(n.startswith(("logs/", "tmp_selfies/", "src/"))
                       for n in names), "排除面泄漏"
        assert not any(n.endswith(("-wal", "-shm")) for n in names)
        manifest = json.loads(zipfile.ZipFile(zp).read(MANIFEST_NAME))
        assert manifest["db_snapshots"] == 1
        assert manifest["file_count"] == len(names) - 1          # 清单自身不计
    finally:
        live.close()

    target = tmp_path / "restored"
    rep = restore_backup(str(zp), str(target))
    assert rep["ok"] is True, rep
    assert rep["hash_mismatch"] == [] and rep["integrity_bad"] == []
    # 行级一致 + WAL 行在场（活库快照的核心断言）
    con = sqlite3.connect(target / "config" / "inbox.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 3
        bodies = {r[0] for r in con.execute("SELECT body FROM messages")}
        assert "wal row" in bodies and "hello 0" in bodies
    finally:
        con.close()
    assert (target / "config" / "license.key").read_text(
        encoding="utf-8") == "LIC-XYZ\n"


# ── 4. 安全边界 ──────────────────────────────────────────────────────────────

def test_restore_refuses_nonempty_target(tmp_path):
    root = _mk_root(tmp_path)
    zp = build_backup(str(root), out_dir=str(tmp_path / "bak"))
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "existing.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        restore_backup(str(zp), str(target))
    rep = restore_backup(str(zp), str(target), force=True)
    assert rep["ok"] is True


def test_restore_detects_tampered_file(tmp_path):
    root = _mk_root(tmp_path)
    zp = build_backup(str(root), out_dir=str(tmp_path / "bak"))
    # 重写 zip：篡改 license.key 内容、清单原样 → 恢复端必须点名
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(zp) as src, zipfile.ZipFile(tampered, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == "config/license.key":
                data = b"EVIL\n"
            dst.writestr(item, data)
    rep = restore_backup(str(tampered), str(tmp_path / "restored2"))
    assert rep["ok"] is False
    assert rep["hash_mismatch"] == ["config/license.key"]


def test_restore_warns_on_newer_tool(tmp_path):
    root = _mk_root(tmp_path)
    zp = build_backup(str(root), out_dir=str(tmp_path / "bak"))
    newer = tmp_path / "newer.zip"
    with zipfile.ZipFile(zp) as src, zipfile.ZipFile(newer, "w") as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == MANIFEST_NAME:
                m = json.loads(data)
                m["tool_version"] = 99
                data = json.dumps(m).encode("utf-8")
            dst.writestr(item, data)
    rep = restore_backup(str(newer), str(tmp_path / "restored3"))
    assert rep["ok"] is True                       # warn 不拦
    assert any("NEWER tool" in w for w in rep["warnings"])


def test_backup_rejects_non_dataroot(tmp_path):
    with pytest.raises(ValueError):
        build_backup(str(tmp_path))                # 无 config/ = 不是数据根
