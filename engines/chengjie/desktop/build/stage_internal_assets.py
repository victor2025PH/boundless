#!/usr/bin/env python3
"""内测包资产暂存：把生产机（zhiliao 实例数据根）的人设/语音/相册/KB 打成随包种子。

用法（在 desktop/ 下）：
    python build/stage_internal_assets.py
    python build/stage_internal_assets.py --source D:/chengjie-instances/zhiliao/data

产出：desktop/build/seed-data/ —— electron-builder extraResources 把它放进安装包
resources/seed-data/，后端首启由 ConfigManager._ensure_seeded_extras 播种进用户数据区。

内容物（白名单制，绝不整目录扫；生产数据根里的会话/收件箱/日志/凭据一概不碰）：
    config.local.internal.yaml      ← 仓库 config/config.desktop.internal.yaml（功能 overlay 种子）
    config/profiles_runtime.yaml    ← 12 个人设（含 voice_profile，相对路径引用参考音）
    config/voice_refs/**            ← 克隆参考音 + 逐字稿 sidecar
    config/prerender_lines/**       ← 预渲染台词库
    config/persona_albums/**        ← 人设相册（剔除 tmp_selfies 运行时产物）
    assets/voices/**                ← 预渲染语音成品（sha1 键 + sidecar + _ref.json 指纹）
    config/knowledge_base.db        ← KB（SQLite backup 快照，活库零写事务）
    config/persona_bio.db           ← 人设深度资料（同上）
    config/persona_media.db         ← 相册注册表（同上 + file_path 归一为相对路径 +
                                       清空发送账本——那是生产客户的会话数据）
    seed-manifest.json              ← 清点（after-pack / smoke_internal 消费）

安全闸（任一不过即退出非零，绝不产出半坏种子）：
    · profiles_runtime / overlay 种子不得含绝对盘符路径与密钥形态字段；
    · 相册注册表行必须能在暂存树内解析到真实文件（解析不到的行剔除并计数）；
    · 数量下限：人设 ≥ 10、参考音 ≥ 3、预渲染 ≥ 100、相册 ≥ 50、KB ≥ 50。

退出码：0 成功；1 安全闸失败；2 环境问题（源目录缺失等）。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent            # desktop/build
DESKTOP = HERE.parent
REPO = DESKTOP.parent
DEFAULT_SOURCE = Path(r"D:/chengjie-instances/zhiliao/data")
OUT_DEFAULT = HERE / "seed-data"
OVERLAY_SEED = REPO / "config" / "config.desktop.internal.yaml"

# 相册里的运行时产物目录（生成图暂存），不属备货内容
ALBUM_EXCLUDES = {"tmp_selfies"}

# 盘符路径（D:\x / D:/x）；(?!//) 放过 URL 的 scheme://
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:(?!//)[\\/]")
_SECRET_RE = re.compile(r"api_key|service_token|auth_token|secret_key|password", re.I)
# overlay 种子里合法出现的键名（行为开关，不是凭据值）：逐行扫描时先剔除这些行
_OVERLAY_SECRET_ALLOW = re.compile(r"^\s*api_key:\s*(ollama|local)\s*$")


def _copy_tree(src: Path, dst: Path, *, excludes: set[str] | None = None) -> int:
    """copytree（顶层剔除 excludes）；返回文件数。"""
    ex = excludes or set()

    def _ignore(dirpath: str, names: list[str]):
        if Path(dirpath).resolve() == src.resolve():
            return set(names) & ex
        return set()

    shutil.copytree(src, dst, ignore=_ignore)
    return sum(1 for p in dst.rglob("*") if p.is_file())


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """SQLite backup API 快照（活库一致性快照，WAL 合并，零写事务）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dst))
        try:
            con.backup(out)
        finally:
            out.close()
    finally:
        con.close()


def _scan_text_gate(path: Path, *, allow_abs: bool = False) -> list[str]:
    """文本安全闸：绝对盘符路径 / 密钥形态字段。返回违规描述列表。"""
    bad: list[str] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for i, line in enumerate(text.splitlines(), 1):
        if _OVERLAY_SECRET_ALLOW.match(line):
            continue
        if not allow_abs and _ABS_PATH_RE.search(line) and not line.lstrip().startswith("#"):
            bad.append(f"{path.name}:{i} 含绝对盘符路径: {line.strip()[:80]}")
        if _SECRET_RE.search(line) and not line.lstrip().startswith("#"):
            # 键名出现即人工复核：值为空/占位也不该进种子（overlay 白名单键除外）
            bad.append(f"{path.name}:{i} 含密钥形态字段: {line.strip()[:80]}")
    return bad


