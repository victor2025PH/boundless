# -*- coding: utf-8 -*-
"""
VPN Manager — V2RayNG 自动配置与管理。

核心职责:
  1. 从二维码/URI 解析 VPN 配置
  2. 自动删除旧配置 → 导入新配置
  3. 设置分应用代理 (仅指定 APP 走 VPN)
  4. 启动/停止 VPN 连接
  5. 验证连接状态

集成:
  - API 端点: /vpn/setup, /vpn/status, /vpn/stop
  - warmup 前自动检查 VPN 状态
  - 与 geo_check 配合验证 IP
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.host.device_registry import config_file
from src.utils.subprocess_text import run_shell

log = logging.getLogger(__name__)

V2RAYNG_PKG = "com.v2ray.ang"
TIKTOK_PKG = "com.ss.android.ugc.trill"
TIKTOK_ALT = "com.zhiliaoapp.musically"

DEFAULT_VPN_APPS = [TIKTOK_PKG, TIKTOK_ALT]

_CONFIG_FILE = config_file("vpn_config.json")
_lock = threading.Lock()


@dataclass
class VPNConfig:
    protocol: str = ""
    server: str = ""
    port: str = ""
    uuid: str = ""
    remark: str = ""
    encryption: str = "none"
    security: str = "none"
    transport: str = "tcp"
    ws_path: str = "/"
    ws_host: str = ""
    sni: str = ""
    uri: str = ""


@dataclass
class VPNStatus:
    device_id: str = ""
    connected: bool = False
    has_tun: bool = False
    has_notification: bool = False
    per_app_enabled: bool = False
    config_name: str = ""
    error: str = ""


def decode_qr(image_path: str) -> str:
    """Decode a V2Ray QR code image and return the URI."""
    import cv2

    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"Cannot read image: {image_path}")

    det = cv2.QRCodeDetector()
    data, _, _ = det.detectAndDecode(img)
    if data:
        return data.strip()

    import numpy as np
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
    data, _, _ = det.detectAndDecode(bw)
    if data:
        return data.strip()

    try:
        from pyzbar.pyzbar import decode as zbar_decode
        results = zbar_decode(img)
        if results:
            return results[0].data.decode().strip()
    except ImportError:
        pass

    raise RuntimeError("QR decode failed")


def parse_uri(uri: str) -> VPNConfig:
    """Parse a V2Ray/Proxy URI into a VPNConfig.

    Supported protocols:
      vless://, vmess://, trojan://, ss://   — V2RayNG app-based VPN
      socks5://, socks5h://                  — SOCKS5 proxy (router-based)
      http://, https://                      — HTTP proxy (router-based)
    """
    uri = uri.strip()

    if uri.startswith("vless://"):
        return _parse_vless(uri)
    elif uri.startswith("vmess://"):
        return _parse_vmess(uri)
    elif uri.startswith("trojan://"):
        return _parse_trojan(uri)
    elif uri.startswith("ss://"):
        return _parse_ss(uri)
    elif uri.startswith(("socks5://", "socks5h://")):
        return _parse_socks5(uri)
    elif uri.startswith(("http://", "https://")):
        return _parse_http_proxy(uri)

    raise ValueError(f"Unsupported protocol: {uri[:20]}")


def build_socks5_uri(host: str, port: int, username: str = "", password: str = "",
                     label: str = "") -> str:
    """Build a socks5:// URI from components. Used for router-based proxy setup."""
    import base64
    if username and password:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        uri = f"socks5://{credentials}@{host}:{port}"
    else:
        uri = f"socks5://{host}:{port}"
    if label:
        uri += f"#{urllib.parse.quote(label)}"
    return uri


def build_http_proxy_uri(host: str, port: int, username: str = "", password: str = "",
                         label: str = "") -> str:
    """Build an http:// proxy URI from components."""
    if username and password:
        u = urllib.parse.quote(username, safe="")
        p = urllib.parse.quote(password, safe="")
        uri = f"http://{u}:{p}@{host}:{port}"
    else:
        uri = f"http://{host}:{port}"
    if label:
        uri += f"#{urllib.parse.quote(label)}"
    return uri


def _parse_vless(uri: str) -> VPNConfig:
    p = urllib.parse.urlparse(uri)
    params = dict(urllib.parse.parse_qsl(p.query))
    return VPNConfig(
        protocol="vless",
        uuid=p.username or "",
        server=p.hostname or "",
        port=str(p.port or 443),
        remark=urllib.parse.unquote(p.fragment) if p.fragment else "VLESS",
        encryption=params.get("encryption", "none"),
        security=params.get("security", "none"),
        transport=params.get("type", "tcp"),
        ws_path=urllib.parse.unquote(params.get("path", "/")),
        ws_host=params.get("host", ""),
        sni=params.get("sni", ""),
        uri=uri,
    )


def _parse_vmess(uri: str) -> VPNConfig:
    import base64
    payload = uri[8:]
    pad = 4 - len(payload) % 4
    if pad != 4:
        payload += "=" * pad
    cfg = json.loads(base64.b64decode(payload).decode())
    return VPNConfig(
        protocol="vmess",
        server=cfg.get("add", ""),
        port=str(cfg.get("port", "")),
        uuid=cfg.get("id", ""),
        remark=cfg.get("ps", "VMess"),
        transport=cfg.get("net", "tcp"),
        ws_path=cfg.get("path", "/"),
        uri=uri,
    )


def _parse_trojan(uri: str) -> VPNConfig:
    p = urllib.parse.urlparse(uri)
    params = dict(urllib.parse.parse_qsl(p.query))
    return VPNConfig(
        protocol="trojan",
        uuid=p.username or "",
        server=p.hostname or "",
        port=str(p.port or 443),
        remark=urllib.parse.unquote(p.fragment) if p.fragment else "Trojan",
        sni=params.get("sni", ""),
        uri=uri,
    )


