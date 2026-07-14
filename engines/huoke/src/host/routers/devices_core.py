# -*- coding: utf-8 -*-
"""设备基础管理路由：列表、状态、删除、重连、扫描、别名、编号、注册表、壁纸"""
import json
import logging
import re
from pathlib import Path
from fastapi import APIRouter, HTTPException, Depends, Query, Body

from src.host.device_registry import (
    DEFAULT_DEVICES_YAML,
    config_file,
    data_dir,
)
from src.host.device_alias_labels import (
    apply_slot_and_labels,
    get_slot,
    next_free_slot_resolved,
    resolve_host_scope_for_device,
    used_slots_resolved,
)

router = APIRouter(prefix="", tags=["devices-core"])
logger = logging.getLogger(__name__)
_config_path = DEFAULT_DEVICES_YAML

_WIFI_ADB_ID = re.compile(r"^\d{1,3}(\.\d{1,3}){3}:\d+$")


def _connection_bool(key: str, default: bool = True) -> bool:
    try:
        import yaml

        with open(DEFAULT_DEVICES_YAML, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        c = cfg.get("connection") or {}
        if key not in c:
            return default
        return bool(c[key])
    except Exception:
        return default


def _merge_dual_transport_rows(entries: list[dict], pool) -> list[dict]:
    """同一指纹下 USB + 无线 ADB 合并为一条展示（device_id 优先 USB），底层仍保留两路连接。"""
    by_fp: dict[str, list[dict]] = {}
    no_fp: list[dict] = []
    for e in entries:
        fp = (e.get("fingerprint") or "").strip()
        if not fp:
            no_fp.append(e)
            continue
        by_fp.setdefault(fp, []).append(e)
    out: list[dict] = []
    for _fp, group in by_fp.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        def _is_wifi_row(g: dict) -> bool:
            return bool(_WIFI_ADB_ID.match((g.get("device_id") or "").strip()))

        usb_rows = [g for g in group if not _is_wifi_row(g)]
        primary = usb_rows[0] if usb_rows else min(group, key=lambda x: x.get("device_id") or "")
        alts = [g["device_id"] for g in group if g["device_id"] != primary["device_id"]]
        merged = dict(primary)
        merged["alternate_device_ids"] = alts
        merged["dual_transport"] = True
        statuses = [g.get("status") for g in group]
        if "connected" in statuses:
            merged["status"] = "connected"
        elif "busy" in statuses:
            merged["status"] = "busy"
        merged["busy"] = any(pool.is_device_busy(g["device_id"]) for g in group)
        merged["last_seen"] = max((g.get("last_seen") or 0) for g in group)
        if primary.get("usb_issue"):
            merged["usb_issue"] = primary["usb_issue"]
        else:
            merged["usb_issue"] = next(
                (g["usb_issue"] for g in group if g.get("usb_issue")), None
            )
        out.append(merged)
    out.extend(no_fp)
    return out


def _local_managed_device_ids() -> set[str]:
    try:
        from src.device_control.device_manager import get_device_manager as _gdm
        return {d.device_id for d in _gdm(_config_path).get_all_devices()}
    except Exception:
        return set()


def _stale_alias_key_count(device_ids: set[str], aliases: dict) -> int:
    """device_aliases.json 中存在、但 device_ids 中不存在的键数量（孤儿别名）。"""
    return sum(1 for k in aliases if k not in device_ids)


def _iter_stale_offline_devices(manager, max_age_seconds: float):
    """离线超过 max_age_seconds 的设备（与 POST /devices/cleanup 判定一致）。"""
    import time as _time

    now = _time.time()
    out: list[tuple] = []
    for dev in list(manager.get_all_devices()):
        if dev.is_online:
            continue
        age_sec = now - dev.last_seen if dev.last_seen > 0 else float("inf")
        if age_sec > max_age_seconds:
            out.append((dev, age_sec))
    return out


def _backup_device_config_files() -> dict:
    """将 devices.yaml 与 device_aliases.json 复制到 data/backups/devices_cleanup_<ts>/。"""
    import shutil
    from datetime import datetime

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = data_dir() / "backups" / f"devices_cleanup_{ts}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    cfg_dir = Path(_config_path).parent
    copied: list[str] = []
    for name in ("devices.yaml", "device_aliases.json"):
        p = cfg_dir / name
        if p.is_file():
            dest = backup_dir / name
            shutil.copy2(p, dest)
            copied.append(str(dest))
    return {"backup_dir": str(backup_dir), "files": copied}


def _coordinator_host_label() -> str | None:
    """协调器模式下为本机 USB 设备打主机名，供屏幕监控卡片与 Worker 设备区分。"""
    try:
        import yaml as _yaml
        p = config_file("cluster.yaml")
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            c = _yaml.safe_load(f) or {}
        if c.get("role") != "coordinator":
            return None
        name = (c.get("host_name") or "").strip()
        return name or "主控"
    except Exception:
        return None


# ── 集群壁纸代理工具函数 ──────────────────────────────────────────────────

def _proxy_wallpaper_to_worker(did: str, number: int, display_name: str = "") -> bool:
    """找到拥有该设备的 Worker 并代理壁纸部署请求。返回是否成功。"""
    import urllib.request, json as _j
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if not coord:
            return False
        for hid, hinfo in coord._hosts.items():
            if not getattr(hinfo, 'online', False):
                continue
            host_devs = {d.get('device_id') for d in (getattr(hinfo, 'devices', None) or [])}
            if did not in host_devs:
                continue
            url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/{did}/wallpaper"
            data = _j.dumps({"number": number, "display_name": display_name}).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            # Worker 首次装 Helper / 大文件 push 可能较慢；壁纸广播已改异步，此处仍留足 HTTP 超时
            resp = urllib.request.urlopen(req, timeout=120)
            result = _j.loads(resp.read().decode())
            if result.get("ok"):
                logger.info("[wallpaper-proxy] %s → Worker %s OK", did[:8], hid)
                return True
    except Exception as e:
        logger.warning("[wallpaper-proxy] %s failed: %s", did[:8], e)
    return False


def _update_wallpaper_status(did: str, num: int):
    """记录壁纸部署成功的编号到 aliases，供前端状态追踪。"""
    try:
        aliases = _load_aliases()
        if did in aliases:
            aliases[did]["wallpaper_number"] = num
            _save_aliases(aliases)
    except Exception as e:
        logger.debug("[wp-status] failed to update wallpaper_number for %s: %s", did[:8], e)


def _mark_wallpaper_error(did: str, reason: str):
    """记录壁纸部署失败原因到 aliases，供前端健康巡检展示。"""
    try:
        aliases = _load_aliases()
        if did in aliases:
            aliases[did]["wallpaper_error"] = reason
            _save_aliases(aliases)
    except Exception:
        pass

def _clear_wallpaper_error(did: str):
    """壁纸部署成功后清除错误标记。"""
    try:
        aliases = _load_aliases()
        if did in aliases and "wallpaper_error" in aliases[did]:
            del aliases[did]["wallpaper_error"]
            _save_aliases(aliases)
    except Exception:
        pass

_HARDENED_SERIALS: set[str] = set()  # 进程级缓存
_HARDEN_TTL_DAYS = 7  # 持久化跳过窗口：7 天内已硬化的设备不重复跑


def _is_recently_hardened(did: str) -> bool:
    """读 aliases.miui_hardened_at，TTL 内则跳过硬化。"""
    try:
        import datetime as _dt
        ts_iso = _load_aliases().get(did, {}).get("miui_hardened_at")
        if not ts_iso:
            return False
        ts = _dt.datetime.fromisoformat(ts_iso)
        return (_dt.datetime.now() - ts).days < _HARDEN_TTL_DAYS
    except Exception:
        return False


def _mark_hardened(did: str):
    """写 aliases.miui_hardened_at，供下次跨进程跳过。"""
    try:
        import datetime as _dt
        aliases = _load_aliases()
        if did in aliases:
            aliases[did]["miui_hardened_at"] = _dt.datetime.now().isoformat(timespec="seconds")
            _save_aliases(aliases)
    except Exception:
        pass


def _ensure_hardened(did: str):
    """统一硬化入口：进程内缓存 → 持久化检查 → 真正硬化 + 写时间戳。

    幂等 + 多调用点共享。供 _deploy_wallpaper_smart / health_monitor._auto_deploy_wallpaper 使用。
    仅 MIUI/Redmi/POCO 设备生效（is_miui_device 内部判断）。
    """
    if did in _HARDENED_SERIALS:
        return
    if _is_recently_hardened(did):
        _HARDENED_SERIALS.add(did)
        return
    try:
        from scripts.disable_miui_security_popups import harden_quietly
        if harden_quietly(did):
            _mark_hardened(did)
            logger.info("[wp] MIUI 安全硬化已对 %s 生效", did[:8])
    except Exception as e:
        logger.debug("[wp] harden 跳过 %s: %s", did[:8], e)
    _HARDENED_SERIALS.add(did)


_IME_UNIFIED_SERIALS: set[str] = set()


def _ensure_ime_unified(did: str):
    """统一 IME 入口：把 default_input_method 切到 ATX 自带的 ADBKeyboard。

    实时查 default_input_method 判断（不需要持久化字段，状态可直接观察）。
    幂等。供 device-online 钩子调用，让新接入的机器自动统一 IME 用于 unicode 输入。
    """
    if did in _IME_UNIFIED_SERIALS:
        return
    try:
        import subprocess as _sp
        cf = getattr(_sp, "CREATE_NO_WINDOW", 0)
        r = _sp.run(["adb", "-s", did, "shell",
                     "settings get secure default_input_method"],
                    capture_output=True, text=True, timeout=10, creationflags=cf)
        cur = (r.stdout or "").strip()
        if "com.github.uiautomator" in cur:
            _IME_UNIFIED_SERIALS.add(did)
            return
    except Exception as e:
        logger.debug("[ime] 检测 default_input_method 失败 %s: %s", did[:8], e)
        return
    try:
        from scripts.unify_ime import unify_quietly
        if unify_quietly(did):
            logger.info("[ime] 输入法统一为 ADBKeyboard 已对 %s 生效", did[:8])
    except Exception as e:
        logger.debug("[ime] unify 跳过 %s: %s", did[:8], e)
    _IME_UNIFIED_SERIALS.add(did)


def _deploy_wallpaper_smart(manager, did: str, number: int, display_name: str = "") -> bool:
    """本地 ADB 优先部署壁纸，成功则记录 wallpaper_number；失败则代理到 Worker（Worker 侧自行记录）。"""
    from src.utils.wallpaper_generator import deploy_wallpaper
    if manager:
        _ensure_hardened(did)
        try:
            ok = deploy_wallpaper(manager, did, number, display_name=display_name)
            if ok:
                _update_wallpaper_status(did, number)  # 本地成功：记录到本地 aliases
                _clear_wallpaper_error(did)
                return True
            else:
                _mark_wallpaper_error(did, "deploy_failed")
        except Exception as e:
            logger.debug("[wp] deploy error %s: %s", did[:8], e)
            _mark_wallpaper_error(did, f"exception:{type(e).__name__}")
    return _proxy_wallpaper_to_worker(did, number, display_name)


# ── GET /devices ──

@router.get("/devices", response_model=list)
async def list_devices():
    import asyncio
    from src.device_control.device_manager import get_device_manager
    from ..api import _resolve_serial_from_config, verify_api_key
    from ..worker_pool import get_worker_pool
    from ..schemas import DeviceListItem

    manager = get_device_manager(_config_path)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, manager.discover_devices)
    pool = get_worker_pool()
    problem_map = {}
    for did, reason in getattr(manager, "_last_problem_devices", []):
        problem_map[did] = reason
    devices = []
    for d in manager.get_all_devices():
        entry = {
            **DeviceListItem(
                device_id=d.device_id,
                display_name=d.display_name,
                status=d.status.value,
                model=d.model or "",
                android_version=d.android_version or "",
            ).model_dump(),
            "busy": pool.is_device_busy(d.device_id),
            "fingerprint": d.fingerprint or "",
            "imei": d.imei or "",
            "hw_serial": d.hw_serial or "",
            "last_seen": d.last_seen or 0,
        }
        if d.device_id in problem_map:
            entry["usb_issue"] = problem_map[d.device_id]
        devices.append(entry)
    removed = getattr(manager, "_removed_devices", set())
    for did, reason in problem_map.items():
        if did in removed:
            continue
        if not any(e["device_id"] == did for e in devices):
            extra: dict = {
                "device_id": did,
                "display_name": did[:8],
                "status": "disconnected",
                "model": "",
                "android_version": "",
                "busy": False,
                "fingerprint": "",
                "imei": "",
                "hw_serial": "",
                "usb_issue": reason,
            }
            hl = _coordinator_host_label()
            if hl:
                extra["host_name"] = hl
            devices.append(extra)

    hl = _coordinator_host_label()
    if hl:
        for e in devices:
            e.setdefault("host_name", hl)
    if _connection_bool("merge_dual_transport_in_api", True):
        devices = _merge_dual_transport_rows(devices, pool)
    return devices


