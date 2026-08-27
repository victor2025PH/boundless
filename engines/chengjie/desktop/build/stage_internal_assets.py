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

lite 定制档（2026-08-24，--profile lite）
=========================================
「1 男 1 女两个人设 + 2 个克隆音、无相册无 KB、功能全开、字符不限额只计量」的
对外定制形态。与标准档同一暂存框架，差异全部显式声明：

    python build/stage_internal_assets.py --profile lite \
        --personas chen_mo,lin_xiaoyu

    · 人设白名单过滤 profiles_runtime.yaml，并按 build/lite_persona_overrides.yaml
      整体替换命中的人设（chen_mo 的园区旧人设 → 马尼拉跨境电商正向人设；生产
      文件零改动——替换发生在暂存副本上）；
    · voice_refs 只带选中人设的 <pid>.wav/.txt（.bak* 一概不带）；
    · prerender_lines 只带 _common.txt + 选中人设专属库；
    · assets/voices 只带选中人设目录（陈默无专属台词，预渲染全部来自通用问候库，
      无旧人设残留——2026-08-24 已核对）；
    · 相册 / persona_media.db / knowledge_base.db / persona_bio.db 一概不带
      （manifest.omitted 声明，after-pack 按单放行）；
    · overlay 种子在暂存层补丁 licensing.trial → {enabled: true, enforce: false,
      chars: 1 亿}：**字符照常落账（与正式授权同一张水表）但实际永不封顶、永不
      拦截**——为以后定额度收集真实用量。注意 local_trial 的 chars 配置键有
      `or DEFAULT` 兜底，写 0 会被偷换成 10 万，所以「不限额」必须用大数实现。
    · 数量下限闸按 lite 口径（人设数=白名单数、参考音 wav+txt 齐、预渲染 ≥10）。

产出同样落 build/seed-data（打包互斥：谁 stage 谁打包，见 dist:win:lite）。
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


LITE_OVERRIDES_DEFAULT = HERE / "lite_persona_overrides.yaml"
#: lite 档「不限额只计量」的额度值。别写 0：local_trial 的 chars 键有 `or DEFAULT`
#: 兜底，0 会被偷换成 10 万尝鲜档；1 亿字符 = 实际永不封顶，且每一笔照常落
#: LicenseQuotaStore（会员页/逐日水表可读），正是「为定价收集真实用量」要的形态。
LITE_TRIAL_CHARS = 100_000_000

#: lite 档不随包的资产（相对种子根；after-pack 按 manifest.omitted 逐项放行）
LITE_OMITTED = (
    "config/persona_albums",
    "config/persona_media.db",
    "config/knowledge_base.db",
    "config/persona_bio.db",
)


def _patch_overlay_trial(overlay_path: Path, chars: int) -> None:
    """把暂存 overlay 的 licensing.trial 补丁成「不限额只计量」。

    优先 ruamel round-trip（保住整份 overlay 的运维注释）；缺 ruamel 回落
    PyYAML 整写（注释丢失可接受——这是构建产物，不是手维护文件）。
    """
    try:
        from ruamel.yaml import YAML  # type: ignore

        ry = YAML()
        ry.preserve_quotes = True
        ry.width = 4096
        data = ry.load(overlay_path.read_text(encoding="utf-8")) or {}
        lic = data.setdefault("licensing", {})
        trial = lic.setdefault("trial", {})
        trial["enabled"] = True
        trial["enforce"] = False
        trial["chars"] = int(chars)
        try:
            lic.yaml_set_comment_before_after_key(
                "trial",
                before=(
                    "lite 档：字符不限额、只计量（enabled=真实落账到与正式授权同一张水表；"
                    "\nenforce=false 永不拦截；chars=1 亿=实际不封顶——为额度商业化收集真实用量）"
                ),
            )
        except Exception:
            pass
        import io

        buf = io.StringIO()
        ry.dump(data, buf)
        overlay_path.write_text(buf.getvalue(), encoding="utf-8")
        return
    except ImportError:
        import yaml

        data = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        lic = data.setdefault("licensing", {})
        trial = lic.setdefault("trial", {})
        trial["enabled"] = True
        trial["enforce"] = False
        trial["chars"] = int(chars)
        overlay_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8")


