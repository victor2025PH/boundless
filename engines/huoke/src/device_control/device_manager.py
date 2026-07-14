#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备管理器模块 (v3 — uiautomator2 优先)

架构:
  UI操作优先通过 uiautomator2 在设备端原子完成（查找+点击一步到位）。
  若 u2 不可用，降级为 adb shell uiautomator dump + 主机解析 + input tap。
  ADB 命令仍用于设备管理、截图、文件传输等非UI操作。
"""

import subprocess
import time

from src.utils.subprocess_text import run as _sp_run_text
import logging
import re
import math
import base64
import xml.etree.ElementTree as ET
from typing import Optional, Dict, List, Any, Tuple, Union
from dataclasses import dataclass
from enum import Enum
import yaml
from pathlib import Path

from src.host.device_registry import PROJECT_ROOT, config_file

# uiautomator2 可选导入
try:
    import uiautomator2 as u2
    _HAS_U2 = True
except ImportError:
    _HAS_U2 = False


class DeviceStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    BUSY = "busy"
    ERROR = "error"
    OFFLINE = "offline"


@dataclass
class DeviceInfo:
    device_id: str
    display_name: str
    platform: str
    manufacturer: str = ""
    model: str = ""
    android_version: str = ""
    resolution: Optional[Dict[str, int]] = None
    dpi: int = 320
    status: DeviceStatus = DeviceStatus.DISCONNECTED
    last_seen: float = 0.0
    imei: str = ""
    hw_serial: str = ""
    android_id: str = ""

    @property
    def is_online(self) -> bool:
        return self.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)

    @property
    def fingerprint(self) -> str:
        """Persistent device identity: IMEI > hw_serial > android_id."""
        return self.imei or self.hw_serial or self.android_id or ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_id": self.device_id,
            "display_name": self.display_name,
            "platform": self.platform,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "android_version": self.android_version,
            "resolution": self.resolution or {},
            "dpi": self.dpi,
            "status": self.status.value,
            "last_seen": self.last_seen,
            "imei": self.imei,
            "hw_serial": self.hw_serial,
            "android_id": self.android_id,
            "fingerprint": self.fingerprint,
        }


@dataclass
class UIElement:
    """通过 legacy dump 解析出的元素（fallback 用）"""
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    class_name: str = ""
    package: str = ""
    bounds: Tuple[int, int, int, int] = (0, 0, 0, 0)
    clickable: bool = False
    enabled: bool = True
    focusable: bool = False
    focused: bool = False
    scrollable: bool = False
    selected: bool = False
    checkable: bool = False
    checked: bool = False

    @property
    def center(self) -> Tuple[int, int]:
        return ((self.bounds[0] + self.bounds[2]) // 2,
                (self.bounds[1] + self.bounds[3]) // 2)

    @property
    def width(self) -> int:
        return self.bounds[2] - self.bounds[0]

    @property
    def height(self) -> int:
        return self.bounds[3] - self.bounds[1]

    def __repr__(self) -> str:
        parts = []
        if self.resource_id:
            parts.append(f"id={self.resource_id.split('/')[-1]}")
        if self.text:
            parts.append(f"text='{self.text[:30]}'")
        if self.content_desc:
            parts.append(f"desc='{self.content_desc[:30]}'")
        parts.append(f"bounds={self.bounds}")
        return f"UIElement({', '.join(parts)})"


_BOUNDS_PATTERN = re.compile(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]')


class DeviceManager:
    """
    设备管理器。
    UI操作优先走 uiautomator2（设备端原子执行），不可用时降级为 ADB dump。
    """

    def __init__(self, config_path: Optional[str] = None):
        self.logger = logging.getLogger(__name__)
        self.devices: Dict[str, DeviceInfo] = {}
        self.adb_path = "adb"
        self.timeout = 5
        self.max_retries = 3
        self._yaml_path: Optional[str] = config_path

        self._u2_connections: Dict[str, Any] = {}
        self._u2_available = _HAS_U2
        self._removed_devices: set = set()

        # 发现缓存
        self._discover_cache_time: float = 0.0
        self._discover_cache_result: List[str] = []

        # 同指纹多路 ADB（USB + 无线）故障转移：device_id -> 优先顺序列表
        self._transport_failover: dict[str, list[str]] = {}
        self._adb_failover = True

        # 检测集群角色
        self._cluster_role = "standalone"
        try:
            if config_path:
                cluster_cfg_path = Path(config_path).resolve().parent / "cluster.yaml"
                if cluster_cfg_path.exists():
                    with open(cluster_cfg_path, encoding="utf-8") as f:
                        cluster_cfg = yaml.safe_load(f) or {}
                    self._cluster_role = cluster_cfg.get("role", "standalone")
        except Exception:
            pass

        if config_path:
            self.load_config(config_path)
        self.discover_devices()

    # =========================================================================
    # uiautomator2 连接管理
    # =========================================================================

    def get_u2(self, device_id: str, force_reconnect: bool = False) -> Optional[Any]:
        """
        获取设备的 uiautomator2 连接（懒初始化 + 健康检测 + 自动重连）。
        返回 u2.Device 或 None。

        Sprint 5 P2-4 优化:真机回归发现,设备掉线后 `u2.connect()` 每次要花
        ~9s 才认清现实(3 次尝试 × 2s 间隔 + 每次 ~1s 超时)。executor 里
        `_u2()` 反复被调用 → 设备真的掉线时单任务会白白卡 300s+。
        这里加入 `_offline_until` 短期缓存:若设备在过去 30s 内被标记 offline
        (通过 `adb devices` 快速预检),直接返回 None,将 9s 省掉。
        """
        if not self._u2_available:
            return None

        cached = self._u2_connections.get(device_id)
        if cached and not force_reconnect:
            try:
                cached.info  # 轻量健康检测
                return cached
            except Exception:
                self.logger.warning(f"u2 连接失效 ({device_id})，尝试重连")
                self._u2_connections.pop(device_id, None)

        # --- P2-4: 快速离线预检 ---------------------------------------------
        # 通过 `adb devices` (≤4s) 判断设备是否在线。若离线,立即返回 None,
        # 免掉 `u2.connect()` 3 × 2s 硬等 ≈ 9s。connect 成功时结果被缓存在
        # `_u2_connections`,不会每次都走 adb devices。
        try:
            result = subprocess.run(
                [self.adb_path, "devices"],
                capture_output=True, text=True, timeout=4,
            )
            out = (result.stdout or "")
            online = (f"{device_id}\tdevice" in out) or (f"{device_id}\tunauthorized" in out)
            if not online:
                self.logger.warning(
                    "[u2-precheck] %s 不在线 (adb devices 未列出或未 authorized),"
                    "跳过 u2 连接尝试",
                    device_id[:12],
                )
                return None
        except Exception as e:
            self.logger.debug("[u2-precheck] adb devices 预检异常: %s", e)

        transports = (
            self.transport_sequence(device_id)
            if getattr(self, "_adb_failover", True)
            else [device_id]
        )
        last_err: Optional[str] = None
        for tdid in transports:
            for attempt in range(3):
                try:
                    d = u2.connect(tdid)
                    d.settings['wait_timeout'] = 15.0
                    d.settings['operation_delay'] = (0.1, 0.1)
                    self._u2_connections[device_id] = d
                    if tdid != device_id:
                        self.logger.info(
                            "[u2 failover] %s → %s 连接成功",
                            device_id[:12],
                            tdid[:22],
                        )
                    else:
                        self.logger.info(f"u2 连接成功: {device_id}")
                    return d
                except Exception as e:
                    last_err = str(e)
                    self.logger.warning(
                        f"u2 连接尝试 {attempt+1}/3 失败 ({tdid}): {e}"
                    )
                    if attempt < 2:
                        time.sleep(2)

        self.logger.warning(
            f"u2 连接最终失败 ({device_id})，将使用 ADB 降级方案: {last_err}"
        )
        return None

    @property
    def has_u2(self) -> bool:
        return self._u2_available

    # =========================================================================
    # ADB 命令执行
    # =========================================================================

    def transport_sequence(self, device_id: str) -> list[str]:
        """同一指纹下多路连接时的尝试顺序（USB 优先于无线）。"""
        return list(self._transport_failover.get(device_id) or [device_id])

    def _rebuild_transport_failover_map(self) -> None:
        """按指纹合并 USB + 无线，供 ADB/u2 故障转移。"""
        self._transport_failover = {}
        fp_map: dict[str, list[str]] = {}

        def _is_wifi_transport(did: str) -> bool:
            return ":" in did and did.split(":")[0].count(".") == 3

        for did, dev in self.devices.items():
            if dev.status != DeviceStatus.CONNECTED:
                continue
            if not dev.fingerprint:
                continue
            fp_map.setdefault(dev.fingerprint, []).append(did)

        covered: set[str] = set()
        for _fp, ids in fp_map.items():
            if len(ids) == 1:
                self._transport_failover[ids[0]] = [ids[0]]
                covered.add(ids[0])
                continue
            ordered = sorted(ids, key=lambda x: (1 if _is_wifi_transport(x) else 0, x))
            for did in ids:
                self._transport_failover[did] = ordered
                covered.add(did)

        for did, dev in self.devices.items():
            if dev.status == DeviceStatus.CONNECTED and did not in covered:
                self._transport_failover[did] = [did]

    def _adb_transport_candidates(self, device_id: Optional[str]) -> list[Optional[str]]:
        if not device_id:
            return [None]
        if not getattr(self, "_adb_failover", True):
            return [device_id]
        seq = self._transport_failover.get(device_id)
        if seq:
            return list(seq)
        return [device_id]

    def _execute_adb_command_once(self, command: str, device_id: Optional[str] = None) -> Tuple[bool, str]:
        """单次 ADB（无故障转移）。"""
        full_command = [self.adb_path]
        if device_id:
            full_command.extend(['-s', device_id])
        full_command.extend(command.split())
        self.logger.debug(f"ADB: {' '.join(full_command)}")
        try:
            result = _sp_run_text(
                full_command,
                capture_output=True,
                timeout=self.timeout,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, f"ADB命令超时: {command}"
        except Exception as e:
            return False, str(e)

    def execute_adb_command(self, command: str, device_id: Optional[str] = None) -> Tuple[bool, str]:
        """执行ADB命令（字符串，按空格拆分）。同一指纹多路时按顺序尝试直至成功。"""
        candidates = self._adb_transport_candidates(device_id)
        if len(candidates) <= 1:
            return self._execute_adb_command_once(command, candidates[0] if candidates else None)
        last_err = ""
        for cand in candidates:
            ok, out = self._execute_adb_command_once(command, cand)
            if ok:
                if cand != device_id:
                    self.logger.info(
                        "[ADB failover] %s → %s OK",
                        (device_id or "")[:12],
                        (cand or "")[:22],
                    )
                return ok, out
            last_err = out
        return False, last_err or "ADB 失败"

    def _run_adb(self, args: List[str], device_id: Optional[str] = None,
                 timeout: Optional[int] = None) -> Tuple[bool, str]:
        """执行ADB命令（列表形式，精确控制参数）。"""
        candidates = self._adb_transport_candidates(device_id)
        if len(candidates) <= 1:
            return self._run_adb_once(args, candidates[0] if candidates else None, timeout)
        last_err = ""
        for cand in candidates:
            ok, out = self._run_adb_once(args, cand, timeout)
            if ok:
                if cand != device_id:
                    self.logger.info(
                        "[ADB failover] %s → %s OK",
                        (device_id or "")[:12],
                        (cand or "")[:22],
                    )
                return ok, out
            last_err = out
        return False, last_err or "ADB 失败"

    def _run_adb_once(self, args: List[str], device_id: Optional[str] = None,
                      timeout: Optional[int] = None) -> Tuple[bool, str]:
        cmd = [self.adb_path]
        if device_id:
            cmd.extend(['-s', device_id])
        cmd.extend(args)
        self.logger.debug(f"ADB: {cmd}")
        try:
            result = _sp_run_text(
                cmd,
                capture_output=True,
                timeout=timeout or self.timeout,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            return False, result.stderr.strip() or result.stdout.strip()
        except subprocess.TimeoutExpired:
            return False, "ADB命令超时"
        except Exception as e:
            return False, str(e)

    # =========================================================================
    # 设备发现与管理
    # =========================================================================

    def load_config(self, config_path: str) -> None:
        self._yaml_path = config_path
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            if 'connection' in config:
                c = config['connection']
                self.adb_path = c.get('adb_path', 'adb')
                self.timeout = c.get('timeout_seconds', 30)
                self.max_retries = c.get('max_retries', 3)
                self._adb_failover = bool(c.get('adb_failover_to_alternate', True))
            if 'devices' in config:
                for did, dc in config['devices'].items():
                    self.devices[did] = DeviceInfo(
                        device_id=did,
                        display_name=dc.get('display_name', did),
                        platform=dc.get('platform', 'android'),
                        manufacturer=dc.get('manufacturer', ''),
                        model=dc.get('model', ''),
                        android_version=dc.get('android_version', ''),
                        resolution=dc.get('resolution'),
                        dpi=dc.get('dpi', 320),
                        status=DeviceStatus(dc.get('status', 'disconnected')),
                    )
            self.logger.info(f"已加载配置: {config_path}")
        except Exception as e:
            self.logger.error(f"加载配置失败: {e}")

    def discover_devices(self, force: bool = False) -> List[str]:
        """发现连接的设备。使用缓存避免频繁 ADB 调用。

        Args:
            force: 强制刷新，忽略缓存
        """
        # 缓存: 10秒内不重复执行 adb devices
        now = time.time()
        if not force and self._discover_cache_time and (now - self._discover_cache_time) < 10:
            return self._discover_cache_result

        # Coordinator 默认跳过本机 ADB（设备在 Worker）；主控与 USB 同机时仍需发现本机序列号。
        # 注意：仅当 adb 状态为「device」才算 has_local 时，若手机均为 unauthorized/offline，
        # 会跳过整段发现逻辑，导致 /devices 列表为空、屏幕监控看不到本机插着的手机。
        # 因此：凡本机 adb 列出任意已连接序列号（含未授权），都应执行完整发现以填充 usb_issue。
        if self._cluster_role == "coordinator" and not force:
            ok_scan, out_scan = self.execute_adb_command("devices")
            has_local = False
            _local_statuses = (
                "device",
                "unauthorized",
                "offline",
                "authorizing",
                "no permissions",
            )
            if ok_scan:
                for line in out_scan.split("\n")[1:]:
                    if line.strip() and "\t" in line:
                        st = line.strip().split("\t", 1)
                        if len(st) >= 2 and st[1].strip() in _local_statuses:
                            has_local = True
                            break
            if not has_local:
                self.logger.debug(
                    "Coordinator 模式且本机无任何 USB 附件，跳过本机 ADB 发现",
                )
                self._discover_cache_time = time.time()
                self._discover_cache_result = list(self.devices.keys())
                self._rebuild_transport_failover_map()
                return self._discover_cache_result

        self.logger.info("开始发现设备...")

        # Sprint 4 P0:TCP 设备自动重连。
        # 背景:IP:PORT 设备(WiFi ADB)会因手机锁屏/网络抖动断开,
        # adb 不会自动重连。每轮发现前对"上次见过但现在不在列表"的
        # 已知 TCP 设备发一次 `adb connect`,成功则本轮 devices 就
        # 会再次列出它。限制:单次最多 5 个,每个超时 3s,避免把 discover
        # 拉长到分钟级。
        try:
            previously_known_tcp = [
                d for d, info in self.devices.items()
                if ":" in d and info.status in (
                    DeviceStatus.DISCONNECTED, DeviceStatus.CONNECTED
                )
            ][:5]
            if previously_known_tcp:
                self.logger.debug("[tcp_reconnect] 尝试重连 %d 个已知 TCP 设备",
                                  len(previously_known_tcp))
                for tcp_addr in previously_known_tcp:
                    try:
                        self.execute_adb_command(f"connect {tcp_addr}")
                    except Exception as e:
                        self.logger.debug("[tcp_reconnect] %s: %s",
                                          tcp_addr, e)
        except Exception as e:
            self.logger.debug("[tcp_reconnect] 整体异常: %s", e)

        for did in self.devices:
            self.devices[did].status = DeviceStatus.DISCONNECTED

        success, output = self.execute_adb_command("devices")
        discovered = []
        problem_devices: list = []
        if success:
            for line in output.split('\n')[1:]:
                if line.strip() and '\t' in line:
                    parts = line.strip().split('\t', 1)
                    did = parts[0]
                    status = parts[1] if len(parts) > 1 else ""
                    if status == 'device':
                        discovered.append(did)
                        if did in self.devices:
                            self.devices[did].status = DeviceStatus.CONNECTED
                            self.devices[did].last_seen = time.time()
                            if not self.devices[did].fingerprint:
                                self._collect_fingerprint(did)
                                self._try_fingerprint_migration(did)
                        else:
                            self.devices[did] = DeviceInfo(
                                device_id=did, display_name=did[:8],
                                platform="android", status=DeviceStatus.CONNECTED,
                                last_seen=time.time(),
                            )
                            self._update_device_details(did)
                            self._try_fingerprint_migration(did)
                    elif status in ('unauthorized', 'offline',
                                    'no permissions', 'authorizing'):
                        if did in self._removed_devices:
                            continue
                        problem_devices.append((did, status))
                        if did in self.devices:
                            self.devices[did].status = DeviceStatus.DISCONNECTED
                        self.logger.warning(
                            "设备 %s 状态异常: %s — %s",
                            did[:8], status,
                            {"unauthorized": "请在手机上确认USB调试授权",
                             "offline": "USB连接异常，请重新插拔",
                             "no permissions": "缺少USB权限，请检查udev规则",
                             "authorizing": "正在等待授权，请在手机上点击确认",
                             }.get(status, "未知状态"))

        self._last_problem_devices = problem_devices
        self.logger.info("发现 %d 个设备: %s%s", len(discovered), discovered,
                         f" (异常: {problem_devices})" if problem_devices else "")

        # 自动清理幽灵设备: disconnected 超过 5 分钟的旧记录
        now = time.time()
        stale_ids = []
        for did, dev in list(self.devices.items()):
            if dev.status == DeviceStatus.DISCONNECTED and did not in discovered:
                age = now - dev.last_seen if dev.last_seen > 0 else 9999
                if age > 300:  # 5 分钟
                    stale_ids.append((did, dev.last_seen))
        for did, last_seen in stale_ids:
            age_min = (now - last_seen) / 60 if last_seen > 0 else float("inf")
            self.devices.pop(did, None)
            self._u2_connections.pop(did, None)
            self._removed_devices.add(did)
            self._remove_from_yaml(did)
            self._cleanup_sidecar_refs(did)
            self.logger.info(
                "自动清理幽灵设备: %s (离线约 %.0f 分钟，已同步 YAML/别名)",
                did[:8],
                age_min if math.isfinite(age_min) else -1,
            )
        if stale_ids:
            self.logger.info("已清理 %d 个幽灵设备", len(stale_ids))

        # 缓存结果
        self._discover_cache_time = time.time()
        self._discover_cache_result = discovered
        self._rebuild_transport_failover_map()
        return discovered

    def _try_fingerprint_migration(self, new_serial: str) -> None:
        """If this serial is new but matches an existing fingerprint, migrate."""
        dev = self.devices.get(new_serial)
        if not dev or not dev.fingerprint:
            return
        try:
            from src.device_control.device_registry import get_device_registry
            registry = get_device_registry()

            # First try direct fingerprint lookup
            entry = registry.lookup(dev.fingerprint)

            # Also try matching placeholder entries by hw_serial or android_id
            if not entry:
                all_reg = registry.get_all()
                for fp_key, reg_entry in all_reg.items():
                    if not fp_key.startswith("serial:"):
                        continue
                    placeholder_serial = fp_key[7:]
                    if placeholder_serial == new_serial:
                        entry = reg_entry
                        # Upgrade placeholder to real fingerprint
                        with registry._lock:
                            reg_data = registry._load()
                            reg_data[dev.fingerprint] = reg_data.pop(fp_key)
                            reg_data[dev.fingerprint]["current_serial"] = new_serial
                            reg_data[dev.fingerprint]["hw_serial"] = dev.hw_serial
                            reg_data[dev.fingerprint]["android_id"] = dev.android_id
                            reg_data[dev.fingerprint]["imei"] = dev.imei
                            registry._save(reg_data)
                        self.logger.info(
                            "[指纹迁移] 升级占位符: %s → fp=%s",
                            fp_key, dev.fingerprint[:12],
                        )
                        break
                    # Match by hw_serial or android_id if available
                    if (dev.hw_serial and reg_entry.get("hw_serial") == dev.hw_serial) or \
                       (dev.android_id and reg_entry.get("android_id") == dev.android_id):
                        entry = reg_entry
                        with registry._lock:
                            reg_data = registry._load()
                            reg_data[dev.fingerprint] = reg_data.pop(fp_key)
                            reg_data[dev.fingerprint]["current_serial"] = new_serial
                            reg_data[dev.fingerprint]["hw_serial"] = dev.hw_serial
                            reg_data[dev.fingerprint]["android_id"] = dev.android_id
                            reg_data[dev.fingerprint]["imei"] = dev.imei
                            old_s = reg_entry.get("current_serial", "")
                            if old_s and old_s != new_serial:
                                prev = reg_data[dev.fingerprint].get("previous_serials", [])
                                if old_s not in prev:
                                    prev.append(old_s)
                                reg_data[dev.fingerprint]["previous_serials"] = prev
                            registry._save(reg_data)
                        self.logger.info(
                            "[指纹迁移] 通过hw/android_id匹配: %s → fp=%s",
                            fp_key, dev.fingerprint[:12],
                        )
                        break

            if not entry:
                return
            old_serial = entry.get("current_serial", "")
            if old_serial == new_serial:
                return
            if not old_serial:
                return

            self.logger.info(
                "[指纹迁移] 识别到旧设备: fp=%s, 旧串号=%s → 新串号=%s",
                dev.fingerprint[:12], old_serial[:8], new_serial[:8],
            )

            # Inherit display_name from old entry
            old_dev = self.devices.get(old_serial)
            if old_dev and old_dev.display_name and old_dev.display_name != old_serial[:8]:
                dev.display_name = old_dev.display_name

            # Update registry
            registry.update_serial(dev.fingerprint, new_serial)

            # Migrate all JSON and SQLite references
            registry.migrate_serial(old_serial, new_serial, PROJECT_ROOT)

            # Remove old serial from in-memory device dict — BUT 仅当老串号"确实不在线"时才 pop。
            # 修复：USB 串号 + 无线 IP:5555 双通道指同一台手机时，两个串号都会被
            # discover_devices 扫到、进入 self.devices，并相互触发指纹迁移。
            # 如果无脑 pop，就会把另一条活通道从 devices 字典里抹掉，
            # 导致 get_device_info(usb_serial) 返回 None、set_current_device 报
            # "Device not found"（2026-04-21 真机 smoke 复现）。
            if (
                old_serial in self.devices
                and old_serial != new_serial
                and self.devices[old_serial].status != DeviceStatus.CONNECTED
            ):
                self.devices.pop(old_serial, None)

            self.logger.info("[指纹迁移] 完成: %s 继承编号 #%02d",
                             new_serial[:8], entry.get("number", 0))
        except Exception as e:
            self.logger.warning("[指纹迁移] 失败 %s: %s", new_serial[:8], e)

    def _update_device_details(self, device_id: str) -> None:
        dev = self.devices.get(device_id)
        if not dev:
            return
        try:
            ok, v = self.execute_adb_command("shell getprop ro.product.model", device_id)
            if ok: dev.model = v.strip()
            ok, v = self.execute_adb_command("shell getprop ro.product.manufacturer", device_id)
            if ok: dev.manufacturer = v.strip()
            ok, v = self.execute_adb_command("shell getprop ro.build.version.release", device_id)
            if ok: dev.android_version = v.strip()
            ok, v = self.execute_adb_command("shell wm size", device_id)
            if ok and 'Physical size:' in v:
                s = v.split('Physical size:')[1].strip()
                if 'x' in s:
                    w, h = s.split('x')
                    dev.resolution = {'width': int(w), 'height': int(h)}
            ok, v = self.execute_adb_command("shell wm density", device_id)
            if ok and 'Physical density:' in v:
                dev.dpi = int(v.split('Physical density:')[1].strip())
        except Exception as e:
            self.logger.warning(f"更新设备信息失败 {device_id}: {e}")

        self._collect_fingerprint(device_id)

    def _collect_fingerprint(self, device_id: str) -> None:
        """Collect persistent identity: IMEI, ro.serialno, android_id."""
        dev = self.devices.get(device_id)
        if not dev:
            return
        try:
            ok, v = self._run_adb(
                ['shell', 'service', 'call', 'iphonesubinfo', '1'], device_id, timeout=10
            )
            if ok and v:
                digits = re.findall(r"'([^']+)'", v)
                imei_raw = "".join(digits).replace(".", "").replace(" ", "")
                if len(imei_raw) >= 15:
                    dev.imei = imei_raw[:15]
        except Exception as e:
            self.logger.debug("设备指纹采集IMEI失败 %s: %s", device_id, e)
        try:
            ok, v = self._run_adb(
                ['shell', 'getprop', 'ro.serialno'], device_id, timeout=5
            )
            if ok and v.strip() and v.strip().lower() != 'unknown':
                dev.hw_serial = v.strip()
        except Exception as e:
            self.logger.debug("设备指纹采集hw_serial失败 %s: %s", device_id, e)
        try:
            ok, v = self._run_adb(
                ['shell', 'settings', 'get', 'secure', 'android_id'], device_id, timeout=5
            )
            if ok and v.strip() and v.strip().lower() != 'null':
                dev.android_id = v.strip()
        except Exception as e:
            self.logger.debug("设备指纹采集android_id失败 %s: %s", device_id, e)
        if dev.fingerprint:
            self.logger.info("设备指纹采集 %s: fp=%s (imei=%s, hw=%s, aid=%s)",
                             device_id[:8], dev.fingerprint[:12],
                             dev.imei[:8] if dev.imei else '-',
                             dev.hw_serial[:8] if dev.hw_serial else '-',
                             dev.android_id[:8] if dev.android_id else '-')

    def run_usb_diagnostics(self) -> dict:
        """Full USB/ADB diagnostic scan with detailed device state info."""
        import subprocess as _sp

        result = {
            "adb_version": "",
            "connected": [],
            "problem": [],
            "configured_missing": [],
            "usb_tree": [],
        }

        try:
            v = _sp.run(
                [self.adb_path, "version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            result["adb_version"] = v.stdout.strip().split("\n")[0] if v.stdout else ""
        except Exception as e:
            self.logger.debug("诊断: 获取adb版本失败: %s", e)

        try:
            r = _sp.run(
                [self.adb_path, "devices", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            seen_ids = set()
            if r.returncode == 0:
                for line in r.stdout.strip().split("\n")[1:]:
                    if not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    did = parts[0]
                    status = parts[1]
                    seen_ids.add(did)
                    props = {}
                    for p in parts[2:]:
                        if ":" in p:
                            k, v2 = p.split(":", 1)
                            props[k] = v2

                    alias = ""
                    try:
                        import json as _json
                        af = config_file("device_aliases.json")
                        if af.exists():
                            with open(af, "r", encoding="utf-8") as f:
                                aliases = _json.load(f)
                            if did in aliases:
                                alias = aliases[did].get("alias", "")
                    except Exception as e:
                        self.logger.debug("诊断: 读取设备别名失败 %s: %s", did, e)

                    entry = {
                        "device_id": did,
                        "alias": alias,
                        "status": status,
                        "model": props.get("model", ""),
                        "product": props.get("product", ""),
                        "transport_id": props.get("transport_id", ""),
                        "usb": props.get("usb", ""),
                        "device": props.get("device", ""),
                    }

                    if status == "device":
                        try:
                            bat = _sp.run(
                                [self.adb_path, "-s", did, "shell",
                                 "dumpsys", "battery"],
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=5,
                            )
                            for bline in bat.stdout.split("\n"):
                                if "level:" in bline:
                                    entry["battery"] = int(
                                        bline.split("level:")[1].strip())
                                    break
                        except Exception as e:
                            self.logger.debug("诊断: 获取电池信息失败 %s: %s", did, e)
                        result["connected"].append(entry)
                    else:
                        diag = {
                            "unauthorized": "手机上弹出了USB调试授权对话框，请点击'允许'",
                            "offline": "设备处于离线状态，请重新插拔USB线",
                            "no permissions": "缺少USB权限，Linux系统请检查udev规则",
                            "authorizing": "正在等待授权，请在手机屏幕上确认",
                            "recovery": "设备处于恢复模式",
                            "sideload": "设备处于sideload模式",
                        }
                        entry["diagnosis"] = diag.get(status, f"未知状态: {status}")
                        result["problem"].append(entry)

            configured = set(self.devices.keys())
            missing = configured - seen_ids
            for mid in missing:
                dev = self.devices.get(mid)
                result["configured_missing"].append({
                    "device_id": mid,
                    "alias": "",
                    "display_name": dev.display_name if dev else mid[:8],
                    "diagnosis": "ADB完全未检测到此设备 — 请检查USB线缆/接口/调试开关",
                })

        except Exception as e:
            self.logger.error("USB诊断失败: %s", e)

        usb_ports: dict = {}
        for dev in result["connected"] + result["problem"]:
            port = dev.get("usb") or dev.get("transport_id") or "unknown"
            usb_ports.setdefault(port, []).append({
                "device_id": dev["device_id"],
                "alias": dev.get("alias", ""),
                "status": dev["status"],
                "model": dev.get("model", ""),
            })
        result["usb_tree"] = [
            {"port": port, "devices": devs}
            for port, devs in sorted(usb_ports.items())
        ]

        result["summary"] = {
            "total_configured": len(self.devices),
            "connected": len(result["connected"]),
            "problem": len(result["problem"]),
            "missing": len(result["configured_missing"]),
            "usb_ports_active": len(usb_ports),
        }

        # —— 深度项：短稳态抽样 + shell 延迟（供电 Hub 仍掉线时用于区分「链路抖动」与「控制器/负载」）——
        result["stability_check"] = {}
        result["shell_latency_ms"] = []
        result["analysis_hints"] = []
        try:
            import time as _time

            def _adb_devices_map() -> dict:
                rr = _sp.run(
                    [self.adb_path, "devices"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=12,
                    creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
                )
                m = {}
                if rr.returncode != 0 or not rr.stdout:
                    return m
                for line in rr.stdout.strip().split("\n")[1:]:
                    if "\t" not in line:
                        continue
                    a, b = line.strip().split("\t", 1)
                    m[a.strip()] = b.strip()
                return m

            snaps = []
            for i in range(3):
                snaps.append(_adb_devices_map())
                if i < 2:
                    _time.sleep(1.2)
            flips = []
            all_ids = set()
            for s in snaps:
                all_ids |= set(s.keys())
            for did in sorted(all_ids):
                states = [sn.get(did, "<missing>") for sn in snaps]
                if len(set(states)) > 1:
                    flips.append({"device_id": did, "states": states})
            result["stability_check"] = {
                "interval_seconds": 1.2,
                "snapshots": snaps,
                "state_changes": flips,
            }

            cf = getattr(_sp, "CREATE_NO_WINDOW", 0)
            for ent in list(result["connected"]):
                did = ent.get("device_id", "")
                if not did:
                    continue
                times_ms = []
                for _ in range(5):
                    t0 = _time.perf_counter()
                    rr = _sp.run(
                        [self.adb_path, "-s", did, "shell", "echo", "1"],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=12,
                        creationflags=cf,
                    )
                    dt = (_time.perf_counter() - t0) * 1000.0
                    times_ms.append(dt if rr.returncode == 0 else -1.0)
                valid = [x for x in times_ms if x >= 0]
                if valid:
                    valid.sort()
                    mid = valid[len(valid) // 2]
                    result["shell_latency_ms"].append({
                        "device_id": did,
                        "median_ms": round(mid, 1),
                        "max_ms": round(max(valid), 1),
                    })

            hints = []
            if flips:
                hints.append(
                    "【抖动】约 2.4s 内 adb 状态不一致：优先查线材/接口微接触、Hub 芯片过热、手机自动休眠或 USB 模式被改。"
                )
            for row in result["shell_latency_ms"]:
                if row.get("median_ms", 0) > 1500:
                    hints.append(
                        f"【延迟】{row['device_id'][:8]}… shell 中位 {row['median_ms']}ms，"
                        "可能 USB 控制器带宽不足、线过长、或同时投屏/大量 adb/uiautomator 占满。"
                    )
            for pe in result["problem"]:
                if pe.get("status") == "offline":
                    hints.append(
                        "【offline】多为链路未就绪或瞬时断开；若已用供电 Hub，可试把设备分散到主板不同 USB 根口、"
                        "关掉省电中「限制 USB」、并减少同 Hub 上高码率投屏数量。"
                    )
                    break
            if len(result["connected"]) >= 3 and result["summary"].get("usb_ports_active", 0) <= 1:
                hints.append(
                    "【拓扑】多机仍显示同一类 USB 路径时，总线争用可能导致随机掉线；尽量让部分设备走机箱后置另一控制器。"
                )
            result["analysis_hints"] = hints
        except Exception as e:
            self.logger.debug("USB 深度诊断附加项失败: %s", e)

        return result

    def get_device_info(self, device_id: str) -> Optional[DeviceInfo]:
        return self.devices.get(device_id)

    def get_all_devices(self) -> List[DeviceInfo]:
        return list(self.devices.values())

    def get_connected_devices(self) -> List[DeviceInfo]:
        return [d for d in self.devices.values() if d.status == DeviceStatus.CONNECTED]

    def remove_device(self, device_id: str) -> bool:
        """Remove a device from memory, YAML config, and blocklist. Returns True if removed."""
        if device_id not in self.devices:
            self._removed_devices.add(device_id)
            return True
        dev = self.devices[device_id]
        if dev.status == DeviceStatus.CONNECTED:
            return False
        del self.devices[device_id]
        self._u2_connections.pop(device_id, None)
        self._removed_devices.add(device_id)
        self._remove_from_yaml(device_id)
        self.logger.info("设备已移除: %s", device_id)
        return True

    def _remove_from_yaml(self, device_id: str):
        """Remove a device entry from the YAML config file."""
        if not self._yaml_path:
            return
        try:
            with open(self._yaml_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            devs = cfg.get('devices', {})
            if device_id in devs:
                del devs[device_id]
                cfg['devices'] = devs
                with open(self._yaml_path, 'w', encoding='utf-8') as f:
                    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
                self.logger.info("YAML 配置已移除设备: %s", device_id)
        except Exception as e:
            self.logger.warning("移除 YAML 设备失败: %s", e)

    def _cleanup_sidecar_refs(self, device_id: str) -> None:
        """device_aliases.json 与 SQLite 成员/状态（与 API 删除一致，供自动幽灵清理复用）。"""
        import json as _json

        try:
            if self._yaml_path:
                ap = Path(self._yaml_path).parent / "device_aliases.json"
                if ap.exists():
                    data = _json.loads(ap.read_text("utf-8"))
                    if device_id in data:
                        del data[device_id]
                        ap.write_text(_json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass
        try:
            from src.host.database import get_conn

            db = get_conn()
            db.execute("DELETE FROM device_group_members WHERE device_id = ?", (device_id,))
            db.execute("DELETE FROM device_states WHERE device_id = ?", (device_id,))
            db.commit()
        except Exception:
            pass

    def get_screen_size(self, device_id: str) -> Optional[Tuple[int, int]]:
        ok, v = self.execute_adb_command("shell wm size", device_id)
        if ok and 'Physical size:' in v:
            s = v.split('Physical size:')[1].strip()
            if 'x' in s:
                w, h = s.split('x')
                return int(w), int(h)
        return None

    # =========================================================================
    # 基础操作（ADB）
    # =========================================================================

    def capture_screen(self, device_id: str, save_path: Optional[str] = None) -> Optional[bytes]:
        self.logger.debug(f"截取设备 {device_id} 屏幕")

        d = self.get_u2(device_id)
        if d:
            try:
                img = d.screenshot()
                from io import BytesIO
                buf = BytesIO()
                img.save(buf, format='PNG')
                data = buf.getvalue()
                if save_path:
                    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                    img.save(save_path)
                return data
            except Exception as e:
                self.logger.debug(f"u2 截图失败: {e}，降级为 ADB")

        temp_file = f"/sdcard/screenshot_{int(time.time())}.png"
        ok, _ = self.execute_adb_command(f"shell screencap -p {temp_file}", device_id)
        if not ok:
            return None
        local_tmp = f"temp_screenshot_{int(time.time())}.png"
        ok, _ = self.execute_adb_command(f"pull {temp_file} {local_tmp}", device_id)
        self.execute_adb_command(f"shell rm {temp_file}", device_id)
        if not ok:
            return None
        try:
            with open(local_tmp, 'rb') as f:
                data = f.read()
            if save_path:
                Path(save_path).parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, 'wb') as f2:
                    f2.write(data)
            Path(local_tmp).unlink(missing_ok=True)
            return data
        except Exception as e:
            self.logger.error(f"处理截图失败: {e}")
            return None

    def input_tap(self, device_id: str, x: int, y: int) -> bool:
        ok, out = self._run_adb(['shell', 'input', 'tap', str(x), str(y)], device_id)
        if not ok:
            self.logger.error(f"点击失败: {out}")
        return ok

    def input_swipe(self, device_id: str, x1: int, y1: int,
                    x2: int, y2: int, duration_ms: int = 300) -> bool:
        ok, out = self._run_adb(
            ['shell', 'input', 'swipe', str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
            device_id
        )
        if not ok:
            self.logger.error(f"滑动失败: {out}")
        return ok

    def input_keyevent(self, device_id: str, keycode: int) -> bool:
        ok, out = self._run_adb(['shell', 'input', 'keyevent', str(keycode)], device_id)
        if not ok:
            self.logger.error(f"按键失败: {out}")
        return ok

    # =========================================================================
    # 智能文本输入
    # =========================================================================

    def input_text(self, device_id: str, text: str) -> bool:
        """
        智能文本输入：
        1. 优先 u2.send_keys（设备端注入，原生支持 Unicode，不受输入法影响）
        2. 降级为 ADB input text（仅 ASCII）
        3. 非ASCII 降级时尝试 ADBKeyboard
        """
        if not text:
            return True

        d = self.get_u2(device_id)
        if d:
            try:
                d.send_keys(text, clear=False)
                self.logger.info(f"u2 文本输入成功: {text[:30]}...")
                return True
            except Exception as e:
                self.logger.debug(f"u2 send_keys 失败: {e}，降级为 ADB")

        has_non_ascii = any(ord(c) > 127 for c in text)
        if not has_non_ascii:
            return self._input_text_ascii(device_id, text)

        self.logger.info("非ASCII文本 + 无u2，尝试 ADBKeyboard")
        if self._try_adb_keyboard(device_id, text):
            return True

        self.logger.warning("非ASCII输入失败。建议: pip install uiautomator2")
        ascii_fallback = ''.join(c if ord(c) < 128 else '?' for c in text)
        return self._input_text_ascii(device_id, ascii_fallback)

    def _input_text_ascii(self, device_id: str, text: str) -> bool:
        escaped = text.replace(' ', '%s').replace("'", "\\'")
        ok, out = self.execute_adb_command(f"shell input text '{escaped}'", device_id)
        if not ok:
            self.logger.error(f"ASCII文本输入失败: {out}")
        return ok

    def _try_adb_keyboard(self, device_id: str, text: str) -> bool:
        ok, ime_list = self.execute_adb_command('shell ime list -s', device_id)
        if not ok or 'com.android.adbkeyboard' not in ime_list:
            return False
        ok, current_ime = self.execute_adb_command(
            'shell settings get secure default_input_method', device_id
        )
        current_ime = current_ime.strip() if ok else ''
        self._run_adb(['shell', 'ime', 'set', 'com.android.adbkeyboard/.AdbIME'], device_id)
        time.sleep(0.3)
        b64 = base64.b64encode(text.encode('utf-8')).decode('ascii')
        ok, out = self._run_adb(
            ['shell', 'am', 'broadcast', '-a', 'ADB_INPUT_B64', '--es', 'msg', b64],
            device_id
        )
        if current_ime:
            self._run_adb(['shell', 'ime', 'set', current_ime], device_id)
        return ok

    # =========================================================================
    # UI 元素交互 — u2 优先路径（核心）
    # =========================================================================

    def u2_click(self, device_id: str, timeout: float = 15.0, **selector) -> bool:
        """
        通过 uiautomator2 在设备端查找元素并点击（原子操作）。
        selector 参数直接传给 u2，常用: resourceId, text, description, className
        """
        d = self.get_u2(device_id)
        if not d:
            self.logger.debug("u2不可用，转legacy")
            return False

        try:
            elem = d(**selector)
            if elem.wait(timeout=timeout):
                elem.click()
                self.logger.info(f"u2 点击成功: {selector}")
                return True
            self.logger.warning(f"u2 等待元素超时: {selector}")
            return False
        except Exception as e:
            self.logger.error(f"u2 点击失败: {e}")
            return False

    def u2_set_text(self, device_id: str, text: str, timeout: float = 15.0,
                    **selector) -> bool:
        """在设备端找到输入框并设置文本（原子操作，支持 Unicode）"""
        d = self.get_u2(device_id)
        if not d:
            return False
        try:
            elem = d(**selector)
            if elem.wait(timeout=timeout):
                elem.set_text(text)
                self.logger.info(f"u2 设置文本成功: {selector}")
                return True
            return False
        except Exception as e:
            self.logger.error(f"u2 设置文本失败: {e}")
            return False

    def u2_exists(self, device_id: str, timeout: float = 5.0, **selector) -> bool:
        """检查元素是否存在（在设备端执行）"""
        d = self.get_u2(device_id)
        if not d:
            return False
        try:
            return d(**selector).wait(timeout=timeout)
        except Exception as e:
            self.logger.debug("u2_exists 检查失败 %s: %s", device_id, e)
            return False

    def u2_get_text(self, device_id: str, timeout: float = 5.0, **selector) -> Optional[str]:
        """获取元素的文本内容"""
        d = self.get_u2(device_id)
        if not d:
            return None
        try:
            elem = d(**selector)
            if elem.wait(timeout=timeout):
                return elem.get_text()
        except Exception as e:
            self.logger.debug("u2_get_text 获取失败 %s: %s", device_id, e)
        return None

    def u2_click_multi(self, device_id: str,
                       strategies: List[Dict[str, Any]],
                       timeout: float = 15.0) -> bool:
        """
        多策略查找并点击（u2版本）。
        按顺序尝试每组 selector，任一命中即点击并返回 True。
        在 timeout 内轮询。
        """
        d = self.get_u2(device_id)
        if not d:
            return False

        start = time.time()
        while time.time() - start < timeout:
            for sel in strategies:
                try:
                    elem = d(**sel)
                    if elem.exists:
                        elem.click()
                        self.logger.info(f"u2 多策略点击成功: {sel}")
                        return True
                except Exception as e:
                    self.logger.debug("u2_click_multi 策略尝试失败 %s sel=%s: %s", device_id, sel, e)
                    continue
            time.sleep(0.5)

        self.logger.warning(f"u2 多策略点击超时 ({timeout}s): {strategies}")
        return False

    def u2_find_multi(self, device_id: str,
                      strategies: List[Dict[str, Any]],
                      timeout: float = 15.0) -> Optional[Any]:
        """多策略查找元素（不点击），返回 u2 UiObject 或 None"""
        d = self.get_u2(device_id)
        if not d:
            return None

        start = time.time()
        while time.time() - start < timeout:
            for sel in strategies:
                try:
                    elem = d(**sel)
                    if elem.exists:
                        return elem
                except Exception as e:
                    self.logger.debug("u2_find_multi 策略尝试失败 %s sel=%s: %s", device_id, sel, e)
                    continue
            time.sleep(0.5)
        return None

    # =========================================================================
    # Legacy UI Automator (ADB dump fallback)
    # =========================================================================

    def dump_ui_hierarchy(self, device_id: str) -> Optional[str]:
        dump_path = '/data/local/tmp/_ui_dump.xml'
        ok, out = self._run_adb(
            ['shell', 'uiautomator', 'dump', dump_path], device_id, timeout=15
        )
        if not ok:
            self.logger.error(f"UI dump 失败: {out}")
            return None
        ok, xml = self._run_adb(['shell', 'cat', dump_path], device_id)
        self._run_adb(['shell', 'rm', '-f', dump_path], device_id)
        return xml if ok and xml else None

    @staticmethod
    def _parse_ui_elements(xml_content: str) -> List[UIElement]:
        elements: List[UIElement] = []
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError:
            return elements
        for node in root.iter('node'):
            bs = node.get('bounds', '')
            m = _BOUNDS_PATTERN.match(bs)
            if not m:
                continue
            elements.append(UIElement(
                resource_id=node.get('resource-id', ''),
                text=node.get('text', ''),
                content_desc=node.get('content-desc', ''),
                class_name=node.get('class', ''),
                package=node.get('package', ''),
                bounds=(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))),
                clickable=node.get('clickable', 'false') == 'true',
                enabled=node.get('enabled', 'true') == 'true',
            ))
        return elements

    @staticmethod
    def _filter_elements(elements: List[UIElement], **criteria) -> List[UIElement]:
        results = []
        rid = criteria.get('resource_id')
        text = criteria.get('text')
        text_contains = criteria.get('text_contains')
        desc = criteria.get('content_desc')
        desc_contains = criteria.get('content_desc_contains')
        cls = criteria.get('class_name')
        pkg = criteria.get('package')
        click = criteria.get('clickable')
        enab = criteria.get('enabled')

        for e in elements:
            if rid is not None:
                if '/' in rid:
                    if e.resource_id != rid:
                        continue
                else:
                    short = e.resource_id.split('/')[-1] if '/' in e.resource_id else e.resource_id
                    if short != rid:
                        continue
            if text is not None and e.text != text:
                continue
            if text_contains is not None and text_contains.lower() not in e.text.lower():
                continue
            if desc is not None and e.content_desc != desc:
                continue
            if desc_contains is not None and desc_contains.lower() not in e.content_desc.lower():
                continue
            if cls is not None and cls not in e.class_name:
                continue
            if pkg is not None and pkg not in e.package:
                continue
            if click is not None and e.clickable != click:
                continue
            if enab is not None and e.enabled != enab:
                continue
            results.append(e)
        return results

    def find_element(self, device_id: str, xml_cache: Optional[str] = None,
                     **criteria) -> Optional[UIElement]:
        xml = xml_cache or self.dump_ui_hierarchy(device_id)
        if not xml:
            return None
        all_elems = self._parse_ui_elements(xml)
        filtered = self._filter_elements(all_elems, **criteria)
        return filtered[0] if filtered else None

    def find_elements(self, device_id: str, xml_cache: Optional[str] = None,
                      **criteria) -> List[UIElement]:
        xml = xml_cache or self.dump_ui_hierarchy(device_id)
        if not xml:
            return []
        return self._filter_elements(self._parse_ui_elements(xml), **criteria)

    def find_element_multi(self, device_id: str,
                           strategies: List[Dict[str, Any]],
                           timeout: float = 15.0,
                           interval: float = 1.0) -> Optional[UIElement]:
        start = time.time()
        while time.time() - start < timeout:
            xml = self.dump_ui_hierarchy(device_id)
            if xml:
                for s in strategies:
                    elem = self.find_element(device_id, xml_cache=xml, **s)
                    if elem:
                        return elem
            time.sleep(interval)
        return None

    def find_and_tap(self, device_id: str, **criteria) -> bool:
        elem = self.find_element(device_id, **criteria)
        if not elem:
            return False
        return self.input_tap(device_id, *elem.center)

    def wait_and_tap(self, device_id: str, timeout: float = 15.0, **criteria) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            elem = self.find_element(device_id, **criteria)
            if elem:
                return self.input_tap(device_id, *elem.center)
            time.sleep(1.0)
        return False

    def tap_element_multi(self, device_id: str,
                          strategies: List[Dict[str, Any]],
                          timeout: float = 15.0) -> bool:
        elem = self.find_element_multi(device_id, strategies, timeout)
        if not elem:
            return False
        return self.input_tap(device_id, *elem.center)

    def get_current_activity(self, device_id: str) -> Optional[str]:
        ok, out = self.execute_adb_command("shell dumpsys window windows", device_id)
        if ok and out:
            for line in out.split('\n'):
                if 'mCurrentFocus' in line or 'mFocusedApp' in line:
                    if 'ActivityRecord' in line:
                        for part in line.split(' '):
                            if '/' in part and '.' in part:
                                return part.strip()
        return None


# =============================================================================
# 单例
# =============================================================================

_device_manager_instance = None

def get_device_manager(config_path: Optional[str] = None) -> DeviceManager:
    global _device_manager_instance
    if _device_manager_instance is None:
        _device_manager_instance = DeviceManager(config_path)
    return _device_manager_instance


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    from src.host.device_registry import DEFAULT_DEVICES_YAML

    manager = get_device_manager(DEFAULT_DEVICES_YAML)

    for did in manager.discover_devices():
        info = manager.get_device_info(did)
        if not info:
            continue
        print(f"设备: {info.display_name} ({did})")
        print(f"  u2可用: {manager.get_u2(did) is not None}")

        d = manager.get_u2(did)
        if d:
            print(f"  u2设备信息: {d.device_info}")
