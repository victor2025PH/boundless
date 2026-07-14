# -*- coding: utf-8 -*-
"""
Router Manager — GL.iNet 软路由统一管理。

职责:
  1. GL.iNet 路由器注册与状态监控
  2. 自动生成 Clash 配置文件并推送到路由器
  3. 代理账号与路由器的绑定管理
  4. 路由器出口 IP 验证（每5分钟）
  5. 路由器离线 → Telegram 告警

架构:
  每台 GL.iNet 路由器承载 N 台手机的 WiFi 接入
  路由器运行 OpenClash（Clash TUN 模式）透明代理
  手机流量: Phone → Router WiFi → Clash → 代理IP → 目标网站

GL.iNet API:
  路由器提供 REST API: http://{router_ip}/rpc
  需要先登录获取 token，然后用 token 调用管理接口
  OpenClash 配置通过 SSH 或文件上传接口推送

改进（2026-04-11）:
  - _glinet_call() 加3次重试 + 指数退避，防止网络抖动误报
  - 代理账号连通性预检（SOCKS5 TCP 握手）
  - push_clash_config() 推送前备份旧配置，推送失败自动回滚
  - deploy_router() 加部署后出口IP验证（30s等待Clash重启）
  - COUNTRY_GPS 扩展至 15 国（新增 brazil/india/japan/korea/australia/
    canada/spain/netherlands/singapore/indonesia）
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request
import urllib.parse
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

_ROUTERS_FILE = config_file("routers.json")
_POOL_FILE = config_file("vpn_pool.json")
_BACKUP_DIR = config_file("clash_backups")

# GL.iNet API 超时（单次请求）
_API_TIMEOUT = 8

# RPC 重试配置
_RPC_RETRIES = 2          # 最多额外重试次数（总共3次尝试）
_RPC_RETRY_DELAY = 1.0    # 首次重试延迟（秒），指数退避


# ═══════════════════════════════════════════════
# 国家 → GPS 坐标（15国，覆盖主要引流市场）
# ═══════════════════════════════════════════════

COUNTRY_GPS: Dict[str, Dict[str, object]] = {
    # ── 北美 ──
    "us": {
        "cities": [
            {"city": "New York",     "lat": 40.7128,  "lon": -74.0060,  "tz": "America/New_York"},
            {"city": "Los Angeles",  "lat": 34.0522,  "lon": -118.2437, "tz": "America/Los_Angeles"},
            {"city": "Chicago",      "lat": 41.8781,  "lon": -87.6298,  "tz": "America/Chicago"},
            {"city": "Houston",      "lat": 29.7604,  "lon": -95.3698,  "tz": "America/Chicago"},
            {"city": "Phoenix",      "lat": 33.4484,  "lon": -112.0740, "tz": "America/Phoenix"},
            {"city": "Philadelphia", "lat": 39.9526,  "lon": -75.1652,  "tz": "America/New_York"},
            {"city": "San Antonio",  "lat": 29.4241,  "lon": -98.4936,  "tz": "America/Chicago"},
            {"city": "Dallas",       "lat": 32.7767,  "lon": -96.7970,  "tz": "America/Chicago"},
            {"city": "Miami",        "lat": 25.7617,  "lon": -80.1918,  "tz": "America/New_York"},
            {"city": "Seattle",      "lat": 47.6062,  "lon": -122.3321, "tz": "America/Los_Angeles"},
        ],
        "language": "en-US",
        "country_code": "US",
    },
    "canada": {
        "cities": [
            {"city": "Toronto",   "lat": 43.6532, "lon": -79.3832, "tz": "America/Toronto"},
            {"city": "Vancouver", "lat": 49.2827, "lon": -123.1207, "tz": "America/Vancouver"},
            {"city": "Montreal",  "lat": 45.5017, "lon": -73.5673, "tz": "America/Toronto"},
            {"city": "Calgary",   "lat": 51.0447, "lon": -114.0719, "tz": "America/Edmonton"},
        ],
        "language": "en-CA",
        "country_code": "CA",
    },
    # ── 西欧 ──
    "uk": {
        "cities": [
            {"city": "London",     "lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
            {"city": "Manchester", "lat": 53.4808, "lon": -2.2426, "tz": "Europe/London"},
            {"city": "Birmingham", "lat": 52.4862, "lon": -1.8904, "tz": "Europe/London"},
            {"city": "Glasgow",    "lat": 55.8642, "lon": -4.2518, "tz": "Europe/London"},
        ],
        "language": "en-GB",
        "country_code": "GB",
    },
    "germany": {
        "cities": [
            {"city": "Berlin",    "lat": 52.5200, "lon": 13.4050, "tz": "Europe/Berlin"},
            {"city": "Munich",    "lat": 48.1351, "lon": 11.5820, "tz": "Europe/Berlin"},
            {"city": "Hamburg",   "lat": 53.5753, "lon": 10.0153, "tz": "Europe/Berlin"},
            {"city": "Frankfurt", "lat": 50.1109, "lon": 8.6821,  "tz": "Europe/Berlin"},
            {"city": "Cologne",   "lat": 50.9333, "lon": 6.9500,  "tz": "Europe/Berlin"},
        ],
        "language": "de-DE",
        "country_code": "DE",
    },
    "france": {
        "cities": [
            {"city": "Paris",     "lat": 48.8566, "lon": 2.3522, "tz": "Europe/Paris"},
            {"city": "Lyon",      "lat": 45.7640, "lon": 4.8357, "tz": "Europe/Paris"},
            {"city": "Marseille", "lat": 43.2965, "lon": 5.3698, "tz": "Europe/Paris"},
            {"city": "Toulouse",  "lat": 43.6047, "lon": 1.4442, "tz": "Europe/Paris"},
        ],
        "language": "fr-FR",
        "country_code": "FR",
    },
    "italy": {
        "cities": [
            {"city": "Milan",   "lat": 45.4642, "lon": 9.1900,  "tz": "Europe/Rome"},
            {"city": "Rome",    "lat": 41.9028, "lon": 12.4964, "tz": "Europe/Rome"},
            {"city": "Naples",  "lat": 40.8518, "lon": 14.2681, "tz": "Europe/Rome"},
            {"city": "Turin",   "lat": 45.0703, "lon": 7.6869,  "tz": "Europe/Rome"},
            {"city": "Palermo", "lat": 38.1157, "lon": 13.3615, "tz": "Europe/Rome"},
        ],
        "language": "it-IT",
        "country_code": "IT",
    },
    "spain": {
        "cities": [
            {"city": "Madrid",    "lat": 40.4168, "lon": -3.7038, "tz": "Europe/Madrid"},
            {"city": "Barcelona", "lat": 41.3851, "lon": 2.1734,  "tz": "Europe/Madrid"},
            {"city": "Valencia",  "lat": 39.4699, "lon": -0.3763, "tz": "Europe/Madrid"},
            {"city": "Seville",   "lat": 37.3891, "lon": -5.9845, "tz": "Europe/Madrid"},
        ],
        "language": "es-ES",
        "country_code": "ES",
    },
    "netherlands": {
        "cities": [
            {"city": "Amsterdam", "lat": 52.3676, "lon": 4.9041,  "tz": "Europe/Amsterdam"},
            {"city": "Rotterdam", "lat": 51.9244, "lon": 4.4777,  "tz": "Europe/Amsterdam"},
            {"city": "The Hague", "lat": 52.0705, "lon": 4.3007,  "tz": "Europe/Amsterdam"},
        ],
        "language": "nl-NL",
        "country_code": "NL",
    },
    # ── 南美 ──
    "brazil": {
        "cities": [
            {"city": "São Paulo",     "lat": -23.5505, "lon": -46.6333, "tz": "America/Sao_Paulo"},
            {"city": "Rio de Janeiro","lat": -22.9068, "lon": -43.1729, "tz": "America/Sao_Paulo"},
            {"city": "Brasília",      "lat": -15.7801, "lon": -47.9292, "tz": "America/Sao_Paulo"},
            {"city": "Salvador",      "lat": -12.9714, "lon": -38.5014, "tz": "America/Bahia"},
            {"city": "Fortaleza",     "lat": -3.7319,  "lon": -38.5267, "tz": "America/Fortaleza"},
        ],
        "language": "pt-BR",
        "country_code": "BR",
    },
    # ── 南亚 ──
    "india": {
        "cities": [
            {"city": "Mumbai",    "lat": 19.0760, "lon": 72.8777,  "tz": "Asia/Kolkata"},
            {"city": "Delhi",     "lat": 28.7041, "lon": 77.1025,  "tz": "Asia/Kolkata"},
            {"city": "Bangalore", "lat": 12.9716, "lon": 77.5946,  "tz": "Asia/Kolkata"},
            {"city": "Hyderabad", "lat": 17.3850, "lon": 78.4867,  "tz": "Asia/Kolkata"},
            {"city": "Chennai",   "lat": 13.0827, "lon": 80.2707,  "tz": "Asia/Kolkata"},
        ],
        "language": "en-IN",
        "country_code": "IN",
    },
    # ── 东亚 ──
    "japan": {
        "cities": [
            {"city": "Tokyo",    "lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
            {"city": "Osaka",    "lat": 34.6937, "lon": 135.5023, "tz": "Asia/Tokyo"},
            {"city": "Yokohama", "lat": 35.4437, "lon": 139.6380, "tz": "Asia/Tokyo"},
            {"city": "Nagoya",   "lat": 35.1815, "lon": 136.9066, "tz": "Asia/Tokyo"},
            {"city": "Sapporo",  "lat": 43.0621, "lon": 141.3544, "tz": "Asia/Tokyo"},
        ],
        "language": "ja-JP",
        "country_code": "JP",
    },
    "korea": {
        "cities": [
            {"city": "Seoul",  "lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul"},
            {"city": "Busan",  "lat": 35.1796, "lon": 129.0756, "tz": "Asia/Seoul"},
            {"city": "Incheon","lat": 37.4563, "lon": 126.7052, "tz": "Asia/Seoul"},
            {"city": "Daegu",  "lat": 35.8714, "lon": 128.6014, "tz": "Asia/Seoul"},
        ],
        "language": "ko-KR",
        "country_code": "KR",
    },
    # ── 大洋洲 ──
    "australia": {
        "cities": [
            {"city": "Sydney",    "lat": -33.8688, "lon": 151.2093, "tz": "Australia/Sydney"},
            {"city": "Melbourne", "lat": -37.8136, "lon": 144.9631, "tz": "Australia/Melbourne"},
            {"city": "Brisbane",  "lat": -27.4698, "lon": 153.0251, "tz": "Australia/Brisbane"},
            {"city": "Perth",     "lat": -31.9505, "lon": 115.8605, "tz": "Australia/Perth"},
        ],
        "language": "en-AU",
        "country_code": "AU",
    },
    # ── 东南亚 ──
    "singapore": {
        "cities": [
            {"city": "Singapore", "lat": 1.3521, "lon": 103.8198, "tz": "Asia/Singapore"},
        ],
        "language": "en-SG",
        "country_code": "SG",
    },
    "indonesia": {
        "cities": [
            {"city": "Jakarta",   "lat": -6.2088,  "lon": 106.8456, "tz": "Asia/Jakarta"},
            {"city": "Surabaya",  "lat": -7.2575,  "lon": 112.7521, "tz": "Asia/Jakarta"},
            {"city": "Bali",      "lat": -8.3405,  "lon": 115.0920, "tz": "Asia/Makassar"},
            {"city": "Medan",     "lat": 3.5952,   "lon": 98.6722,  "tz": "Asia/Jakarta"},
        ],
        "language": "id-ID",
        "country_code": "ID",
    },
}


@dataclass
class RouterInfo:
    router_id: str          # 唯一ID，如 "router-01"
    name: str               # 显示名称，如 "美国组A"
    ip: str                 # 路由器 IP，如 "192.168.0.201"
    port: int = 80          # GL.iNet 管理端口
    password: str = ""      # 路由器管理密码
    country: str = ""       # 目标国家代码，如 "us"
    city: str = ""          # 目标城市
    proxy_ids: List[str] = field(default_factory=list)   # 分配的代理账号ID列表
    device_ids: List[str] = field(default_factory=list)  # 连接的手机列表
    online: bool = False
    current_exit_ip: str = ""
    last_check: float = 0.0
    clash_config_pushed: bool = False
    ssh_user: str = "root"
    ssh_port: int = 22
    notes: str = ""


@dataclass
class RouterStatus:
    router_id: str
    name: str
    online: bool
    exit_ip: str = ""
    exit_country: str = ""
    proxy_count: int = 0
    device_count: int = 0
    clash_running: bool = False
    error: str = ""


_routers_lock = threading.Lock()
_routers_cache: Dict[str, RouterInfo] = {}
_monitor_thread: Optional[threading.Thread] = None
_monitor_running = False


# ═══════════════════════════════════════════════
# 配置持久化
# ═══════════════════════════════════════════════

def _load_routers() -> Dict[str, RouterInfo]:
    if _ROUTERS_FILE.exists():
        try:
            raw = json.loads(_ROUTERS_FILE.read_text(encoding="utf-8"))
            return {k: RouterInfo(**v) for k, v in raw.items()}
        except Exception as e:
            log.warning("[Router] 加载路由器配置失败: %s", e)
    return {}


def _save_routers(routers: Dict[str, RouterInfo]):
    _ROUTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {k: asdict(v) for k, v in routers.items()}
    _ROUTERS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_pool() -> dict:
    if _POOL_FILE.exists():
        try:
            return json.loads(_POOL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"configs": [], "assignments": {}}


# ═══════════════════════════════════════════════
# GL.iNet API 交互
# ═══════════════════════════════════════════════

def _glinet_login(router: RouterInfo) -> Optional[str]:
    """登录 GL.iNet 路由器，返回 sid (session token)。"""
    import hashlib
    try:
        pwd_md5 = hashlib.md5(router.password.encode()).hexdigest()
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "method": "call",
            "params": ["00000000000000000000000000000000", "session", "login",
                       {"username": "root", "password": pwd_md5}]
        }).encode()
        req = urllib.request.Request(
            f"http://{router.ip}:{router.port}/rpc",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=_API_TIMEOUT)
        data = json.loads(resp.read().decode())
        sid = data.get("result", {}).get("sid", "")
        return sid if sid and sid != "00000000000000000000000000000000" else None
    except Exception as e:
        log.debug("[Router] %s 登录失败: %s", router.router_id, e)
        return None


def _glinet_call(router: RouterInfo, sid: str, subsystem: str, method: str,
                 params: dict = None, *, retries: int = _RPC_RETRIES) -> Optional[dict]:
    """调用 GL.iNet RPC 接口，带指数退避重试。

    改进：
    - 区分网络错误（URLError/TimeoutError）和业务错误
    - 网络错误触发重试（最多 retries 次），间隔 1s/2s 指数退避
    - 非网络错误（如 JSON 解析失败）直接返回 None，不重试
    - 防止网络抖动导致路由器误报离线

    Args:
        retries: 额外重试次数（默认2次，总共3次尝试）
    """
    last_error: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            payload = json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method": "call",
                "params": [sid, subsystem, method, params or {}]
            }).encode()
            req = urllib.request.Request(
                f"http://{router.ip}:{router.port}/rpc",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=_API_TIMEOUT)
            result = json.loads(resp.read().decode()).get("result", {})
            if attempt > 0:
                log.debug("[Router] %s RPC %s.%s 重试%d次后成功",
                          router.router_id, subsystem, method, attempt)
            return result

        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # 网络层错误 → 可重试
            last_error = e
            if attempt < retries:
                delay = _RPC_RETRY_DELAY * (2 ** attempt)  # 1s, 2s 指数退避
                log.debug("[Router] %s RPC %s.%s 网络错误（第%d次），%.1fs后重试: %s",
                          router.router_id, subsystem, method, attempt + 1, delay, e)
                time.sleep(delay)
            # else: 最后一次失败，走到循环外

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            # 业务层错误 → 不重试，直接返回
            log.debug("[Router] %s RPC %s.%s 响应解析失败: %s",
                      router.router_id, subsystem, method, e)
            return None

        except Exception as e:
            # 其他未知错误 → 不重试
            log.debug("[Router] %s RPC %s.%s 异常: %s",
                      router.router_id, subsystem, method, e)
            return None

    log.debug("[Router] %s RPC %s.%s 重试%d次均失败: %s",
              router.router_id, subsystem, method, retries, last_error)
    return None


def check_router_online(router: RouterInfo) -> bool:
    """快速检测路由器是否在线（HTTP ping）。"""
    try:
        req = urllib.request.Request(
            f"http://{router.ip}:{router.port}/",
            method="GET",
        )
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


def get_router_exit_ip(router: RouterInfo) -> str:
    """通过路由器获取当前出口 IP（路由器执行 curl）。

    策略：尝试多个 IP 查询服务，返回第一个成功的结果。
    """
    sid = _glinet_login(router)
    if not sid:
        return ""
    for api_url in ["https://api.ipify.org", "https://ip.sb", "https://ifconfig.me/ip"]:
        result = _glinet_call(router, sid, "system", "exec",
                              {"command": f"curl -s --max-time 5 {api_url}"})
        if result and result.get("stdout", "").strip():
            ip = result["stdout"].strip()
            if ip and "." in ip and len(ip) < 20:
                return ip
    return ""


# ═══════════════════════════════════════════════
# 代理连通性预检
# ═══════════════════════════════════════════════

def test_proxy_connection(acc: dict, timeout: int = 8) -> dict:
    """测试单个代理账号的 SOCKS5/HTTP 连通性（TCP 握手）。

    这是 deploy_router 的前置检查，确保代理账号可用再生成配置。
    只做 TCP 连接测试，不做完整的 SOCKS5 协议握手（速度快，足够判断连通性）。

    Returns:
        {"ok": bool, "host": str, "port": int, "latency_ms": float, "error": str}
    """
    host = acc.get("server", "")
    port = int(acc.get("port", 1080))

    if not host:
        return {"ok": False, "host": host, "port": port,
                "latency_ms": 0, "error": "代理账号缺少 server 字段"}

    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        latency_ms = round((time.time() - t0) * 1000, 1)
        log.debug("[Router] 代理 %s:%d 连通（%.0fms）", host, port, latency_ms)
        return {"ok": True, "host": host, "port": port, "latency_ms": latency_ms, "error": ""}
    except socket.timeout:
        return {"ok": False, "host": host, "port": port,
                "latency_ms": timeout * 1000, "error": f"连接超时（>{timeout}s）"}
    except ConnectionRefusedError:
        return {"ok": False, "host": host, "port": port,
                "latency_ms": 0, "error": "连接被拒绝（端口未开放）"}
    except OSError as e:
        return {"ok": False, "host": host, "port": port,
                "latency_ms": 0, "error": str(e)}


def test_all_proxy_connections(proxy_accounts: List[dict], timeout: int = 8) -> dict:
    """并发测试多个代理账号连通性。

    Returns:
        {"total": N, "ok": N, "failed": N, "results": [...], "all_ok": bool}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=min(10, len(proxy_accounts))) as ex:
        futs = {ex.submit(test_proxy_connection, acc, timeout): acc
                for acc in proxy_accounts}
        for f in as_completed(futs):
            try:
                results.append(f.result())
            except Exception as e:
                acc = futs[f]
                results.append({
                    "ok": False,
                    "host": acc.get("server", ""),
                    "port": acc.get("port", 0),
                    "latency_ms": 0,
                    "error": str(e),
                })

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "total": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
        "all_ok": ok_count == len(results),
    }


