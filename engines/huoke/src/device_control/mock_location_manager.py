# -*- coding: utf-8 -*-
"""
智能 MockLocation 多 APP 适配器 — Phase 6 P0

解决的问题:
  旧版 set_device_gps() 在真机上用 adb emu geo fix 无效（模拟器专属命令）。
  真机需要通过 MockLocation Provider APP 发送 Intent 来设置位置。
  但用户安装了哪款 FakeGPS 应用因设备而异，需要自动探测并缓存。

架构:
  1. 扫描20+已知 FakeGPS 包名（按可靠性排序）
  2. Per-device 缓存已探测到的应用（避免每次重复扫描）
  3. 通过标准化 Intent 发送 Mock 位置
  4. 未找到应用时，提供 APK 安装助手（下载+push+install）
  5. 线程安全，可从后台调用

使用:
  from src.device_control.mock_location_manager import set_mock_location, ensure_mock_app

  ok = set_mock_location(device_serial, latitude, longitude, altitude=0.0)
  if not ok:
      # 应用未安装，尝试安装
      installed = ensure_mock_app(device_serial)
"""

from __future__ import annotations

import json
import logging
import subprocess

from src.utils.subprocess_text import run as _sp_run_text
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

# ─────────────────────── 常量 ───────────────────────

# 已知的 FakeGPS / MockLocation 应用包名（按可靠性 + 流行度排序）
# 每个条目: (package_name, mock_provider_name, intent_action, description)
_KNOWN_MOCK_APPS: List[Tuple[str, str, str, str]] = [
    # ★ OpenClaw 自建 MockLocation Helper（最高优先级，行为可控）
    # Phase 7 P2: build.py 构建后安装到设备
    ("com.openclaw.mocklocation", "com.openclaw.mocklocation.MockLocationReceiver",
     "com.openclaw.SET_MOCK_LOCATION", "OpenClaw MockLocation Helper"),

    # Lexa LLC - 最广泛使用，支持完整Mock Provider
    ("com.lexa.fakegps", "com.lexa.fakegps.FakeLocationProvider",
     "com.lexa.fakegps.FAKE_LOCATION", "Fake GPS Location - Lexa"),

    # Hola VPN 旗下，广泛使用
    ("com.incorporateapps.fakegps.fre", "com.incorporateapps.fakegps.fre",
     "com.incorporateapps.fakegps.SET_LOCATION", "Fake GPS Go"),

    # 专业版假GPS
    ("com.incorporateapps.fakegps", "com.incorporateapps.fakegps",
     "com.incorporateapps.fakegps.SET_LOCATION", "Fake GPS Go Pro"),

    # LocationSpoofer
    ("com.applocationspoofer", "com.applocationspoofer.provider",
     "com.applocationspoofer.MOCK_LOCATION", "Location Spoofer"),

    # Mock Mock Location
    ("de.robv.android.xposed.modules.mock_location", "de.robv.android.xposed.modules.mock_location",
     "de.robv.android.xposed.modules.mock_location.MOCK", "Xposed Mock Location"),

    # GPS Emulator
    ("com.rosteam.gpsemulator", "com.rosteam.gpsemulator.provider",
     "com.rosteam.gpsemulator.ACTION_SET_LOCATION", "GPS Emulator"),

    # Fake Location
    ("com.evezzon.fakelocation", "com.evezzon.fakelocation.LocationProvider",
     "com.evezzon.fakelocation.SET_LOCATION", "Fake Location - Evezzon"),

    # Expert GPS Tools
    ("com.expertgpstools", "com.expertgpstools.provider",
     "com.expertgpstools.MOCK_LOCATION", "Expert GPS Tools"),

    # GPS Joystick
    ("com.theappninjas.gpsjoystick", "com.theappninjas.gpsjoystick.provider",
     "com.theappninjas.gpsjoystick.MOCK", "GPS Joystick"),

    # LGE (LG 专用内置)
    ("com.lge.fakegps", "com.lge.fakegps.provider",
     "com.lge.fakegps.MOCK_LOCATION", "LG Fake GPS"),

    # Fly GPS (国内常用)
    ("com.fly.gps", "com.fly.gps.provider",
     "com.fly.gps.MOCK", "Fly GPS"),

    # iTools Virtual Location
    ("cn.jingling.motu.photoshop", "cn.jingling.motu.photoshop.provider",
     "cn.jingling.motu.MOCK_LOCATION", "iTools Virtual Location"),

    # Location Faker
    ("net.f1yan.fakelocation", "net.f1yan.fakelocation.provider",
     "net.f1yan.fakelocation.MOCK", "Location Faker"),

    # Fake GPS Pro (by Byterev)
    ("com.byterev.fakegpspro", "com.byterev.fakegpspro.provider",
     "com.byterev.fakegpspro.SET_LOCATION", "Fake GPS Pro - Byterev"),

    # GPS Faker - Mock Location
    ("uk.co.birchlabs.openwifi", "uk.co.birchlabs.openwifi.provider",
     "uk.co.birchlabs.openwifi.FAKE_GPS", "Open Wifi Location"),

    # 小米/MIUI 自带调试工具（需开发者模式）
    ("com.miui.mockgps", "com.miui.mockgps.provider",
     "com.miui.mockgps.SET_LOCATION", "MIUI Mock GPS"),

    # 华为开发者工具
    ("com.huawei.fake.gps", "com.huawei.fake.gps.provider",
     "com.huawei.fake.gps.MOCK", "Huawei Fake GPS"),

    # Mock My GPS (v2)
    ("com.mockmygps.v2", "com.mockmygps.v2.provider",
     "com.mockmygps.v2.MOCK_LOCATION", "Mock My GPS v2"),

    # AnyGo (备用)
    ("com.anygo.app", "com.anygo.app.provider",
     "com.anygo.app.MOCK_LOCATION", "AnyGo"),

    # Location Changer
    ("com.location.changer.fake.gps.unlimited", "com.location.changer.fake.gps.unlimited.provider",
     "com.location.changer.fake.gps.unlimited.MOCK", "Location Changer - Fake GPS"),
]

