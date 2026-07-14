# -*- coding: utf-8 -*-
"""
VPN Manager — 通过 ADB 自动配置 V2RayNG 到手机。

功能：
  1. 解码二维码图片 → 提取 V2Ray URI
  2. 通过 ADB 剪贴板导入配置到 V2RayNG
  3. 启动 V2RayNG 并连接
  4. 验证 VPN 生效（Geo-IP 检查）
  5. 批量配置多台设备

用法：
  # 从二维码图片配置单台设备
  python scripts/vpn_manager.py push --qr <qr_image.png> --device AIUKQ8WSKZBUQK4X

  # 从二维码图片配置所有在线设备
  python scripts/vpn_manager.py push --qr <qr_image.png> --all

  # 直接用 URI 配置
  python scripts/vpn_manager.py push --uri "vless://..." --device AIUKQ8WSKZBUQK4X

  # 更新二维码（替换旧配置）
  python scripts/vpn_manager.py update --qr <new_qr.png> --all

  # 检查所有设备 VPN 状态
  python scripts/vpn_manager.py status

  # 断开 VPN
  python scripts/vpn_manager.py stop --device AIUKQ8WSKZBUQK4X
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Optional, List, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

V2RAYNG_PKG = "com.v2ray.ang"
V2RAYNG_MAIN = "com.v2ray.ang.ui.MainActivity"


# ═══════════════════════════════════════════════════════════════
# QR Code Decoding
# ═══════════════════════════════════════════════════════════════

def decode_qr(image_path: str) -> str:
    """Decode a QR code image and return the raw text."""
    try:
        import cv2
        img = cv2.imread(image_path)
        if img is None:
            raise ValueError(f"Cannot read image: {image_path}")
        detector = cv2.QRCodeDetector()
        data, _, _ = detector.detectAndDecode(img)
        if data:
            return data
        import numpy as np
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
        data, _, _ = detector.detectAndDecode(binary)
        if data:
            return data
    except ImportError:
        pass

    try:
        from pyzbar.pyzbar import decode as pyzbar_decode
        from PIL import Image
        img = Image.open(image_path)
        results = pyzbar_decode(img)
        if results:
            return results[0].data.decode("utf-8")
    except (ImportError, Exception):
        pass

    raise RuntimeError(f"无法解码二维码: {image_path}")


def parse_v2ray_uri(uri: str) -> Dict:
    """Parse a V2Ray URI into structured config."""
    uri = uri.strip()
    if uri.startswith("vmess://"):
        payload = uri[8:]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        config = json.loads(base64.b64decode(payload).decode())
        return {
            "protocol": "vmess",
            "server": config.get("add", ""),
            "port": config.get("port", ""),
            "uuid": config.get("id", ""),
            "remark": config.get("ps", ""),
            "uri": uri,
        }
    elif uri.startswith("vless://"):
        parts = urllib.parse.urlparse(uri)
        uuid = parts.username or ""
        host = parts.hostname or ""
        port = parts.port or 443
        fragment = urllib.parse.unquote(parts.fragment) if parts.fragment else ""
        params = dict(urllib.parse.parse_qsl(parts.query))
        return {
            "protocol": "vless",
            "server": host,
            "port": port,
            "uuid": uuid,
            "remark": fragment,
            "transport": params.get("type", "tcp"),
            "security": params.get("security", "none"),
            "uri": uri,
        }
    elif uri.startswith("ss://"):
        return {"protocol": "shadowsocks", "uri": uri, "remark": ""}
    elif uri.startswith("trojan://"):
        parts = urllib.parse.urlparse(uri)
        return {
            "protocol": "trojan",
            "server": parts.hostname,
            "port": parts.port,
            "remark": urllib.parse.unquote(parts.fragment) if parts.fragment else "",
            "uri": uri,
        }
    raise ValueError(f"不支持的协议: {uri[:30]}...")


# ═══════════════════════════════════════════════════════════════
# ADB Operations
# ═══════════════════════════════════════════════════════════════

def adb(device_id: str, cmd: str, timeout: int = 15) -> str:
    """Run an ADB command and return output."""
    full_cmd = f"adb -s {device_id} {cmd}"
    try:
        result = subprocess.run(
            full_cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def get_online_devices() -> List[str]:
    """Get list of online ADB device IDs."""
    result = subprocess.run(
        "adb devices", shell=True, capture_output=True, text=True, timeout=10
    )
    devices = []
    for line in result.stdout.strip().split("\n")[1:]:
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


def set_clipboard(device_id: str, text: str):
    """Set device clipboard content via ADB."""
    escaped = text.replace("'", "'\\''")
    adb(device_id, f"shell am broadcast -a clipper.set -e text '{escaped}'", timeout=5)
    time.sleep(0.3)
    adb(device_id, f"shell input keyevent 279", timeout=3)
    time.sleep(0.2)


def push_uri_to_v2rayng(device_id: str, uri: str, connect: bool = True) -> bool:
    """
    Push a V2Ray URI to V2RayNG via ADB.

    Strategy:
    1. Write URI to a temp file on device
    2. Use V2RayNG's built-in intent to import
    3. Fallback: clipboard + UI automation
    """
    print(f"  [{device_id[:12]}] 推送 V2Ray 配置...")

    adb(device_id, "shell am force-stop com.v2ray.ang")
    time.sleep(1)

    adb(device_id, f"shell am start -n {V2RAYNG_PKG}/{V2RAYNG_MAIN}")
    time.sleep(3)

    tmp_file = "/sdcard/v2ray_import.txt"
    escaped_uri = uri.replace("'", "'\\''")
    adb(device_id, f"shell \"echo '{escaped_uri}' > {tmp_file}\"")
    time.sleep(0.5)

    result = adb(device_id, f"shell am start -a android.intent.action.VIEW -d '{uri}'")
    time.sleep(2)

    if "Error" in result or not result:
        print(f"  [{device_id[:12]}] Intent 导入失败，尝试剪贴板方式...")
        _import_via_clipboard(device_id, uri)

    if connect:
        time.sleep(2)
        _connect_v2rayng(device_id)

    adb(device_id, f"shell rm -f {tmp_file}")

    print(f"  [{device_id[:12]}] 配置完成")
    return True


def _import_via_clipboard(device_id: str, uri: str):
    """Import config via clipboard + UI taps."""
    adb(device_id, f"shell am start -n {V2RAYNG_PKG}/{V2RAYNG_MAIN}")
    time.sleep(2)

    adb(device_id, f"shell \"echo '{uri}' | am broadcast -a clipper.set --es text -\"")
    time.sleep(0.5)

    try:
        import uiautomator2 as u2
        d = u2.connect(device_id)

        plus_btn = d(resourceId="com.v2ray.ang:id/fab")
        if plus_btn.exists(timeout=3):
            plus_btn.click()
            time.sleep(1)

        clipboard_opt = d(textContains="clipboard")
        if not clipboard_opt.exists(timeout=2):
            clipboard_opt = d(textContains="剪贴板")
        if not clipboard_opt.exists(timeout=2):
            clipboard_opt = d(textContains="Clipboard")

        if clipboard_opt.exists(timeout=2):
            clipboard_opt.click()
            time.sleep(2)
            print(f"  [{device_id[:12]}] 剪贴板导入成功")
        else:
            _manual_clipboard_import(d, device_id, uri)
    except Exception as e:
        print(f"  [{device_id[:12]}] u2 导入失败: {e}")
        _fallback_file_import(device_id, uri)


def _manual_clipboard_import(d, device_id: str, uri: str):
    """Manual clipboard import using u2 UI automation."""
    d.set_clipboard(uri)
    time.sleep(0.5)

    menu_items = [
        "Import config from clipboard",
        "从剪贴板导入",
        "Import from clipboard",
        "从剪切板导入",
    ]
    for text in menu_items:
        el = d(textContains=text)
        if el.exists(timeout=1):
            el.click()
            time.sleep(2)
            print(f"  [{device_id[:12]}] 手动剪贴板导入: '{text}'")
            return

    print(f"  [{device_id[:12]}] 无法找到剪贴板导入选项")


def _fallback_file_import(device_id: str, uri: str):
    """Fallback: write config file directly (requires root)."""
    print(f"  [{device_id[:12]}] 尝试文件直接写入 (需要 root)...")
    check = adb(device_id, "shell su -c 'whoami'")
    if "root" not in check:
        print(f"  [{device_id[:12]}] 无 root 权限，跳过文件写入")
        return

    config_dir = f"/data/data/{V2RAYNG_PKG}/files"
    adb(device_id, f"shell su -c 'mkdir -p {config_dir}'")

    escaped = uri.replace("'", "'\\''")
    adb(device_id,
        f"shell su -c \"echo '{escaped}' > {config_dir}/v2ray_import.txt\"")
    print(f"  [{device_id[:12]}] URI 已写入 {config_dir}")


def _connect_v2rayng(device_id: str):
    """Tap the connect button in V2RayNG."""
    try:
        import uiautomator2 as u2
        d = u2.connect(device_id)

        connect_btn = d(resourceId="com.v2ray.ang:id/fab")
        if not connect_btn.exists(timeout=3):
            connect_btn = d(description="connect")
        if not connect_btn.exists(timeout=2):
            connect_btn = d(className="com.google.android.material.floatingactionbutton.FloatingActionButton")

        if connect_btn.exists(timeout=3):
            connect_btn.click()
            time.sleep(2)

            ok_btn = d(text="OK")
            if not ok_btn.exists(timeout=1):
                ok_btn = d(text="确定")
            if ok_btn.exists(timeout=2):
                ok_btn.click()
                time.sleep(1)

            print(f"  [{device_id[:12]}] VPN 连接已启动")
        else:
            print(f"  [{device_id[:12]}] 未找到连接按钮")
    except Exception as e:
        print(f"  [{device_id[:12]}] 连接操作失败: {e}")


def stop_v2rayng(device_id: str):
    """Stop V2RayNG on device."""
    adb(device_id, f"shell am force-stop {V2RAYNG_PKG}")
    print(f"  [{device_id[:12]}] V2RayNG 已停止")


def check_vpn_status(device_id: str) -> Dict:
    """Check VPN status and geo-IP on device."""
    vpn_active = "tun0" in adb(device_id, "shell ip addr show tun0 2>/dev/null")

    ip_info = {"ip": "", "country": "", "vpn_active": vpn_active}

    if vpn_active:
        ip_raw = adb(device_id,
                     "shell curl -s --max-time 5 http://ip-api.com/json/?fields=query,country,countryCode",
                     timeout=10)
        try:
            data = json.loads(ip_raw)
            ip_info["ip"] = data.get("query", "")
            ip_info["country"] = data.get("country", "")
            ip_info["country_code"] = data.get("countryCode", "")
        except (json.JSONDecodeError, Exception):
            ip_raw2 = adb(device_id,
                          "shell curl -s --max-time 5 https://ipapi.co/json/",
                          timeout=10)
            try:
                data2 = json.loads(ip_raw2)
                ip_info["ip"] = data2.get("ip", "")
                ip_info["country"] = data2.get("country_name", "")
                ip_info["country_code"] = data2.get("country_code", "")
            except Exception:
                pass

    return ip_info


# ═══════════════════════════════════════════════════════════════
# Config Persistence
# ═══════════════════════════════════════════════════════════════

CONFIG_FILE = Path(__file__).parent.parent / "config" / "vpn_config.json"


def save_current_config(uri: str, parsed: Dict):
    """Save current VPN config for later reference."""
    data = {
        "uri": uri,
        "protocol": parsed.get("protocol", ""),
        "server": parsed.get("server", ""),
        "port": parsed.get("port", ""),
        "remark": parsed.get("remark", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  配置已保存到 {CONFIG_FILE}")


def load_saved_config() -> Optional[Dict]:
    """Load previously saved VPN config."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ═══════════════════════════════════════════════════════════════
