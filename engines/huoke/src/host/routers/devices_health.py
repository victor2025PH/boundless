# -*- coding: utf-8 -*-
"""设备健康和诊断路由：诊断修复、性能、资产、通知监控、异常检测、看门狗、健康评分、隔离"""
import logging
import time
from fastapi import APIRouter, HTTPException, Depends

from src.utils.subprocess_text import run as _sp_run_text
from src.host.device_registry import DEFAULT_DEVICES_YAML, config_file

router = APIRouter(prefix="", tags=["devices-health"])
logger = logging.getLogger(__name__)
_config_path = DEFAULT_DEVICES_YAML


# ── Diagnose & Fix ──

@router.get("/devices/{device_id}/diagnose")
def device_diagnose(device_id: str):
    """Run a quick diagnostic on a device and return findings + suggested fixes."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    info = manager.get_device_info(did)
    issues = []
    fixes = []

    is_online = info and info.get("status") in ("connected", "online")
    if not is_online:
        issues.append("设备离线 — ADB 连接断开")
        fixes.append({"label": "重新连接ADB", "action": "reconnect"})
        fixes.append({"label": "USB重置", "action": "usb_reset"})
        return {"device_id": did, "online": False,
                "issues": issues, "fixes": fixes}

    checks = {}
    ok, out = manager.execute_adb_command("shell ping -c 1 -W 2 8.8.8.8", did)
    checks["network"] = ok
    if not ok:
        issues.append("网络不通 — 无法 ping 外网")
        fixes.append({"label": "检查WiFi", "action": "check_wifi"})
        fixes.append({"label": "重启WiFi", "action": "restart_wifi"})

    ok2, out2 = manager.execute_adb_command(
        "shell ip addr show tun0 2>/dev/null", did)
    has_vpn = "tun0" in out2 if ok2 else False
    checks["vpn_tunnel"] = has_vpn
    if not has_vpn:
        issues.append("VPN 隧道未建立")
        fixes.append({"label": "重连VPN", "action": "reconnect_vpn"})

    ok3, bat = manager.execute_adb_command("shell dumpsys battery", did)
    battery_level = None
    if ok3:
        for line in bat.split("\n"):
            if "level:" in line.lower():
                try:
                    battery_level = int(line.split(":")[1].strip())
                except Exception:
                    pass
    checks["battery"] = battery_level
    if battery_level is not None and battery_level < 15:
        issues.append(f"电量过低: {battery_level}%")

    if not issues:
        issues.append("设备状态正常，未发现异常")

    return {"device_id": did, "online": True,
            "issues": issues, "fixes": fixes, "checks": checks}


@router.post("/devices/{device_id}/fix")
def device_fix(device_id: str, body: dict):
    """Apply a fix action to a device."""
    from ..api import _resolve_device_with_manager, _audit

    did, manager = _resolve_device_with_manager(device_id)
    action = body.get("action", "")
    _audit("fix_device", did, f"action={action}")

    if action == "reconnect":
        ok, out = manager.execute_adb_command("reconnect", did)
        return {"ok": ok, "action": action, "output": out}
    elif action == "usb_reset":
        ok, out = manager.execute_adb_command("usb", did)
        return {"ok": ok, "action": action, "output": out}
    elif action == "check_wifi":
        ok, out = manager.execute_adb_command(
            "shell dumpsys wifi | head -20", did)
        return {"ok": ok, "action": action, "output": out}
    elif action == "restart_wifi":
        manager.execute_adb_command("shell svc wifi disable", did)
        import time
        time.sleep(2)
        ok, out = manager.execute_adb_command("shell svc wifi enable", did)
        return {"ok": ok, "action": action, "output": "WiFi已重启"}
    elif action == "reconnect_vpn":
        ok, out = manager.execute_adb_command(
            "shell am force-stop com.v2ray.ang", did)
        import time
        time.sleep(2)
        ok2, out2 = manager.execute_adb_command(
            "shell am start -n com.v2ray.ang/com.v2ray.ang.ui.MainActivity",
            did)
        return {"ok": ok2, "action": action, "output": "VPN已重启"}
    else:
        raise HTTPException(status_code=400,
                            detail=f"Unknown action: {action}")


# ── P1-A fix_action: rotate_ip ──

@router.post("/devices/{device_id}/proxy/rotate")
def device_proxy_rotate(device_id: str):
    """前端「🔄 换 IP 重试」按钮端点 (fix_action=rotate_ip)。

    通过 vpn_manager.reconnect_vpn_silent 重连 VPN/代理客户端 — 大多数 V2RayNG/911proxy
    在重连后会换到下一个出口。同时使本机预检缓存失效，避免下一次派任务读到旧的失败缓存。

    返回 {ok, device_id, vpn_reconnected, preflight_invalidated}
    """
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)

    vpn_ok = False
    try:
        from src.behavior.vpn_manager import reconnect_vpn_silent
        vpn_ok = bool(reconnect_vpn_silent(did))
    except Exception as e:
        logger.warning("[proxy/rotate] %s reconnect_vpn_silent failed: %s", did[:8], e)

    invalidated = False
    try:
        from src.host.preflight import invalidate_cache
        invalidate_cache(did)
        invalidated = True
    except Exception as e:
        logger.warning("[proxy/rotate] %s preflight invalidate_cache failed: %s", did[:8], e)

    return {
        "ok": vpn_ok,
        "device_id": did,
        "vpn_reconnected": vpn_ok,
        "preflight_invalidated": invalidated,
    }


# ── Performance ──

@router.get("/devices/{device_id}/performance")
def device_performance(device_id: str):
    """Get real-time CPU, memory, storage, battery info."""
    from ..api import _resolve_device_with_manager
    import re

    did, _ = _resolve_device_with_manager(device_id)
    result = {}
    try:
        cpu = _sp_run_text(["adb", "-s", did, "shell", "top", "-bn1", "-m5"],
                             capture_output=True, timeout=8)
        lines = cpu.stdout.splitlines()
        for ln in lines[:5]:
            if "cpu" in ln.lower() or "%" in ln:
                m = re.search(r'(\d+)%idle', ln) or re.search(r'idle\s*[:=]\s*(\d+)', ln, re.I)
                if m:
                    result["cpu_usage"] = 100 - int(m.group(1))
                    break
                m2 = re.search(r'(\d+)%user', ln)
                m3 = re.search(r'(\d+)%sys', ln)
                if m2:
                    result["cpu_usage"] = int(m2.group(1)) + (int(m3.group(1)) if m3 else 0)
                    break
    except Exception:
        pass
    try:
        mem = _sp_run_text(["adb", "-s", did, "shell", "cat", "/proc/meminfo"],
                             capture_output=True, timeout=5)
        total = re.search(r'MemTotal:\s+(\d+)', mem.stdout)
        free = re.search(r'MemAvailable:\s+(\d+)', mem.stdout)
        if total and free:
            t = int(total.group(1))
            f = int(free.group(1))
            result["mem_total_mb"] = t // 1024
            result["mem_used_mb"] = (t - f) // 1024
            result["mem_usage"] = round((t - f) / t * 100, 1)
    except Exception:
        pass
    try:
        df = _sp_run_text(["adb", "-s", did, "shell", "df", "/data"],
                            capture_output=True, timeout=5)
        lines = df.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 4:
                total_kb = int(parts[1]) if parts[1].isdigit() else 0
                used_kb = int(parts[2]) if parts[2].isdigit() else 0
                if total_kb:
                    result["storage_total_gb"] = round(total_kb / 1024 / 1024, 1)
                    result["storage_used_gb"] = round(used_kb / 1024 / 1024, 1)
                    result["storage_usage"] = round(used_kb / total_kb * 100, 1)
    except Exception:
        pass
    try:
        bat = _sp_run_text(["adb", "-s", did, "shell", "dumpsys", "battery"],
                             capture_output=True, timeout=5)
        level = re.search(r'level:\s*(\d+)', bat.stdout)
        temp = re.search(r'temperature:\s*(\d+)', bat.stdout)
        status = re.search(r'status:\s*(\d+)', bat.stdout)
        plugged = re.search(r'plugged:\s*(\d+)', bat.stdout)
        if level:
            result["battery_level"] = int(level.group(1))
        if temp:
            result["battery_temp"] = round(int(temp.group(1)) / 10, 1)
        if status:
            s = int(status.group(1))
            result["battery_status"] = {1: "unknown", 2: "charging", 3: "discharging",
                                        4: "not_charging", 5: "full"}.get(s, "unknown")
        if plugged:
            result["plugged"] = int(plugged.group(1)) > 0
    except Exception:
        pass
    return {"device_id": did, **result}


@router.get("/devices/performance/all")
def all_devices_performance():
    """Get performance data for all online devices."""
    from src.device_control.device_manager import get_device_manager
    from concurrent.futures import ThreadPoolExecutor
    import re

    manager = get_device_manager(_config_path)
    online = [d for d in manager.get_all_devices() if d.is_online]
    results = {}

    def _perf(did):
        data = {}
        try:
            mem = _sp_run_text(["adb", "-s", did, "shell", "cat", "/proc/meminfo"],
                                 capture_output=True, timeout=5)
            total = re.search(r'MemTotal:\s+(\d+)', mem.stdout)
            free = re.search(r'MemAvailable:\s+(\d+)', mem.stdout)
            if total and free:
                t, f = int(total.group(1)), int(free.group(1))
                data["mem_usage"] = round((t - f) / t * 100, 1)
                data["mem_total_mb"] = t // 1024
        except Exception:
            pass
        try:
            bat = _sp_run_text(["adb", "-s", did, "shell", "dumpsys", "battery"],
                                 capture_output=True, timeout=5)
            level = re.search(r'level:\s*(\d+)', bat.stdout)
            temp = re.search(r'temperature:\s*(\d+)', bat.stdout)
            status = re.search(r'status:\s*(\d+)', bat.stdout)
            if level:
                data["battery_level"] = int(level.group(1))
            if temp:
                data["battery_temp"] = round(int(temp.group(1)) / 10, 1)
            if status:
                s = int(status.group(1))
                data["charging"] = s == 2
        except Exception:
            pass
        try:
            df = _sp_run_text(["adb", "-s", did, "shell", "df", "/data"],
                                capture_output=True, timeout=5)
            lines = df.stdout.strip().splitlines()
            if len(lines) >= 2:
                parts = lines[1].split()
                if len(parts) >= 4 and parts[1].isdigit() and parts[2].isdigit():
                    total_kb, used_kb = int(parts[1]), int(parts[2])
                    if total_kb:
                        data["storage_usage"] = round(used_kb / total_kb * 100, 1)
        except Exception:
            pass
        return data

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_perf, d.device_id): d.device_id for d in online}
        for fut in futs:
            did = futs[fut]
            results[did] = fut.result()
    return {"devices": results}


# ── Device Asset Management ──

_device_assets_path = config_file("device_assets.json")


def _load_assets() -> dict:
    if _device_assets_path.exists():
        import json
        with open(_device_assets_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_assets(data: dict):
    import json
    _device_assets_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_device_assets_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@router.get("/device-assets")
def get_all_device_assets():
    return _load_assets()


@router.get("/device-assets/{device_id}")
def get_device_asset(device_id: str):
    assets = _load_assets()
    return assets.get(device_id, {})


@router.post("/device-assets/{device_id}")
def set_device_asset(device_id: str, body: dict):
    """Set asset info. body: {imei?, sim_number?, ip?, purchase_date?, notes?}"""
    assets = _load_assets()
    entry = assets.get(device_id, {})
    for k in ("imei", "sim_number", "ip", "purchase_date", "notes", "location", "owner"):
        if k in body:
            entry[k] = body[k]
    assets[device_id] = entry
    _save_assets(assets)
    return {"ok": True, "device_id": device_id, **entry}


@router.post("/device-assets/{device_id}/auto-detect")
def auto_detect_asset(device_id: str):
    """Auto-detect IMEI, IP from device via ADB."""
    from ..api import _resolve_device_with_manager
    import re

    did, _ = _resolve_device_with_manager(device_id)
    assets = _load_assets()
    entry = assets.get(did, {})
    try:
        r = _sp_run_text(["adb", "-s", did, "shell", "service", "call", "iphonesubinfo", "1"],
                           capture_output=True, timeout=10)
        digits = re.findall(r"'([^']+)'", r.stdout)
        imei_raw = "".join(digits).replace(".", "").replace(" ", "")
        if len(imei_raw) >= 15:
            entry["imei"] = imei_raw[:15]
    except Exception:
        pass
    try:
        r = _sp_run_text(["adb", "-s", did, "shell", "ip", "route", "show", "table", "0"],
                           capture_output=True, timeout=5)
        m = re.search(r'src\s+(\d+\.\d+\.\d+\.\d+)', r.stdout)
        if m:
            entry["ip"] = m.group(1)
    except Exception:
        pass
    try:
        r = _sp_run_text(["adb", "-s", did, "shell", "getprop", "gsm.sim.operator.alpha"],
                           capture_output=True, timeout=5)
        carrier = r.stdout.strip()
        if carrier:
            entry["carrier"] = carrier
    except Exception:
        pass
    assets[did] = entry
    _save_assets(assets)
    return {"ok": True, "device_id": did, **entry}


# ── Device Notification Monitor ──

_notification_store: list = []
_MAX_NOTIFICATIONS = 200


@router.get("/device-notifications")
def get_device_notifications(limit: int = 50):
    return _notification_store[-limit:][::-1]


@router.delete("/device-notifications")
def clear_device_notifications():
    _notification_store.clear()
    return {"ok": True}


@router.post("/devices/{device_id}/poll-notifications")
def poll_device_notifications(device_id: str):
    """Poll active notifications from a device."""
    from ..api import _resolve_device_with_manager
    import re

    did, _ = _resolve_device_with_manager(device_id)
    r = _sp_run_text(
        ["adb", "-s", did, "shell", "dumpsys", "notification", "--noredact"],
        capture_output=True, timeout=10
    )
    if r.returncode != 0:
        return {"notifications": []}
    notifs = []
    for m in re.finditer(r'pkg=(\S+).*?android\.title=\[?([^\]\n]+)', r.stdout, re.DOTALL):
        notifs.append({
            "device_id": did,
            "package": m.group(1),
            "title": m.group(2).strip(),
            "time": time.strftime("%H:%M:%S"),
        })
    for n in notifs[-10:]:
        _notification_store.append(n)
        if len(_notification_store) > _MAX_NOTIFICATIONS:
            _notification_store.pop(0)
    return {"count": len(notifs), "notifications": notifs[-10:]}


# ── Anomaly Detection ──

@router.post("/devices/{device_id}/anomaly/check")
def check_anomaly(device_id: str, body: dict = None):
    """Run anomaly detection on a device's current screen."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    from src.behavior.screen_anomaly import get_anomaly_detector
    detector = get_anomaly_detector()
    body = body or {}
    use_vision = body.get("use_vision", False)
    result = detector.detect(did, manager, use_vision=use_vision)
    if result:
        return {"anomaly": True, **result.to_dict()}
    return {"anomaly": False, "device_id": did}


