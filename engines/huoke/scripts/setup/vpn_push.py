# -*- coding: utf-8 -*-
"""
Push V2Ray config to V2RayNG via UI automation.

Handles Android 13+ clipboard restrictions by directly filling
the manual config form using uiautomator2.

Usage:
  python scripts/vpn_push.py <qr_image_or_uri> [device_id]
"""
import json
import os
import sys
import time
import subprocess
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

V2RAYNG_PKG = "com.v2ray.ang"

def decode_qr(path: str) -> str:
    import cv2
    img = cv2.imread(path)
    detector = cv2.QRCodeDetector()
    data, _, _ = detector.detectAndDecode(img)
    if data:
        return data
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, b = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    data, _, _ = detector.detectAndDecode(b)
    return data or ""


def parse_vless(uri: str) -> dict:
    p = urllib.parse.urlparse(uri)
    params = dict(urllib.parse.parse_qsl(p.query))
    return {
        "uuid": p.username or "",
        "server": p.hostname or "",
        "port": str(p.port or 443),
        "remark": urllib.parse.unquote(p.fragment) if p.fragment else "",
        "security": params.get("security", "none"),
        "encryption": params.get("encryption", "none"),
        "transport": params.get("type", "tcp"),
        "ws_path": urllib.parse.unquote(params.get("path", "/")),
        "ws_host": params.get("host", ""),
        "sni": params.get("sni", ""),
    }


def adb(device_id: str, cmd: str) -> str:
    r = subprocess.run(f"adb -s {device_id} {cmd}",
                       shell=True, capture_output=True, text=True, timeout=15)
    return r.stdout.strip()