# Main Commands
# ═══════════════════════════════════════════════════════════════

def cmd_push(args):
    """Push VPN config to device(s)."""
    if args.qr:
        print(f"解码二维码: {args.qr}")
        uri = decode_qr(args.qr)
    elif args.uri:
        uri = args.uri
    else:
        print("ERROR: 需要 --qr 或 --uri 参数")
        return

    parsed = parse_v2ray_uri(uri)
    print(f"协议: {parsed['protocol']}")
    print(f"服务器: {parsed.get('server', '?')}:{parsed.get('port', '?')}")
    print(f"备注: {parsed.get('remark', '')}")
    print()

    save_current_config(uri, parsed)

    if args.all:
        devices = get_online_devices()
    elif args.device:
        devices = [args.device]
    else:
        devices = get_online_devices()

    if not devices:
        print("ERROR: 没有在线设备")
        return

    print(f"目标设备: {len(devices)} 台")
    for did in devices:
        try:
            push_uri_to_v2rayng(did, uri, connect=not args.no_connect)
        except Exception as e:
            print(f"  [{did[:12]}] 失败: {e}")

    if not args.no_connect:
        print(f"\n等待 VPN 连接生效 (5 秒)...")
        time.sleep(5)
        for did in devices:
            status = check_vpn_status(did)
            vpn = "已连接" if status["vpn_active"] else "未连接"
            ip = status.get("ip", "?")
            country = status.get("country", "?")
            print(f"  [{did[:12]}] VPN {vpn} | IP={ip} | 国家={country}")