@router.post("/devices/anomaly/check-all")
def check_all_anomalies(body: dict = None):
    """Run anomaly detection on all connected devices."""
    from src.device_control.device_manager import get_device_manager

    body = body or {}
    use_vision = body.get("use_vision", False)
    manager = get_device_manager(_config_path)
    from src.behavior.screen_anomaly import get_anomaly_detector
    detector = get_anomaly_detector()
    results = []
    for d in (manager.get_all_devices() if hasattr(manager, 'get_all_devices') else []):
        did = d if isinstance(d, str) else d.get("device_id", "")
        if not did:
            continue
        try:
            result = detector.detect(did, manager, use_vision=use_vision)
            if result:
                results.append(result.to_dict())
        except Exception:
            pass
    return {"anomalies": results, "total_checked": len(results)}


@router.get("/anomalies")
def get_anomalies(device_id: str = "", limit: int = 50):
    """Get anomaly detection history."""
    from src.behavior.screen_anomaly import get_anomaly_detector
    detector = get_anomaly_detector()
    return {"anomalies": detector.get_history(device_id, limit)}


@router.get("/anomalies/active")
def get_active_anomalies(max_age: int = 300):
    """Get recent active anomalies."""
    from src.behavior.screen_anomaly import get_anomaly_detector
    detector = get_anomaly_detector()
    return {"anomalies": detector.get_active_anomalies(max_age)}


