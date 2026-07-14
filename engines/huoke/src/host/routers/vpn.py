# -*- coding: utf-8 -*-
"""VPN 管理与 Geo-IP 检测路由。"""
import logging
import subprocess
from fastapi import APIRouter, HTTPException, Depends, UploadFile, File
from .auth import verify_api_key
from src.device_control.device_manager import get_device_manager
from src.host.device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT
from src.utils.subprocess_text import run as _sp_run_text

router = APIRouter(tags=["vpn"])
logger = logging.getLogger(__name__)
_project_root = PROJECT_ROOT
_config_path = DEFAULT_DEVICES_YAML


# ── Geo-IP Check API ──

@router.get("/geo/check/{device_id}", dependencies=[Depends(verify_api_key)])
def geo_check_device(device_id: str, country: str = "italy"):
    """Check if device IP matches expected country."""
    from src.behavior.geo_check import check_device_geo
    result = check_device_geo(device_id, country)
    return {
        "device_id": result.device_id,
        "public_ip": result.public_ip,
        "detected_country": result.detected_country,
        "detected_country_code": result.detected_country_code,
        "expected_country": result.expected_country,
        "matches": result.matches,
        "vpn_detected": result.vpn_detected,
        "error": result.error,
    }


@router.get("/geo/check-all", dependencies=[Depends(verify_api_key)])
def geo_check_all(country: str = "italy"):
    """Check geo-IP for all connected devices."""
    from src.behavior.geo_check import check_all_devices
    return {"results": check_all_devices(country)}


# ── VPN Management API ──

@router.post("/vpn/setup", dependencies=[Depends(verify_api_key)])
def vpn_setup(body: dict):
    """Setup VPN on device(s). Body: {uri_or_qr, device_id?, all?}"""
    from src.behavior.vpn_manager import get_vpn_manager
    mgr = get_vpn_manager()
    uri_or_qr = body.get("uri_or_qr", body.get("uri", body.get("qr", "")))
    if not uri_or_qr:
        raise HTTPException(status_code=400, detail="uri_or_qr is required")
    device_id = body.get("device_id")
    do_all = body.get("all", False)

    if do_all:
        results = mgr.setup_all(uri_or_qr)
        return {"results": [
            {"device_id": s.device_id, "connected": s.connected,
             "config_name": s.config_name, "error": s.error}
            for s in results
        ]}
    elif device_id:
        s = mgr.setup(device_id, uri_or_qr)
        return {"device_id": s.device_id, "connected": s.connected,
                "config_name": s.config_name, "per_app": s.per_app_enabled,
                "error": s.error}
    else:
        results = mgr.setup_all(uri_or_qr)
        return {"results": [
            {"device_id": s.device_id, "connected": s.connected,
             "config_name": s.config_name, "error": s.error}
            for s in results
        ]}


@router.get("/vpn/status", dependencies=[Depends(verify_api_key)])
def vpn_status_all():
    """Get VPN status for all connected devices (including cluster workers)."""
    from src.behavior.vpn_manager import get_vpn_manager
    from src.behavior.vpn_health import get_vpn_health_monitor
    mgr = get_vpn_manager()
    manager = get_device_manager(_config_path)
    devices = manager.get_all_devices()
    # 获取健康监控缓存的 IP 和国家信息
    health_mon = get_vpn_health_monitor()
    health_data = health_mon.get_status() if health_mon else {}
    results = []
    for d in devices:
        did = d.get("device_id", "") if isinstance(d, dict) else getattr(d, "device_id", "")
        if did:
            s = mgr.status(did)
            entry = {
                "device_id": s.device_id,
                "connected": s.connected,
                "has_tun": s.has_tun,
                "has_notification": s.has_notification,
                "config_name": s.config_name,
            }
            # 附加健康监控缓存的 IP 和国家
            h = health_data.get(did, {})
            if h.get("verified_ip"):
                entry["ip"] = h["verified_ip"]
            country = health_mon._expected_countries.get(did, "") if health_mon else ""
            if country:
                entry["country"] = country
            results.append(entry)

    # Coordinator 模式：聚合 Worker 的 VPN 状态
    if not results:
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            cfg_cluster = load_cluster_config()
            if cfg_cluster.get("role") == "coordinator":
                coord = get_cluster_coordinator()
                overview = coord.get_overview()
                import urllib.request, json as _json
                for host in overview.get("hosts", []):
                    if not host.get("online"):
                        continue
                    host_ip = host.get("host_ip", "")
                    port = host.get("port", 8000)
                    # 尝试所有可用IP
                    for ip in _get_worker_ips(coord, host.get("host_id", ""), host_ip):
                        try:
                            url = f"http://{ip}:{port}/vpn/status"
                            req = urllib.request.Request(url, method="GET")
                            resp = urllib.request.urlopen(req, timeout=5)
                            worker_data = _json.loads(resp.read().decode())
                            results.extend(worker_data.get("devices", []))
                            # 用Worker的config作为fallback
                            if not mgr.current_config and worker_data.get("current_config"):
                                wc = worker_data["current_config"]
                                return {"devices": results, "current_config": wc}
                            break
                        except Exception:
                            continue
        except Exception:
            pass

    cfg = mgr.current_config
    return {
        "devices": results,
        "current_config": {
            "remark": cfg.remark if cfg else "",
            "server": cfg.server if cfg else "",
            "protocol": cfg.protocol if cfg else "",
        } if cfg else None,
    }


def _get_worker_ips(coord, host_id: str, default_ip: str) -> list:
    """获取 Worker 的所有可用 IP（包括 ZeroTier）。"""
    ips = []
    try:
        h = coord._hosts.get(host_id)
        if h and h.ips:
            ips = list(h.ips)
    except Exception:
        pass
    if default_ip and default_ip not in ips:
        ips.insert(0, default_ip)
    return ips or [default_ip]


@router.get("/vpn/status/{device_id}", dependencies=[Depends(verify_api_key)])
def vpn_status_device(device_id: str):
    """Get VPN status for a specific device."""
    from src.behavior.vpn_manager import get_vpn_manager
    s = get_vpn_manager().status(device_id)
    return {
        "device_id": s.device_id,
        "connected": s.connected,
        "has_tun": s.has_tun,
        "has_notification": s.has_notification,
        "per_app_enabled": s.per_app_enabled,
        "config_name": s.config_name,
        "error": s.error,
    }


@router.post("/vpn/stop/{device_id}", dependencies=[Depends(verify_api_key)])
def vpn_stop(device_id: str):
    """Stop VPN on a device."""
    from src.behavior.vpn_manager import get_vpn_manager
    s = get_vpn_manager().stop(device_id)
    return {"device_id": s.device_id, "connected": s.connected}


@router.get("/vpn/health", dependencies=[Depends(verify_api_key)])
def vpn_health_status():
    """Return VPN health status for all devices."""
    from src.behavior.vpn_health import get_vpn_health_monitor
    return {"devices": get_vpn_health_monitor().get_status()}


@router.post("/vpn/health/{device_id}/check", dependencies=[Depends(verify_api_key)])
def vpn_health_check(device_id: str):
    """Force a VPN health check for a specific device (with reconnect)."""
    from src.behavior.vpn_health import get_vpn_health_monitor
    mon = get_vpn_health_monitor()
    result = mon.check_device(device_id, allow_reconnect=True)
    return result