# ═══════════════════════════════════════════════
# Clash 配置生成
# ═══════════════════════════════════════════════

def generate_clash_config(router: RouterInfo, proxy_accounts: List[dict]) -> str:
    """为路由器生成 Clash 配置文件（YAML 格式）。

    路由器模式：TUN 透明代理，所有流量走代理。
    多个代理账号使用 load-balance 负载均衡，不同手机可获得不同 IP。
    """
    if not proxy_accounts:
        log.warning("[Router] %s 无代理账号，无法生成配置", router.router_id)
        return ""

    lines = [
        "# OpenClaw Auto-Generated Clash Config",
        f"# Router: {router.name} ({router.router_id})",
        f"# Country: {router.country}",
        f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "mixed-port: 7890",
        "allow-lan: true",
        "bind-address: '*'",
        "mode: rule",
        "log-level: warning",
        "ipv6: false",
        "",
        "# DNS 配置（防止 DNS 泄漏）",
        "dns:",
        "  enable: true",
        "  ipv6: false",
        "  listen: 0.0.0.0:53",
        "  enhanced-mode: fake-ip",
        "  fake-ip-range: 198.18.0.1/16",
        "  nameserver:",
        "    - 8.8.8.8",
        "    - 1.1.1.1",
        "  fallback:",
        "    - 8.8.4.4",
        "    - 1.0.0.1",
        "",
        "proxies:",
    ]

    proxy_names = []
    for i, acc in enumerate(proxy_accounts):
        proto = acc.get("protocol", "socks5").lower()
        name = acc.get("label", f"proxy-{i+1}")
        host = acc.get("server", "")
        port = int(acc.get("port", 1080))
        username = acc.get("username", "")
        password = _extract_password_from_pool(acc)

        proxy_names.append(name)

        if proto in ("socks5", "socks5h"):
            lines.append(f"  - name: \"{name}\"")
            lines.append(f"    type: socks5")
            lines.append(f"    server: {host}")
            lines.append(f"    port: {port}")
            if username:
                lines.append(f"    username: \"{username}\"")
            if password:
                lines.append(f"    password: \"{password}\"")
            lines.append(f"    udp: true")
        elif proto in ("http", "https"):
            lines.append(f"  - name: \"{name}\"")
            lines.append(f"    type: http")
            lines.append(f"    server: {host}")
            lines.append(f"    port: {port}")
            if username:
                lines.append(f"    username: \"{username}\"")
            if password:
                lines.append(f"    password: \"{password}\"")
        lines.append("")

    # 代理组：单账号用 select，多账号用 load-balance（sticky-sessions保证同设备IP稳定）
    lines.append("proxy-groups:")
    if len(proxy_names) == 1:
        lines.append(f"  - name: PROXY")
        lines.append(f"    type: select")
        lines.append(f"    proxies:")
        lines.append(f"      - {proxy_names[0]}")
    else:
        lines.append(f"  - name: PROXY")
        lines.append(f"    type: load-balance")
        lines.append(f"    strategy: sticky-sessions")
        lines.append(f"    proxies:")
        for pn in proxy_names:
            lines.append(f"      - {pn}")

    lines.append("")
    lines.append("  - name: DIRECT")
    lines.append("    type: select")
    lines.append("    proxies:")
    lines.append("      - DIRECT")
    lines.append("")

    # 路由规则：本地直连，其余全部走代理
    lines.extend([
        "rules:",
        "  - IP-CIDR,192.168.0.0/16,DIRECT",
        "  - IP-CIDR,10.0.0.0/8,DIRECT",
        "  - IP-CIDR,172.16.0.0/12,DIRECT",
        "  - IP-CIDR,127.0.0.0/8,DIRECT",
        "  - MATCH,PROXY",
    ])

    return "\n".join(lines)