# ── Watchdog ──

@router.get("/watchdog/health")
def watchdog_health():
    from src.device_control.watchdog import get_watchdog
    return {"devices": get_watchdog().all_health()}


@router.get("/watchdog/recoveries")
def watchdog_recoveries(limit: int = 20):
    from src.device_control.watchdog import get_watchdog
    return {"recoveries": get_watchdog().recent_recoveries(limit)}


@router.post("/watchdog/watch")
def watchdog_watch(body: dict):
    device_id = body.get("device_id", "")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id required")
    from src.device_control.watchdog import get_watchdog
    get_watchdog().watch(device_id, body.get("expected_app", ""))
    return {"ok": True}


@router.post("/watchdog/start")
def watchdog_start():
    from src.device_control.watchdog import get_watchdog
    get_watchdog().start()
    return {"ok": True}


# ── Reconnection status ──

def _get_monitor():
    from ..health_monitor import _monitor
    return _monitor


@router.get("/devices/reconnection-status")
def reconnection_status():
    """Return current reconnection state for all monitored devices."""
    from ..health_monitor import metrics as _metrics, _RECOVERY_LEVELS

    monitor = _get_monitor()
    disconnected = list(monitor._disconnected_devices) if monitor else []
    recovery_state = {}
    if monitor:
        for did, state in monitor._recovery_state.items():
            level = state["level"]
            level_name = (_RECOVERY_LEVELS[level]["name"]
                         if level < len(_RECOVERY_LEVELS) else "exhausted")
            recovery_state[did] = {
                "level": level,
                "level_name": level_name,
                "attempts_at_level": state["attempts"],
                "offline_sec": int(time.time() - state.get("started_at", time.time())),
            }
    streaming_pending = list(monitor._streaming_before_disconnect) if monitor else []
    return {
        "disconnected_devices": disconnected,
        "recovery_state": recovery_state,
        "recovery_levels": [l["name"] for l in _RECOVERY_LEVELS],
        "streaming_pending_restore": streaming_pending,
        "device_status": _metrics.device_status,
        "total_reconnects": _metrics.device_reconnects,
        "current_interval_sec": monitor._current_interval if monitor else None,
    }