@router.get("/vpn/auto-reconnect", dependencies=[Depends(verify_api_key)])
def vpn_auto_reconnect_status():
    """Get VPN auto-reconnect toggle status."""
    from src.behavior.vpn_health import get_vpn_health_monitor
    mon = get_vpn_health_monitor()
    return {"enabled": mon.auto_reconnect_enabled}


@router.post("/vpn/auto-reconnect", dependencies=[Depends(verify_api_key)])
def vpn_auto_reconnect_toggle(body: dict):
    """Toggle VPN auto-reconnect. Body: {enabled: bool}."""
    from src.behavior.vpn_health import get_vpn_health_monitor
    mon = get_vpn_health_monitor()
    mon.auto_reconnect_enabled = bool(body.get("enabled", False))
    return {"enabled": mon.auto_reconnect_enabled}


@router.post("/vpn/health/{device_id}/country", dependencies=[Depends(verify_api_key)])
def vpn_set_expected_country(device_id: str, body: dict):
    """Set expected VPN country for IP leak detection."""
    from src.behavior.vpn_health import get_vpn_health_monitor
    country = body.get("country", "")
    if not country:
        raise HTTPException(status_code=400, detail="country required")
    get_vpn_health_monitor().set_expected_country(device_id, country)
    return {"status": "ok", "device_id": device_id, "country": country}


# ═══════════════════════════════════════════════════════════════
#  VPN 二维码一键配置
# ═══════════════════════════════════════════════════════════════

@router.post("/vpn/upload-qr", dependencies=[Depends(verify_api_key)])
async def vpn_upload_qr(file: UploadFile = File(...)):
    """上传 VPN 二维码图片 → 解码 → 返回 URI。

    支持格式: PNG, JPG, BMP, WEBP
    支持协议: VLESS, VMess, Trojan, Shadowsocks
    """
    from src.host.vpn_qr_service import decode_qr_image, validate_vpn_uri, save_qr_image

    # 验证文件类型
    if not file.filename:
        raise HTTPException(400, "未选择文件")
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ("png", "jpg", "jpeg", "bmp", "webp"):
        raise HTTPException(400, f"不支持的格式: .{ext}，请上传 PNG/JPG 图片")

    # 读取并保存
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "文件过大 (最大 10MB)")

    path = save_qr_image(file.filename, content)

    # 解码 QR
    uri = decode_qr_image(path)
    if not uri:
        raise HTTPException(400, "无法识别二维码，请确保图片清晰完整")

    # 验证 URI
    valid, msg = validate_vpn_uri(uri)
    if not valid:
        raise HTTPException(400, f"无效的 VPN 配置: {msg}")

    # 解析协议信息
    protocol = uri.split("://")[0]
    return {
        "ok": True,
        "uri": uri,
        "protocol": protocol,
        "filename": file.filename,
        "saved_path": path,
    }


@router.post("/vpn/setup-from-qr", dependencies=[Depends(verify_api_key)])
async def vpn_setup_from_qr(file: UploadFile = File(...)):
    """上传 VPN 二维码 → 一键配置到所有设备 → 验证连接。

    完整流程: 解码 → 推送到所有在线设备 → 全局模式 → Geo-IP验证
    """
    from src.host.vpn_qr_service import (
        decode_qr_image_with_detail, validate_vpn_uri, save_qr_image, setup_all_devices
    )

    # 保存 + 解码
    content = await file.read()
    if not content:
        raise HTTPException(400, "空文件")

    uri = None
    error_detail = ""
    filename = file.filename or "upload.png"

    # 尝试1: 如果是文本文件，直接读取 URI
    try:
        text = content.decode("utf-8").strip()
        if text.startswith(("vless://", "vmess://", "trojan://", "ss://")):
            uri = text.split("\n")[0].strip()
    except (UnicodeDecodeError, Exception):
        pass

    # 尝试2: QR 图片解码（带详细错误）
    if not uri:
        path = save_qr_image(filename, content)
        uri, error_detail = decode_qr_image_with_detail(path)

    if not uri:
        raise HTTPException(400, error_detail or "无法识别，请上传清晰的QR码图片或直接粘贴URI")
    valid, msg = validate_vpn_uri(uri)
    if not valid:
        raise HTTPException(400, f"无效配置: {msg}")

    # 推送到所有设备
    result = setup_all_devices(uri)
    result["uri"] = uri[:50] + "..."
    result["protocol"] = uri.split("://")[0]
    return result


@router.post("/vpn/apply-uri", dependencies=[Depends(verify_api_key)])
def vpn_apply_uri(body: dict):
    """直接用 URI 配置所有设备（不需要二维码）。

    Body: {"uri": "vless://...", "device_ids": ["xxx"] (可选)}
    """
    from src.host.vpn_qr_service import validate_vpn_uri, setup_all_devices

    uri = body.get("uri", "").strip()
    valid, msg = validate_vpn_uri(uri)
    if not valid:
        raise HTTPException(400, f"无效配置: {msg}")

    device_ids = body.get("device_ids")
    result = setup_all_devices(uri, device_ids)
    return result


@router.post("/vpn/batch-setup", dependencies=[Depends(verify_api_key)])
def vpn_batch_setup(body: dict):
    """统一批量 VPN 配置（导入+启动+国家验证）。

    Body:
    {
        "uri": "vless://...",           # VPN 配置 URI
        "country": "italy",            # 预期国家（用于 IP 泄漏检测，可选）
        "auto_start": true,            # 导入后自动启动 VPN（默认 true）
        "device_ids": ["xxx"]          # 指定设备（可选，默认全部在线设备）
    }

    流程:
    1. 批量导入配置（am start SEND intent，秒级）
    2. 批量启动 VPN（widget broadcast，秒级）
    3. 设置预期国家（IP 泄漏检测）
    """
    import traceback
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    from src.behavior.vpn_manager import (
        parse_uri, _import_via_intent, _start_vpn_adb,
        _stop_vpn_adb, check_vpn_status, _save_config_file, V2RAYNG_PKG,
    )
    from src.behavior.vpn_health import get_vpn_health_monitor

    uri = body.get("uri", "").strip()
    if not uri:
        raise HTTPException(400, "缺少 uri 参数")

    country = body.get("country", "").strip()
    auto_start = body.get("auto_start", True)
    device_ids = body.get("device_ids")

    # 解析 URI
    config = parse_uri(uri)
    if not config.server:
        raise HTTPException(400, f"无法解析 URI: {uri[:50]}...")

    # 获取目标设备
    manager = get_device_manager(_config_path)

    local_devices = {d.device_id for d in manager.get_all_devices()}

    if device_ids:
        targets = device_ids
    else:
        targets = [d.device_id for d in manager.get_all_devices()
                   if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    # Coordinator：如果目标设备不在本机，转发到 Worker
    if not targets or (targets and not any(t in local_devices for t in targets)):
        forwarded = _forward_to_workers("vpn/batch-setup", body)
        if forwarded is not None:
            return forwarded
        if not targets:
            return {"ok": False, "error": "没有在线设备"}

    results = {}

    def _setup_one(did):
        short = did[:8]
        try:
            import time
            _stop_vpn_adb(did)
            time.sleep(0.5)

            if not _import_via_intent(did, uri):
                return did, {"ok": False, "error": "导入失败"}
            time.sleep(1.5)

            if auto_start:
                _start_vpn_adb(did)
                time.sleep(2)

            if country:
                try:
                    monitor = get_vpn_health_monitor()
                    if monitor:
                        monitor.set_expected_country(did, country)
                except Exception:
                    pass

            status = check_vpn_status(did)
            return did, {
                "ok": True,
                "connected": status.connected,
                "config": config.remark,
            }
        except Exception as e:
            return did, {"ok": False, "error": str(e)[:100]}

    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_setup_one, did): did for did in targets}
        for fut in as_completed(futs):
            did, res = fut.result()
            results[did[:8]] = res

    # 保存配置
    _save_config_file(config)

    ok_count = sum(1 for v in results.values() if v.get("ok"))
    connected = sum(1 for v in results.values() if v.get("connected"))

    return {
        "ok": ok_count > 0,
        "total": len(results),
        "imported": ok_count,
        "connected": connected,
        "country": country or "未设置",
        "config_name": config.remark,
        "results": results,
    }