def _extract_password_from_pool(acc: dict) -> str:
    """从代理账号信息中提取密码。"""
    if acc.get("password"):
        return acc["password"]
    if acc.get("encryption"):
        return acc["encryption"]
    uri = acc.get("uri", "")
    if uri and ("://" in uri):
        try:
            p = urllib.parse.urlparse(uri)
            if p.password:
                return urllib.parse.unquote(p.password)
            if p.username and not p.password:
                import base64
                raw = p.username
                pad = 4 - len(raw) % 4
                decoded = base64.b64decode(raw + "=" * (pad % 4)).decode("utf-8")
                if ":" in decoded:
                    return decoded.split(":", 1)[1]
        except Exception:
            pass
    return ""


# ═══════════════════════════════════════════════
# 路由器配置推送（含备份/回滚）
# ═══════════════════════════════════════════════

def _backup_clash_config(router: RouterInfo, sid: str) -> str:
    """读取路由器当前 Clash 配置并保存到本地备份。

    Returns:
        备份文件路径，或 "" 表示备份失败（不影响推送流程）。
    """
    try:
        result = _glinet_call(router, sid, "system", "exec",
                              {"command": "cat /etc/openclash/config.yaml 2>/dev/null"})
        if not result or not result.get("stdout", "").strip():
            return ""

        existing_yaml = result["stdout"]
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_path = _BACKUP_DIR / f"{router.router_id}_{ts}.yaml"
        backup_path.write_text(existing_yaml, encoding="utf-8")
        log.debug("[Router] %s 配置已备份到: %s", router.router_id, backup_path.name)
        return str(backup_path)
    except Exception as e:
        log.debug("[Router] %s 配置备份失败: %s（不影响推送）", router.router_id, e)
        return ""


