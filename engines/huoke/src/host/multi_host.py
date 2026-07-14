# -*- coding: utf-8 -*-
"""
分布式多主机管理 — 多 PC 统一汇聚控制。

架构:
  - 每台 PC 运行 OpenClaw 实例 (Worker)
  - 其中一台可选作"中心节点" (Coordinator)
  - Worker 定期向 Coordinator 发送心跳 + 设备/任务状态
  - Coordinator 聚合所有 Worker 的数据，提供统一 Dashboard 视图
  - 支持跨主机任务路由

数据流:
  Worker → POST /cluster/heartbeat → Coordinator (聚合)
  Dashboard → GET /cluster/overview → Coordinator (统一视图)
  Dashboard → POST /cluster/dispatch → Coordinator → Worker (路由任务)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.openclaw_env import DEFAULT_OPENCLAW_PORT
from src.host.device_registry import DEFAULT_DEVICES_YAML, config_file

log = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 15
_HOST_TIMEOUT = 45


@dataclass
class HostInfo:
    """Information about a connected host."""
    host_id: str
    host_name: str = ""
    host_ip: str = ""
    port: int = DEFAULT_OPENCLAW_PORT
    devices: List[dict] = field(default_factory=list)
    tasks_active: int = 0
    tasks_completed: int = 0
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    ips: List[str] = field(default_factory=list)
    last_heartbeat: float = 0.0
    online: bool = True
    version: str = ""


class ClusterCoordinator:
    """
    Aggregates state from multiple OpenClaw workers.
    Runs on one designated PC; others send heartbeats to it.
    """

    _PERSIST_PATH = "config/cluster_state.json"

    def __init__(self):
        self._lock = threading.Lock()
        self._hosts: Dict[str, HostInfo] = {}
        cfg = load_cluster_config()
        self._secret = cfg.get("shared_secret", "")
        self._load_persisted_state()

    def receive_heartbeat(self, data: dict) -> dict:
        """Process heartbeat from a worker host."""
        host_id = data.get("host_id", "")
        if not host_id:
            return {"error": "host_id required"}

        _drop_data = None
        with self._lock:
            if host_id not in self._hosts:
                self._hosts[host_id] = HostInfo(host_id=host_id)
                log.info("[Cluster] 新主机上线: %s (%s)",
                         data.get("host_name", host_id[:8]),
                         data.get("host_ip", "?"))

            host = self._hosts[host_id]
            was_offline = not host.online
            old_online = sum(
                1 for d in (host.devices or []) if d.get("status") == "connected"
            )
            old_total = len(host.devices or [])
            host.host_name = data.get("host_name", host.host_name)
            host.host_ip = data.get("host_ip", host.host_ip)
            host.port = data.get("port", host.port)
            host.devices = data.get("devices", [])
            host.tasks_active = data.get("tasks_active", 0)
            host.tasks_completed = data.get("tasks_completed", 0)
            host.cpu_usage = data.get("cpu_usage", 0)
            host.memory_usage = data.get("memory_usage", 0)
            host.last_heartbeat = time.time()
            host.ips = data.get("ips", [])
            host.online = True
            host.version = data.get("version", "")
            if was_offline:
                log.info("[Cluster] 主机恢复在线: %s", host_id)
                try:
                    from .event_stream import push_event
                    push_event("worker.online", {
                        "host_id": host_id,
                        "host_name": host.host_name,
                        "host_ip": host.host_ip,
                    })
                except Exception:
                    pass

            new_online = sum(
                1 for d in (host.devices or []) if d.get("status") == "connected"
            )
            new_total = len(host.devices or [])
            if (
                not was_offline
                and old_online > 0
                and new_online < old_online
            ):
                _drop_data = (host.host_name or host_id, old_online, new_online, old_total, new_total)

        self._persist_state()

        if _drop_data:
            try:
                _cfg_role = str(load_cluster_config().get("role", "standalone")).lower()
                if _cfg_role == "coordinator":
                    from src.host.alert_notifier import AlertNotifier
                    _an = AlertNotifier.get()
                    if _an.worker_online_drop_telegram_enabled():
                        _hn, _o, _n, _ot, _nt = _drop_data
                        _an.notify_event(
                            "cluster.worker_online_drop",
                            "",
                            "",
                            "warning",
                            alert_code="CLUSTER_WORKER_ONLINE_DROP",
                            params={
                                "host": _hn,
                                "adb_o": str(_o),
                                "adb_n": str(_n),
                                "reg_o": str(_ot),
                                "reg_n": str(_nt),
                            },
                        )
            except Exception as e:
                log.debug("[Cluster] Worker 在线数下降 Telegram 跳过: %s", e)

        return {"status": "ok", "host_id": host_id}

    def get_overview(self) -> dict:
        """Return aggregated cluster overview."""
        self._refresh_online_status()
        with self._lock:
            hosts = []
            total_devices = 0
            total_online = 0
            total_tasks = 0

            for h in self._hosts.values():
                device_count = len(h.devices)
                online_count = sum(1 for d in h.devices
                                   if d.get("status") == "connected")
                hosts.append({
                    "host_id": h.host_id,
                    "host_name": h.host_name,
                    "host_ip": h.host_ip,
                    "port": h.port,
                    "online": h.online,
                    "devices": device_count,
                    "devices_online": online_count,
                    "tasks_active": h.tasks_active,
                    "tasks_completed": h.tasks_completed,
                    "cpu_usage": round(h.cpu_usage, 1),
                    "memory_usage": round(h.memory_usage, 1),
                    "last_heartbeat": h.last_heartbeat,
                    "version": h.version,
                })
                total_devices += device_count
                total_online += online_count
                total_tasks += h.tasks_active

            # 主控本机 USB 不会出现在 Worker 心跳里；合并计数避免「只显示 W03 的 26 台」
            coord_usb_n = 0
            coord_usb_on = 0
            try:
                import yaml as _yaml
                _cy = config_file("cluster.yaml")
                if _cy.exists():
                    with open(_cy, encoding="utf-8") as f:
                        _cc = _yaml.safe_load(f) or {}
                    if str(_cc.get("role", "")).lower() == "coordinator":
                        from src.device_control.device_manager import get_device_manager
                        _dm = get_device_manager(DEFAULT_DEVICES_YAML)
                        _dm.discover_devices(force=True)
                        _cds = _dm.get_connected_devices() or []
                        coord_usb_n = len(_cds)
                        coord_usb_on = coord_usb_n
            except Exception:
                pass

            return {
                "hosts": hosts,
                "total_hosts": len(self._hosts),
                "hosts_online": sum(1 for h in self._hosts.values() if h.online),
                "total_devices": total_devices + coord_usb_n,
                "total_devices_online": total_online + coord_usb_on,
                "total_tasks_active": total_tasks,
                "worker_devices_total": total_devices,
                "coordinator_usb_devices": coord_usb_n,
                "count_legend": (
                    "Worker: devices_online=发心跳前经 adb 校准后的在线台数, "
                    "devices=心跳中的设备条数(含已断条); "
                    "本机USB=主控 discover 在线台数"
                ),
            }

    def refresh_all_devices(self) -> int:
        """主动查询所有在线 Worker 的设备列表，更新内存中的设备数据。

        Returns:
            int: 从所有 Worker 获取的设备总数
        """
        import urllib.request
        total = 0
        with self._lock:
            hosts = [(h.host_id, h.host_ip, h.port, h.online) for h in self._hosts.values()]

        for host_id, host_ip, port, online in hosts:
            if not online:
                continue
            # 尝试多个可能的 URL（包括所有已知 IP）
            urls_to_try = []
            with self._lock:
                h = self._hosts.get(host_id)
                if h and h.ips:
                    for ip in h.ips:
                        urls_to_try.append(f"http://{ip}:{port}")
            if host_ip:
                url = f"http://{host_ip}:{port}"
                if url not in urls_to_try:
                    urls_to_try.insert(0, url)
            if not urls_to_try:
                continue

            for base_url in urls_to_try:
                try:
                    url = f"{base_url}/devices"
                    req = urllib.request.Request(url, method="GET")
                    # 添加集群密钥认证
                    if self._secret:
                        req.add_header("X-Cluster-Secret", self._secret)
                    req.add_header("Connection", "close")
                    resp = urllib.request.urlopen(req, timeout=10)
                    data = json.loads(resp.read().decode())
                    resp.close()
                    # Worker /devices 可能返回数组或 {"devices": [...]}
                    devices = data if isinstance(data, list) else data.get("devices", [])
                    if isinstance(devices, list) and devices:
                        # 标准化设备数据
                        normalized = []
                        for d in devices:
                            if isinstance(d, dict):
                                normalized.append(d)
                            elif isinstance(d, str):
                                normalized.append({"device_id": d, "status": "unknown"})

                        with self._lock:
                            if host_id in self._hosts:
                                self._hosts[host_id].devices = normalized
                        total += len(normalized)
                        log.info("[Cluster] 从 %s 刷新了 %d 个设备", host_id, len(normalized))
                    break  # 成功就不再尝试其他 URL
                except Exception as e:
                    log.debug("[Cluster] 从 %s (%s) 查询设备失败: %s", host_id, base_url, e)

        return total

    def reverse_probe_worker(self, host_id: str,
                              timeout: float = 5.0) -> bool:
        """主动 GET worker /devices, 成功则注册成 online via heartbeat.

        2026-05-05 Stage I.1: 解决 worker HeartbeatSender 没在 push 心跳但
        worker server 实际活着的场景 (Stage B 真机验证发现 W03/W175 直接
        HTTP 200 但 last_heartbeat 944 分钟前 → /cluster/devices 返 0).

        本函数主动 probe **已知**历史 worker (cluster_state.json 持久化的
        host_id), 不主动扫局域网 (安全 + 不浪费网络).

        失败的 host 保持 offline 状态.

        Args:
            host_id: 要 probe 的 worker host_id (必须在 self._hosts 里)
            timeout: HTTP timeout 秒数

        Returns:
            True 如果 probe 成功 + 注册 online; False 否则
        """
        import urllib.request
        with self._lock:
            h = self._hosts.get(host_id)
            if h is None:
                return False
            host_ip = h.host_ip
            port = h.port or DEFAULT_OPENCLAW_PORT
            host_name = h.host_name
        # coordinator 自己不 probe; 没 IP 的 host 不 probe (没法连)
        if not host_ip or host_id == "coordinator":
            return False

        url = f"http://{host_ip}:{port}/devices"
        try:
            req = urllib.request.Request(url, method="GET")
            if self._secret:
                req.add_header("X-Cluster-Secret", self._secret)
            req.add_header("Connection", "close")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            devices = data if isinstance(data, list) else data.get("devices", [])
            if not isinstance(devices, list):
                devices = []
        except Exception as e:
            log.debug("[Cluster] reverse probe %s @ %s 失败: %s",
                      host_id, host_ip, e)
            return False

        # 标准化设备数据 (与 refresh_all_devices 同处理)
        normalized = []
        for d in devices:
            if isinstance(d, dict):
                normalized.append(d)
            elif isinstance(d, str):
                normalized.append({"device_id": d, "status": "unknown"})

        # 注册成 online via 内部 heartbeat 路径 (复用 receive_heartbeat 的
        # online state machine + persist + event_stream worker.online).
        self.receive_heartbeat({
            "host_id": host_id,
            "host_name": host_name,
            "host_ip": host_ip,
            "port": port,
            "devices": normalized,
            "tasks_active": 0,
            "tasks_completed": 0,
            "cpu_usage": 0.0,
            "memory_usage": 0.0,
            "version": "",
            "secret": self._secret,
            "_via": "reverse_probe",
        })
        log.info("[Cluster] reverse probe 成功: %s @ %s — %d devices",
                 host_id, host_ip, len(normalized))
        return True

    def get_all_devices(self) -> List[dict]:
        """Return unified device list across all hosts."""
        self._refresh_online_status()
        devices = []
        with self._lock:
            for h in self._hosts.values():
                if not h.online:
                    continue
                for d in h.devices:
                    d_copy = dict(d)
                    d_copy["host_id"] = h.host_id
                    d_copy["host_name"] = h.host_name
                    d_copy["host_ip"] = h.host_ip
                    d_copy["host_port"] = h.port
                    devices.append(d_copy)

        # 备选: 如果设备列表为空且有在线 Worker，主动刷新
        if not devices:
            online_count = sum(1 for h in self._hosts.values() if h.online)
            if online_count > 0:
                log.info("[Cluster] 设备列表为空但有 %d 个在线 Worker，尝试主动刷新...", online_count)
                self.refresh_all_devices()
                # 重新收集
                with self._lock:
                    for h in self._hosts.values():
                        if not h.online:
                            continue
                        for d in h.devices:
                            d_copy = dict(d) if isinstance(d, dict) else {"device_id": str(d)}
                            d_copy["host_id"] = h.host_id
                            d_copy["host_name"] = h.host_name
                            d_copy["host_ip"] = h.host_ip
                            d_copy["host_port"] = h.port
                            devices.append(d_copy)

        return devices

    def select_host_for_task(self, task_type: str = "",
                             preferred_device: str = "",
                             platform: str = "") -> Optional[dict]:
        """
        Multi-factor host selection:
        1. Device affinity: route to host that owns preferred_device
        2. Platform affinity: for platform-specific tasks, prefer hosts with
           devices running that platform's app
        3. Load balancing: health score - active tasks penalty
        4. CPU/memory headroom bonus
        """
        self._refresh_online_status()
        with self._lock:
            online_hosts = [h for h in self._hosts.values() if h.online]
            if not online_hosts:
                return None

            if preferred_device:
                for h in online_hosts:
                    for d in h.devices:
                        if d.get("device_id") == preferred_device:
                            return self._host_result(h)

            if not platform and task_type:
                for prefix in ("tiktok_", "telegram_", "whatsapp_", "facebook_"):
                    if task_type.startswith(prefix):
                        platform = prefix.rstrip("_")
                        break

            def _host_score(h):
                load_penalty = h.tasks_active * 10
                avg_health = 50
                connected = [d for d in h.devices
                             if d.get("status") == "connected"]
                if connected:
                    scores = [d.get("health_score", 50) for d in connected]
                    avg_health = sum(scores) / len(scores)

                cpu_bonus = max(0, (100 - h.cpu_usage) * 0.1)
                mem_bonus = max(0, (100 - h.memory_usage) * 0.05)
                device_count_bonus = len(connected) * 2

                return avg_health - load_penalty + cpu_bonus + mem_bonus + device_count_bonus

            best = max(online_hosts, key=_host_score)
            return self._host_result(best)

    @staticmethod
    def _host_result(h: HostInfo) -> dict:
        return {
            "host_id": h.host_id,
            "host_name": h.host_name,
            "host_ip": h.host_ip,
            "port": h.port,
            "url": f"http://{h.host_ip}:{h.port}",
        }

    def remove_host(self, host_id: str):
        with self._lock:
            self._hosts.pop(host_id, None)
        self._persist_state()

    def _persist_state(self):
        """Save minimal host info to disk for fast recovery after restart."""
        try:
            import json
            from pathlib import Path
            data = {}
            with self._lock:
                for hid, h in self._hosts.items():
                    data[hid] = {
                        "host_name": h.host_name,
                        "host_ip": h.host_ip,
                        "port": h.port,
                        "last_heartbeat": h.last_heartbeat,
                    }
            p = Path(self._PERSIST_PATH)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            log.debug("[Cluster] 持久化失败: %s", e)

    def _load_persisted_state(self):
        """Load previously known hosts so Coordinator knows about workers
        immediately after restart, before they send new heartbeats."""
        try:
            import json
            from pathlib import Path
            p = Path(self._PERSIST_PATH)
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            for hid, info in data.items():
                h = HostInfo(host_id=hid)
                h.host_name = info.get("host_name", "")
                h.host_ip = info.get("host_ip", "")
                h.port = info.get("port", DEFAULT_OPENCLAW_PORT)
                h.last_heartbeat = info.get("last_heartbeat", 0)
                h.online = False
                self._hosts[hid] = h
            if data:
                log.info("[Cluster] 从缓存恢复 %d 个已知主机（等待心跳上线）",
                         len(data))
        except Exception as e:
            log.debug("[Cluster] 加载持久化状态失败: %s", e)

    def _refresh_online_status(self):
        now = time.time()
        with self._lock:
            for h in self._hosts.values():
                if now - h.last_heartbeat > _HOST_TIMEOUT:
                    if h.online:
                        log.warning("[Cluster] 主机离线: %s (%s)",
                                    h.host_name, h.host_id[:8])
                    h.online = False
                    try:
                        from .event_stream import push_event
                        push_event("worker.offline", {
                            "host_id": h.host_id,
                            "host_name": h.host_name,
                            "host_ip": h.host_ip,
                            "last_heartbeat": h.last_heartbeat,
                        })
                    except Exception:
                        pass


class HeartbeatSender:
    """
    Runs on worker hosts. Periodically sends heartbeat to coordinator.
    """

    _consecutive_failures: int = 0
    _standalone_mode: bool = False
    _last_collected_data: Optional[dict] = None
    _collect_timeout: float = 5.0
    # 2026-05-05 Stage N.1: auto-trigger reverse probe cooldown 防 spam
    _last_auto_trigger_at: float = 0.0
    _AUTO_TRIGGER_COOLDOWN_SEC: float = 300.0  # 5 min

    def __init__(self, coordinator_url: str, local_port: int = DEFAULT_OPENCLAW_PORT,
                 interval: int = _HEARTBEAT_INTERVAL):
        self._coordinator_url = coordinator_url.rstrip("/")
        self._local_port = local_port
        self._interval = interval
        self._host_id = self._generate_host_id()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._consecutive_failures = 0
        self._standalone_mode = False
        self._last_collected_data = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="heartbeat-sender")
        self._thread.start()
        log.info("[Cluster] 心跳发送已启动 → %s", self._coordinator_url)

    def stop(self):
        self._stop.set()

    def is_standalone_mode(self) -> bool:
        """Returns True if this worker has lost contact with coordinator (degraded mode)."""
        return self._standalone_mode

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._send_heartbeat()
                self._consecutive_failures = 0
                if self._standalone_mode:
                    log.info("[Cluster] 心跳已恢复，退出降级模式")
                    self._standalone_mode = False
            except Exception as e:
                self._consecutive_failures += 1
                if self._standalone_mode:
                    log.debug("[Cluster] 心跳发送失败 (降级模式): %s", e)
                else:
                    log.warning("[Cluster] 心跳发送失败: %s", e)
                if self._consecutive_failures >= 3 and not self._standalone_mode:
                    log.warning("[Cluster] 连续 %d 次心跳失败，进入降级独立模式",
                                self._consecutive_failures)
                    self._standalone_mode = True
                    # Stage N.1: 进入降级时立即尝试 trigger reverse probe
                    # (容灾响应从 worker push 30s 间隔 → 主控 1 tick 内接管)
                    self._try_auto_trigger()
            # Stage N.1: 持续 standalone 期间周期性 retry trigger
            # (单次 trigger 失败后, 5min cooldown 后再试; 直到 push 心跳恢复
            # 退出 standalone_mode)
            if self._standalone_mode:
                self._try_auto_trigger()
            self._stop.wait(self._interval)

    def _try_auto_trigger(self) -> bool:
        """Stage N.1 — push 心跳挂掉时通知主控反向 probe.

        步骤:
          1. cooldown 检查 (5min 一次, 防 spam)
          2. GET 主控 /health 预检 (双向网断时 skip 不浪费)
          3. POST /cluster/reverse-probe/trigger 让主控 immediate probe

        签名复用 _send_heartbeat 模式 (HMAC-SHA256 over host_id:ts).
        全部失败被 catch — 不影响心跳主循环.

        Returns:
            True 如果 trigger 成功推到主控 (主控收到 200);
            False 如果 cooldown / 网断 / 主控 5xx / 签名拒.
        """
        import urllib.request
        import json
        import time as _time
        now = _time.time()
        if (now - self._last_auto_trigger_at) < self._AUTO_TRIGGER_COOLDOWN_SEC:
            return False  # cooldown 内不重复 trigger
        self._last_auto_trigger_at = now

        # Step 1: 主控可达性预检 (避免双向网断时浪费 5s timeout)
        health_url = f"{self._coordinator_url}/health"
        try:
            req = urllib.request.Request(health_url, method="GET")
            req.add_header("Connection", "close")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status >= 400:
                    log.debug("[Cluster] auto-trigger 主控 /health %d, skip",
                              resp.status)
                    return False
        except Exception as e:
            log.debug("[Cluster] auto-trigger 主控 /health 不可达 (双向网断): %s", e)
            return False

        # Step 2: 主控可达, 调 trigger endpoint
        cfg = load_cluster_config()
        secret = cfg.get("shared_secret", "")
        # P.2: worker auto-trigger 用 async 模式 — endpoint 立即返
        # {"queued":[...]} 不阻塞 worker 心跳循环 (默认 sync 等 5s probe).
        body = {"host_id": self._host_id, "wait": False}
        if secret:
            import hmac as _hmac
            import hashlib as _hashlib
            ts = str(now)
            sig = _hmac.new(
                secret.encode(), f"{self._host_id}:{ts}".encode(),
                _hashlib.sha256,
            ).hexdigest()
            body["_sig"] = sig
            body["_ts"] = ts

        trigger_url = (
            f"{self._coordinator_url}/cluster/reverse-probe/trigger")
        try:
            payload = json.dumps(body).encode()
            req = urllib.request.Request(
                trigger_url, data=payload,
                headers={"Content-Type": "application/json",
                         "Connection": "close"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status < 400:
                    log.info(
                        "[Cluster] auto-trigger 成功 (push 心跳挂掉, "
                        "主控反向 probe 已接管)")
                    return True
        except Exception as e:
            log.debug("[Cluster] auto-trigger POST 失败: %s", e)
        return False

    def _send_heartbeat(self):
        import urllib.request
        import json
        import hmac as _hmac
        import hashlib as _hashlib
        import time as _time
        import concurrent.futures as _cf

        # Collect with timeout to avoid blocking heartbeat
        with _cf.ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(self._collect_status)
            try:
                data = fut.result(timeout=self._collect_timeout)
                self._last_collected_data = data
            except _cf.TimeoutError:
                log.warning("[Cluster] Status collection timed out, using last known data")
                data = self._last_collected_data or self._minimal_status()

        # Add HMAC-SHA256 signing to payload
        secret = data.pop("secret", "")  # remove plain secret if present
        if not secret:
            cfg = load_cluster_config()
            secret = cfg.get("shared_secret", "")
        if secret:
            host_id = data.get("host_id", "")
            ts = str(_time.time())
            sig = _hmac.new(
                secret.encode(),
                f"{host_id}:{ts}".encode(),
                _hashlib.sha256
            ).hexdigest()
            data["_sig"] = sig
            data["_ts"] = ts

        payload = json.dumps(data).encode()
        req = urllib.request.Request(
            f"{self._coordinator_url}/cluster/heartbeat",
            data=payload,
            headers={"Content-Type": "application/json", "Connection": "close"},
            method="POST",
        )
        # 2026-05-05 Stage N.1: 修 swallow bug — HTTP exception 必须 raise
        # 给 _loop 让 _consecutive_failures 累加进入 standalone_mode (Stage B
        # 真机发现根因: 旧版 try: urlopen except: log.warning 让 fail 沉默,
        # _consecutive_failures 永远 0, standalone 永不进, auto-trigger 永不触).
        resp = urllib.request.urlopen(req, timeout=10)
        try:
            resp.read()
        finally:
            resp.close()

    def _collect_status(self) -> dict:
        """Collect local status for heartbeat."""
        import socket

        devices = []
        tasks_active = 0
        tasks_completed = 0

        try:
            from src.host.health_monitor import metrics
            for did, status in metrics.device_status.items():
                dev_info = {
                    "device_id": did,
                    "display_name": status.get("display_name", ""),
                    "status": status.get("status", "unknown"),
                }
                try:
                    score = metrics.device_health_score(did)
                    dev_info["health_score"] = score.get("total", 0)
                except Exception:
                    pass
                try:
                    from src.host.health_monitor import _monitor
                    if _monitor and hasattr(_monitor, '_wifi_backup'):
                        wifi = _monitor._wifi_backup.get(did)
                        dev_info["wifi_backup"] = wifi or ""
                except Exception:
                    pass
                devices.append(dev_info)
            tasks_completed = metrics.tasks_total
        except Exception:
            pass

        # 用当前 adb 在线集合校准 status（metrics 由 HealthMonitor 周期更新，默认约 60s 才变；心跳约 10s 一发，避免「已掉线仍显示 26 在线」）
        try:
            from pathlib import Path as _Path
            from src.device_control.device_manager import get_device_manager

            _mgr = get_device_manager(DEFAULT_DEVICES_YAML)
            _mgr.discover_devices(force=True)
            _live = {d.device_id for d in (_mgr.get_connected_devices() or [])}
            _by_id = {d["device_id"]: d for d in devices}
            for _d in devices:
                _id = _d.get("device_id", "")
                if not _id:
                    continue
                if _id in _live:
                    _d["status"] = "connected"
                else:
                    if (_d.get("status") or "") == "connected":
                        _d["status"] = "disconnected"
            for _id in _live:
                if _id not in _by_id:
                    devices.append(
                        {
                            "device_id": _id,
                            "display_name": _id[:8],
                            "status": "connected",
                        }
                    )
        except Exception:
            pass

        try:
            from src.host.worker_pool import get_worker_pool
            pool = get_worker_pool()
            st = pool.get_status()
            tasks_active = st.get("active_count", 0)
        except Exception:
            pass

        cpu = 0.0
        mem = 0.0
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0)
            mem = psutil.virtual_memory().percent
        except ImportError:
            pass

        cfg = load_cluster_config()
        host_name = getattr(self, "_host_name_override", "") or cfg.get("host_name", "") or platform.node()
        result = {
            "host_id": self._host_id,
            "host_name": host_name,
            "host_ip": cfg.get("advertise_ip", "") or self._get_local_ip(),
            "ips": self._get_all_ips(),
            "port": self._local_port,
            "devices": devices,
            "tasks_active": tasks_active,
            "tasks_completed": tasks_completed,
            "cpu_usage": cpu,
            "memory_usage": mem,
            "version": "1.0.0",
        }
        # 恢复状态信息
        recovery_info = {}
        try:
            from src.host.health_monitor import _monitor
            if _monitor:
                recovery_state = getattr(_monitor, '_recovery_state', {})
                disconnected = getattr(_monitor, '_disconnected_devices', set())
                recovery_info = {
                    "disconnected_count": len(disconnected),
                    "recovering_devices": list(disconnected)[:10],
                }
        except Exception:
            pass
        result["recovery"] = recovery_info

        secret = cfg.get("shared_secret", "")
        if secret:
            result["secret"] = secret
        return result

    def _minimal_status(self) -> dict:
        """Return bare-minimum status payload when full collection times out."""
        cfg = load_cluster_config()
        return {
            "host_id": self._host_id,
            "host_name": cfg.get("host_name", ""),
            "host_ip": self._get_local_ip(),
            "port": self._local_port,
            "devices": [],
            "tasks_active": 0,
            "tasks_completed": 0,
        }

    @staticmethod
    def _get_local_ip() -> str:
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    @staticmethod
    def _get_all_ips() -> list:
        """获取所有网卡的 IPv4 地址。"""
        import socket
        ips = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ip and not ip.startswith('127.'):
                    if ip not in ips:
                        ips.append(ip)
        except Exception:
            pass
        # 也尝试获取默认路由 IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            default_ip = s.getsockname()[0]
            s.close()
            if default_ip not in ips:
                ips.insert(0, default_ip)
        except Exception:
            pass
        return ips

    @staticmethod
    def _generate_host_id() -> str:
        cfg = load_cluster_config()
        custom_id = cfg.get("host_id", "")
        if custom_id:
            return custom_id
        raw = f"{platform.node()}-{platform.machine()}"
        try:
            import uuid as _uuid
            raw += f"-{_uuid.getnode()}"
        except Exception:
            pass
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


_coordinator: Optional[ClusterCoordinator] = None
_coord_lock = threading.Lock()
_sender: Optional[HeartbeatSender] = None
_sender_lock = threading.Lock()
_cluster_config: Optional[Dict[str, Any]] = None


def load_cluster_config() -> Dict[str, Any]:
    """Load cluster config from config/cluster.yaml."""
    global _cluster_config
    if _cluster_config is not None:
        return _cluster_config
    cfg_path = config_file("cluster.yaml")
    default = {
        "role": "standalone",
        "coordinator_url": "",
        "local_port": DEFAULT_OPENCLAW_PORT,
        "shared_secret": "",
        "heartbeat_interval": 10,
        "host_timeout": 30,
        "auto_join": True,
        "host_name": "",
        "host_id": "",
        "advertise_ip": "",
        # 2026-05-05 Stage K.2: reverse heartbeat prober 配置
        # interval=默认 30s probe stale host
        # max_interval=300s 退避上限 (probe 持续失败时, 5min 一次足够)
        # backoff_multiplier=2.0 失败一次间隔翻倍 (30→60→120→240→300 cap)
        "reverse_probe_interval": 30,
        "reverse_probe_max_interval": 300,
        "reverse_probe_backoff_multiplier": 2.0,
        "reverse_probe_startup_delay": 10,
    }
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            default.update(loaded)
        except ImportError:
            try:
                text = cfg_path.read_text(encoding="utf-8")
                for line in text.splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if ":" in line:
                        k, v = line.split(":", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k in default:
                            if isinstance(default[k], bool):
                                default[k] = v.lower() in ("true", "yes", "1")
                            elif isinstance(default[k], int):
                                try:
                                    default[k] = int(v)
                                except ValueError:
                                    pass
                            else:
                                default[k] = v
            except Exception as e:
                log.debug("Failed to parse cluster config: %s", e)
    # 环境变量回退: shared_secret
    if not default.get("shared_secret"):
        env_secret = os.environ.get("OPENCLAW_CLUSTER_SECRET", "")
        if env_secret:
            default["shared_secret"] = env_secret
        else:
            # 自动生成随机密钥
            import secrets
            generated = secrets.token_hex(16)
            default["shared_secret"] = generated
            log.info("[Cluster] shared_secret 为空，已自动生成随机密钥")

    _cluster_config = default
    return _cluster_config


def save_cluster_config(config: Dict[str, Any]):
    """Save cluster config back to YAML."""
    global _cluster_config
    cfg_path = config_file("cluster.yaml")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# OpenClaw 集群配置",
        f"role: {config.get('role', 'standalone')}",
        f"coordinator_url: \"{config.get('coordinator_url', '')}\"",
        f"local_port: {config.get('local_port', DEFAULT_OPENCLAW_PORT)}",
        f"shared_secret: \"{config.get('shared_secret', '')}\"",
        f"heartbeat_interval: {config.get('heartbeat_interval', 10)}",
        f"host_timeout: {config.get('host_timeout', 30)}",
        f"auto_join: {'true' if config.get('auto_join') else 'false'}",
        f"host_name: \"{config.get('host_name', '')}\"",
        f"host_id: \"{config.get('host_id', '')}\"",
    ]
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _cluster_config = config


def get_cluster_coordinator() -> ClusterCoordinator:
    global _coordinator
    if _coordinator is None:
        with _coord_lock:
            if _coordinator is None:
                cfg = load_cluster_config()
                _coordinator = ClusterCoordinator()
                if cfg.get("host_timeout"):
                    global _HOST_TIMEOUT
                    _HOST_TIMEOUT = cfg["host_timeout"]
    return _coordinator


def start_heartbeat_sender(coordinator_url: str,
                           local_port: int = DEFAULT_OPENCLAW_PORT) -> HeartbeatSender:
    global _sender
    with _sender_lock:
        if _sender:
            _sender.stop()
        cfg = load_cluster_config()
        interval = cfg.get("heartbeat_interval", _HEARTBEAT_INTERVAL)
        host_name = cfg.get("host_name", "")
        _sender = HeartbeatSender(coordinator_url, local_port, interval=interval)
        if host_name:
            _sender._host_name_override = host_name
        _sender.start()
    return _sender


def auto_start_cluster():
    """Called at startup — auto-join cluster if configured."""
    cfg = load_cluster_config()
    role = cfg.get("role", "standalone")
    if role == "worker" and cfg.get("auto_join") and cfg.get("coordinator_url"):
        log.info("[Cluster] Auto-joining as worker → %s",
                 cfg["coordinator_url"])
        start_heartbeat_sender(cfg["coordinator_url"],
                               cfg.get("local_port", DEFAULT_OPENCLAW_PORT))
    elif role == "coordinator":
        log.info("[Cluster] Starting as coordinator")
        get_cluster_coordinator()
    else:
        log.info("[Cluster] Standalone mode")


def is_worker_standalone() -> bool:
    """Returns True if this worker has lost contact with coordinator (degraded mode)."""
    global _sender
    sender = _sender
    if sender is None:
        return False
    return getattr(sender, '_standalone_mode', False)


# ── Reverse Heartbeat Prober (Stage I, 2026-05-05) ───────────────────
#
# 动机:
#   worker HeartbeatSender 失效 (worker 进程跑但 sender 没启动 / 网络
#   单向通) 时, 主控反向 GET worker /devices 把它注册成 online. 配合
#   既有 push heartbeat 的双轨容灾.
#
# 设计:
#   - 仅 probe **已知**历史 worker (cluster_state.json 持久化的 host_id
#     即 _hosts 里有的 host); 不主动扫局域网 (安全)
#   - 仅 probe last_heartbeat 超过 _HOST_TIMEOUT 的 stale host (即
#     被判 offline 的) — 已 online 的 host 让 push 心跳处理
#   - probe 间隔默认 30s (push 心跳 _HEARTBEAT_INTERVAL=15s 的 2 倍,
#     防双轨重复噪音)
#   - 失败的 host 保持 offline (probe 不引入 false positive)
#   - 单例 thread, idempotent start/stop
#
# 关闭开关:
#   OPENCLAW_DISABLE_REVERSE_PROBE=1 完全禁用 (worker only 模式或测试)

_REVERSE_PROBE_INTERVAL = 30.0
_REVERSE_PROBE_STARTUP_DELAY = 10.0
_REVERSE_PROBE_MAX_INTERVAL = 300.0      # 退避上限 5 min
_REVERSE_PROBE_BACKOFF_MULT = 2.0        # 失败一次间隔翻倍


class _ReverseHeartbeatProber(threading.Thread):
    """主控后台线程, 周期性 reverse probe stale worker.

    2026-05-05 Stage K.1 加 per-host 指数退避:
      - 默认 _interval (30s) 间隔 probe
      - probe 失败 → 该 host 下次 probe 时间退避 (2x, 60s/120s/240s/300s 上限)
      - probe 成功 → reset 退避到默认间隔
      - 减真宕机 worker 的网络噪音 (避免每 30s 浪费 5s timeout)
    """

    def __init__(self, interval: float = _REVERSE_PROBE_INTERVAL,
                 startup_delay: float = _REVERSE_PROBE_STARTUP_DELAY,
                 max_interval: float = _REVERSE_PROBE_MAX_INTERVAL,
                 backoff_multiplier: float = _REVERSE_PROBE_BACKOFF_MULT):
        super().__init__(daemon=True, name="reverse-hb-prober")
        # interval 下限 1ms 防 0 引发死循环; 实际 caller 默认 30s
        self._interval = max(0.001, interval)
        self._startup_delay = max(0.0, startup_delay)
        self._max_interval = max(self._interval, max_interval)
        self._backoff_mult = max(1.0, backoff_multiplier)
        self._stop_event = threading.Event()
        # 2026-05-05 Stage P.1: 外部唤醒事件 (trigger endpoint async 路径用).
        # request_immediate_probe set 此 event → run loop 的 wait 立即返 →
        # 下次 _tick 优先 probe 已 pop 退避的 host.
        self._wake_event = threading.Event()
        self._iterations = 0
        self._last_probed = 0
        self._last_recovered = 0
        # K.1: per-host 退避 state. host_id → next 允许 probe 的 monotonic 时间
        # (time.time() 基). 初次 probe 永远允许 (字典里没的 host = 立即可 probe).
        self._next_probe_at: Dict[str, float] = {}
        # 当前每 host 的退避间隔 (上次失败后将间隔翻倍, 写到这里供下次用).
        self._current_interval: Dict[str, float] = {}
        # V.2: per-host 连续失败计数 (UX). probe 成功 reset 0; 失败累加.
        # status() 暴露给 ops 看 "死透节奏" — 比退避间隔更直观.
        self._consecutive_failures: Dict[str, int] = {}

    def stop(self) -> None:
        self._stop_event.set()
        # P.1: 设 wake_event 让 _wait_for_next_tick polling 立即退出
        self._wake_event.set()

    def request_immediate_probe(self, host_id: str = "") -> None:
        """Stage P.1: 外部 trigger — pop 指定 host 的退避窗口 + 唤醒 prober.

        host_id 为空时 pop 所有 host 的退避 (全 stale 立即 probe). 调用方
        可立即返 {"queued": [...]}, prober 在 ≤_wake_check_step 秒内被唤醒.
        """
        if host_id:
            self._next_probe_at.pop(host_id, None)
        else:
            self._next_probe_at.clear()
        self._wake_event.set()

    def run(self) -> None:
        if self._startup_delay > 0:
            self._stop_event.wait(self._startup_delay)
            if self._stop_event.is_set():
                return
        log.info("[Cluster] reverse heartbeat prober 启动 (interval=%.0fs)",
                 self._interval)
        while not self._stop_event.is_set():
            self._tick()
            self._wait_for_next_tick()
        log.info(
            "[Cluster] reverse heartbeat prober 停止, "
            "iterations=%d total_probed=%d total_recovered=%d",
            self._iterations, self._last_probed, self._last_recovered,
        )

    def _wait_for_next_tick(self) -> None:
        """P.1: polling-based wait, stop/wake event 任一 set 立即返.

        threading 没原生 wait_any. polling 0.05s step 在 30s interval 下
        是 600 次循环, 性能可忽略. 让外部 trigger 唤醒延迟 ≤ 50ms.
        """
        step = 0.05
        elapsed = 0.0
        while elapsed < self._interval:
            if self._stop_event.is_set():
                return
            if self._wake_event.is_set():
                self._wake_event.clear()
                return
            time.sleep(step)
            elapsed += step

    def _tick(self) -> None:
        """单 tick: 找 stale host + 逐个 probe (尊重 per-host 退避).

        异常被 catch 不让线程死 — 单 host probe 失败不影响其他 host.
        """
        self._iterations += 1
        try:
            coord = get_cluster_coordinator()
            now = time.time()
            with coord._lock:
                stale_hosts = [
                    hid for hid, h in coord._hosts.items()
                    if hid != "coordinator"
                    and h.host_ip
                    and (now - h.last_heartbeat) > _HOST_TIMEOUT
                ]
            # K.1: 退避过滤 — 跳过 next_probe_at 在未来的 host
            due_hosts = [
                hid for hid in stale_hosts
                if self._next_probe_at.get(hid, 0.0) <= now
            ]
            if not due_hosts:
                return
            log.debug(
                "[Cluster] reverse prober tick #%d: %d stale, %d due",
                self._iterations, len(stale_hosts), len(due_hosts),
            )
            for hid in due_hosts:
                if self._stop_event.is_set():
                    return
                self._last_probed += 1
                ok = coord.reverse_probe_worker(hid)
                self._update_backoff(hid, ok, now)
                if ok:
                    self._last_recovered += 1
        except Exception:  # noqa: BLE001
            log.exception("[Cluster] reverse prober tick 失败 (continue)")

    def _update_backoff(self, host_id: str, ok: bool, now: float) -> None:
        """K.1: probe 成功 reset 间隔到默认; 失败 翻倍间隔到 max_interval 上限.

        间隔记 _current_interval[host_id], 下次允许 probe 的时间记
        _next_probe_at[host_id]. 字典里没的 host = 立即可 probe (init 状态).
        """
        if ok:
            self._current_interval[host_id] = self._interval
            self._next_probe_at[host_id] = now + self._interval
            # V.2: 成功 reset 失败计数
            self._consecutive_failures[host_id] = 0
            return
        prev = self._current_interval.get(host_id, self._interval)
        new_interval = min(self._max_interval, prev * self._backoff_mult)
        self._current_interval[host_id] = new_interval
        self._next_probe_at[host_id] = now + new_interval
        # V.2: 累加失败计数
        self._consecutive_failures[host_id] = (
            self._consecutive_failures.get(host_id, 0) + 1
        )
        log.debug(
            "[Cluster] reverse probe %s 退避 → %.0fs (next at +%.0fs)",
            host_id, new_interval, new_interval,
        )

    def status(self) -> dict:
        # 2026-05-05 Stage H.1: 暴露 per_host_next_probe_at_iso 让 ops 看
        # "下次 何时 probe" (vs 只看间隔不知道几秒后).
        from datetime import datetime as _dt, timezone as _tz
        next_probe_iso: Dict[str, str] = {}
        for hid, ts in self._next_probe_at.items():
            try:
                next_probe_iso[hid] = _dt.fromtimestamp(
                    ts, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                next_probe_iso[hid] = ""
        return {
            "running": self.is_alive() and not self._stop_event.is_set(),
            "iterations": self._iterations,
            "total_probed": self._last_probed,
            "total_recovered": self._last_recovered,
            "interval_sec": self._interval,
            "max_interval_sec": self._max_interval,
            "backoff_multiplier": self._backoff_mult,
            "per_host_backoff": dict(self._current_interval),
            "per_host_next_probe_at_iso": next_probe_iso,
            "per_host_consecutive_failures": dict(self._consecutive_failures),
        }


_reverse_prober: Optional[_ReverseHeartbeatProber] = None
_prober_lock = threading.Lock()


def start_reverse_prober(
    interval: Optional[float] = None,
    startup_delay: Optional[float] = None,
    max_interval: Optional[float] = None,
    backoff_multiplier: Optional[float] = None,
) -> Optional[_ReverseHeartbeatProber]:
    """启动主控的 reverse heartbeat prober. idempotent.

    参数 None 时从 cluster.yaml 读取 (default values 来自 load_cluster_config).
    OPENCLAW_DISABLE_REVERSE_PROBE=1 完全禁用 (返 None).
    """
    if os.environ.get("OPENCLAW_DISABLE_REVERSE_PROBE", "").strip() in (
        "1", "true", "yes",
    ):
        log.info("[Cluster] reverse prober 已通过 env 关闭")
        return None
    # K.2: 默认值从 cluster.yaml 读
    cfg = load_cluster_config()
    interval = float(
        interval if interval is not None
        else cfg.get("reverse_probe_interval", _REVERSE_PROBE_INTERVAL)
    )
    startup_delay = float(
        startup_delay if startup_delay is not None
        else cfg.get("reverse_probe_startup_delay", _REVERSE_PROBE_STARTUP_DELAY)
    )
    max_interval = float(
        max_interval if max_interval is not None
        else cfg.get("reverse_probe_max_interval", _REVERSE_PROBE_MAX_INTERVAL)
    )
    backoff_multiplier = float(
        backoff_multiplier if backoff_multiplier is not None
        else cfg.get("reverse_probe_backoff_multiplier",
                     _REVERSE_PROBE_BACKOFF_MULT)
    )
    global _reverse_prober
    with _prober_lock:
        if _reverse_prober is not None and _reverse_prober.is_alive():
            return _reverse_prober
        t = _ReverseHeartbeatProber(
            interval=interval, startup_delay=startup_delay,
            max_interval=max_interval,
            backoff_multiplier=backoff_multiplier,
        )
        t.start()
        _reverse_prober = t
        return t


def stop_reverse_prober(timeout_sec: float = 5.0) -> bool:
    """优雅停止 reverse prober. 返 True 如果在 timeout 内退出."""
    global _reverse_prober
    with _prober_lock:
        t = _reverse_prober
        _reverse_prober = None
    if t is None:
        return True
    t.stop()
    t.join(timeout=timeout_sec)
    return not t.is_alive()


def reset_reverse_prober_for_tests() -> None:
    """仅测试用. 强制 stop+join 防孤儿 + 清单例.

    与 central_push_drain.reset_for_tests 同 pattern (Stage C.2 教训:
    旧 '只清单例不停 thread' 让孤儿 daemon 跨 test 污染).
    """
    global _reverse_prober
    with _prober_lock:
        t = _reverse_prober
        _reverse_prober = None
    if t is not None:
        t.stop()
        t.join(timeout=2.0)