@router.post("/vpn/toggle", dependencies=[Depends(verify_api_key)])
def vpn_toggle(body: dict = None):
    """批量切换 VPN 开关（启动/停止）。

    Body: {"device_ids": ["xxx"] (可选，默认全部)}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    from src.behavior.vpn_manager import _toggle_vpn

    body = body or {}
    device_ids = body.get("device_ids")

    manager = get_device_manager(_config_path)

    if device_ids:
        targets = device_ids
    else:
        targets = [d.device_id for d in manager.get_all_devices()
                   if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    results = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_toggle_vpn, did): did for did in targets}
        for fut in as_completed(futs):
            did = futs[fut]
            ok = fut.result()
            results[did[:8]] = "OK" if ok else "FAIL"

    return {"total": len(results), "results": results}


@router.post("/vpn/batch-stop", dependencies=[Depends(verify_api_key)])
def vpn_batch_stop(body: dict = None):
    """批量停止全部设备 VPN。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    from src.behavior.vpn_manager import _stop_vpn_adb

    body = body or {}
    device_ids = body.get("device_ids")

    manager = get_device_manager(_config_path)

    if device_ids:
        targets = device_ids
    else:
        targets = [d.device_id for d in manager.get_all_devices()
                   if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    stopped = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(_stop_vpn_adb, did): did for did in targets}
        for fut in as_completed(futs):
            try:
                fut.result()
                stopped += 1
            except Exception:
                pass

    return {"total": len(targets), "stopped": stopped}