def _restore_clash_config(router: RouterInfo, sid: str, backup_path: str) -> bool:
    """从本地备份文件恢复路由器 Clash 配置（用于回滚）。"""
    if not backup_path:
        return False
    try:
        backup_content = Path(backup_path).read_text(encoding="utf-8")
        result = _glinet_call(router, sid, "system", "exec", {
            "command": f"cat > /etc/openclash/config.yaml << 'CLASHEOF'\n{backup_content}\nCLASHEOF"
        })
        if result is not None:
            _glinet_call(router, sid, "system", "exec",
                         {"command": "/etc/init.d/openclash restart &"})
            log.info("[Router] %s 配置已从备份回滚: %s", router.router_id,
                     Path(backup_path).name)
            return True
    except Exception as e:
        log.error("[Router] %s 配置回滚失败: %s", router.router_id, e)
    return False


def push_clash_config(router: RouterInfo, clash_yaml: str) -> Tuple[bool, str]:
    """将 Clash 配置推送到 GL.iNet 路由器。

    改进（P1-3）：
    1. 推送前备份当前配置
    2. 推送失败时自动回滚（仅当有备份时）
    3. 返回 (success, backup_path) 元组，backup_path 供调用者验证后决定是否保留

    推送方式（按优先级）:
    1. GL.iNet RPC API 写入（主要）
    2. SSH 写入（备用）
    """
    if not clash_yaml:
        return False, ""

    backup_path = ""

    # 方式1：GL.iNet RPC API
    sid = _glinet_login(router)
    if sid:
        # 推送前备份
        backup_path = _backup_clash_config(router, sid)

        result = _glinet_call(router, sid, "system", "exec", {
            "command": f"mkdir -p /etc/openclash && cat > /etc/openclash/config.yaml << 'CLASHEOF'\n{clash_yaml}\nCLASHEOF"
        })
        if result is not None:
            # 重启 OpenClash 服务
            _glinet_call(router, sid, "system", "exec",
                         {"command": "/etc/init.d/openclash restart &"})
            log.info("[Router] %s Clash 配置已推送（API方式）", router.router_id)
            return True, backup_path
        else:
            # RPC 推送失败，尝试回滚
            if backup_path:
                log.warning("[Router] %s 配置推送失败，尝试回滚...", router.router_id)
                _restore_clash_config(router, sid, backup_path)
            return False, backup_path

    # 方式2：SSH 推送（备用，此路径暂不做备份）
    ok = _push_via_ssh(router, clash_yaml)
    return ok, ""


