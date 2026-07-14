# -*- coding: utf-8 -*-
"""
make_portable.py — 装配「便携版」分发（绿色版，免安装/免管理员）。

为什么是便携版而非 Inno 安装包：本产品重型、每用户、就地写 logs/config，且首启向导
负责下载一切。便携 ZIP 免管理员、免安装步骤、免 ISCC 依赖、托管简单、规避安装期杀软告警。
Inno 安装包（installer\\AvatarHub.iss）作为「加桌面图标/卸载项」的可选锦上添花并行保留。

产物：
  dist\\AvatarHub-portable-<ver>\\        便携目录（AvatarHub.exe + 运行脚本 + static + manifest + 文档）
  dist\\AvatarHub-portable-<ver>.zip      压缩分发包

用法：
  python make_portable.py                                   # 用 manifest 版本号，base_url 不改
  python make_portable.py --version 1.0.0 --base-url https://你的下载站/avatarhub/1.0.0
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
EXE = DIST / "AvatarHub.exe"
MANIFEST = DIST / "manifest.json"

# 不进用户分发的 .py：测试 / 构建 / 开发探针 / 厂商签发 CLI。
EXCLUDE_PY = [
    "test_*.py", "run_all_tests.py", "build_packs.py", "gate.py", "make_manual.py",
    "make_portable.py", "make_release.py", "pack_acceptance.py", "pack_gui_acceptance.py",
    "license_admin.py", "license_server.py", "_*.py",
]
# 运行期 .bat 需随附（mem_watchdog 用 _launch_*.bat 拉起/复活服务；start_*.bat 为手动启动入口）。
# 仅排除机密/构建/开发脚本，与 installer\AvatarHub.iss 的排除集保持一致。
EXCLUDE_BAT = [
    "secrets.bat", "build_launcher.bat", "sign_artifacts.bat", "gate.bat",
]
# 随附文档（存在才拷）。
DOCS = ["config.example.json", "README.md", "部署指南.md", "打包与分发方案_v1.md", "交付与验收清单.md"]

SHORTCUT_BAT = """@echo off
REM 在桌面创建 AvatarHub 快捷方式（便携版无安装器，用此脚本一键加图标）。
powershell -NoProfile -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($d+'\\AvatarHub.lnk'); $s.TargetPath='%~dp0AvatarHub.exe'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0AvatarHub.exe,0'; $s.Save()"
echo [done] 已在桌面创建 AvatarHub 快捷方式。
pause
"""


def _excluded(name: str, patterns: list) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def main():
    ap = argparse.ArgumentParser(description="装配便携版分发")
    ap.add_argument("--version", default="", help="版本号（默认取 manifest.version）")
    ap.add_argument("--base-url", default="", help="覆盖 manifest.base_url（指向你的下载站）")
    args = ap.parse_args()

    if not EXE.exists():
        print("[ERROR] 未找到 dist\\AvatarHub.exe，请先运行 build_launcher.bat。")
        return 2

    manifest = {}
    if MANIFEST.exists():
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    else:
        print("[WARN] 未找到 dist\\manifest.json —— 便携包将缺少 manifest，首启向导无法工作。")

    ver = args.version or manifest.get("version") or "1.0.0"
    out = DIST / f"AvatarHub-portable-{ver}"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    # 1) 启动器 exe
    shutil.copy2(EXE, out / "AvatarHub.exe")

    # 2) 运行期 .py（排除测试/构建/开发/厂商脚本）
    n_py = 0
    for p in sorted(HERE.glob("*.py")):
        if _excluded(p.name, EXCLUDE_PY):
            continue
        shutil.copy2(p, out / p.name)
        n_py += 1

    # 2b) 运行期 .bat（mem_watchdog 复活/手动启动入口需要；排除机密/构建脚本）
    n_bat = 0
    for p in sorted(HERE.glob("*.bat")):
        if _excluded(p.name, EXCLUDE_BAT):
            continue
        shutil.copy2(p, out / p.name)
        n_bat += 1

    # 3) 前端
    if (HERE / "static").is_dir():
        shutil.copytree(HERE / "static", out / "static")

    # 4) 文档 / 配置模板
    for f in DOCS:
        if (HERE / f).exists():
            shutil.copy2(HERE / f, out / f)

    # 5) 图标资源
    (out / "assets").mkdir(exist_ok=True)
    if (HERE / "assets" / "app.ico").exists():
        shutil.copy2(HERE / "assets" / "app.ico", out / "assets" / "app.ico")

    # 6) manifest（按需改写 base_url，指向下载站）
    if manifest:
        if args.base_url:
            manifest["base_url"] = args.base_url
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7) 桌面快捷方式助手（便携版补偿无安装器）
    (out / "创建桌面快捷方式.bat").write_text(SHORTCUT_BAT, encoding="gbk", errors="replace")

    # 8) 打 zip
    zip_path = DIST / f"AvatarHub-portable-{ver}.zip"
    zip_path.unlink(missing_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in out.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(out.parent))

    size_mb = zip_path.stat().st_size / 1048576
    print("=" * 60)
    print(" 便携版装配完成")
    print("=" * 60)
    print(f" 版本：      {ver}")
    print(f" 运行脚本：  {n_py} 个 .py + {n_bat} 个 .bat")
    print(f" base_url：  {manifest.get('base_url') or '(空——发布前请用 --base-url 指向下载站)'}")
    print(f" 目录：      {out}")
    print(f" 压缩包：    {zip_path}  ({size_mb:.1f} MB)")
    print(" 用户用法：解压 → 双击 AvatarHub.exe → 首启向导按档下载 → 即用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