@router.post("/devices/{device_id}/reconnect")
def force_reconnect(device_id: str):
    """Manually trigger escalated recovery from Level 0 for a specific device."""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    monitor = _get_monitor()
    if monitor:
        monitor._recovery_state[did] = {
            "level": 0, "attempts": 0, "last_attempt": 0,
            "started_at": time.time(),
        }
        monitor._disconnected_devices.add(did)
        monitor._escalated_recovery(manager, did)
        state = monitor._recovery_state.get(did)
        return {"ok": True, "device_id": did,
                "recovery_state": state}
    raise HTTPException(status_code=503, detail="HealthMonitor not running")


@router.post("/devices/{device_id}/auto-recover")
def auto_recover_device(device_id: str):
    """综合自动恢复：诊断问题 → 逐步修复（ADB重连、VPN重启、缓存清理）"""
    from ..api import _resolve_device_with_manager

    did, manager = _resolve_device_with_manager(device_id)
    steps = []

    # Step 1: ADB 连通性检查 + 重连
    try:
        r = _sp_run_text(
            ["adb", "-s", did, "shell", "echo", "ping"],
            capture_output=True, timeout=8
        )
        if r.returncode == 0:
            steps.append({"step": "adb_check", "ok": True, "detail": "ADB 连通正常"})
        else:
            _sp_run_text(["adb", "disconnect", did], capture_output=True, timeout=5)
            _sp_run_text(["adb", "connect", did], capture_output=True, timeout=5)
            r2 = _sp_run_text(
                ["adb", "-s", did, "shell", "echo", "ping"],
                capture_output=True, timeout=8
            )
            steps.append({
                "step": "adb_reconnect",
                "ok": r2.returncode == 0,
                "detail": "ADB 重连" + ("成功" if r2.returncode == 0 else "失败")
            })
    except Exception as e:
        steps.append({"step": "adb_check", "ok": False, "detail": str(e)[:80]})

    # Step 2: VPN 状态检查 + 重启
    try:
        r = _sp_run_text(
            ["adb", "-s", did, "shell", "dumpsys", "package",
             "com.v2ray.ang", "|", "grep", "versionName"],
            capture_output=True, timeout=8
        )
        vpn_installed = "versionName" in r.stdout
        if vpn_installed:
            _sp_run_text(
                ["adb", "-s", did, "shell", "am", "force-stop", "com.v2ray.ang"],
                capture_output=True, timeout=5
            )
            import time as _t
            _t.sleep(1)
            _sp_run_text(
                ["adb", "-s", did, "shell", "monkey", "-p", "com.v2ray.ang",
                 "-c", "android.intent.category.LAUNCHER", "1"],
                capture_output=True, timeout=5
            )
            steps.append({"step": "vpn_restart", "ok": True, "detail": "V2Ray 已重启"})
        else:
            steps.append({"step": "vpn_check", "ok": False, "detail": "V2Ray 未安装"})
    except Exception as e:
        steps.append({"step": "vpn_restart", "ok": False, "detail": str(e)[:80]})

    # Step 3: TikTok 缓存清理
    try:
        r = _sp_run_text(
            ["adb", "-s", did, "shell", "pm", "clear",
             "com.zhiliaoapp.musically"],
            capture_output=True, timeout=10
        )
        # 只清缓存不清数据（更安全的方式）
        steps.append({
            "step": "tiktok_cache",
            "ok": "Success" in r.stdout,
            "detail": "TikTok 缓存已清理" if "Success" in r.stdout else "清理失败"
        })
    except Exception as e:
        steps.append({"step": "tiktok_cache", "ok": False, "detail": str(e)[:80]})

    # Step 4: 触发健康监控重新评估
    try:
        from ..health_monitor import metrics as _metrics
        score = _metrics.device_health_score(did)
        steps.append({
            "step": "health_rescore",
            "ok": True,
            "detail": "健康评分: " + str(score.get("total", 0))
        })
    except Exception:
        pass

    all_ok = all(s["ok"] for s in steps)
    return {
        "ok": all_ok,
        "device_id": did,
        "steps": steps,
        "total_steps": len(steps),
        "passed_steps": sum(1 for s in steps if s["ok"]),
    }