def _push_via_ssh(router: RouterInfo, clash_yaml: str) -> bool:
    """通过 SSH 推送 Clash 配置（需要系统 ssh/scp 命令）。"""
    try:
        import subprocess
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml",
                                         delete=False, encoding="utf-8") as f:
            f.write(clash_yaml)
            tmp_path = f.name

        try:
            scp_cmd = [
                "scp", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "-P", str(router.ssh_port),
                tmp_path,
                f"{router.ssh_user}@{router.ip}:/etc/openclash/config.yaml"
            ]
            r = subprocess.run(scp_cmd, capture_output=True, timeout=30)
            if r.returncode != 0:
                log.warning("[Router] %s SCP 失败: %s", router.router_id,
                            r.stderr.decode()[:200])
                return False

            ssh_cmd = [
                "ssh", "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=10",
                "-p", str(router.ssh_port),
                f"{router.ssh_user}@{router.ip}",
                "/etc/init.d/openclash restart"
            ]
            subprocess.run(ssh_cmd, capture_output=True, timeout=30)
            log.info("[Router] %s Clash 配置已推送（SSH方式）", router.router_id)
            return True
        finally:
            os.unlink(tmp_path)
    except Exception as e:
        log.error("[Router] %s SSH 推送失败: %s", router.router_id, e)
        return False