@router.get("/devices/meta")
async def devices_list_meta():
    """设备列表元信息：schema 版本与本机 ADB 侧计数（不含浏览器合并的集群行）。

    供脚本/集成与 UI 对照；集群设备数以客户端合并结果为准。
    """
    import asyncio
    from src.device_control.device_manager import DeviceStatus, get_device_manager

    manager = get_device_manager(_config_path)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, manager.discover_devices)
    all_devs = manager.get_all_devices()
    n_connected = sum(1 for d in all_devs if d.status == DeviceStatus.CONNECTED)
    n_disconnected = sum(1 for d in all_devs if d.status == DeviceStatus.DISCONNECTED)
    problem_raw = getattr(manager, "_last_problem_devices", []) or []
    problem_n = len(problem_raw)
    aliases = _load_aliases()
    managed_ids = {d.device_id for d in all_devs}
    stale_n = _stale_alias_key_count(managed_ids, aliases)
    alert_items: list[dict] = []
    if problem_n:
        alert_items.append(
            {
                "code": "adb_usb",
                "severity": "warning",
                "count": problem_n,
                "message": f"ADB 列出 {problem_n} 路异常 USB 附件（未授权/离线等），请在设备上授权或重插数据线",
            }
        )
    if stale_n:
        alert_items.append(
            {
                "code": "stale_aliases",
                "severity": "warning",
                "count": stale_n,
                "message": f"device_aliases.json 中有 {stale_n} 条孤儿记录（本机已无对应设备），可一键修剪",
            }
        )
    preview: list[dict] = []
    for row in problem_raw[:8]:
        if isinstance(row, (tuple, list)) and len(row) >= 2:
            preview.append({"device_id": row[0], "status": row[1]})
        elif isinstance(row, (tuple, list)) and len(row) == 1:
            preview.append({"device_id": row[0], "status": ""})

    return {
        "schema_version": 1,
        "note": "Cluster rows are merged in the dashboard JS; breakdown here is host ADB/config only.",
        "breakdown": {
            "configured_devices_yaml": len(manager.devices),
            "list_rows": len(all_devs),
            "connected": n_connected,
            "disconnected": n_disconnected,
            "adb_problem_attachments": problem_n,
            "stale_alias_keys": stale_n,
        },
        "adb_problem_preview": preview,
        "alerts": {
            "has_warning": bool(alert_items),
            "items": alert_items,
        },
    }


# ── GET /devices/{id}/status ──

@router.get("/devices/{device_id}/status")
def device_status(device_id: str):
    from src.device_control.device_manager import get_device_manager
    from ..api import _resolve_serial_from_config
    from ..worker_pool import get_worker_pool

    manager = get_device_manager(_config_path)
    manager.discover_devices()
    info = manager.get_device_info(device_id)
    if not info:
        serial = _resolve_serial_from_config(_config_path, device_id)
        info = manager.get_device_info(serial)
        if not info:
            raise HTTPException(status_code=404, detail="设备不存在")
        device_id = serial
    pool = get_worker_pool()
    activity = manager.get_current_activity(device_id)
    return {
        "device_id": info.device_id,
        "display_name": info.display_name,
        "status": info.status.value,
        "busy": pool.is_device_busy(device_id),
        "current_activity": activity,
        "model": info.model or "",
        "android_version": info.android_version or "",
    }


# ── GET /devices/health-summary ── (必须在 {device_id} 通配路由之前注册)

@router.get("/devices/health-summary")
def get_device_health_summary():
    """返回基于真实ADB连接的设备健康汇总。
    统计来源：manager.get_all_devices()（实时ADB扫描），而非aliases历史记录。
    """
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    manager = get_device_manager(_config_path)
    manager.discover_devices()
    all_devices = manager.get_all_devices()
    aliases = _load_aliases()

    online = 0       # 在线且编号正常且壁纸最新
    offline = 0      # 已编号但当前离线
    unset = 0        # 无编号（新设备）
    wp_outdated = 0  # 在线但壁纸编号不匹配或有错误

    for d in all_devices:
        did = d.device_id
        is_on = d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)
        info = aliases.get(did, {})
        num = info.get("number")

        if not num:
            unset += 1
            continue
        if not is_on:
            offline += 1
            continue
        wp_num = info.get("wallpaper_number")
        wp_err = info.get("wallpaper_error")
        if wp_err or (wp_num and wp_num != num):
            wp_outdated += 1
        else:
            online += 1

    managed_ids = {d.device_id for d in all_devices}
    stale_aliases = _stale_alias_key_count(managed_ids, aliases)
    return {
        "online": online,
        "offline": offline,
        "unset": unset,
        "wallpaper_outdated": wp_outdated,
        "total": len(all_devices),
        "stale_aliases": stale_aliases,
    }


@router.get("/devices/cleanup-candidates")
def get_cleanup_candidates(
    max_age_minutes: int = Query(5, ge=1, le=365 * 24 * 60, description="离线时长阈值（分钟），超过则列为可清理"),
):
    """列出若立即执行 POST /devices/cleanup（同参数）将被移除的离线设备（先 discover）。"""
    import math
    from src.device_control.device_manager import get_device_manager

    manager = get_device_manager(_config_path)
    manager.discover_devices(force=True)
    max_age_sec = float(max_age_minutes * 60)
    rows = []
    for dev, age_sec in _iter_stale_offline_devices(manager, max_age_sec):
        reason = "never_seen_offline" if dev.last_seen <= 0 else "offline_over_age"
        rows.append(
            {
                "device_id": dev.device_id,
                "display_name": dev.display_name,
                "status": dev.status.value,
                "last_seen": dev.last_seen,
                "age_minutes": math.floor(age_sec / 60) if math.isfinite(age_sec) else None,
                "reason": reason,
            }
        )
    return {
        "max_age_minutes": max_age_minutes,
        "count": len(rows),
        "candidates": rows,
    }


# ── DELETE /devices/{id} ──

@router.delete("/devices/{device_id}")
def delete_device(device_id: str):
    """Remove an offline device from config, aliases, and groups."""
    from src.device_control.device_manager import get_device_manager

    manager = get_device_manager(_config_path)
    info = manager.get_device_info(device_id)
    if not info:
        raise HTTPException(status_code=404, detail="设备不存在")
    if info.is_online:
        raise HTTPException(status_code=409, detail="在线设备不能删除，请先断开连接")
    display = info.display_name
    if not manager.remove_device(device_id):
        raise HTTPException(status_code=500, detail="删除失败")
    _cleanup_device_refs(device_id)
    return {"ok": True, "device_id": device_id, "display_name": display}