@router.post("/vpn/batch-setup-stream", dependencies=[Depends(verify_api_key)])
def vpn_batch_setup_stream(body: dict):
    """SSE 流式批量 VPN 配置 — 实时推送每台设备的进度。

    与 /vpn/batch-setup 相同的逻辑，但返回 text/event-stream。
    每完成一台设备就推送一条 event。
    """
    import json as _json
    import time as _time
    import traceback
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from starlette.responses import StreamingResponse
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    from src.behavior.vpn_manager import (
        parse_uri, _import_via_intent, _start_vpn_adb,
        _stop_vpn_adb, check_vpn_status, _save_config_file, V2RAYNG_PKG,
    )
    from src.behavior.vpn_health import get_vpn_health_monitor

    uri = body.get("uri", "").strip()
    if not uri:
        raise HTTPException(400, "缺少 uri 参数")

    country = body.get("country", "").strip()
    auto_start = body.get("auto_start", True)
    device_ids = body.get("device_ids")

    try:
        config = parse_uri(uri)
    except (ValueError, Exception) as e:
        raise HTTPException(400, f"无法解析 URI: {str(e)[:80]}")
    if not config.server:
        raise HTTPException(400, f"无法解析 URI: {uri[:50]}...")

    manager = get_device_manager(_config_path)
    local_devices = {d.device_id for d in manager.get_all_devices()}

    if device_ids:
        targets = device_ids
    else:
        targets = [d.device_id for d in manager.get_all_devices()
                   if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    if not targets or (targets and not any(t in local_devices for t in targets)):
        # Coordinator: 转发到 Worker（非流式 fallback）
        forwarded = _forward_to_workers("vpn/batch-setup", body)
        if forwarded is not None:
            # 将非流式结果包装为单次 SSE 返回
            import json as _json_fb
            def _fallback_gen():
                results = forwarded.get("results", {})
                total = forwarded.get("total", 0)
                yield f"data:{_json_fb.dumps({'type':'start','total':total})}\n\n"
                for short, res in results.items():
                    ev = {"type": "device_done", "short": short,
                          "ok": res.get("ok", False),
                          "connected": res.get("connected", False),
                          "error": res.get("error", ""),
                          "time": 0}
                    yield f"data:{_json_fb.dumps(ev)}\n\n"
                ok_count = forwarded.get("imported", 0)
                connected = forwarded.get("connected", 0)
                yield f"data:{_json_fb.dumps({'type':'done','total':total,'imported':ok_count,'connected':connected,'config_name':forwarded.get('config_name','')})}\n\n"
            return StreamingResponse(_fallback_gen(), media_type="text/event-stream")
        raise HTTPException(400, "没有在线设备")

    def _setup_one_timed(did):
        """配置单台设备并计时。"""
        t0 = _time.time()
        short = did[:8]
        try:
            _stop_vpn_adb(did)
            _time.sleep(0.5)

            if not _import_via_intent(did, uri):
                return did, {"ok": False, "short": short, "error": "导入失败",
                             "connected": False, "time": _time.time() - t0}
            _time.sleep(1.5)

            if auto_start:
                _start_vpn_adb(did)
                _time.sleep(2)

            if country:
                try:
                    get_vpn_health_monitor().set_expected_country(did, country)
                except Exception:
                    pass

            status = check_vpn_status(did)
            return did, {"ok": True, "short": short, "connected": status.connected,
                         "time": _time.time() - t0}
        except Exception as e:
            return did, {"ok": False, "short": short, "error": str(e)[:80],
                         "connected": False, "time": _time.time() - t0}

    def event_generator():
        # 发送开始事件
        yield f"data:{_json.dumps({'type':'start','total':len(targets)})}\n\n"

        ok_count = 0
        connected_count = 0

        with ThreadPoolExecutor(max_workers=10) as pool:
            futs = {pool.submit(_setup_one_timed, did): did for did in targets}
            for fut in as_completed(futs):
                did, res = fut.result()
                if res.get("ok"):
                    ok_count += 1
                if res.get("connected"):
                    connected_count += 1
                ev = {"type": "device_done", **res}
                yield f"data:{_json.dumps(ev)}\n\n"

        # 保存配置
        _save_config_file(config)

        done_ev = {
            "type": "done",
            "total": len(targets),
            "imported": ok_count,
            "connected": connected_count,
            "config_name": config.remark,
        }
        yield f"data:{_json.dumps(done_ev)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ═══════════════════════════════════════════════════════════════
#  VPN 配置池（多配置管理 + 设备分配）
# ═══════════════════════════════════════════════════════════════

_POOL_FILE = _project_root / "config" / "vpn_pool.json"


def _load_pool() -> dict:
    """加载配置池。结构: {configs: [...], assignments: {device_id: config_id}}"""
    import json as _j
    if _POOL_FILE.exists():
        try:
            return _j.loads(_POOL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"configs": [], "assignments": {}}


def _save_pool(pool: dict):
    import json as _j
    _POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    _POOL_FILE.write_text(_j.dumps(pool, indent=2, ensure_ascii=False), encoding="utf-8")


def _forward_to_workers(endpoint: str, body: dict, method: str = "POST", timeout: int = 120):
    """Coordinator 通用转发：将请求转发到第一个在线 Worker 并返回结果。"""
    try:
        from src.host.multi_host import get_cluster_coordinator, load_cluster_config
        import urllib.request, json as _jfw
        cfg = load_cluster_config()
        if cfg.get("role") != "coordinator":
            return None
        coord = get_cluster_coordinator()
        if not coord:
            return None
        overview = coord.get_overview()
        for host in overview.get("hosts", []):
            if not host.get("online"):
                continue
            ip = host.get("host_ip", "")
            port = host.get("port", 8000)
            for worker_ip in _get_worker_ips(coord, host.get("host_id", ""), ip):
                try:
                    url = f"http://{worker_ip}:{port}/{endpoint}"
                    data = _jfw.dumps(body).encode() if body else None
                    req = urllib.request.Request(
                        url, data=data, method=method,
                        headers={"Content-Type": "application/json"} if data else {})
                    resp = urllib.request.urlopen(req, timeout=timeout)
                    return _jfw.loads(resp.read().decode())
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _sync_pool_to_workers(pool: dict):
    """Coordinator 自动将配置池同步到所有 Worker（后台异步）。"""
    import threading
    def _do_sync():
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            import urllib.request, json as _j
            cfg = load_cluster_config()
            if cfg.get("role") != "coordinator":
                return
            coord = get_cluster_coordinator()
            if not coord:
                return
            overview = coord.get_overview()
            data = _j.dumps(pool).encode()
            for host in overview.get("hosts", []):
                if not host.get("online"):
                    continue
                ip = host.get("host_ip", "")
                port = host.get("port", 8000)
                try:
                    req = urllib.request.Request(
                        f"http://{ip}:{port}/vpn/pool/sync",
                        data=data, method="POST",
                        headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=10)
                except Exception:
                    pass
        except Exception:
            pass
    threading.Thread(target=_do_sync, daemon=True).start()


@router.get("/vpn/pool", dependencies=[Depends(verify_api_key)])
def vpn_pool_list():
    """获取配置池中所有 VPN 配置 + 设备分配关系。"""
    pool = _load_pool()
    return pool


@router.post("/vpn/pool/add", dependencies=[Depends(verify_api_key)])
def vpn_pool_add(body: dict):
    """添加一个 VPN 配置到配置池。

    Body: {uri: "vless://...", country: "italy", label: "意大利节点1"}
    """
    import time as _t
    from src.behavior.vpn_manager import parse_uri

    uri = body.get("uri", "").strip()
    if not uri:
        raise HTTPException(400, "缺少 uri")

    try:
        config = parse_uri(uri)
    except (ValueError, Exception) as e:
        raise HTTPException(400, f"无效配置: {e}")

    pool = _load_pool()
    # 生成唯一 ID
    config_id = f"vpn_{int(_t.time()*1000)}"
    entry = {
        "id": config_id,
        "uri": uri,
        "protocol": config.protocol,
        "server": config.server,
        "port": config.port,
        "remark": config.remark,
        "country": body.get("country", "").strip(),
        "label": body.get("label", "").strip() or config.remark,
        "added_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
    }
    pool["configs"].append(entry)
    _save_pool(pool)
    _sync_pool_to_workers(pool)
    return {"ok": True, "config": entry}


@router.post("/vpn/pool/add-proxy", dependencies=[Depends(verify_api_key)])
def vpn_pool_add_proxy(body: dict):
    """表单方式添加 HTTP/SOCKS5 代理到配置池（无需手写 URI）。

    Body:
    {
        "type": "socks5",          # socks5 | http
        "host": "us.proxy.922s5.com",
        "port": 10001,
        "username": "user123",
        "password": "pass456",
        "country": "us",           # 目标国家代码
        "label": "美国-纽约-01",   # 备注标签
        "city": "New York"         # 城市（用于GPS mock）
    }
    """
    import time as _t
    from src.behavior.vpn_manager import build_socks5_uri, build_http_proxy_uri, parse_uri

    proxy_type = body.get("type", "socks5").lower().strip()
    host = body.get("host", "").strip()
    port = int(body.get("port", 1080))
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    country = body.get("country", "").strip()
    city = body.get("city", "").strip()
    label = body.get("label", "").strip() or f"{proxy_type.upper()}-{host}:{port}"

    if not host:
        raise HTTPException(400, "缺少 host 参数")
    if proxy_type not in ("socks5", "http", "https"):
        raise HTTPException(400, f"不支持的代理类型: {proxy_type}，支持 socks5/http")

    # 自动生成 URI
    if proxy_type == "socks5":
        uri = build_socks5_uri(host, port, username, password, label)
    else:
        uri = build_http_proxy_uri(host, port, username, password, label)

    config = parse_uri(uri)

    pool = _load_pool()
    config_id = f"proxy_{int(_t.time()*1000)}"
    entry = {
        "id": config_id,
        "uri": uri,
        "protocol": proxy_type,
        "server": host,
        "port": str(port),
        "username": username,
        "remark": label,
        "country": country,
        "city": city,
        "label": label,
        "added_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
        "proxy_mode": "router",   # 标记为路由器代理模式（非V2RayNG）
    }
    pool["configs"].append(entry)
    _save_pool(pool)
    _sync_pool_to_workers(pool)
    return {"ok": True, "config": entry}


@router.post("/vpn/pool/import-subscription", dependencies=[Depends(verify_api_key)])
def vpn_pool_import_subscription(body: dict):
    """从订阅链接或多行 URI 批量导入配置到配置池。

    Body:
    {
        "url": "https://sub.example.com/subscribe?token=xxx",  # 订阅链接（base64编码）
        或
        "text": "vless://...\\nvmess://...\\ntrojan://...",  # 多行 URI 文本
        "country": "italy",  # 默认国家（可选）
        "replace": false     # 是否清空已有配置（默认追加）
    }
    """
    import time as _t, base64
    from src.behavior.vpn_manager import parse_uri

    url = body.get("url", "").strip()
    text = body.get("text", "").strip()
    country = body.get("country", "").strip()
    replace = body.get("replace", False)

    uris = []

    # 从订阅链接获取
    if url:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={
                "User-Agent": "v2rayNG/1.8.0"
            })
            resp = urllib.request.urlopen(req, timeout=15)
            raw = resp.read()
            # 尝试 base64 解码
            try:
                decoded = base64.b64decode(raw).decode("utf-8")
            except Exception:
                decoded = raw.decode("utf-8")
            for line in decoded.strip().splitlines():
                line = line.strip()
                if line and "://" in line:
                    uris.append(line)
        except Exception as e:
            raise HTTPException(400, f"获取订阅失败: {str(e)[:80]}")

    # 从文本解析
    elif text:
        # 尝试 base64 解码
        try:
            decoded = base64.b64decode(text).decode("utf-8")
            for line in decoded.strip().splitlines():
                line = line.strip()
                if line and "://" in line:
                    uris.append(line)
        except Exception:
            for line in text.strip().splitlines():
                line = line.strip()
                if line and "://" in line:
                    uris.append(line)
    else:
        raise HTTPException(400, "需要提供 url 或 text 参数")

    if not uris:
        raise HTTPException(400, "未找到有效的 VPN 配置链接")

    pool = _load_pool()
    if replace:
        pool["configs"] = []
        pool["assignments"] = {}

    added = 0
    errors = []
    for uri in uris:
        try:
            config = parse_uri(uri)
            config_id = f"vpn_{int(_t.time()*1000)}_{added}"
            entry = {
                "id": config_id,
                "uri": uri,
                "protocol": config.protocol,
                "server": config.server,
                "port": config.port,
                "remark": config.remark,
                "country": country,
                "label": config.remark or f"{config.protocol}@{config.server}",
                "added_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
            }
            pool["configs"].append(entry)
            added += 1
        except Exception as e:
            errors.append(f"{uri[:30]}...: {str(e)[:40]}")

    _save_pool(pool)
    _sync_pool_to_workers(pool)

    return {
        "ok": added > 0,
        "added": added,
        "errors": len(errors),
        "error_details": errors[:10],
        "total_in_pool": len(pool["configs"]),
    }


