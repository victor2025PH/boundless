# -*- coding: utf-8 -*-
"""实例数据根 备份/恢复 核心（WP-8 2026-08-17）。

私有化客户的硬性采购问题：「数据怎么备份？换机器怎么办？」本模块＝可单测的
核心逻辑；运维入口是薄壳 ``scripts/instance_backup.ps1`` / ``instance_restore.ps1``
（规格钉的接口），desktop 端登录态另有 ``restore_chatx_accounts_node.ps1`` 家族，
本工具面向**实例布局**（``D:\\chengjie-instances\\<inst>\\data`` / 任意 AITR 数据根）。

设计要点（都有测试钉住）：
- **活库安全**：``*.db`` 一律走 sqlite3 online backup API 出一致性快照（裸 copy
  会撕裂热页；快照自带合并 -wal，故 ``-wal/-shm`` 不单独进包）；
- **排除面用 denylist**（logs / temp / tmp_* / src / __pycache__ / 既有备份产物）
  ——allowlist 会随新 store 出现而烂（本周就新增了 compliance_*.json 三个）；
- **逐文件 sha256 清单**：恢复端逐文件校验，坏一个点名一个；清单还带引擎版本
  戳（git + desktop package.json）供恢复端做版本兼容提示（warn-only，不拦人）；
- **恢复绝不默认覆盖**（目标非空须显式 --force）；恢复完成对每个 .db 跑
  ``PRAGMA integrity_check`` 把「拷完了」升级成「拷对了」。

⚠ 恢复出的数据根**不要**在原账号仍在线的机器上直接起实例（Telegram 会话会
互踢）——换机全流程（含授权重签）见 docs/智聊实例备份迁移换机SOP_2026-08.md。
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TOOL_VERSION = 1
MANIFEST_NAME = "backup_manifest.json"

#: 顶层目录级排除（数据根第一层，名字全匹配）
_EXCLUDE_TOP = {"logs", "temp", "src", "backups"}
#: 任意层目录名排除
_EXCLUDE_DIR_ANY = {"__pycache__"}
#: 顶层前缀排除（tmp_selfies / tmp_tts_preview / …）
_EXCLUDE_TOP_PREFIX = ("tmp_",)
#: 文件模式排除（sqlite 热文件由快照合并；备份产物防套娃）
_EXCLUDE_FILE_PATTERNS = ("*.db-wal", "*.db-shm", "*.db-journal",
                          "instance-backup-*.zip", "*.tmp")


def should_exclude(rel_path: str, *, is_dir: bool) -> bool:
    """备份排除判定（纯函数；``rel_path`` 用 ``/`` 分隔、相对数据根）。"""
    rel = rel_path.replace("\\", "/").strip("/")
    if not rel:
        return False
    parts = rel.split("/")
    top = parts[0]
    if top in _EXCLUDE_TOP:
        return True
    if any(top.startswith(p) for p in _EXCLUDE_TOP_PREFIX):
        return True
    if any(seg in _EXCLUDE_DIR_ANY for seg in parts):
        return True
    if not is_dir:
        name = parts[-1]
        if any(fnmatch.fnmatch(name, pat) for pat in _EXCLUDE_FILE_PATTERNS):
            return True
    return False


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_sqlite(src: Path, dst: Path) -> None:
    """SQLite online backup（对活库出一致性快照；WAL 内容一并合入）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    # ro URI：绝不因备份把源库建出来/升级 schema；busy_timeout 防长写锁下直接炸
    con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=30)
    try:
        out = sqlite3.connect(str(dst))
        try:
            con.backup(out)
            out.commit()
        finally:
            out.close()
    finally:
        con.close()


def _engine_stamp() -> Dict[str, Any]:
    """引擎版本戳（best-effort：git rev + desktop package.json version）。"""
    stamp: Dict[str, Any] = {}
    eng = Path(__file__).resolve().parents[1]
    try:
        rev = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=str(eng),
            capture_output=True, text=True, timeout=10)
        if rev.returncode == 0:
            stamp["git"] = rev.stdout.strip()
    except Exception:
        pass
    try:
        pkg = json.loads((eng / "desktop" / "package.json").read_text(
            encoding="utf-8"))
        stamp["pkg_version"] = str(pkg.get("version") or "")
    except Exception:
        pass
    return stamp


def build_backup(data_root: str, out_dir: str = "", label: str = "") -> Path:
    """备份数据根 → 带清单的 zip。返回 zip 路径；数据根不合法抛 ValueError。"""
    root = Path(data_root).resolve()
    if not root.is_dir() or not (root / "config").is_dir():
        raise ValueError(f"not an instance data root (missing config/): {root}")
    stamp_ts = time.strftime("%Y%m%d-%H%M%S")
    inst = root.parent.name if root.name == "data" else root.name
    tag = f"-{label}" if label else ""
    out_base = Path(out_dir).resolve() if out_dir else (root.parent / "backups")
    out_base.mkdir(parents=True, exist_ok=True)
    zip_path = out_base / f"instance-backup-{inst}{tag}-{stamp_ts}.zip"

    files: List[Dict[str, Any]] = []
    db_snapshots = 0
    tmp_snap_dir = Path(out_base / f".snap-{stamp_ts}")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            for dirpath, dirnames, filenames in os.walk(root):
                rel_dir = os.path.relpath(dirpath, root)
                rel_dir = "" if rel_dir == "." else rel_dir
                # 剪枝：被排除的目录整棵不进（也不递归）
                dirnames[:] = [
                    d for d in dirnames
                    if not should_exclude(os.path.join(rel_dir, d), is_dir=True)]
                for fn in sorted(filenames):
                    rel = os.path.join(rel_dir, fn) if rel_dir else fn
                    if should_exclude(rel, is_dir=False):
                        continue
                    src = Path(dirpath) / fn
                    arc = rel.replace("\\", "/")
                    if fn.endswith(".db"):
                        snap = tmp_snap_dir / arc
                        _snapshot_sqlite(src, snap)
                        db_snapshots += 1
                        digest = _sha256(snap)
                        size = snap.stat().st_size
                        zf.write(snap, arc)
                    else:
                        digest = _sha256(src)
                        size = src.stat().st_size
                        zf.write(src, arc)
                    files.append({"path": arc, "size": size, "sha256": digest})
            manifest = {
                "tool_version": TOOL_VERSION,
                "created_at": int(time.time()),
                "created_at_str": stamp_ts,
                "instance": inst,
                "data_root": str(root),
                "engine": _engine_stamp(),
                "db_snapshots": db_snapshots,
                "file_count": len(files),
                "total_bytes": sum(f["size"] for f in files),
                "files": files,
            }
            zf.writestr(MANIFEST_NAME,
                        json.dumps(manifest, ensure_ascii=False, indent=1))
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp_snap_dir, ignore_errors=True)
    return zip_path