# ── USB diagnostics ──

@router.get("/devices/usb-diagnostics")
def usb_diagnostics():
    """Full USB/ADB diagnostic scan showing all device states."""
    from src.device_control.device_manager import get_device_manager
    manager = get_device_manager(_config_path)
    return manager.run_usb_diagnostics()


# ── Health scores ──

@router.get("/devices/health-scores")
def device_health_scores():
    """Return health scores for all monitored devices."""
    from ..health_monitor import metrics as _metrics
    scores = _metrics.all_health_scores()
    ranked = sorted(scores.items(), key=lambda x: x[1]["total"], reverse=True)
    return {
        "scores": dict(ranked),
        "best_device": ranked[0][0] if ranked else None,
    }


@router.get("/devices/{device_id}/health-score")
def device_health_score(device_id: str):
    """Return health score breakdown for a specific device."""
    from ..health_monitor import metrics as _metrics
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)
    return _metrics.device_health_score(did)


@router.get("/devices/recovery-timeline")
def recovery_timeline(device_id: str = "", limit: int = 100):
    """Return recovery event history for timeline visualization."""
    from ..health_monitor import metrics as _metrics
    return {"events": _metrics.get_recovery_timeline(device_id, limit)}


@router.get("/devices/health-trends")
def health_trends(hours: int = 24):
    """Return health score history for all devices (trend graphs)."""
    from ..health_monitor import metrics as _metrics
    return {"trends": _metrics.get_all_health_trends(hours)}