# Universal Intent Action（多数 Mock 应用都响应这个）
_UNIVERSAL_MOCK_INTENT = "android.intent.action.MOCK_LOCATION"

# ADB 超时（秒）
_ADB_TIMEOUT = 10

# 设备应用缓存（device_serial → 已找到的 mock app 条目）
_device_app_cache: Dict[str, dict] = {}
_cache_lock = threading.Lock()

# 缓存文件路径
_CACHE_FILE = config_file("mock_location_cache.json")

# APK 下载目录
_APK_DIR = config_file("apks")

# 推荐安装的 APK（Phase 7 P2: 优先使用 OpenClaw 自建 APK）
_RECOMMENDED_APK = {
    "package": "com.openclaw.mocklocation",
    "filename": "openclaw_mock_location.apk",
    # 自建 APK：运行 tools/mock_location_helper/build.py 生成
    "instructions": "运行以下命令构建并安装 OpenClaw MockLocation Helper:\n"
                    "  python tools/mock_location_helper/build.py --install <device_serial>\n"
                    "或在 Web UI 调用:\n"
                    "  POST /devices/{device_id}/mock-location/install\n"
                    "注意：设备需要在「开发者选项 > 选择模拟位置应用」中选择本应用",
}

# 备用 APK（自建 APK 不可用时使用第三方）
_FALLBACK_APK = {
    "package": "com.lexa.fakegps",
    "filename": "fake_gps_lexa.apk",
    "instructions": "请从 Google Play 或可信来源下载 com.lexa.fakegps 的 APK，\n"
                    "重命名为 fake_gps_lexa.apk 并放置到 config/apks/ 目录下",
}


# ─────────────────────── ADB 辅助 ───────────────────────