@router.post("/vpn/pool/sync", dependencies=[Depends(verify_api_key)])
def vpn_pool_sync(body: dict):
    """Worker 接收 Coordinator 推送的配置池同步数据。"""
    if "configs" in body:
        _save_pool(body)
        return {"ok": True, "synced": len(body.get("configs", []))}
    raise HTTPException(400, "无效的同步数据")


@router.delete("/vpn/pool/{config_id}", dependencies=[Depends(verify_api_key)])
def vpn_pool_remove(config_id: str):
    """从配置池中删除一个配置。"""
    pool = _load_pool()
    pool["configs"] = [c for c in pool["configs"] if c["id"] != config_id]
    pool["assignments"] = {k: v for k, v in pool["assignments"].items() if v != config_id}
    _save_pool(pool)
    _sync_pool_to_workers(pool)
    return {"ok": True}


@router.post("/vpn/pool/assign", dependencies=[Depends(verify_api_key)])
def vpn_pool_assign(body: dict):
    """分配配置到设备。

    Body: {config_id: "vpn_xxx", device_ids: ["aaa", "bbb"]}
    或    {config_id: "vpn_xxx", all: true}  分配到所有在线设备
    或    {config_id: "vpn_xxx", group_id: "abc123"}  分配到某个分组
    """
    from src.device_control.device_manager import get_device_manager, DeviceStatus

    config_id = body.get("config_id", "")
    pool = _load_pool()

    config_entry = next((c for c in pool["configs"] if c["id"] == config_id), None)
    if not config_entry:
        raise HTTPException(404, "配置不存在")

    device_ids = body.get("device_ids")
    group_id = body.get("group_id")

    if body.get("all"):
        mgr = get_device_manager(_config_path)
        device_ids = [d.device_id for d in mgr.get_all_devices()
                      if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]
    elif group_id:
        # 按分组分配
        from ..database import get_conn
        with get_conn() as conn:
            members = conn.execute(
                "SELECT device_id FROM device_group_members WHERE group_id=?",
                (group_id,)).fetchall()
            device_ids = [m["device_id"] for m in members]

    if not device_ids:
        raise HTTPException(400, "没有目标设备")

    for did in device_ids:
        pool["assignments"][did] = config_id
    _save_pool(pool)

    # 自动同步到 Worker
    _sync_pool_to_workers(pool)

    return {"ok": True, "assigned": len(device_ids), "config_id": config_id}