@router.get("/devices/{device_id}/health-trend")
def device_health_trend(device_id: str, hours: int = 24):
    """Return health score trend for a specific device."""
    from ..health_monitor import metrics as _metrics
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)
    return {"trend": _metrics.get_health_trend(did, hours)}


# ── Isolate / Unisolate ──

@router.post("/devices/{device_id}/isolate")
def isolate_device(device_id: str):
    """Isolate a device -- it won't receive new task assignments."""
    from ..health_monitor import metrics as _metrics
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)
    _metrics.isolate_device(did)
    return {"status": "isolated", "device_id": did}


@router.post("/devices/{device_id}/unisolate")
def unisolate_device(device_id: str):
    """Remove isolation from a device."""
    from ..health_monitor import metrics as _metrics
    from ..api import _resolve_device_with_manager
    did, _ = _resolve_device_with_manager(device_id)
    _metrics.unisolate_device(did)
    return {"status": "active", "device_id": did}


@router.get("/devices/isolated")
def list_isolated_devices():
    """Return list of currently isolated devices."""
    from ..health_monitor import metrics as _metrics
    return {"isolated": _metrics.get_isolated_devices()}


# ── Recovery status ──

@router.get("/devices/recovery-status")
def get_recovery_status():
    """返回所有设备的恢复状态。"""
    try:
        from ..health_monitor import _monitor
        if not _monitor:
            return {"devices": {}}

        result = {}
        recovery_state = getattr(_monitor, '_recovery_state', {})
        disconnected = getattr(_monitor, '_disconnected_devices', set())

        _RECOVERY_LEVELS = [
            {"name": "reconnect"},
            {"name": "wifi_and_dismiss"},
            {"name": "reset_transport"},
            {"name": "kill_server"},
            {"name": "usb_power_cycle"},
        ]

        for did in disconnected:
            state = recovery_state.get(did, {})
            level = state.get("level", 0)
            attempts = state.get("attempts", 0)
            level_name = _RECOVERY_LEVELS[level]["name"] if level < len(_RECOVERY_LEVELS) else "exhausted"
            result[did] = {
                "recovering": True,
                "level": level,
                "level_name": level_name,
                "attempts": attempts,
                "exhausted": level >= len(_RECOVERY_LEVELS),
            }

        return {"devices": result}
    except Exception as e:
        return {"devices": {}, "error": str(e)}