def _adb(serial: str, *args, timeout: int = _ADB_TIMEOUT) -> Tuple[str, int]:
    """执行 ADB 命令，返回 (stdout, returncode)。"""
    cmd = ["adb", "-s", serial] + list(args)
    try:
        r = _sp_run_text(cmd, capture_output=True, timeout=timeout)
        return (r.stdout + r.stderr).strip(), r.returncode
    except subprocess.TimeoutExpired:
        log.warning("[MockLoc] ADB 超时: %s", " ".join(cmd))
        return "timeout", 1
    except FileNotFoundError:
        log.error("[MockLoc] ADB 未找到，请确保 adb 在 PATH 中")
        return "adb_not_found", 127


def _adb_out(serial: str, *args, timeout: int = _ADB_TIMEOUT) -> str:
    """执行 ADB 命令，仅返回 stdout 字符串。"""
    out, _ = _adb(serial, *args, timeout=timeout)
    return out


def _is_package_installed(serial: str, package: str) -> bool:
    """检查包名是否已安装。"""
    out = _adb_out(serial, "shell", "pm", "list", "packages", package)
    return f"package:{package}" in out


def _get_mock_app_setting(serial: str) -> str:
    """获取当前系统 mock_location app 设置（Android 6+）。"""
    out = _adb_out(serial, "shell", "settings", "get", "secure", "mock_location_app")
    return out.strip()


def _set_mock_app_setting(serial: str, package: str) -> bool:
    """设置系统授权的 mock_location app。"""
    _, rc = _adb(serial, "shell", "appops", "set", package, "MOCK_LOCATION", "allow")
    _, rc2 = _adb(serial, "shell", "settings", "put", "secure", "mock_location_app", package)
    return rc2 == 0


# ─────────────────────── 缓存持久化 ───────────────────────

def _load_cache():
    """从磁盘加载设备应用缓存。"""
    global _device_app_cache
    if _CACHE_FILE.exists():
        try:
            data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
            _device_app_cache = data
            log.debug("[MockLoc] 加载 %d 条设备缓存", len(_device_app_cache))
        except Exception as e:
            log.debug("[MockLoc] 缓存加载失败: %s", e)
            _device_app_cache = {}


def _save_cache():
    """将缓存写入磁盘。"""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock:
            data = dict(_device_app_cache)
        _CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.debug("[MockLoc] 缓存保存失败: %s", e)


# ─────────────────────── 核心逻辑 ───────────────────────

def scan_mock_apps(serial: str, force_rescan: bool = False) -> Optional[dict]:
    """扫描设备上已安装的 FakeGPS 应用，返回找到的第一个可用应用信息。

    Args:
        serial: 设备 ADB 序列号
        force_rescan: 忽略缓存，强制重新扫描

    Returns:
        {package, provider, intent_action, description} 或 None
    """
    # 检查缓存
    if not force_rescan:
        with _cache_lock:
            cached = _device_app_cache.get(serial)
        if cached:
            log.debug("[MockLoc] 使用缓存应用: %s @ %s", cached.get("package"), serial)
            return cached

    log.info("[MockLoc] 扫描设备 %s 的 MockLocation 应用...", serial)

    # 先检查当前系统设置的 mock app（用户已手动配置的最优先）
    current_mock = _get_mock_app_setting(serial)
    if current_mock and current_mock != "null":
        # 验证它是否还在已知列表里
        for pkg, provider, action, desc in _KNOWN_MOCK_APPS:
            if pkg == current_mock:
                result = {"package": pkg, "provider": provider,
                          "intent_action": action, "description": desc,
                          "source": "system_setting"}
                _update_cache(serial, result)
                log.info("[MockLoc] 使用系统已设置的应用: %s", pkg)
                return result

    # 按优先级扫描
    for pkg, provider, action, desc in _KNOWN_MOCK_APPS:
        if _is_package_installed(serial, pkg):
            result = {"package": pkg, "provider": provider,
                      "intent_action": action, "description": desc,
                      "source": "scan"}
            # 尝试授权
            _set_mock_app_setting(serial, pkg)
            _update_cache(serial, result)
            log.info("[MockLoc] 找到 MockLocation 应用: %s (%s)", pkg, desc)
            return result

    log.warning("[MockLoc] 设备 %s 未找到任何 MockLocation 应用", serial)
    return None