@router.post("/vpn/pool/apply", dependencies=[Depends(verify_api_key)])
def vpn_pool_apply(body: dict):
    """按分配关系，给每台设备应用其对应的 VPN 配置。

    Body: {device_ids: [...] (可选，默认全部有分配的设备)}
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.behavior.vpn_manager import (
        _import_via_intent, _start_vpn_adb, _stop_vpn_adb, check_vpn_status,
    )

    pool = _load_pool()
    assignments = pool.get("assignments", {})
    config_map = {c["id"]: c for c in pool.get("configs", [])}

    device_ids = body.get("device_ids")
    if not device_ids:
        device_ids = list(assignments.keys())

    targets = []
    for did in device_ids:
        cfg_id = assignments.get(did)
        if cfg_id and cfg_id in config_map:
            targets.append((did, config_map[cfg_id]))

    if not targets:
        return {"ok": False, "error": "没有可应用的设备-配置对"}

    results = {}

    verify_geo = body.get("verify_geo", False)

    def _apply_one(did, cfg):
        t0 = _t.time()
        short = did[:8]
        try:
            _stop_vpn_adb(did)
            _t.sleep(0.5)
            if not _import_via_intent(did, cfg["uri"]):
                return did, {"ok": False, "short": short, "error": "导入失败",
                             "time": _t.time() - t0}
            _t.sleep(1.5)
            _start_vpn_adb(did)
            _t.sleep(2)
            status = check_vpn_status(did)
            res = {"ok": True, "short": short, "connected": status.connected,
                   "config": cfg.get("label", ""), "country": cfg.get("country", ""),
                   "time": _t.time() - t0}
            # Geo-IP 验证
            if verify_geo and status.connected and cfg.get("country"):
                try:
                    from src.behavior.geo_check import check_device_geo
                    geo = check_device_geo(did, cfg["country"])
                    res["geo_ip"] = geo.public_ip
                    res["geo_country"] = geo.detected_country
                    res["geo_match"] = geo.matches
                except Exception:
                    res["geo_match"] = None
            return did, res
        except Exception as e:
            return did, {"ok": False, "short": short, "error": str(e)[:80],
                         "time": _t.time() - t0}

    with ThreadPoolExecutor(max_workers=10) as pool_exec:
        futs = {pool_exec.submit(_apply_one, did, cfg): did for did, cfg in targets}
        for fut in as_completed(futs):
            did, res = fut.result()
            results[did[:8]] = res

    ok_count = sum(1 for v in results.values() if v.get("ok"))
    connected = sum(1 for v in results.values() if v.get("connected"))
    return {
        "ok": ok_count > 0,
        "total": len(results),
        "imported": ok_count,
        "connected": connected,
        "results": results,
    }


@router.post("/vpn/pool/rotate", dependencies=[Depends(verify_api_key)])
def vpn_pool_rotate(body: dict = None):
    """轮换配置：按配置池中的下一个配置重新分配设备。

    Body (可选):
    {
        "strategy": "round-robin" | "random" | "country-balanced",
        "device_ids": [...] (可选，默认全部已分配设备),
        "apply": true  (是否立即应用，默认 false 只更新分配)
    }

    round-robin: 每台设备切换到配置池中的下一个配置
    random: 随机分配
    country-balanced: 按国家均匀分配（相同国家的配置轮换）
    """
    import random as _rand

    body = body or {}
    strategy = body.get("strategy", "round-robin")
    apply_now = body.get("apply", False)

    pool = _load_pool()
    configs = pool.get("configs", [])
    if len(configs) < 2:
        return {"ok": False, "error": "配置池中至少需要 2 个配置才能轮换"}

    assignments = pool.get("assignments", {})
    device_ids = body.get("device_ids") or list(assignments.keys())
    if not device_ids:
        return {"ok": False, "error": "没有已分配的设备"}

    config_ids = [c["id"] for c in configs]
    changed = 0

    if strategy == "random":
        for did in device_ids:
            old = assignments.get(did)
            candidates = [cid for cid in config_ids if cid != old] or config_ids
            assignments[did] = _rand.choice(candidates)
            changed += 1

    elif strategy == "country-balanced":
        # 按国家分组配置，每个国家内轮换
        country_configs = {}
        for c in configs:
            country = c.get("country", "unknown")
            country_configs.setdefault(country, []).append(c["id"])
        for did in device_ids:
            old = assignments.get(did)
            # 找到当前配置的国家
            old_cfg = next((c for c in configs if c["id"] == old), None)
            country = old_cfg["country"] if old_cfg else "unknown"
            pool_cids = country_configs.get(country, config_ids)
            if len(pool_cids) > 1 and old in pool_cids:
                idx = (pool_cids.index(old) + 1) % len(pool_cids)
                assignments[did] = pool_cids[idx]
            else:
                assignments[did] = pool_cids[0] if pool_cids else config_ids[0]
            changed += 1

    else:  # round-robin
        for did in device_ids:
            old = assignments.get(did)
            if old in config_ids:
                idx = (config_ids.index(old) + 1) % len(config_ids)
            else:
                idx = 0
            assignments[did] = config_ids[idx]
            changed += 1

    pool["assignments"] = assignments
    pool["last_rotation"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    _save_pool(pool)
    _sync_pool_to_workers(pool)

    result = {"ok": True, "changed": changed, "strategy": strategy}

    # 立即应用
    if apply_now:
        apply_result = vpn_pool_apply({"device_ids": device_ids})
        result["apply_result"] = apply_result

    return result


@router.post("/vpn/pool/rotation-settings", dependencies=[Depends(verify_api_key)])
def vpn_pool_rotation_settings(body: dict):
    """保存轮换设置到配置池。

    Body: {enabled: true, interval_minutes: 120, strategy: "round-robin"}
    """
    pool = _load_pool()
    pool["rotation"] = {
        "enabled": body.get("enabled", False),
        "interval_minutes": body.get("interval_minutes", 120),
        "strategy": body.get("strategy", "round-robin"),
    }
    _save_pool(pool)
    _sync_pool_to_workers(pool)
    return {"ok": True, "rotation": pool["rotation"]}


@router.post("/vpn/speed-test", dependencies=[Depends(verify_api_key)])
def vpn_speed_test(body: dict = None):
    """测试设备 VPN 连接质量（延迟 + DNS 泄漏检测）。

    Body: {device_ids: [...] (可选，默认全部在线设备)}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.device_control.device_manager import get_device_manager, DeviceStatus
    import time as _t

    body = body or {}
    device_ids = body.get("device_ids")

    if not device_ids:
        mgr = get_device_manager(_config_path)
        device_ids = [d.device_id for d in mgr.get_all_devices()
                      if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    if not device_ids:
        # Coordinator 模式：转发到 Worker
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            import urllib.request, json as _json_st
            cfg_cl = load_cluster_config()
            if cfg_cl.get("role") == "coordinator":
                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        ip = host.get("host_ip", "")
                        port = host.get("port", 8000)
                        try:
                            req = urllib.request.Request(
                                f"http://{ip}:{port}/vpn/speed-test",
                                data=_json_st.dumps({}).encode(),
                                method="POST",
                                headers={"Content-Type": "application/json"})
                            resp = urllib.request.urlopen(req, timeout=60)
                            return _json_st.loads(resp.read().decode())
                        except Exception:
                            continue
        except Exception:
            pass
        return {"ok": False, "error": "没有在线设备"}

    def _test_one(did):
        short = did[:8]
        result = {"device_id": did, "short": short, "latency_ms": None,
                  "dns_ok": None, "reachable": False}
        try:
            # Ping Google DNS 测延迟
            t0 = _t.time()
            r = _sp_run_text(
                ["adb", "-s", did, "shell", "ping", "-c", "3", "-W", "3", "8.8.8.8"],
                capture_output=True, timeout=15,
            )
            if "avg" in r.stdout:
                # 解析 avg latency
                parts = r.stdout.split("avg")[0].strip().split("/")
                if len(parts) >= 2:
                    try:
                        result["latency_ms"] = float(r.stdout.split("mdev = ")[0].split("/")[-2])
                    except Exception:
                        pass
            result["reachable"] = r.returncode == 0 and "0% packet loss" in r.stdout

            # DNS 检查（curl 一个已知网站）
            r2 = _sp_run_text(
                ["adb", "-s", did, "shell", "curl", "-s", "-o", "/dev/null",
                 "-w", "%{http_code}", "--connect-timeout", "5", "https://www.google.com"],
                capture_output=True, timeout=15,
            )
            result["dns_ok"] = r2.stdout.strip() in ("200", "301", "302")
        except Exception as e:
            result["error"] = str(e)[:60]
        return result

    results = []
    with ThreadPoolExecutor(max_workers=10) as pool_exec:
        futs = {pool_exec.submit(_test_one, did): did for did in device_ids}
        for fut in as_completed(futs):
            results.append(fut.result())

    # 统计
    reachable = sum(1 for r in results if r["reachable"])
    avg_latency = None
    latencies = [r["latency_ms"] for r in results if r["latency_ms"] is not None]
    if latencies:
        avg_latency = round(sum(latencies) / len(latencies), 1)

    return {
        "ok": True,
        "total": len(results),
        "reachable": reachable,
        "avg_latency_ms": avg_latency,
        "results": results,
    }


@router.get("/vpn/connection-history", dependencies=[Depends(verify_api_key)])
def vpn_connection_history():
    """获取 VPN 连接历史数据（用于图表）。

    返回最近 24 小时每小时的连接率快照。
    """
    from src.behavior.vpn_health import get_vpn_health_monitor
    import time as _t

    health_mon = get_vpn_health_monitor()
    all_history = health_mon.get_status() if health_mon else {}

    # Coordinator 模式：如果本机没有数据，从 Worker 获取
    if not all_history:
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            import urllib.request, json as _json_ch
            cfg_cl = load_cluster_config()
            if cfg_cl.get("role") == "coordinator":
                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        ip = host.get("host_ip", "")
                        port = host.get("port", 8000)
                        try:
                            req = urllib.request.Request(
                                f"http://{ip}:{port}/vpn/connection-history",
                                method="GET")
                            resp = urllib.request.urlopen(req, timeout=8)
                            return _json_ch.loads(resp.read().decode())
                        except Exception:
                            continue
        except Exception:
            pass

    # 构建每小时快照（从当前往前 24 小时）
    now = _t.time()
    hourly = []
    for i in range(24):
        hour_start = now - (23 - i) * 3600
        hour_end = hour_start + 3600
        hour_label = _t.strftime("%H:00", _t.localtime(hour_start))

        # 统计该小时内有事件的设备
        connected = 0
        total = len(all_history)
        for did, state in all_history.items():
            events = state.get("recent_events", [])
            # 找到该时间段内最新的状态
            latest = None
            for ev in events:
                ts = ev.get("ts", 0)
                if ts <= hour_end:
                    latest = ev
            if latest and latest.get("type") in ("connected", "check_ok"):
                connected += 1

        hourly.append({
            "hour": hour_label,
            "connected": connected,
            "total": total,
            "rate": round(connected / total * 100, 1) if total else 0,
        })

    return {"history": hourly}


@router.get("/vpn/dashboard-stats", dependencies=[Depends(verify_api_key)])
def vpn_dashboard_stats():
    """VPN 管理大盘数据：状态汇总 + 配置池 + 分配关系 + 健康历史。

    Coordinator 模式自动聚合 Worker 数据。
    """
    from src.behavior.vpn_manager import get_vpn_manager
    from src.behavior.vpn_health import get_vpn_health_monitor

    mgr = get_vpn_manager()
    manager = get_device_manager(_config_path)
    devices = manager.get_all_devices()
    health_mon = get_vpn_health_monitor()
    health_data = health_mon.get_status() if health_mon else {}

    # 本机设备 VPN 状态
    device_statuses = []
    for d in devices:
        did = d.get("device_id", "") if isinstance(d, dict) else getattr(d, "device_id", "")
        if not did:
            continue
        s = mgr.status(did)
        h = health_data.get(did, {})
        device_statuses.append({
            "device_id": did,
            "short": did[:8],
            "connected": s.connected,
            "config_name": s.config_name,
            "ip": h.get("verified_ip", ""),
            "country": health_mon._expected_countries.get(did, "") if health_mon else "",
            "failures": h.get("consecutive_failures", 0),
            "paused": h.get("paused", False),
        })

    # Coordinator 模式：聚合 Worker 的设备数据
    if not device_statuses:
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            cfg_cluster = load_cluster_config()
            if cfg_cluster.get("role") == "coordinator":
                coord = get_cluster_coordinator()
                overview = coord.get_overview()
                import urllib.request, json as _json
                for host in overview.get("hosts", []):
                    if not host.get("online"):
                        continue
                    host_ip = host.get("host_ip", "")
                    port = host.get("port", 8000)
                    for ip in _get_worker_ips(coord, host.get("host_id", ""), host_ip):
                        try:
                            url = f"http://{ip}:{port}/vpn/dashboard-stats"
                            req = urllib.request.Request(url, method="GET")
                            resp = urllib.request.urlopen(req, timeout=8)
                            worker_data = _json.loads(resp.read().decode())
                            device_statuses.extend(worker_data.get("devices", []))
                            # 用 Worker 的 current_config 作为 fallback
                            if not mgr.current_config and worker_data.get("current_config"):
                                mgr._current_config_cache = worker_data["current_config"]
                            break
                        except Exception:
                            continue
        except Exception:
            pass

    connected = sum(1 for d in device_statuses if d["connected"])
    total = len(device_statuses)
    disconnected = total - connected
    failed = sum(1 for d in device_statuses if d.get("failures", 0) >= 3)

    # 配置池
    pool = _load_pool()

    # 当前活跃配置
    cfg = mgr.current_config
    current = {
        "remark": cfg.remark if cfg else "",
        "server": cfg.server if cfg else "",
        "protocol": cfg.protocol if cfg else "",
    } if cfg else None

    return {
        "summary": {
            "total": total,
            "connected": connected,
            "disconnected": disconnected,
            "failed": failed,
        },
        "devices": device_statuses,
        "pool": pool,
        "current_config": current,
        "auto_reconnect": health_mon.auto_reconnect_enabled if health_mon else False,
    }


@router.get("/vpn/pool/export", dependencies=[Depends(verify_api_key)])
def vpn_pool_export(format: str = "json"):
    """导出配置池。

    Query: ?format=json (完整JSON) 或 ?format=subscription (base64订阅)
    """
    import base64
    pool = _load_pool()

    if format == "subscription":
        # 导出为 base64 订阅格式
        uris = [c["uri"] for c in pool.get("configs", []) if c.get("uri")]
        raw = "\n".join(uris)
        encoded = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        return {"format": "subscription", "data": encoded,
                "count": len(uris)}
    else:
        return {"format": "json", "data": pool, "count": len(pool.get("configs", []))}


@router.post("/vpn/pool/deploy", dependencies=[Depends(verify_api_key)])
def vpn_pool_deploy(body: dict):
    """一键全流程部署：选配置 → 分配全部 → 应用 → Geo-IP 验证。

    Body:
    {
        "config_id": "vpn_xxx",      # 要部署的配置 ID
        "device_ids": [...],          # 目标设备（可选，默认全部在线）
        "verify_geo": true            # 是否验证 Geo-IP（默认 true）
    }

    流程: assign → apply → geo verify → update scores
    """
    from src.device_control.device_manager import get_device_manager, DeviceStatus

    config_id = body.get("config_id", "")
    verify_geo = body.get("verify_geo", True)
    device_ids = body.get("device_ids")

    pool = _load_pool()
    config_entry = next((c for c in pool["configs"] if c["id"] == config_id), None)
    if not config_entry:
        raise HTTPException(404, "配置不存在")

    # Step 1: 分配
    if not device_ids:
        mgr = get_device_manager(_config_path)
        device_ids = [d.device_id for d in mgr.get_all_devices()
                      if d.status in (DeviceStatus.CONNECTED, DeviceStatus.BUSY)]

    if not device_ids:
        return {"ok": False, "error": "没有在线设备"}

    for did in device_ids:
        pool["assignments"][did] = config_id
    _save_pool(pool)

    # Step 2: 应用 + Geo 验证
    result = vpn_pool_apply({"device_ids": device_ids, "verify_geo": verify_geo})

    # Step 3: 更新配置健康评分
    _update_config_score(config_id, result.get("results", {}))

    result["deployed_config"] = config_entry.get("label", config_entry.get("remark", ""))
    result["deployed_country"] = config_entry.get("country", "")
    return result


def _update_config_score(config_id: str, results: dict):
    """根据部署结果更新配置的健康评分。"""
    pool = _load_pool()
    cfg = next((c for c in pool["configs"] if c["id"] == config_id), None)
    if not cfg:
        return

    total = len(results)
    if total == 0:
        return

    connected = sum(1 for r in results.values() if r.get("connected"))
    geo_checked = sum(1 for r in results.values() if r.get("geo_match") is not None)
    geo_ok = sum(1 for r in results.values() if r.get("geo_match") is True)

    # 计算评分 (0-100)
    connect_rate = connected / total if total else 0
    geo_rate = geo_ok / geo_checked if geo_checked else 1.0  # 没检查的不扣分

    # 加权: 连接率 60% + Geo 匹配率 40%
    score = round((connect_rate * 60 + geo_rate * 40), 1)

    # 指数平滑: 新评分 = 0.3 * 本次 + 0.7 * 历史
    old_score = cfg.get("score", 50)
    cfg["score"] = round(0.3 * score + 0.7 * old_score, 1)
    cfg["last_deploy"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
    cfg["last_connect_rate"] = round(connect_rate * 100, 1)

    _save_pool(pool)


@router.post("/vpn/geo-verify-all", dependencies=[Depends(verify_api_key)])
def vpn_geo_verify_all(body: dict = None):
    """对所有 VPN 已连接的设备执行 Geo-IP 验证。

    返回每台设备的真实 IP 和国家。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from src.behavior.vpn_manager import get_vpn_manager
    from src.behavior.geo_check import check_device_geo

    body = body or {}
    mgr = get_vpn_manager()
    manager = get_device_manager(_config_path)
    devices = manager.get_all_devices()

    pool = _load_pool()
    assignments = pool.get("assignments", {})
    config_map = {c["id"]: c for c in pool.get("configs", [])}

    targets = []
    for d in devices:
        did = d.get("device_id", "") if isinstance(d, dict) else getattr(d, "device_id", "")
        if not did:
            continue
        s = mgr.status(did)
        if s.connected:
            cfg_id = assignments.get(did)
            cfg = config_map.get(cfg_id, {}) if cfg_id else {}
            country = cfg.get("country", "")
            targets.append((did, country))

    if not targets:
        # Coordinator: 转发到 Worker
        try:
            from src.host.multi_host import get_cluster_coordinator, load_cluster_config
            import urllib.request, json as _json_gv
            cfg_cl = load_cluster_config()
            if cfg_cl.get("role") == "coordinator":
                coord = get_cluster_coordinator()
                if coord:
                    overview = coord.get_overview()
                    for host in overview.get("hosts", []):
                        if not host.get("online"):
                            continue
                        ip = host.get("host_ip", "")
                        port = host.get("port", 8000)
                        try:
                            req = urllib.request.Request(
                                f"http://{ip}:{port}/vpn/geo-verify-all",
                                data=_json_gv.dumps({}).encode(),
                                method="POST",
                                headers={"Content-Type": "application/json"})
                            resp = urllib.request.urlopen(req, timeout=60)
                            return _json_gv.loads(resp.read().decode())
                        except Exception:
                            continue
        except Exception:
            pass
        return {"ok": False, "error": "没有已连接设备", "results": []}

    results = []

    def _check_one(did, country):
        try:
            geo = check_device_geo(did, country or "")
            return {
                "device_id": did,
                "short": did[:8],
                "ip": geo.public_ip,
                "country": geo.detected_country,
                "country_code": geo.detected_country_code,
                "expected": country,
                "match": geo.matches if country else None,
                "vpn_detected": geo.vpn_detected,
            }
        except Exception as e:
            return {"device_id": did, "short": did[:8], "error": str(e)[:60]}

    with ThreadPoolExecutor(max_workers=5) as pool_exec:
        futs = {pool_exec.submit(_check_one, did, c): did for did, c in targets}
        for fut in as_completed(futs):
            results.append(fut.result())

    matched = sum(1 for r in results if r.get("match") is True)
    mismatched = sum(1 for r in results if r.get("match") is False)

    return {
        "ok": True,
        "total": len(results),
        "matched": matched,
        "mismatched": mismatched,
        "results": results,
    }


@router.get("/vpn/qr-history", dependencies=[Depends(verify_api_key)])
def vpn_qr_history():
    """列出已上传的 VPN 二维码历史。"""
    from src.host.vpn_qr_service import list_qr_images
    return {"images": list_qr_images()}


@router.post("/vpn/cluster-distribute")
def vpn_cluster_distribute(body: dict):
    """Coordinator 专用：把 VPN URI 分发到所有在线 Worker。

    由 Worker 调用，Worker 自己已经配好了本机设备，
    请求 Coordinator 帮忙把同一个 URI 推送到其他 Worker。

    Body: {"uri": "vless://...", "skip_host_id": "worker-03"}
    """
    from src.host.vpn_qr_service import validate_vpn_uri, _distribute_to_workers

    uri = body.get("uri", "").strip()
    valid, msg = validate_vpn_uri(uri)
    if not valid:
        raise HTTPException(400, f"无效配置: {msg}")

    skip = body.get("skip_host_id", "")
    results = _distribute_to_workers(uri, skip_host_id=skip)

    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_count > 0 or len(results) == 0,
        "total": len(results),
        "success": ok_count,
        "failed": len(results) - ok_count,
        "results": results,
    }


@router.get("/vpn/qr-folder")
def vpn_qr_folder():
    """返回 VPN 二维码存放目录路径。"""
    from src.host.vpn_qr_service import QR_DIR
    return {"path": str(QR_DIR), "exists": QR_DIR.exists()}


# ── V2RayNG 安装 ──

@router.get("/vpn/install-v2rayng/check", dependencies=[Depends(verify_api_key)])
def vpn_check_v2rayng_apk():
    """检查 apk_repo/ 中是否存在 V2RayNG APK。"""
    apk_repo = _project_root / "apk_repo"
    found = []
    for apk in sorted(apk_repo.glob("*.apk")):
        name = apk.name.lower()
        if "v2ray" in name:
            found.append({"name": apk.name, "size_kb": round(apk.stat().st_size / 1024)})
    return {
        "apk_repo": str(apk_repo),
        "found": found,
        "ready": len(found) > 0,
    }


@router.post("/vpn/install-v2rayng", dependencies=[Depends(verify_api_key)])
def vpn_install_v2rayng(body: dict = None):
    """批量安装 V2RayNG APK 到设备。

    Body: {"device_ids": ["xxx", ...]}  — 可选，默认全部在线设备

    APK 需预先放入 apk_repo/ 目录（文件名含 v2ray）。
    """
    try:
        return _vpn_install_v2rayng_impl(body)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        raise HTTPException(500, f"安装失败: {e}\n{traceback.format_exc()}")


def _vpn_install_v2rayng_impl(body):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    body = body or {}
    device_ids = body.get("device_ids")

    # 查找 APK（项目根目录 apk_repo/）
    _proj = _project_root
    _cfg = _config_path
    apk_repo = _proj / "apk_repo"
    # 读取 adb 路径（来自 devices.yaml，默认 adb）
    _adb_exe = "adb"
    try:
        import yaml as _yaml
        with open(_cfg, "r", encoding="utf-8") as _f:
            _dcfg = _yaml.safe_load(_f) or {}
        _adb_exe = (_dcfg.get("connection") or {}).get("adb_path", "adb")
    except Exception:
        pass
    v2rayng_apk = None
    for apk in sorted(apk_repo.glob("*.apk")):
        if "v2ray" in apk.name.lower():
            v2rayng_apk = apk
            break

    if not v2rayng_apk:
        raise HTTPException(
            400,
            f"apk_repo/ 中未找到 V2RayNG APK。"
            f"请将 v2rayng-*.apk 下载后放入 {apk_repo} 目录，再重试。"
        )

    if device_ids:
        targets = list(device_ids)
    else:
        manager = get_device_manager(_cfg)
        targets = [d.device_id for d in manager.get_all_devices() if d.is_online]

    if not targets:
        raise HTTPException(400, "没有可用的在线设备")

    results = {}

    def _install_one(did):
        # 用 push + pm install 绕开 MIUI 14+ 的 securitycenter/AdbInstallActivity 拦截
        from src.utils.safe_apk_install import safe_install_apk
        try:
            success, output = safe_install_apk(
                _adb_exe, did, str(v2rayng_apk),
                replace=True, test=True, timeout=120)
            if success:
                _sp_run_text(
                    [_adb_exe, "-s", did, "shell",
                     "appops", "set", "com.v2ray.ang",
                     "SYSTEM_ALERT_WINDOW", "allow"],
                    capture_output=True, timeout=10,
                )
                return True, "安装成功"
            return False, output[:200] or "安装失败（未知错误）"
        except Exception as e:
            return False, str(e)

    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(_install_one, did): did for did in targets}
        for fut in as_completed(futs):
            did = futs[fut]
            ok, msg = fut.result()
            results[did] = {"success": ok, "message": msg}

    success_count = sum(1 for v in results.values() if v["success"])
    logger.info("[V2RayNG安装] %d/%d 成功, APK=%s", success_count, len(results), v2rayng_apk.name)
    return {
        "apk": v2rayng_apk.name,
        "apk_size_kb": round(v2rayng_apk.stat().st_size / 1024),
        "total": len(results),
        "success": success_count,
        "failed": len(results) - success_count,
        "results": results,
    }