def cmd_update(args):
    """Update VPN config (remove old, add new)."""
    print("更新 VPN 配置...")

    if args.qr:
        uri = decode_qr(args.qr)
    elif args.uri:
        uri = args.uri
    else:
        print("ERROR: 需要 --qr 或 --uri")
        return

    parsed = parse_v2ray_uri(uri)
    save_current_config(uri, parsed)

    devices = get_online_devices() if args.all else [args.device]
    for did in devices:
        stop_v2rayng(did)
        time.sleep(1)

    for did in devices:
        push_uri_to_v2rayng(did, uri, connect=True)

    time.sleep(5)
    for did in devices:
        status = check_vpn_status(did)
        vpn = "已连接" if status["vpn_active"] else "未连接"
        print(f"  [{did[:12]}] VPN {vpn} | {status.get('country', '?')}")


def cmd_status(args):
    """Check VPN status on all online devices."""
    devices = get_online_devices()
    if not devices:
        print("没有在线设备")
        return

    print(f"检查 {len(devices)} 台设备的 VPN 状态:\n")
    for did in devices:
        status = check_vpn_status(did)
        vpn = "已连接" if status["vpn_active"] else "未连接"
        ip = status.get("ip", "N/A")
        country = status.get("country", "N/A")
        code = status.get("country_code", "")
        print(f"  {did[:16]} | VPN: {vpn} | IP: {ip} | {country} ({code})")

    saved = load_saved_config()
    if saved:
        print(f"\n  当前配置: {saved.get('remark', '')} "
              f"({saved.get('server', '')}:{saved.get('port', '')}) "
              f"更新于 {saved.get('updated_at', '')}")