def _rewrite_media_db(db: Path, staged_albums: Path, source_root: Path) -> tuple[int, int]:
    """把注册表 file_path 归一为相对路径（相对数据根），并清空生产发送账本。

    返回 (保留行数, 剔除行数)。解析不到实际文件的行剔除——发不出来的登记比缺图更糟
    （挑中后发送失败计入 fallback，还占轮播权重）。
    """
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT id, file_path FROM persona_media").fetchall()
        kept = dropped = 0
        src_prefix = str(source_root).replace("/", "\\").rstrip("\\").lower() + "\\"
        for mid, fp in rows:
            raw = str(fp or "")
            norm = raw.replace("/", "\\")
            rel: str | None = None
            if norm.lower().startswith(src_prefix):
                rel = norm[len(src_prefix):]
            elif not _ABS_PATH_RE.match(norm):
                rel = norm.lstrip("\\")
            if rel is not None:
                # 相册文件必须真在暂存树里（tmp_selfies 等被剔除目录的行一并淘汰）
                candidate = (staged_albums.parent.parent / rel).resolve()
                try:
                    candidate.relative_to(staged_albums.parent.parent.resolve())
                except ValueError:
                    candidate = None  # type: ignore[assignment]
                if candidate is not None and candidate.is_file():
                    con.execute(
                        "UPDATE persona_media SET file_path = ? WHERE id = ?",
                        (rel, mid))
                    kept += 1
                    continue
            con.execute("DELETE FROM persona_media WHERE id = ?", (mid,))
            dropped += 1
        # 发送账本 = 生产客户会话数据（chat_key/时间），不随包
        try:
            con.execute("DELETE FROM persona_media_sends")
        except sqlite3.OperationalError:
            pass  # 旧库无此表
        con.commit()
        con.execute("VACUUM")
        return kept, dropped
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help=f"生产数据根（默认 {DEFAULT_SOURCE}）")
    ap.add_argument("--out", default=str(OUT_DEFAULT),
                    help=f"暂存输出目录（默认 {OUT_DEFAULT}）")
    args = ap.parse_args()

    source = Path(args.source)
    out = Path(args.out)
    if not (source / "config").is_dir():
        print(f"✗ 源数据根不存在或无 config/: {source}", file=sys.stderr)
        return 2
    if not OVERLAY_SEED.exists():
        print(f"✗ overlay 种子缺失: {OVERLAY_SEED}", file=sys.stderr)
        return 2

    shutil.rmtree(out, ignore_errors=True)
    (out / "config").mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    counts: dict[str, int] = {}

    # 1) overlay 种子（含安全闸：这份文件公开分发）
    overlay_dst = out / "config.local.internal.yaml"
    shutil.copyfile(OVERLAY_SEED, overlay_dst)
    problems += _scan_text_gate(overlay_dst)

    # 2) 人设 runtime
    prof_src = source / "config" / "profiles_runtime.yaml"
    prof_dst = out / "config" / "profiles_runtime.yaml"
    if prof_src.exists():
        shutil.copyfile(prof_src, prof_dst)
        problems += _scan_text_gate(prof_dst)
        try:
            import yaml
            data = yaml.safe_load(prof_dst.read_text(encoding="utf-8")) or {}
            profs = data.get("profiles") if isinstance(data, dict) else None
            counts["personas"] = len(profs) if isinstance(profs, (list, dict)) else 0
        except Exception as exc:  # noqa: BLE001
            problems.append(f"profiles_runtime.yaml 不可解析: {exc}")
            counts["personas"] = 0
    else:
        problems.append(f"缺 {prof_src}")

    # 3) 目录树资产
    for rel, key, excludes in (
        ("config/voice_refs", "voice_ref_files", None),
        ("config/prerender_lines", "prerender_line_files", None),
        ("config/persona_albums", "album_files", ALBUM_EXCLUDES),
        ("assets/voices", "prerendered_files", None),
    ):
        src = source / rel
        if src.is_dir():
            counts[key] = _copy_tree(src, out / rel, excludes=excludes)
        else:
            counts[key] = 0
            problems.append(f"缺 {src}")

    # 4) SQLite 快照
    for name in ("knowledge_base.db", "persona_bio.db", "persona_media.db"):
        src = source / "config" / name
        if src.exists():
            _sqlite_snapshot(src, out / "config" / name)
        else:
            problems.append(f"缺 {src}")

    kb = out / "config" / "knowledge_base.db"
    if kb.exists():
        con = sqlite3.connect(str(kb))
        try:
            counts["kb_entries"] = int(
                con.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0])
        finally:
            con.close()

    media_db = out / "config" / "persona_media.db"
    if media_db.exists():
        kept, dropped = _rewrite_media_db(
            media_db, out / "config" / "persona_albums", source)
        counts["media_rows"] = kept
        counts["media_rows_dropped"] = dropped

    # 5) 数量下限闸（防「源目录挪了位」产出空种子这类静默事故）
    floors = {
        "personas": 10,
        "voice_ref_files": 3,
        "prerendered_files": 100,
        "album_files": 50,
        "kb_entries": 50,
        "media_rows": 20,
    }
    for key, floor in floors.items():
        if counts.get(key, 0) < floor:
            problems.append(f"{key}={counts.get(key, 0)} 低于下限 {floor}")

    manifest = {
        "flavor": "internal",
        "source": str(source),
        "counts": counts,
    }
    (out / "seed-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("→ 暂存清点：")
    for k, v in sorted(counts.items()):
        print(f"  · {k:<22} {v}")
    if problems:
        print(f"✗ 安全闸/完整性 {len(problems)} 项未过：", file=sys.stderr)
        for p in problems:
            print("   - " + p, file=sys.stderr)
        return 1
    total_mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    print(f"✓ 内测种子已暂存：{out}（{total_mb:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