def _parse_ss(uri: str) -> VPNConfig:
    return VPNConfig(protocol="ss", uri=uri, remark="Shadowsocks")


def _parse_socks5(uri: str) -> VPNConfig:
    """Parse socks5:// or socks5h:// proxy URI.

    Formats supported:
      socks5://host:port
      socks5://user:pass@host:port
      socks5://BASE64(user:pass)@host:port   (922S5 / Proxy-Seller style)
    """
    import base64 as _b64
    p = urllib.parse.urlparse(uri)
    host = p.hostname or ""
    port = str(p.port or 1080)
    remark = urllib.parse.unquote(p.fragment) if p.fragment else f"SOCKS5-{host}"

    username, password = "", ""
    raw_user = p.username or ""
    raw_pass = p.password or ""

    # Try base64 decode (some providers encode credentials as base64)
    if raw_user and not raw_pass:
        try:
            pad = 4 - len(raw_user) % 4
            decoded = _b64.b64decode(raw_user + "=" * (pad % 4)).decode("utf-8")
            if ":" in decoded:
                username, password = decoded.split(":", 1)
        except Exception:
            username = urllib.parse.unquote(raw_user)
    else:
        username = urllib.parse.unquote(raw_user)
        password = urllib.parse.unquote(raw_pass)

    return VPNConfig(
        protocol="socks5",
        server=host,
        port=port,
        uuid=username,          # reuse uuid field for username
        encryption=password,    # reuse encryption field for password
        remark=remark,
        uri=uri,
    )


def _parse_http_proxy(uri: str) -> VPNConfig:
    """Parse http:// or https:// proxy URI."""
    p = urllib.parse.urlparse(uri)
    remark = urllib.parse.unquote(p.fragment) if p.fragment else f"HTTP-{p.hostname}"
    return VPNConfig(
        protocol="http",
        server=p.hostname or "",
        port=str(p.port or 8080),
        uuid=urllib.parse.unquote(p.username or ""),
        encryption=urllib.parse.unquote(p.password or ""),
        remark=remark,
        uri=uri,
    )


# ═══════════════════════════════════════════════════════════════
# Core operations
# ═══════════════════════════════════════════════════════════════