def push_config(device_id: str, uri: str):
    """Push V2Ray config to device using UI automation."""
    import uiautomator2 as u2

    if uri.startswith("vless://"):
        cfg = parse_vless(uri)
        protocol = "vless"
    else:
        print(f"暂不支持该协议，仅支持 vless://")
        return False

    print(f"配置信息:")
    print(f"  协议:   VLESS")
    print(f"  服务器: {cfg['server']}:{cfg['port']}")
    print(f"  UUID:   {cfg['uuid'][:8]}...")
    print(f"  传输:   {cfg['transport']}")
    print(f"  备注:   {cfg['remark']}")
    print()

    adb(device_id, f"shell am force-stop {V2RAYNG_PKG}")
    time.sleep(1)

    d = u2.connect(device_id)
    print(f"[1/7] 启动 V2RayNG...")
    d.app_start(V2RAYNG_PKG)
    time.sleep(3)

    print(f"[2/7] 点击 + 按钮...")
    fab = d(resourceId="com.v2ray.ang:id/fab")
    if not fab.exists(timeout=5):
        d(description="Start or Stop").click()
        time.sleep(1)
        fab = d(resourceId="com.v2ray.ang:id/fab")

    if not fab.exists(timeout=3):
        for cls in ["ImageButton", "FloatingActionButton"]:
            btn = d(className=f"android.widget.{cls}")
            if btn.exists(timeout=2):
                fab = btn
                break

    if fab.exists(timeout=3):
        fab.click()
        time.sleep(1.5)
    else:
        print("  找不到 + 按钮，尝试菜单...")
        d(description="More options").click_exists(timeout=2)
        time.sleep(1)

    print(f"[3/7] 选择手动配置 [Vless]...")
    vless_options = [
        "Type manually[VLESS]",
        "手动输入[VLESS]",
        "Type manually [VLESS]",
        "VLESS",
        "手动输入[Vless]",
        "Type manually[Vless]",
    ]
    found = False
    for text in vless_options:
        el = d(textContains=text)
        if el.exists(timeout=1):
            el.click()
            found = True
            time.sleep(2)
            print(f"  选中: '{text}'")
            break

    if not found:
        items = d(className="android.widget.TextView")
        available = []
        for i in range(items.count):
            try:
                t = items[i].get_text()
                available.append(t)
                if "vless" in t.lower() or "manually" in t.lower() or "手动" in t:
                    items[i].click()
                    found = True
                    time.sleep(2)
                    print(f"  选中: '{t}'")
                    break
            except Exception:
                pass

        if not found:
            print(f"  可用选项: {available}")
            print("  ERROR: 找不到 VLESS 手动配置选项")
            return False

    print(f"[4/7] 填写配置表单...")

    field_map = {
        "remarks": cfg["remark"],
        "address": cfg["server"],
        "port": cfg["port"],
        "id": cfg["uuid"],
        "encryption": cfg["encryption"],
    }

    def fill_field(hint_text: str, value: str) -> bool:
        """Find an EditText by hint and fill it."""
        targets = [
            d(resourceId=f"com.v2ray.ang:id/et_remarks"),
            d(resourceId=f"com.v2ray.ang:id/et_address"),
            d(resourceId=f"com.v2ray.ang:id/et_port"),
            d(resourceId=f"com.v2ray.ang:id/et_id"),
        ]
        el = d(className="android.widget.EditText", textContains=hint_text)
        if not el.exists(timeout=1):
            el = d(className="android.widget.EditText",
                   resourceIdMatches=f".*{hint_text}.*")
        if el.exists(timeout=2):
            el.clear_text()
            time.sleep(0.3)
            el.set_text(value)
            time.sleep(0.3)
            return True
        return False

    all_edits = d(className="android.widget.EditText")
    edit_count = all_edits.count
    print(f"  找到 {edit_count} 个输入框")

    field_values = [
        cfg["remark"],
        cfg["server"],
        cfg["port"],
        cfg["uuid"],
    ]

    for i, val in enumerate(field_values):
        if i < edit_count:
            try:
                all_edits[i].clear_text()
                time.sleep(0.2)
                all_edits[i].set_text(val)
                time.sleep(0.3)
                print(f"  字段 {i}: '{val[:30]}' ✓")
            except Exception as e:
                print(f"  字段 {i}: 填写失败 - {e}")

    if edit_count > 4:
        try:
            all_edits[4].clear_text()
            all_edits[4].set_text(cfg["encryption"])
            print(f"  字段 4 (encryption): '{cfg['encryption']}' ✓")
        except Exception:
            pass

    print(f"[5/7] 配置传输层 ({cfg['transport']})...")
    d.swipe(0.5, 0.8, 0.5, 0.3)
    time.sleep(1)

    transport_spinner = d(resourceIdMatches=".*network.*|.*transport.*")
    if not transport_spinner.exists(timeout=2):
        transport_spinner = d(textContains="tcp")
    if transport_spinner.exists(timeout=2):
        transport_spinner.click()
        time.sleep(1)
        ws_opt = d(text="ws")
        if ws_opt.exists(timeout=2):
            ws_opt.click()
            time.sleep(1)
            print(f"  传输: ws ✓")

            ws_edits = d(className="android.widget.EditText")
            for i in range(ws_edits.count):
                try:
                    hint = ws_edits[i].info.get("text", "")
                    if not hint or hint == "/" or "path" in str(ws_edits[i].info).lower():
                        ws_edits[i].clear_text()
                        ws_edits[i].set_text(cfg["ws_path"])
                        print(f"  ws path: '{cfg['ws_path']}' ✓")
                        break
                except Exception:
                    pass
    else:
        print("  传输层 spinner 未找到")

    print(f"[6/7] 保存配置...")
    d.swipe(0.5, 0.3, 0.5, 0.8)
    time.sleep(0.5)

    save_btn = d(description="save")
    if not save_btn.exists(timeout=2):
        save_btn = d(resourceIdMatches=".*save.*|.*confirm.*|.*done.*")
    if not save_btn.exists(timeout=2):
        save_btn = d(text="✓")
    if not save_btn.exists(timeout=2):
        save_btn = d(className="android.widget.ImageButton", instance=1)

    if save_btn.exists(timeout=3):
        save_btn.click()
        time.sleep(2)
        print(f"  保存 ✓")
    else:
        d.press("back")
        time.sleep(1)
        ok = d(text="OK")
        if not ok.exists(timeout=1):
            ok = d(text="确定")
        if ok.exists(timeout=2):
            ok.click()
            time.sleep(1)
        print(f"  尝试 back 保存")

    print(f"[7/7] 启动 VPN 连接...")
    time.sleep(1)

    connect_btn = d(resourceId="com.v2ray.ang:id/fab")
    if connect_btn.exists(timeout=3):
        connect_btn.click()
        time.sleep(2)

    vpn_dialog = d(text="OK")
    if not vpn_dialog.exists(timeout=1):
        vpn_dialog = d(text="确定")
    if vpn_dialog.exists(timeout=3):
        vpn_dialog.click()
        time.sleep(2)
        print(f"  VPN 权限已确认")

    time.sleep(3)
    tun = adb(device_id, "shell ip addr show tun0 2>/dev/null")
    if "tun0" in tun:
        print(f"\n  VPN 已连接 ✓")
    else:
        print(f"\n  VPN 未检测到 tun0 (可能需要手动确认)")

    ip_raw = adb(device_id,
                 "shell curl -s --max-time 8 http://ip-api.com/json/?fields=query,country,countryCode")
    try:
        ip_data = json.loads(ip_raw)
        print(f"  IP: {ip_data.get('query', '?')} → {ip_data.get('country', '?')} ({ip_data.get('countryCode', '?')})")
    except Exception:
        print(f"  IP 检测失败 (VPN 可能需要更多时间)")

    config_path = Path(__file__).parent.parent / "config" / "vpn_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"uri": uri, **cfg, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  f, indent=2, ensure_ascii=False)
    print(f"  配置已保存到 {config_path}")

    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/vpn_push.py <qr_image_or_uri> [device_id]")
        sys.exit(1)

    input_arg = sys.argv[1]
    device_id = sys.argv[2] if len(sys.argv) > 2 else None

    if not device_id:
        r = subprocess.run("adb devices", shell=True, capture_output=True, text=True)
        for line in r.stdout.strip().split("\n")[1:]:
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1] == "device":
                device_id = parts[0]
                break
        if not device_id:
            print("ERROR: 没有在线设备")
            sys.exit(1)

    print(f"目标设备: {device_id}")
    print()

    if os.path.isfile(input_arg):
        print(f"解码二维码: {input_arg}")
        uri = decode_qr(input_arg)
        if not uri:
            print("ERROR: 无法解码二维码")
            sys.exit(1)
    else:
        uri = input_arg

    print(f"URI: {uri[:80]}...")
    print()

    push_config(device_id, uri)