# ── POST /devices/batch-delete ──

@router.post("/devices/batch-delete")
def batch_delete_devices(body: dict):
    """Remove multiple offline devices."""
    from src.device_control.device_manager import get_device_manager

    ids = body.get("device_ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="未提供设备列表")
    manager = get_device_manager(_config_path)
    results = []
    for did in ids:
        info = manager.get_device_info(did)
        if not info:
            results.append({"device_id": did, "ok": False, "reason": "不存在"})
            continue
        if info.is_online:
            results.append({"device_id": did, "ok": False, "reason": "在线"})
            continue
        ok = manager.remove_device(did)
        if ok:
            _cleanup_device_refs(did)
        results.append({"device_id": did, "ok": ok})
    return {"deleted": sum(1 for r in results if r["ok"]), "results": results}


# ── POST /devices/cleanup ──

@router.post("/devices/cleanup")
def cleanup_ghost_devices(body: dict = None):
    """一键清理所有离线幽灵设备。

    Coordinator 模式下自动转发到所有 Worker 执行。
    Body:
      - max_age_minutes: int，默认 5
      - backup: bool，默认 True，清理前备份 config/devices.yaml 与 device_aliases.json 到 data/backups/
      - device_ids: 可选，非空时仅清理「同时满足离线超时且 id 在此列表内」的设备（与 GET /devices/cleanup-candidates 对照）。
        传空列表 [] 表示不清理任何本机设备（仍会转发 Worker，由 Worker 各自解释）。
    """
    import json as _json
    from src.device_control.device_manager import get_device_manager

    body = body or {}
    max_age = int(body.get("max_age_minutes", 5)) * 60
    do_backup = bool(body.get("backup", True))
    raw_ids = body.get("device_ids", None)
    id_filter: set[str] | None = None
    if raw_ids is not None:
        if not isinstance(raw_ids, list):
            raise HTTPException(status_code=400, detail="device_ids 须为字符串数组")
        id_filter = {str(x).strip() for x in raw_ids if str(x).strip()}

    manager = get_device_manager(_config_path)
    manager.discover_devices(force=True)

    backup_info: dict | None = None
    if do_backup:
        try:
            backup_info = _backup_device_config_files()
        except Exception as e:
            logger.warning("[cleanup] backup failed: %s", e)
            backup_info = {"error": str(e)}

    removed: list[str] = []
    stale_pairs = _iter_stale_offline_devices(manager, float(max_age))
    if id_filter is not None:
        if len(id_filter) == 0:
            stale_pairs = []
        else:
            stale_pairs = [(d, a) for d, a in stale_pairs if d.device_id in id_filter]

    for dev, _age_sec in stale_pairs:
        did = dev.device_id
        ok = manager.remove_device(did)
        if ok:
            _cleanup_device_refs(did)
            removed.append(did[:12])

    local_remaining = len(manager.get_all_devices())

    # Coordinator: 转发到所有 Worker
    worker_removed = 0
    worker_remaining = 0
    try:
        import yaml
        cluster_cfg_path = config_file("cluster.yaml")
        if cluster_cfg_path.exists():
            with open(cluster_cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if cfg.get("role") == "coordinator":
                from src.host.multi_host import get_cluster_coordinator
                import urllib.request
                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        try:
                            url = f"http://{host['host_ip']}:{host.get('port',8000)}/devices/cleanup"
                            data = _json.dumps(body).encode()
                            req = urllib.request.Request(url, data=data, method="POST",
                                                        headers={"Content-Type": "application/json"})
                            resp = urllib.request.urlopen(req, timeout=30)
                            wr = _json.loads(resp.read().decode())
                            worker_removed += wr.get("removed", 0)
                            worker_remaining += wr.get("remaining", 0)
                        except Exception as e:
                            logger.warning("[清理] Worker %s 失败: %s", host.get("host_name"), e)
    except Exception:
        pass

    return {
        "ok": True,
        "removed": len(removed) + worker_removed,
        "devices": removed,
        "remaining": local_remaining + worker_remaining,
        "backup": backup_info,
        "device_ids_filter": sorted(id_filter) if id_filter is not None else None,
    }


# ── POST /devices/{id}/reconnect ──

@router.post("/devices/{device_id}/reconnect")
def reconnect_device(device_id: str):
    """Attempt ADB reconnect for an offline device."""
    import subprocess as _sp
    import time
    from src.device_control.device_manager import get_device_manager

    manager = get_device_manager(_config_path)
    adb = getattr(manager, 'adb_path', 'adb')
    steps = []
    try:
        r = _sp.run(
            [adb, "-s", device_id, "reconnect"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        steps.append({"cmd": "reconnect", "rc": r.returncode,
                       "out": (r.stdout + r.stderr).strip()[:200]})
    except Exception as e:
        steps.append({"cmd": "reconnect", "error": str(e)})
    time.sleep(1.5)
    try:
        r2 = _sp.run(
            [adb, "-s", device_id, "shell", "echo", "ok"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        online = r2.returncode == 0 and "ok" in r2.stdout
        steps.append({"cmd": "verify", "online": online})
    except Exception:
        online = False
        steps.append({"cmd": "verify", "online": False})
    if online:
        manager.discover_devices()
    return {"ok": online, "device_id": device_id, "steps": steps}


# ── POST /devices/batch-reconnect ──

@router.post("/devices/batch-reconnect")
def batch_reconnect_devices(body: dict):
    """Attempt ADB reconnect for multiple devices."""
    import subprocess as _sp
    import time
    from src.device_control.device_manager import get_device_manager

    ids = body.get("device_ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="未提供设备列表")
    manager = get_device_manager(_config_path)
    adb = getattr(manager, 'adb_path', 'adb')
    results = []
    for did in ids:
        try:
            _sp.run([adb, "-s", did, "reconnect"],
                     capture_output=True, timeout=8,
                     creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    time.sleep(2)
    manager.discover_devices()
    connected = {d.device_id for d in manager.get_connected_devices()}
    for did in ids:
        results.append({"device_id": did, "online": did in connected})
    return {"reconnected": sum(1 for r in results if r["online"]),
            "results": results}


# ── POST /devices/dual-channel/setup ──

@router.post("/devices/dual-channel/setup")
def setup_dual_channel(body: dict | None = Body(default=None)):
    """为 USB 设备开启 adb tcpip 5555 并尝试 adb connect（Wi‑Fi 双通道备份）。

    请求体二选一：
    - ``{"all_usb": true}``：对本机所有已连接的 USB 设备执行；
    - ``{"device_id": "<USB 序列号>"}``：对单台 USB 设备执行。

    无线 ADB 设备（形如 ``192.168.x.x:5555``）不可作为 device_id。
    """
    import subprocess as _sp
    import time
    from typing import Any

    body = body or {}
    device_id = body.get("device_id")
    all_usb = bool(body.get("all_usb", False))

    from src.device_control.device_manager import get_device_manager

    manager = get_device_manager(_config_path)
    adb = getattr(manager, "adb_path", "adb")
    _cflags = getattr(_sp, "CREATE_NO_WINDOW", 0)

    def _is_usb(did: str) -> bool:
        return not _WIFI_ADB_ID.match(did or "")

    def _one(usb_serial: str) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        wifi_ip = None
        try:
            r0 = _sp.run(
                [adb, "-s", usb_serial, "shell", "ip", "route", "get", "8.8.8.8"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=_cflags,
            )
            m = re.search(r"\bsrc\s+(\d+\.\d+\.\d+\.\d+)", r0.stdout or "")
            if m:
                wifi_ip = m.group(1)
            steps.append({
                "cmd": "ip route get",
                "rc": r0.returncode,
                "ip": wifi_ip,
                "out": (r0.stdout or "")[:400],
            })
        except Exception as e:
            steps.append({"cmd": "ip route get", "error": str(e)})
            return {"device_id": usb_serial, "ok": False, "wifi_ip": None, "steps": steps}

        if not wifi_ip or wifi_ip.startswith("127."):
            return {
                "device_id": usb_serial,
                "ok": False,
                "wifi_ip": wifi_ip,
                "steps": steps,
                "error": "no_lan_ip",
            }

        try:
            r1 = _sp.run(
                [adb, "-s", usb_serial, "tcpip", "5555"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=_cflags,
            )
            steps.append({
                "cmd": "tcpip 5555",
                "rc": r1.returncode,
                "out": (r1.stdout + r1.stderr)[:400],
            })
        except Exception as e:
            steps.append({"cmd": "tcpip", "error": str(e)})
            return {"device_id": usb_serial, "ok": False, "wifi_ip": wifi_ip, "steps": steps}

        time.sleep(2)

        addr = f"{wifi_ip}:5555"
        try:
            r2 = _sp.run(
                [adb, "connect", addr],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=25,
                creationflags=_cflags,
            )
            out = (r2.stdout or "") + (r2.stderr or "")
            steps.append({"cmd": f"connect {addr}", "rc": r2.returncode, "out": out[:400]})
            ok = r2.returncode == 0 and "connected" in out.lower()
        except Exception as e:
            steps.append({"cmd": "connect", "error": str(e)})
            ok = False

        manager.discover_devices(force=True)
        after = {d.device_id for d in manager.get_connected_devices()}
        wifi_ok = addr in after or any(x.startswith(f"{wifi_ip}:") for x in after)
        return {
            "device_id": usb_serial,
            "ok": ok or wifi_ok,
            "wifi_ip": wifi_ip,
            "wireless_id": addr if (ok or wifi_ok) else None,
            "steps": steps,
        }

    targets: list[str] = []
    if device_id:
        if not _is_usb(str(device_id)):
            raise HTTPException(status_code=400, detail="device_id 须为 USB 序列号，不是无线地址")
        targets = [str(device_id)]
    elif all_usb:
        for d in manager.get_connected_devices():
            if _is_usb(d.device_id):
                targets.append(d.device_id)
    else:
        raise HTTPException(
            status_code=400,
            detail='请传 {"device_id": "<USB序列号>"} 或 {"all_usb": true}',
        )

    results = [_one(t) for t in targets]
    manager.discover_devices(force=True)

    # 主控 + all_usb：向各在线 Worker 同步执行（Worker 本机 USB）
    worker_summaries: list[dict] = []
    try:
        cluster_cfg_path = config_file("cluster.yaml")
        if all_usb and cluster_cfg_path.exists():
            import yaml as _yaml
            with open(cluster_cfg_path, encoding="utf-8") as f:
                _cc = _yaml.safe_load(f) or {}
            if _cc.get("role") == "coordinator":
                from src.host.multi_host import get_cluster_coordinator
                import urllib.request

                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        try:
                            url = (
                                f"http://{host['host_ip']}:"
                                f"{host.get('port', 8000)}/devices/dual-channel/setup"
                            )
                            req = urllib.request.Request(
                                url,
                                data=json.dumps({"all_usb": True}).encode("utf-8"),
                                headers={"Content-Type": "application/json"},
                                method="POST",
                            )
                            with urllib.request.urlopen(req, timeout=120) as resp:
                                wr = json.loads(resp.read().decode())
                            worker_summaries.append({
                                "host_name": host.get("host_name"),
                                "host_ip": host.get("host_ip"),
                                **wr,
                            })
                        except Exception as e:
                            logger.warning(
                                "[dual-channel] Worker %s: %s",
                                host.get("host_name"),
                                e,
                            )
                            worker_summaries.append({
                                "host_name": host.get("host_name"),
                                "host_ip": host.get("host_ip"),
                                "error": str(e)[:200],
                            })
    except Exception as e:
        logger.debug("[dual-channel] coordinator forward: %s", e)

    def _all_ok(rs: list) -> bool:
        if not rs:
            return True
        return all(r.get("ok") for r in rs)

    local_ok = _all_ok(results)
    worker_ok = True
    for w in worker_summaries:
        if w.get("error"):
            worker_ok = False
            break
        if w.get("ok") is False:
            worker_ok = False
            break

    return {
        "ok": local_ok and worker_ok,
        "results": results,
        "workers": worker_summaries,
    }


# ── POST /devices/rescan ──

@router.post("/devices/rescan")
def rescan_all_devices():
    """Force re-discover all devices, re-collect fingerprints, and update registry."""
    from src.device_control.device_manager import get_device_manager
    from src.utils.wallpaper_generator import get_wallpaper_auto_manager

    manager = get_device_manager(_config_path)
    for dev in manager.get_all_devices():
        if dev.is_online:
            manager._collect_fingerprint(dev.device_id)
    wp_mgr = get_wallpaper_auto_manager()
    results = wp_mgr.ensure_all_numbered(manager)
    return {
        "ok": True,
        "total": len(results),
        "deployed": sum(1 for v in results.values() if v.get("deployed")),
        "devices": [{
            "device_id": did,
            "number": v.get("number", 0),
            "deployed": v.get("deployed", False),
        } for did, v in results.items()],
    }


# ── GET /devices/aliases, POST /devices/{id}/alias ──

_device_aliases_path = config_file("device_aliases.json")


def _load_aliases() -> dict:
    if _device_aliases_path.exists():
        import json
        with open(_device_aliases_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_aliases(data: dict):
    import json
    _device_aliases_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_device_aliases_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_aliases_global() -> dict:
    """Load aliases from coordinator + all online Workers (merged view).
    Used for conflict detection so we see all assigned numbers across the cluster."""
    aliases = _load_aliases()
    try:
        from src.host.multi_host import get_cluster_coordinator
        import urllib.request, json as _json
        coord = get_cluster_coordinator()
        if coord:
            overview = coord.get_overview()
            for host in overview.get("hosts", []):
                if not host.get("online"):
                    continue
                try:
                    url = f"http://{host['host_ip']}:{host.get('port', 8000)}/devices/aliases"
                    resp = urllib.request.urlopen(url, timeout=5)
                    worker_aliases = _json.loads(resp.read().decode())
                    for did, info in worker_aliases.items():
                        if did not in aliases:
                            aliases[did] = info
                except Exception:
                    pass
    except Exception:
        pass
    return aliases


@router.get("/devices/aliases")
def get_all_aliases():
    """Get all device aliases/numbers. Coordinator merges Worker aliases."""
    aliases = _load_aliases()

    # Coordinator: merge aliases from online Workers
    try:
        import yaml
        cluster_cfg = config_file("cluster.yaml")
        if cluster_cfg.exists():
            with open(cluster_cfg, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            if cfg.get("role") == "coordinator":
                from src.host.multi_host import get_cluster_coordinator
                coord = get_cluster_coordinator()
                if coord:
                    import urllib.request, json as _json
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        try:
                            url = f"http://{host['host_ip']}:{host.get('port', 8000)}/devices/aliases"
                            resp = urllib.request.urlopen(url, timeout=5)
                            worker_aliases = _json.loads(resp.read().decode())
                            # Merge: Worker aliases fill gaps (don't overwrite local)
                            for did, info in worker_aliases.items():
                                if did not in aliases:
                                    aliases[did] = info
                        except Exception:
                            pass
    except Exception:
        pass

    return aliases


@router.post("/devices/aliases/prune-orphans")
def prune_orphan_aliases(body: dict | None = None):
    """移除本机 device_aliases.json 中、当前 ADB 管理集合里不存在的设备键。

    仅处理本机 JSON；集群协调器不会替 Worker 删别名。建议先 dry_run。
    Body: dry_run (默认 false), backup (默认 true)
    """
    import shutil
    from datetime import datetime
    from src.device_control.device_manager import get_device_manager

    body = body or {}
    dry_run = bool(body.get("dry_run", False))
    do_backup = bool(body.get("backup", True))

    manager = get_device_manager(_config_path)
    manager.discover_devices(force=True)
    aliases = _load_aliases()
    managed = {d.device_id for d in manager.get_all_devices()}
    orphan_keys = [k for k in aliases if k not in managed]

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_remove": len(orphan_keys),
            "keys": orphan_keys[:500],
        }
    if not orphan_keys:
        return {"ok": True, "removed": 0, "keys": [], "backup": None}

    backup_info: dict | None = None
    if do_backup:
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bdir = data_dir() / "backups" / f"aliases_prune_{ts}"
            bdir.mkdir(parents=True, exist_ok=True)
            if _device_aliases_path.is_file():
                shutil.copy2(_device_aliases_path, bdir / "device_aliases.json")
            backup_info = {"backup_dir": str(bdir), "files": [str(bdir / "device_aliases.json")]}
        except Exception as e:
            logger.warning("[aliases-prune] backup failed: %s", e)
            backup_info = {"error": str(e)}

    new_aliases = {k: v for k, v in aliases.items() if k in managed}
    _save_aliases(new_aliases)
    return {
        "ok": True,
        "removed": len(orphan_keys),
        "keys": orphan_keys[:500],
        "backup": backup_info,
    }


@router.post("/devices/{device_id}/alias")
def set_device_alias(device_id: str, body: dict):
    """Set alias, number, remark for a device."""
    from ..api import _audit

    aliases = _load_aliases()
    entry = dict(aliases.get(device_id, {}))
    _lid = _local_managed_device_ids()
    if "alias" in body and "number" not in body:
        entry["alias"] = body["alias"]
    if "number" in body:
        n = int(body["number"])
        sc, hn = resolve_host_scope_for_device(device_id, entry, local_device_ids=_lid)
        entry = apply_slot_and_labels(entry, n, sc, hn or entry.get("host_name", ""))
    if "remark" in body:
        entry["remark"] = body["remark"]
    aliases[device_id] = entry
    _save_aliases(aliases)
    _audit("set_device_alias", target=device_id, detail=str(entry))
    return {"ok": True, "device_id": device_id, **entry}


# ── POST /devices/auto-number ──

@router.post("/devices/auto-number")
def auto_number_devices(body: dict = None):
    """Auto-assign sequential numbers to all devices and optionally deploy wallpapers."""
    import traceback
    try:
        from src.device_control.device_manager import get_device_manager
        from src.utils.wallpaper_generator import get_wallpaper_auto_manager
        from ..api import _audit

        body = body or {}
        deploy_wp = body.get("deploy_wallpaper", False)
        manager = get_device_manager(_config_path)
        wp_mgr = get_wallpaper_auto_manager()
        results = wp_mgr.ensure_all_numbered(manager)
        aliases = _load_aliases()
        result = {"total": len(results), "aliases": aliases}
        if deploy_wp:
            result["wallpaper"] = {
                "success": sum(1 for v in results.values() if v.get("deployed")),
                "failed": sum(1 for v in results.values() if not v.get("deployed")),
            }
        _audit("auto_number_devices", detail=f"total={result.get('total',0)}")
        return result
    except Exception as e:
        logger.error("auto-number failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(500, detail=f"auto-number error: {e}")


# ── PUT /devices/{id}/number ──

@router.put("/devices/{device_id}/number")
def set_device_number(device_id: str, body: dict):
    """Manually set a device's number. Handles conflicts by swapping."""
    from ..api import _resolve_device_with_manager, _audit

    new_num = int(body.get("number", 0))
    deploy_wp = body.get("deploy_wallpaper", True)
    if new_num <= 0:
        raise HTTPException(status_code=400, detail="编号必须为正整数")

    # 集群设备在主控上没有本地 ADB 连接，捕获 404 并标记为远程设备
    try:
        did, manager = _resolve_device_with_manager(device_id)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        did, manager = device_id, None  # 集群设备：manager=None，后续用代理部署壁纸

    # 分域槽位：同 host_scope 内 slot 唯一；跨 Worker/主控可同为 01
    local_aliases = _load_aliases()          # 主控本机文件，用于持久化
    aliases = _load_aliases_global()         # 全集群合并视图，用于冲突检测

    _lid = _local_managed_device_ids()
    scope, hname = resolve_host_scope_for_device(did, aliases.get(did), local_device_ids=_lid)

    conflict_did = None
    for other_did, oentry in aliases.items():
        if other_did == did:
            continue
        oscope, _ = resolve_host_scope_for_device(other_did, oentry, local_device_ids=_lid)
        if oscope != scope:
            continue
        if get_slot(oentry) == new_num:
            conflict_did = other_did
            break

    old_num = get_slot(aliases.get(did, {}))
    if conflict_did and old_num:
        ce = dict(aliases.get(conflict_did, {}))
        chn = ce.get("host_name") or hname
        aliases[conflict_did] = apply_slot_and_labels(ce, old_num, scope, chn)
    elif conflict_did:
        ce = dict(aliases.get(conflict_did, {}))
        chn = ce.get("host_name") or hname
        free = next_free_slot_resolved(
            aliases, scope, 1, 999,
            local_device_ids=_lid, exclude_device_id=conflict_did,
        )
        aliases[conflict_did] = apply_slot_and_labels(ce, free, scope, chn)

    entry = dict(aliases.get(did, {}))
    entry = apply_slot_and_labels(entry, new_num, scope, hname or entry.get("host_name", ""))
    aliases[did] = entry

    # 识别属于 Worker 的设备 ID（不应写入主控本地 aliases）
    _worker_dids: set = set()
    try:
        from src.host.multi_host import get_cluster_coordinator as _gcc2
        _coord2 = _gcc2()
        if _coord2:
            for _hid2, _hi2 in _coord2._hosts.items():
                for _d2 in (getattr(_hi2, "devices", None) or []):
                    _worker_dids.add(_d2.get("device_id", ""))
    except Exception:
        pass

    # Worker 专属设备（在 Worker 列表里 AND 不是本机 ADB 直连）
    try:
        from src.device_control.device_manager import get_device_manager as _gdm4
        _local_managed4 = {d.device_id for d in _gdm4(_config_path).get_all_devices()}
    except Exception:
        _local_managed4 = set()
    _worker_only = _worker_dids - _local_managed4  # 纯 Worker 设备（coordinator 没有 ADB 连接）

    # 只把本机设备的 alias 写回主控本地文件，不污染纯 Worker 设备记录
    for k in list(aliases.keys()):
        if (k in local_aliases or k == did) and k not in _worker_only:
            local_aliases[k] = aliases[k]
    # 如果被 swap 的 conflict_did 也是本机设备（非纯 Worker），一并保存
    if conflict_did and conflict_did in local_aliases and conflict_did not in _worker_only:
        local_aliases[conflict_did] = aliases[conflict_did]
    # 主动清除 coordinator 本地 aliases 中的真正 stale：纯 Worker 设备
    _stale_local = [k for k in list(local_aliases.keys()) if k in _worker_only]
    if _stale_local:
        for k in _stale_local:
            del local_aliases[k]
        logger.info("[set_device_number] cleaned %d stale Worker entries from coordinator local aliases", len(_stale_local))
    _save_aliases(local_aliases)

    # 同步变更到拥有 did / conflict_did 的 Worker
    try:
        from src.host.multi_host import get_cluster_coordinator
        import urllib.request as _ur, json as _jj
        coord = get_cluster_coordinator()
        if coord:
            for hid, hinfo in coord._hosts.items():
                if not getattr(hinfo, 'online', False):
                    continue
                host_devs = {d.get('device_id') for d in (getattr(hinfo, 'devices', None) or [])}
                targets = {d for d in (did, conflict_did) if d and d in host_devs}
                if not targets:
                    continue
                try:
                    # 只把该 Worker 实际拥有的设备 alias 推送过去
                    worker_aliases_update = {k: aliases[k] for k in host_devs if k in aliases}
                    url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/renumber-all"
                    data = _jj.dumps({"aliases": worker_aliases_update, "deploy_wallpaper": False}).encode()
                    req = _ur.Request(url, data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
                    _ur.urlopen(req, timeout=10)
                    logger.debug("[alias-sync] Pushed aliases to Worker %s (devices: %s)", hid, targets)
                except Exception as _e:
                    logger.warning("[alias-sync] Worker %s sync failed: %s", hid, _e)
    except Exception:
        pass

    from src.device_control.device_registry import get_device_registry
    registry = get_device_registry()
    info = manager.get_device_info(did) if manager else None
    if info and info.fingerprint:
        _al = aliases.get(did, {}).get("alias") or f"{new_num:02d}号"
        registry.register(
            info.fingerprint, did, new_num, _al,
            imei=info.imei, hw_serial=info.hw_serial,
            android_id=info.android_id, model=info.model,
        )

    result = {
        "ok": True,
        "device_id": did,
        "number": new_num,
        "slot": new_num,
        "host_scope": scope,
        "display_label": aliases.get(did, {}).get("display_label"),
    }

    if conflict_did:
        conflict_num = aliases[conflict_did]["number"]
        result["swapped_with"] = {"device_id": conflict_did, "number": conflict_num}
        if deploy_wp:
            _deploy_wallpaper_smart(manager, conflict_did, conflict_num)

    if deploy_wp:
        info = manager.get_device_info(did) if manager else None
        display_name = info.display_name if info else ""
        _deploy_wallpaper_smart(manager, did, new_num, display_name=display_name)

    _audit("set_device_number", detail=f"device={did[:8]}, num={new_num}")
    return result


# ── POST /devices/batch-number ──

@router.post("/devices/batch-number")
def batch_set_numbers(body: dict):
    """批量设置多台设备编号，同步到 Worker，可选部署壁纸。
    body: {assignments:[{device_id,number},...], deploy_wallpaper:true}
    """
    from ..api import _audit
    assignments = body.get("assignments", [])
    deploy_wp = body.get("deploy_wallpaper", True)
    if not assignments:
        raise HTTPException(400, "assignments 不能为空")

    # 构建本次变更映射
    new_numbers = {}
    for item in assignments:
        did = item.get("device_id", "")
        num = int(item.get("number", 0))
        if did and num > 0:
            new_numbers[did] = num

    # 获取全局视图，在其上打补丁
    global_aliases = _load_aliases_global()
    local_aliases = _load_aliases()
    _lid = _local_managed_device_ids()

    for did, num in new_numbers.items():
        entry = dict(global_aliases.get(did) or {})
        sc, hn = resolve_host_scope_for_device(did, entry, local_device_ids=_lid)
        entry = apply_slot_and_labels(
            entry, int(num), sc, hn or entry.get("host_name", ""),
        )
        global_aliases[did] = entry
        if did in local_aliases:
            local_aliases[did] = entry

    _save_aliases(local_aliases)

    # 按 Worker 分组同步
    sync_results = {}
    try:
        from src.host.multi_host import get_cluster_coordinator
        import urllib.request as _ur, json as _jj
        coord = get_cluster_coordinator()
        if coord:
            for hid, hinfo in coord._hosts.items():
                if not getattr(hinfo, "online", False):
                    continue
                host_devs = {d.get("device_id") for d in (getattr(hinfo, "devices", None) or [])}
                touched = [d for d in new_numbers if d in host_devs]
                if not touched:
                    continue
                patch = {k: global_aliases[k] for k in host_devs if k in global_aliases}
                try:
                    url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/renumber-all"
                    data = _jj.dumps({"aliases": patch, "deploy_wallpaper": False}).encode()
                    req = _ur.Request(url, data=data, method="POST",
                                      headers={"Content-Type": "application/json"})
                    _ur.urlopen(req, timeout=15)
                    sync_results[hid] = len(touched)
                    logger.info("[batch-number] synced %d devices to Worker %s", len(touched), hid)
                except Exception as e:
                    logger.warning("[batch-number] Worker %s sync failed: %s", hid, e)
                    sync_results[hid] = -1
    except Exception as e:
        logger.warning("[batch-number] cluster sync error: %s", e)

    # 壁纸部署（后台线程，不阻塞响应）
    if deploy_wp:
        import threading
        _ga_snap = dict(global_aliases)
        _nn_snap = dict(new_numbers)
        def _bg_deploy():
            for did, num in _nn_snap.items():
                try:
                    _deploy_wallpaper_smart(None, did, num,
                        display_name=(_ga_snap.get(did) or {}).get("display_name", ""))
                except Exception:
                    pass
        threading.Thread(target=_bg_deploy, daemon=True).start()

    _audit("batch_set_numbers", detail=f"count={len(new_numbers)}, deploy_wp={deploy_wp}")
    return {"ok": True, "total": len(new_numbers), "synced": sync_results}


# ── GET /devices/conflicts — 返回全局重复编号摘要 ──

@router.get("/devices/conflicts")
def get_conflicts():
    """返回同一 host_scope 下 slot 重复的列表（跨机同为 01 不算冲突）。"""
    global_aliases = _load_aliases_global()

    # 收集 Worker 设备归属
    worker_devices: dict[str, str] = {}  # did → host_id
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if coord:
            for hid, hinfo in coord._hosts.items():
                for d in (getattr(hinfo, "devices", None) or []):
                    worker_devices[d.get("device_id", "")] = hid
    except Exception:
        pass

    # 主控本地文件中的 stale 记录 = 在 Worker 列表里 AND 主控 ADB 当前没有管理
    local_aliases = _load_aliases()
    _local_dev_ids = _local_managed_device_ids()
    stale_in_local = [did for did in local_aliases
                      if did in worker_devices and did not in _local_dev_ids]

    # 检测 (host_scope, slot) 重复
    key_map: dict[tuple[str, int], list] = {}
    for did, info in global_aliases.items():
        sc, _ = resolve_host_scope_for_device(did, info or {}, local_device_ids=_local_dev_ids)
        sl = get_slot(info or {})
        if sl <= 0:
            continue
        key_map.setdefault((sc, sl), []).append({
            "device_id": did,
            "alias": (info or {}).get("alias", f"{sl:02d}号"),
            "display_label": (info or {}).get("display_label"),
            "host_scope": sc,
            "host_id": worker_devices.get(did, sc),
            "in_local": did in local_aliases,
        })

    conflicts = []
    for (sc, sl), devs in sorted(key_map.items(), key=lambda x: (x[0][0], x[0][1])):
        if len(devs) > 1:
            conflicts.append({"host_scope": sc, "slot": sl, "devices": devs})

    return {
        "conflict_count": len(conflicts),
        "stale_local_count": len(stale_in_local),
        "conflicts": conflicts,
        "stale_in_coordinator": stale_in_local,
    }


# ── POST /devices/self-fix-conflicts — Worker 侧内部自我修复重复编号 ──

@router.post("/devices/self-fix-conflicts")
def self_fix_conflicts():
    """Worker 自我修复本地 aliases 中「同 host_scope 下 slot 重复」。"""
    aliases = _load_aliases()
    from src.device_control.device_manager import get_device_manager
    try:
        local_ids = {d.device_id for d in get_device_manager(_config_path).get_all_devices()}
    except Exception:
        local_ids = set(aliases.keys())

    key_map: dict[tuple[str, int], list] = {}
    for did, info in aliases.items():
        sc, _ = resolve_host_scope_for_device(did, info or {}, local_device_ids=local_ids)
        sl = get_slot(info or {})
        if sl > 0:
            key_map.setdefault((sc, sl), []).append(did)

    conflicts = {k: v for k, v in key_map.items() if len(v) > 1}
    if not conflicts:
        return {"ok": True, "fixed": 0, "renumbered": {}}

    renumbered = {}
    for (_sc, _sl), dids in conflicts.items():
        online = [d for d in dids if d in local_ids]
        keep = online[0] if online else dids[0]
        for did in dids:
            if did == keep:
                continue
            entry = dict(aliases.get(did, {}))
            sc, hn = resolve_host_scope_for_device(did, entry, local_device_ids=local_ids)
            new_n = next_free_slot_resolved(
                aliases, sc, 1, 999,
                local_device_ids=local_ids, exclude_device_id=did,
            )
            aliases[did] = apply_slot_and_labels(entry, new_n, sc, hn or entry.get("host_name", ""))
            renumbered[did] = new_n

    _save_aliases(aliases)
    logger.info("[self-fix-conflicts] fixed %d internal conflicts", len(renumbered))
    return {"ok": True, "fixed": len(renumbered), "renumbered": renumbered}


# ── POST /devices/fix-conflicts — 智能修复重复编号 ──

@router.post("/devices/fix-conflicts")
def fix_conflicts(body: dict = None):
    """根治重复编号（内部循环至零，一次调用保证清零）：
    1. 清除主控本地 alias 中属于 Worker 的历史遗留（根本原因）
    2. 循环检测+修复剩余真实冲突（最多5轮），每轮同步到对应 Worker
    3. 可选后台部署壁纸
    """
    import urllib.request as _ur, json as _jj, threading
    from ..api import _audit
    deploy_wp = (body or {}).get("deploy_wallpaper", True)

    # 获取 Worker 设备归属（不变，只需加载一次）
    worker_devices: dict[str, str] = {}
    worker_device_set_by_host: dict[str, set] = {}
    cluster_online_dids: set[str] = set()
    coord = None
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if coord:
            for hid, hinfo in coord._hosts.items():
                devs = getattr(hinfo, "devices", None) or []
                s = {d.get("device_id", "") for d in devs}
                worker_device_set_by_host[hid] = s
                for did in s:
                    worker_devices[did] = hid
                    if getattr(hinfo, "online", False):
                        cluster_online_dids.add(did)
    except Exception:
        pass

    # 一次性清除主控本地 alias 中的真正 stale 记录：
    # 在 Worker 列表里 AND 主控 ADB 当前没有直连（非本地设备）
    local_aliases = _load_aliases()
    try:
        from src.device_control.device_manager import get_device_manager as _gdm
        _local_managed = {d.device_id for d in _gdm(_config_path).get_all_devices()}
    except Exception:
        _local_managed = set()
    stale = [did for did in list(local_aliases.keys())
             if did in worker_devices and did not in _local_managed]
    for did in stale:
        del local_aliases[did]
    _save_aliases(local_aliases)
    logger.info("[fix-conflicts] removed %d stale Worker entries from coordinator local aliases", len(stale))

    # 先让每个 Worker 自我修复内部冲突（处理 coordinator 全局视图看不到的 Worker 内部冲突）
    if coord:
        import time
        for hid, hinfo in coord._hosts.items():
            if not getattr(hinfo, "online", False):
                continue
            try:
                url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/self-fix-conflicts"
                req = _ur.Request(url, data=b'{}', method="POST",
                                  headers={"Content-Type": "application/json"})
                resp = _ur.urlopen(req, timeout=10)
                r = _jj.loads(resp.read().decode())
                if r.get("fixed", 0) > 0:
                    logger.info("[fix-conflicts] Worker %s self-fixed %d internal conflicts", hid, r["fixed"])
            except Exception as e:
                logger.warning("[fix-conflicts] Worker %s self-fix failed: %s", hid, e)
        time.sleep(0.5)  # 等 Workers 完成保存

    # 循环修复真实冲突（最多5轮）
    total_renumbered: dict[str, int] = {}
    rounds_done = 0

    _lid_fc = _local_managed_device_ids()
    for round_n in range(5):
        rounds_done = round_n + 1
        global_aliases = _load_aliases_global()
        key_map: dict[tuple[str, int], list] = {}
        for did, info in global_aliases.items():
            sc, _ = resolve_host_scope_for_device(did, info or {}, local_device_ids=_lid_fc)
            sl = get_slot(info or {})
            if sl > 0:
                key_map.setdefault((sc, sl), []).append(did)

        conflicts = {k: v for k, v in key_map.items() if len(v) > 1}
        if not conflicts:
            break

        round_renumbered: dict[str, int] = {}

        for (_sc, _sl), dids in conflicts.items():
            online = [d for d in dids if d in cluster_online_dids]
            keep = online[0] if online else dids[0]
            for did in dids:
                if did == keep:
                    continue
                entry = dict(global_aliases.get(did, {}))
                sc, hn = resolve_host_scope_for_device(did, entry, local_device_ids=_lid_fc)
                new_n = next_free_slot_resolved(
                    global_aliases, sc, 1, 999,
                    local_device_ids=_lid_fc, exclude_device_id=did,
                )
                entry = apply_slot_and_labels(entry, new_n, sc, hn or entry.get("host_name", ""))
                round_renumbered[did] = new_n
                global_aliases[did] = entry
                if did not in worker_devices:
                    local_aliases[did] = entry
                    _save_aliases(local_aliases)

        total_renumbered.update(round_renumbered)

        # 同步本轮 renumbered 设备到对应 Worker
        if round_renumbered and coord:
            for hid, host_devs in worker_device_set_by_host.items():
                hinfo = coord._hosts.get(hid)
                if not getattr(hinfo, "online", False):
                    continue
                touched = [d for d in round_renumbered if d in host_devs]
                if not touched:
                    continue
                patch = {k: global_aliases[k] for k in host_devs if k in global_aliases}
                url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/renumber-all"
                data = _jj.dumps({"aliases": patch, "deploy_wallpaper": False}).encode()
                req = _ur.Request(url, data=data, method="POST",
                                  headers={"Content-Type": "application/json"})
                try:
                    _ur.urlopen(req, timeout=15)
                    logger.info("[fix-conflicts] round %d: synced %d devices to Worker %s", round_n+1, len(touched), hid)
                except Exception as e:
                    logger.warning("[fix-conflicts] Worker %s sync failed: %s", hid, e)

    # 后台部署壁纸（仅 renumbered 设备）
    if deploy_wp and total_renumbered:
        _ga_snap = dict(global_aliases)
        _rn_snap = dict(total_renumbered)
        def _bg():
            for did, num in _rn_snap.items():
                try:
                    _deploy_wallpaper_smart(None, did, num,
                        display_name=(_ga_snap.get(did) or {}).get("display_name", ""))
                except Exception:
                    pass
        threading.Thread(target=_bg, daemon=True).start()

    _audit("fix_conflicts", detail=f"stale={len(stale)}, renumbered={len(total_renumbered)}, rounds={rounds_done}")
    return {
        "ok": True,
        "rounds": rounds_done,
        "removed_stale": len(stale),
        "renumbered": total_renumbered,
        "message": f"已清理 {len(stale)} 条历史遗留，修复 {len(total_renumbered)} 台设备编号（{rounds_done} 轮）",
    }


# ── POST /devices/auto-assign-segments — 按编号段智能分配编号 ──

@router.post("/devices/auto-assign-segments")
def auto_assign_segments(body: dict = None):
    """按编号段为各主机分配槽位；同一 host 段内不撞号，不同主机可同为 01。
    body: {deploy_wallpaper: false}
    """
    import json as _json
    from src.host.device_alias_labels import load_local_cluster_identity
    deploy_wp = (body or {}).get("deploy_wallpaper", False)

    # 加载编号段配置
    ranges: dict = {}
    if _ranges_path.exists():
        with open(_ranges_path, encoding="utf-8") as f:
            ranges = _json.load(f)

    global_aliases = _load_aliases_global()
    _lid = _local_managed_device_ids()

    # 按 host 分组设备
    host_to_devs: dict[str, list] = {}
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if coord:
            for hid, hinfo in coord._hosts.items():
                devs = [d.get("device_id") for d in (getattr(hinfo, "devices", None) or [])
                        if d.get("device_id")]
                if devs:
                    host_to_devs[hid] = devs
    except Exception:
        pass

    try:
        from src.device_control.device_manager import get_device_manager
        mgr = get_device_manager(_config_path)
        coord_devs = [d.device_id for d in mgr.get_all_devices()]
        if coord_devs:
            chid, _ = load_local_cluster_identity()
            host_to_devs.setdefault(chid or "coordinator", coord_devs)
    except Exception:
        pass

    if not host_to_devs:
        return {"ok": True, "assigned": 0, "assignments": [], "message": "没有可分配的设备"}

    assignments = []

    for hid, dev_ids in host_to_devs.items():
        rng = ranges.get(hid) or ranges.get("coordinator") or {}
        seg_start = int(rng.get("start", 1))
        seg_end = int(rng.get("end", 99))
        used_here = used_slots_resolved(global_aliases, hid, local_device_ids=_lid)

        for did in dev_ids:
            if not did:
                continue
            if get_slot(global_aliases.get(did, {})):
                continue
            available = [n for n in range(seg_start, seg_end + 1) if n not in used_here]
            if available:
                n = min(available)
            else:
                n = 1
                while n in used_here:
                    n += 1
            used_here.add(n)
            assignments.append({"device_id": did, "number": n})

    if not assignments:
        return {"ok": True, "assigned": 0, "assignments": [],
                "message": "所有设备已有编号，无需分配"}

    # 复用 batch_set_numbers 完成同步和壁纸部署
    result = batch_set_numbers({"assignments": assignments, "deploy_wallpaper": deploy_wp})
    result["assigned"] = len(assignments)
    result["assignments"] = assignments
    return result


# ── GET/POST /cluster/number-ranges — Worker 编号段配置 ──

_ranges_path = config_file("number_ranges.json")

@router.get("/cluster/number-ranges")
def get_number_ranges():
    """返回各 Worker 的编号段配置。"""
    if _ranges_path.exists():
        import json
        with open(_ranges_path, encoding="utf-8") as f:
            return json.load(f)
    return {}

@router.post("/cluster/number-ranges")
def save_number_ranges(body: dict):
    """保存各 Worker 的编号段配置。
    body: {"w03": {"start": 1, "end": 20}, "worker-175": {"start": 21, "end": 40}}
    """
    import json
    _ranges_path.parent.mkdir(parents=True, exist_ok=True)
    # 验证格式
    cleaned = {}
    for host_id, cfg in body.items():
        s = int(cfg.get("start", 1))
        e = int(cfg.get("end", 99))
        if s > 0 and e >= s:
            cleaned[host_id] = {"start": s, "end": e}
    with open(_ranges_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)
    return {"ok": True, "ranges": cleaned}


@router.get("/cluster/number-ranges/suggest")
def suggest_number_ranges():
    """根据各 Worker 当前设备数量智能建议不重叠的编号段（含1.5x缓冲）。"""
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        overview = coord.get_overview() if coord else {"hosts": []}
    except Exception:
        overview = {"hosts": []}
    hosts = sorted(overview.get("hosts", []), key=lambda h: h["host_id"])
    suggestions = {}
    current_start = 1
    for h in hosts:
        cnt = max(h.get("devices", 0), 5)
        # 1.5x 缓冲 + 最小 10，向上取整到10
        capacity = ((int(cnt * 1.5) + 10 + 9) // 10) * 10
        suggestions[h["host_id"]] = {
            "start": current_start,
            "end": current_start + capacity - 1,
            "host_name": h.get("host_name", h["host_id"]),
            "current_devices": h.get("devices", 0),
            "capacity": capacity,
        }
        current_start += capacity
    return {"suggestions": suggestions, "total_capacity": current_start - 1}


# ── POST /devices/renumber-all ──

@router.post("/devices/renumber-all")
def renumber_all_devices(body: dict = None):
    """Renumber all devices sequentially 01, 02, 03... and deploy wallpapers."""
    import traceback
    try:
        return _do_renumber_all(body)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("renumber-all failed: %s\n%s", e, traceback.format_exc())
        raise HTTPException(500, detail=f"renumber error: {e}")


def _do_renumber_all(body: dict = None):
    from src.device_control.device_manager import get_device_manager
    from src.device_control.device_registry import get_device_registry
    from ..api import _audit

    body = body or {}
    deploy_wp = body.get("deploy_wallpaper", True)
    # 如果 body 里传了 aliases（从主控推过来），直接用
    pushed_aliases = body.get("aliases")
    manager = get_device_manager(_config_path)
    all_devs = manager.get_all_devices()

    aliases = _load_aliases()
    registry = get_device_registry()

    if pushed_aliases:
        # Worker 收到主控推过来的编号，只更新本地设备的别名
        local_ids = {d.device_id for d in all_devs}
        for did, info in pushed_aliases.items():
            if did in local_ids:
                aliases[did] = info
        _save_aliases(aliases)
    else:
        # 主控/独立模式：统一编号所有设备（本地+集群）
        all_items = []  # [(device_id, display_name, host_name, is_cluster)]

        from src.host.device_alias_labels import load_local_cluster_identity, apply_slot_and_labels
        chid, chname = load_local_cluster_identity()

        all_rows = []  # (device_id, display_name, host_scope, host_name)
        for d in all_devs:
            all_rows.append((
                d.device_id,
                d.display_name or d.device_id[:8],
                chid or "coordinator",
                chname or "",
            ))

        is_coordinator = False
        try:
            import yaml
            cluster_cfg_path = config_file("cluster.yaml")
            if cluster_cfg_path.exists():
                with open(cluster_cfg_path, encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                is_coordinator = cfg.get("role") == "coordinator"
        except Exception:
            pass

        cluster_hosts_info = []
        if is_coordinator:
            try:
                from src.host.multi_host import get_cluster_coordinator
                import urllib.request, json as _json
                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    local_ids = {d.device_id for d in all_devs}
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        cluster_hosts_info.append(host)
                        try:
                            url = f"http://{host['host_ip']}:{host.get('port', 8000)}/devices"
                            resp = urllib.request.urlopen(url, timeout=10)
                            worker_devs = _json.loads(resp.read().decode())
                            for wd in worker_devs:
                                wdid = wd.get("device_id", "")
                                if wdid and wdid not in local_ids:
                                    all_rows.append((
                                        wdid,
                                        wd.get("display_name", wdid[:8]),
                                        host.get("host_id", "") or "worker",
                                        host.get("host_name", ""),
                                    ))
                                    local_ids.add(wdid)
                        except Exception as e:
                            logger.warning("[编号] 获取Worker %s 设备列表失败: %s",
                                          host.get("host_name"), e)
            except Exception:
                pass

        seen_ids: set[str] = set()
        unique_rows: list = []
        for row in all_rows:
            if row[0] not in seen_ids:
                seen_ids.add(row[0])
                unique_rows.append(row)
        all_rows = unique_rows

        from collections import defaultdict
        by_scope: dict[str, list] = defaultdict(list)
        for did, dname, hscope, hname in all_rows:
            hs = hscope or "unknown"
            by_scope[hs].append((did, dname, hname))

        aliases = {}
        for hscope in sorted(by_scope.keys()):
            rows = sorted(by_scope[hscope], key=lambda x: x[1])
            for i, (did, dname, hname) in enumerate(rows, 1):
                base = {"display_name": dname}
                if hname:
                    base["host_name"] = hname
                aliases[did] = apply_slot_and_labels(base, i, hscope, hname or chname or "")

        for d in all_devs:
            if d.fingerprint and d.device_id in aliases:
                ent = aliases[d.device_id]
                registry.register(
                    d.fingerprint, d.device_id, ent["number"], ent.get("alias", f'{ent["number"]:02d}号'),
                    imei=d.imei, hw_serial=d.hw_serial,
                    android_id=d.android_id, model=d.model,
                )
        _save_aliases(aliases)

    # 部署壁纸（并行）
    deployed = 0
    if deploy_wp:
        from src.utils.wallpaper_generator import deploy_wallpapers_parallel
        from src.device_control.device_manager import DeviceStatus
        deploy_list = []
        for dev in all_devs:
            if dev.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY):
                entry = aliases.get(dev.device_id)
                if entry:
                    deploy_list.append((
                        dev.device_id,
                        entry["number"],
                        dev.display_name or "",
                    ))
        if deploy_list:
            results = deploy_wallpapers_parallel(manager, deploy_list, max_workers=4)
            deployed = sum(1 for v in results.values() if v)

        # 集群：推 aliases 到 Worker，让 Worker 本地部署壁纸
        if not pushed_aliases and cluster_hosts_info:
            import urllib.request, json as _json
            for host in cluster_hosts_info:
                if not host.get("online"):
                    continue
                try:
                    url = f"http://{host['host_ip']}:{host.get('port', 8000)}/devices/renumber-all"
                    data = _json.dumps({
                        "deploy_wallpaper": True,
                        "aliases": aliases,
                    }).encode()
                    req = urllib.request.Request(url, data=data, method="POST",
                                                headers={"Content-Type": "application/json"})
                    resp = urllib.request.urlopen(req, timeout=120)
                    wr = _json.loads(resp.read().decode())
                    deployed += wr.get("deployed", 0)
                    logger.info("[编号] Worker %s 壁纸部署: %d 台",
                               host.get("host_name"), wr.get("deployed", 0))
                except Exception as e:
                    logger.warning("[编号] Worker %s 壁纸部署失败: %s",
                                  host.get("host_name"), e)

    total = len([d for d in aliases.values() if "number" in d])
    _audit("renumber_all_devices", detail=f"total={total}, deployed={deployed}")
    return {"ok": True, "total": total, "deployed": deployed,
            "aliases": aliases}


# ── GET /devices/registry ──

@router.get("/devices/registry")
def get_device_registry_info():
    """Get the fingerprint registry data."""
    from src.device_control.device_registry import get_device_registry
    registry = get_device_registry()
    return registry.get_all()


# ── Wallpaper endpoints ──

@router.post("/devices/{device_id}/wallpaper")
def set_device_wallpaper(device_id: str, body: dict = None):
    """Generate and deploy a numbered wallpaper to a device."""
    from src.device_control.device_manager import get_device_manager
    from ..api import _resolve_device_with_manager, _load_config

    body = body or {}
    # 主控+本机 USB 时须刷新发现，避免 coordinator 缓存或跳过逻辑导致 404
    get_device_manager(_config_path).discover_devices(force=True)

    # 集群设备在主控无本地 ADB，捕获 404 后走代理
    try:
        did, manager = _resolve_device_with_manager(device_id)
    except HTTPException as e:
        if e.status_code != 404:
            raise
        did, manager = device_id, None

    number = int(body.get("number", 0))
    if not number:
        aliases = _load_aliases()
        number = aliases.get(did, {}).get("number", 0)
    if not number:
        cfg = _load_config(_config_path)
        devices_cfg = cfg.get("devices", {})
        sorted_keys = sorted(devices_cfg.keys(),
                             key=lambda k: devices_cfg[k].get("display_name", ""))
        number = sorted_keys.index(did) + 1 if did in sorted_keys else 1

    display_name = body.get("display_name", "")
    if not display_name:
        info = manager.get_device_info(did) if manager else None
        display_name = info.display_name if info else f"Phone-{number}"

    ok = _deploy_wallpaper_smart(manager, did, number, display_name)
    if not ok:
        raise HTTPException(status_code=500, detail="壁纸部署失败，请检查设备ADB连接")
    # Worker 代理模式下（manager=None），本机 aliases 可能没有该设备，
    # 但 Worker 侧已通过 _update_wallpaper_status 记录；此处尝试更新本机（如存在则覆盖）
    if not manager:
        _update_wallpaper_status(did, number)
    return {"ok": True, "device_id": did, "number": number}


@router.post("/devices/wallpaper/all")
def set_all_wallpapers():
    """Deploy numbered wallpapers to all configured devices (local + all cluster workers)."""
    import urllib.request, json as _j
    from src.device_control.device_manager import get_device_manager
    from ..api import _load_config

    cfg = _load_config(_config_path)
    devices_cfg = cfg.get("devices", {})
    manager = get_device_manager(_config_path)
    from src.utils.wallpaper_generator import deploy_all_wallpapers

    # 本地设备
    results = deploy_all_wallpapers(manager, devices_cfg)

    # 广播到所有在线 Worker
    try:
        from src.host.multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        if coord:
            for hid, hinfo in coord._hosts.items():
                if not getattr(hinfo, 'online', False):
                    continue
                try:
                    url = f"http://{hinfo.host_ip}:{hinfo.port or 8000}/devices/wallpaper/all"
                    req = urllib.request.Request(url, data=b'{}', method="POST",
                                                 headers={"Content-Type": "application/json"})
                    resp = urllib.request.urlopen(req, timeout=60)
                    wr = _j.loads(resp.read().decode())
                    worker_success = wr.get("success", 0)
                    results[f"_worker_{hid}"] = worker_success > 0
                    logger.info("[wallpaper/all] Worker %s: %d 台成功", hid, worker_success)
                except Exception as e:
                    logger.warning("[wallpaper/all] Worker %s 失败: %s", hid, e)
                    results[f"_worker_{hid}"] = False
    except Exception as e:
        logger.warning("[wallpaper/all] 集群广播异常: %s", e)

    return {
        "total": len(results),
        "success": sum(1 for v in results.values() if v),
        "failed": sum(1 for v in results.values() if not v),
        "details": {k[:12]: v for k, v in results.items()},
    }



# ── 壁纸部署进度追踪 ────────────────────────────────────────────────────────
import threading as _th_wp
import time as _time_wp
import uuid as _uuid_wp

_wp_jobs: dict = {}
_wp_jobs_lock = _th_wp.Lock()


@router.post("/devices/wallpaper/deploy-outdated")
def deploy_outdated_wallpapers():
    """仅为壁纸已过期（wallpaper_number != number 或从未部署）的设备部署壁纸。
    返回 job_id 供前端轮询进度。
    """
    import threading
    global_aliases = _load_aliases_global()
    outdated = [
        (did, info["number"])
        for did, info in global_aliases.items()
        if info.get("number") and info.get("number") != info.get("wallpaper_number")
    ]
    if not outdated:
        return {"ok": True, "total": 0, "job_id": None, "message": "所有设备壁纸均为最新，无需部署"}

    job_id = _uuid_wp.uuid4().hex[:8]
    with _wp_jobs_lock:
        _wp_jobs[job_id] = {
            "total": len(outdated),
            "done": 0,
            "failed": 0,
            "running": True,
            "started": _time_wp.time(),
        }

    def _bg():
        from src.device_control.device_manager import get_device_manager
        mgr = get_device_manager(_config_path)
        ok_count = 0
        for did, num in outdated:
            ok = False
            try:
                ok = _deploy_wallpaper_smart(mgr, did, num)
                if ok:
                    ok_count += 1
            except Exception as e:
                logger.warning("[wp-outdated] %s failed: %s", did[:8], e)
            with _wp_jobs_lock:
                j = _wp_jobs.get(job_id)
                if j:
                    if ok:
                        j["done"] += 1
                    else:
                        j["failed"] += 1
        with _wp_jobs_lock:
            j = _wp_jobs.get(job_id)
            if j:
                j["running"] = False
        logger.info("[wp-outdated] job %s: %d/%d OK", job_id, ok_count, len(outdated))

    threading.Thread(target=_bg, daemon=True).start()
    return {"ok": True, "total": len(outdated), "job_id": job_id,
            "message": f"正在后台为 {len(outdated)} 台设备补充壁纸"}


@router.get("/devices/wallpaper/deploy-status/{job_id}")
def get_wallpaper_deploy_status(job_id: str):
    """查询壁纸批量部署任务的实时进度。"""
    now = _time_wp.time()
    with _wp_jobs_lock:
        # 清理超过10分钟的已完成任务
        stale = [jid for jid, j in _wp_jobs.items()
                 if not j["running"] and now - j["started"] > 600]
        for jid in stale:
            del _wp_jobs[jid]
        j = _wp_jobs.get(job_id)
        if not j:
            return {"error": "job not found", "job_id": job_id}
        return {
            "job_id": job_id,
            "total": j["total"],
            "done": j["done"],
            "failed": j["failed"],
            "running": j["running"],
        }


@router.post("/devices/install-helper")
def install_helper_all():
    """批量安装壁纸 Helper APK 到所有在线设备（自动点击 MIUI 确认按钮）。"""
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    from src.utils.wallpaper_generator import _ensure_helper_installed
    from concurrent.futures import ThreadPoolExecutor, as_completed

    manager = get_device_manager(_config_path)
    online = [d for d in manager.get_all_devices()
              if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    results = {}

    def _install_one(dev):
        try:
            ok = _ensure_helper_installed(manager, dev.device_id)
            return dev.device_id, ok
        except Exception as e:
            logger.warning("[APK] 安装失败 %s: %s", dev.device_id[:8], e)
            return dev.device_id, False

    # 逐台安装（不能并行，因为自动点击确认会冲突——每台设备独立的 ADB 连接）
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = {pool.submit(_install_one, d): d for d in online}
        for fut in as_completed(futs):
            did, ok = fut.result()
            results[did[:8]] = "OK" if ok else "FAIL"

    ok_count = sum(1 for v in results.values() if v == "OK")
    return {
        "total": len(results),
        "installed": ok_count,
        "results": results,
    }


@router.post("/devices/{device_id}/open-wallpaper")
def open_wallpaper_on_device(device_id: str):
    """Open wallpaper image in MIUI Gallery on the device for manual setting."""
    from src.device_control.device_manager import get_device_manager
    from src.utils.wallpaper_generator import _try_gallery_wallpaper

    manager = get_device_manager(_config_path)
    ok = _try_gallery_wallpaper(manager, device_id)
    return {"ok": ok, "device_id": device_id}


# ── Helper: cleanup device references ──

def _cleanup_device_refs(device_id: str):
    """Clean up aliases, groups, and other references for a deleted device."""
    import json
    alias_path = Path(_config_path).parent / "device_aliases.json"
    try:
        if alias_path.exists():
            data = json.loads(alias_path.read_text("utf-8"))
            if device_id in data:
                del data[device_id]
                alias_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass
    try:
        from ..database import get_conn
        db = get_conn()
        db.execute("DELETE FROM device_group_members WHERE device_id = ?",
                    (device_id,))
        db.execute("DELETE FROM device_states WHERE device_id = ?",
                    (device_id,))
        db.commit()
    except Exception:
        pass
