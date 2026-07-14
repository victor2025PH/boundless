# 关闭 MIUI / 红米手机管家在 ADB 安装、壁纸变更、键盘安装时弹出的所有确认/风险弹窗。
# 同时锁定 USB 调试与开发者选项，避免被 securitycenter 劝退。
#
# 兼容：MIUI 14/15 (Redmi 23106RN0DA 实测)，无 root 也能跑（部分操作受 SELinux 限制时会跳过）。
# 用法：
#   单台:     python scripts/disable_miui_security_popups.py <serial>
#   全集群:   python scripts/disable_miui_security_popups.py --all
#   预检:     python scripts/disable_miui_security_popups.py <serial> --dry-run

import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_ROOT = Path(__file__).resolve().parent.parent
_ALIASES = _ROOT / "config" / "device_aliases.json"

# settings put 形式：(table, key, value, 治什么)
_SETTINGS = [
    ("global", "adb_install_need_confirm",   "0", "ADB 安装确认弹窗（MIUI 官方开关）"),
    ("secure", "adb_install_need_confirm",   "0", "同上 secure 表（双保险）"),
    ("global", "package_verifier_enable",    "0", "包验证弹窗"),
    ("global", "verifier_verify_adb_installs", "0", "ADB 旁路验证"),
    ("global", "adb_enabled",                "1", "锁定 USB 调试（防 securitycenter 劝退）"),
    ("global", "development_settings_enabled", "1", "锁定开发者选项"),
    ("secure", "install_non_market_apps",    "1", "允许未知来源（MIUI 仍要求开）"),
    ("secure", "lock_screen_magazine_status", "0", "关锁屏画报轮播（否则会立即覆盖我们设的锁屏壁纸）"),
    ("secure", "miui_wallpaper_content_type", "0", "关 MIUI 壁纸内容轮播（否则会覆盖桌面壁纸）"),
]

# 包级 disable-user — MIUI 核心包会拒绝，仅记录失败，不报错。
_DISABLE_PACKAGES = [
    ("com.miui.cleaner",                 "深度清理（壁纸/文件变更触发）"),
    ("com.lbe.security.miui",            "LBE 风险扫描（核心包，通常会被拒；触发链已被 settings 切断）"),
    ("com.miui.android.fashiongallery",  "壁纸画廊（会自动覆盖我们设的桌面壁纸）"),
]

# 拔悬浮窗权限作为兜底
_APPOPS_DENY = [
    "com.miui.securitycenter",
    "com.miui.cleaner",
    "com.lbe.security.miui",
    "com.miui.guardprovider",
    "com.miui.securityadd",
]


@dataclass
class StepResult:
    label: str
    ok: bool
    detail: str = ""


def _adb(serial: str, cmd: str, dry: bool) -> str:
    full = ["adb", "-s", serial, "shell", cmd]
    if dry:
        return f"[dry-run] {' '.join(shlex.quote(p) for p in full)}"
    out = subprocess.run(full, capture_output=True, text=True, timeout=15)
    return (out.stdout + out.stderr).strip()


def _set_setting(serial: str, table: str, key: str, val: str, dry: bool) -> StepResult:
    _adb(serial, f"settings put {table} {key} {val}", dry)
    if dry:
        return StepResult(f"settings.{table}.{key}", True, f"(dry-run) -> {val}")
    got = _adb(serial, f"settings get {table} {key}", False)
    return StepResult(f"settings.{table}.{key}", got.strip() == val, f"got={got}")


def _disable_pkg(serial: str, pkg: str, dry: bool) -> StepResult:
    out = _adb(serial, f"pm disable-user --user 0 {pkg}", dry)
    if dry:
        return StepResult(f"disable {pkg}", True, "(dry-run)")
    if "new state: disabled" in out:
        return StepResult(f"disable {pkg}", True, "disabled-user")
    if "Cannot disable miui core" in out:
        return StepResult(f"disable {pkg}", False, "miui core (skipped)")
    if "not found" in out.lower() or "unknown package" in out.lower():
        return StepResult(f"disable {pkg}", True, "not installed (ok)")
    return StepResult(f"disable {pkg}", False, out.splitlines()[0] if out else "no output")


