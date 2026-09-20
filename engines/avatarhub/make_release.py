# -*- coding: utf-8 -*-
"""
make_release.py — 一键发布编排器（把零散的构建步骤变成可复现的一条命令）。

它把以下步骤串成可复现流程，并产出一份「可直接上传到下载站的 publish 树 + 上传操作单」：
  1) 确保启动器 exe（缺失或 --rebuild-exe 时调 build_launcher.bat）
  2) 构建分发包 + manifest（调 build_packs.py，统一注入 --base-url / --version / --arch-tag）
  3) 装配便携版（调 make_portable.py，注入同一 base_url）
  4) 可选：编译 Inno 安装包（--with-installer，调 installer\\build_installer.bat）
  5) 装配 dist\\publish\\<ver>\\：manifest.json + packs\\*（硬链，不重复占盘）
     + release_index.json（每个可上传文件的 remote 路径/体积/sha256）
     + UPLOAD.md（站点布局 + 三种后端上传命令 + 发布后冒烟命令）
  6) 增量发布：给 --prev-manifest 时，标出相对上一版「新增/变更/未变」，可只传变更包。

关键点：base_url 只在这里注入一处，manifest 与便携版/安装包随附的 manifest 因此天然一致；
        publish\\packs 用硬链接指向 dist\\packs，几十 GB 也不会二次占盘。

用法：
  # 真实发布（HTTP 下载站）：建议带 --smoke，装配前自动拦坏包
  python make_release.py --version 1.0.0 --base-url https://dl.example.com/avatarhub/1.0.0 \
         --build-packs --include-models --with-installer --smoke
  # 只重新装配 publish（packs/manifest 已就绪）：
  python make_release.py --version 1.0.0 --base-url https://dl.example.com/avatarhub/1.0.0
  # 本地下载站自测（base_url 指向 publish 目录本身）：
  python make_release.py --version 1.0.0 --local-station --build-packs --only --only-models gfpgan
  # 增量发布（与上一版 manifest 比对，发布方视角的待传包）：
  python make_release.py --version 1.1.0 --base-url <url> --build-packs --prev-manifest dist\\publish\\1.0.0\\manifest.json
  # 烟测门禁（单独跑）/ 升级增量校验（老用户各档位实际下载量）：
  python make_release.py --smoke
  python make_release.py --release-diff dist\\publish\\1.0.0\\manifest.json
  # 全流水线一条命令门禁（预检→构建→烟测→装配→增量报告，逐级门禁失败即停，写 dist\\ci_report.json）：
  python make_release.py --ci --version 1.1.0 --base-url <url> --build-packs --include-models \\
         --with-installer --prev-manifest dist\\publish\\1.0.0\\manifest.json
  # 末段追加 STT 实时闭环 SLA 闸门（GPU 验收机；阈值取 config.json[stt_sla]，未达标流水线退码 6）：
  python make_release.py --ci --version 1.1.0 --base-url <url> --build-packs --ci-stt-bench 1,4,8
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
MANIFEST = DIST / "manifest.json"
EXE = DIST / "AvatarHub.exe"
VERSIONS = DIST / "versions.json"   # 历史版本链（累积每次发布的组件 sha 快照，供跨多版"跳版升级"净增量计算）
PYBASE = sys.executable


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def _sha256(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_components(m: dict):
    comps = m.get("components", {})
    for sid, c in comps.get("shared", {}).items():
        yield f"shared:{sid}", c
    for env, c in comps.get("env", {}).items():
        yield f"env:{env}", c
    for g, c in comps.get("model", {}).items():
        yield f"model:{g}", c


def _run(cmd, label):
    print(f"\n[run] {label}: {' '.join(str(c) for c in cmd)}")
    subprocess.run(cmd, check=True, cwd=str(HERE))


def _find_iscc():
    cands = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%ProgramFiles%\Inno Setup 6\ISCC.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
    ]
    return next((c for c in cands if Path(c).exists()), None)


def preflight(args, gate: bool) -> int:
    """母机发布预检：磁盘 / 显存 / conda-pack / 各环境 / ISCC / exe 工具链。
    gate=True 时存在 FAIL 即返回非 0（用于 --preflight 独立闸门）。"""
    import build_packs as bp
    try:
        import pack_installer as pi
    except Exception:
        pi = None

    print("=" * 64)
    print(" 母机发布预检")
    print("=" * 64)
    issues = []  # (level, msg)
    man = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    mcomp = man.get("components", {})

    envs = args.only if args.only is not None else bp.PRODUCT_ENVS
    do_models = args.include_models or bool(args.only_models)
    all_groups = sorted({g for spec in bp.EDITIONS.values() for g in spec["models"]})
    model_groups = (args.only_models if args.only_models else all_groups) if do_models else []

    packed_est = 0
    if envs:
        print(" 环境（解释器是否就位 + 体积估算）：")
    for env in envs:
        prefix = bp._env_prefix(env)
        exists = prefix.exists()
        me = mcomp.get("env", {}).get(env)
        if me:
            sz, src = me.get("size_bytes", 0), "manifest"
        elif exists:
            sz, src = int(bp._dir_size(prefix) * 0.6), "扫描×0.6"
        else:
            sz, src = 0, "-"
        packed_est += sz
        print(f"   [{'OK' if exists else '缺失'}] {env:14s} 估算包 {bp._human(sz):>9s}  ({src})")
        if not exists:
            issues.append(("FAIL", f"环境缺失，无法打包：{env}  ({prefix})"))

    if model_groups:
        print(" 模型组（文件是否就位）：")
        for g in model_groups:
            spec = bp.MODEL_GROUPS.get(g, {})
            ex = [HERE / p for p in spec.get("paths", []) if (HERE / p).exists()]
            mm = mcomp.get("model", {}).get(g)
            if mm:
                sz = mm.get("size_bytes", 0)
            elif ex:
                sz = sum(bp._dir_size(p) for p in ex)
            else:
                sz = 0
            packed_est += sz
            print(f"   [{'OK' if ex else '无'}] model:{g:14s} 估算 {bp._human(sz):>9s}")
            if not ex:
                issues.append(("WARN", f"模型组无任何文件，将跳过：{g}"))

    # 共享基座（--shared）：计入磁盘估算（环境包会变小、但多出基座包）。
    if getattr(args, "shared", False) or mcomp.get("shared"):
        shared = mcomp.get("shared", {})
        if shared:
            for sid, sc in shared.items():
                sz = sc.get("size_bytes", 0)
                packed_est += sz
                print(f"   [OK] shared:{sid:11s} 估算 {bp._human(sz):>9s}")
        else:
            print("   [..] shared 基座将于本次构建产出（torch/CUDA 等，未压缩约 6–12GB / 压缩更小）")

    # 磁盘
    free = shutil.disk_usage(str(HERE)).free
    need = int(packed_est * 1.3)
    print("-" * 64)
    print(f" 产物估算总体积 ≈ {bp._human(packed_est)}；建议预留(×1.3) ≈ {bp._human(need)}")
    print(f" 输出盘可用空间   = {bp._human(free)}  ({Path(HERE).anchor})")
    if packed_est and free < need:
        issues.append(("FAIL", f"磁盘不足：需≈{bp._human(need)}，仅余 {bp._human(free)}"))

    # conda-pack（打环境才需要）
    if envs:
        try:
            import conda_pack  # noqa: F401
            print(" conda-pack：已安装")
        except Exception:
            issues.append(("FAIL", '未装 conda-pack：用 base 解释器 `python -m pip install conda-pack`'))

    # 启动器工具链
    if EXE.exists():
        print(f" 启动器 exe：已存在（{bp._human(EXE.stat().st_size)}）")
    elif (HERE / ".venv_launcher").exists():
        print(" 启动器 exe：缺失，但有 .venv_launcher，可现打")
    else:
        issues.append(("WARN", "无 exe 且无 .venv_launcher，build_launcher.bat 将现建虚拟环境（较慢）"))

    # ISCC（要打安装包才需要）
    if args.with_installer:
        iscc = _find_iscc()
        if iscc:
            print(f" Inno Setup：{iscc}")
        else:
            issues.append(("FAIL", "要 --with-installer 但未找到 ISCC（winget install JRSoftware.InnoSetup）"))

    # GPU（信息项；构建机不强制需要 GPU，但便于核对档位）
    if pi is not None:
        gpus = pi.detect_gpus()
        if gpus:
            for g in gpus:
                print(f" GPU#{g['index']} {g['name']}  显存 {g['total_mb']/1024:.0f}GB")
            best, _ = pi.recommend_edition(man or {"editions": bp.EDITIONS}, gpus)
            print(f" 本机可跑最高档（参考）：{best or '硬件不足'}")
        else:
            print(" GPU：未检测到（构建机不强制需要；用户机才需要）")

    # 结论
    fails = [m for lv, m in issues if lv == "FAIL"]
    warns = [m for lv, m in issues if lv == "WARN"]
    print("-" * 64)
    for m in fails:
        print(f" [FAIL] {m}")
    for m in warns:
        print(f" [WARN] {m}")
    if not issues:
        print(" 预检通过：可以发布。")
    elif not fails:
        print(" 预检通过（有警告，可继续）。")
    else:
        print(" 预检未通过：请先解决上述 FAIL 项。")
    print("=" * 64)
    return 1 if (gate and fails) else 0


def ensure_exe(rebuild: bool):
    if rebuild or not EXE.exists():
        print("[step] 构建启动器 exe …")
        subprocess.run(["cmd", "/c", "build_launcher.bat"], check=True, cwd=str(HERE))
        if not EXE.exists():
            raise SystemExit("[ERROR] build_launcher.bat 未产出 dist\\AvatarHub.exe")
    else:
        print(f"[skip] 已存在 {EXE.name}（--rebuild-exe 可强制重打）")


def build_packs(args):
    cmd = [PYBASE, "build_packs.py", "--build", "--version", args.version, "--arch-tag", args.arch_tag]
    if getattr(args, "shared", False):
        cmd.append("--shared")
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    for m in getattr(args, "mirror", []) or []:
        cmd += ["--mirror", m]
    if getattr(args, "telemetry_url", ""):
        cmd += ["--telemetry-url", args.telemetry_url]
    if args.only is not None:
        cmd += ["--only", *args.only]
    if args.include_models:
        cmd.append("--include-models")
    if args.only_models is not None:
        cmd += ["--only-models", *args.only_models]
    if args.force:
        cmd.append("--force")
    _run(cmd, "build_packs")


def build_portable(args):
    cmd = [PYBASE, "make_portable.py", "--version", args.version]
    if args.base_url:
        cmd += ["--base-url", args.base_url]
    _run(cmd, "make_portable")


def build_installer():
    subprocess.run(["cmd", "/c", "installer\\build_installer.bat"], check=True, cwd=str(HERE))


def _load_prev_shas(src: str) -> dict:
    """读取上一版 manifest（路径或 URL），返回 {cid: sha256}。"""
    try:
        if src.startswith(("http://", "https://")):
            with urllib.request.urlopen(src, timeout=30) as r:
                m = json.loads(r.read().decode("utf-8"))
        else:
            m = json.loads(Path(src).read_text(encoding="utf-8"))
        return {cid: c.get("sha256", "") for cid, c in _iter_components(m)}
    except Exception as e:
        print(f"[warn] 读取 --prev-manifest 失败（按全新发布处理）：{e}")
        return {}


def _link_or_copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)            # 硬链接：同卷不二次占盘
    except OSError:
        shutil.copy2(src, dst)       # 跨卷回退复制


def assemble_publish(args) -> Path:
    if not MANIFEST.exists():
        raise SystemExit("[ERROR] 未找到 dist\\manifest.json，请先 --build-packs 或先跑 build_packs.py。")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    # 未重建但传了 --mirror：直接补丁现有 manifest 的 mirrors（并回写 dist，保持各处一致）。
    patched = False
    if getattr(args, "mirror", None):
        manifest["mirrors"] = [m.rstrip("/") for m in args.mirror]
        patched = True
    if getattr(args, "telemetry_url", ""):
        manifest["telemetry_url"] = args.telemetry_url.strip()
        patched = True
    base_url = (manifest.get("base_url") or "").strip()
    # 站点稳定根：channels.json / versions.json 应放此处（非版本子目录）。默认取 base_url 的父级。
    site_root = (getattr(args, "site_root", "") or "").strip().rstrip("/")
    if not site_root and base_url:
        site_root = base_url.rsplit("/", 1)[0]
    if site_root:
        manifest["versions_url"] = f"{site_root}/versions.json"
        manifest["channels_url"] = f"{site_root}/channels.json"
        patched = True
    if patched:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    ver = manifest.get("version", args.version)
    base_url = (manifest.get("base_url") or "").strip()

    pub = DIST / "publish" / ver
    if pub.exists():
        shutil.rmtree(pub, ignore_errors=True)
    pub.mkdir(parents=True)

    shutil.copy2(MANIFEST, pub / "manifest.json")

    prev = _load_prev_shas(args.prev_manifest) if args.prev_manifest else {}
    index = []
    missing, changed_n, new_n, unchanged_n = [], 0, 0, 0
    dl_ids: set[str] = set()                  # 相对上一版「需用户下载」的组件（新增+变更）
    csize = {cid: c.get("size_bytes", 0) for cid, c in _iter_components(manifest)}

    # manifest 本体（站点根，供客户端「更新检查」拉取）
    mp = pub / "manifest.json"
    index.append({"cid": "manifest", "remote": "manifest.json", "present": True,
                  "size_bytes": mp.stat().st_size, "size_human": _human(mp.stat().st_size),
                  "sha256": _sha256(mp), "status": "always"})

    for cid, c in _iter_components(manifest):
        rel = c["file"].replace("\\", "/")           # 形如 packs/xxx.tar.gz
        srcf = DIST / rel
        present = srcf.exists()
        sha = c.get("sha256", "")
        if cid in prev:
            status = "changed" if prev[cid] != sha else "unchanged"
        else:
            status = "new" if prev else "all"
        if status == "changed":
            changed_n += 1
            dl_ids.add(cid)
        elif status == "new":
            new_n += 1
            dl_ids.add(cid)
        elif status == "unchanged":
            unchanged_n += 1
        if present:
            _link_or_copy(srcf, pub / rel)
        else:
            missing.append(cid)
        index.append({"cid": cid, "remote": rel, "present": present,
                      "size_bytes": c.get("size_bytes", 0), "size_human": c.get("size_human", "?"),
                      "sha256": sha, "status": status})

    (pub / "release_index.json").write_text(
        json.dumps({"project": "AvatarHub", "version": ver, "base_url": base_url,
                    "assembled_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                    "files": index}, ensure_ascii=False, indent=2), encoding="utf-8")

    edition_inc = None
    if prev:                                  # 老用户升级到本版，各档位实际下载（仅涉及的新增+变更）
        edition_inc = {}
        for ed, spec in manifest.get("editions", {}).items():
            ids = _edition_component_ids(manifest, ed, spec)
            inc = sum(csize.get(c, 0) for c in ids if c in dl_ids)
            full = sum(csize.get(c, 0) for c in ids)
            edition_inc[ed] = (inc, full)

    hist = _update_versions_history(manifest, pub, getattr(args, "seed_versions", ""))
    channel = getattr(args, "channel", "") or "stable"
    chans = _update_channels(manifest, pub, channel, getattr(args, "seed_channels", ""))

    _write_upload_md(pub, ver, base_url, index, prev, missing,
                     changed_n, new_n, unchanged_n, edition_inc,
                     n_versions=len(hist["versions"]), channel=channel,
                     channels=list(chans.get("channels", {}).keys()))

    # 摘要
    up_total = sum(e["size_bytes"] for e in index
                   if e["present"] and (not prev or e["status"] in ("new", "changed", "always")))
    print("\n" + "=" * 64)
    print(f" publish 装配完成：{pub}")
    print(f" 版本 {ver}   base_url：{base_url or '(未设)'}")
    if prev:
        print(f" 增量：新增 {new_n} / 变更 {changed_n} / 未变 {unchanged_n}（未变包可不重传）")
    if missing:
        print(f" [注意] 以下组件 manifest 有记录但 dist\\packs 缺文件（需先构建）：{', '.join(missing)}")
    print(f" 待上传体积（含 manifest）：约 {_human(up_total)}")
    print(f" 上传操作单：{pub / 'UPLOAD.md'}")
    return pub


def _write_upload_md(pub, ver, base_url, index, prev, missing,
                     changed_n, new_n, unchanged_n, edition_inc=None, n_versions=0,
                     channel="stable", channels=None):
    present = [e for e in index if e["present"]]
    to_upload = [e for e in present if (not prev or e["status"] in ("new", "changed", "always"))]
    bucket_hint = "<bucket>"
    lines = []
    lines.append(f"# AvatarHub 发布操作单 · v{ver}\n")
    lines.append(f"- base_url：`{base_url or '(未设，发布前务必用 --base-url 指定)'}`")
    lines.append(f"- 下载器取包地址 = `base_url` + 组件 `file`（如 `{base_url}/packs/xxx.tar.gz`）")
    lines.append(f"- 客户端更新检查地址 = `base_url/manifest.json`（已写入 manifest.manifest_url）\n")
    if prev:
        lines.append(f"## 增量（相对上一版）：新增 {new_n} / 变更 {changed_n} / 未变 {unchanged_n}")
        lines.append("- 「未变」包可不重传，但 **manifest.json 必须每次都覆盖上传**。\n")
        if edition_inc:
            lines.append("## 老用户升级到本版的实际下载量（按档位）")
            lines.append("| 档位 | 升级需下载 | 全量 | 占比 |")
            lines.append("| --- | --- | --- | --- |")
            for ed, (inc, full) in edition_inc.items():
                pct = (inc / full * 100) if full else 0
                lines.append(f"| {ed} | {_human(inc)} | {_human(full)} | {pct:.1f}% |")
            lines.append("- 得益于可复现打包：未改动组件 sha 不变，老用户**不重下**。\n")
    if missing:
        lines.append("## ⚠ 缺失组件（manifest 有记录但本地无包文件，需先构建后再发布）")
        for cid in missing:
            lines.append(f"- `{cid}`")
        lines.append("")

    if n_versions:
        lines.append("## 版本链 + 发布通道（跨版升级 / 回滚 / stable·beta）")
        lines.append(f"- 本次发布登记到通道 **{channel}**（现有通道：{'、'.join(channels or [channel])}）。")
        lines.append(f"- 已把 v{ver} 快照写入 `versions.json`（累计 {n_versions} 版，含各版 manifest_url 供回滚定位）。")
        lines.append("- **`versions.json` 与 `channels.json` 必须传到站点【稳定根】**（如 `<站点>/versions.json`、`<站点>/channels.json`，不要放进版本子目录）。")
        lines.append("  客户端据此可算「我当前版→最新版」净下载、按通道选 stable/beta、出问题一键回滚到旧版（旧版 packs 仍在其版本目录）。")
        lines.append("- 干净 CI 机首发可 `--seed-versions <站点>/versions.json --seed-channels <站点>/channels.json` 续接线上历史。\n")

    lines.append("## 站点目标布局")
    lines.append("```")
    lines.append(f"{base_url or '<base_url>'}/")
    lines.append("├─ manifest.json")
    lines.append("├─ ../versions.json   (版本链 → 站点稳定根)")
    lines.append("├─ ../channels.json   (通道表 → 站点稳定根)")
    lines.append("└─ packs/")
    for e in present:
        if e["cid"] != "manifest":
            lines.append(f"   ├─ {Path(e['remote']).name}    ({e['size_human']})")
    lines.append("```\n")

    lines.append("## 待上传清单（present + 需传）")
    for e in to_upload:
        lines.append(f"- `{e['remote']}`  {e['size_human']}  sha256=`{e['sha256'][:16]}…`")
    lines.append("")

    lines.append("## 上传命令（按你的后端任选其一；在 publish 目录上层执行）")
    lines.append("### 阿里云 OSS（ossutil）")
    lines.append("```bash")
    lines.append(f"ossutil cp -r -f publish/{ver}/ oss://{bucket_hint}/avatarhub/{ver}/")
    lines.append("```")
    lines.append("### AWS S3 / 兼容 S3")
    lines.append("```bash")
    lines.append(f"aws s3 sync publish/{ver}/ s3://{bucket_hint}/avatarhub/{ver}/ --size-only")
    lines.append("```")
    lines.append("### 自建服务器（rsync over ssh）")
    lines.append("```bash")
    lines.append(f"rsync -avz --progress publish/{ver}/ user@host:/var/www/avatarhub/{ver}/")
    lines.append("```\n")

    lines.append("## 发布后冒烟验证（任意一台联网机）")
    lines.append("```bash")
    lines.append(f"python pack_installer.py --manifest {base_url or '<base_url>'}/manifest.json --gpu")
    lines.append(f"python pack_installer.py --manifest {base_url or '<base_url>'}/manifest.json --status")
    lines.append("```")
    lines.append("- 期望：能读到 manifest、验机给出推荐档位、各组件 URL 可达（HEAD 200）。")

    (pub / "UPLOAD.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def smoke_test(envs_filter, log=print) -> tuple[bool, dict]:
    """发布烟测门禁：把（基座分片 + 各环境）装进临时目录，逐环境 `import torch` + cuda 可用性。
    用本地 dist 源（不联网），不污染真实安装目录。返回 (全过?, {env: 结果})。"""
    import subprocess
    import tempfile
    import pack_installer as pi
    if not MANIFEST.exists():
        print("[ERROR] 无 dist\\manifest.json，先构建再烟测。")
        return False, {}
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    envcomps = manifest["components"].get("env", {})
    sharedcomps = manifest["components"].get("shared", {})
    targets = [e for e in (envs_filter or list(envcomps)) if e in envcomps]
    if not targets:
        print("[ERROR] 无可烟测环境。")
        return False, {}

    print("=" * 64)
    print(" 发布烟测门禁：装环境 + import torch（临时目录，本地源）")
    print("=" * 64)
    tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
    saved = (pi.BASE, pi.ENVS_ROOT, pi.STORE_DIR, pi.CACHE_DIR, pi.CONFIG_PATH, pi.INSTALLED_STATE)
    pi.BASE = tmp
    pi.ENVS_ROOT = tmp / "runtime" / "envs"
    pi.STORE_DIR = tmp / "runtime" / "_store"
    pi.CACHE_DIR = tmp / "_pack_cache"
    pi.CONFIG_PATH = tmp / "config.json"
    pi.INSTALLED_STATE = tmp / "runtime" / "installed.json"
    results: dict = {}
    try:
        mani, src_root = pi.load_manifest(str(MANIFEST))
        # 收集所有需要的分片（先）+ 目标环境（后），去重一次装好
        ordered, seen = [], set()
        for env in targets:
            for sid in envcomps[env].get("needs_shared", []):
                if sid in sharedcomps and ("shared:" + sid) not in seen:
                    ordered.append((f"shared:{sid}", sharedcomps[sid])); seen.add("shared:" + sid)
        for env in targets:
            ordered.append((f"env:{env}", envcomps[env]))
        print(f" 安装 {len(targets)} 环境 + {len(seen)} 分片 → {tmp}")
        failed = pi.install_components(mani, ordered, src_root, log=lambda *_: None)
        if failed:
            print(" [FAIL] 安装阶段失败：", failed)
            for cid, _ in failed:
                results[cid] = "install-fail"
        for env in targets:
            py = pi.ENVS_ROOT / env / "python.exe"
            if not py.exists():
                results[env] = "no-python"; print(f"   [{env}] FAIL 无 python.exe"); continue
            r = subprocess.run(
                [str(py), "-c", "import torch;print(torch.__version__, torch.cuda.is_available())"],
                capture_output=True, text=True, timeout=300)
            if r.returncode == 0:
                results[env] = "ok"; print(f"   [{env}] OK  {r.stdout.strip()}")
            else:
                results[env] = "import-fail"; print(f"   [{env}] FAIL\n{r.stderr[-400:]}")
        allok = bool(targets) and all(results.get(e) == "ok" for e in targets)
        print("-" * 64)
        print(" 烟测结果：" + ("全部通过 ✓" if allok else "存在失败 ✗ → 不应发布"))
        return allok, results
    finally:
        (pi.BASE, pi.ENVS_ROOT, pi.STORE_DIR, pi.CACHE_DIR, pi.CONFIG_PATH, pi.INSTALLED_STATE) = saved
        shutil.rmtree(tmp, ignore_errors=True)


def _read_manifest_any(p: str) -> dict:
    """读取 manifest：本地路径或 http(s) URL。"""
    if p.lower().startswith(("http://", "https://")):
        import urllib.request
        with urllib.request.urlopen(p, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _edition_component_ids(m: dict, ed: str, spec: dict) -> list[str]:
    ids = [f"shared:{s}" for s in spec.get("shared", [])]
    ids += [f"env:{e}" for e in spec.get("envs", [])]
    ids += [f"model:{g}" for g in spec.get("models", [])]
    comps = m.get("components", {})
    have = {f"{k}:{name}" for k in ("shared", "env", "model") for name in comps.get(k, {})}
    return [i for i in ids if i in have]


def release_diff(prev_path: str, log=print) -> int:
    """可复现发布增量校验：对比上一版与当前 manifest，按组件分类（新增/变更/未变/移除），
    并按档位算出「老用户升级到本版的实际下载字节」——量化"省更新"，也反向验证可复现
    （未改动内容 sha 不变 → 不重下）。返回 0。"""
    if not MANIFEST.exists():
        print("[ERROR] 无 dist\\manifest.json，先构建。")
        return 2
    cur = json.loads(MANIFEST.read_text(encoding="utf-8"))
    try:
        prev = _read_manifest_any(prev_path)
    except Exception as e:
        print(f"[ERROR] 读取上一版 manifest 失败：{e}")
        return 2

    def cmap(m):
        return {cid: (c.get("sha256", ""), c.get("size_bytes", 0)) for cid, c in _iter_components(m)}
    pc, cc = cmap(prev), cmap(cur)
    new = [c for c in cc if c not in pc]
    changed = [c for c in cc if c in pc and cc[c][0] != pc[c][0]]
    removed = [c for c in pc if c not in cc]
    unchanged = [c for c in cc if c in pc and cc[c][0] == pc[c][0]]
    dl_ids = set(new) | set(changed)

    print("=" * 64)
    print(" 可复现发布增量校验  %s → %s" % (prev.get("version", "?"), cur.get("version", "?")))
    print("=" * 64)
    print(" 组件级：新增 %d / 变更 %d / 未变 %d / 移除 %d"
          % (len(new), len(changed), len(unchanged), len(removed)))
    for cid in sorted(changed):
        print("   ~ 变更 %-28s %s" % (cid, _human(cc[cid][1])))
    for cid in sorted(new):
        print("   + 新增 %-28s %s" % (cid, _human(cc[cid][1])))
    for cid in sorted(removed):
        print("   - 移除 %-28s" % cid)
    total_changed = sum(cc[c][1] for c in dl_ids)
    print(" 发布方需上传（新增+变更包）：%s（未变包免传，manifest 必传）" % _human(total_changed))
    print("-" * 64)
    print(" 老用户升级各档位实际下载（仅该档位涉及的新增+变更组件）：")
    for ed, spec in cur.get("editions", {}).items():
        ids = _edition_component_ids(cur, ed, spec)
        inc = sum(cc[c][1] for c in ids if c in dl_ids)
        full = sum(cc[c][1] for c in ids)
        pct = (inc / full * 100) if full else 0
        print("   %-9s 增量 %9s / 全量 %9s（%.1f%%）" % (ed, _human(inc), _human(full), pct))
    if not new and not changed:
        print(" ✓ 无任何组件变化：可复现构建确认，老用户升级零下载（仅刷新 manifest）。")
    return 0


def _snapshot_of(m: dict) -> dict:
    """把一版 manifest 压成跨版增量所需的最小快照：仅 {cid: [sha, size]}（每版几 KB）。
    存它即可在不保留 N 份旧 manifest 的前提下，算任意旧版→当前的【净】下载量。"""
    return {cid: [c.get("sha256", ""), c.get("size_bytes", 0)] for cid, c in _iter_components(m)}


def _load_versions(src: str = "") -> dict:
    """读历史版本链：给 src（路径/URL）则读它，否则读 dist/versions.json；无则返回空骨架。"""
    raw = None
    try:
        if src:
            raw = _read_manifest_any(src)
        elif VERSIONS.exists():
            raw = json.loads(VERSIONS.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] 读取版本链失败（按空处理）：{e}")
    if not isinstance(raw, dict) or "versions" not in raw:
        return {"project": "AvatarHub", "versions": []}
    return raw


def _update_versions_history(manifest: dict, pub: Path, seed: str = ""):
    """发布时把当前版本快照 upsert 进版本链：先（可选）从 seed 拉既有链（便于干净 CI 机续接
    线上历史），再写回 dist/versions.json 并随 publish 产出，建议传到站点【稳定根】供客户端发现。"""
    hist = _load_versions(seed)
    # 若同时存在本地链与 seed，合并：以版本号去重，seed 优先做底再叠本地（保证不丢历史）。
    if seed and VERSIONS.exists():
        local = _load_versions("")
        seen = {e.get("version") for e in hist["versions"]}
        for e in local["versions"]:
            if e.get("version") not in seen:
                hist["versions"].append(e)
    ver = manifest.get("version", "")
    entry = {"version": ver,
             "date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
             "base_url": (manifest.get("base_url") or "").strip(),
             "manifest_url": (manifest.get("manifest_url") or "").strip(),
             "comps": _snapshot_of(manifest)}
    hist["versions"] = [e for e in hist["versions"] if e.get("version") != ver] + [entry]
    hist["project"] = manifest.get("project", "AvatarHub")
    txt = json.dumps(hist, ensure_ascii=False, indent=2)
    VERSIONS.write_text(txt, encoding="utf-8")
    (pub / "versions.json").write_text(txt, encoding="utf-8")
    return hist


def _compute_upgrade_matrix(cur: dict, hist: dict) -> dict:
    """纯计算：返回 {cur_ver, eds, ed_full, rows:[{version, inc:{ed:bytes}}], worst:{ed:bytes}}。
    正确性核心：每行直接拿【旧版快照 sha】比【当前 sha】，故"改了又回滚"的包 sha 复原即计 0。"""
    cur_ver = cur.get("version", "?")
    cur_snap = _snapshot_of(cur)
    editions = cur.get("editions", {})
    eds = list(editions.keys())
    ed_ids = {ed: _edition_component_ids(cur, ed, spec) for ed, spec in editions.items()}
    ed_full = {ed: sum(cur_snap.get(c, ["", 0])[1] for c in ed_ids[ed]) for ed in eds}
    olds = [e for e in hist.get("versions", []) if e.get("version") != cur_ver]
    rows, worst = [], {ed: 0 for ed in eds}
    for e in sorted(olds, key=lambda x: x.get("version", "")):
        old = e.get("comps", {})
        inc = {ed: sum(cur_snap.get(c, ["", 0])[1] for c in ed_ids[ed]
                       if old.get(c, ["", 0])[0] != cur_snap.get(c, ["", 0])[0]) for ed in eds}
        for ed in eds:
            worst[ed] = max(worst[ed], inc[ed])
        rows.append({"version": e.get("version", "?"), "inc": inc})
    return {"cur_ver": cur_ver, "eds": eds, "ed_full": ed_full, "rows": rows, "worst": worst}


CHANNELS = DIST / "channels.json"   # 发布通道表（stable/beta…→ 各通道最新 manifest_url），传站点稳定根


def _update_channels(manifest: dict, pub: Path, channel: str, seed: str = ""):
    """把本次发布登记为某通道（默认 stable）的最新版：upsert channels[channel]={version,manifest_url,date}。
    seed 可从线上既有 channels.json 续接（干净 CI 机首发）。写 dist 并随 publish 产出，传站点稳定根。"""
    data = {"project": "AvatarHub", "channels": {}}
    try:
        if seed:
            data = _read_manifest_any(seed)
        elif CHANNELS.exists():
            data = json.loads(CHANNELS.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] 读取 channels 失败（按空处理）：{e}")
    data.setdefault("channels", {})
    if seed and CHANNELS.exists():            # seed 与本地并存：本地未含的通道补回，避免丢道
        local = json.loads(CHANNELS.read_text(encoding="utf-8")).get("channels", {})
        for k, v in local.items():
            data["channels"].setdefault(k, v)
    data["channels"][channel] = {
        "version": manifest.get("version", ""),
        "manifest_url": (manifest.get("manifest_url") or "").strip(),
        "date": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    txt = json.dumps(data, ensure_ascii=False, indent=2)
    CHANNELS.write_text(txt, encoding="utf-8")
    (pub / "channels.json").write_text(txt, encoding="utf-8")
    return data


def upgrade_matrix(versions_src: str = "", log=print) -> int:
    """跨多版「跳版升级」矩阵：对版本链中【每个】历史版本，算出升级到当前版各档位的【净】下载量。
    关键正确性：直接比对 旧版快照 sha vs 当前 sha——某包改了又改回（sha 复原）则净增量为 0，
    naive 地逐版累加 diff 会高估，这里不会。返回 0；无历史返回 1。"""
    if not MANIFEST.exists():
        print("[ERROR] 无 dist\\manifest.json，先构建。")
        return 2
    cur = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mx = _compute_upgrade_matrix(cur, _load_versions(versions_src))
    if not mx["rows"]:
        print("版本链中暂无其它历史版本（首版或链为空）：发布过 ≥2 版后此矩阵才有意义。")
        return 1

    eds, ed_full, worst = mx["eds"], mx["ed_full"], mx["worst"]

    def cell(b, ed):
        return "%9s/%-5s" % (_human(b), "%.0f%%" % (b / (ed_full[ed] or 1) * 100))

    print("=" * 72)
    print(" 跨版升级矩阵：历史各版 → 当前 v%s 的【净】下载量（按档位）" % mx["cur_ver"])
    print("=" * 72)
    print("   %-12s %s" % ("来源版本", "  ".join("%-16s" % e for e in eds)))
    for r in mx["rows"]:
        print("   v%-11s %s" % (r["version"], "  ".join("%-16s" % cell(r["inc"][ed], ed) for ed in eds)))
    print("-" * 72)
    print("   %-12s %s" % ("最坏情况", "  ".join("%-16s" % cell(worst[ed], ed) for ed in eds)))
    print("   %-12s %s" % ("全量(当前)", "  ".join("%-16s" % _human(ed_full[ed]) for ed in eds)))
    print("- 净下载＝当前 sha 与该旧版不同的组件之和；改了又回滚的包 sha 复原→计 0（不会高估）。")
    return 0


def telemetry_report(src: str, log=print) -> int:
    """聚合客户端匿名回执成发布质量看板：读 <目录>下 *.json 或一个 .jsonl（每行一回执），
    汇总安装成功率、最常失败组件、错误类名分布、档位/版本/通道分布、下载源命中。返回 0。"""
    p = Path(src)
    recs = []
    try:
        if p.is_dir():
            for f in sorted(p.glob("*.json")):
                try:
                    recs.append(json.loads(f.read_text(encoding="utf-8-sig")))
                except Exception:
                    pass
        elif p.suffix.lower() == ".jsonl":
            for line in p.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
        else:
            recs.append(json.loads(p.read_text(encoding="utf-8-sig")))
    except Exception as e:
        print(f"[ERROR] 读取回执失败：{e}")
        return 2
    if not recs:
        print("无可聚合的回执（目录无 *.json / 文件为空）。")
        return 1

    a = _aggregate_receipts(recs)

    def _bar(d, topn=8):
        return [f"   {k:28s} {v}" for k, v in sorted(d.items(), key=lambda x: -x[1])[:topn]]

    print("=" * 64)
    print(" 发布质量看板（来自 %d 份匿名回执）" % a["n"])
    print("=" * 64)
    print(" 会话成功率：%d/%d（%.1f%%）  组件成功率：%d/%d（%.1f%%）" % (
        a["sess_ok"], a["n"], a["sess_ok_pct"],
        a["comp_ok"], a["comp_total"], a["comp_ok_pct"]))
    print(" 操作类型：" + "，".join(f"{k} {v}" for k, v in
          sorted(a["by_kind"].items(), key=lambda x: -x[1])))
    if a["fail_by_cid"]:
        print("-" * 64)
        print(" 最常失败组件（cid → 次数）："); print("\n".join(_bar(a["fail_by_cid"])))
        print(" 失败错误类名分布："); print("\n".join(_bar(a["err_by_class"])))
    print("-" * 64)
    print(" 档位分布："); print("\n".join(_bar(a["by_edition"])))
    print(" 版本分布："); print("\n".join(_bar(a["by_version"])))
    print(" 通道分布："); print("\n".join(_bar(a["by_channel"])))
    if a["src_hit"]:
        print(" 主用下载源命中（主机名）："); print("\n".join(_bar(a["src_hit"]))) 
    return 0


def _aggregate_receipts(recs: list) -> dict:
    """把回执列表聚合成质量看板数据（纯计算，便于测试）。"""
    import collections
    n = len(recs)
    sess_ok = sum(1 for r in recs if r.get("fail", 0) == 0)
    comp_total = comp_fail = 0
    fail_by_cid, err_by_class = collections.Counter(), collections.Counter()
    by_edition, by_version, by_channel, by_kind, src_hit = (collections.Counter() for _ in range(5))
    for r in recs:
        by_edition[r.get("edition", "") or "-"] += 1
        by_version[r.get("manifest_version", "") or "-"] += 1
        by_channel[r.get("channel", "") or "stable"] += 1
        by_kind[r.get("kind", "") or "-"] += 1
        for s in r.get("sources", [])[:1]:
            src_hit[s] += 1
        for it in r.get("items", []):
            comp_total += 1
            if not it.get("ok", True):
                comp_fail += 1
                fail_by_cid[it.get("cid", "?")] += 1
                if it.get("err"):
                    err_by_class[it["err"]] += 1
    return {
        "n": n, "sess_ok": sess_ok, "sess_ok_pct": (sess_ok / n * 100) if n else 100,
        "comp_total": comp_total, "comp_ok": comp_total - comp_fail,
        "comp_ok_pct": ((comp_total - comp_fail) / comp_total * 100) if comp_total else 100,
        "fail_by_cid": dict(fail_by_cid), "err_by_class": dict(err_by_class),
        "by_edition": dict(by_edition), "by_version": dict(by_version),
        "by_channel": dict(by_channel), "by_kind": dict(by_kind), "src_hit": dict(src_hit),
    }


def run_stt_gate(ladder: str) -> int:
    """CI 末段 STT 实时闭环 SLA 闸门：以 facefusion 环境跑 interp_selfcheck.py --stt-bench，
    其退出码即门禁结果(0 达标/非0 未达标或不通)。SLA 阈值由 interp_selfcheck 读 config.json[stt_sla]
    （命令行/环境变量可覆盖,见 11-C/13-C）。需 Hub/nemo_stt/GPU 就绪;故为 opt-in。"""
    try:
        import app_config
        py = app_config.conda_python("facefusion")
        if not Path(py).exists():
            py = PYBASE
    except Exception:
        py = PYBASE
    cmd = [py, "interp_selfcheck.py", "--stt-bench", ladder, "--ci"]
    print("[run] STT 闭环 SLA 闸门: " + " ".join(str(c) for c in cmd))
    try:
        return subprocess.run(cmd, cwd=str(HERE)).returncode
    except Exception as e:
        print(f"[ERROR] STT 闸门执行失败：{e}")
        return 1


def _pipeline_summary(stages: list[dict], passed: bool, exit_code: int):
    print("\n" + "=" * 64)
    print(" CI 发布流水线小结")
    print("=" * 64)
    for s in stages:
        sec = (" %.1fs" % s["sec"]) if s.get("sec") else ""
        print("   %-12s %-22s%s" % (s["name"], s["status"], sec))
    print("-" * 64)
    print(" 结果：" + ("全部通过 ✓ 可发布" if passed else "未通过 ✗ 已中止（不产出/不发布）"))
    # 机器可读结论：真实 CI（GitHub Actions 等）可据此 gate 并在面板展示。
    report = {
        "ok": passed,
        "exit_code": exit_code,
        "at": datetime.now().isoformat(timespec="seconds"),
        "stages": stages,
    }
    try:
        DIST.mkdir(parents=True, exist_ok=True)
        rp = DIST / "ci_report.json"
        rp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(" 机器可读报告：%s" % rp)
    except Exception as e:
        print(" [warn] 写 ci_report.json 失败：%s" % e)


def run_pipeline(args) -> int:
    """一条命令跑完整发布质量闸：预检→(构建)→烟测→装配→增量报告，逐级门禁、失败即停。
    无 --build-packs 时复用现有 dist（便于快速重跑闸门）；烟测在装配前拦坏包。
    退出码：0 全过 / 2 预检挂 / 3 烟测挂 / 5 验收快检挂 / 6 STT 实时闭环未达标；
    并写 dist/ci_report.json 供外部 CI 消费。"""
    stages: list[dict] = []

    def add(name, status, t0):
        stages.append({"name": name, "status": status, "sec": round(time.time() - t0, 1)})

    print("\n########## CI 发布流水线开始 ##########")

    print("\n[闸门] 母机预检 …")
    t = time.time()
    if preflight(args, gate=True) != 0:
        add("预检", "FAIL", t); _pipeline_summary(stages, False, 2); return 2
    add("预检", "PASS", t)

    print("\n[步骤] 启动器 exe …")
    t = time.time(); ensure_exe(args.rebuild_exe); add("exe", "OK", t)

    print("\n[步骤] 构建分发包 …")
    t = time.time()
    if args.build_packs:
        build_packs(args); add("构建", "PASS", t)
    else:
        add("构建", "SKIP（复用现有 dist）", t)

    t = time.time()
    if not args.skip_portable:
        build_portable(args); add("便携版", "OK", t)
    else:
        add("便携版", "SKIP", t)
    if args.with_installer:
        t = time.time(); build_installer(); add("安装包", "OK", t)

    print("\n[闸门] 发布烟测（装配前，装环境+import torch）…")
    t = time.time()
    ok, _ = smoke_test(args.smoke_envs)
    if not ok:
        add("烟测", "FAIL", t); _pipeline_summary(stages, False, 3)
        print("[GATE] 烟测未通过：已中止，不生成 publish 树。")
        return 3
    add("烟测", "PASS", t)

    print("\n[步骤] 装配 publish …")
    t = time.time(); assemble_publish(args); add("装配", "OK", t)

    if args.prev_manifest:
        print("\n[报告] 相对上一版的增量 …")
        t = time.time(); release_diff(args.prev_manifest); add("增量报告", "DONE", t)

    if len(_load_versions("")["versions"]) >= 2:
        print("\n[报告] 跨版升级矩阵 …")
        t = time.time(); upgrade_matrix(""); add("跨版矩阵", "DONE", t)

    if getattr(args, "ci_acceptance", False) or getattr(args, "acceptance_quick", False):
        print("\n[闸门] 端到端验收快检（探测包链路）…")
        t = time.time()
        import pack_acceptance as pa
        a2 = argparse.Namespace(quick=True, envs=None, with_telemetry_server=False)
        rc = pa.run_acceptance(a2)
        add("验收快检", "PASS" if rc == 0 else "FAIL", t)
        if rc != 0:
            _pipeline_summary(stages, False, 5)
            return 5

    if getattr(args, "ci_stt_bench", ""):
        print("\n[闸门] STT 实时闭环 SLA（barge-in + 并发阶梯 + 达标线）…")
        t = time.time()
        rc = run_stt_gate(args.ci_stt_bench)
        add("STT闭环", "PASS" if rc == 0 else "FAIL", t)
        if rc != 0:
            _pipeline_summary(stages, False, 6)
            print("[GATE] STT 实时闭环未达标：已中止。")
            return 6

    _pipeline_summary(stages, True, 0)
    return 0


def main():
    ap = argparse.ArgumentParser(description="一键发布编排器")
    ap.add_argument("--version", default="1.0.0")
    ap.add_argument("--base-url", default="", help="下载站根 URL（真实发布必填）")
    ap.add_argument("--local-station", action="store_true",
                    help="自测：base_url 指向 publish 目录本身（本地源跑通安装闭环）")
    ap.add_argument("--arch-tag", default="cu128")
    ap.add_argument("--rebuild-exe", action="store_true")
    ap.add_argument("--build-packs", action="store_true", help="构建分发包 + manifest")
    ap.add_argument("--shared", action="store_true",
                    help="抽 torch/CUDA 共享基座（只下一次，环境包大降）；传给 build_packs --shared")
    ap.add_argument("--only", nargs="*", help="只打指定环境（给 --only 不跟值=不打环境）")
    ap.add_argument("--include-models", action="store_true")
    ap.add_argument("--only-models", nargs="*", help="只打指定模型组")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--skip-portable", action="store_true")
    ap.add_argument("--with-installer", action="store_true", help="编译 Inno 安装包（需 ISCC）")
    ap.add_argument("--prev-manifest", default="", help="上一版 manifest（路径/URL）用于增量发布对比")
    ap.add_argument("--preflight", action="store_true", help="只做母机发布预检（磁盘/显存/conda-pack/环境/ISCC）后退出")
    ap.add_argument("--smoke", action="store_true", help="发布烟测门禁：装环境+import torch（装配前闸门，失败则不发布；也可单独运行）")
    ap.add_argument("--smoke-envs", nargs="*", help="只烟测指定环境（默认全部）")
    ap.add_argument("--release-diff", default="", help="对比上一版 manifest（路径/URL），报告组件变化与各档位老用户升级下载量后退出")
    ap.add_argument("--ci", action="store_true", help="一条命令跑完整质量闸：预检→(构建)→烟测→装配→增量报告，逐级门禁失败即停")
    ap.add_argument("--upgrade-matrix", nargs="?", const="", default=None,
                    help="跨版升级矩阵：历史各版→当前版各档位净下载量（可给 versions.json 路径/URL，默认读 dist/versions.json）后退出")
    ap.add_argument("--seed-versions", default="",
                    help="发布时从该 versions.json（路径/URL）续接历史版本链（干净 CI 机首发用）")
    ap.add_argument("--mirror", action="append", default=[],
                    help="镜像根 URL（与 base_url 同布局，可重复）；写入 manifest.mirrors，客户端择优+failover")
    ap.add_argument("--channel", default="stable", help="本次发布登记到的通道（stable/beta…），写入 channels.json")
    ap.add_argument("--site-root", default="",
                    help="站点稳定根 URL（放 channels.json/versions.json）；默认取 base_url 父级")
    ap.add_argument("--seed-channels", default="",
                    help="发布时从该 channels.json（路径/URL）续接通道表（干净 CI 机首发用）")
    ap.add_argument("--telemetry-url", default="",
                    help="匿名健康回执上报端点 URL（写入 manifest.telemetry_url，可选）")
    ap.add_argument("--telemetry-report", default="",
                    help="聚合回执成发布质量看板：给 <目录>（含 *.json）或 .jsonl 文件后退出")
    ap.add_argument("--acceptance", action="store_true",
                    help="端到端真机验收（install→update→rollback→telemetry，写 dist/acceptance_report.json）")
    ap.add_argument("--acceptance-quick", action="store_true", help="验收快检：仅探测包链路（秒级）")
    ap.add_argument("--ci-acceptance", action="store_true",
                    help="CI 流水线末段追加 --acceptance-quick 快检（不替代发布前完整 --acceptance）")
    ap.add_argument("--ci-stt-bench", metavar="LADDER", default="",
                    help="CI 末段追加 STT 实时闭环 SLA 闸门（如 8 或 1,4,8,16；需 Hub/nemo_stt/GPU 就绪）。"
                         "阈值读 config.json[stt_sla];未达标则流水线以退出码 6 失败")
    ap.add_argument("--gui-acceptance", action="store_true",
                    help="GUI/安装包验收（首启向导+维护页+Inno/便携清单，写 dist/gui_acceptance_report.json）")
    args = ap.parse_args()

    if args.gui_acceptance:
        import pack_gui_acceptance as pga
        return pga.run_gui_acceptance(argparse.Namespace(with_exe_selftest=False))

    if args.acceptance or args.acceptance_quick:
        import pack_acceptance as pa
        a2 = argparse.Namespace(quick=args.acceptance_quick, envs=None,
                                 with_telemetry_server=False)
        return pa.run_acceptance(a2)

    if args.telemetry_report:
        return telemetry_report(args.telemetry_report)

    if args.preflight:
        return preflight(args, gate=True)

    if args.release_diff:
        return release_diff(args.release_diff)

    if args.upgrade_matrix is not None:
        return upgrade_matrix(args.upgrade_matrix)

    if args.ci:
        return run_pipeline(args)

    if args.smoke and not (args.build_packs or args.with_installer):
        ok, _ = smoke_test(args.smoke_envs)
        return 0 if ok else 3

    # 本地站：base_url 指向 publish 目录（必须在构建前确定，注入 manifest/便携版）
    if args.local_station:
        args.base_url = str(DIST / "publish" / args.version)
        print(f"[local-station] base_url = {args.base_url}")

    ensure_exe(args.rebuild_exe)
    if args.build_packs:
        preflight(args, gate=False)   # 构建前先做一次预检（仅提示，不阻断）
        build_packs(args)
    if not args.skip_portable:
        build_portable(args)
    if args.with_installer:
        build_installer()
    # 烟测门禁：在装配/发布之前拦截坏包——任一环境装不起来/import 失败即中止，绝不产出 publish 树。
    if args.smoke:
        ok, _ = smoke_test(args.smoke_envs)
        if not ok:
            print("[GATE] 烟测未通过：已中止，不生成 publish 树。修复后重跑。")
            return 3
    assemble_publish(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
