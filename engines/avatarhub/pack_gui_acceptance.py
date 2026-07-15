# -*- coding: utf-8 -*-
"""pack_gui_acceptance.py — B-16 安装包/便携版/GUI 验收

在隔离沙箱模拟「已安装便携版/Setup 后的用户机」，无头 Qt 驱动首启向导与维护页，
验证：便携/Inno 产物清单、首启 UI（速度/ETA/源/重试）、维护页（通道/回滚/遥测）。

与 B-15 分工：B-15 验 pack_installer 引擎；B-16 验 launcher_qt GUI 接线与用户可见路径。

用法：
  python pack_gui_acceptance.py
  python pack_gui_acceptance.py --with-exe-selftest   # 额外跑 AvatarHub.exe 自检（需已构建 exe）
  python make_release.py --gui-acceptance
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
MANIFEST = DIST / "manifest.json"
EXE = DIST / "AvatarHub.exe"
REPORT = DIST / "gui_acceptance_report.json"
ISS = HERE / "installer" / "AvatarHub.iss"

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

VENV_PY = HERE / ".venv_launcher" / "Scripts" / "python.exe"


def _ensure_gui_python():
    """GUI 验收需 PySide6；若当前解释器无则自动切到 .venv_launcher（与 build_launcher 一致）。"""
    try:
        import PySide6  # noqa: F401
        return
    except ImportError:
        pass
    if VENV_PY.exists() and Path(sys.executable).resolve() != VENV_PY.resolve():
        os.execv(str(VENV_PY), [str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise SystemExit("[ERROR] 需要 PySide6：请先创建 .venv_launcher（见 build_launcher.bat）")


def _stage(name: str, fn, stages: list) -> bool:
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
    print(f" GUI 验收：{'全部通过 ✓' if ok else '存在失败 ✗'}")
    print(f" 阶段 {report['passed']}/{len(stages)} · 耗时 {report['total_sec']}s")
    print(f" 报告 → {REPORT}")
    return 0 if ok else 5


def check_portable_layout() -> dict:
    """便携版/Inno 共用的「薄核心」清单（不跑 ISCC，只验源文件齐备）。"""
    required = {
        "AvatarHub.exe": EXE,
        "manifest.json": MANIFEST,
        "pack_installer.py": HERE / "pack_installer.py",
        "launcher_qt.py": HERE / "launcher_qt.py",
        "static/": HERE / "static",
        "assets/app.ico": HERE / "assets" / "app.ico",
        "config.example.json": HERE / "config.example.json",
    }
    missing = [k for k, p in required.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"薄核心缺失：{', '.join(missing)}（先 build_launcher.bat + build_packs）")
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if not m.get("editions"):
        raise ValueError("manifest 无 editions")
    return {"exe_mb": round(EXE.stat().st_size / (1 << 20), 2),
            "manifest_version": m.get("version"), "editions": list(m["editions"])}


def check_installer_sources() -> dict:
    """Inno 脚本引用的源是否存在（编译前门禁，零 ISCC 依赖）。"""
    if not ISS.exists():
        raise FileNotFoundError(f"缺少 {ISS}")
    iss_text = ISS.read_text(encoding="utf-8")
    checks = {
        "AvatarHub.exe": EXE.exists(),
        "manifest.json": MANIFEST.exists(),
        "static": (HERE / "static").is_dir(),
        "requirements": (HERE / "requirements").is_dir(),
        "app.ico": (HERE / "assets" / "app.ico").exists(),
        "install_notes.txt": (HERE / "installer" / "install_notes.txt").exists(),
    }
    bad = [k for k, ok in checks.items() if not ok]
    if bad:
        raise FileNotFoundError(f"Inno 源缺失：{', '.join(bad)}")
    return {"iss": str(ISS), "sources_ok": list(checks), "privileges": "lowest" in iss_text}


def _bootstrap_installed_tree(base: Path, manifest_path: Path):
    """模拟 Setup/便携版解压结果：exe + manifest + 关键 .py（开发态用源码代替冻结包内的模块）。"""
    base.mkdir(parents=True, exist_ok=True)
    shutil.copy2(EXE, base / "AvatarHub.exe")
    shutil.copy2(manifest_path, base / "manifest.json")
    for name in ("pack_installer.py", "launcher_qt.py", "app_config.py", "telemetry.py",
                 "service_manager.py", "license.py"):
        src = HERE / name
        if src.exists():
            shutil.copy2(src, base / name)


def _wait_until(app, cond, timeout=30.0, step_ms=50):
    from PySide6.QtCore import QTimer, QEventLoop
    loop = QEventLoop()
    t0 = time.time()

    def tick():
        if cond() or time.time() - t0 > timeout:
            loop.quit()

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(step_ms)
    loop.exec()
    if not cond():
        raise TimeoutError(f"等待超时 ({timeout}s)")


def test_first_run_wizard(app, pi, m1: dict, src1: str) -> dict:
    """无头驱动 FirstRunWizard：验机 → 安装探测包 → 速度/源/进度 UI。"""
    import launcher_qt as lq
    from PySide6.QtWidgets import QMessageBox

    # 避免模态框阻塞无头运行
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)

    pi.detect_gpus = lambda: [{"index": 0, "name": "Accept GPU", "total_mb": 24576, "free_mb": 20000}]
    pi.recommend_edition = lambda m, g: ("lite", {"lite": True, "standard": True, "flagship": False})

    orig_edition = pi.install_edition

    def _fast_edition(manifest, edition, src_root, **kw):
        probe = manifest["components"]["model"]["accept_probe"]
        return pi.install_components(manifest, [("model:accept_probe", probe)], src_root,
                                     log=kw.get("log"), on_overall=kw.get("on_overall"))

    pi.install_edition = _fast_edition

    wiz = lq.FirstRunWizard(m1, src1)
    _wait_until(app, lambda: wiz.combo.count() > 0)
    if not wiz.gpu_label.text().startswith("显卡："):
        raise RuntimeError(f"GPU 标签异常：{wiz.gpu_label.text()}")

    wiz._on_install()
    _wait_until(app, lambda: not wiz.installing, timeout=60)
    if wiz.progress.value() < 100:
        raise RuntimeError(f"进度未满：{wiz.progress.value()}%")
    spd = wiz.speed_label.text()
    if "速度" not in spd:
        raise RuntimeError(f"速度标签缺失：{spd}")
    src = wiz.src_label.text()
    if "生效下载源" not in src:
        raise RuntimeError(f"源标签缺失：{src}")

    pi.install_edition = orig_edition
    marker = (pi.BASE / "runtime" / "accept_probe" / "marker.txt").read_text(encoding="utf-8")
    if marker != "accept-v1":
        raise RuntimeError(f"向导安装 marker 错误：{marker}")
    return {"gpu_label": wiz.gpu_label.text()[:40], "progress": wiz.progress.value(),
            "speed_sample": spd[:60], "src_sample": src[:80], "marker": marker}


def test_maintenance_dialog(app, pi, m2: dict, site: dict) -> dict:
    """无头驱动 MaintenanceDialog：通道持久化、遥测开关、GUI 回滚。"""
    import launcher_qt as lq
    from PySide6.QtWidgets import QMessageBox

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.information = staticmethod(lambda *a, **k: None)

    # 预置 v2 状态（探测包 accept-v2）
    probe = m2["components"]["model"]["accept_probe"]
    v2_root = site["v2_root"]
    failed = pi.install_components(m2, [("model:accept_probe", probe)], v2_root,
                                   log=lambda *_: None)
    if failed:
        raise RuntimeError(f"预置 v2 失败：{failed}")

    dlg = lq.MaintenanceDialog(m2, v2_root)
    _wait_until(app, lambda: dlg.rb_combo.count() > 0 and dlg.btn_rb.isEnabled(), timeout=30)
    if not any("1.0.0" in dlg.rb_combo.itemText(i) for i in range(dlg.rb_combo.count())):
        raise RuntimeError("回滚列表无 v1.0.0")

    # 通道切换 → launcher_settings.json
    idx = dlg.chan_combo.findText("beta")
    if idx >= 0:
        dlg.chan_combo.setCurrentIndex(idx)
    else:
        dlg.chan_combo.addItem("beta")
        dlg.chan_combo.setCurrentText("beta")
    dlg._on_apply_channel()
    settings = json.loads((pi.BASE / "launcher_settings.json").read_text(encoding="utf-8"))
    if settings.get("channel") != "beta":
        raise RuntimeError(f"通道未持久化：{settings}")

    # 遥测 opt-in
    if lq.telemetry is not None:
        dlg.chk_tele.setChecked(True)
        if not lq.telemetry.enabled():
            raise RuntimeError("遥测开关未生效")

    dlg._on_rollback()
    _wait_until(app, lambda: not dlg.busy, timeout=60)
    marker = (pi.BASE / "runtime" / "accept_probe" / "marker.txt").read_text(encoding="utf-8")
    if marker != "accept-v1":
        raise RuntimeError(f"GUI 回滚后 marker 错误：{marker}")
    return {"channel": settings.get("channel"), "telemetry": lq.telemetry.enabled() if lq.telemetry else None,
            "rollback_marker": marker, "rollback_points": dlg.rb_combo.count()}


def test_exe_selftest(temp_base: Path) -> dict:
    """冻结 exe 自检模式：验证 pack_installer/license/cryptography 打进包。"""
    env = os.environ.copy()
    env["AVATARHUB_SELFTEST"] = "1"
    env["AVATARHUB_BASE"] = str(temp_base)
    env["QT_QPA_PLATFORM"] = "offscreen"
    exe = temp_base / "AvatarHub.exe"
    if not exe.exists():
        raise FileNotFoundError("sandbox 无 AvatarHub.exe")
    proc = subprocess.run([str(exe)], env=env, cwd=str(temp_base), timeout=90,
                          capture_output=True, text=True)
    out = temp_base / "selftest_result.txt"
    if not out.exists():
        raise RuntimeError(f"exe 未写 selftest_result.txt（exit={proc.returncode}）\n{proc.stderr[-500:]}")
    text = out.read_text(encoding="utf-8")
    if "ok" not in text or "pack_installer=ok" not in text:
        raise RuntimeError(f"exe 自检未通过：\n{text}")
    return {"selftest": text.strip().split("\n")[0], "exit": proc.returncode}


def run_gui_acceptance(args) -> int:
    _ensure_gui_python()
    import pack_acceptance as pa

    stages: list[dict] = []
    t_all = time.time()
    print("=" * 64)
    print(" AvatarHub GUI/安装包验收（B-16）")
    print("=" * 64)

    if not _stage("layout_portable", check_portable_layout, stages):
        return _finish(stages, t_all, False)
    if not _stage("layout_installer", check_installer_sources, stages):
        return _finish(stages, t_all, False)

    work = Path(tempfile.mkdtemp(prefix="gui_accept_"))
    sandbox = work / "installed"
    try:
        dist_m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        site = pa._build_local_site(work, dist_m)
        _bootstrap_installed_tree(sandbox, site["v1_manifest"])

        if args.with_exe_selftest:
            if not _stage("exe_selftest", lambda: test_exe_selftest(sandbox), stages):
                return _finish(stages, t_all, False)

        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv)

        with pa._sandbox(sandbox) as (pi, _tele):
            m1, src1 = pi.load_manifest(str(site["v1_manifest"]))
            m2 = site["m2"]

            if not _stage("gui_wizard", lambda: test_first_run_wizard(app, pi, m1, src1), stages):
                return _finish(stages, t_all, False)
            if not _stage("gui_maintenance", lambda: test_maintenance_dialog(
                    app, pi, m2, site), stages):
                return _finish(stages, t_all, False)

        return _finish(stages, t_all, True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="AvatarHub GUI/安装包验收（B-16）")
    ap.add_argument("--with-exe-selftest", action="store_true",
                    help="额外运行 AvatarHub.exe AVATARHUB_SELFTEST=1（需 dist/AvatarHub.exe）")
    return run_gui_acceptance(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