def restore_backup(zip_path: str, target_root: str,
                   force: bool = False) -> Dict[str, Any]:
    """恢复 zip → 目标数据根；逐文件 sha256 校验 + 每 .db integrity_check。

    目标已存在且非空时必须 ``force=True``（绝不静默覆盖）。返回报告 dict；
    校验失败在报告里逐文件点名并置 ``ok=False``（不抛——运维要看全清单）。
    """
    zp = Path(zip_path).resolve()
    target = Path(target_root).resolve()
    if target.exists() and any(target.iterdir()) and not force:
        raise ValueError(f"target not empty (use --force to overlay): {target}")
    target.mkdir(parents=True, exist_ok=True)

    report: Dict[str, Any] = {"ok": True, "restored": 0, "hash_mismatch": [],
                              "integrity_bad": [], "warnings": []}
    with zipfile.ZipFile(zp, "r") as zf:
        try:
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        except KeyError:
            raise ValueError("not an instance backup (manifest missing)")
        report["manifest"] = {k: manifest.get(k) for k in (
            "tool_version", "created_at_str", "instance", "data_root",
            "engine", "file_count", "total_bytes")}
        if int(manifest.get("tool_version") or 0) > TOOL_VERSION:
            report["warnings"].append(
                f"backup written by NEWER tool v{manifest.get('tool_version')} "
                f"(this is v{TOOL_VERSION}) -- fields may be unknown")
        cur = _engine_stamp()
        b_eng = manifest.get("engine") or {}
        if (b_eng.get("pkg_version") and cur.get("pkg_version")
                and str(b_eng["pkg_version"]) > str(cur["pkg_version"])):
            report["warnings"].append(
                f"backup came from a NEWER engine ({b_eng.get('pkg_version')}) "
                f"than this machine ({cur.get('pkg_version')}) -- upgrade first "
                "or schemas may not be understood")
        for entry in manifest.get("files") or []:
            arc = str(entry["path"])
            dst = target / arc
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(arc) as srcf, open(dst, "wb") as dstf:
                shutil.copyfileobj(srcf, dstf, 1 << 20)
            got = _sha256(dst)
            if got != entry.get("sha256"):
                report["hash_mismatch"].append(arc)
                continue
            report["restored"] += 1
            if arc.endswith(".db"):
                try:
                    con = sqlite3.connect(str(dst))
                    try:
                        row = con.execute("PRAGMA integrity_check").fetchone()
                    finally:
                        con.close()
                    if not row or str(row[0]).lower() != "ok":
                        report["integrity_bad"].append(arc)
                except Exception:
                    report["integrity_bad"].append(arc)
    if report["hash_mismatch"] or report["integrity_bad"]:
        report["ok"] = False
    return report


def _cli() -> int:
    ap = argparse.ArgumentParser(description="instance data-root backup/restore")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--data-root", required=True)
    b.add_argument("--out-dir", default="")
    b.add_argument("--label", default="")
    r = sub.add_parser("restore")
    r.add_argument("--zip", required=True)
    r.add_argument("--target-root", required=True)
    r.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.cmd == "backup":
        zp = build_backup(args.data_root, args.out_dir, args.label)
        meta = json.loads(zipfile.ZipFile(zp).read(MANIFEST_NAME))
        print(f"[backup] OK {zp}")
        print(f"[backup] files={meta['file_count']} "
              f"bytes={meta['total_bytes']:,} db_snapshots={meta['db_snapshots']} "
              f"engine={meta.get('engine')}")
        return 0
    rep = restore_backup(args.zip, args.target_root, force=args.force)
    print(f"[restore] manifest: {rep.get('manifest')}")
    for w in rep["warnings"]:
        print(f"[restore] WARN: {w}")
    print(f"[restore] restored={rep['restored']} "
          f"hash_mismatch={len(rep['hash_mismatch'])} "
          f"integrity_bad={len(rep['integrity_bad'])}")
    for bad in rep["hash_mismatch"]:
        print(f"[restore] HASH MISMATCH: {bad}")
    for bad in rep["integrity_bad"]:
        print(f"[restore] DB INTEGRITY BAD: {bad}")
    if not rep["ok"]:
        return 4
    print("[restore] OK - all files verified, all dbs integrity_check=ok")
    # ASCII-only 输出（PS 5.1 控制台按 GBK 解码，中文路径会乱码）
    print("[restore] NEXT: license rebind + start checklist -> "
          "docs folder: instance migration SOP 2026-08 (ShiShi38 doc)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
