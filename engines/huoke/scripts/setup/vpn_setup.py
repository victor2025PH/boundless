# -*- coding: utf-8 -*-
"""
V2RayNG 完整自动配置工具。

完整流程:
  1. 停止 VPN 服务
  2. 删除所有旧配置
  3. 导入新配置 (从二维码或 URI)
  4. 设置分应用代理 (仅 TikTok 走 VPN)
  5. 启动 VPN 连接
  6. 验证 IP 地址

用法:
  python scripts/vpn_setup.py <二维码图片路径或URI> [设备ID]
  python scripts/vpn_setup.py <二维码图片路径或URI> --all
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

V2RAYNG = "com.v2ray.ang"
TIKTOK_PKG = "com.ss.android.ugc.trill"
TIKTOK_ALT = "com.zhiliaoapp.musically"

VPN_APPS = [TIKTOK_PKG, TIKTOK_ALT]


def decode_qr(path: str) -> str:
    import cv2
    img = cv2.imread(path)
    if img is None:
        raise FileNotFoundError(f"无法读取图片: {path}")
    det = cv2.QRCodeDetector()
    data, _, _ = det.detectAndDecode(img)
    if data:
        return data
    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, b = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    data, _, _ = det.detectAndDecode(b)
    if not data:
        raise RuntimeError("二维码解码失败")
    return data


def parse_vless(uri: str) -> dict:
    p = urllib.parse.urlparse(uri)
    params = dict(urllib.parse.parse_qsl(p.query))
    return {
        "protocol": "vless",
        "uuid": p.username or "",
        "server": p.hostname or "",
        "port": str(p.port or 443),
        "remark": urllib.parse.unquote(p.fragment) if p.fragment else "Italy VPN",
        "encryption": params.get("encryption", "none"),
        "security": params.get("security", "none"),
        "transport": params.get("type", "tcp"),
        "ws_path": urllib.parse.unquote(params.get("path", "/")),
        "ws_host": params.get("host", ""),
        "sni": params.get("sni", ""),
        "uri": uri,
    }


def parse_vmess(uri: str) -> dict:
    import base64
    payload = uri[8:]
    pad = 4 - len(payload) % 4
    if pad != 4:
        payload += "=" * pad
    cfg = json.loads(base64.b64decode(payload).decode())
    return {
        "protocol": "vmess",
        "server": cfg.get("add", ""),
        "port": str(cfg.get("port", "")),
        "uuid": cfg.get("id", ""),
        "remark": cfg.get("ps", "VMess"),
        "uri": uri,
    }


def parse_uri(uri: str) -> dict:
    uri = uri.strip()
    if uri.startswith("vless://"):
        return parse_vless(uri)
    elif uri.startswith("vmess://"):
        return parse_vmess(uri)
    elif uri.startswith("ss://") or uri.startswith("trojan://"):
        return {"protocol": uri.split("://")[0], "uri": uri,
                "remark": "Proxy", "server": "", "port": ""}
    raise ValueError(f"不支持的协议: {uri[:30]}")


def adb(device_id: str, cmd: str, timeout: int = 15) -> str:
    try:
        r = subprocess.run(f"adb -s {device_id} {cmd}",
                           shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def get_online_devices() -> list:
    r = subprocess.run("adb devices", shell=True, capture_output=True, text=True)
    devices = []
    for line in r.stdout.strip().split("\n")[1:]:
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices


# ═══════════════════════════════════════════════════════════════
# Core VPN Setup
# ═══════════════════════════════════════════════════════════════

def setup_vpn(device_id: str, uri: str, cfg: dict):
    """Complete VPN setup: delete old → import new → per-app → connect → verify."""
    import uiautomator2 as u2
    d = u2.connect(device_id)
    short = device_id[:12]

    print(f"\n{'='*60}")
    print(f"  设备: {device_id}")
    print(f"  配置: {cfg['remark']} ({cfg['server']}:{cfg['port']})")
    print(f"{'='*60}")

    # ── Step 1: 停止 VPN 服务 ──
    print(f"\n[1/6] 停止 VPN 服务...")
    adb(device_id, f"shell am force-stop {V2RAYNG}")
    time.sleep(2)

    # ── Step 2: 删除所有旧配置 ──
    print(f"[2/6] 删除所有旧配置...")
    d.app_start(V2RAYNG)
    time.sleep(3)

    _delete_all_configs(d, short)

    # ── Step 3: 导入新配置 ──
    print(f"[3/6] 导入新配置...")
    _import_config(d, cfg, short)

    # ── Step 4: 设置分应用代理 ──
    print(f"[4/6] 设置分应用代理 (仅 TikTok)...")
    _setup_per_app_proxy(d, short)

    # ── Step 5: 启动 VPN ──
    print(f"[5/6] 启动 VPN 连接...")
    _start_vpn(d, device_id, short)

    # ── Step 6: 验证连接 ──
    print(f"[6/6] 验证 VPN 连接...")
    time.sleep(5)
    _verify_ip(d, device_id, cfg, short)

    _save_config(uri, cfg)
    print(f"\n  [{short}] 配置完成 ✓")


def _delete_all_configs(d, short: str):
    """Delete all existing V2RayNG configs via More Options menu."""
    configs_exist = d(resourceId="com.v2ray.ang:id/tv_name")
    if not configs_exist.exists(timeout=2):
        print(f"  [{short}] 无旧配置，跳过删除")
        return

    more = d(description="More options")
    if more.exists(timeout=3):
        more.click()
        time.sleep(1)

        del_all = d(text="Delete config")
        if not del_all.exists(timeout=1):
            del_all = d(text="删除配置")
        if del_all.exists(timeout=2):
            del_all.click()
            time.sleep(1)

            _confirm_dialog(d)
            time.sleep(1)

            remaining = d(resourceId="com.v2ray.ang:id/tv_name")
            if not remaining.exists(timeout=2):
                print(f"  [{short}] 已通过菜单批量删除所有配置 ✓")
                return
            else:
                print(f"  [{short}] 菜单删除后仍有残留，逐个清理...")
        else:
            d.press("back")
            time.sleep(0.5)

    count = 0
    while count < 30:
        item_del = d(resourceId="com.v2ray.ang:id/layout_remove")
        if not item_del.exists(timeout=2):
            break
        item_del.click()
        time.sleep(0.5)
        _confirm_dialog(d)
        time.sleep(0.5)
        count += 1

    print(f"  [{short}] 已删除 {count} 条残留配置 ✓")


def _confirm_dialog(d):
    """Click the confirm button in a dialog."""
    for text in ["OK", "确定", "Yes", "确认", "删除", "Delete"]:
        btn = d(text=text)
        if btn.exists(timeout=1):
            btn.click()
            return True
    return False


def _import_config(d, cfg: dict, short: str):
    """Import a new V2Ray config by filling the manual form."""
    add_btn = d(description="Add config")
    if not add_btn.exists(timeout=3):
        print(f"  [{short}] 找不到 Add config 按钮")
        return

    add_btn.click()
    time.sleep(1.5)

    protocol = cfg["protocol"].upper()
    options = [
        f"Type manually[{protocol}]",
        f"Type manually [{protocol}]",
        f"手动输入[{protocol}]",
        protocol,
    ]

    found = False
    for text in options:
        el = d(textContains=text)
        if el.exists(timeout=1):
            el.click()
            found = True
            time.sleep(2)
            break

    if not found:
        all_texts = d(className="android.widget.TextView")
        for i in range(all_texts.count):
            try:
                t = all_texts[i].get_text()
                if cfg["protocol"].lower() in t.lower():
                    all_texts[i].click()
                    found = True
                    time.sleep(2)
                    break
            except Exception:
                pass

    if not found:
        print(f"  [{short}] 找不到 {protocol} 手动配置选项")
        return

    time.sleep(1)
    edits = d(className="android.widget.EditText")
    edit_count = edits.count
    print(f"  [{short}] 找到 {edit_count} 个输入框")

    values = [cfg["remark"], cfg["server"], cfg["port"], cfg["uuid"]]
    labels = ["备注", "服务器", "端口", "UUID"]

    for i, (val, label) in enumerate(zip(values, labels)):
        if i < edit_count:
            try:
                edits[i].clear_text()
                time.sleep(0.2)
                edits[i].set_text(val)
                time.sleep(0.3)
                print(f"  [{short}] {label}: {val[:30]} ✓")
            except Exception as e:
                print(f"  [{short}] {label}: 填写失败 - {e}")

    if cfg.get("encryption") and edit_count > 4:
        try:
            edits[4].clear_text()
            edits[4].set_text(cfg["encryption"])
            print(f"  [{short}] 加密: {cfg['encryption']} ✓")
        except Exception:
            pass

    if cfg.get("transport") == "ws":
        print(f"  [{short}] 配置 WebSocket 传输...")
        d.swipe(0.5, 0.8, 0.5, 0.3)
        time.sleep(1)

        spinners = d(className="android.widget.Spinner")
        for i in range(spinners.count):
            try:
                txt = spinners[i].info.get("text", "")
                content_desc = spinners[i].info.get("contentDescription", "")
                child_texts = []
                try:
                    for c in range(spinners[i].child_count):
                        ct = spinners[i].child(instance=c).get_text()
                        if ct:
                            child_texts.append(ct)
                except Exception:
                    pass

                if "tcp" in txt.lower() or "tcp" in str(child_texts).lower() or "network" in content_desc.lower():
                    spinners[i].click()
                    time.sleep(1)
                    ws_el = d(text="ws")
                    if ws_el.exists(timeout=2):
                        ws_el.click()
                        time.sleep(1)
                        print(f"  [{short}] 传输: ws ✓")
                    break
            except Exception:
                pass

        if cfg.get("ws_path"):
            new_edits = d(className="android.widget.EditText")
            for i in range(new_edits.count):
                try:
                    t = new_edits[i].get_text()
                    if t in ("", "/") or i >= edit_count:
                        new_edits[i].clear_text()
                        new_edits[i].set_text(cfg["ws_path"])
                        print(f"  [{short}] ws path: {cfg['ws_path']} ✓")
                        break
                except Exception:
                    pass

    d.swipe(0.5, 0.3, 0.5, 0.8)
    time.sleep(0.5)

    saved = False
    for desc_text in ["save", "Save", "done", "Done", "确认"]:
        btn = d(description=desc_text)
        if btn.exists(timeout=1):
            btn.click()
            saved = True
            time.sleep(2)
            break

    if not saved:
        for res in ["save", "action_save", "menu_save"]:
            btn = d(resourceIdMatches=f".*{res}.*")
            if btn.exists(timeout=1):
                btn.click()
                saved = True
                time.sleep(2)
                break

    if not saved:
        checkmark = d(text="✓")
        if checkmark.exists(timeout=1):
            checkmark.click()
            saved = True
            time.sleep(2)

    if not saved:
        d.press("back")
        time.sleep(1)
        for text in ["OK", "确定", "Yes", "Save"]:
            btn = d(text=text)
            if btn.exists(timeout=1):
                btn.click()
                time.sleep(1)
                break

    verify = d(text=cfg["remark"])
    if verify.exists(timeout=3):
        print(f"  [{short}] 配置已保存 ✓")
    else:
        print(f"  [{short}] 配置可能已保存 (无法确认)")


def _setup_per_app_proxy(d, short: str):
    """Configure per-app proxy to only route TikTok through VPN."""
    main_activity = d.app_current().get("activity", "")
    if "PerApp" not in main_activity:
        if "ServerActivity" in main_activity or "EditActivity" in main_activity:
            d.press("back")
            time.sleep(1)

        drawer = d(description="Open navigation drawer")
        if drawer.exists(timeout=3):
            drawer.click()
            time.sleep(1.5)
        else:
            d.swipe(0.05, 0.5, 0.8, 0.5)
            time.sleep(1.5)

        perapp = d(resourceId="com.v2ray.ang:id/per_app_proxy_settings")
        if perapp.exists(timeout=3):
            perapp.click()
            time.sleep(2)
        else:
            print(f"  [{short}] 找不到分应用代理选项")
            return

    switch = d(resourceId="com.v2ray.ang:id/switch_per_app_proxy")
    if switch.exists(timeout=3):
        is_on = switch.info.get("checked", False)
        if not is_on:
            switch.click()
            time.sleep(1)
            print(f"  [{short}] 分应用代理: 已启用 ✓")
        else:
            print(f"  [{short}] 分应用代理: 已是启用状态")

    bypass_switch = d(resourceId="com.v2ray.ang:id/switch_bypass_apps")
    if bypass_switch.exists(timeout=2):
        bypass_on = bypass_switch.info.get("checked", False)
        if bypass_on:
            bypass_switch.click()
            time.sleep(0.5)
            print(f"  [{short}] Bypass 模式: 已关闭 ✓ (仅选中的APP走VPN)")

    print(f"  [{short}] 等待应用列表加载...")
    for _ in range(15):
        progress = d(resourceId="com.v2ray.ang:id/progress_bar")
        if not progress.exists(timeout=1):
            break
        time.sleep(1)
    time.sleep(2)

    for app_name in ["TikTok"]:
        _search_and_check_app(d, short, app_name)

    d.press("back")
    time.sleep(1)
    d.press("back")
    time.sleep(1)


def _search_and_check_app(d, short: str, app_name: str):
    """Search for an app in per-app list and check its checkbox."""
    search = d(description="Search")
    if not search.exists(timeout=1):
        search = d(resourceId="com.v2ray.ang:id/search_button")
    if not search.exists(timeout=2):
        print(f"  [{short}] {app_name}: 搜索按钮未找到")
        return

    search.click()
    time.sleep(1.5)

    search_input = d(resourceId="com.v2ray.ang:id/search_src_text")
    if not search_input.exists(timeout=2):
        search_input = d(className="android.widget.AutoCompleteTextView")
    if not search_input.exists(timeout=2):
        search_input = d(className="android.widget.EditText")

    if not search_input.exists(timeout=2):
        print(f"  [{short}] {app_name}: 搜索输入框未找到")
        return

    search_input.set_text(app_name)
    time.sleep(2)

    checkbox = d(resourceId="com.v2ray.ang:id/check_box")
    if checkbox.exists(timeout=3):
        is_checked = checkbox.info.get("checked", False)
        if not is_checked:
            checkbox.click()
            time.sleep(0.5)
            print(f"  [{short}] {app_name}: 已勾选 ✓")
        else:
            print(f"  [{short}] {app_name}: 已是勾选状态 ✓")
    else:
        print(f"  [{short}] {app_name}: 未在列表中找到")

    close_btn = d(resourceId="com.v2ray.ang:id/search_close_btn")
    if close_btn.exists(timeout=1):
        close_btn.click()
        time.sleep(0.5)

    d.press("back")
    time.sleep(0.5)


def _start_vpn(d, device_id: str, short: str):
    """Start VPN connection."""
    main_check = d(resourceId="com.v2ray.ang:id/fab")
    if not main_check.exists(timeout=3):
        d.app_start(V2RAYNG)
        time.sleep(2)

    fab = d(resourceId="com.v2ray.ang:id/fab")
    if fab.exists(timeout=3):
        state = d(resourceId="com.v2ray.ang:id/tv_test_state")
        if state.exists(timeout=2):
            state_text = state.get_text()
            if "connected" in state_text.lower() and "not" not in state_text.lower():
                print(f"  [{short}] VPN 已在运行中")
                return

        fab.click()
        time.sleep(2)

        for text in ["OK", "确定", "Allow", "允许"]:
            btn = d(text=text)
            if btn.exists(timeout=2):
                btn.click()
                time.sleep(1)
                break

        print(f"  [{short}] VPN 连接已启动 ✓")
    else:
        print(f"  [{short}] 找不到连接按钮")


def _verify_ip(d, device_id: str, cfg: dict, short: str):
    """Verify VPN is working."""
    import uiautomator2 as u2

    tun = adb(device_id, "shell ip addr show tun0 2>/dev/null")
    has_tun = "tun0" in tun

    state = d(resourceId="com.v2ray.ang:id/tv_test_state")
    state_text = state.get_text() if state.exists(timeout=2) else ""

    notif = adb(device_id, "shell dumpsys notification | grep 'v2rayNG'")
    has_notif = "v2rayNG" in notif

    if has_tun:
        print(f"  [{short}] tun0 隧道: 已建立 ✓")
    else:
        print(f"  [{short}] tun0 隧道: 未建立")

    if has_notif:
        print(f"  [{short}] V2RayNG 通知: 运行中 ✓")
    else:
        print(f"  [{short}] V2RayNG 通知: 未检测到")

    if state_text:
        print(f"  [{short}] 连接状态: {state_text}")

    if not has_tun and not has_notif:
        print(f"  [{short}] ⚠ VPN 未能连接")
        ping = adb(device_id, "shell ping -c 2 -W 3 8.8.8.8")
        if "bytes from" in ping:
            print(f"  [{short}] 基础网络正常 ✓ (VPN节点可能失效)")
        else:
            print(f"  [{short}] 基础网络也不通，检查WiFi/SIM")
        return

    per_app_on = _is_per_app_enabled(d)
    if per_app_on:
        print(f"  [{short}] 分应用代理已启用 → shell curl 结果为本地IP (非VPN)")
        print(f"  [{short}] 仅 TikTok 流量走VPN，IP验证需通过TikTok自身确认")
        ping = adb(device_id, "shell ping -c 1 -W 3 8.8.8.8")
        if "bytes from" in ping:
            print(f"  [{short}] 基础网络正常 ✓")
        else:
            print(f"  [{short}] ⚠ 网络不通，VPN可能阻断所有流量")
            print(f"  [{short}] 正在停止 VPN...")
            adb(device_id, f"shell am force-stop {V2RAYNG}")
            time.sleep(2)
    else:
        ip_raw = adb(device_id,
                     "shell curl -s --max-time 10 http://ip-api.com/json/?fields=query,country,countryCode,city",
                     timeout=15)
        try:
            data = json.loads(ip_raw)
            ip = data.get("query", "?")
            country = data.get("country", "?")
            code = data.get("countryCode", "?")
            city = data.get("city", "?")
            print(f"  [{short}] 公网 IP: {ip} ({city}, {country})")
        except Exception:
            print(f"  [{short}] IP 检测失败")


def _is_per_app_enabled(d) -> bool:
    """Check if per-app proxy is enabled without navigating away."""
    try:
        switch = d(resourceId="com.v2ray.ang:id/switch_per_app_proxy")
        if switch.exists(timeout=1):
            return switch.info.get("checked", False)
    except Exception:
        pass
    return True


def _save_config(uri: str, cfg: dict):
    """Save current VPN config to file."""
    config_path = Path(__file__).parent.parent / "config" / "vpn_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "uri": uri,
        "protocol": cfg.get("protocol", ""),
        "server": cfg.get("server", ""),
        "port": cfg.get("port", ""),
        "remark": cfg.get("remark", ""),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  python scripts/vpn_setup.py <二维码图片或URI> [设备ID]")
        print("  python scripts/vpn_setup.py <二维码图片或URI> --all")
        sys.exit(1)

    input_arg = sys.argv[1]
    target = sys.argv[2] if len(sys.argv) > 2 else None

    if os.path.isfile(input_arg):
        print(f"解码二维码: {input_arg}")
        uri = decode_qr(input_arg)
    else:
        uri = input_arg

    cfg = parse_uri(uri)
    print(f"协议: {cfg['protocol'].upper()}")
    print(f"服务器: {cfg['server']}:{cfg['port']}")
    print(f"备注: {cfg['remark']}")

    if target == "--all":
        devices = get_online_devices()
    elif target:
        devices = [target]
    else:
        devices = get_online_devices()
        if not devices:
            print("ERROR: 没有在线设备")
            sys.exit(1)
        print(f"自动选择设备: {devices[0]}")

    print(f"目标: {len(devices)} 台设备")

    for did in devices:
        try:
            setup_vpn(did, uri, cfg)
        except Exception as e:
            print(f"\n  [{did[:12]}] 配置失败: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  全部完成")
    print(f"{'='*60}")
