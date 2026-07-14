# -*- coding: utf-8 -*-
"""B-level tests: device connectivity, u2, screenshot, health API."""
import sys
import os
import time
import json
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OPENCLAW_API_KEY", "")

DEVICE_ID = "AIUKQ8WSKZBUQK4X"
results = {}


def test_b1_u2_connection():
    """B1: u2 connectivity test."""
    import uiautomator2 as u2
    d = u2.connect(DEVICE_ID)
    info = d.info
    w, h = info["displayWidth"], info["displayHeight"]
    sdk = info["sdkInt"]
    bat = d.shell("dumpsys battery | grep level").output.strip()
    pkg = d.app_current()

    result = {
        "status": "PASS",
        "screen": f"{w}x{h}",
        "sdk": sdk,
        "battery": bat,
        "current_app": pkg,
    }
    print(f"  [B1] u2 连接: PASS | {w}x{h} | SDK {sdk} | {bat}")
    print(f"       当前应用: {pkg}")
    return result


def test_b2_screenshot():
    """B2: Screenshot capture test."""
    import uiautomator2 as u2
    d = u2.connect(DEVICE_ID)
    img = d.screenshot()
    tmp = os.path.join(tempfile.gettempdir(), "b2_screenshot.png")
    img.save(tmp)
    size = os.path.getsize(tmp)
    ok = size > 10000
    status = "PASS" if ok else "FAIL"
    print(f"  [B2] 截屏: {status} | {size / 1024:.0f} KB | {tmp}")
    os.unlink(tmp)
    return {"status": status, "size_kb": size / 1024}


def test_b3_tiktok_installed():
    """B3: Check TikTok is installed."""
    import uiautomator2 as u2
    d = u2.connect(DEVICE_ID)
    packages = ["com.ss.android.ugc.trill", "com.zhiliaoapp.musically"]
    found = None
    for pkg in packages:
        check = d.shell(f"pm list packages {pkg}").output.strip()
        if pkg in check:
            found = pkg
            break
    status = "PASS" if found else "FAIL"
    print(f"  [B3] TikTok 安装: {status} | package={found}")
    return {"status": status, "package": found}


def test_b4_device_manager_api():
    """B4: Device manager discovery test via code."""
    from src.device_control.device_manager import DeviceManager
    dm = DeviceManager()
    dm.load_config("config/devices.yaml")
    dm.discover_devices()
    all_devs = dm.get_all_devices()
    connected = dm.get_connected_devices()
    status = "PASS" if len(connected) >= 1 else "FAIL"
    print(f"  [B4] DeviceManager: {status} | 配置 {len(all_devs)} 台 | 在线 {len(connected)} 台")
    return {"status": status, "total": len(all_devs), "connected": len(connected)}


def test_b5_health_api():
    """B5: Health API endpoint test."""
    from src.host.database import init_db
    init_db()
    from src.host.api import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.get("/health")
        data = r.json()
        status = "PASS" if r.status_code == 200 else "FAIL"
        print(f"  [B5] /health API: {status} | status={data.get('status')}")

        r2 = client.get("/devices")
        devs = r2.json()
        n = len(devs) if isinstance(devs, list) else devs.get("count", "?")
        print(f"       /devices: {n} 台")

        return {"status": status, "health": data.get("status"), "devices": n}


def test_b6_geo_check():
    """B6: Geo-IP check (requires network on device)."""
    try:
        from src.behavior.geo_check import check_device_geo
        from src.device_control.device_manager import DeviceManager
        dm = DeviceManager()
        dm.load_config("config/devices.yaml")
        dm.discover_devices()
        result = check_device_geo(DEVICE_ID, "italy", device_manager=dm)
        status = "PASS" if result.public_ip else "INFO"
        match_str = "匹配" if result.matches else "不匹配"
        print(f"  [B6] Geo-IP: {status} | IP={result.public_ip} | "
              f"国家={result.detected_country} ({result.detected_country_code}) | "
              f"{match_str}")
        return {
            "status": status,
            "ip": result.public_ip,
            "country": result.detected_country,
            "country_code": result.detected_country_code,
            "matches": result.matches,
        }
    except Exception as e:
        print(f"  [B6] Geo-IP: SKIP | {e}")
        return {"status": "SKIP", "error": str(e)}


if __name__ == "__main__":
    print("=" * 60)
    print("B级测试: 设备连接验证")
    print("=" * 60)
    print(f"目标设备: {DEVICE_ID}")
    print()

    tests = [
        ("B1_u2", test_b1_u2_connection),
        ("B2_screenshot", test_b2_screenshot),
        ("B3_tiktok", test_b3_tiktok_installed),
        ("B4_device_manager", test_b4_device_manager_api),
        ("B5_health_api", test_b5_health_api),
        ("B6_geo_check", test_b6_geo_check),
    ]

    for name, fn in tests:
        try:
            results[name] = fn()
        except Exception as e:
            results[name] = {"status": "ERROR", "error": str(e)}
            print(f"  [{name}] ERROR: {e}")
        print()

    passed = sum(1 for r in results.values() if r["status"] == "PASS")
    total = len(results)
    print("=" * 60)
    print(f"B级结果: {passed}/{total} PASS")
    for name, r in results.items():
        print(f"  {name}: {r['status']}")
    print("=" * 60)