def _update_cache(serial: str, app_info: dict):
    """更新设备缓存并异步保存。"""
    with _cache_lock:
        _device_app_cache[serial] = app_info
    threading.Thread(target=_save_cache, daemon=True).start()


def clear_device_cache(serial: str):
    """清除指定设备的应用缓存（重新安装应用后调用）。"""
    with _cache_lock:
        _device_app_cache.pop(serial, None)
    _save_cache()
    log.info("[MockLoc] 已清除设备 %s 的缓存", serial)


def set_mock_location(serial: str, latitude: float, longitude: float,
                      altitude: float = 0.0, accuracy: float = 1.0) -> bool:
    """在真机上设置 Mock 位置（通过已安装的 FakeGPS 应用）。

    Args:
        serial: 设备 ADB 序列号
        latitude: 纬度 (-90 ~ 90)
        longitude: 经度 (-180 ~ 180)
        altitude: 海拔（米），默认 0
        accuracy: 精度（米），默认 1（最精确）

    Returns:
        True = 位置设置成功，False = 失败（应用未安装或命令失败）
    """
    app = scan_mock_apps(serial)
    if not app:
        log.warning("[MockLoc] %s: 无 MockLocation 应用，无法设置位置", serial)
        return False

    pkg = app["package"]
    action = app["intent_action"]

    # 方法1: 发送带坐标参数的 Intent（最标准做法）
    # 格式因应用而异，先尝试标准格式
    intent_extras = (
        f"--ef latitude {latitude} "
        f"--ef longitude {longitude} "
        f"--ef altitude {altitude} "
        f"--ef accuracy {accuracy}"
    )
    cmd = (
        f"am broadcast -a {action} "
        f"-p {pkg} "
        f"{intent_extras}"
    )
    out, rc = _adb(serial, "shell", cmd)
    if rc == 0 and ("result=0" in out or "Broadcast completed" in out or "result=-1" in out):
        log.info("[MockLoc] %s: 位置设置成功 (%.4f, %.4f) via %s",
                 serial, latitude, longitude, pkg)
        return True

    # 方法2: 用 double 类型而非 float
    intent_extras2 = (
        f"--ed lat {latitude} "
        f"--ed lng {longitude} "
        f"--ed alt {altitude}"
    )
    cmd2 = f"am broadcast -a {action} -p {pkg} {intent_extras2}"
    out2, rc2 = _adb(serial, "shell", cmd2)
    if rc2 == 0 and "Broadcast completed" in out2:
        log.info("[MockLoc] %s: 位置设置成功(方法2) (%.4f, %.4f)", serial, latitude, longitude)
        return True

    # 方法3: Universal Intent（不指定包名，广播给所有监听者）
    cmd3 = (
        f"am broadcast -a {_UNIVERSAL_MOCK_INTENT} "
        f"--ef latitude {latitude} --ef longitude {longitude}"
    )
    out3, rc3 = _adb(serial, "shell", cmd3)
    if rc3 == 0:
        log.info("[MockLoc] %s: 位置设置成功(通用Intent)", serial)
        return True

    # 所有方法失败，清除缓存重试一次
    log.warning("[MockLoc] %s: Intent 发送失败，清除缓存重新扫描", serial)
    clear_device_cache(serial)
    app2 = scan_mock_apps(serial, force_rescan=True)
    if app2 and app2["package"] != pkg:
        return set_mock_location(serial, latitude, longitude, altitude, accuracy)

    log.error("[MockLoc] %s: 设置位置失败，app=%s rc=%d out=%s", serial, pkg, rc, out)
    return False


