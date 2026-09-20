#!/usr/bin/env python3
"""backend-dist 源码指纹 —— 防「改了 src 却直接 dist 打出旧后端」。

事故（2026-08-07 ChatX 1.0.16 / 198）：
``npm run dist:win`` 的 predist **不会**重打 PyInstaller；它只把上一次
``build:backend`` 烤好的 ``build/backend-dist`` 原样拷进安装包。坐席侧
「复制回复一发就带图」的 ``_cancelMedia`` 双定义修复晚于 1.0.16 打包 52 分钟
落盘 → 198 自动更新吃到的仍是旧模板。冒烟只验证「exe 能起」，不验新鲜度。

本模块：
  · ``compute_fingerprint`` 对打包相关源树做内容哈希（非 mtime——mtime 跨机/touch 会撒谎）
  · ``write_stamp`` 在 ``build_backend.py`` 成功后落盘
  · ``verify_stamp`` 供 ``check_backend_freshness.py`` / predist 拦截陈旧快照

覆盖范围（与「改了就会进包」对齐）：
  · 整个 ``src/``（.py 进 exe；templates/static 进 datas）
  · ``main.py``
  · ``DATAS`` / ``STAGED_DESTS`` 指向的仓库内资源（shared/copilot、domains、config 种子…）
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Iterable, Mapping, Sequence

STAMP_NAME = ".source-fingerprint.json"
STAMP_VERSION = 1
ALGORITHM = "sha256"

# 与 build_backend.RUNTIME_STATIC_EXCLUDES 同口径（隐私目录永不入指纹）
_IGNORE_DIR_NAMES = frozenset({
    "__pycache__", ".git", "protocol_media", "persona_avatars",
    "node_modules", ".pytest_cache",
})
# 运行时/机密数据后缀：这些不是「源码」，且会被运行进程写动（如 credpool/config
# 下的 account_registry.db、registry.key）——纳入指纹会让 predist 出现「没改代码却报
# STALE」的假红，且与 build_backend.PLATFORM_SECRET_SUFFIXES 的「机密永不入包」同哲学。
_IGNORE_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".tmp", ".log",
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".key", ".pem",
})


def _norm_rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def iter_fingerprint_files(root: Path) -> Iterable[Path]:
    """稳定遍历：目录名过滤 + 后缀过滤，按相对路径排序后产出。"""
    root = root.resolve()
    if not root.exists():
        return
    if root.is_file():
        yield root
        return
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # 相对 root 的部件才过滤，避免把盘符路径段误伤
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            rel_parts = p.parts
        if any(part in _IGNORE_DIR_NAMES for part in rel_parts):
            continue
        if p.suffix.lower() in _IGNORE_SUFFIXES:
            continue
        out.append(p)
    out.sort(key=lambda x: _norm_rel(x, root))
    yield from out


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint_tree(label: str, root: Path, *, detail: bool = False) -> dict:
    """对单棵树（或单文件）出 ``{label, path, digest, file_count}``。

    ``detail=True`` 额外带 ``files: {relpath: sha256}`` 逐文件明细（哈希本就逐文件算，
    只是顺手记录，零额外 IO）——供 ``refresh_backend_datas.py`` 做「stamp vs 当前树」
    文件级 diff，判定变化是否全落在 DATAS 类资产内。聚合 digest 算法不变。
    """
    root = Path(root)
    h = hashlib.sha256()
    n = 0
    files: dict[str, str] = {}
    if not root.exists():
        out = {
            "label": label,
            "path": str(root),
            "digest": h.hexdigest(),
            "file_count": 0,
            "missing": True,
        }
        if detail:
            out["files"] = files
        return out
    base = root if root.is_dir() else root.parent
    for p in iter_fingerprint_files(root):
        rel = _norm_rel(p, base) if root.is_dir() else p.name
        digest = hash_file(p)
        # path 进哈希：防「两文件内容对调」碰撞；内容进哈希：防静默改文
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\n")
        if detail:
            files[rel] = digest
        n += 1
    out = {
        "label": label,
        "path": str(root.resolve()),
        "digest": h.hexdigest(),
        "file_count": n,
        "missing": False,
    }
    if detail:
        out["files"] = files
    return out


def _staged_source_roots(repo: Path) -> list[tuple[str, Path]]:
    """暂存资产（打包走清洗副本）对应的**真实源码根**。

    指纹必须恒指向源码而非暂存副本：暂存目录在 predist verify 时可能已被 ``--clean``
    删掉、或换机 checkout 根本没有，指向它会让 stamp/verify 两路算不出同一值。
    覆盖 static / domains 以及**仓库外**的集团底座瘦模块 platform/credpool|licensing
    （2026-08-09 起走暂存清洗打包，不再随 DATAS 直指原目录——原目录含机密数据）。
    与 ``build_backend.STAGED_DESTS`` 同口径；此处独立硬编码以免 verify 依赖 build 脚本可导入。
    """
    repo = Path(repo).resolve()
    out: list[tuple[str, Path]] = [
        ("src/web/static", repo / "src" / "web" / "static"),
        # i18n 词条包（2026-08-18 起走暂存清洗进 datas，frozen 态文件优先 exec）：
        # 指纹指真实源目录（iter 已剔 __pycache__/.pyc，与暂存清洗口径一致）。
        ("src/web/i18n_packs", repo / "src" / "web" / "i18n_packs"),
    ]
    if (repo / "domains").is_dir():
        out.append(("domains", repo / "domains"))
    # 集团底座瘦模块在仓库外：boundless/platform/<pkg>（= engines/chengjie 的祖父目录）
    platform_root = repo.parent.parent / "platform"
    for pkg in ("credpool", "licensing"):
        d = platform_root / pkg
        if d.is_dir():
            out.append((f"platform/{pkg}", d))
    return out


def default_roots(repo: Path, datas: Sequence[tuple[Path, str]] | None = None) -> list[tuple[str, Path]]:
    """打包相关源根。``datas`` 缺省时从 build_backend 加载 DATAS。

    stamp（build_backend 传 ``datas=DATAS``）与 verify（predist 传 ``datas=None``）两路
    必须产出**同一组 roots、同一顺序**——故暂存源统一由 ``_staged_source_roots`` 补齐，
    与 ``datas`` 是否显式传入无关。
    """
    repo = Path(repo).resolve()
    roots: list[tuple[str, Path]] = [
        ("main.py", repo / "main.py"),
        ("src", repo / "src"),
    ]
    roots.extend(_staged_source_roots(repo))
    if datas is None:
        try:
            from importlib.util import module_from_spec, spec_from_file_location
            script = repo / "desktop" / "build" / "build_backend.py"
            spec = spec_from_file_location("_bd_build_backend_fp", script)
            mod = module_from_spec(spec)
            assert spec and spec.loader
            spec.loader.exec_module(mod)
            datas = list(mod.DATAS)
        except Exception:
            datas = []

    seen = {label for label, _ in roots}
    for src, dst in datas or []:
        src_p = Path(src)
        label = str(dst).replace("\\", "/").strip("/")
        if label in seen:
            continue
        # 只指纹仓库内 / 显式存在的源；platform 瘦模块已由 _staged_source_roots 覆盖
        if src_p.exists():
            roots.append((label, src_p))
            seen.add(label)
    return roots


def compute_fingerprint(
    repo: Path,
    datas: Sequence[tuple[Path, str]] | None = None,
    *,
    detail: bool = False,
) -> dict:
    roots = default_roots(repo, datas=datas)
    parts = [fingerprint_tree(label, path, detail=detail) for label, path in roots]
    agg = hashlib.sha256()
    for part in parts:
        agg.update(part["label"].encode("utf-8"))
        agg.update(b":")
        agg.update(part["digest"].encode("ascii"))
        agg.update(b"\n")
    return {
        "version": STAMP_VERSION,
        "algorithm": ALGORITHM,
        "aggregate": agg.hexdigest(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "file_count": sum(int(p["file_count"]) for p in parts),
        "roots": parts,
    }


def stamp_path(dist_dir: Path) -> Path:
    return Path(dist_dir) / STAMP_NAME


def write_stamp(dist_dir: Path, fingerprint: Mapping) -> Path:
    dist_dir = Path(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)
    path = stamp_path(dist_dir)
    path.write_text(
        json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_stamp(dist_dir: Path) -> dict | None:
    path = stamp_path(dist_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def verify_stamp(
    repo: Path,
    dist_dir: Path,
    datas: Sequence[tuple[Path, str]] | None = None,
) -> tuple[bool, str, dict | None, dict | None]:
    """返回 ``(ok, reason, current, stamped)``。"""
    dist_dir = Path(dist_dir)
    exe = dist_dir / ("backend.exe" if __import__("os").name == "nt" else "backend")
    if not exe.is_file() and not (dist_dir / "_internal").is_dir():
        return False, f"backend-dist missing (no {exe.name}): run npm run build:backend", None, None

    stamped = load_stamp(dist_dir)
    if not stamped or not stamped.get("aggregate"):
        return (
            False,
            f"no {STAMP_NAME} in backend-dist — rebuild with current build_backend.py "
            f"(old snapshots have no stamp; refusing to ship unstamped backend)",
            None,
            stamped,
        )

    current = compute_fingerprint(repo, datas=datas)
    if current["aggregate"] != stamped["aggregate"]:
        #  diff 哪棵树变了，方便坐席判断要不要重打
        stamped_roots = {r["label"]: r.get("digest") for r in stamped.get("roots") or []}
        changed = [
            r["label"]
            for r in current["roots"]
            if stamped_roots.get(r["label"]) != r["digest"]
        ]
        hint = ", ".join(changed[:8]) or "unknown"
        return (
            False,
            f"backend-dist STALE vs working tree (changed: {hint}). "
            f"Try: npm run refresh:datas   (datas-only, seconds; refuses if .py changed) "
            f"else: npm run build:backend   then retry dist. "
            f"stamped={stamped['aggregate'][:12]}… current={current['aggregate'][:12]}…",
            current,
            stamped,
        )
    return True, "fresh", current, stamped
