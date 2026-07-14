# -*- coding: utf-8 -*-
"""
代理健康监控 — 出口IP验证与熔断保护。

核心功能:
  1. 每5分钟验证每台手机的真实出口IP
  2. 出口IP与路由器预期IP不匹配 → 触发熔断（连续3次失败）
  3. 熔断后自动停止该手机的TikTok任务
  4. Telegram实时告警
  5. 自动GPS/时区/语言配置（与目标国家匹配）

IP状态机（4态）:
  ok         — 手机IP与路由器出口IP匹配，一切正常
  leak       — IP不匹配，流量未走代理（触发熔断计数）
  no_ip      — ADB/网络问题，无法获取手机出口IP（触发熔断计数）
  unverified — 路由器预期IP未知，无法比对（不触发熔断，标记待验证）

熔断逻辑:
  连续3次 leak/no_ip → 熔断打开 → 停止任务 + 告警
  熔断打开后15分钟冷却期内不自动关闭（防止IP抖动假恢复）
  冷却期满 + IP正常 → 熔断关闭 + consecutive_fails归零

GPS策略（真机 vs 模拟器）:
  模拟器（ro.kernel.qemu=1）: adb emu geo fix（原生支持）
  真机: am broadcast MockLocation intent（需开发者模式+MockLocation应用）
  失败不影响时区/语言配置（GPS是辅助，IP地理位置才是主防线）
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from pathlib import Path
import yaml

from src.utils.subprocess_text import run_shell
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# 默认配置（可被 devices.yaml 中的 proxy_health 节覆盖）
# 检查间隔（秒）
CHECK_INTERVAL = 300  # 5分钟

# 熔断冷却期（秒）— 熔断打开后最少等待这么久才允许自动关闭
CIRCUIT_COOLDOWN = 900  # 15分钟

# 连续失败触发熔断的阈值
CIRCUIT_THRESHOLD = 3

_PROXY_HEALTH_CFG_MTIME: float = 0.0


def _load_proxy_health_config() -> None:
    """从 devices.yaml 中的 proxy_health 节加载可配置的熔断参数.

    支持的字段:
      proxy_health:
        check_interval: 300     # 定期检查间隔(s)
        circuit_threshold: 3    # 连续失败触发熔断次数
        circuit_cooldown: 900   # 熔断冷却期(s)
    """
    global CHECK_INTERVAL, CIRCUIT_THRESHOLD, CIRCUIT_COOLDOWN, _PROXY_HEALTH_CFG_MTIME
    try:
        # 项目根 config/devices.yaml
        cfg_path = Path(__file__).parent.parent.parent / "config" / "devices.yaml"
        if not cfg_path.exists():
            return
        mtime = cfg_path.stat().st_mtime
        if mtime <= _PROXY_HEALTH_CFG_MTIME:
            return
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        ph = data.get("proxy_health") or {}
        if ph:
            CHECK_INTERVAL = int(ph.get("check_interval", CHECK_INTERVAL))
            CIRCUIT_THRESHOLD = int(ph.get("circuit_threshold", CIRCUIT_THRESHOLD))
            CIRCUIT_COOLDOWN = int(ph.get("circuit_cooldown", CIRCUIT_COOLDOWN))
            log.info("[ProxyHealth] 配置已加载: check=%ds threshold=%d cooldown=%ds",
                     CHECK_INTERVAL, CIRCUIT_THRESHOLD, CIRCUIT_COOLDOWN)
        _PROXY_HEALTH_CFG_MTIME = mtime
    except Exception as exc:
        log.debug("[ProxyHealth] 配置加载失败 (ignored): %s", exc)


# 模块导入时加载一次
_load_proxy_health_config()

# IP 查询服务（多个备选，防止单点故障）
IP_CHECK_URLS = [
    "https://api.ipify.org",
    "https://ip.sb",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

# IPv4 格式验证
_IPV4_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")


def _is_valid_ipv4(ip: str) -> bool:
    """验证 IPv4 地址格式和范围。"""
    if not _IPV4_RE.match(ip):
        return False
    parts = ip.split(".")
    return all(0 <= int(p) <= 255 for p in parts)


@dataclass
class DeviceProxyStatus:
    device_id: str
    router_id: str = ""
    expected_ip: str = ""        # 路由器的出口IP（可能未知）
    actual_ip: str = ""          # 手机实际出口IP
    ip_match: bool = True
    state: str = "unverified"    # ok / leak / no_ip / unverified
    country: str = ""
    last_check: float = 0.0
    circuit_open: bool = False   # 熔断器状态
    circuit_open_time: float = 0.0  # 熔断打开时间（用于冷却期计算）
    consecutive_fails: int = 0   # 连续失败次数（只计 leak/no_ip）
    error: str = ""


_status_cache: Dict[str, DeviceProxyStatus] = {}
_status_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_running = False


# ═══════════════════════════════════════════════
# ADB 工具函数
# ═══════════════════════════════════════════════

def _adb(device_id: str, cmd: str, timeout: int = 20) -> Tuple[str, int]:
    """执行 ADB 命令，返回 (stdout, returncode)。

    相比旧版只返回 stdout，增加 returncode 用于验证命令是否真正成功。
    """
    try:
        r = run_shell(
            f"adb -s {device_id} {cmd}",
            capture_output=True,
            timeout=timeout,
        )
        return (r.stdout or "").strip(), r.returncode
    except subprocess.TimeoutExpired:
        log.debug("[Proxy] %s ADB超时: %s", device_id[:8], cmd[:60])
        return "", -1
    except Exception as e:
        log.debug("[Proxy] %s ADB异常: %s | cmd=%s", device_id[:8], e, cmd[:60])
        return "", -1


def _adb_out(device_id: str, cmd: str, timeout: int = 20) -> str:
    """只返回 stdout（向后兼容的便捷方法）。"""
    out, _ = _adb(device_id, cmd, timeout)
    return out


def _is_emulator(device_id: str) -> bool:
    """检测设备是否为 Android 模拟器。

    通过 ro.kernel.qemu 属性判断：
    - "1" → 模拟器（AVD/Genymotion/BlueStacks等）
    - ""  → 真实物理设备
    """
    out = _adb_out(device_id, "shell getprop ro.kernel.qemu", timeout=5)
    return out.strip() == "1"


# ═══════════════════════════════════════════════
# 出口IP获取（并行多服务）
# ═══════════════════════════════════════════════

def get_device_real_ip(device_id: str) -> str:
    """通过ADB在手机上执行curl获取真实出口IP。

    优化：并行查询多个IP服务，取最先返回的有效结果（更快，防单点故障）。
    返回值：有效IPv4字符串，或 "" 表示失败。
    """
    result_holder: List[str] = []
    result_lock = threading.Lock()
    found_event = threading.Event()

    def _query(url: str):
        if found_event.is_set():
            return
        out = _adb_out(device_id,
                       f'shell "curl -s --max-time 8 {url} 2>/dev/null"',
                       timeout=15)
        ip = out.strip()
        if ip and _is_valid_ipv4(ip):
            with result_lock:
                if not result_holder:
                    result_holder.append(ip)
                    found_event.set()

    # 并行查询，但最多等12秒
    with ThreadPoolExecutor(max_workers=len(IP_CHECK_URLS)) as pool:
        futs = [pool.submit(_query, url) for url in IP_CHECK_URLS]
        found_event.wait(timeout=12)
        # 取消未完成的任务
        for f in futs:
            f.cancel()

    return result_holder[0] if result_holder else ""


# ═══════════════════════════════════════════════
# 设备地理配置（GPS / 时区 / 语言）
# ═══════════════════════════════════════════════

def set_device_gps(device_id: str, lat: float, lon: float) -> bool:
    """通过ADB模拟GPS位置。

    策略（按设备类型）:
    1. 模拟器: adb emu geo fix（原生支持，立即生效）
    2. 真机: 使用 mock_location_manager 智能多APP适配器
       - 自动扫描 20+ 已知 FakeGPS 应用包名
       - Per-device 缓存，避免每次重复扫描
       - 支持多种 Intent 格式，覆盖不同应用
       - 未找到应用时提供 APK 安装指引
       - GPS设置失败不影响整体配置（IP地理位置才是TikTok主要检测手段）
    """
    is_emu = _is_emulator(device_id)

    if is_emu:
        # 模拟器路径：直接发送 emu 命令
        out, rc = _adb(device_id, f"emu geo fix {lon} {lat}", timeout=10)
        if rc == 0 or "OK" in out:
            log.debug("[Proxy] %s GPS(模拟器) 设置成功: %.4f, %.4f", device_id[:8], lat, lon)
            return True
        log.debug("[Proxy] %s GPS(模拟器) 命令返回: rc=%d out=%s", device_id[:8], rc, out[:50])
        # 模拟器命令失败时也尝试真机路径作为备用

    # 真机路径：使用智能 MockLocation 多APP适配器（Phase 6 P0）
    try:
        from src.device_control.mock_location_manager import set_mock_location
        ok = set_mock_location(device_id, lat, lon)
        if ok:
            log.debug("[Proxy] %s GPS(MockLocation适配器) 设置成功: %.4f, %.4f",
                      device_id[:8], lat, lon)
            return True
        # 适配器失败时记录提示（非阻塞性）
        log.warning(
            "[Proxy] %s GPS设置失败（%s）。"
            "请确保设备已安装 MockLocation 应用（如 Fake GPS by Lexa）并授权。"
            "当前IP地理位置检测仍然有效，GPS仅为辅助防护。",
            device_id[:8],
            "真实设备" if not is_emu else "模拟器",
        )
        return False
    except ImportError:
        # mock_location_manager 不可用时回退到旧方式
        log.debug("[Proxy] mock_location_manager 不可用，使用通用 Intent")
        out2, rc2 = _adb(
            device_id,
            f'shell "am broadcast -a android.intent.action.MOCK_LOCATION '
            f'--ef latitude {lat} --ef longitude {lon}"',
            timeout=10,
        )
        if "Broadcast completed" in out2 or "result=0" in out2 or rc2 == 0:
            log.debug("[Proxy] %s GPS(通用intent) 已发送: %.4f, %.4f", device_id[:8], lat, lon)
            return True
        log.warning("[Proxy] %s GPS设置失败（通用Intent），请安装 MockLocation 应用", device_id[:8])
        return False


def set_device_timezone(device_id: str, timezone: str) -> bool:
    """通过ADB设置手机时区，并验证设置是否生效。

    例: America/New_York, Europe/Rome, Asia/Tokyo
    验证方式: 设置后读取 persist.sys.timezone 确认

    注意: 部分MIUI设备需重启才能完全生效，但大多数应用（包括TikTok）
    读取实时时区不依赖重启。
    """
    # 双重设置：persist属性 + Android settings
    _adb(device_id, f"shell setprop persist.sys.timezone {timezone}", timeout=5)
    _adb(device_id, f"shell settings put global time_zone {timezone}", timeout=5)

    # 验证1：读取 persist 属性
    actual, _ = _adb(device_id, "shell getprop persist.sys.timezone", timeout=5)
    if actual.strip() == timezone:
        log.debug("[Proxy] %s 时区已验证: %s", device_id[:8], timezone)
        return True

    # 验证2：读取 settings
    actual2, _ = _adb(device_id, "shell settings get global time_zone", timeout=5)
    if actual2.strip() == timezone:
        log.debug("[Proxy] %s 时区(settings)已验证: %s", device_id[:8], timezone)
        return True

    log.warning(
        "[Proxy] %s 时区设置未生效（期望=%s 实际=%s）。"
        "MIUI设备可能需要重启后生效。",
        device_id[:8], timezone, actual.strip() or "空"
    )
    return False


def set_device_language(device_id: str, language: str) -> bool:
    """通过ADB设置系统语言，并验证设置是否生效。

    例: en-US, it-IT, ja-JP
    注意: MIUI等深度定制ROM可能需要在系统设置中操作，ADB命令效果有限。
    """
    locale, *region = language.split("-")
    region_str = region[0] if region else locale.upper()

    # 设置语言属性
    _adb(device_id,
         f'shell "setprop persist.sys.language {locale} && '
         f'setprop persist.sys.country {region_str}"',
         timeout=8)

    # Android 7+ locale 统一格式
    _adb(device_id,
         f'shell "setprop persist.sys.locale {language}"',
         timeout=5)

    # 验证
    actual_lang, _ = _adb(device_id, "shell getprop persist.sys.language", timeout=5)
    actual_locale, _ = _adb(device_id, "shell getprop persist.sys.locale", timeout=5)

    lang_ok = (actual_lang.strip().lower() == locale.lower())
    locale_ok = (language.lower() in actual_locale.strip().lower())

    if lang_ok or locale_ok:
        log.debug("[Proxy] %s 语言已验证: %s", device_id[:8], language)
        return True

    log.warning(
        "[Proxy] %s 语言设置未验证（期望=%s, getprop返回: lang=%s locale=%s）。"
        "MIUI设备需在系统设置中手动切换语言。",
        device_id[:8], language,
        actual_lang.strip() or "空",
        actual_locale.strip() or "空",
    )
    return False


def configure_device_for_country(device_id: str, country: str, city: str = "") -> dict:
    """根据目标国家配置手机的GPS/时区/语言。

    一键配置，与代理IP地理位置匹配，防止TikTok检测。
    GPS失败不影响整体成功状态（GPS是辅助，时区/语言才是主要配置）。
    """
    from src.device_control.router_manager import COUNTRY_GPS
    country_info = COUNTRY_GPS.get(country.lower(), {})
    if not country_info:
        return {"ok": False, "error": f"不支持的国家: {country}（支持: {list(COUNTRY_GPS.keys())}）"}

    cities = country_info.get("cities", [])
    if not cities:
        return {"ok": False, "error": "无城市GPS数据"}

    # 选择对应城市，或默认第一个
    city_info = cities[0]
    if city:
        for c in cities:
            if city.lower() in c["city"].lower():
                city_info = c
                break

    lat = city_info["lat"]
    lon = city_info["lon"]
    tz = city_info["tz"]
    lang = country_info.get("language", "en-US")

    gps_ok = set_device_gps(device_id, lat, lon)
    tz_ok = set_device_timezone(device_id, tz)
    lang_ok = set_device_language(device_id, lang)

    # GPS失败不影响整体ok（GPS是辅助）
    overall_ok = tz_ok or lang_ok

    result = {
        "device_id": device_id,
        "country": country,
        "city": city_info["city"],
        "lat": lat,
        "lon": lon,
        "timezone": tz,
        "language": lang,
        "gps_set": gps_ok,
        "timezone_set": tz_ok,
        "language_set": lang_ok,
        "ok": overall_ok,
    }

    if not gps_ok:
        result["gps_warning"] = "GPS设置失败（真机需MockLocation应用），其他配置正常"

    log.info(
        "[Proxy] %s 地理配置完成: %s %s GPS=%s TZ=%s Lang=%s",
        device_id[:8], country, city_info["city"],
        "✓" if gps_ok else "✗",
        "✓" if tz_ok else "✗",
        "✓" if lang_ok else "✗",
    )
    return result


# ═══════════════════════════════════════════════
# 健康监控
# ═══════════════════════════════════════════════

class ProxyHealthMonitor:
    """代理健康监控器 — 出口IP验证与熔断保护。

    IP状态机（4态）:
      ok         → IP匹配，consecutive_fails清零
      leak       → IP不匹配，consecutive_fails+1
      no_ip      → ADB/网络故障，consecutive_fails+1
      unverified → expected_ip未知，不计失败，等待路由器IP就绪

    熔断逻辑:
      连续 CIRCUIT_THRESHOLD 次 leak/no_ip → 熔断打开
      熔断打开后 CIRCUIT_COOLDOWN 秒内：不自动关闭（防抖动假恢复）
      冷却期满且IP=ok → 熔断关闭，consecutive_fails=0
    """

    def __init__(self):
        self._device_router_map: Dict[str, str] = {}   # device_id → router_id
        self._router_exit_ips: Dict[str, str] = {}      # router_id → exit_ip

    def register_device(self, device_id: str, router_id: str):
        """注册手机与路由器的绑定关系。"""
        self._device_router_map[device_id] = router_id
        log.debug("[ProxyHealth] 注册: %s → 路由器%s", device_id[:8], router_id)

    def register_all_from_routers(self):
        """从路由器配置自动注册所有设备。"""
        try:
            from src.device_control.router_manager import get_router_manager
            mgr = get_router_manager()
            for r in mgr.list_routers():
                for did in r.device_ids:
                    self._device_router_map[did] = r.router_id
                if r.current_exit_ip:
                    self._router_exit_ips[r.router_id] = r.current_exit_ip
        except Exception as e:
            log.warning("[ProxyHealth] 自动注册失败: %s", e)

    def _get_expected_ip(self, router_id: str) -> str:
        """获取路由器预期出口IP（先查缓存，再查RouterManager）。

        优化：先从本地缓存取，避免每次都调用 RouterManager。
        若缓存无，则从 RouterManager 读取并更新缓存。
        """
        if not router_id:
            return ""

        # 1. 先查缓存
        cached = self._router_exit_ips.get(router_id, "")
        if cached:
            return cached

        # 2. 缓存未命中，从 RouterManager 获取
        try:
            from src.device_control.router_manager import get_router_manager
            r = get_router_manager().get_router(router_id)
            if r and r.current_exit_ip:
                self._router_exit_ips[router_id] = r.current_exit_ip
                return r.current_exit_ip
        except Exception as e:
            log.debug("[ProxyHealth] 获取路由器%s出口IP失败: %s", router_id, e)

        return ""

    def check_device(self, device_id: str) -> DeviceProxyStatus:
        """检查单台手机的出口IP，更新状态机，触发熔断逻辑。

        4态 IP 状态机:
          no_ip      → 无法获取手机出口IP（ADB/网络问题）
          unverified → 没有预期IP可供比对（路由器IP未知）
          leak       → IP不匹配（IP泄漏，流量未走代理）
          ok         → IP匹配，代理正常

        熔断: leak/no_ip 累计3次 → 打开熔断 → 停止任务 + 告警
        恢复: ok + 熔断冷却期满 → 关闭熔断 + consecutive_fails归零
        """
        router_id = self._device_router_map.get(device_id, "")

        with _status_lock:
            status = _status_cache.get(device_id) or DeviceProxyStatus(
                device_id=device_id,
                router_id=router_id,
            )

        status.router_id = router_id
        status.last_check = time.time()

        # ── Step 1: 获取两端IP ──
        expected_ip = self._get_expected_ip(router_id)
        actual_ip = get_device_real_ip(device_id)

        status.expected_ip = expected_ip
        status.actual_ip = actual_ip

        # ── Step 2: 4态状态机判断 ──
        if not actual_ip:
            # 无法获取手机IP（ADB断线或网络问题）
            status.state = "no_ip"
            status.ip_match = False
            status.consecutive_fails += 1
            status.error = "无法获取出口IP（ADB或网络问题）"
            log.warning("[ProxyHealth] %s 无法获取出口IP（连续失败%d次）",
                        device_id[:8], status.consecutive_fails)

        elif not expected_ip:
            # 路由器预期IP未知，无法比对 → 不计入失败，等路由器IP就绪
            status.state = "unverified"
            status.ip_match = True   # 不触发熔断
            status.error = f"路由器{router_id or '未绑定'}预期IP未知，无法验证"
            # 不修改 consecutive_fails
            log.debug("[ProxyHealth] %s 出口IP未知，跳过验证（手机IP=%s）",
                      device_id[:8], actual_ip)

        elif actual_ip != expected_ip:
            # IP不匹配：流量未走路由器代理（IP泄漏）
            status.state = "leak"
            status.ip_match = False
            status.consecutive_fails += 1
            status.error = f"IP泄漏: 期望={expected_ip} 实际={actual_ip}"
            log.warning("[ProxyHealth] %s IP泄漏！期望=%s 实际=%s（连续失败%d次）",
                        device_id[:8], expected_ip, actual_ip, status.consecutive_fails)

        else:
            # IP匹配：代理正常
            status.state = "ok"
            status.ip_match = True
            status.consecutive_fails = 0
            status.error = ""
            log.debug("[ProxyHealth] %s IP正常: %s", device_id[:8], actual_ip)

        # ── Step 3: 熔断器逻辑 ──
        now = time.time()

        # 熔断打开：连续失败达到阈值且当前未熔断
        if status.consecutive_fails >= CIRCUIT_THRESHOLD and not status.circuit_open:
            status.circuit_open = True
            status.circuit_open_time = now
            log.error("[ProxyHealth] %s 触发熔断！连续%d次失败（state=%s）",
                      device_id[:8], status.consecutive_fails, status.state)
            self._on_circuit_open(device_id, status)

        # 熔断恢复：IP正常 + 熔断冷却期已满
        elif (status.state == "ok" and status.circuit_open):
            elapsed = now - status.circuit_open_time
            if elapsed >= CIRCUIT_COOLDOWN:
                status.circuit_open = False
                status.consecutive_fails = 0  # ★ 关键：恢复时必须归零，否则下次立即重新熔断
                log.info("[ProxyHealth] %s 熔断恢复（冷却期已满%.0fs），IP正常: %s",
                         device_id[:8], elapsed, actual_ip)
                self._send_alert(
                    f"✅ 代理恢复正常\n"
                    f"设备: {device_id}\n"
                    f"当前IP: {actual_ip}\n"
                    f"路由器: {router_id}\n"
                    f"恢复时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                )
            else:
                remaining = CIRCUIT_COOLDOWN - elapsed
                log.info("[ProxyHealth] %s IP已恢复，但仍在冷却期（剩余%.0fs）",
                         device_id[:8], remaining)

        with _status_lock:
            _status_cache[device_id] = status

        return status

    def _on_circuit_open(self, device_id: str, status: DeviceProxyStatus):
        """熔断触发：停止该设备的任务 + 尝试自动代理轮换 + 发告警。

        改进（Phase5）：
        - state='leak'（IP不匹配）时，触发代理自动轮换，尝试无人工干预恢复
        - state='no_ip'（设备/ADB问题）时，只停任务+告警，不轮换代理
        - 轮换由 proxy_rotator 模块负责，带速率限制和黑名单
        """
        # ── 停止该设备上的任务 ──
        stopped = 0
        try:
            from src.host import task_store as _ts
            running = [t for t in _ts.list_tasks(device_id=device_id, limit=200)
                       if t.get("status") in ("running", "pending")]
            for t in running:
                tid = t.get("task_id") or t.get("id")
                if not tid:
                    continue
                try:
                    _ts.set_task_cancelled(tid)
                    stopped += 1
                except Exception as ce:
                    log.warning("[ProxyHealth] cancel %s 失败: %s", tid, ce)
            log.warning("[ProxyHealth] %s 已停止 %d 个任务（熔断保护）",
                        device_id[:8], stopped)
        except Exception as e:
            log.warning("[ProxyHealth] 停止任务失败: %s", e)

        # ── 发送初始告警 ──
        self._send_alert(
            f"🚨 代理IP泄漏熔断告警\n"
            f"设备: {device_id}\n"
            f"路由器: {status.router_id or '未绑定'}\n"
            f"状态: {status.state}\n"
            f"期望IP: {status.expected_ip or '未知'}\n"
            f"实际IP: {status.actual_ip or '无法获取'}\n"
            f"已停止任务: {stopped} 个\n"
            f"冷却期: {CIRCUIT_COOLDOWN//60} 分钟后可自动恢复\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'正在尝试自动代理轮换...' if status.state == 'leak' and status.router_id else '请手动检查设备ADB连接'}"
        )

        # ── 若是 IP 泄漏（代理问题），尝试自动轮换 ──
        # no_ip 说明是设备/ADB问题，轮换代理没有意义
        if status.state == "leak" and status.router_id:
            threading.Thread(
                target=self._try_proxy_rotation,
                args=(device_id, status),
                daemon=True,
                name=f"proxy-rotate-{device_id[:8]}",
            ).start()
        else:
            log.info("[ProxyHealth] %s state=%s，跳过自动轮换（需手动处理设备端问题）",
                     device_id[:8], status.state)

    def _try_proxy_rotation(self, device_id: str, status: DeviceProxyStatus):
        """在后台线程中尝试代理自动轮换（不阻塞监控循环）。"""
        router_id = status.router_id
        log.info("[ProxyHealth] %s 启动代理自动轮换（路由器=%s）...",
                 device_id[:8], router_id)
        try:
            from src.device_control.proxy_rotator import rotate_proxy
            result = rotate_proxy(router_id, reason=f"IP泄漏熔断（设备={device_id}）")

            if result.get("ok"):
                log.info("[ProxyHealth] %s 代理轮换成功，新出口IP=%s，等待监控自动恢复熔断器",
                         device_id[:8], result.get("exit_ip", "未知"))
                # 轮换成功后，更新路由器出口IP缓存，下次check_device可以用新IP验证
                new_ip = result.get("exit_ip", "")
                if new_ip:
                    self._router_exit_ips[router_id] = new_ip
            elif result.get("skipped"):
                log.info("[ProxyHealth] %s 代理轮换跳过: %s",
                         device_id[:8], result.get("skipped"))
            else:
                log.warning("[ProxyHealth] %s 代理轮换失败: %s",
                            device_id[:8], result.get("error", "未知原因"))
        except Exception as e:
            log.error("[ProxyHealth] %s 代理轮换异常: %s", device_id[:8], e)

    def reset_circuit(self, device_id: str) -> bool:
        """手动重置指定设备的熔断器（用于运维干预）。"""
        with _status_lock:
            s = _status_cache.get(device_id)
            if not s:
                return False
            s.circuit_open = False
            s.circuit_open_time = 0.0
            s.consecutive_fails = 0
            s.error = ""
            s.state = "unverified"
        log.info("[ProxyHealth] %s 熔断器已手动重置", device_id[:8])
        return True

    def _send_alert(self, message: str):
        try:
            from src.host.routers.notifications import send_telegram_message
            send_telegram_message(message)
        except Exception:
            log.warning("[ProxyHealth] 告警发送失败: %s", message[:80])

    def get_all_status(self) -> Dict[str, dict]:
        with _status_lock:
            return {
                did: {
                    "device_id": s.device_id,
                    "router_id": s.router_id,
                    "expected_ip": s.expected_ip,
                    "actual_ip": s.actual_ip,
                    "ip_match": s.ip_match,
                    "state": s.state,
                    "circuit_open": s.circuit_open,
                    "circuit_open_time": s.circuit_open_time,
                    "circuit_cooldown_remaining": max(
                        0, CIRCUIT_COOLDOWN - (time.time() - s.circuit_open_time)
                    ) if s.circuit_open else 0,
                    "consecutive_fails": s.consecutive_fails,
                    "last_check": s.last_check,
                    "error": s.error,
                }
                for did, s in _status_cache.items()
            }

    def get_summary(self) -> dict:
        """返回健康摘要（用于 dashboard 展示）。"""
        with _status_lock:
            statuses = list(_status_cache.values())

        total = len(statuses)
        ok = sum(1 for s in statuses if s.state == "ok")
        leak = sum(1 for s in statuses if s.state == "leak")
        no_ip = sum(1 for s in statuses if s.state == "no_ip")
        unverified = sum(1 for s in statuses if s.state == "unverified")
        breakers_open = sum(1 for s in statuses if s.circuit_open)

        return {
            "total": total,
            "ok": ok,
            "leak": leak,
            "no_ip": no_ip,
            "unverified": unverified,
            "circuit_breakers_open": breakers_open,
            "health_rate": round(ok / total * 100, 1) if total else 0,
        }

    def start_monitor(self, interval: int = CHECK_INTERVAL):
        """启动后台监控线程。"""
        global _monitor_thread, _monitor_running
        if _monitor_running:
            return
        _monitor_running = True

        def _loop():
            log.info("[ProxyHealth] 监控线程启动，间隔 %ds, 熔断阈值 %d次, 冷却期 %ds",
                     interval, CIRCUIT_THRESHOLD, CIRCUIT_COOLDOWN)
            # 等待系统完全启动
            time.sleep(30)
            while _monitor_running:
                try:
                    # 热重载：检测 devices.yaml 配置变化（mtime 比对，开销极低）
                    _load_proxy_health_config()
                    self.register_all_from_routers()
                    devices = list(self._device_router_map.keys())
                    if devices:
                        log.debug("[ProxyHealth] 开始检查 %d 台设备", len(devices))
                    for did in devices:
                        if not _monitor_running:
                            break
                        try:
                            self.check_device(did)
                            time.sleep(2)  # 每台间隔2秒，避免ADB并发冲突
                        except Exception as e:
                            log.debug("[ProxyHealth] %s 检查异常: %s", did[:8], e)
                except Exception as e:
                    log.error("[ProxyHealth] 监控循环异常: %s", e)
                time.sleep(interval)

        _monitor_thread = threading.Thread(
            target=_loop, daemon=True, name="proxy-health-monitor"
        )
        _monitor_thread.start()

    def stop_monitor(self):
        global _monitor_running
        _monitor_running = False
        log.info("[ProxyHealth] 监控线程已停止")


# 单例
_health_monitor: Optional[ProxyHealthMonitor] = None
_health_lock = threading.Lock()


def get_proxy_health_monitor() -> ProxyHealthMonitor:
    global _health_monitor
    with _health_lock:
        if _health_monitor is None:
            _health_monitor = ProxyHealthMonitor()
    return _health_monitor