def _appops_deny(serial: str, pkg: str, dry: bool) -> StepResult:
    _adb(serial, f"appops set {pkg} SYSTEM_ALERT_WINDOW deny", dry)
    if dry:
        return StepResult(f"appops {pkg}", True, "(dry-run)")
    got = _adb(serial, f"appops get {pkg} SYSTEM_ALERT_WINDOW", False)
    return StepResult(f"appops {pkg}", "deny" in got.lower() or "no operations" in got.lower(), got.split("\n")[0])


def is_miui_device(serial: str) -> bool:
    """检测设备是否为 MIUI/Redmi/POCO（非此类设备无需硬化）。"""
    out = _adb(serial, "getprop ro.miui.ui.version.name", False)
    if out and out.strip():
        return True
    brand = _adb(serial, "getprop ro.product.brand", False).strip().lower()
    return brand in ("xiaomi", "redmi", "poco")


def _force_stop_daemons(serial: str, dry: bool) -> StepResult:
    """让 settings 改动立即生效：杀死所有相关守护进程，让其重启时读新 settings。"""
    pkgs = [
        "com.miui.securitycenter", "com.miui.cleaner", "com.lbe.security.miui",
        "com.miui.guardprovider", "com.miui.securityadd",
    ]
    for p in pkgs:
        _adb(serial, f"am force-stop {p}", dry)
    return StepResult("force-stop daemons", True, f"{len(pkgs)} packages")


def harden(serial: str, dry: bool = False) -> list[StepResult]:
    results: list[StepResult] = []
    if not dry and not is_miui_device(serial):
        return [StepResult("device check", True, "skipped — not MIUI/Redmi/POCO")]
    for table, key, val, _why in _SETTINGS:
        results.append(_set_setting(serial, table, key, val, dry))
    for pkg, _why in _DISABLE_PACKAGES:
        results.append(_disable_pkg(serial, pkg, dry))
    for pkg in _APPOPS_DENY:
        results.append(_appops_deny(serial, pkg, dry))
    results.append(_force_stop_daemons(serial, dry))
    return results


def harden_quietly(serial: str) -> bool:
    """供 host 集成调用：只返回 bool，不抛异常，不打印。

    在 src/host/routers/devices_core.py 设备首次注册流程中调用，让 MIUI 机
    一接入就自动硬化，避免点"部署壁纸"才被手机管家拦下来。
    """
    try:
        if not is_miui_device(serial):
            return True
        results = harden(serial, dry=False)
        # 只要 settings 主开关全成功就算 OK（包级 disable 失败可接受）
        critical = [r for r in results if r.label.startswith("settings.")]
        return all(r.ok for r in critical)
    except Exception:
        return False


def _list_serials() -> Iterable[str]:
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            yield parts[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("serial", nargs="?", help="设备序列号")
    ap.add_argument("--all", action="store_true", help="对所有 adb devices 中 state=device 的机器执行")
    ap.add_argument("--dry-run", action="store_true", help="只打印要执行的命令，不真改")
    args = ap.parse_args()

    if not args.all and not args.serial:
        ap.print_help()
        return 1

    serials = list(_list_serials()) if args.all else [args.serial]
    if not serials:
        print("没有在线设备")
        return 1

    aliases = json.loads(_ALIASES.read_text(encoding="utf-8")) if _ALIASES.is_file() else {}
    rc = 0
    for s in serials:
        alias = aliases.get(s, {}).get("alias", "?")
        print(f"\n=== {s} ({alias}) ===")
        for r in harden(s, args.dry_run):
            mark = "OK " if r.ok else "FAIL"
            print(f"  [{mark}] {r.label:<48} {r.detail}")
            if not r.ok and "miui core" not in r.detail:
                rc = 2
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