# ─────────────────────── APK 安装助手 ───────────────────────

def get_apk_install_instructions(serial: str) -> str:
    """返回 APK 安装指引（当设备未找到任何 MockLocation 应用时）。"""
    apk_path = _APK_DIR / _RECOMMENDED_APK["filename"]
    instructions = [
        f"设备 {serial} 未安装 MockLocation 应用。",
        "",
        "自动安装方式（需先获取 APK）：",
        f"  1. {_RECOMMENDED_APK['instructions']}",
        f"  2. 将 APK 放置到: {apk_path}",
        f"  3. 调用 ensure_mock_app('{serial}') 自动安装",
        "",
        "手动安装方式：",
        "  1. 在手机上开启「允许未知来源安装」",
        f"  2. adb -s {serial} install -r fake_gps_lexa.apk",
        "  3. 手机上打开 Fake GPS 应用，授权 Mock Location",
        "  4. 调用 clear_device_cache() 清除缓存后重试",
    ]
    return "\n".join(instructions)


def ensure_mock_app(serial: str) -> bool:
    """确保设备有可用的 MockLocation 应用，如果没有则尝试安装。

    安装策略:
      1. 先扫描，如果已有则直接返回 True
      2. 检查 config/apks/ 目录是否有预备 APK
      3. 有 APK 则 adb install，完成后重新扫描
      4. 无 APK 则记录指引日志，返回 False

    Returns:
        True = 有可用应用（原有或新安装），False = 无应用且无法自动安装
    """
    # 先检查是否已有
    app = scan_mock_apps(serial)
    if app:
        return True

    # 按优先级尝试安装 APK
    candidates = [
        _RECOMMENDED_APK,   # 优先: OpenClaw 自建 APK
        _FALLBACK_APK,      # 备用: 第三方 Fake GPS
    ]

    for apk_cfg in candidates:
        apk_path = _APK_DIR / apk_cfg["filename"]
        if not apk_path.exists():
            continue
        log.info("[MockLoc] 尝试安装 APK: %s → %s", apk_path.name, serial)
        # 用 push + pm install 绕开 MIUI 14+ 的 securitycenter/AdbInstallActivity 拦截
        from src.utils.safe_apk_install import safe_install_apk
        success, out = safe_install_apk(
            "adb", serial, str(apk_path), replace=True, timeout=120)
        rc = 0 if success else 1
        if rc == 0 and "Success" in out:
            log.info("[MockLoc] APK 安装成功: %s", apk_cfg["package"])
            clear_device_cache(serial)
            pkg = apk_cfg["package"]
            _set_mock_app_setting(serial, pkg)
            app2 = scan_mock_apps(serial, force_rescan=True)
            return app2 is not None
        else:
            log.warning("[MockLoc] %s 安装失败: rc=%d, 尝试备用APK...", apk_cfg["filename"], rc)

    log.warning("[MockLoc] 所有 APK 均不可用，请手动准备。\n%s",
                get_apk_install_instructions(serial))
    return False