def _adb(device_id: str, cmd: str, timeout: int = 15) -> str:
    try:
        r = run_shell(
            f"adb -s {device_id} {cmd}",
            capture_output=True,
            timeout=timeout,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def setup_vpn(device_id: str, config: VPNConfig,
              vpn_apps: Optional[List[str]] = None) -> VPNStatus:
    """
    Complete VPN setup: stop → import via intent → start → verify.

    优先用 Intent 导入（秒级，不依赖 UI 坐标），失败时降级到 UI 自动化。
    """
    if vpn_apps is None:
        vpn_apps = DEFAULT_VPN_APPS

    short = device_id[:12]
    status = VPNStatus(device_id=device_id, config_name=config.remark)

    log.info("[VPN] %s: Starting setup for %s (%s:%s)",
             short, config.remark, config.server, config.port)

    _adb(device_id, "shell settings put system accelerometer_rotation 0")
    _adb(device_id, "shell settings put system user_rotation 0")
    _adb(device_id, f"shell am force-stop {V2RAYNG_PKG}")
    time.sleep(1)

    # 方法1: Intent 导入（秒级，推荐）
    if config.uri and _import_via_intent(device_id, config.uri):
        log.info("[VPN] %s: Intent 导入成功", short)
        time.sleep(2)

        # 启动 VPN 连接
        _start_vpn_adb(device_id)
        time.sleep(3)

        status = check_vpn_status(device_id)
        status.config_name = config.remark
        _save_config_file(config)
        _emit_vpn_event("vpn.configured", device_id, config)
        return status

    # 方法2: 降级到 UI 自动化（需要 uiautomator2）
    log.info("[VPN] %s: Intent 导入失败，降级到 UI 自动化", short)
    try:
        import uiautomator2 as u2
        d = u2.connect(device_id)
    except Exception as e:
        status.error = f"u2 connect failed: {e}"
        return status

    d.app_start(V2RAYNG_PKG)
    time.sleep(3)

    _delete_all_configs(d, short)
    _import_config(d, config, short)
    _configure_per_app(d, short, vpn_apps)
    _start_service(d, device_id, short)

    time.sleep(5)
    status = check_vpn_status(device_id, d)
    status.config_name = config.remark

    _save_config_file(config)
    _emit_vpn_event("vpn.configured", device_id, config)

    return status


def stop_vpn(device_id: str) -> VPNStatus:
    """Stop V2RayNG VPN service."""
    _adb(device_id, f"shell am force-stop {V2RAYNG_PKG}")
    time.sleep(2)
    _emit_vpn_event("vpn.stopped", device_id)
    return VPNStatus(device_id=device_id, connected=False)


def check_vpn_status(device_id: str, d=None) -> VPNStatus:
    """Check current VPN connection status.

    检测策略（按优先级）:
    1. tun0 接口存在且 UP → 已连接（最可靠）
    2. VPN 网络连接检测 → 已连接
    注意: V2RayNG 的通知在未连接时也存在（前台服务），不能作为连接判据。
    """
    status = VPNStatus(device_id=device_id)

    # 方法1: 检查 tun0 接口（最可靠的方法）
    tun = _adb(device_id, 'shell "ip addr show tun0 2>/dev/null"')
    if not tun:
        tun = _adb(device_id, "shell ip addr show tun0")
    status.has_tun = "tun0" in tun and ("UP" in tun or "inet" in tun)

    # 方法2: 检查 VPN 网络连接（Android connectivity）
    netstats = _adb(device_id, 'shell "cat /proc/net/if_inet6 2>/dev/null"')
    if not netstats:
        netstats = _adb(device_id, "shell cat /proc/net/if_inet6")
    has_vpn_iface = "tun0" in netstats or "tun" in netstats

    # V2RayNG 通知仅用于确认 app 在运行（不用于判断连接状态）
    notif = _adb(device_id, 'shell "dumpsys notification | grep -i v2ray"')
    if not notif:
        all_notif = _adb(device_id, "shell dumpsys notification --noredact", timeout=10)
        notif = "v2ray" if "v2ray" in all_notif.lower() else ""
    status.has_notification = "v2ray" in notif.lower()

    # 连接判断: 只以 tun0 接口为准（通知不作为连接依据）
    status.connected = status.has_tun or has_vpn_iface

    if d is not None:
        try:
            state = d(resourceId="com.v2ray.ang:id/tv_test_state")
            if state.exists(timeout=2):
                txt = state.get_text()
                if "connected" in txt.lower() and "not" not in txt.lower():
                    status.connected = True
                    status.config_name = txt

            switch = d(resourceId="com.v2ray.ang:id/switch_per_app_proxy")
            if switch.exists(timeout=1):
                status.per_app_enabled = switch.info.get("checked", False)
            else:
                status.per_app_enabled = True
        except Exception:
            pass

    return status


def is_vpn_healthy(device_id: str) -> bool:
    """Quick health check — is VPN connected?"""
    s = check_vpn_status(device_id)
    return s.connected


# ═══════════════════════════════════════════════════════════════
# UI automation helpers
# ═══════════════════════════════════════════════════════════════

def _delete_all_configs(d, short: str):
    """Delete all V2RayNG configs."""
    if not d(resourceId="com.v2ray.ang:id/tv_name").exists(timeout=2):
        log.info("[VPN] %s: No existing configs", short)
        return

    more = d(description="More options")
    if more.exists(timeout=3):
        more.click()
        time.sleep(1)
        del_menu = d(text="Delete config")
        if not del_menu.exists(timeout=1):
            del_menu = d(text="删除配置")
        if del_menu.exists(timeout=2):
            del_menu.click()
            time.sleep(1)
            _confirm_dialog(d)
            time.sleep(1)
            if not d(resourceId="com.v2ray.ang:id/tv_name").exists(timeout=2):
                log.info("[VPN] %s: All configs deleted via menu", short)
                return
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

    log.info("[VPN] %s: Deleted %d configs", short, count)


def _import_config(d, cfg: VPNConfig, short: str):
    """Import config via manual form."""
    add_btn = d(description="Add config")
    if not add_btn.exists(timeout=3):
        log.error("[VPN] %s: Add config button not found", short)
        return

    add_btn.click()
    time.sleep(1.5)

    protocol = cfg.protocol.upper()
    found = False
    for text in [f"Type manually[{protocol}]", f"Type manually [{protocol}]",
                 f"手动输入[{protocol}]", protocol]:
        el = d(textContains=text)
        if el.exists(timeout=1):
            el.click()
            found = True
            time.sleep(2)
            break

    if not found:
        log.error("[VPN] %s: Protocol option %s not found", short, protocol)
        return

    edits = d(className="android.widget.EditText")
    values = [cfg.remark, cfg.server, cfg.port, cfg.uuid]
    for i, val in enumerate(values):
        if i < edits.count:
            try:
                edits[i].clear_text()
                time.sleep(0.2)
                edits[i].set_text(val)
                time.sleep(0.3)
            except Exception:
                pass

    if cfg.encryption and edits.count > 4:
        try:
            edits[4].clear_text()
            edits[4].set_text(cfg.encryption)
        except Exception:
            pass

    if cfg.transport == "ws":
        d.swipe(0.5, 0.8, 0.5, 0.3)
        time.sleep(1)
        spinners = d(className="android.widget.Spinner")
        for i in range(spinners.count):
            try:
                txt = spinners[i].info.get("text", "")
                if "tcp" in txt.lower():
                    spinners[i].click()
                    time.sleep(1)
                    ws_el = d(text="ws")
                    if ws_el.exists(timeout=2):
                        ws_el.click()
                        time.sleep(1)
                    break
            except Exception:
                pass

        if cfg.ws_path:
            new_edits = d(className="android.widget.EditText")
            for i in range(new_edits.count):
                try:
                    t = new_edits[i].get_text()
                    if t in ("", "/"):
                        new_edits[i].clear_text()
                        new_edits[i].set_text(cfg.ws_path)
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
        d.press("back")
        time.sleep(1)
        _confirm_dialog(d)
        time.sleep(1)

    if d(text=cfg.remark).exists(timeout=3):
        log.info("[VPN] %s: Config '%s' saved", short, cfg.remark)
    else:
        log.warning("[VPN] %s: Config save uncertain", short)


def _configure_per_app(d, short: str, apps: List[str]):
    """Enable per-app proxy and select only specified apps."""
    activity = d.app_current().get("activity", "")
    if "PerApp" not in activity:
        if "ServerActivity" in activity or "EditActivity" in activity:
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
            log.error("[VPN] %s: Per-app settings not found", short)
            return

    switch = d(resourceId="com.v2ray.ang:id/switch_per_app_proxy")
    if switch.exists(timeout=3):
        if not switch.info.get("checked", False):
            switch.click()
            time.sleep(1)
            log.info("[VPN] %s: Per-app proxy enabled", short)

    bypass = d(resourceId="com.v2ray.ang:id/switch_bypass_apps")
    if bypass.exists(timeout=2) and bypass.info.get("checked", False):
        bypass.click()
        time.sleep(0.5)

    for _ in range(15):
        if not d(resourceId="com.v2ray.ang:id/progress_bar").exists(timeout=1):
            break
        time.sleep(1)
    time.sleep(2)

    app_names = ["TikTok"]
    for name in app_names:
        _search_and_check(d, short, name)

    d.press("back")
    time.sleep(1)
    d.press("back")
    time.sleep(1)


def _search_and_check(d, short: str, app_name: str):
    """Search for an app in per-app list and check it."""
    search = d(description="Search")
    if not search.exists(timeout=1):
        search = d(resourceId="com.v2ray.ang:id/search_button")
    if not search.exists(timeout=2):
        log.warning("[VPN] %s: Search button not found for %s", short, app_name)
        return

    search.click()
    time.sleep(1.5)

    search_input = d(resourceId="com.v2ray.ang:id/search_src_text")
    if not search_input.exists(timeout=2):
        search_input = d(className="android.widget.AutoCompleteTextView")
    if not search_input.exists(timeout=2):
        search_input = d(className="android.widget.EditText")

    if not search_input.exists(timeout=2):
        log.warning("[VPN] %s: Search input not found for %s", short, app_name)
        return

    search_input.set_text(app_name)
    time.sleep(2)

    checkbox = d(resourceId="com.v2ray.ang:id/check_box")
    if checkbox.exists(timeout=3):
        if not checkbox.info.get("checked", False):
            checkbox.click()
            time.sleep(0.5)
            log.info("[VPN] %s: %s checked", short, app_name)
        else:
            log.info("[VPN] %s: %s already checked", short, app_name)
    else:
        log.warning("[VPN] %s: %s not found in list", short, app_name)

    close = d(resourceId="com.v2ray.ang:id/search_close_btn")
    if close.exists(timeout=1):
        close.click()
        time.sleep(0.5)

    d.press("back")
    time.sleep(0.5)


def _start_service(d, device_id: str, short: str):
    """Start V2RayNG VPN service."""
    main_check = d(resourceId="com.v2ray.ang:id/fab")
    if not main_check.exists(timeout=3):
        d.app_start(V2RAYNG_PKG)
        time.sleep(2)

    fab = d(resourceId="com.v2ray.ang:id/fab")
    if not fab.exists(timeout=3):
        log.error("[VPN] %s: Start button not found", short)
        return

    state = d(resourceId="com.v2ray.ang:id/tv_test_state")
    if state.exists(timeout=2):
        txt = state.get_text()
        if "connected" in txt.lower() and "not" not in txt.lower():
            log.info("[VPN] %s: Already connected", short)
            return

    fab.click()
    time.sleep(2)

    _confirm_dialog(d)
    time.sleep(1)

    log.info("[VPN] %s: Service started", short)


def _confirm_dialog(d) -> bool:
    """点掉常见系统/V2RayNG 弹窗（多语言、多控件）。可连续多轮直至无新弹窗。"""
    any_click = False
    for _ in range(4):
        hit = False
        for text in (
            "OK", "确定", "Yes", "确认", "Allow", "允许", "Delete", "删除",
            "CONNECT", "连接", "Continue", "继续", "Got it", "知道了",
            "Accept", "同意", "Start", "开始", "I agree", "SKIP", "跳过",
        ):
            btn = d(text=text)
            if btn.exists(timeout=0.35):
                btn.click()
                hit = True
                any_click = True
                time.sleep(0.35)
                break
        if not hit:
            for pat in ("Allow", "OK", "确定", "允许", "连接"):
                btn = d(textContains=pat)
                if btn.exists(timeout=0.28):
                    btn.click()
                    hit = True
                    any_click = True
                    time.sleep(0.3)
                    break
        if not hit and d(resourceId="android:id/button1").exists(timeout=0.3):
            d(resourceId="android:id/button1").click()
            hit = True
            any_click = True
            time.sleep(0.3)
        if not hit and d(resourceId="android:id/button2").exists(timeout=0.25):
            t = d(resourceId="android:id/button2").info.get("text", "")
            if t and any(x in t for x in ("OK", "确定", "连接", "允许")):
                d(resourceId="android:id/button2").click()
                hit = True
                any_click = True
                time.sleep(0.3)
        if not hit:
            break
    return any_click


def _dismiss_v2ray_popups(d, short: str) -> None:
    """进入 v2rayNG 后快速清掉更新提示、权限等浮层。"""
    for _ in range(10):
        if not _confirm_dialog(d):
            break
        time.sleep(0.22)
    log.debug("[VPN] %s: popups dismissed", short)


def _open_v2ray_drawer(d) -> bool:
    """打开侧栏：多种 content-desc / 边缘滑动。"""
    candidates = (
        lambda: d(description="Open navigation drawer"),
        lambda: d(descriptionContains="Open navigation drawer"),
        lambda: d(descriptionContains="navigation drawer"),
        lambda: d(description="Navigate up"),
        lambda: d(descriptionContains="Navigate up"),
    )
    for getter in candidates:
        try:
            el = getter()
            if el.exists(timeout=1.0):
                el.click()
                time.sleep(0.85)
                return True
        except Exception:
            continue
    try:
        w, h = d.window_size()
        for x0 in (6, 20, 36, 52):
            d.swipe(x0, h // 2, int(w * 0.58), h // 2, 0.32)
            time.sleep(0.55)
            if d(scrollable=True).exists(timeout=0.6):
                return True
            if d(textContains="Settings").exists(timeout=0.35):
                return True
            if d(textContains="设置").exists(timeout=0.35):
                return True
    except Exception:
        pass
    return False


def _scroll_until_click(
    d,
    labels: List[str],
    *,
    max_swipes: int = 28,
) -> bool:
    """在可滚动区域内反复上滑，直到点到含某文案的控件。"""
    w, h = d.window_size()
    for _ in range(max_swipes):
        for label in labels:
            for sel in (
                d(text=label),
                d(textContains=label),
            ):
                try:
                    if sel.exists(timeout=0.38):
                        sel.click()
                        return True
                except Exception:
                    continue
        try:
            scr = d(scrollable=True)
            if scr.exists(timeout=0.35):
                scr.scroll.to(text=labels[0])
                time.sleep(0.45)
                for label in labels:
                    if d(textContains=label).exists(timeout=0.4):
                        d(textContains=label).click()
                        return True
        except Exception:
            pass
        d.swipe(w * 0.48, h * 0.74, w * 0.48, h * 0.30, 0.24)
        time.sleep(0.32)
    return False


def _click_import_rulesets_menu(d, w: int, h: int) -> bool:
    """路由页右上角菜单 → 导入预定义规则集。"""
    for desc in ("More options", "更多", "More", "Menu"):
        el = d(descriptionContains=desc)
        if el.exists(timeout=1.2):
            el.click()
            time.sleep(0.9)
            break
    else:
        for px, py in ((0.96, 0.12), (0.94, 0.10), (0.92, 0.16)):
            d.click(int(w * px), int(h * py))
            time.sleep(0.85)

    menu_labels = (
        "Import predefined rulesets",
        "Import predefined ruleset",
        "导入预定义规则集",
        "导入预定义规则",
        "Predefined rulesets",
        "Import rulesets",
        "导入规则集",
        "Rulesets",
    )
    for t in menu_labels:
        if d(text=t).exists(timeout=1.2):
            d(text=t).click()
            return True
        if d(textContains=t).exists(timeout=0.8):
            d(textContains=t).click()
            return True
    return False


def _click_global_option(d) -> bool:
    for t in ("Global", "全局", "GLOBAL", "Global routing"):
        if d(text=t).exists(timeout=1.5):
            d(text=t).click()
            return True
        if d(textContains=t).exists(timeout=0.9):
            d(textContains=t).click()
            return True
    return False


def _set_global_routing_inner_from_drawer(d, device_id: str, short: str) -> bool:
    """已从侧栏：找「路由」入口 → 导入 Global 规则。"""
    w, h = d.window_size()
    routing_labels = (
        "Routing Settings",
        "Routing settings",
        "路由设置",
        "Routing",
        "Policy routing",
        "策略路由",
        "路由",
        "Geofence",
    )
    if not _scroll_until_click(d, list(routing_labels)):
        log.warning("[VPN] %s: drawer 内未点到路由项，尝试 XPath", short)
        try:
            d.xpath(
                '//*[contains(@text,"Routing") or contains(@text,"路由")]'
            ).click()
            time.sleep(1.8)
        except Exception:
            try:
                d.xpath('//*[contains(@text,"路由")]').click()
                time.sleep(1.8)
            except Exception:
                return False
    else:
        time.sleep(1.8)

    if not _click_import_rulesets_menu(d, w, h):
        log.error("[VPN] %s: Import rulesets 菜单未找到", short)
        return False
    time.sleep(1.8)

    if not _click_global_option(d):
        log.error("[VPN] %s: Global 选项未找到", short)
        return False
    time.sleep(1.0)
    d.press("back")
    time.sleep(0.35)
    d.press("back")
    time.sleep(0.45)
    log.info("[VPN] %s: Global routing set (drawer 路径)", short)
    return True


def _set_global_routing_via_settings(d, device_id: str, short: str) -> bool:
    """备用：侧栏 → 设置 → 路由 / Routing。"""
    if not _open_v2ray_drawer(d):
        return False
    time.sleep(0.4)
    settings_labels = ("Settings", "设置", "Preferences", "偏好设置")
    opened = False
    for lab in settings_labels:
        if d(text=lab).exists(timeout=0.9):
            d(text=lab).click()
            opened = True
            break
        if d(textContains=lab).exists(timeout=0.7):
            d(textContains=lab).click()
            opened = True
            break
    if not opened:
        return False
    time.sleep(1.6)

    inner = (
        "Routing", "路由", "Routing settings", "路由设置",
        "Policy routing", "策略路由",
    )
    if not _scroll_until_click(d, list(inner), max_swipes=16):
        return False
    time.sleep(1.6)
    w, h = d.window_size()
    if not _click_import_rulesets_menu(d, w, h):
        return False
    time.sleep(1.6)
    if not _click_global_option(d):
        return False
    time.sleep(0.9)
    d.press("back")
    time.sleep(0.35)
    d.press("back")
    time.sleep(0.4)
    log.info("[VPN] %s: Global routing set (Settings 路径)", short)
    return True


def _dismiss_vpn_permission_taps(d, device_id: str, short: str) -> None:
    """MIUI/原生 VPN 授权框：文字 + 多组比例坐标点击。"""
    for _ in range(5):
        if _confirm_dialog(d):
            time.sleep(0.4)
            continue
        break
    try:
        w, h = d.window_size()
        for fx, fy in (
            (0.52, 0.88), (0.50, 0.84), (0.48, 0.80), (0.55, 0.76),
            (0.45, 0.90), (0.58, 0.82),
        ):
            d.click(int(w * fx), int(h * fy))
            time.sleep(0.35)
    except Exception as e:
        log.debug("[VPN] %s: permission tap: %s", short, e)


# ═══════════════════════════════════════════════════════════════
# 全局 VPN 配置流程（导入→全局路由→启动→验证）
# ═══════════════════════════════════════════════════════════════

def _import_via_intent(device_id: str, uri: str) -> bool:
    """通过 v2rayNG 原生 Intent 导入配置。

    流程: force-stop → 清除旧配置 → 导入新配置
    确保新配置成为活跃配置（不残留旧配置干扰）。
    """
    short = device_id[:12]
    try:
        # Step 0: 彻底停止 V2RayNG
        subprocess.run(
            ["adb", "-s", device_id, "shell",
             "am", "force-stop", V2RAYNG_PKG],
            capture_output=True, timeout=10)
        time.sleep(0.5)

        # Step 1: 清除旧的服务器配置（删除 angXXX.json 配置文件）
        # V2RayNG 配置存在 /data/data/com.v2ray.ang/files/ 下
        subprocess.run(
            ["adb", "-s", device_id, "shell",
             "run-as", V2RAYNG_PKG,
             "sh", "-c", "rm -f files/ang_*.json files/ANG_CONFIG 2>/dev/null"],
            capture_output=True, timeout=10)
        # 备用方式：通过 am broadcast 清除配置（V2RayNG 内置支持）
        subprocess.run(
            ["adb", "-s", device_id, "shell",
             "am", "broadcast", "-a", "com.v2ray.ang.action.REMOVE_ALL_CONFIG",
             "-n", f"{V2RAYNG_PKG}/.receiver.WidgetProvider"],
            capture_output=True, timeout=10)
        time.sleep(0.3)

        # Step 2: 导入新配置（SEND intent）
        r = subprocess.run(
            ["adb", "-s", device_id, "shell",
             "am", "start",
             "-a", "android.intent.action.SEND",
             "-t", "text/plain",
             "--es", "android.intent.extra.TEXT", uri,
             "-p", V2RAYNG_PKG],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        ok = r.returncode == 0 and "Error" not in r.stderr
        if ok:
            log.info("[VPN] %s: Intent 导入成功（已清除旧配置）", short)
            return True

        # Step 3: 降级到 v2rayng:// scheme
        import urllib.parse
        encoded = urllib.parse.quote(uri, safe='')
        r2 = subprocess.run(
            ["adb", "-s", device_id, "shell",
             "am", "start", "-a", "android.intent.action.VIEW",
             "-d", f"v2rayng://install-config?url={encoded}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        ok2 = r2.returncode == 0
        log.info("[VPN] %s: Scheme 导入 %s", short, "OK" if ok2 else "FAIL")
        return ok2
    except Exception as e:
        log.error("[VPN] %s: 导入失败: %s", short, e)
        return False


def reconnect_vpn_silent(device_id: str) -> bool:
    """静默重连 VPN（不弹 V2RayNG UI）。

    策略:
    1. Widget toggle 重连（秒级，不弹 UI）
    2. 失败则尝试配置池中的备份服务器
    """
    short = device_id[:12]
    status = check_vpn_status(device_id)
    if status.connected:
        return True

    # 方法1: Widget toggle（最快）
    log.info("[VPN] %s: 静默重连 (toggle)", short)
    _toggle_vpn(device_id)
    time.sleep(5)
    if check_vpn_status(device_id).connected:
        log.info("[VPN] %s: toggle 重连成功", short)
        return True

    # 重试 toggle
    _toggle_vpn(device_id)
    time.sleep(5)
    if check_vpn_status(device_id).connected:
        log.info("[VPN] %s: toggle 重试成功", short)
        return True

    # 方法2: 从配置池尝试备份服务器
    try:
        import json as _j
        pool_file = config_file("vpn_pool.json")
        if pool_file.exists():
            pool = _j.loads(pool_file.read_text(encoding="utf-8"))
            configs = pool.get("configs", [])
            if len(configs) > 1:
                # 轮换到下一个配置
                current_assign = pool.get("assignments", {}).get(device_id, "")
                other_configs = [c for c in configs if c["id"] != current_assign]
                if other_configs:
                    backup = other_configs[0]
                    log.info("[VPN] %s: 尝试备份配置 %s", short, backup.get("label", ""))
                    if _import_via_intent(device_id, backup["uri"]):
                        time.sleep(2)
                        _start_vpn_adb(device_id)
                        time.sleep(5)
                        if check_vpn_status(device_id).connected:
                            log.info("[VPN] %s: 备份配置连接成功", short)
                            return True
    except Exception as e:
        log.debug("[VPN] %s: 备份切换失败: %s", short, e)

    log.warning("[VPN] %s: 所有重连方式失败", short)
    return False


def _toggle_vpn(device_id: str) -> bool:
    """通过 Widget Broadcast 切换 VPN 开关（启动/停止，无需 UI 交互）。"""
    try:
        r = subprocess.run(
            ["adb", "-s", device_id, "shell",
             "am", "broadcast",
             "-a", "com.v2ray.ang.action.widget.click",
             "-n", f"{V2RAYNG_PKG}/.receiver.WidgetProvider"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        ok = r.returncode == 0
        log.info("[VPN] %s: Widget toggle %s", device_id[:12],
                 "OK" if ok else "FAIL")
        return ok
    except Exception as e:
        log.error("[VPN] %s: Widget toggle error: %s", device_id[:12], e)
        return False


def _start_vpn_adb(device_id: str) -> bool:
    """启动 VPN 连接。先检查状态，未连接则通过 Widget 切换。"""
    short = device_id[:12]
    # 检查当前是否已连接
    status = check_vpn_status(device_id)
    if status.connected:
        log.info("[VPN] %s: 已连接，跳过启动", short)
        return True

    # 通过 Widget broadcast 启动（一键，无 UI）
    _toggle_vpn(device_id)
    time.sleep(3)

    # 处理首次 VPN 权限确认（多分辨率比例点击，避免固定 530,880 失效）
    def _tap_pct(fx: float, fy: float) -> None:
        raw = _adb(device_id, "shell wm size", timeout=5)
        w_px, h_px = 1080, 2400
        try:
            import re as _re
            m = _re.search(r"(\d+)\s*x\s*(\d+)", raw.replace("Physical size:", ""))
            if m:
                w_px, h_px = int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        tx = max(10, int(w_px * fx))
        ty = max(10, int(h_px * fy))
        subprocess.run(
            ["adb", "-s", device_id, "shell", "input", "tap", str(tx), str(ty)],
            capture_output=True,
            timeout=5,
        )

    for fx, fy in ((0.52, 0.88), (0.50, 0.84), (0.48, 0.80), (0.55, 0.76)):
        _tap_pct(fx, fy)
        time.sleep(0.35)
    time.sleep(0.6)

    log.info("[VPN] %s: VPN 启动命令已发送", short)
    return True


def _stop_vpn_adb(device_id: str):
    """停止 VPN 连接。通过 Widget 切换或强制停止。"""
    status = check_vpn_status(device_id)
    if status.connected:
        _toggle_vpn(device_id)
        time.sleep(1)
    else:
        subprocess.run(
            ["adb", "-s", device_id, "shell",
             "am", "force-stop", V2RAYNG_PKG],
            capture_output=True, timeout=10,
        )


def _set_global_routing(d, device_id: str) -> bool:
    """通过 UI 自动化设置 V2RayNG 全局路由（侧栏主路径 + 设置备用路径）。"""
    short = device_id[:12]
    _dismiss_v2ray_popups(d, short)

    if not _open_v2ray_drawer(d):
        log.warning("[VPN] %s: 侧栏首次打开失败，尝试左缘滑动", short)
        try:
            w, h = d.window_size()
            d.swipe(10, h // 2, int(w * 0.58), h // 2, 0.3)
            time.sleep(0.75)
        except Exception:
            pass

    if _set_global_routing_inner_from_drawer(d, device_id, short):
        return True

    log.warning("[VPN] %s: 路由主路径失败，尝试 设置 → 路由", short)
    for _ in range(3):
        d.press("back")
        time.sleep(0.35)
    try:
        d.app_start(V2RAYNG_PKG)
        time.sleep(2.0)
    except Exception:
        pass
    _dismiss_v2ray_popups(d, short)

    if _set_global_routing_via_settings(d, device_id, short):
        return True

    log.error("[VPN] %s: 全局路由设置失败（侧栏与设置路径均已尝试）", short)
    return False


def _verify_vpn_connection(device_id: str) -> dict:
    """验证 VPN 连接状态：tun0 接口 + dumpsys VPN VALIDATED。"""
    result = {"tun0": False, "validated": False, "connected": False, "detail": ""}

    tun = _adb(device_id, "shell ip addr show tun0 2>/dev/null", timeout=10)
    result["tun0"] = "tun0:" in tun and "UP" in tun.upper()

    dump = _adb(device_id, "shell dumpsys connectivity", timeout=30)
    key = "InterfaceName: tun0"
    if key in dump:
        i = dump.find(key)
        start = dump.rfind("NetworkAgentInfo{", 0, i)
        if start == -1:
            start = max(0, i - 4000)
        next_block = dump.find("\n  NetworkAgentInfo{", i + 10)
        chunk = dump[start:] if next_block == -1 else dump[start:next_block]
        vpn_markers = ("ni{VPN", "VpnTransportInfo", "VPN CONNECTED")
        if any(m in chunk for m in vpn_markers):
            if "VALIDATED" in chunk or "IS_VALIDATED" in chunk:
                result["validated"] = True
                result["detail"] = "VPN(tun0) VALIDATED"

    result["connected"] = result["tun0"] and result["validated"]
    if not result["tun0"]:
        result["detail"] = "tun0 接口未建立"
    elif not result["validated"]:
        result["detail"] = "VPN 未通过系统验证(VALIDATED)"

    return result


def setup_global_vpn(device_id: str, config: VPNConfig) -> VPNStatus:
    """
    全局 VPN 配置完整流程：
    1. 强制停止 V2RayNG
    2. 通过 Intent 导入配置 URI
    3. 启动 V2RayNG 并设置全局路由
    4. 启动 VPN 连接
    5. 验证连接状态
    """
    import uiautomator2 as u2

    short = device_id[:12]
    status = VPNStatus(device_id=device_id, config_name=config.remark)

    log.info("[VPN] %s: === 开始全局 VPN 配置 (%s) ===",
             short, config.remark)

    _adb(device_id, "shell settings put system accelerometer_rotation 0")
    _adb(device_id, "shell settings put system user_rotation 0")
    _adb(device_id, "shell settings put global private_dns_mode off")

    _adb(device_id, f"shell am force-stop {V2RAYNG_PKG}")
    time.sleep(2)

    try:
        d = u2.connect(device_id)
    except Exception as e:
        status.error = f"uiautomator2 连接失败: {e}"
        return status

    d.app_start(V2RAYNG_PKG)
    time.sleep(3)

    _dismiss_v2ray_popups(d, short)

    _delete_all_configs(d, short)
    time.sleep(1)

    if not _import_via_intent(device_id, config.uri):
        status.error = "配置导入失败"
        return status
    time.sleep(3)

    d.app_start(V2RAYNG_PKG)
    time.sleep(2)
    _dismiss_v2ray_popups(d, short)

    if not _set_global_routing(d, device_id):
        status.error = "全局路由设置失败"
        return status

    d.app_start(V2RAYNG_PKG)
    time.sleep(1)
    _start_service(d, device_id, short)
    time.sleep(4)

    _dismiss_vpn_permission_taps(d, device_id, short)
    _dismiss_v2ray_popups(d, short)
    time.sleep(4)

    verify = _verify_vpn_connection(device_id)
    status.has_tun = verify["tun0"]
    status.connected = verify["connected"]

    if not status.connected:
        log.warning("[VPN] %s: 首次验证未通过，等待重试...", short)
        time.sleep(5)
        verify = _verify_vpn_connection(device_id)
        status.has_tun = verify["tun0"]
        status.connected = verify["connected"]

    if status.connected:
        log.info("[VPN] %s: 全局 VPN 配置成功 — %s", short, verify["detail"])
    else:
        status.error = f"连接验证失败: {verify['detail']}"
        log.error("[VPN] %s: %s", short, status.error)

    _save_config_file(config)
    _emit_vpn_event("vpn.global_configured", device_id, config)

    return status


# ═══════════════════════════════════════════════════════════════
# Config persistence
# ═══════════════════════════════════════════════════════════════

def _save_config_file(config: VPNConfig):
    with _lock:
        _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "uri": config.uri,
            "protocol": config.protocol,
            "server": config.server,
            "port": config.port,
            "remark": config.remark,
            "transport": config.transport,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _CONFIG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        log.info("[VPN] Config saved to %s", _CONFIG_FILE)


def get_saved_config() -> Optional[dict]:
    if _CONFIG_FILE.exists():
        return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
    return None


def _emit_vpn_event(event_type: str, device_id: str,
                    config: Optional[VPNConfig] = None):
    try:
        from ..workflow.event_bus import get_event_bus
        bus = get_event_bus()
        payload = {"device_id": device_id}
        if config:
            payload["remark"] = config.remark
            payload["server"] = config.server
        bus.emit_simple(event_type, source="vpn_manager", **payload)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════

_instance: Optional["VPNManager"] = None
_inst_lock = threading.Lock()


class VPNManager:
    """Stateful VPN manager that tracks per-device VPN status."""

    def __init__(self):
        self._statuses: Dict[str, VPNStatus] = {}
        self._config: Optional[VPNConfig] = None
        saved = get_saved_config()
        if saved and saved.get("uri"):
            try:
                self._config = parse_uri(saved["uri"])
            except Exception:
                pass

    @property
    def current_config(self) -> Optional[VPNConfig]:
        return self._config

    def setup(self, device_id: str, uri_or_qr: str,
              vpn_apps: Optional[List[str]] = None) -> VPNStatus:
        """Setup VPN on a device from URI or QR code image path."""
        import os
        if os.path.isfile(uri_or_qr):
            uri = decode_qr(uri_or_qr)
        else:
            uri = uri_or_qr

        config = parse_uri(uri)
        self._config = config
        status = setup_vpn(device_id, config, vpn_apps)
        self._statuses[device_id] = status
        return status

    def setup_all(self, uri_or_qr: str,
                  device_ids: Optional[List[str]] = None) -> List[VPNStatus]:
        """Setup VPN on all connected devices."""
        import os
        if os.path.isfile(uri_or_qr):
            uri = decode_qr(uri_or_qr)
        else:
            uri = uri_or_qr

        config = parse_uri(uri)
        self._config = config

        if not device_ids:
            out = subprocess.run(
                "adb devices",
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            device_ids = []
            for line in out.stdout.strip().split("\n")[1:]:
                parts = line.strip().split("\t")
                if len(parts) == 2 and parts[1] == "device":
                    device_ids.append(parts[0])

        results = []
        for did in device_ids:
            try:
                s = setup_vpn(did, config)
                self._statuses[did] = s
                results.append(s)
            except Exception as e:
                results.append(VPNStatus(device_id=did, error=str(e)))

        return results

    def stop(self, device_id: str) -> VPNStatus:
        s = stop_vpn(device_id)
        self._statuses[device_id] = s
        return s

    def status(self, device_id: str) -> VPNStatus:
        s = check_vpn_status(device_id)
        self._statuses[device_id] = s
        return s

    def setup_global(self, device_id: str, uri_or_qr: str) -> VPNStatus:
        """全局模式配置 VPN：解码→导入→设全局路由→启动→验证。"""
        import os
        if os.path.isfile(uri_or_qr):
            uri = decode_qr(uri_or_qr)
        else:
            uri = uri_or_qr

        config = parse_uri(uri)
        self._config = config
        status = setup_global_vpn(device_id, config)
        self._statuses[device_id] = status
        return status

    def setup_global_all(self, uri_or_qr: str,
                         device_ids: Optional[List[str]] = None) -> List[VPNStatus]:
        """在所有设备上执行全局 VPN 配置。"""
        import os
        if os.path.isfile(uri_or_qr):
            uri = decode_qr(uri_or_qr)
        else:
            uri = uri_or_qr

        config = parse_uri(uri)
        self._config = config

        if not device_ids:
            out = subprocess.run(
                "adb devices",
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            device_ids = []
            for line in out.stdout.strip().split("\n")[1:]:
                parts = line.strip().split("\t")
                if len(parts) == 2 and parts[1] == "device":
                    device_ids.append(parts[0])

        results = []
        for did in device_ids:
            try:
                s = setup_global_vpn(did, config)
                self._statuses[did] = s
                results.append(s)
            except Exception as e:
                results.append(VPNStatus(device_id=did, error=str(e)))
        return results

    def ensure_connected(self, device_id: str) -> bool:
        """Ensure VPN is connected; restart if needed."""
        s = check_vpn_status(device_id)
        if s.connected:
            return True

        if self._config:
            log.info("[VPN] %s: Reconnecting...", device_id[:12])
            s = setup_vpn(device_id, self._config)
            self._statuses[device_id] = s
            return s.connected

        log.warning("[VPN] %s: No config to reconnect", device_id[:12])
        return False


def get_vpn_manager() -> VPNManager:
    global _instance
    if _instance is None:
        with _inst_lock:
            if _instance is None:
                _instance = VPNManager()
    return _instance