# ═══════════════════════════════════════════════
# RouterManager 单例
# ═══════════════════════════════════════════════

class RouterManager:
    """GL.iNet 路由器统一管理器（单例）。"""

    def __init__(self):
        with _routers_lock:
            global _routers_cache
            _routers_cache = _load_routers()

    # ── CRUD ──

    def add_router(self, info: RouterInfo) -> RouterInfo:
        with _routers_lock:
            _routers_cache[info.router_id] = info
            _save_routers(_routers_cache)
        log.info("[Router] 已添加路由器: %s (%s)", info.name, info.ip)
        return info

    def update_router(self, router_id: str, updates: dict) -> Optional[RouterInfo]:
        with _routers_lock:
            r = _routers_cache.get(router_id)
            if not r:
                return None
            for k, v in updates.items():
                if hasattr(r, k):
                    setattr(r, k, v)
            _save_routers(_routers_cache)
        return r

    def delete_router(self, router_id: str) -> bool:
        with _routers_lock:
            if router_id not in _routers_cache:
                return False
            del _routers_cache[router_id]
            _save_routers(_routers_cache)
        return True

    def get_router(self, router_id: str) -> Optional[RouterInfo]:
        return _routers_cache.get(router_id)

    def list_routers(self) -> List[RouterInfo]:
        return list(_routers_cache.values())

    # ── 代理分配 ──

    def assign_proxies(self, router_id: str, proxy_ids: List[str]) -> bool:
        return bool(self.update_router(router_id, {"proxy_ids": proxy_ids}))

    def assign_devices(self, router_id: str, device_ids: List[str]) -> bool:
        return bool(self.update_router(router_id, {"device_ids": device_ids}))

    # ── 配置部署（含预检 + 部署后验证）──

    def deploy_router(self, router_id: str, skip_proxy_check: bool = False) -> dict:
        """生成 Clash 配置并推送到路由器。

        改进（P1-3）：
        1. 部署前对所有代理账号做 TCP 连通性预检
        2. 推送后等待 Clash 重启（30秒），再验证出口IP
        3. 验证失败时报告但不回滚（Clash 正在生效中，IP可能有延迟）

        Args:
            skip_proxy_check: 跳过代理连通性预检（快速部署时使用）

        Returns:
            {ok, clash_yaml, pushed, proxy_check, exit_ip_verified, error}
        """
        router = self.get_router(router_id)
        if not router:
            return {"ok": False, "error": f"路由器 {router_id} 不存在"}

        # 获取分配的代理账号
        pool = _load_pool()
        proxy_accounts = [c for c in pool.get("configs", [])
                          if c["id"] in router.proxy_ids]
        if not proxy_accounts:
            return {"ok": False, "error": "未分配代理账号，请先分配代理"}

        result: dict = {
            "ok": False,
            "router_id": router_id,
            "proxy_count": len(proxy_accounts),
        }

        # ── Step 1: 代理连通性预检 ──
        proxy_check_result = None
        if not skip_proxy_check:
            log.info("[Router] %s 开始代理连通性预检（%d个账号）...",
                     router.name, len(proxy_accounts))
            proxy_check_result = test_all_proxy_connections(proxy_accounts)
            result["proxy_check"] = proxy_check_result

            if not proxy_check_result["all_ok"]:
                failed = proxy_check_result["failed"]
                total = proxy_check_result["total"]
                log.warning("[Router] %s %d/%d 代理账号不可达（仍继续部署）",
                            router.name, failed, total)
                # 不中止部署：部分代理失败时仍可用剩余的
                # 但若全部失败，则中止
                if proxy_check_result["ok"] == 0:
                    result["error"] = f"所有 {total} 个代理账号均不可达，部署中止"
                    return result

        # ── Step 2: 生成配置 ──
        clash_yaml = generate_clash_config(router, proxy_accounts)
        if not clash_yaml:
            result["error"] = "配置生成失败"
            return result
        result["clash_yaml"] = clash_yaml

        # ── Step 3: 推送配置（含备份/回滚）──
        pushed, backup_path = push_clash_config(router, clash_yaml)
        result["pushed"] = pushed
        result["backup_path"] = backup_path
        self.update_router(router_id, {"clash_config_pushed": pushed})

        if not pushed:
            result["error"] = "配置推送失败（已尝试回滚）"
            result["message"] = "配置推送失败，请手动上传或检查路由器连接"
            return result

        # ── Step 4: 等待 Clash 重启后验证出口IP ──
        log.info("[Router] %s 配置已推送，等待 Clash 重启（30s）...", router.name)
        time.sleep(30)

        new_exit_ip = get_router_exit_ip(router)
        result["exit_ip_after_deploy"] = new_exit_ip

        if new_exit_ip:
            self.update_router(router_id, {
                "current_exit_ip": new_exit_ip,
                "last_check": time.time(),
                "online": True,
            })
            log.info("[Router] %s 部署验证通过，出口IP: %s", router.name, new_exit_ip)
            result["exit_ip_verified"] = True

            # 部署后地理位置验证
            if router.country:
                try:
                    from src.device_control.ip_geolocation import verify_ip_for_country
                    geo_match, geo_info = verify_ip_for_country(new_exit_ip, router.country)
                    result["geo_match"] = geo_match
                    result["geo_info"] = geo_info
                    if not geo_match:
                        log.warning("[Router] %s 出口IP %s 不在目标国家 %s（实际: %s）",
                                    router.name, new_exit_ip, router.country,
                                    geo_info.get("actual", "?"))
                        result["geo_warning"] = (
                            f"出口IP {new_exit_ip} 实际在 {geo_info.get('actual','?')} "
                            f"而非 {router.country.upper()}"
                        )
                    else:
                        log.info("[Router] %s 地理验证通过: %s %s",
                                 router.name, new_exit_ip, geo_info.get("city", ""))
                except Exception as e:
                    log.debug("[Router] %s 地理验证异常（不影响部署结果）: %s", router.name, e)

            result["ok"] = True
            result["message"] = f"部署成功，出口IP: {new_exit_ip}"
        else:
            log.warning("[Router] %s 部署后无法获取出口IP（Clash可能仍在重启中）",
                        router.name)
            result["exit_ip_verified"] = False
            result["ok"] = True  # 推送本身成功，只是验证有延迟
            result["message"] = "配置已推送，出口IP验证超时（Clash重启中，稍后自动检测）"
            # 发送告警
            self._send_alert(
                f"⚠️ 路由器 {router.name} 配置已推送，但出口IP验证超时\n"
                f"可能原因: Clash重启需要更长时间\n"
                f"请在1-2分钟后检查: GET /routers/{router_id}/status"
            )

        return result

    def deploy_all(self) -> List[dict]:
        """部署所有路由器。"""
        return [self.deploy_router(r.router_id) for r in self.list_routers()]

    # ── 状态检测 ──

    def get_status(self, router_id: str) -> RouterStatus:
        router = self.get_router(router_id)
        if not router:
            return RouterStatus(router_id=router_id, name="未知", online=False,
                                error="路由器不存在")

        online = check_router_online(router)
        exit_ip = ""
        if online:
            exit_ip = get_router_exit_ip(router)
            self.update_router(router_id, {
                "online": True,
                "current_exit_ip": exit_ip,
                "last_check": time.time(),
            })
        else:
            self.update_router(router_id, {"online": False, "last_check": time.time()})

        return RouterStatus(
            router_id=router.router_id,
            name=router.name,
            online=online,
            exit_ip=exit_ip,
            proxy_count=len(router.proxy_ids),
            device_count=len(router.device_ids),
        )

    def get_all_status(self) -> List[RouterStatus]:
        """并发检测所有路由器状态。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        routers = self.list_routers()
        if not routers:
            return []
        with ThreadPoolExecutor(max_workers=min(10, len(routers))) as ex:
            futs = {ex.submit(self.get_status, r.router_id): r for r in routers}
            return [f.result() for f in as_completed(futs)]

    # ── 后台监控 ──

    def start_monitor(self, interval: int = 300):
        """启动后台监控线程（默认每5分钟检查一次）。"""
        global _monitor_thread, _monitor_running
        if _monitor_running:
            return
        _monitor_running = True

        def _loop():
            log.info("[Router] 监控线程启动，检查间隔 %ds", interval)
            while _monitor_running:
                try:
                    self._monitor_tick()
                except Exception as e:
                    log.error("[Router] 监控异常: %s", e)
                time.sleep(interval)

        _monitor_thread = threading.Thread(target=_loop, daemon=True, name="router-monitor")
        _monitor_thread.start()

    def stop_monitor(self):
        global _monitor_running
        _monitor_running = False

    def _monitor_tick(self):
        """单次监控：检查所有路由器在线状态 + 出口IP + 地理位置验证。

        新增（Phase5）：
        - 获取出口IP后，调用 ip_geolocation 验证国家是否匹配
        - 国家不匹配时发告警（soft warning，不影响运行）
        """
        routers = self.list_routers()
        for router in routers:
            was_online = router.online
            online = check_router_online(router)

            if not online and was_online:
                log.warning("[Router] %s (%s) 离线！", router.name, router.ip)
                self._send_alert(
                    f"⚠️ 路由器离线告警\n"
                    f"路由器: {router.name} ({router.router_id})\n"
                    f"IP: {router.ip}\n"
                    f"影响手机: {len(router.device_ids)} 台\n"
                    f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )

            if online:
                exit_ip = get_router_exit_ip(router)
                if exit_ip:
                    log.debug("[Router] %s 出口IP: %s", router.name, exit_ip)
                    updates = {
                        "online": True,
                        "current_exit_ip": exit_ip,
                        "last_check": time.time(),
                    }
                    self.update_router(router.router_id, updates)

                    # 地理位置验证（有配置 country 时才验证）
                    if router.country:
                        self._verify_exit_ip_country_async(router, exit_ip)
            else:
                self.update_router(router.router_id, {
                    "online": False,
                    "last_check": time.time(),
                })

    def _verify_exit_ip_country_async(self, router: "RouterInfo", exit_ip: str):
        """异步验证出口IP是否在目标国家（不阻塞监控主循环）。"""
        def _verify():
            try:
                from src.device_control.ip_geolocation import verify_ip_for_country
                match, info = verify_ip_for_country(exit_ip, router.country)
                if match:
                    log.debug("[Router] %s 出口IP %s 地理验证通过: %s %s",
                              router.name, exit_ip, info.get("actual"), info.get("city", ""))
                else:
                    actual = info.get("actual", "unknown")
                    log.warning(
                        "[Router] %s 出口IP %s 地理不匹配！期望=%s 实际=%s (%s) ISP=%s",
                        router.name, exit_ip, router.country.upper(),
                        actual, info.get("city", ""), info.get("isp", ""),
                    )
                    self._send_alert(
                        f"🌍 路由器出口IP地理不匹配\n"
                        f"路由器: {router.name}\n"
                        f"出口IP: {exit_ip}\n"
                        f"期望国家: {router.country.upper()}\n"
                        f"实际国家: {actual} ({info.get('country', '')})\n"
                        f"城市: {info.get('city', '未知')}\n"
                        f"ISP: {info.get('isp', '未知')}\n"
                        f"⚠️ 代理可能路由到了错误的国家，建议检查代理账号设置"
                    )
            except Exception as e:
                log.debug("[Router] %s 地理验证异常: %s", router.name, e)

        threading.Thread(target=_verify, daemon=True,
                         name=f"geo-verify-{router.router_id}").start()

    def _send_alert(self, message: str):
        """发送 Telegram 告警。"""
        try:
            from src.host.routers.notifications import send_telegram_message
            send_telegram_message(message)
        except Exception:
            try:
                from src.behavior.notifier import notify
                notify(message)
            except Exception:
                log.warning("[Router] 无法发送告警: %s", message[:100])

    # ── Clash 配置预览 ──

    def preview_clash_config(self, router_id: str) -> str:
        router = self.get_router(router_id)
        if not router:
            return ""
        pool = _load_pool()
        proxy_accounts = [c for c in pool.get("configs", [])
                          if c["id"] in router.proxy_ids]
        return generate_clash_config(router, proxy_accounts)

    # ── 国家地理信息 ──

    @staticmethod
    def get_country_info(country: str) -> dict:
        return COUNTRY_GPS.get(country.lower(), {})

    @staticmethod
    def list_supported_countries() -> List[str]:
        return list(COUNTRY_GPS.keys())


# 单例
_manager_instance: Optional[RouterManager] = None
_manager_lock = threading.Lock()


def get_router_manager() -> RouterManager:
    global _manager_instance
    with _manager_lock:
        if _manager_instance is None:
            _manager_instance = RouterManager()
    return _manager_instance