def cmd_stop(args):
    """Stop VPN on device(s)."""
    devices = get_online_devices() if args.all else [args.device]
    for did in devices:
        stop_v2rayng(did)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="V2RayNG VPN Manager")
    subparsers = parser.add_subparsers(dest="command")

    push_p = subparsers.add_parser("push", help="推送 VPN 配置到设备")
    push_p.add_argument("--qr", help="二维码图片路径")
    push_p.add_argument("--uri", help="V2Ray URI 字符串")
    push_p.add_argument("--device", "-d", help="目标设备 ID")
    push_p.add_argument("--all", "-a", action="store_true", help="所有在线设备")
    push_p.add_argument("--no-connect", action="store_true", help="不自动连接")

    update_p = subparsers.add_parser("update", help="更新 VPN 配置（替换旧的）")
    update_p.add_argument("--qr", help="新二维码图片路径")
    update_p.add_argument("--uri", help="新 V2Ray URI")
    update_p.add_argument("--device", "-d", help="目标设备 ID")
    update_p.add_argument("--all", "-a", action="store_true", help="所有在线设备")

    status_p = subparsers.add_parser("status", help="检查 VPN 状态")

    stop_p = subparsers.add_parser("stop", help="停止 VPN")
    stop_p.add_argument("--device", "-d", help="目标设备 ID")
    stop_p.add_argument("--all", "-a", action="store_true", help="所有在线设备")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {"push": cmd_push, "update": cmd_update,
     "status": cmd_status, "stop": cmd_stop}[args.command](args)
