"""
Phase 7A — Device Probe Script
Discovers all connected devices and reports:
  - Hardware info (model, Android version, CPU, RAM, storage)
  - Root/Magisk status
  - Installed target apps
  - System settings (developer mode, animations, screen timeout)
  - uiautomator2 availability
"""

import subprocess
import json
import sys
import time
from pathlib import Path

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

TARGET_APPS = {
    "org.telegram.messenger": "Telegram",
    "com.whatsapp": "WhatsApp",
    "com.linkedin.android": "LinkedIn",
    "com.facebook.katana": "Facebook",
    "com.instagram.android": "Instagram",
    "com.twitter.android": "X/Twitter",
    "com.ss.android.ugc.trill": "TikTok (Trill)",
    "com.zhiliaoapp.musically": "TikTok",
    "com.zhiliaoapp.musically.go": "TikTok Lite",
}

MAGISK_PACKAGES = [
    "com.topjohnwu.magisk",
    "io.github.huskydg.magisk",
]


def adb(device_id: str, cmd: str, timeout: int = 10) -> str:
    try:
        result = subprocess.run(
            ["adb", "-s", device_id, "shell", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip()
    except Exception as e:
        return f"ERROR: {e}"


def adb_getprop(device_id: str, prop: str) -> str:
    return adb(device_id, f"getprop {prop}")


def get_online_devices() -> list:
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
    devices = []
    for line in result.stdout.strip().split("\n")[1:]:
        parts = line.split("\t")
        if len(parts) == 2:
            did, status = parts
            devices.append({"id": did.strip(), "status": status.strip()})
    return devices


def probe_device(device_id: str) -> dict:
    info = {"device_id": device_id}

    # Hardware
    info["brand"] = adb_getprop(device_id, "ro.product.brand")
    info["model"] = adb_getprop(device_id, "ro.product.model")
    info["manufacturer"] = adb_getprop(device_id, "ro.product.manufacturer")
    info["android_version"] = adb_getprop(device_id, "ro.build.version.release")
    info["sdk"] = adb_getprop(device_id, "ro.build.version.sdk")
    info["fingerprint"] = adb_getprop(device_id, "ro.build.fingerprint")
    info["security_patch"] = adb_getprop(device_id, "ro.build.version.security_patch")

    # Screen
    wm = adb(device_id, "wm size")
    info["screen"] = wm.split(":")[-1].strip() if ":" in wm else wm

    # CPU
    info["cpu"] = adb(device_id, "getprop ro.product.board")
    info["cpu_abi"] = adb_getprop(device_id, "ro.product.cpu.abi")

    # RAM
    meminfo = adb(device_id, "cat /proc/meminfo | head -1")
    if "MemTotal" in meminfo:
        kb = int("".join(filter(str.isdigit, meminfo)))
        info["ram_gb"] = round(kb / 1024 / 1024, 1)
    else:
        info["ram_gb"] = "unknown"

    # Storage
    df = adb(device_id, "df /data | tail -1")
    parts = df.split()
    if len(parts) >= 4:
        try:
            total_kb = int(parts[1])
            info["storage_gb"] = round(total_kb / 1024 / 1024, 1)
        except ValueError:
            info["storage_gb"] = "unknown"
    else:
        info["storage_gb"] = "unknown"

    # Root / Magisk
    su_check = adb(device_id, "which su 2>/dev/null")
    info["has_su"] = "su" in su_check and "not found" not in su_check

    magisk_version = adb(device_id, "su -c 'magisk -v' 2>/dev/null")
    info["magisk_version"] = magisk_version if magisk_version and "ERROR" not in magisk_version else "not found"

    magisk_code = adb(device_id, "su -c 'magisk -V' 2>/dev/null")
    info["magisk_version_code"] = magisk_code if magisk_code and "ERROR" not in magisk_code else ""

    zygisk = adb(device_id, "su -c 'magisk --zygisk-status' 2>/dev/null")
    info["zygisk"] = "enabled" if "enabled" in zygisk.lower() else zygisk

    # Magisk app package
    packages = adb(device_id, "pm list packages")
    magisk_pkg = "none"
    for pkg in MAGISK_PACKAGES:
        if f"package:{pkg}" in packages:
            magisk_pkg = pkg
            break
    # Check hidden Magisk (random package name)
    if magisk_pkg == "none":
        for line in packages.split("\n"):
            pkg = line.replace("package:", "").strip()
            if pkg and len(pkg) > 20 and "." in pkg:
                label = adb(device_id, f"dumpsys package {pkg} | grep -i magisk")
                if "magisk" in label.lower():
                    magisk_pkg = pkg + " (hidden)"
                    break
    info["magisk_package"] = magisk_pkg

    # Installed target apps
    installed_apps = {}
    for pkg, name in TARGET_APPS.items():
        installed_apps[name] = f"package:{pkg}" in packages
    info["target_apps"] = installed_apps

    # System settings
    info["developer_mode"] = adb(device_id, "settings get global development_settings_enabled")
    info["adb_enabled"] = adb(device_id, "settings get global adb_enabled")
    info["animator_scale"] = adb(device_id, "settings get global animator_duration_scale")
    info["screen_off_timeout"] = adb(device_id, "settings get system screen_off_timeout")
    info["stay_awake"] = adb(device_id, "settings get global stay_on_while_plugged_in")

    # uiautomator2
    u2_pkg = "com.github.uiautomator2.test" in packages or "com.github.uiautomator" in packages
    info["u2_installed"] = u2_pkg

    # Timezone and locale
    info["timezone"] = adb_getprop(device_id, "persist.sys.timezone")
    info["locale"] = adb_getprop(device_id, "persist.sys.locale")

    return info


def print_device_report(info: dict, idx: int):
    print(f"\n{'='*65}")
    print(f"  D{idx} — {info['device_id']}")
    print(f"{'='*65}")
    print(f"  品牌/型号  : {info['brand']} {info['model']} ({info['manufacturer']})")
    print(f"  Android    : {info['android_version']} (SDK {info['sdk']})")
    print(f"  安全补丁   : {info['security_patch']}")
    print(f"  屏幕       : {info['screen']}")
    print(f"  CPU        : {info['cpu']} ({info['cpu_abi']})")
    print(f"  RAM        : {info['ram_gb']} GB")
    print(f"  存储       : {info['storage_gb']} GB")
    print(f"  指纹       : {info['fingerprint'][:60]}...")
    print(f"  时区       : {info['timezone']}")
    print(f"  语言       : {info['locale']}")

    print(f"\n  Root/Magisk:")
    print(f"    su 可用     : {'✓' if info['has_su'] else '✗'}")
    print(f"    Magisk 版本 : {info['magisk_version']}")
    print(f"    版本号      : {info['magisk_version_code']}")
    print(f"    Zygisk      : {info['zygisk']}")
    print(f"    Magisk 包名 : {info['magisk_package']}")

    print(f"\n  系统设置:")
    print(f"    开发者模式  : {info['developer_mode']}")
    print(f"    ADB 开启    : {info['adb_enabled']}")
    print(f"    动画缩放    : {info['animator_scale']}")
    print(f"    屏幕超时    : {info['screen_off_timeout']}ms")
    print(f"    充电常亮    : {info['stay_awake']}")
    print(f"    u2 已安装   : {'✓' if info['u2_installed'] else '✗'}")

    print(f"\n  已安装 APP:")
    for app, installed in info["target_apps"].items():
        print(f"    {'✓' if installed else '✗'} {app}")


if __name__ == "__main__":
    print("=" * 65)
    print("  Phase 7A — 全设备探测报告")
    print("=" * 65)

    devices = get_online_devices()
    print(f"\n检测到 {len(devices)} 台设备:")
    for d in devices:
        print(f"  {d['id'][:16]}... — {d['status']}")

    online = [d for d in devices if d["status"] == "device"]
    print(f"\n在线设备: {len(online)} 台，开始详细探测...\n")

    all_info = []
    for idx, d in enumerate(online, 1):
        print(f"探测 D{idx} ({d['id'][:12]}...)...", end="", flush=True)
        try:
            info = probe_device(d["id"])
            all_info.append(info)
            print(" 完成")
        except Exception as e:
            print(f" 失败: {e}")

    for idx, info in enumerate(all_info, 1):
        print_device_report(info, idx)

    # Summary
    print(f"\n{'='*65}")
    print(f"  总结")
    print(f"{'='*65}")

    all_apps = set()
    for info in all_info:
        for app, installed in info["target_apps"].items():
            if not installed:
                all_apps.add(app)

    rooted = sum(1 for i in all_info if i["has_su"])
    magisk_ok = sum(1 for i in all_info if "not found" not in i["magisk_version"])
    u2_ok = sum(1 for i in all_info if i["u2_installed"])

    print(f"  在线设备   : {len(all_info)}")
    print(f"  已 Root    : {rooted}/{len(all_info)}")
    print(f"  Magisk 可用: {magisk_ok}/{len(all_info)}")
    print(f"  u2 已安装  : {u2_ok}/{len(all_info)}")

    if all_apps:
        print(f"\n  缺失 APP (需要安装):")
        for app in sorted(all_apps):
            missing_count = sum(1 for i in all_info if not i["target_apps"].get(app, False))
            print(f"    {app}: {missing_count} 台缺失")
    else:
        print(f"\n  所有 APP 全部已安装 ✓")

    # Save JSON
    report_path = Path(ROOT) / "data" / "device_probe_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(all_info, f, ensure_ascii=False, indent=2)
    print(f"\n  详细报告已保存: {report_path}")