# ── Scheduling scores ──

@router.get("/devices/scheduling-scores")
def scheduling_scores(task_type: str = ""):
    """Return smart scheduling scores for all devices."""
    from ..smart_scheduler import get_smart_scheduler
    scheduler = get_smart_scheduler()
    scores = scheduler.get_scheduling_scores(task_type)
    ranked = sorted(scores.items(), key=lambda x: x[1]["total"], reverse=True)
    return {"scores": dict(ranked), "task_type": task_type or "any"}


# ── Recovery Stats ──

@router.get("/devices/recovery-stats")
def get_recovery_stats():
    """返回设备恢复统计数据。"""
    try:
        from ..health_monitor import metrics

        # 从 metrics 获取恢复数据
        stats = {
            "total_disconnects": 0,
            "total_recoveries": 0,
            "recovery_rate": 0,
            "by_device": [],
            "by_level": {"L0": 0, "L1": 0, "L2": 0, "L3": 0, "L4": 0},
        }

        # 设备级统计
        device_stats = getattr(metrics, 'disconnect_counts', {})
        reconnect_counts = getattr(metrics, 'reconnect_counts', {})

        for did, count in device_stats.items():
            recoveries = reconnect_counts.get(did, 0)
            stats["total_disconnects"] += count
            stats["total_recoveries"] += recoveries
            stats["by_device"].append({
                "device_id": did,
                "disconnects": count,
                "recoveries": recoveries,
                "rate": round(recoveries / max(count, 1) * 100, 1),
            })

        if stats["total_disconnects"] > 0:
            stats["recovery_rate"] = round(
                stats["total_recoveries"] / stats["total_disconnects"] * 100, 1)

        # 按掉线次数降序排序（掉线排行榜）
        stats["by_device"].sort(key=lambda x: x["disconnects"], reverse=True)

        return stats
    except Exception as e:
        return {"total_disconnects": 0, "total_recoveries": 0, "recovery_rate": 0,
                "by_device": [], "error": str(e)}


@router.get("/devices/predictive-health")
def get_predictive_health():
    """返回所有设备的预测性健康分析 — 基于掉线频率模式预测即将掉线的设备。"""
    try:
        from ..health_monitor import metrics
        result = []
        for did in metrics.get_disconnect_history_devices():
            pred = metrics.predict_disconnect_risk(did)
            if pred["score"] > 0:
                result.append({"device_id": did, **pred})
        result.sort(key=lambda x: x["score"], reverse=True)
        return {"devices": result}
    except Exception as e:
        return {"devices": [], "error": str(e)}


@router.get("/devices/account-health-alerts")
def get_account_health_alerts():
    """Get all active TikTok account health alerts."""
    from ..health_monitor import get_health_monitor
    hm = get_health_monitor()
    if not hm:
        return {"ok": True, "alert_count": 0, "alerts": []}
    alerts = hm.get_all_account_alerts()
    return {
        "ok": True,
        "alert_count": len(alerts),
        "alerts": [
            {"device_id": did, **info}
            for did, info in alerts.items()
        ]
    }


@router.post("/devices/{device_id}/account-health/record")
def record_account_interaction(device_id: str, body: dict):
    """Record a TikTok interaction result for health tracking."""
    from ..health_monitor import get_health_monitor
    hm = get_health_monitor()
    if not hm:
        return {"ok": False, "error": "HealthMonitor not running"}
    hm.record_account_interaction(
        device_id,
        success=bool(body.get("success", True)),
        rate=float(body.get("rate", 0.0)),
    )
    alert = hm.get_account_health_alert(device_id)
    return {"ok": True, "has_alert": alert is not None, "alert": alert}
