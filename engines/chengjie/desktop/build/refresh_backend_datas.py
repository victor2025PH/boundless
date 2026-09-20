#!/usr/bin/env python3
"""backend-dist「仅刷数据资产」增量档 —— 模板/static 变了不必整跑 PyInstaller。

背景（2026-08-18 1.0.41 发版实录）：predist freshness 门禁把「backend-dist 落后于
工作树」一律打回全量 build:backend（~7.5 分钟）。但共享树上 sibling 线高频保存的
多是 templates/static/shared——这些在包内是 **datas**（运行时按文件路径读，不进
exe/PYZ），逐字节替换即真生效。整跑 PyInstaller 只为刷几个 html 是纯浪费，且 7.5
分钟窗口本身就是与 sibling 保存赛跑的根因。

本脚本的诚实性契约（比「省时间」更重要，缺一不可）：
  1. stamp 必须带逐文件明细（build_backend 现以 detail=True 落 stamp）——没有明细
     无从证明「exe 里的 .py 没变」，直接拒绝（exit 4，先全量重打一次）。
  2. 文件级 diff（stamp vs 当前树）逐条分类：变化落在 DATAS 类安全区 → 可刷；
     任何编进 exe 的代码（main.py / src/**/*.py 非 templates|static）变了 →
     拒绝（exit 3），老老实实全量重打。宁可拒绝也不产出「戳说新、exe 是旧」的包。
  3. 暂存清洗资产（static/domains/platform 瘦模块）复用 build_backend 同一套
     staging 函数——隐私/机密剔除口径零分叉（RUNTIME_STATIC_EXCLUDES、
     PLATFORM_SECRET_SUFFIXES 的断言原样生效，泄漏即中止）。
  4. 先算指纹→再复制→再落 stamp→末尾复检：若复制期间树又动了（sibling 保存），
     复检红 → 有界重试 2 轮，仍红则如实退出非零（与全量构建面对的竞态同语义，
     只是窗口从分钟级缩到秒级）。

用法（desktop/ 下）：
    npm run refresh:datas            # = python build/refresh_backend_datas.py
    python build/refresh_backend_datas.py --dry-run   # 只看分类判定不动产物
    python build/refresh_backend_datas.py --diff-stamps <坐席stamp.json>
        # 坐席差量推送判定（push_chatx_datas.ps1 消费）：本机 backend-dist stamp
        # vs 坐席已装 stamp，JSON 判定到 stdout——分类逻辑与本地增量同源，
        # 「哪些变更能只动 datas」全链只有一个答案。

exit：0=已刷新/本就新鲜/两 stamp 一致；2=前置缺失（无产物/无 stamp/输入损坏）；
      3=有编进 exe 的代码变更（本地须全量重打 / 坐席须走完整安装包）；
      4=stamp 无逐文件明细（旧格式，先全量重打一次）；5=树持续变动刷不干净；
      6=（仅 --diff-stamps）差异全落安全区，可差量推送（JSON 带 zones/dirs）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent            # desktop/build
DESKTOP = HERE.parent
REPO = DESKTOP.parent
OUT = HERE / "backend-dist"

# 安全区：这些指纹根整棵都是「包内 datas / 运行时按路径读文件」的资产
# （i18n_packs 例外见 _classify_src_rel：`_` 开头文件的逻辑始终来自 PYZ，不安全）
_SAFE_LABELS = frozenset({
    "src/web/static", "src/web/templates", "shared/copilot",
    "domains", "platform/credpool", "platform/licensing",
    "config", "config/profiles", "src/web/i18n_packs",
})
# "src" 根覆盖全 src/：只有这些子树是 datas，其余（.py 等）编进 exe/PYZ。
# 顺序即判定序；i18n_packs 词条 .py 自 2026-08-18 起 frozen 态文件优先 exec，
# 属 datas——但 __init__.py 等 `_` 开头文件仍走 PYZ import，变更必须全量重打。
_SRC_SAFE_MAP = (
    ("web/templates/", "src/web/templates"),
    ("web/static/", "src/web/static"),
    ("web/i18n_packs/", "src/web/i18n_packs"),
)
# 兼容旧消费方（历史门禁按下标取前两项）
_SRC_SAFE_PREFIXES = tuple(p for p, _ in _SRC_SAFE_MAP[:2])

# ── 坐席差量推送（P2）：zone → backend-dist/_internal 内的相对目录 ─────────────
# push_chatx_datas.ps1 只搬运 _internal 下这些目录——它们已经过构建期暂存清洗
# （隐私/机密剔除断言生效后落盘），是「可以进坐席机」的唯一事实源；映射放
# Python 侧＝与 classify 同源，防 PS 里再写一套迟早漂移的判定。
_ZONE_INTERNAL_DIRS = {
    "src/web/templates": "src/web/templates",
    "src/web/static": "src/web/static",
    "src/web/i18n_packs": "src/web/i18n_packs",
    "shared/copilot": "shared/copilot",
    "domains": "domains",
    "platform/credpool": "platform/credpool",
    "platform/licensing": "platform/licensing",
    "config": "config",
    "config/profiles": "config/profiles",
}
# 热生效 zone：坐席后端在跑也即时生效（模板 Jinja auto_reload / static 按请求
# 读盘 / i18n packs frozen 态文件优先 exec + mtime 热加载 / shared 组件按路径
# 服务）。其余（domains / config 种子 / platform 瘦模块）多为进程启动或首启
# 种子期消费 → 推送后建议重启壳，PS 侧据此打提示。
_HOT_ZONES = frozenset({
    "src/web/templates", "src/web/static", "src/web/i18n_packs", "shared/copilot",
})

# 使用账本（P2 观测）：一行 JSONL 记每次判定/刷新，攒两周回答「增量档接住了
# 多少、被拒原因分布、全量剩余大头是谁」。只记真实产物目录（tests 传 tmp out
# 时天然跳过，不掺假读数）；写失败一律静默——账本绝不影响主流程。
USAGE_LOG_NAME = "refresh_usage.jsonl"


def _maybe_log_usage(out: Path, entry: dict) -> None:
    try:
        if Path(out).resolve() != OUT.resolve():
            return
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        with (HERE / USAGE_LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def zones_to_internal_dirs(zones: set[str]) -> list[str]:
    """zone 集合 → _internal 相对目录清单（嵌套去重：config 整目录已含 profiles）。"""
    dirs = sorted({_ZONE_INTERNAL_DIRS[z] for z in zones})
    return [d for d in dirs
            if not any(d != k and d.startswith(k + "/") for k in dirs)]


def diff_two_stamps(seat: dict, local: dict) -> dict:
    """两份 stamp（seat=坐席已装 / local=本机 backend-dist）→ 差量推送判定。

    与本地增量刷新共用 diff_stamp_files + classify_changes。verdict：
      in_sync   聚合指纹一致，坐席已最新
      sync      差异全落安全区 → zones/dirs 可差量推送
      unsafe    存在编进 exe 的差异（含坐席多出的指纹根、或聚合异变但 diff
                无解释）→ 必须走完整安装包
      no_detail 任一侧 stamp 缺逐文件明细 → 先全量重打自举
    """
    if seat.get("aggregate") and seat["aggregate"] == local.get("aggregate"):
        return {"verdict": "in_sync", "aggregate": local.get("aggregate")}
    seat_roots = {r["label"]: r for r in seat.get("roots") or []}
    local_labels = {r["label"] for r in local.get("roots") or []}
    extra = sorted(set(seat_roots) - local_labels)
    if extra:
        # 坐席 stamp 有本机没有的根 = 两边构建脚本版本分叉，差量语义不成立
        return {"verdict": "unsafe", "unsafe_total": len(extra),
                "unsafe": [[lbl, "", "root_only_on_seat"] for lbl in extra]}
    no_detail: list[str] = []
    for r in local.get("roots") or []:
        old = seat_roots.get(r["label"])
        if old is not None and r.get("digest") == old.get("digest"):
            continue
        if r.get("files") is None:   # 本机侧旧格式（diff_stamp_files 只查 seat 侧）
            no_detail.append(r["label"])
    changes, nd_seat = diff_stamp_files(seat, local)
    no_detail = sorted(set(no_detail) | set(nd_seat))
    if no_detail:
        return {"verdict": "no_detail", "roots": no_detail}
    zones, unsafe = classify_changes(changes)
    if unsafe:
        return {"verdict": "unsafe", "unsafe_total": len(unsafe),
                "unsafe": [list(u) for u in unsafe[:40]]}
    if not zones:
        # 聚合不同却 diff 不出文件级差异（根序/label 漂移等）——宁可拒绝也不猜
        return {"verdict": "unsafe", "unsafe_total": 0,
                "unsafe": [["", "", "aggregate_mismatch_no_file_diff"]]}
    return {
        "verdict": "sync",
        "zones": sorted(zones),
        "dirs": zones_to_internal_dirs(zones),
        "hot_zones": sorted(z for z in zones if z in _HOT_ZONES),
        "restart_zones": sorted(z for z in zones if z not in _HOT_ZONES),
        "changes": len(changes),
        "aggregate_local": local.get("aggregate"),
        "aggregate_seat": seat.get("aggregate"),
    }


def run_diff_stamps(seat_stamp_path: Path, out: Path = OUT, *, fp=None) -> int:
    """--diff-stamps 入口。JSON 判定到 stdout；exit 语义见模块 docstring。

    前置由调用方（push_chatx_datas.ps1）保证：本机 backend-dist 已过 freshness
    门禁（stamp == 工作树），故本函数只比 stamp 不算树，秒级返回。
    """
    fp = fp or load_fp_module()
    try:
        seat = json.loads(Path(seat_stamp_path).read_text(encoding="utf-8-sig"))
    except Exception as e:
        print(json.dumps({"verdict": "bad_input",
                          "error": f"seat stamp unreadable: {e}"}, ensure_ascii=False))
        return 2
    local = fp.load_stamp(out)
    if not local or not local.get("aggregate"):
        print(json.dumps({"verdict": "bad_input",
                          "error": "local backend-dist has no stamp"}, ensure_ascii=False))
        return 2
    if not isinstance(seat, dict) or not seat.get("aggregate"):
        print(json.dumps({"verdict": "bad_input",
                          "error": "seat stamp has no aggregate"}, ensure_ascii=False))
        return 2
    res = diff_two_stamps(seat, local)
    print(json.dumps(res, ensure_ascii=False))
    _maybe_log_usage(out, {"mode": "diff_stamps", "verdict": res["verdict"],
                           "zones": res.get("zones"),
                           "changes": res.get("changes"),
                           "unsafe_total": res.get("unsafe_total")})
    return {"in_sync": 0, "sync": 6, "unsafe": 3, "no_detail": 4}[res["verdict"]]


def _classify_src_rel(rel: str) -> str | None:
    """"src" 根内相对路径 → 可刷 zone；None = 不安全（编进 exe/PYZ）。"""
    for prefix, zone in _SRC_SAFE_MAP:
        if rel.startswith(prefix):
            if zone == "src/web/i18n_packs":
                name = rel[len(prefix):]
                # 子目录（当前无）或 `_` 开头模块（__init__ 逻辑在 PYZ）→ 不安全
                if "/" in name or name.startswith("_"):
                    return None
            return zone
    return None


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_fp_module():
    return _load_module("_bd_src_fp_refresh", HERE / "backend_source_fingerprint.py")


def load_build_module():
    return _load_module("_bd_build_backend_refresh", HERE / "build_backend.py")


def diff_stamp_files(stamped: dict, current: dict) -> tuple[list[tuple[str, str, str]], list[str]]:
    """逐文件 diff。返回 (changes, roots_without_detail)。

    changes 条目 = (root_label, relpath, kind)；kind ∈ changed/added/removed。
    roots_without_detail 非空 ⇒ stamp 是旧格式，调用方必须拒绝增量。
    """
    stamped_roots = {r["label"]: r for r in stamped.get("roots") or []}
    changes: list[tuple[str, str, str]] = []
    no_detail: list[str] = []
    for cur in current.get("roots") or []:
        label = cur["label"]
        old = stamped_roots.get(label)
        if old is None:
            # stamp 里没有这棵根（新加的 DATAS）：整棵按 added 记
            for rel in (cur.get("files") or {}):
                changes.append((label, rel, "added"))
            continue
        if cur.get("digest") == old.get("digest"):
            continue
        old_files = old.get("files")
        if old_files is None:
            no_detail.append(label)
            continue
        cur_files = cur.get("files") or {}
        for rel, digest in cur_files.items():
            if rel not in old_files:
                changes.append((label, rel, "added"))
            elif old_files[rel] != digest:
                changes.append((label, rel, "changed"))
        for rel in old_files:
            if rel not in cur_files:
                changes.append((label, rel, "removed"))
    return changes, no_detail


def classify_changes(changes: list[tuple[str, str, str]]) -> tuple[set[str], list[tuple[str, str, str]]]:
    """把文件级变化归入安全区（可刷 zone 集合）或标为不安全（须全量重打）。

    zone 名 = 指纹根 label（"src" 根下的 datas 子树归并到对应专属 label）。
    未知 label 一律不安全——宁可全量重打也不猜。
    """
    zones: set[str] = set()
    unsafe: list[tuple[str, str, str]] = []
    for label, rel, kind in changes:
        if label == "src/web/i18n_packs":
            # 专属根也要过 `_` 守卫（__init__.py 变了 = PYZ 逻辑变了，须全量）
            zone = _classify_src_rel(f"web/i18n_packs/{rel}")
            if zone:
                zones.add(zone)
            else:
                unsafe.append((label, rel, kind))
        elif label in _SAFE_LABELS:
            zones.add(label)
        elif label == "src":
            zone = _classify_src_rel(rel)
            if zone:
                zones.add(zone)
            else:
                unsafe.append((label, rel, kind))
        else:
            unsafe.append((label, rel, kind))
    return zones, unsafe


def _replace_dir(src: Path, dest: Path) -> None:
    shutil.rmtree(dest, ignore_errors=True)
    if src.is_dir():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)


def resync_zones(zones: set[str], repo: Path, internal: Path, bb) -> None:
    """把安全区资产从源树重新同步进 backend-dist/_internal。

    暂存清洗资产走 build_backend 同名函数（隐私/机密剔除断言原样生效）；
    直打资产整棵替换（与 PyInstaller --add-data 的原样拷贝同语义，天然处理增删改）。
    """
    for zone in sorted(zones):
        if zone == "src/web/templates":
            _replace_dir(repo / "src" / "web" / "templates", internal / "src" / "web" / "templates")
        elif zone == "shared/copilot":
            _replace_dir(repo / "shared" / "copilot", internal / "shared" / "copilot")
        elif zone == "config/profiles":
            _replace_dir(repo / "config" / "profiles", internal / "config" / "profiles")
        elif zone == "config":
            dest = internal / "config"
            dest.mkdir(parents=True, exist_ok=True)
            for name in ("config.desktop.min.yaml", "config.example.yaml",
                         "release_defaults_changed.json"):  # Q-4 #267 F
                src = repo / "config" / name
                if src.is_file():
                    shutil.copy2(src, dest / name)
        elif zone == "src/web/static":
            staged = bb._stage_static()          # 含 RUNTIME_STATIC_EXCLUDES 剔除+泄漏断言
            _replace_dir(staged, internal / "src" / "web" / "static")
        elif zone == "src/web/i18n_packs":
            staged = bb._stage_i18n_packs()      # 剔 __pycache__ + 逐文件语法自检
            _replace_dir(staged, internal / "src" / "web" / "i18n_packs")
        elif zone == "domains":
            staged = bb._stage_domains()
            _replace_dir(staged, internal / "domains")
        elif zone in ("platform/credpool", "platform/licensing"):
            pkg = zone.split("/", 1)[1]
            staged = bb._stage_platform_pkg(pkg)  # 含机密后缀剔除+泄漏断言
            if staged is not None:
                _replace_dir(staged, internal / "platform" / pkg)
            else:
                shutil.rmtree(internal / "platform" / pkg, ignore_errors=True)
        else:  # pragma: no cover - classify_changes 只产已知 zone
            raise RuntimeError(f"unknown refresh zone: {zone}")
        print(f"  ✓ resynced {zone}")


def run(repo: Path = REPO, out: Path = OUT, *, dry_run: bool = False,
        fp=None, bb=None, max_rounds: int = 3) -> int:
    t0 = time.monotonic()
    rc = _run(repo, out, dry_run=dry_run, fp=fp, bb=bb, max_rounds=max_rounds)
    _maybe_log_usage(out, {"mode": "refresh", "rc": rc, "dry_run": dry_run,
                           "secs": round(time.monotonic() - t0, 1)})
    return rc


def _run(repo: Path = REPO, out: Path = OUT, *, dry_run: bool = False,
         fp=None, bb=None, max_rounds: int = 3) -> int:
    fp = fp or load_fp_module()
    internal = Path(out) / "_internal"
    exe = Path(out) / ("backend.exe" if sys.platform == "win32" else "backend")
    if not internal.is_dir() or not exe.is_file():
        print("✗ backend-dist 无 onedir 产物（_internal/exe 缺失）——先 npm run build:backend", file=sys.stderr)
        return 2
    stamped = fp.load_stamp(out)
    if not stamped or not stamped.get("aggregate"):
        print("✗ backend-dist 无源码指纹戳——先 npm run build:backend", file=sys.stderr)
        return 2

    for round_no in range(1, max_rounds + 1):
        current = fp.compute_fingerprint(repo, detail=True)
        if current["aggregate"] == stamped["aggregate"]:
            # 本就新鲜。旧格式 stamp（无逐文件明细）顺手原地升级，下次增量可用。
            if any(r.get("files") is None for r in stamped.get("roots") or []):
                if dry_run:
                    print("✓ 已新鲜（stamp 为旧格式；非 dry-run 时会原地升级为逐文件明细）")
                else:
                    fp.write_stamp(out, current)
                    print("✓ 已新鲜；stamp 已原地升级为逐文件明细格式")
            else:
                print("✓ backend-dist 已新鲜，无需刷新")
            return 0

        changes, no_detail = diff_stamp_files(stamped, current)
        if no_detail:
            print(f"✗ stamp 缺逐文件明细（旧格式根：{', '.join(no_detail)}）——"
                  f"先全量 npm run build:backend 一次以升级 stamp", file=sys.stderr)
            return 4
        zones, unsafe = classify_changes(changes)
        if unsafe:
            print("✗ 存在编进 exe 的代码变更，增量刷新无法覆盖（须全量 npm run build:backend）：", file=sys.stderr)
            for label, rel, kind in unsafe[:20]:
                print(f"    [{kind}] {label}/{rel}", file=sys.stderr)
            if len(unsafe) > 20:
                print(f"    … 共 {len(unsafe)} 处", file=sys.stderr)
            return 3

        print(f"→ 第 {round_no} 轮：{len(changes)} 处数据资产变更，刷新 {len(zones)} 个 zone: "
              + ", ".join(sorted(zones)))
        if dry_run:
            print("  (dry-run 到此为止，未改动产物)")
            return 0

        bb = bb or load_build_module()
        resync_zones(zones, Path(repo), internal, bb)
        fp.write_stamp(out, current)

        ok, reason, _, _ = fp.verify_stamp(repo, out)
        if ok:
            print(f"✓ 刷新完成并复检通过（aggregate={current['aggregate'][:16]}…）")
            return 0
        # 复制期间树又动了：stamp 已如实落为「复制前快照」，门禁红是诚实信号 → 重试
        print(f"⚠ 复检未过（复制期间树仍在变动）：{reason}")
        stamped = fp.load_stamp(out)

    print("✗ 连续多轮树仍在变动，刷不干净——等 sibling 保存停歇后重试", file=sys.stderr)
    return 5


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", type=Path, default=REPO)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--dry-run", action="store_true", help="只做 diff/分类判定，不动产物")
    ap.add_argument("--diff-stamps", type=Path, metavar="SEAT_STAMP",
                    help="坐席差量推送判定：与本机 backend-dist stamp 比对，JSON 到 stdout")
    args = ap.parse_args()
    if args.diff_stamps:
        return run_diff_stamps(args.diff_stamps, args.out)
    return run(args.repo, args.out, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
