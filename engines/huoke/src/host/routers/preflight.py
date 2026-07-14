# -*- coding: utf-8 -*-
"""预检 API — 设备就绪状态查询与强制刷新。

从所有在线Worker聚合设备状态，无硬编码IP。
"""

import logging
import json
import urllib.request
import concurrent.futures
import time

from fastapi import APIRouter, Query

from src.host.device_registry import DEFAULT_DEVICES_YAML

router = APIRouter(prefix="/preflight", tags=["preflight"])
logger = logging.getLogger(__name__)

_TIMEOUT = 5


def _get_worker_bases() -> list[str]:
    """Get all online worker base URLs from cluster coordinator."""
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        hosts = coord._hosts if hasattr(coord, '_hosts') else {}
        bases = []
        for host_info in hosts.values():
            online = getattr(host_info, 'online', True)
            ip = getattr(host_info, 'host_ip', '')
            port = getattr(host_info, 'port', 8000)
            if online and ip:
                bases.append(f"http://{ip}:{port}")
        return bases if bases else ["http://192.168.0.103:8000"]  # fallback
    except Exception:
        return ["http://192.168.0.103:8000"]  # fallback


def _single_get(base: str, path: str) -> dict | list | None:
    """向指定 Worker 发 GET 请求，返回解析后的 JSON，失败返回 None。"""
    try:
        req = urllib.request.Request(f"{base}{path}", method="GET",
                                     headers={"Connection": "close"})
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        try:
            return json.loads(resp.read().decode())
        finally:
            resp.close()
    except Exception as e:
        logger.warning("[preflight] Worker GET %s%s 失败: %s", base, path, e)
        return None


def _workers_get(path: str) -> dict | list | None:
    """Query all online workers, aggregate results."""
    bases = _get_worker_bases()
    if len(bases) == 1:
        # Single worker - existing behavior
        return _single_get(bases[0], path)

    # Multiple workers - aggregate
    results = []
    for base in bases:
        data = _single_get(base, path)
        if data is None:
            continue
        if isinstance(data, list):
            results.extend(data)
        elif isinstance(data, dict):
            items = data.get("devices", data.get("items", []))
            if isinstance(items, list):
                results.extend(items)
    return results if results else None


def _fetch_w3_devices() -> list[dict]:
    """获取所有 Worker 全部设备列表（含在线状态）。"""
    data = _workers_get("/devices")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("devices", [])
    return []


def _fetch_w3_vpn_status() -> dict[str, dict]:
    """获取所有 Worker 全部设备的 VPN 状态，返回 {device_id: status_dict}。"""
    data = _workers_get("/vpn/status")
    if data is None:
        return {}
    devices_list = []
    if isinstance(data, list):
        devices_list = data
    elif isinstance(data, dict):
        devices_list = data.get("devices", [])
    return {d.get("device_id", ""): d for d in devices_list if d.get("device_id")}


def _check_one_device(did: str, vpn_map: dict, online: bool) -> dict:
    """
    根据已有的 VPN 状态数据组装单台设备的预检结果。

    逻辑：
      - 设备离线 → passed=False, blocked_step='offline'
      - VPN 未连接 → passed=False, blocked_step='vpn'（VPN 连接隐含网络可用）
      - VPN 已连接 → network_ok=True, vpn_ok=True
      - account_ok 暂时设为 True（TikTok进程检查需ADB，成本较高，后续可扩展）
    """
    if not online:
        return {
            "device_id": did,
            "passed": False,
            "blocked_step": "offline",
            "blocked_reason": "设备离线",
            "network_ok": False,
            "vpn_ok": False,
            "account_ok": None,
        }

    vpn_info = vpn_map.get(did, {})
    vpn_connected = bool(vpn_info.get("connected", False))

    if vpn_connected:
        return {
            "device_id": did,
            "passed": True,
            "blocked_step": None,
            "blocked_reason": None,
            "network_ok": True,
            "vpn_ok": True,
            "account_ok": True,
            "vpn_config": vpn_info.get("config_name", ""),
        }
    else:
        # VPN 未连接 — 网络状态未知（可能有网但未开VPN）
        return {
            "device_id": did,
            "passed": False,
            "blocked_step": "vpn",
            "blocked_reason": "VPN未连接" + (f"（{vpn_info.get('error')}）" if vpn_info.get("error") else ""),
            "network_ok": None,
            "vpn_ok": False,
            "account_ok": None,
        }


