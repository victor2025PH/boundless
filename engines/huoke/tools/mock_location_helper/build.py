#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenClaw MockLocation Helper APK 构建脚本。

环境要求:
  - Android SDK Build-Tools（提供 aapt, d8, apksigner）
  - Java JDK 8+（提供 javac, jar）
  - android.jar（来自 Android SDK platforms/）

快速构建（推荐）:
  python tools/mock_location_helper/build.py

构建成功后，APK 将输出到:
  config/apks/openclaw_mock_location.apk

然后通过以下命令安装到设备:
  python tools/mock_location_helper/build.py --install <device_serial>

或者在 Web UI 调用:
  POST /devices/{device_id}/mock-location/install
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# ── 路径配置 ──
_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "apk_src"
_BUILD = _HERE / "build"
_PROJECT_ROOT = _HERE.parent.parent
_OUT_APK = _PROJECT_ROOT / "config" / "apks" / "openclaw_mock_location.apk"

# APK 包名
_PACKAGE = "com.openclaw.mocklocation"

# Android SDK 可能的安装位置（按优先级）
_SDK_CANDIDATES = [
    os.environ.get("ANDROID_HOME", ""),
    os.environ.get("ANDROID_SDK_ROOT", ""),
    os.path.expanduser("~/Library/Android/sdk"),       # macOS
    os.path.expanduser("~/Android/Sdk"),               # Linux
    "C:/Users/Administrator/AppData/Local/Android/Sdk",  # Windows (当前机器)
    "C:/Android/sdk",
    "/opt/android-sdk",
]

# Build-Tools 版本（自动探测最高版本）
_BUILD_TOOLS_VERSION = None

# Android API level（platform jar）
_API_LEVEL = 33


def find_sdk() -> str:
    """查找 Android SDK 根目录。"""
    for candidate in _SDK_CANDIDATES:
        if candidate and Path(candidate).exists():
            bt = Path(candidate) / "build-tools"
            if bt.exists():
                return candidate
    return ""


def find_build_tools(sdk_root: str) -> str:
    """找到最高版本的 build-tools。"""
    bt_dir = Path(sdk_root) / "build-tools"
    if not bt_dir.exists():
        return ""
    versions = sorted([d.name for d in bt_dir.iterdir() if d.is_dir()], reverse=True)
    if versions:
        return str(bt_dir / versions[0])
    return ""


def find_android_jar(sdk_root: str, api: int = _API_LEVEL) -> str:
    """查找 android.jar（从指定 API level 开始向下找）。"""
    platforms = Path(sdk_root) / "platforms"
    for level in range(api, 20, -1):
        jar = platforms / f"android-{level}" / "android.jar"
        if jar.exists():
            return str(jar)
    return ""