def get_country_gps_for_mock(country: str) -> Optional[Tuple[float, float]]:
    """获取国家的代表性 GPS 坐标（适合用于 MockLocation）。

    使用城市级别坐标（而非国家几何中心），更接近真实用户位置。
    """
    # country → (latitude, longitude) — 主要城市
    _COUNTRY_GPS: Dict[str, Tuple[float, float]] = {
        "us": (40.7128, -74.0060),       # New York
        "usa": (40.7128, -74.0060),
        "uk": (51.5074, -0.1278),         # London
        "gb": (51.5074, -0.1278),
        "germany": (52.5200, 13.4050),    # Berlin
        "de": (52.5200, 13.4050),
        "france": (48.8566, 2.3522),      # Paris
        "fr": (48.8566, 2.3522),
        "italy": (41.9028, 12.4964),      # Rome
        "it": (41.9028, 12.4964),
        "spain": (40.4168, -3.7038),      # Madrid
        "es": (40.4168, -3.7038),
        "netherlands": (52.3676, 4.9041), # Amsterdam
        "nl": (52.3676, 4.9041),
        "brazil": (-23.5505, -46.6333),   # Sao Paulo
        "br": (-23.5505, -46.6333),
        "india": (19.0760, 72.8777),      # Mumbai
        "in": (19.0760, 72.8777),
        "japan": (35.6762, 139.6503),     # Tokyo
        "jp": (35.6762, 139.6503),
        "korea": (37.5665, 126.9780),     # Seoul
        "kr": (37.5665, 126.9780),
        "australia": (-33.8688, 151.2093), # Sydney
        "au": (-33.8688, 151.2093),
        "canada": (43.6532, -79.3832),    # Toronto
        "ca": (43.6532, -79.3832),
        "singapore": (1.3521, 103.8198),  # Singapore
        "sg": (1.3521, 103.8198),
        "indonesia": (-6.2088, 106.8456), # Jakarta
        "id": (-6.2088, 106.8456),
        "thailand": (13.7563, 100.5018),  # Bangkok
        "th": (13.7563, 100.5018),
        "vietnam": (10.8231, 106.6297),   # Ho Chi Minh City
        "vn": (10.8231, 106.6297),
        "philippines": (14.5995, 120.9842), # Manila
        "ph": (14.5995, 120.9842),
        "malaysia": (3.1390, 101.6869),   # Kuala Lumpur
        "my": (3.1390, 101.6869),
        "turkey": (41.0082, 28.9784),     # Istanbul
        "tr": (41.0082, 28.9784),
        "mexico": (19.4326, -99.1332),    # Mexico City
        "mx": (19.4326, -99.1332),
        "argentina": (-34.6037, -58.3816), # Buenos Aires
        "ar": (-34.6037, -58.3816),
        "egypt": (30.0444, 31.2357),      # Cairo
        "eg": (30.0444, 31.2357),
        "russia": (55.7558, 37.6176),     # Moscow
        "ru": (55.7558, 37.6176),
        "china": (31.2304, 121.4737),     # Shanghai
        "cn": (31.2304, 121.4737),
    }
    key = country.lower().strip()
    return _COUNTRY_GPS.get(key)


def configure_mock_location_for_country(serial: str, country: str) -> bool:
    """根据国家代码为设备设置 Mock GPS 位置。

    Args:
        serial: ADB 设备序列号
        country: 国家代码（'us', 'uk', 'japan' 等）

    Returns:
        True = 设置成功，False = 失败（应用不可用或国家未知）
    """
    coords = get_country_gps_for_mock(country)
    if not coords:
        log.warning("[MockLoc] 未找到国家 '%s' 的 GPS 坐标", country)
        return False

    lat, lon = coords
    log.info("[MockLoc] 为设备 %s 设置 %s 位置: (%.4f, %.4f)", serial, country, lat, lon)
    return set_mock_location(serial, lat, lon)


# ─────────────────────── 状态查询 ───────────────────────

def get_device_mock_status(serial: str) -> dict:
    """获取设备的 MockLocation 应用状态。"""
    with _cache_lock:
        cached = _device_app_cache.get(serial)

    current_system_app = _get_mock_app_setting(serial)
    installed_apps = []

    for pkg, _, _, desc in _KNOWN_MOCK_APPS[:5]:  # 只检查前5个（减少ADB调用）
        if _is_package_installed(serial, pkg):
            installed_apps.append({"package": pkg, "description": desc})

    return {
        "serial": serial,
        "cached_app": cached,
        "system_mock_app": current_system_app,
        "installed_apps": installed_apps,
        "has_mock_app": cached is not None or len(installed_apps) > 0,
        "apk_available": (_APK_DIR / _RECOMMENDED_APK["filename"]).exists(),
    }


# ─────────────────────── 模块初始化 ───────────────────────
_load_cache()
