# -*- coding: utf-8 -*-
"""pack_acceptance.py — B-15 端到端真机验收（干净沙箱：install→update→rollback→telemetry）

在**隔离临时目录**模拟「无 conda、无既有 runtime」的用户机，用本地 dist 源跑通：
  首装（真实环境包）→ import torch → 增量更新 → 一键回滚 → 匿名回执。

设计要点：
  - **真 + 轻**：cosytts+rvc 走真实 zst 大包（验 conda-unpack / 基座 / torch）；更新/回滚用
    百字节级 accept_probe 探测包（不篡改 torch 分片，快且可预期）。
  - **干净 PATH**：子进程剔除 conda/miniconda/anaconda，证明「免 conda 首装」成立。
  - **双版本本地站**：v1/v2 各带独立 manifest + base_url + 探测包差异，versions.json 供回滚。
  - **报告**：dist/acceptance_report.json（机器可读）+ 控制台摘要。

用法：
  python pack_acceptance.py                     # 默认 minimal（cosytts+rvc，约 3.5GB 本地 IO）
  python pack_acceptance.py --quick             # 仅探测包链路（秒级，CI 快检）
  python pack_acceptance.py --with-telemetry-server
  python pack_acceptance.py --quick --stt-bench 1,4,8   # 末段加 STT 实时闭环 SLA 闸门(需 GPU/服务就绪)
  python make_release.py --acceptance
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
MANIFEST = DIST / "manifest.json"
REPORT = DIST / "acceptance_report.json"
DEFAULT_ENVS = ("cosytts", "rvc")


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def _clean_path() -> str:
    """剔除 PATH 中的 conda，模拟干净用户机。"""
    parts = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        low = p.lower()
        if any(k in low for k in ("miniconda", "anaconda", "conda\\", "conda/")):
            continue
        if p:
            parts.append(p)
    return os.pathsep.join(parts)


def _make_probe_pack(out: Path, marker: str) -> tuple[str, int]:
    """生成可复现的 tiny model tar.zst；返回 (sha256, size_bytes)。"""
    import zstandard

    bio = io.BytesIO()
    payload = marker.encode("utf-8")
    info = tarfile.TarInfo(name="runtime/accept_probe/marker.txt")
    info.size = len(payload)
    info.mtime = 0
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    with tarfile.open(fileobj=bio, mode="w", format=tarfile.GNU_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(payload))
    raw = bio.getvalue()
    cctx = zstandard.ZstdCompressor(level=19, threads=1)
    compressed = cctx.compress(raw)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(compressed)
    return hashlib.sha256(compressed).hexdigest(), len(compressed)


def _link_or_copy(src: Path, dst: Path):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _components_for_envs(manifest: dict, envs: list[str]) -> list[tuple[str, dict]]:
    """按 smoke 顺序：各 env 的 needs_shared（去重）+ env 本体。"""
    envcomps = manifest["components"].get("env", {})
    sharedcomps = manifest["components"].get("shared", {})
    ordered, seen = [], set()
    for env in envs:
        for sid in envcomps.get(env, {}).get("needs_shared", []):
            key = f"shared:{sid}"
            if sid in sharedcomps and key not in seen:
                ordered.append((key, sharedcomps[sid]))
                seen.add(key)
    for env in envs:
        if env in envcomps:
            ordered.append((f"env:{env}", envcomps[env]))
    probe = manifest["components"].get("model", {}).get("accept_probe")
    if probe:
        ordered.append(("model:accept_probe", probe))
    return ordered


def _build_local_site(work: Path, dist_manifest: dict) -> dict:
    """构建 v1/v2 本地双版本站 + versions.json；packs 硬链 dist，探测包各版独立。"""
    site = work / "site"
    v1_dir = site / "v1.0.0"
    v2_dir = site / "v2.0.0"
    for d in (v1_dir, v2_dir):
        (d / "packs").mkdir(parents=True, exist_ok=True)

    src_packs = DIST / "packs"
    if src_packs.exists():
        for f in src_packs.iterdir():
            if f.is_file():
                _link_or_copy(f, v1_dir / "packs" / f.name)
                _link_or_copy(f, v2_dir / "packs" / f.name)

    sha1, sz1 = _make_probe_pack(v1_dir / "packs" / "model-accept-probe.tar.zst", "accept-v1")
    sha2, sz2 = _make_probe_pack(v2_dir / "packs" / "model-accept-probe.tar.zst", "accept-v2")

    def _snap(m: dict) -> dict:
        out = {}
        for sid, c in m["components"].get("shared", {}).items():
            out[f"shared:{sid}"] = [c["sha256"], c["size_bytes"]]
        for env, c in m["components"].get("env", {}).items():
            out[f"env:{env}"] = [c["sha256"], c["size_bytes"]]
        for grp, c in m["components"].get("model", {}).items():
            out[f"model:{grp}"] = [c["sha256"], c["size_bytes"]]
        return out

    def _clone_manifest(ver: str, base: Path, sha: str, size: int) -> dict:
        m = json.loads(json.dumps(dist_manifest))
        m["version"] = ver
        m["base_url"] = str(base).replace("\\", "/")
        m["manifest_url"] = str((base / "manifest.json")).replace("\\", "/")
        m["versions_url"] = str((site / "versions.json")).replace("\\", "/")
        m.setdefault("components", {}).setdefault("model", {})["accept_probe"] = {
            "kind": "model", "group": "accept_probe",
            "label": "验收探测包（仅测试，不进生产发布）",
            "file": "packs/model-accept-probe.tar.zst",
            "sha256": sha, "size_bytes": size, "size_human": _human(size),
            "members": ["runtime/accept_probe/marker.txt"],
        }
        return m

    m1 = _clone_manifest("1.0.0", v1_dir, sha1, sz1)
    m2 = _clone_manifest("2.0.0", v2_dir, sha2, sz2)
    (v1_dir / "manifest.json").write_text(json.dumps(m1, ensure_ascii=False, indent=2), encoding="utf-8")
    (v2_dir / "manifest.json").write_text(json.dumps(m2, ensure_ascii=False, indent=2), encoding="utf-8")

    versions = {
        "project": "AvatarHub",
        "versions": [
            {"version": "1.0.0", "date": "2026-06-19T00:00:00+08:00",
             "base_url": m1["base_url"], "manifest_url": m1["manifest_url"],
             "comps": _snap(m1)},
            {"version": "2.0.0", "date": "2026-06-19T12:00:00+08:00",
             "base_url": m2["base_url"], "manifest_url": m2["manifest_url"],
             "comps": _snap(m2)},
        ],
    }
    (site / "versions.json").write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"v1_manifest": v1_dir / "manifest.json", "v2_manifest": v2_dir / "manifest.json",
            "versions": site / "versions.json", "v1_root": str(v1_dir), "v2_root": str(v2_dir),
            "m1": m1, "m2": m2}


@contextlib.contextmanager
def _sandbox(base: Path):
    """把 pack_installer / telemetry / app_config 指到隔离目录。"""
    import app_config
    import pack_installer as pi

    saved_env = os.environ.get("AVATARHUB_BASE")
    saved_app_base = app_config.BASE
    saved_pi = (pi.BASE, pi.ENVS_ROOT, pi.STORE_DIR, pi.CACHE_DIR, pi.CONFIG_PATH, pi.INSTALLED_STATE)
    os.environ["AVATARHUB_BASE"] = str(base)
    app_config.BASE = base
    app_config.BASE_DIR = base
    pi.BASE = base
    pi.ENVS_ROOT = base / "runtime" / "envs"
    pi.STORE_DIR = base / "runtime" / "_store"
    pi.CACHE_DIR = base / "_pack_cache"
    pi.CONFIG_PATH = base / "config.json"
    pi.INSTALLED_STATE = base / "runtime" / "installed.json"
    tele_saved = None
    try:
        import telemetry as tele
        tele_saved = (tele.TELE_DIR, tele.TELE_CONF)
        tele.TELE_DIR = base / "runtime" / "telemetry"
        tele.TELE_CONF = tele.TELE_DIR / "config.json"
    except Exception:
        tele = None
    try:
        yield pi, tele
    finally:
        if saved_env is None:
            os.environ.pop("AVATARHUB_BASE", None)
        else:
            os.environ["AVATARHUB_BASE"] = saved_env
        app_config.BASE = saved_app_base
        app_config.BASE_DIR = saved_app_base
        pi.BASE, pi.ENVS_ROOT, pi.STORE_DIR, pi.CACHE_DIR, pi.CONFIG_PATH, pi.INSTALLED_STATE = saved_pi
        if tele_saved and tele is not None:
            tele.TELE_DIR, tele.TELE_CONF = tele_saved


def _torch_smoke(pi, envs: list[str], clean_path: str) -> dict:
    results = {}
    for env in envs:
        py = pi.ENVS_ROOT / env / "python.exe"
        if not py.exists():
            results[env] = "no-python"
            continue
        r = subprocess.run(
            [str(py), "-c", "import torch;print(torch.__version__, torch.cuda.is_available())"],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "PATH": clean_path})
        results[env] = "ok" if r.returncode == 0 else f"fail:{r.stderr[-200:]}"
    return results


def _read_marker(base: Path) -> str:
    p = base / "runtime" / "accept_probe" / "marker.txt"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _stage(name: str, fn, stages: list):
    t0 = time.time()
    try:
        detail = fn()
        stages.append({"name": name, "status": "PASS", "sec": round(time.time() - t0, 1),
                       "detail": detail or {}})
        print(f"  [PASS] {name} ({stages[-1]['sec']}s)")
        return True
    except Exception as e:
        stages.append({"name": name, "status": "FAIL", "sec": round(time.time() - t0, 1),
                       "detail": {"error": str(e)}})
        print(f"  [FAIL] {name}: {e}")
        return False


def run_acceptance(args) -> int:
    stages: list[dict] = []
    envs = list(args.envs or DEFAULT_ENVS)
    quick = args.quick
    t_all = time.time()

    print("=" * 64)
    print(" AvatarHub 端到端验收（B-15）")
    print(f" 配置：{'quick（仅探测包）' if quick else 'minimal（' + '+'.join(envs) + '）'}")
    print("=" * 64)

    if not _stage("preflight", lambda: _preflight(quick, envs), stages):
        return _finish(stages, t_all, False)

    work = Path(tempfile.mkdtemp(prefix="accept_"))
    sandbox = work / "sandbox"
    sandbox.mkdir()
    clean_path = _clean_path()
    site = None
    tele_srv = None

    try:
        site = _build_local_site(work, json.loads(MANIFEST.read_text(encoding="utf-8")))
        stages.append({"name": "site_build", "status": "PASS", "sec": 0,
                       "detail": {"v1": str(site["v1_manifest"]), "v2": str(site["v2_manifest"])}})
        print(f"  [PASS] site_build → v1/v2 本地站")

        if args.with_telemetry_server:
            import license_server as ls
            tele_path = work / "telemetry.jsonl"
            ls._STATE["telemetry_path"] = tele_path
            ls._STATE["sk"] = None
            tele_srv = ThreadingHTTPServer(("127.0.0.1", 0), ls.Handler)
            threading.Thread(target=tele_srv.serve_forever, daemon=True).start()
            tele_url = f"http://127.0.0.1:{tele_srv.server_address[1]}/api/telemetry"
            site["m2"]["telemetry_url"] = tele_url
            (Path(site["v2_manifest"]).parent / "manifest.json").write_text(
                json.dumps(site["m2"], ensure_ascii=False, indent=2), encoding="utf-8")

        with _sandbox(sandbox) as (pi, tele):
            os.environ["AVATARHUB_TELEMETRY"] = "1"
            if tele:
                tele.set_enabled(True)

            v1_path = str(site["v1_manifest"])
            m1, src1 = pi.load_manifest(v1_path)

            def do_install():
                if quick:
                    comps = [("model:accept_probe", m1["components"]["model"]["accept_probe"])]
                else:
                    comps = _components_for_envs(m1, envs)
                n = len(comps)
                sz = sum(c.get("size_bytes", 0) for _, c in comps)
                failed = pi.install_components(m1, comps, src1, log=lambda *_: None)
                if failed:
                    raise RuntimeError(f"安装失败：{failed}")
                if not quick and any(cid.startswith("env:") for cid, _ in comps):
                    pi.dedup_runtime_envs(log=lambda *_: None)
                return {"components": n, "bytes": sz}

            if not _stage("install_v1", do_install, stages):
                return _finish(stages, t_all, False)

            if not quick:
                def do_torch():
                    res = _torch_smoke(pi, envs, clean_path)
                    bad = [e for e, s in res.items() if s != "ok"]
                    if bad:
                        raise RuntimeError(f"torch 失败：{res}")
                    return res

                if not _stage("torch_smoke", do_torch, stages):
                    return _finish(stages, t_all, False)

            if _read_marker(sandbox) != "accept-v1":
                stages.append({"name": "probe_v1", "status": "FAIL", "sec": 0,
                               "detail": {"marker": _read_marker(sandbox)}})
                return _finish(stages, t_all, False)
            print("  [PASS] probe_v1 marker=accept-v1")

            # 更新到 v2
            def do_update():
                m2, src2 = pi.load_manifest(str(site["v2_manifest"]))
                ups = pi.check_updates(m2)
                probe_up = [u for u in ups if u[0] == "model:accept_probe"]
                if not probe_up and quick:
                    raise RuntimeError("quick 模式应检测到 probe 待更新")
                if not quick:
                    # 真实环境下 v1→v2 仅 probe 应变（env/shared 与 v1 相同 sha）
                    non_probe = [u for u in ups if u[0] != "model:accept_probe"]
                    if non_probe:
                        raise RuntimeError(f"意外待更新组件：{[x[0] for x in non_probe[:5]]}")
                failed = pi.update_all(m2, src2, log=lambda *_: None)
                if failed:
                    raise RuntimeError(f"更新失败：{failed}")
                if _read_marker(sandbox) != "accept-v2":
                    raise RuntimeError(f"probe 未更新：{_read_marker(sandbox)}")
                return {"updates": len(ups), "marker": "accept-v2"}

            if not _stage("update_v2", do_update, stages):
                return _finish(stages, t_all, False)

            # 回滚到 v1
            def do_rollback():
                m2 = site["m2"]
                pts = pi.list_rollback_points(m2)
                if not any(p.get("version") == "1.0.0" for p in pts):
                    raise RuntimeError(f"无 v1 回滚点：{pts}")
                failed = pi.rollback_to(str(site["v1_manifest"]), log=lambda *_: None)
                if failed:
                    raise RuntimeError(f"回滚失败：{failed}")
                if _read_marker(sandbox) != "accept-v1":
                    raise RuntimeError(f"回滚后 marker 错误：{_read_marker(sandbox)}")
                return {"rollback_to": "1.0.0", "marker": "accept-v1"}

            if not _stage("rollback_v1", do_rollback, stages):
                return _finish(stages, t_all, False)

            def do_telemetry():
                tele_dir = sandbox / "runtime" / "telemetry"
                files = list(tele_dir.glob("*_*.json")) if tele_dir.exists() else []
                if not files:
                    raise RuntimeError("无本地遥测回执")
                detail = {"local_receipts": len(files)}
                if args.with_telemetry_server and tele_srv:
                    time.sleep(0.5)
                    tp = work / "telemetry.jsonl"
                    detail["server_lines"] = sum(1 for ln in tp.read_text(encoding="utf-8").splitlines() if ln.strip())
                # 聚合可读性抽检
                try:
                    import make_release as mr
                    recs = []
                    for f in files:
                        recs.append(json.loads(f.read_text(encoding="utf-8-sig")))
                    agg = mr._aggregate_receipts(recs)
                    detail["aggregate_ok_pct"] = agg.get("comp_ok_pct")
                except Exception:
                    pass
                return detail

            _stage("telemetry", do_telemetry, stages)

        # 可选:STT 实时闭环 SLA 闸门(需 Hub/nemo_stt/GPU 就绪;opt-in)。复用 make_release.run_stt_gate
        # 单一事实源——阈值同样取 config.json[stt_sla]。在 sandbox 之外,对运行中的真实服务做活体压测。
        if getattr(args, "stt_bench", ""):
            def do_stt_gate():
                import make_release as mr
                rc = mr.run_stt_gate(args.stt_bench)
                if rc != 0:
                    raise RuntimeError(f"STT 实时闭环未达标(exit={rc})")
                return {"ladder": args.stt_bench}
            _stage("stt_sla", do_stt_gate, stages)

        return _finish(stages, t_all, all(s["status"] == "PASS" for s in stages))

    finally:
        if tele_srv:
            tele_srv.shutdown()
        shutil.rmtree(work, ignore_errors=True)


def _preflight(quick: bool, envs: list[str]) -> dict:
    if not MANIFEST.exists():
        raise FileNotFoundError(f"缺少 {MANIFEST}，请先构建 dist")
    if not (DIST / "packs").exists():
        raise FileNotFoundError("缺少 dist/packs")
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not quick:
        for env in envs:
            if env not in m.get("components", {}).get("env", {}):
                raise ValueError(f"manifest 无环境 {env}")
    free = shutil.disk_usage(DIST).free
    if free < (5 if quick else 8) * (1 << 30):
        raise RuntimeError(f"磁盘余量偏低：{_human(free)}（建议 ≥8GB）")
    return {"manifest_version": m.get("version"), "envs": envs, "disk_free": _human(free),
            "clean_path_stripped": "conda" not in _clean_path().lower()}


def _finish(stages: list, t_all: float, ok: bool) -> int:
    report = {
        "schema": 1,
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "ok": ok,
        "total_sec": round(time.time() - t_all, 1),
        "stages": stages,
        "passed": sum(1 for s in stages if s["status"] == "PASS"),
        "failed": sum(1 for s in stages if s["status"] == "FAIL"),
    }
    DIST.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("-" * 64)
    print(f" 验收结果：{'全部通过 ✓' if ok else '存在失败 ✗'}")
    print(f" 阶段 {report['passed']}/{len(stages)} · 耗时 {report['total_sec']}s")
    print(f" 报告 → {REPORT}")
    return 0 if ok else 4


def main():
    ap = argparse.ArgumentParser(description="AvatarHub 端到端真机验收（B-15）")
    ap.add_argument("--quick", action="store_true", help="仅探测包链路（秒级，跳过真实环境安装）")
    ap.add_argument("--envs", nargs="*", help=f"真实安装环境（默认 {'+'.join(DEFAULT_ENVS)}）")
    ap.add_argument("--with-telemetry-server", action="store_true",
                    help="起本地 license_server 验证回执接收")
    ap.add_argument("--stt-bench", metavar="LADDER", default="",
                    help="末段追加 STT 实时闭环 SLA 闸门（如 8 或 1,4,8,16；需 Hub/nemo_stt/GPU 就绪）。"
                         "阈值读 config.json[stt_sla]，未达标计入验收失败")
    return run_acceptance(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