def run(cmd: list, cwd=None, check=True) -> int:
    """执行命令，打印输出，返回退出码。"""
    print(f"  $ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        print(f"  [ERROR] 命令失败: returncode={result.returncode}", file=sys.stderr)
        sys.exit(result.returncode)
    return result.returncode


def build(sdk_root: str = "") -> Path:
    """执行完整构建流程，返回 APK 路径。"""
    # 自动探测 SDK
    if not sdk_root:
        sdk_root = find_sdk()
    if not sdk_root:
        print("[ERROR] 未找到 Android SDK！请设置 ANDROID_HOME 环境变量", file=sys.stderr)
        sys.exit(1)
    print(f"[Build] SDK: {sdk_root}")

    bt = find_build_tools(sdk_root)
    if not bt:
        print("[ERROR] 未找到 build-tools！请通过 SDK Manager 安装", file=sys.stderr)
        sys.exit(1)
    print(f"[Build] Build-Tools: {bt}")

    android_jar = find_android_jar(sdk_root)
    if not android_jar:
        print("[ERROR] 未找到 android.jar！请安装 Android SDK Platform", file=sys.stderr)
        sys.exit(1)
    print(f"[Build] android.jar: {android_jar}")

    # 清理 & 创建 build 目录
    if _BUILD.exists():
        shutil.rmtree(_BUILD)
    _BUILD.mkdir(parents=True, exist_ok=True)

    gen_dir = _BUILD / "gen"
    obj_dir = _BUILD / "obj"
    dex_dir = _BUILD / "dex"
    for d in [gen_dir, obj_dir, dex_dir]:
        d.mkdir(parents=True, exist_ok=True)

    _OUT_APK.parent.mkdir(parents=True, exist_ok=True)

    aapt = str(Path(bt) / ("aapt.exe" if sys.platform == "win32" else "aapt"))
    d8 = str(Path(bt) / ("d8.bat" if sys.platform == "win32" else "d8"))
    apksigner = str(Path(bt) / ("apksigner.bat" if sys.platform == "win32" else "apksigner"))

    # Step 1: aapt — 处理资源（含 Manifest）
    print("\n[Step 1] 处理资源 (aapt)...")
    manifest = str(_SRC / "AndroidManifest.xml")
    ap_ = str(_BUILD / "resources.ap_")
    run([aapt, "package", "-f", "-m",
         "-J", str(gen_dir),
         "-M", manifest,
         "-I", android_jar,
         "-F", ap_])

    # Step 2: javac — 编译 Java 源码
    print("\n[Step 2] 编译 Java (javac)...")
    java_files = list(_SRC.glob("*.java"))
    if not java_files:
        print("[ERROR] 未找到 Java 源文件", file=sys.stderr)
        sys.exit(1)

    # 检查是否有 gen/R.java
    r_java_files = list(gen_dir.rglob("R.java"))
    all_java_files = [str(f) for f in java_files] + [str(f) for f in r_java_files]

    run(["javac",
         "-source", "1.8", "-target", "1.8",
         "-classpath", android_jar,
         "-d", str(obj_dir),
         ] + all_java_files)

    # Step 3: d8 — dex 编译
    print("\n[Step 3] 转换为 dex (d8)...")
    class_files = list(obj_dir.rglob("*.class"))
    run([d8] + [str(f) for f in class_files]
        + ["--output", str(dex_dir)]
        + ["--classpath", android_jar])

    # Step 4: 合并 APK
    print("\n[Step 4] 合并 APK (aapt)...")
    unsigned_apk = str(_BUILD / "unsigned.apk")
    shutil.copy(ap_, unsigned_apk)
    run([aapt, "add", unsigned_apk, "classes.dex"], cwd=str(dex_dir))

    # Step 5: apksigner — 使用 debug keystore 签名
    print("\n[Step 5] 签名 APK (apksigner)...")
    # 寻找 debug keystore
    debug_ks = Path.home() / ".android" / "debug.keystore"
    if not debug_ks.exists():
        # 生成一个
        print("[Build] 生成 debug keystore...")
        run(["keytool", "-genkey", "-v",
             "-keystore", str(debug_ks),
             "-alias", "androiddebugkey",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-storepass", "android", "-keypass", "android",
             "-dname", "CN=Android Debug,O=Android,C=US"],
            check=False)

    run([apksigner, "sign",
         "--ks", str(debug_ks),
         "--ks-pass", "pass:android",
         "--key-pass", "pass:android",
         "--out", str(_OUT_APK),
         unsigned_apk])

    print(f"\n[Build] 构建成功！APK: {_OUT_APK}")
    return _OUT_APK


def install(apk_path: Path, device_serial: str):
    """安装 APK 到指定设备。"""
    print(f"\n[Install] 安装到设备: {device_serial}")
    adb_cmd = ["adb", "-s", device_serial, "install", "-r", "-t", str(apk_path)]
    rc = run(adb_cmd, check=False)
    if rc == 0:
        print(f"[Install] 安装成功！")
        # 授权 MOCK_LOCATION
        print("[Install] 授权 MockLocation...")
        run(["adb", "-s", device_serial, "shell",
             "appops", "set", _PACKAGE, "MOCK_LOCATION", "allow"],
            check=False)
        run(["adb", "-s", device_serial, "shell",
             "settings", "put", "secure", "mock_location_app", _PACKAGE],
            check=False)
        print("[Install] 授权完成！设备应已可接收模拟位置广播。")
    else:
        print(f"[Install] 安装失败（可能需要允许未知来源安装）")
        sys.exit(rc)


def verify(device_serial: str):
    """验证安装效果（发送测试广播）。"""
    print(f"\n[Verify] 发送测试广播到 {device_serial}...")
    # 测试坐标: 纽约
    cmd = [
        "adb", "-s", device_serial, "shell",
        f"am broadcast -a com.openclaw.SET_MOCK_LOCATION "
        f"--ef latitude 40.7128 --ef longitude -74.0060 "
        f"-n {_PACKAGE}/.MockLocationReceiver"
    ]
    run(cmd, check=False)
    print("[Verify] 广播已发送。如果返回 result=0 则表示位置设置成功！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OpenClaw MockLocation APK 构建工具")
    parser.add_argument("--sdk", default="", help="Android SDK 根目录路径")
    parser.add_argument("--install", default="", metavar="DEVICE_SERIAL",
                        help="构建后自动安装到指定设备")
    parser.add_argument("--verify", default="", metavar="DEVICE_SERIAL",
                        help="验证安装效果（发送测试广播）")
    parser.add_argument("--install-only", default="", metavar="DEVICE_SERIAL",
                        help="仅安装（跳过构建，使用已有 APK）")
    args = parser.parse_args()

    if args.install_only:
        if not _OUT_APK.exists():
            print(f"[ERROR] APK 不存在: {_OUT_APK}")
            sys.exit(1)
        install(_OUT_APK, args.install_only)
    else:
        apk = build(sdk_root=args.sdk)

        if args.install:
            install(apk, args.install)
            if args.verify:
                verify(args.verify)
        elif args.verify:
            verify(args.verify)