def stage_lite(source: Path, out: Path, persona_ids: list[str],
               overrides_path: Path, trial_chars: int) -> int:
    """lite 定制档暂存（人设白名单 + 人设覆写 + 无相册无 KB + trial 计量补丁）。"""
    import yaml

    if not (source / "config").is_dir():
        print(f"✗ 源数据根不存在或无 config/: {source}", file=sys.stderr)
        return 2
    if not OVERLAY_SEED.exists():
        print(f"✗ overlay 种子缺失: {OVERLAY_SEED}", file=sys.stderr)
        return 2
    if not persona_ids:
        print("✗ lite 档必须显式给 --personas（逗号分隔人设 id）", file=sys.stderr)
        return 2

    shutil.rmtree(out, ignore_errors=True)
    (out / "config").mkdir(parents=True, exist_ok=True)

    problems: list[str] = []
    counts: dict[str, int] = {}

    # 1) overlay 种子 + trial 计量补丁（补丁后再过安全闸：补丁产物也是公开分发物）
    overlay_dst = out / "config.local.internal.yaml"
    shutil.copyfile(OVERLAY_SEED, overlay_dst)
    try:
        _patch_overlay_trial(overlay_dst, trial_chars)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"overlay trial 补丁失败: {exc}")
    problems += _scan_text_gate(overlay_dst)
    try:
        chk = yaml.safe_load(overlay_dst.read_text(encoding="utf-8")) or {}
        trial = ((chk.get("licensing") or {}).get("trial") or {})
        if not (trial.get("enabled") is True and trial.get("enforce") is False
                and int(trial.get("chars") or 0) == int(trial_chars)):
            problems.append(f"overlay trial 补丁校验失败: {trial}")
        if ((chk.get("licensing") or {}).get("feature_gate") or {}).get(
                "plan_override") != "flagship":
            problems.append("overlay 缺 plan_override=flagship（功能全开档）")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"overlay 补丁后不可解析: {exc}")

    # 2) 人设 runtime：白名单过滤 + 覆写整体替换
    prof_src = source / "config" / "profiles_runtime.yaml"
    prof_dst = out / "config" / "profiles_runtime.yaml"
    overrides: dict = {}
    if overrides_path.exists():
        try:
            odata = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
            overrides = odata.get("profiles") or {}
        except Exception as exc:  # noqa: BLE001
            problems.append(f"人设覆写文件不可解析: {overrides_path}: {exc}")
    if not prof_src.exists():
        problems.append(f"缺 {prof_src}")
    else:
        try:
            data = yaml.safe_load(prof_src.read_text(encoding="utf-8")) or {}
            profs = data.get("profiles")
            picked: dict = {}
            if isinstance(profs, dict):
                for pid in persona_ids:
                    if pid in profs:
                        picked[pid] = profs[pid]
                    else:
                        problems.append(f"生产 profiles_runtime 缺人设 {pid}")
            elif isinstance(profs, list):
                by_id = {str(p.get("id")): p for p in profs if isinstance(p, dict)}
                for pid in persona_ids:
                    if pid in by_id:
                        picked[pid] = by_id[pid]
                    else:
                        problems.append(f"生产 profiles_runtime 缺人设 {pid}")
            else:
                problems.append("profiles_runtime.yaml 结构异常（无 profiles）")
            for pid, repl in overrides.items():
                if pid in picked and isinstance(repl, dict):
                    # 整体替换而非 deep-merge：残留旧人设字段比缺字段更糟
                    picked[pid] = repl
            staged = dict(data)
            staged["profiles"] = ({k: v for k, v in picked.items()}
                                  if isinstance(profs, dict)
                                  else [v for v in picked.values()])
            prof_dst.write_text(
                yaml.safe_dump(staged, allow_unicode=True, sort_keys=False,
                               width=100),
                encoding="utf-8")
            counts["personas"] = len(picked)
            problems += _scan_text_gate(prof_dst)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"profiles_runtime 过滤失败: {exc}")
            counts["personas"] = 0

    # 3) 克隆参考音：只带选中人设的 <pid>.wav/.txt（.bak* 一概不带）
    refs_out = out / "config" / "voice_refs"
    refs_out.mkdir(parents=True, exist_ok=True)
    n_refs = 0
    for pid in persona_ids:
        for ext in (".wav", ".txt"):
            src = source / "config" / "voice_refs" / f"{pid}{ext}"
            if src.exists():
                shutil.copyfile(src, refs_out / src.name)
                n_refs += 1
            else:
                problems.append(f"缺参考音 {src}")
    counts["voice_ref_files"] = n_refs

    # 4) 台词库：_common + 选中人设专属（缺专属库不算错——陈默就没有）
    lines_out = out / "config" / "prerender_lines"
    lines_out.mkdir(parents=True, exist_ok=True)
    n_lines = 0
    for name in ["_common.txt"] + [f"{pid}.txt" for pid in persona_ids]:
        src = source / "config" / "prerender_lines" / name
        if src.exists():
            shutil.copyfile(src, lines_out / name)
            n_lines += 1
    counts["prerender_line_files"] = n_lines
    if not (lines_out / "_common.txt").exists():
        problems.append("缺 prerender_lines/_common.txt")

    # 5) 预渲染语音成品：只带选中人设目录（含 sidecar/_ref.json 指纹）
    n_ogg = 0
    for pid in persona_ids:
        src = source / "assets" / "voices" / pid
        if src.is_dir():
            n_ogg += _copy_tree(src, out / "assets" / "voices" / pid)
        else:
            problems.append(f"缺预渲染目录 {src}")
    counts["prerendered_files"] = n_ogg

    # 6) 数量下限闸（lite 口径）
    floors = {
        "personas": len(persona_ids),
        "voice_ref_files": len(persona_ids) * 2,   # wav+txt 齐才算备货
        "prerendered_files": 10,
    }
    for key, floor in floors.items():
        if counts.get(key, 0) < floor:
            problems.append(f"{key}={counts.get(key, 0)} 低于下限 {floor}")

    manifest = {
        "flavor": "internal",
        "profile": "lite",
        "source": str(source),
        "personas": list(persona_ids),
        "omitted": list(LITE_OMITTED),
        "trial_chars": int(trial_chars),
        "counts": counts,
    }
    (out / "seed-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("→ lite 暂存清点：")
    for k, v in sorted(counts.items()):
        print(f"  · {k:<22} {v}")
    print(f"  · omitted               {', '.join(LITE_OMITTED)}")
    if problems:
        print(f"✗ 安全闸/完整性 {len(problems)} 项未过：", file=sys.stderr)
        for p in problems:
            print("   - " + p, file=sys.stderr)
        return 1
    total_mb = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 1e6
    print(f"✓ lite 种子已暂存：{out}（{total_mb:.1f} MB，人设 {','.join(persona_ids)}）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE),
                    help=f"生产数据根（默认 {DEFAULT_SOURCE}）")
    ap.add_argument("--out", default=str(OUT_DEFAULT),
                    help=f"暂存输出目录（默认 {OUT_DEFAULT}）")
    ap.add_argument("--profile", choices=("standard", "lite"), default="standard",
                    help="standard=内测全量种子（默认）；lite=定制精简档")
    ap.add_argument("--personas", default="",
                    help="lite 档人设白名单（逗号分隔 id，如 chen_mo,lin_xiaoyu）")
    ap.add_argument("--persona-overrides", default=str(LITE_OVERRIDES_DEFAULT),
                    help="lite 档人设覆写 yaml（默认 build/lite_persona_overrides.yaml）")
    ap.add_argument("--trial-chars", type=int, default=LITE_TRIAL_CHARS,
                    help="lite 档 trial 计量额度（默认 1 亿=不限额只计量）")
    args = ap.parse_args()

    if args.profile == "lite":
        return stage_lite(
            Path(args.source), Path(args.out),
            [p.strip() for p in str(args.personas).split(",") if p.strip()],
            Path(args.persona_overrides), int(args.trial_chars))

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