@router.get("/devices")
def get_devices_readiness(quick: bool = Query(False, description="quick=1 时只返回缓存摘要")):
    """
    返回所有设备的就绪状态摘要。

    quick=0（默认）: 从 W03 实时拉取 VPN 状态（约 3-5 秒）
    quick=1:         读取本地预检缓存（毫秒级，可能过期）
    """
    from src.host.preflight import _cache, _cache_lock

    if quick:
        # 纯缓存模式
        with _cache_lock:
            snapshot = dict(_cache)
        results = []
        for did, (r, ts) in snapshot.items():
            results.append(r)
    else:
        # 并行从 W03 拉取设备列表 + VPN 状态
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_devices = ex.submit(_fetch_w3_devices)
            f_vpn = ex.submit(_fetch_w3_vpn_status)
            w3_devices = f_devices.result()
            vpn_map = f_vpn.result()

        if not w3_devices:
            return {
                "ok": False,
                "error": "无法连接任何 Worker，请检查集群网络",
                "total_count": 0,
                "online_count": 0,
                "offline_count": 0,
                "ready_count": 0,
                "blocked_network": 0,
                "blocked_vpn": 0,
                "blocked_account": 0,
                "blocked_count": 0,
                "devices": [],
            }

        results = []
        now_ts = time.time()

        # 更新本地 preflight 缓存
        from src.host.preflight import _cache, _cache_lock

        for dev in w3_devices:
            did = dev.get("device_id", "")
            if not did:
                continue
            status = dev.get("status", "offline")
            online = status in ("connected", "online", "active")
            r = _check_one_device(did, vpn_map, online)
            results.append(r)
            # 写入缓存供 tiktok/device-grid 使用
            with _cache_lock:
                _cache[did] = (r, now_ts)

    # 统计摘要
    ready_count = 0
    blocked_network = 0
    blocked_vpn = 0
    blocked_account = 0
    offline_count = 0

    for r in results:
        passed = r.get("passed")
        step = r.get("blocked_step")
        if passed:
            ready_count += 1
        elif step == "offline":
            offline_count += 1
        elif step == "network":
            blocked_network += 1
        elif step == "vpn":
            blocked_vpn += 1
        elif step == "account":
            blocked_account += 1

    online_count = len(results) - offline_count

    return {
        "ok": True,
        "total_count": len(results),
        "online_count": online_count,
        "offline_count": offline_count,
        "ready_count": ready_count,
        "blocked_network": blocked_network,
        "blocked_vpn": blocked_vpn,
        "blocked_account": blocked_account,
        "blocked_count": blocked_network + blocked_vpn + blocked_account,
        "devices": results,
    }


@router.post("/devices/{device_id}/refresh")
def refresh_device_preflight(device_id: str):
    """强制重新检查指定设备（清除本地缓存后从 W03 实时拉取）。"""
    from src.host.preflight import _cache, _cache_lock

    # 清除本地缓存
    with _cache_lock:
        _cache.pop(device_id, None)

    # 查询该设备的 W03 状态
    w3_devices = _fetch_w3_devices()
    dev = next((d for d in w3_devices if d.get("device_id") == device_id), None)
    if dev is None:
        return {"ok": False, "error": f"设备 {device_id[:8]} 在 W03 未找到"}

    status = dev.get("status", "offline")
    online = status in ("connected", "online", "active")

    vpn_data = _workers_get(f"/vpn/status/{device_id}")
    vpn_map = {device_id: vpn_data} if vpn_data else {}

    r = _check_one_device(device_id, vpn_map, online)

    # 写回缓存
    with _cache_lock:
        from src.host.preflight import _cache_lock as _cl
        _cache[device_id] = (r, time.time())

    return {"ok": True, **r}


@router.post("/devices/refresh-all")
def refresh_all_preflight():
    """清除所有设备预检缓存，下次查询时重新检查。"""
    from src.host.preflight import _cache, _cache_lock
    with _cache_lock:
        count = len(_cache)
        _cache.clear()
    return {"ok": True, "cleared": count, "message": f"已清除 {count} 台设备的预检缓存"}


@router.get("/settings")
def get_preflight_settings():
    """读取当前预检/自动恢复设置。"""
    from src.host import health_monitor as _hm
    from src.host.executor import _vpn_required

    _cfg = DEFAULT_DEVICES_YAML
    return {
        "auto_recover_tasks": _hm._effective_auto_recover(),
        "vpn_required_default": _vpn_required(),
        "disconnect_confirm_rounds": _hm.effective_disconnect_confirm_rounds(str(_cfg)),
        "disconnect_confirm_rounds_override": _hm._runtime_disconnect_confirm_rounds,
    }


@router.post("/settings")
def update_preflight_settings(body: dict):
    """
    更新预检设置（运行时生效，重启后恢复默认）。

    body:
      - auto_recover_tasks: true/false
      - disconnect_confirm_rounds: 1–10 或 null（清除运行时覆盖，恢复 env/yaml）
    """
    from src.host import health_monitor as _hm

    changed = []
    _cfg = DEFAULT_DEVICES_YAML
    if "auto_recover_tasks" in body:
        try:
            from src.host.task_policy import policy_blocks_reconnect_recovery
            if policy_blocks_reconnect_recovery() and bool(body["auto_recover_tasks"]):
                return {"ok": False, "detail": "task_execution_policy.yaml 已禁用掉线恢复，无法开启"}
        except Exception:
            pass
        _hm._AUTO_RECOVER_TASKS = bool(body["auto_recover_tasks"])
        changed.append(f"auto_recover_tasks={_hm._effective_auto_recover()}")
        logger.info("[preflight] auto_recover_tasks 已设为 %s (effective=%s)",
                      _hm._AUTO_RECOVER_TASKS, _hm._effective_auto_recover())
    if "disconnect_confirm_rounds" in body:
        v = body.get("disconnect_confirm_rounds")
        if v is None:
            _hm.set_runtime_disconnect_confirm_rounds(None)
            changed.append("disconnect_confirm_rounds_override=cleared")
        else:
            _hm.set_runtime_disconnect_confirm_rounds(int(v))
            eff = _hm.effective_disconnect_confirm_rounds(str(_cfg))
            changed.append(f"disconnect_confirm_rounds_effective={eff}")
        logger.info(
            "[preflight] disconnect_confirm_rounds override=%s effective=%s",
            _hm._runtime_disconnect_confirm_rounds,
            _hm.effective_disconnect_confirm_rounds(str(_cfg)),
        )
    return {"ok": True, "changed": changed}
