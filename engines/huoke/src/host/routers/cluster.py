# -*- coding: utf-8 -*-
"""Cluster / Multi-Host API — 集群管理路由。"""

import json as _json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from .auth import verify_api_key, requires_role
from src.openclaw_env import DEFAULT_OPENCLAW_PORT
from src.host.device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT, config_file, scripts_dir

logger = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["cluster"])

# ── 心跳驱动的自动编号（防抖2分钟，仅协调者响应此端点）────────────────────
import time as _time_c
_last_auto_assign_ts = 0.0
_AUTO_ASSIGN_DEBOUNCE = 120  # 最少间隔120秒


def _try_heartbeat_auto_assign(host_id: str) -> None:
    """心跳后检查是否有新设备需要自动编号。
    仅在段配置存在 + 有未编号设备 + 距上次至少2分钟时才触发。
    """
    global _last_auto_assign_ts
    now = _time_c.time()
    if now - _last_auto_assign_ts < _AUTO_ASSIGN_DEBOUNCE:
        return
    try:
        ranges_path = config_file("number_ranges.json")
        if not ranges_path.exists():
            return
        import json as _j2
        with open(ranges_path, encoding="utf-8") as _f:
            ranges = _j2.load(_f)
        if not ranges:
            return
        from .devices_core import _load_aliases_global, auto_assign_segments
        global_aliases = _load_aliases_global()
        unassigned = [did for did, info in global_aliases.items() if not info.get("number")]
        if not unassigned:
            return
        _last_auto_assign_ts = now  # 标记已触发，防止并发重入
        import threading

        def _bg():
            try:
                r = auto_assign_segments({})
                cnt = r.get("assigned", 0)
                if cnt > 0:
                    logger.info("[heartbeat-auto] 自动分配 %d 台新设备编号（触发来自 %s）", cnt, host_id)
            except Exception as e:
                logger.debug("[heartbeat-auto] 自动编号失败: %s", e)

        threading.Thread(target=_bg, daemon=True, name="heartbeat-auto-assign").start()
    except Exception:
        pass


def _http(req: urllib.request.Request, timeout: int = 15) -> bytes:
    """统一 HTTP 调用，确保连接关闭防止 CLOSE_WAIT 泄漏。"""
    req.add_header("Connection", "close")
    resp = urllib.request.urlopen(req, timeout=timeout)
    try:
        return resp.read()
    finally:
        resp.close()

_project_root = PROJECT_ROOT
_config_path = DEFAULT_DEVICES_YAML
_scripts_dir = scripts_dir()


# ── helpers ──────────────────────────────────────────────────────────────

import threading
import time

_worker_best_ip = {}  # host_id -> {"ip": "x.x.x.x", "checked_at": timestamp}
_worker_ip_lock = threading.Lock()


def _get_best_worker_url(device_id: str) -> dict:
    """找到设备所在 Worker 的最佳可达 IP。"""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    all_devs = coord.get_all_devices()

    target = None
    for d in all_devs:
        if d.get("device_id") == device_id:
            target = d
            break
    if not target:
        return None

    host_id = target.get("host_id", "")
    host_ip = target.get("host_ip", "")
    port = target.get("host_port", 8000)

    # 获取该 Worker 的所有 IP
    all_ips = []
    hosts = coord._hosts if hasattr(coord, '_hosts') else {}
    host_info = hosts.get(host_id)
    if host_info and hasattr(host_info, 'ips') and host_info.ips:
        all_ips = list(host_info.ips)

    # 确保 advertise_ip 也在列表中
    if host_ip and host_ip not in all_ips:
        all_ips.insert(0, host_ip)

    if not all_ips:
        return {"ip": host_ip, "port": port}

    # 检查缓存（60秒内有效）
    with _worker_ip_lock:
        cached = _worker_best_ip.get(host_id)
        if cached and time.time() - cached["checked_at"] < 60:
            return {"ip": cached["ip"], "port": port}

    # 逐个测试连通性（最多 3 秒超时）
    best_ip = all_ips[0]  # 默认第一个
    for ip in all_ips:
        try:
            url = f"http://{ip}:{port}/health"
            req = urllib.request.Request(url, method="GET")
            _http(req, timeout=3)
            best_ip = ip
            break  # 第一个能通的就用
        except Exception:
            continue

    with _worker_ip_lock:
        _worker_best_ip[host_id] = {"ip": best_ip, "checked_at": time.time()}

    return {"ip": best_ip, "port": port}


def _reachable_ip_for_host_info(host) -> str:
    """对集群 HostInfo 选取第一个能打开 /health 的 IP（与 _get_best_worker_url 共用 60s 缓存）。"""
    host_id = getattr(host, "host_id", "") or ""
    hip = (getattr(host, "host_ip", None) or "").strip()
    port = int(getattr(host, "port", None) or 8000)
    all_ips = []
    if hip:
        all_ips.append(hip)
    for ip in getattr(host, "ips", None) or []:
        if ip and ip not in all_ips:
            all_ips.append(ip)
    if not all_ips:
        return hip or "127.0.0.1"

    with _worker_ip_lock:
        cached = _worker_best_ip.get(host_id)
        if cached and time.time() - cached["checked_at"] < 60:
            return cached["ip"]

    best_ip = all_ips[0]
    for ip in all_ips:
        try:
            url = f"http://{ip}:{port}/health"
            req = urllib.request.Request(url, method="GET")
            _http(req, timeout=3)
            best_ip = ip
            break
        except Exception:
            continue

    with _worker_ip_lock:
        _worker_best_ip[host_id] = {"ip": best_ip, "checked_at": time.time()}
    return best_ip


def _get_worker_url_by_host_id(host_id: str):
    """在线 Worker 的可达 {ip, port}；host 离线或不存在时返回 None。"""
    from ..multi_host import get_cluster_coordinator

    coord = get_cluster_coordinator()
    with coord._lock:
        host = coord._hosts.get(host_id)
    if not host or not getattr(host, "online", False):
        return None
    ip = _reachable_ip_for_host_info(host)
    port = int(host.port or 8000)
    return {"ip": ip, "port": port}


def _verify_cluster_secret(body: dict, request_headers: dict = None):
    """Verify heartbeat authenticity via HMAC-SHA256 or legacy plain secret."""
    from ..multi_host import load_cluster_config
    import hmac, hashlib, time as _time
    cfg = load_cluster_config()
    secret = cfg.get("shared_secret", "")
    if not secret:
        return  # No secret configured, allow all

    # New: HMAC signature in body
    sig = body.get("_sig")
    ts = body.get("_ts")
    host_id = body.get("host_id", "")

    if sig and ts:
        # Validate timestamp (±60s to allow clock drift)
        try:
            ts_val = float(ts)
            if abs(_time.time() - ts_val) > 60:
                raise HTTPException(403, "Heartbeat timestamp expired")
        except (ValueError, TypeError):
            raise HTTPException(403, "Invalid timestamp")

        # Validate HMAC: sign(secret, host_id + ":" + ts)
        expected = hmac.new(
            secret.encode(),
            f"{host_id}:{ts}".encode(),
            hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(403, "Invalid cluster signature")
        return

    # Legacy: plain secret in body (backward compat)
    if body.get("secret") != secret:
        raise HTTPException(403, "Invalid cluster secret")


def _cluster_proxy_post(device_id: str, path: str, body: dict):
    """Generic POST proxy to the worker that owns the device."""
    target_host = _get_best_worker_url(device_id)
    if not target_host:
        raise HTTPException(404, "Device not found in cluster")
    url = f"http://{target_host['ip']}:{target_host['port']}{path}"
    try:
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        return _json.loads(_http(req, timeout=15).decode())
    except Exception as e:
        raise HTTPException(502, f"Cluster proxy failed: {e}")


# ── endpoints ────────────────────────────────────────────────────────────


@router.post("/cluster/heartbeat")
def cluster_heartbeat(body: dict):
    """Receive heartbeat from a worker host."""
    _verify_cluster_secret(body)
    from ..multi_host import get_cluster_coordinator
    result = get_cluster_coordinator().receive_heartbeat(body)
    # 心跳驱动：检测新设备并自动编号（防抖2分钟）
    _try_heartbeat_auto_assign(body.get("host_id", ""))
    return result


@router.get("/cluster/overview")
def cluster_overview():
    """Get aggregated cluster status."""
    from ..multi_host import get_cluster_coordinator
    return get_cluster_coordinator().get_overview()


@router.get("/cluster/devices")
def cluster_all_devices():
    """Get unified device list across all hosts."""
    from ..multi_host import get_cluster_coordinator
    return {"devices": get_cluster_coordinator().get_all_devices()}


@router.get("/cluster/reverse-probe/status")
def cluster_reverse_probe_status():
    """主控 reverse heartbeat prober 状态 (Stage I/K/L 暴露).

    返回字段:
      - running: prober 是否在跑
      - iterations: 累计 tick 次数
      - total_probed / total_recovered: 累计 probe 数 / 成功数
      - interval_sec / max_interval_sec / backoff_multiplier: 退避配置
      - per_host_backoff: {host_id: 当前退避间隔} — 失败 host 看退避节奏
    """
    from .. import multi_host as _mh
    pr = _mh._reverse_prober
    if pr is None:
        return {
            "running": False,
            "reason": "not started (非 coordinator role 或 OPENCLAW_DISABLE_REVERSE_PROBE)",
        }
    return pr.status()


@router.post("/cluster/reverse-probe/trigger")
def cluster_reverse_probe_trigger(body: dict = None):  # type: ignore[assignment]
    """Stage M.1 容灾响应加速: 让主控立即 probe 一个 host (跳过退避窗口).

    body: {"host_id": "w03"} or empty 触发 全 stale host probe
    返回: {"probed": [...], "recovered": [...]}

    用途:
      - worker push 心跳挂掉时, worker 调本 endpoint 让主控立即接管探测
        (容灾响应从 30s probe 间隔降到 ~1s)
      - ops 手动 trigger 调试

    安全: shared_secret 验证 (与 /cluster/heartbeat 同).
    """
    body = body or {}
    _verify_cluster_secret(body)
    import time as _t
    from .. import multi_host as _mh
    coord = _mh.get_cluster_coordinator()
    # 2026-05-05 Stage V.1: 优先级 host_id (单) > host_ids (批) > {} 全 stale.
    # 让 ops 一次性 trigger 多个特定 worker (e.g. 网络分区恢复时).
    target = (body.get("host_id") or "").strip()
    targets_batch: list[str] = []
    if not target:
        raw_batch = body.get("host_ids")
        if isinstance(raw_batch, list):
            targets_batch = [
                str(h).strip() for h in raw_batch if str(h).strip()
            ]
    # 2026-05-05 Stage P.2: wait field 控制 sync vs async.
    #   wait=True (default): sync 等 probe 完成, 返 {probed, recovered}
    #   wait=False: async 入队 (set wake_event), 立即返 {queued, mode:async}
    # Worker auto-trigger (N.1) 用 wait=False 不阻塞自身心跳循环;
    # dashboard 单击用 wait=True 拿即时反馈.
    wait = bool(body.get("wait", True))

    # ── Async 路径: 委托 prober wake event 处理 ──
    if not wait:
        prober = _mh._reverse_prober
        if prober is None:
            # prober 没启动 → 必须 sync 兜底 (worker mode 或 env disabled)
            wait = True
        else:
            queued: list[str] = []
            now = _t.time()
            if target:
                queued.append(target)
                prober.request_immediate_probe(target)
            elif targets_batch:
                # V.1: 批量 host_ids — 逐个 pop 退避 + 一次 wake
                for hid in targets_batch:
                    queued.append(hid)
                    prober._next_probe_at.pop(hid, None)
                prober.request_immediate_probe("")  # wake 一次
            else:
                with coord._lock:
                    stale = [
                        hid for hid, h in coord._hosts.items()
                        if hid != "coordinator" and h.host_ip
                        and (now - h.last_heartbeat) > _mh._HOST_TIMEOUT
                    ]
                for hid in stale:
                    queued.append(hid)
                # 全 stale 一次性 wake (传空 host_id 清所有退避)
                if stale:
                    prober.request_immediate_probe("")
            return {"queued": queued, "mode": "async"}

    # ── Sync 路径 (现行行为, dashboard 默认) ──
    probed: list[str] = []
    recovered: list[str] = []
    now = _t.time()
    if target:
        # 单 host 立即 probe (清退避窗口)
        if _mh._reverse_prober is not None:
            _mh._reverse_prober._next_probe_at.pop(target, None)
        probed.append(target)
        if coord.reverse_probe_worker(target):
            recovered.append(target)
    elif targets_batch:
        # V.1: 批量精确 trigger
        for hid in targets_batch:
            if _mh._reverse_prober is not None:
                _mh._reverse_prober._next_probe_at.pop(hid, None)
            probed.append(hid)
            if coord.reverse_probe_worker(hid):
                recovered.append(hid)
    else:
        # 全 stale host 立即 probe (清所有退避)
        with coord._lock:
            stale = [
                hid for hid, h in coord._hosts.items()
                if hid != "coordinator" and h.host_ip
                and (now - h.last_heartbeat) > _mh._HOST_TIMEOUT
            ]
        for hid in stale:
            if _mh._reverse_prober is not None:
                _mh._reverse_prober._next_probe_at.pop(hid, None)
            probed.append(hid)
            if coord.reverse_probe_worker(hid):
                recovered.append(hid)
    return {"probed": probed, "recovered": recovered, "mode": "sync"}


@router.get("/cluster/execution-policies")
def cluster_execution_policies():
    """聚合本机与各 Worker 的 /tasks/meta/execution-policy（经主控中转，避免浏览器直连 Worker CORS）。"""
    import os

    from src.host.task_policy import load_task_execution_policy

    pol = load_task_execution_policy()
    local = {
        "manual_execution_only": bool(pol.get("manual_execution_only")),
        "disable_db_scheduler": bool(pol.get("disable_db_scheduler", True)),
        "disable_json_scheduled_jobs": bool(pol.get("disable_json_scheduled_jobs", True)),
        "disable_reconnect_task_recovery": bool(pol.get("disable_reconnect_task_recovery", True)),
        "disable_auto_tiktok_check_inbox": bool(pol.get("disable_auto_tiktok_check_inbox")),
        "disable_executor_inbox_followup": bool(pol.get("disable_executor_inbox_followup")),
        "disable_strategy_optimizer": bool(pol.get("disable_strategy_optimizer")),
        "disable_event_driven_auto_tasks": bool(pol.get("disable_event_driven_auto_tasks")),
    }
    nodes = [
        {
            "role": "coordinator",
            "host_id": "__local__",
            "host_name": "本机（当前节点）",
            "host_ip": "",
            "online": True,
            "policy": local,
        }
    ]
    key = os.environ.get("OPENCLAW_API_KEY", "").strip()
    hdrs: dict = {}
    if key:
        hdrs["X-API-Key"] = key
    try:
        from ..multi_host import get_cluster_coordinator

        coord = get_cluster_coordinator()
        for host in coord._hosts.values():
            hid = host.host_id
            name = host.host_name or hid[:8]
            hip = (host.host_ip or "").strip()
            port = int(host.port or 8000)
            if not getattr(host, "online", False):
                nodes.append(
                    {
                        "role": "worker",
                        "host_id": hid,
                        "host_name": name,
                        "host_ip": hip,
                        "port": port,
                        "online": False,
                        "error": "offline",
                    }
                )
                continue
            probe_ip = _reachable_ip_for_host_info(host)
            url = f"http://{probe_ip}:{port}/tasks/meta/execution-policy"
            try:
                req = urllib.request.Request(url, headers=hdrs, method="GET")
                raw = _http(req, timeout=4)
                p2 = _json.loads(raw.decode())
                nodes.append(
                    {
                        "role": "worker",
                        "host_id": hid,
                        "host_name": name,
                        "host_ip": hip,
                        "reachable_ip": probe_ip,
                        "port": port,
                        "online": True,
                        "policy": {
                            "manual_execution_only": bool(p2.get("manual_execution_only")),
                            "disable_db_scheduler": bool(p2.get("disable_db_scheduler", True)),
                            "disable_json_scheduled_jobs": bool(p2.get("disable_json_scheduled_jobs", True)),
                            "disable_reconnect_task_recovery": bool(
                                p2.get("disable_reconnect_task_recovery", True)
                            ),
                            "disable_auto_tiktok_check_inbox": bool(
                                p2.get("disable_auto_tiktok_check_inbox")
                            ),
                            "disable_executor_inbox_followup": bool(
                                p2.get("disable_executor_inbox_followup")
                            ),
                            "disable_strategy_optimizer": bool(p2.get("disable_strategy_optimizer")),
                            "disable_event_driven_auto_tasks": bool(
                                p2.get("disable_event_driven_auto_tasks")
                            ),
                        },
                    }
                )
            except Exception as e:
                nodes.append(
                    {
                        "role": "worker",
                        "host_id": hid,
                        "host_name": name,
                        "host_ip": hip,
                        "reachable_ip": probe_ip,
                        "port": port,
                        "online": True,
                        "error": str(e)[:200],
                    }
                )
    except Exception:
        pass
    return {"nodes": nodes}


@router.post("/cluster/dispatch")
def cluster_dispatch_task(body: dict):
    """
    Dispatch a task to the best host in the cluster.
    Routes to the host with the target device, or the least loaded host.
    """
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    task_type = body.get("type", "")
    device_id = body.get("device_id", "")

    target = coord.select_host_for_task(task_type, device_id)
    if not target:
        raise HTTPException(status_code=503,
                            detail="No online hosts available")

    target_url = target["url"]
    try:
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(
            f"{target_url}/tasks",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        result = _json.loads(_http(req, timeout=15).decode())
        result["routed_to"] = target
        return result
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"Failed to dispatch to {target_url}: {e}")


@router.post("/cluster/join")
def cluster_join(body: dict):
    """Start sending heartbeats to a coordinator."""
    coordinator_url = body.get("coordinator_url", "")
    local_port = body.get("local_port", DEFAULT_OPENCLAW_PORT)
    if not coordinator_url:
        raise HTTPException(status_code=400,
                            detail="coordinator_url required")
    from ..multi_host import start_heartbeat_sender
    sender = start_heartbeat_sender(coordinator_url, local_port)
    return {"status": "joined", "coordinator": coordinator_url}


@router.delete("/cluster/hosts/{host_id}")
def cluster_remove_host(host_id: str):
    """Remove a host from the cluster."""
    from ..multi_host import get_cluster_coordinator
    get_cluster_coordinator().remove_host(host_id)
    return {"status": "removed", "host_id": host_id}


@router.get("/cluster/devices/{device_id}/screenshot",
            )
def cluster_device_screenshot(device_id: str, max_h: int = 360,
                              quality: int = 40):
    """Proxy screenshot request to the worker that owns the device."""
    from fastapi.responses import Response as _Resp
    from src.device_control.device_manager import get_device_manager
    from ..routers.devices_control import device_screenshot

    target_host = _get_best_worker_url(device_id)

    if not target_host:
        manager = get_device_manager(_config_path)
        info = manager.get_device_info(device_id)
        if info:
            return device_screenshot(device_id, max_h=max_h, quality=quality)
        raise HTTPException(404, "Device not found in cluster")

    url = (f"http://{target_host['ip']}:{target_host['port']}"
           f"/devices/{device_id}/screenshot?max_h={max_h}&quality={quality}")
    try:
        req = urllib.request.Request(url, method="GET")
        jpeg_data = _http(req, timeout=20)
        return _Resp(content=jpeg_data, media_type="image/jpeg",
                     headers={"Cache-Control": "no-cache",
                              "X-Proxied-From": target_host["ip"]})
    except Exception as e:
        raise HTTPException(502, f"Screenshot proxy failed: {e}")


@router.post("/cluster/devices/{device_id}/shell",
             )
def cluster_device_shell(device_id: str, body: dict):
    """Proxy shell command to the worker that owns the device."""
    target_host = _get_best_worker_url(device_id)
    if not target_host:
        raise HTTPException(404, "Device not found in cluster")
    url = f"http://{target_host['ip']}:{target_host['port']}/devices/{device_id}/shell"
    try:
        payload = _json.dumps(body).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        return _json.loads(_http(req, timeout=15).decode())
    except Exception as e:
        raise HTTPException(502, f"Shell proxy failed: {e}")


@router.post("/cluster/devices/{device_id}/input/tap",
             )
def cluster_device_tap(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/tap", body)


@router.post("/cluster/devices/{device_id}/input/swipe",
             )
def cluster_device_swipe(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/swipe", body)


@router.post("/cluster/devices/{device_id}/input/key",
             )
def cluster_device_key(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/key", body)


@router.post("/cluster/devices/{device_id}/input/text",
             )
def cluster_device_text(device_id: str, body: dict):
    return _cluster_proxy_post(device_id, f"/devices/{device_id}/input/text", body)


@router.post("/cluster/refresh-devices")
def refresh_cluster_devices():
    """手动触发从所有 Worker 刷新设备列表。"""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    if not coord:
        raise HTTPException(status_code=400, detail="非集群模式或未初始化")
    count = coord.refresh_all_devices()
    return {"ok": True, "total_devices": count}


@router.get("/cluster/config")
def cluster_get_config():
    """Get current cluster configuration."""
    from ..multi_host import load_cluster_config
    return load_cluster_config()


@router.websocket("/cluster/devices/{device_id}/stream/ws")
async def cluster_stream_proxy(websocket: WebSocket, device_id: str):
    """WebSocket tunnel: proxy scrcpy stream from remote worker to browser.

    Coordinator receives browser WS, opens WS to worker, relays frames.
    """
    import asyncio
    import websockets

    target = _get_best_worker_url(device_id)

    if not target:
        await websocket.close(code=4004, reason="Device not in cluster")
        return

    await websocket.accept()
    worker_url = f"ws://{target['ip']}:{target['port']}/devices/{device_id}/stream/ws"

    try:
        async with websockets.connect(worker_url) as worker_ws:
            async def browser_to_worker():
                try:
                    while True:
                        data = await websocket.receive()
                        if "text" in data:
                            await worker_ws.send(data["text"])
                        elif "bytes" in data:
                            await worker_ws.send(data["bytes"])
                except Exception:
                    pass

            async def worker_to_browser():
                try:
                    async for msg in worker_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception:
                    pass

            await asyncio.gather(browser_to_worker(), worker_to_browser())
    except ImportError:
        await websocket.send_text(
            '{"error":"websockets library not installed, '
            'install with: pip install websockets"}')
        await websocket.close()
    except Exception as e:
        try:
            await websocket.send_text(f'{{"error":"{e}"}}')
            await websocket.close()
        except Exception:
            pass


@router.post("/cluster/batch")
def cluster_batch_task(body: dict):
    """Dispatch a task to all online devices across all hosts."""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    task_type = body.get("type") or body.get("task_type", "")
    target = body.get("target", "all")
    params = body.get("params", {})
    if not task_type:
        raise HTTPException(400, "type/task_type required")

    overview = coord.get_overview()
    hosts = [h for h in overview.get("hosts", []) if h.get("online")]
    results = []
    for h in hosts:
        if target.startswith("host:") and h.get("host_name") != target[5:]:
            continue
        url = f"http://{h['host_ip']}:{h['port']}/tasks/batch"
        try:
            payload = _json.dumps({
                "type": task_type, "params": params,
                "device_ids": [d.get("device_id") for d in
                               coord.get_all_devices()
                               if d.get("host_id") == h.get("host_id")
                               and d.get("status") == "connected"],
            }).encode()
            req = urllib.request.Request(
                url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST")
            r = _json.loads(_http(req, timeout=15).decode())
            results.append({
                "host": h.get("host_name", h["host_ip"]),
                "status": "ok", "result": r,
            })
        except Exception as e:
            results.append({
                "host": h.get("host_name", h["host_ip"]),
                "status": "error", "error": str(e),
            })
    return {"ok": True, "results": results, "hosts_targeted": len(hosts)}


async def run_cluster_batch_install_apk(request: Request):
    """
    主控将 APK 按设备所属 Worker 分组，转发到各机的 POST /batch/install-apk。
    Body 与 /batch/install-apk 相同：apk_data(base64)、filename、device_ids。
    仅处理集群心跳中的在线设备；本机直连设备请用 /batch/install-apk。
    """
    from collections import defaultdict

    from ..api import _audit
    from ..multi_host import get_cluster_coordinator

    body = await request.json()
    apk_b64 = body.get("apk_data", "")
    filename = body.get("filename", "app.apk") or "app.apk"
    device_ids_body = body.get("device_ids")
    if not apk_b64:
        raise HTTPException(400, "No APK data")
    if not device_ids_body or not isinstance(device_ids_body, list):
        raise HTTPException(400, "device_ids required (list)")

    wanted = [str(x).strip() for x in device_ids_body if x and str(x).strip()]
    if not wanted:
        raise HTTPException(400, "device_ids empty")

    coord = get_cluster_coordinator()
    all_cluster_devs = coord.get_all_devices()
    by_id = {}
    for d in all_cluster_devs:
        did = d.get("device_id")
        if did and did not in by_id:
            by_id[did] = d

    host_to_ids = defaultdict(list)
    results = {}

    for did in wanted:
        dev = by_id.get(did)
        if not dev:
            results[did] = {
                "success": False,
                "message": "设备不在集群心跳列表中（仅支持 Worker 上报的设备）",
            }
            continue
        st = dev.get("status", "")
        if st not in ("connected", "online"):
            results[did] = {"success": False, "message": f"设备离线 ({st})"}
            continue
        hid = dev.get("host_id", "") or ""
        if not hid:
            results[did] = {"success": False, "message": "缺少 host_id"}
            continue
        host_to_ids[hid].append(did)

    for hid in list(host_to_ids.keys()):
        host_to_ids[hid] = list(dict.fromkeys(host_to_ids[hid]))

    key = os.environ.get("OPENCLAW_API_KEY", "").strip()
    forward_base = {"apk_data": apk_b64, "filename": filename}
    host_summaries = []

    def _host_label(hid, id_list):
        d0 = by_id.get(id_list[0]) if id_list else None
        return (d0 or {}).get("host_name") or ""

    for host_id, ids in host_to_ids.items():
        hname = _host_label(host_id, ids)
        target = _get_worker_url_by_host_id(host_id)
        if not target:
            logger.warning("[cluster_apk] host_id=%s unreachable, devices=%s", (host_id or "")[:16], ids)
            for did in ids:
                results[did] = {"success": False, "message": "Worker 离线或不可达"}
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": 0,
                    "total": len(ids),
                    "error": "Worker 离线或不可达",
                }
            )
            continue
        url = f"http://{target['ip']}:{target['port']}/batch/install-apk"
        body_fwd = dict(forward_base)
        body_fwd["device_ids"] = ids
        try:
            logger.info(
                "[cluster_apk] forward host_id=%s worker_url=%s device_count=%d",
                (host_id or "")[:16],
                url[:120],
                len(ids),
            )
            payload = _json.dumps(body_fwd).encode()
            headers = {"Content-Type": "application/json"}
            if key:
                headers["X-API-Key"] = key
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            raw = _http(req, timeout=600)
            r2 = _json.loads(raw.decode())
            r_res = r2.get("results") or {}
            ok_n = sum(1 for did in ids if (r_res.get(did) or {}).get("success"))
            logger.info(
                "[cluster_apk] host_id=%s worker_ok=%d/%d",
                (host_id or "")[:16],
                ok_n,
                len(ids),
            )
            for did in ids:
                row = r_res.get(did)
                if row:
                    results[did] = row
                else:
                    results[did] = {
                        "success": False,
                        "message": "Worker 未返回该设备结果",
                    }
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": ok_n,
                    "total": len(ids),
                    "worker_url": url[:160],
                }
            )
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            err_short = f"HTTP {e.code} {e.reason}"
            if err_body.strip().startswith("{"):
                try:
                    ej = _json.loads(err_body)
                    if isinstance(ej, dict) and ej.get("detail") is not None:
                        err_short += f": {str(ej['detail'])[:160]}"
                except Exception:
                    err_short += f" {err_body[:120]}"
            else:
                err_short += f" {err_body[:120]}"
            err_short = err_short[:280]
            logger.warning(
                "[cluster_apk] host_id=%s http_error=%s",
                (host_id or "")[:16],
                err_short,
            )
            for did in ids:
                results[did] = {
                    "success": False,
                    "message": f"转发失败: {err_short}",
                }
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": 0,
                    "total": len(ids),
                    "error": err_short,
                    "worker_url": url[:160],
                }
            )
        except Exception as e:
            err = str(e)[:220]
            logger.warning("[cluster_apk] host_id=%s forward_error=%s", (host_id or "")[:16], err)
            for did in ids:
                results[did] = {"success": False, "message": f"转发失败: {err}"}
            host_summaries.append(
                {
                    "host_id": host_id,
                    "host_name": hname,
                    "device_ids": ids,
                    "ok": 0,
                    "total": len(ids),
                    "error": err,
                    "worker_url": url[:160],
                }
            )

    total = len(results)
    success = sum(1 for v in results.values() if v.get("success"))
    try:
        _audit(
            "cluster_batch_install_apk",
            detail=f"filename={filename}, devices={total}, ok={success}",
        )
    except Exception:
        pass
    out = {"total": total, "success": success, "results": results}
    if host_summaries:
        out["hosts"] = host_summaries
    return out


@router.post("/cluster/batch/install-apk")
async def cluster_batch_install_apk(request: Request):
    return await run_cluster_batch_install_apk(request)


@router.post("/cluster/config")
def cluster_set_config(body: dict):
    """Update cluster configuration and optionally restart cluster."""
    from ..multi_host import load_cluster_config, save_cluster_config, auto_start_cluster
    cfg = load_cluster_config()
    cfg.update(body)
    save_cluster_config(cfg)
    auto_start_cluster()
    return {"status": "updated", "config": cfg}


@router.post("/cluster/execute-script",
             dependencies=[Depends(requires_role("admin"))])
def cluster_execute_script(body: dict):
    """Execute a script across all cluster hosts.
    body: {filename, type, target: 'all'|'host:name', variables?}"""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    filename = body.get("filename", "")
    script_type = body.get("type", "adb")
    target = body.get("target", "all")
    variables = body.get("variables", {})

    path = _scripts_dir / filename
    if not path.exists():
        raise HTTPException(404, "Script not found")
    content = path.read_text(encoding="utf-8")

    overview = coord.get_overview()
    hosts = [h for h in overview.get("hosts", []) if h.get("online")]
    results = {}

    for h in hosts:
        if target.startswith("host:") and h.get("host_name") != target[5:]:
            continue
        host_devices = [d for d in coord.get_all_devices()
                        if d.get("host_id") == h.get("host_id")
                        and d.get("status") == "connected"]
        url = f"http://{h['host_ip']}:{h['port']}/scripts/execute"
        try:
            # First upload script to the host
            upload_url = f"http://{h['host_ip']}:{h['port']}/scripts/upload"
            upload_payload = _json.dumps({"filename": filename, "content": content}).encode()
            req = urllib.request.Request(upload_url, data=upload_payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
            _http(req, timeout=10)

            exec_payload = _json.dumps({
                "filename": filename, "type": script_type,
                "device_ids": [d["device_id"] for d in host_devices],
                "variables": variables,
            }).encode()
            req2 = urllib.request.Request(url, data=exec_payload,
                                          headers={"Content-Type": "application/json"}, method="POST")
            r = _json.loads(_http(req2, timeout=60).decode())
            results[h.get("host_name", h["host_ip"])] = {
                "status": "ok", "total": r.get("total", 0), "results": r.get("results", {}),
            }
        except Exception as e:
            results[h.get("host_name", h["host_ip"])] = {"status": "error", "error": str(e)}
    return {"ok": True, "hosts": results}


@router.get("/cluster/stats")
def cluster_stats():
    """Get aggregated cluster performance stats."""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    overview = coord.get_overview()
    all_devs = coord.get_all_devices()
    hosts = overview.get("hosts", [])
    online_hosts = [h for h in hosts if h.get("online")]
    online_devs = [d for d in all_devs if d.get("status") in ("connected", "online")]
    return {
        "total_hosts": len(hosts),
        "online_hosts": len(online_hosts),
        "total_devices": len(all_devs),
        "online_devices": len(online_devs),
        "hosts": [{
            "name": h.get("host_name", h.get("host_ip")),
            "ip": h.get("host_ip"),
            "port": h.get("port", 8000),
            "online": h.get("online", False),
            "devices": len([d for d in all_devs if d.get("host_id") == h.get("host_id")]),
            "online_devices": len([d for d in online_devs if d.get("host_id") == h.get("host_id")]),
            "last_heartbeat": h.get("last_heartbeat"),
        } for h in hosts],
    }


@router.post("/cluster/batch-reconnect")
def cluster_batch_reconnect():
    """Trigger reconnect for all offline devices across all cluster hosts."""
    from ..multi_host import get_cluster_coordinator
    coord = get_cluster_coordinator()
    overview = coord.get_overview()
    hosts = [h for h in overview.get("hosts", []) if h.get("online")]

    results = {}
    for h in hosts:
        url = f"http://{h['host_ip']}:{h.get('port', 8000)}/devices/batch-reconnect"
        try:
            req = urllib.request.Request(
                url, data=b'{}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            body = _json.loads(_http(req, timeout=15))
            results[h.get("host_name", h["host_ip"])] = {
                "status": "ok", "detail": body}
        except Exception as e:
            results[h.get("host_name", h["host_ip"])] = {
                "status": "error", "detail": str(e)}

    return {"hosts_contacted": len(hosts), "results": results}


# ── OTA 远程更新 ──────────────────────────────────────────────

_update_cache = {"path": None, "time": 0}


@router.get("/cluster/update-package")
def cluster_update_package():
    """主控端：打包最新代码供 Worker 下载。"""
    import zipfile
    import io
    import time as _time
    from fastapi.responses import Response as _Resp

    # 5分钟内缓存
    if _update_cache["path"] and _time.time() - _update_cache["time"] < 300:
        cached = Path(_update_cache["path"])
        if cached.exists():
            return _Resp(content=cached.read_bytes(),
                         media_type="application/zip",
                         headers={"Content-Disposition": "attachment; filename=openclaw-update.zip"})

    skip_ext = {'.pyc', '.pyo', '.db', '.log', '.png', '.jpg', '.sqlite', '.sqlite3', '.apk', '.ipa', '.zip'}
    skip_dirs = {'__pycache__', '.git', 'node_modules', 'logs', 'data', 'apk_repo'}

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 项目代码
        proj = _project_root
        for dirpath, dirnames, filenames in os.walk(proj):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fn in filenames:
                if any(fn.endswith(e) for e in skip_ext):
                    continue
                full = os.path.join(dirpath, fn)
                arc = os.path.relpath(full, proj.parent)
                zf.write(full, arc)
                count += 1
        # 守护脚本
        deploy_dir = _project_root.parent / "deploy"
        if deploy_dir.exists():
            for fn in os.listdir(deploy_dir):
                fp = deploy_dir / fn
                if fp.is_file():
                    zf.write(str(fp), f"deploy/{fn}")
                    count += 1

    data = buf.getvalue()
    # 写缓存文件
    cache_path = _project_root / "data" / "openclaw-update.zip"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(data)
    _update_cache["path"] = str(cache_path)
    _update_cache["time"] = _time.time()

    logger.info("[OTA] 生成更新包: %d 文件, %.0f KB", count, len(data) / 1024)
    return _Resp(content=data, media_type="application/zip",
                 headers={"Content-Disposition": "attachment; filename=openclaw-update.zip"})


@router.get("/cluster/update-package/info")
def cluster_update_info():
    """主控端：返回更新包元信息。"""
    import time as _time
    return {
        "version": "1.0.0",
        "timestamp": _time.time(),
        "coordinator": _project_root.name,
    }


@router.post("/cluster/pull-update")
def cluster_pull_update(body: dict = None):
    """Worker 端：从主控拉取最新代码并自动更新（不含 config/）。"""
    import zipfile
    import io
    import time as _time
    import shutil

    body = body or {}
    coordinator_url = body.get("coordinator_url", "")
    if not coordinator_url:
        try:
            from ..multi_host import load_cluster_config
            cfg = load_cluster_config()
            coordinator_url = cfg.get("coordinator_url", "")
        except Exception:
            pass
    if not coordinator_url:
        raise HTTPException(400, "未配置 coordinator_url")

    url = coordinator_url.rstrip("/") + "/cluster/update-package"
    logger.info("[OTA] 从 %s 拉取更新...", url)

    try:
        req = urllib.request.Request(url, method="GET")
        data = _http(req, timeout=60)
    except Exception as e:
        raise HTTPException(502, f"下载更新包失败: {e}")

    # 解压到临时目录
    tmp_dir = _project_root / "data" / "_update_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(tmp_dir)

    # 覆盖文件（不覆盖 config/ 和 data/）
    updated = 0
    src_base = tmp_dir / "mobile-auto-project"
    if not src_base.exists():
        # 如果压缩包根目录就是项目文件
        src_base = tmp_dir

    protect = {'config', 'data', 'logs'}
    for dirpath, dirnames, filenames in os.walk(src_base):
        dirnames[:] = [d for d in dirnames if d not in {'__pycache__', 'data', 'logs'}]
        rel = os.path.relpath(dirpath, src_base)
        top_dir = rel.split(os.sep)[0] if rel != '.' else ''
        if top_dir in protect:
            continue
        dst_dir = _project_root / rel
        dst_dir.mkdir(parents=True, exist_ok=True)
        for fn in filenames:
            src_file = os.path.join(dirpath, fn)
            dst_file = dst_dir / fn
            shutil.copy2(src_file, dst_file)
            # 更新 mtime 为当前时间，确保 Python 重新编译而不是用旧 .pyc
            os.utime(str(dst_file), None)
            updated += 1

    # 复制守护脚本
    deploy_src = tmp_dir / "deploy"
    if deploy_src.exists():
        deploy_dst = _project_root.parent / "deploy" if (_project_root.parent / "deploy").exists() else _project_root.parent
        for fn in os.listdir(deploy_src):
            src_f = deploy_src / fn
            dst_f = (deploy_dst if deploy_dst.name == 'deploy' else deploy_dst) / fn
            if src_f.is_file():
                shutil.copy2(str(src_f), str(dst_f))
                updated += 1

    # 清理临时目录
    shutil.rmtree(tmp_dir, ignore_errors=True)

    # 清理 __pycache__，防止 Python 用旧的 .pyc 覆盖新 .py（时间戳问题）
    pycache_cleared = 0
    for dirpath, dirnames, _ in os.walk(_project_root):
        dirnames[:] = [d for d in dirnames if d not in {'data', 'logs'}]
        if os.path.basename(dirpath) == '__pycache__':
            shutil.rmtree(dirpath, ignore_errors=True)
            pycache_cleared += 1
    if pycache_cleared:
        logger.info("[OTA] 已清理 %d 个 __pycache__ 目录", pycache_cleared)

    logger.info("[OTA] 更新完成: %d 文件已覆盖", updated)

    # 自动重启：优先直接用当前 Python 可执行文件重启，回退到哨兵文件
    auto_restart = body.get("auto_restart", True)
    restarting = False
    if auto_restart and updated > 0:
        sentinel = _project_root / ".restart-required"
        reason = f"OTA更新 {updated} 文件 @ {__import__('datetime').datetime.now().isoformat()}"
        sentinel.write_text(reason, encoding="utf-8")
        restarting = True
        logger.info("[OTA] 已写入哨兵文件，等待 service_wrapper 重启")
        # 直接重启：用当前 sys.executable 启动新进程，再退出自身
        try:
            import sys as _sys, subprocess as _sp, threading as _th, os as _os
            _server_py = str(_project_root / "server.py")
            def _do_restart():
                import time as _t; _t.sleep(1)
                _sp.Popen([_sys.executable, _server_py],
                          creationflags=getattr(_sp, "CREATE_NEW_CONSOLE", 0))
                _os._exit(0)
            _th.Thread(target=_do_restart, daemon=True).start()
        except Exception as _e:
            logger.warning("[OTA] 直接重启失败，依赖哨兵文件: %s", _e)

    return {
        "ok": True,
        "updated_files": updated,
        "size_kb": round(len(data) / 1024),
        "restarting": restarting,
        "message": f"已更新 {updated} 个文件" + ("，正在自动重启..." if restarting else ""),
    }


@router.post("/cluster/push-update-all",
             dependencies=[Depends(requires_role("admin"))])
def push_update_to_all_workers():
    """主控端：向所有在线 Worker 推送更新（服务端中转，避免浏览器 CORS）。"""
    from src.host.multi_host import get_cluster_coordinator
    import socket

    coord = get_cluster_coordinator()
    if not coord:
        raise HTTPException(400, "非集群模式")

    # 获取主控的局域网 IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "192.168.0.118"

    import yaml
    cluster_cfg_path = _project_root / "config" / "cluster.yaml"
    port = DEFAULT_OPENCLAW_PORT
    try:
        with open(cluster_cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        port = cfg.get("local_port", DEFAULT_OPENCLAW_PORT)
    except Exception:
        pass

    coordinator_url = f"http://{local_ip}:{port}"

    overview = coord.get_overview()
    hosts = overview.get("hosts", [])
    results = []
    for h in hosts:
        if not h.get("online"):
            continue
        host_ip = h.get("host_ip", "")
        host_port = h.get("port", 8000)
        host_name = h.get("host_name", h.get("host_id", "?"))
        try:
            url = f"http://{host_ip}:{host_port}/cluster/pull-update"
            data = _json.dumps({"coordinator_url": coordinator_url}).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                                        headers={"Content-Type": "application/json"})
            result = _json.loads(_http(req, timeout=90).decode())
            results.append({
                "host": host_name, "ip": host_ip,
                "ok": True, "updated_files": result.get("updated_files", 0),
            })
            logger.info("[OTA] %s: 更新成功 (%d 文件)", host_name, result.get("updated_files", 0))
        except Exception as e:
            results.append({"host": host_name, "ip": host_ip, "ok": False, "error": str(e)[:100]})
            logger.warning("[OTA] %s: 更新失败: %s", host_name, e)

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = sum(1 for r in results if not r["ok"])
    return {
        "ok": ok_count > 0,
        "results": results,
        "summary": f"{ok_count} 成功, {fail_count} 失败",
    }


@router.post("/cluster/restart",
             dependencies=[Depends(requires_role("admin"))])
def cluster_restart():
    """本机重启：先尝试直接重启进程，fallback 写哨兵文件。"""
    import threading

    sentinel = _project_root / ".restart-required"
    reason = f"手动重启 @ {__import__('datetime').datetime.now().isoformat()}"
    sentinel.write_text(reason, encoding="utf-8")

    # 先启动新进程，再退出当前进程
    def _do_restart():
        import time, subprocess, os, sys
        time.sleep(2)  # 让 HTTP 响应先返回
        logger.info("[重启] 正在启动新进程...")
        python = sys.executable
        script = str(_project_root / "server.py")
        try:
            subprocess.Popen(
                [python, script],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                cwd=str(_project_root),
            )
            logger.info("[重启] 新进程已启动，当前进程即将退出")
            time.sleep(1)
        except Exception as e:
            logger.error("[重启] 启动新进程失败: %s", e)
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    logger.info("[重启] 已触发重启（3秒后执行）")
    return {"ok": True, "message": "重启中，3秒后生效"}


@router.post("/cluster/restart-worker/{host_id}",
             dependencies=[Depends(requires_role("admin"))])
def restart_remote_worker(host_id: str):
    """主控端：远程触发指定 Worker 重启。"""
    from src.host.multi_host import get_cluster_coordinator

    coord = get_cluster_coordinator()
    if not coord:
        raise HTTPException(400, "当前不是 Coordinator")

    target = None
    for hid, info in coord._hosts.items():
        if hid == host_id or getattr(info, "host_name", "") == host_id:
            target = info
            break
    if not target:
        raise HTTPException(404, f"未找到主机: {host_id}")

    host_ip = target.host_ip
    port = target.port or 8000

    # 尝试多个 IP
    urls = []
    if hasattr(target, "all_ips") and target.all_ips:
        for ip in target.all_ips:
            urls.append(f"http://{ip}:{port}/cluster/restart")
    urls.append(f"http://{host_ip}:{port}/cluster/restart")

    for url in urls:
        try:
            req = urllib.request.Request(url, data=b'{}', method="POST",
                                        headers={"Content-Type": "application/json"})
            result = _json.loads(_http(req, timeout=10).decode())
            logger.info("[远程重启] %s: 成功", host_id)
            return {"ok": True, "host_id": host_id, "host_ip": host_ip, "result": result}
        except Exception as e:
            logger.warning("[远程重启] %s 尝试 %s 失败: %s", host_id, url, e)
            continue

    raise HTTPException(502, f"无法连接到 Worker {host_id}")


@router.post("/cluster/download-file", dependencies=[Depends(verify_api_key)])
def cluster_download_file(body: dict):
    """从指定 URL 下载文件到项目目录（仅允许写入 apk_repo/ 和 data/ 目录）。

    Body: {"url": "http://...", "dest": "apk_repo/xxx.apk"}
    """
    import urllib.request as _req
    dest_rel = body.get("dest", "")
    url = body.get("url", "")
    if not url or not dest_rel:
        raise HTTPException(400, "url 和 dest 均为必填")

    # 安全限制：只允许写入 apk_repo/、data/、config/（限 .yaml/.json）
    dest_norm = dest_rel.replace("\\", "/")
    allowed = (
        dest_norm.startswith("apk_repo/") or
        dest_norm.startswith("data/") or
        (dest_norm.startswith("config/") and dest_norm.endswith((".yaml", ".json")))
    )
    if not allowed:
        raise HTTPException(403, "只允许写入 apk_repo/、data/ 或 config/*.yaml 目录")

    dest_path = _project_root / dest_rel.replace("\\", "/")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("[下载文件] %s → %s", url, dest_path)
    try:
        _req.urlretrieve(url, str(dest_path))
    except Exception as e:
        raise HTTPException(502, f"下载失败: {e}")

    size_kb = round(dest_path.stat().st_size / 1024)
    logger.info("[下载文件] 完成: %s (%d KB)", dest_path.name, size_kb)
    return {"ok": True, "dest": str(dest_path), "size_kb": size_kb, "filename": dest_path.name}


# ── Cluster Lock Service endpoints (200 设备跨机锁) ─────────────────────


def _is_coordinator_role() -> bool:
    """仅 coordinator 节点才暴露 lock service. worker 应通过 HTTP 调主控."""
    try:
        cfg_path = config_file("cluster.yaml")
        if not cfg_path.exists():
            return True  # standalone 默认按 coordinator 处理
        import yaml
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(cfg.get("role", "standalone")).lower() in ("coordinator", "standalone")
    except Exception:
        return True


@router.post("/cluster/lock/acquire", dependencies=[Depends(verify_api_key)])
def cluster_lock_acquire(body: dict):
    """申请跨 worker 设备锁.

    Body: {worker_id, device_id, resource="default", priority=50,
           ttl_sec=300, wait_timeout_sec=180}
    Response: {granted: bool, lock_id?, wait_ms, evicted_lock?, reason?}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    worker_id = (body.get("worker_id") or "").strip()
    device_id = (body.get("device_id") or "").strip()
    if not worker_id or not device_id:
        raise HTTPException(400, "worker_id 和 device_id 必填")
    resource = (body.get("resource") or "default").strip()
    priority = int(body.get("priority") or 50)
    ttl_sec = float(body.get("ttl_sec") or 300.0)
    wait_timeout_sec = float(body.get("wait_timeout_sec") or 180.0)

    from src.host.cluster_lock import get_lock_service
    res = get_lock_service().acquire(
        worker_id=worker_id,
        device_id=device_id,
        resource=resource,
        priority=priority,
        ttl_sec=ttl_sec,
        wait_timeout_sec=wait_timeout_sec,
    )
    return {
        "granted": res.granted,
        "lock_id": res.lock_id,
        "wait_ms": round(res.wait_ms, 1),
        "evicted_lock": res.evicted_lock,
        "reason": res.reason,
    }


@router.post("/cluster/lock/heartbeat", dependencies=[Depends(verify_api_key)])
def cluster_lock_heartbeat(body: dict):
    """续 lease.

    Body: {lock_id, extend_ttl_sec?}
    Response: {ok: bool, lock?: {...}}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    lock_id = (body.get("lock_id") or "").strip()
    if not lock_id:
        raise HTTPException(400, "lock_id 必填")
    extend = body.get("extend_ttl_sec")
    extend_f = float(extend) if extend is not None else None

    from src.host.cluster_lock import get_lock_service
    lock = get_lock_service().heartbeat(lock_id, extend_ttl_sec=extend_f)
    if lock is None:
        return {"ok": False, "reason": "lock_not_found_or_expired"}
    return {"ok": True, "lock": lock}


@router.post("/cluster/lock/release", dependencies=[Depends(verify_api_key)])
def cluster_lock_release(body: dict):
    """释放锁.

    Body: {lock_id}
    Response: {ok: bool}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    lock_id = (body.get("lock_id") or "").strip()
    if not lock_id:
        raise HTTPException(400, "lock_id 必填")

    from src.host.cluster_lock import get_lock_service
    ok = get_lock_service().release(lock_id)
    return {"ok": ok}


@router.get("/cluster/locks", dependencies=[Depends(verify_api_key)])
def cluster_lock_list(worker_id: str = "", device_id: str = ""):
    """列出当前 active locks. 可按 worker_id / device_id 过滤."""
    if not _is_coordinator_role():
        raise HTTPException(400, "lock service 仅在 coordinator 节点可用")
    from src.host.cluster_lock import get_lock_service
    svc = get_lock_service()
    locks = svc.list_locks(
        worker_id=worker_id or None,
        device_id=device_id or None,
    )
    return {
        "locks": locks,
        "metrics": svc.metrics(),
    }


# ── L2 中央客户画像 API (worker → 主控 push, 主控统一查询) ─────────────


_safe_get_store_last_err_ts: float = 0.0   # rate-limit error logging

def _safe_get_store():
    """延迟加载 store (PG 不可达时不阻塞 import).

    Phase-13: pool 进入坏状态时 (常见: Chinese Windows PG 返 GBK 错误消息触发
    UnicodeDecodeError) 自动 reset_store + 重试一次.
    """
    global _safe_get_store_last_err_ts
    import time as _t
    from src.host.central_customer_store import get_store, reset_store
    try:
        store = get_store()
        _ensure_ab_auto_graduate_thread()  # Phase-8: 守护线程
        return store
    except Exception as exc:
        now = _t.time()
        _should_log = (now - _safe_get_store_last_err_ts) > 60
        if _should_log:
            logger.warning("[central_store] init/get failed (will reset+retry): %s", exc)
        try:
            reset_store()
            store = get_store()
            _ensure_ab_auto_graduate_thread()
            return store
        except Exception as exc2:
            if _should_log:
                logger.error("[central_store] reset+retry also failed: %s", exc2)
                _safe_get_store_last_err_ts = now
            raise HTTPException(503, f"central store unavailable: {exc2}")


# ── Phase-8: A/B 自动 graduate 守护线程 (coordinator 进程内单例) ──────────
import threading as _phase8_th
_ab_auto_thread_started = False
_ab_auto_thread_lock = _phase8_th.Lock()
_AB_AUTO_CHECK_INTERVAL_SEC = 6 * 3600.0      # 每 6h 检一次
_AB_AUTO_MIN_EXPERIMENT_AGE_SEC = 7 * 86400.0 # 实验跑足 7 天才考虑 graduate
_AB_AUTO_COOLDOWN_SEC = 7 * 86400.0           # graduate 之间至少 7 天


def _ensure_ab_auto_graduate_thread() -> None:
    global _ab_auto_thread_started
    if _ab_auto_thread_started:
        return
    if not _is_coordinator_role():
        return
    with _ab_auto_thread_lock:
        if _ab_auto_thread_started:
            return
        t = _phase8_th.Thread(target=_ab_auto_loop, daemon=True,
                               name="ab-auto-graduate")
        t.start()
        _ab_auto_thread_started = True
        logger.info("[ab_auto_graduate] thread started")


def _ab_auto_loop() -> None:
    """每 6h 检查一次 A/B graduate. 启动 5min 后第一次跑, 避免开机风暴.
    Phase-13: 同时跑 SLA 大批超时 / push 失败率检查 (高频, 5 min 一次).
    """
    import time as _t
    _t.sleep(300)
    last_ab_tick = 0.0
    while True:
        now = _t.time()
        # A/B graduate 6h 一次
        if now - last_ab_tick >= _AB_AUTO_CHECK_INTERVAL_SEC:
            try:
                _ab_auto_tick()
            except Exception:
                logger.exception("[ab_auto_graduate] tick failed")
            last_ab_tick = now
        # Phase-13: SLA + push 失败率检查 5min 一次
        try:
            _ops_health_check_tick()
        except Exception:
            logger.exception("[ops_health_check] tick failed")
        _t.sleep(300)


def _ops_health_check_tick() -> None:
    """Phase-13: 运维健康指标检查, 触发 webhook 通知.
    - SLA 大批超时 (>=5 个 pending handoff 超 30 min) → webhook
    - push 失败率 (push_failure / push_total > 30% 5min 内) → webhook
    """
    if not _is_coordinator_role():
        return
    # SLA 大批超时
    try:
        from src.host.central_customer_store import get_store
        store = get_store()
        pending = store.list_pending_handoffs(limit=200)
        import time as _t
        now_ts = _t.time()
        breach_count = 0
        for h in pending:
            init_at = h.get("initiated_at")
            if not init_at:
                continue
            try:
                age_sec = now_ts - init_at.timestamp()
            except Exception:
                continue
            if age_sec > 1800:  # 30 min
                breach_count += 1
        if breach_count >= 5:
            _send_webhook_notification(
                title="⏰ SLA 大批超时报警",
                markdown=(f"待人工接管 handoff 超 30 min 已达 **{breach_count}** 个\n"
                          f"建议: 立即上线客服或检查接管池"),
                dedup_key=f"sla_breach_bulk:{breach_count // 5}",
            )
    except Exception as exc:
        logger.debug("[ops_health_check] SLA breach check failed: %s", exc)
    # push 失败率
    try:
        from src.host.central_push_client import get_push_metrics
        m = get_push_metrics()
        total = int(m.get("push_total") or 0)
        fail = int(m.get("push_failure") or 0)
        if total >= 50 and (fail / total) > 0.30:
            _send_webhook_notification(
                title="⚠️ push 失败率异常",
                markdown=(f"central push 失败率 **{(fail/total*100):.1f}%** "
                          f"(fail={fail} / total={total})\n"
                          f"建议: 检查 coordinator 网络 / PG 连接"),
                dedup_key="push_fail_rate_high",
            )
    except Exception as exc:
        logger.debug("[ops_health_check] push fail check failed: %s", exc)


def _ab_auto_tick() -> None:
    """单次决策: 实验 7 天 + winner graduated + 距上次 graduate ≥ 7 天 → 自动启新."""
    import time as _t
    from src.host.central_customer_store import get_store
    store = get_store()
    cur = store.get_running_experiment()
    if not cur:
        logger.debug("[ab_auto_graduate] no running experiment")
        return

    started_at = cur.get("started_at")
    if started_at:
        try:
            from datetime import datetime as _dt
            if hasattr(started_at, "timestamp"):
                age_sec = _t.time() - started_at.timestamp()
            else:
                age_sec = _t.time() - _dt.fromisoformat(str(started_at).replace("Z", "+00:00")).timestamp()
        except Exception:
            age_sec = _AB_AUTO_MIN_EXPERIMENT_AGE_SEC + 1  # 解析失败按"够老"处理
    else:
        age_sec = _AB_AUTO_MIN_EXPERIMENT_AGE_SEC + 1
    if age_sec < _AB_AUTO_MIN_EXPERIMENT_AGE_SEC:
        logger.info("[ab_auto_graduate] skip: experiment age %.1fh < 7 days", age_sec / 3600)
        return

    history = store.list_experiments(limit=5)
    archived = [e for e in history if e.get("status") == "archived" and e.get("ended_at")]
    if archived:
        last = archived[0]
        try:
            from datetime import datetime as _dt
            ended = last.get("ended_at")
            if hasattr(ended, "timestamp"):
                cooldown = _t.time() - ended.timestamp()
            else:
                cooldown = _t.time() - _dt.fromisoformat(str(ended).replace("Z", "+00:00")).timestamp()
            if cooldown < _AB_AUTO_COOLDOWN_SEC:
                logger.info("[ab_auto_graduate] skip: last graduate %.1fh ago < cooldown",
                            cooldown / 3600)
                return
        except Exception:
            pass

    winner_state = store.compute_ab_winner(days=30)
    winner = winner_state.get("winner")
    graduated = winner_state.get("graduated")
    if not winner or not graduated:
        logger.info("[ab_auto_graduate] skip: winner=%s graduated=%s", winner, graduated)
        return

    variants = cur.get("variants") or [winner]
    losers = [v for v in variants if v != winner]
    challenger = losers[0] if losers else (winner + "_v2")
    new_name = f"auto_{challenger}_vs_{winner}_{int(_t.time())}"

    store.archive_experiment_with_winner(
        experiment_id=cur["experiment_id"],
        winner=winner,
        samples=winner_state.get("samples", {}),
    )
    new_id = store.start_new_experiment(
        name=new_name, variants=[challenger, winner],
        note=f"auto-graduated from {cur.get('name', '')}, prev winner={winner}",
    )
    logger.warning("[ab_auto_graduate] graduated %s (winner=%s) → new experiment %s (%s)",
                   cur.get("name"), winner, new_id, new_name)
    # SSE 广播 (best-effort)
    try:
        from src.host.lead_mesh.events_stream import emit_event
        emit_event("ab_auto_graduated", {
            "previous_experiment": cur.get("name"),
            "previous_winner": winner,
            "new_experiment_id": new_id,
            "new_experiment_name": new_name,
        })
    except Exception:
        pass
    # Phase-11: webhook 推钉钉/feishu/Slack
    try:
        _send_webhook_notification(
            title="🎓 A/B 实验自动 graduate",
            markdown=(
                f"前实验: **{cur.get('name')}**\n"
                f"赢家: **{winner}** (graduated)\n"
                f"新实验: **{new_name}** ({new_id[:8]}…)\n"
                f"对手: **{challenger}** vs winner"
            ),
        )
    except Exception:
        pass


# Phase-13: webhook 冷却 dict (5min 内同 hash 不重发)
_WEBHOOK_COOLDOWN: dict = {}
_WEBHOOK_COOLDOWN_LOCK = _phase8_th.Lock()
_WEBHOOK_COOLDOWN_SEC = 300.0


def _send_webhook_notification(title: str, markdown: str,
                                 dedup_key: str = "") -> None:
    """Phase-11/13: 通用 webhook 通知 (admin 不盯盘也能看到 graduate / SLA 报警).

    env:
      OPENCLAW_NOTIFY_WEBHOOK = URL
      OPENCLAW_NOTIFY_TYPE    = generic | slack | dingtalk | feishu (default generic)
    无 URL 不发. 失败 catch 不抛.

    Phase-13 dedup_key: 5 分钟内同 key 不重发, 防刷屏 (默认 = title).
    """
    import os as _os
    url = (_os.environ.get("OPENCLAW_NOTIFY_WEBHOOK") or "").strip()
    if not url:
        return
    # Phase-13: 冷却去重
    key = (dedup_key or title).strip()
    if key:
        import time as _t
        now = _t.time()
        with _WEBHOOK_COOLDOWN_LOCK:
            last = _WEBHOOK_COOLDOWN.get(key, 0.0)
            if now - last < _WEBHOOK_COOLDOWN_SEC:
                logger.debug("[notify_webhook] cooldown skip key=%s (last %.0fs ago)",
                             key, now - last)
                return
            _WEBHOOK_COOLDOWN[key] = now
            # 简单 LRU: > 200 条删最旧 50
            if len(_WEBHOOK_COOLDOWN) > 200:
                old = sorted(_WEBHOOK_COOLDOWN.items(), key=lambda kv: kv[1])[:50]
                for k, _v in old:
                    _WEBHOOK_COOLDOWN.pop(k, None)
    notify_type = (_os.environ.get("OPENCLAW_NOTIFY_TYPE") or "generic").strip().lower()
    body: dict
    if notify_type == "dingtalk":
        body = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n{markdown}"},
        }
    elif notify_type == "feishu":
        body = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}},
                "elements": [{"tag": "markdown", "content": markdown}],
            },
        }
    elif notify_type == "slack":
        body = {"text": f"*{title}*\n{markdown}"}
    else:  # generic
        body = {"title": title, "markdown": markdown,
                "text": f"{title}\n\n{markdown}"}
    import json as _j
    import urllib.request as _ureq
    data = _j.dumps(body).encode()
    req = _ureq.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with _ureq.urlopen(req, timeout=8.0):
            pass
        logger.info("[notify_webhook] sent (%s) title=%s", notify_type, title)
    except Exception as exc:
        logger.warning("[notify_webhook] failed: %s", exc)


@router.post("/cluster/customers/upsert", dependencies=[Depends(verify_api_key)])
def cluster_customers_upsert(body: dict):
    """worker push 客户记录 (idempotent by canonical_source+id).

    Body: {canonical_id, canonical_source, customer_id?, primary_name?, age_band?,
           gender?, country?, interests?, ai_profile?, status?, worker_id?, device_id?}
    customer_id 可由 worker 端 UUIDv5 预生成 (compute_customer_id), 不传则主控生成.
    Response: {customer_id}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    canonical_id = (body.get("canonical_id") or "").strip()
    canonical_source = (body.get("canonical_source") or "").strip()
    if not canonical_id or not canonical_source:
        raise HTTPException(400, "canonical_id 和 canonical_source 必填")

    store = _safe_get_store()
    cid = store.upsert_customer(
        canonical_id=canonical_id,
        canonical_source=canonical_source,
        primary_name=body.get("primary_name"),
        age_band=body.get("age_band"),
        gender=body.get("gender"),
        country=body.get("country"),
        interests=body.get("interests"),
        ai_profile=body.get("ai_profile"),
        status=body.get("status"),
        worker_id=body.get("worker_id"),
        device_id=body.get("device_id"),
        customer_id=body.get("customer_id"),
    )
    return {"customer_id": cid}


@router.post("/cluster/customers/{customer_id}/events/push",
             dependencies=[Depends(verify_api_key)])
def cluster_customers_event_push(customer_id: str, body: dict):
    """worker push 业务事件.

    Body: {event_type, worker_id, device_id?, meta?}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    event_type = (body.get("event_type") or "").strip()
    worker_id = (body.get("worker_id") or "").strip()
    if not event_type or not worker_id:
        raise HTTPException(400, "event_type 和 worker_id 必填")

    store = _safe_get_store()
    eid = store.record_event(
        customer_id=customer_id,
        event_type=event_type,
        worker_id=worker_id,
        device_id=body.get("device_id"),
        meta=body.get("meta"),
    )
    return {"event_id": eid}


@router.post("/cluster/customers/{customer_id}/chats/push",
             dependencies=[Depends(verify_api_key)])
def cluster_customers_chat_push(customer_id: str, body: dict):
    """worker push 聊天 msg.

    Body: {channel, direction, content, content_lang?, ai_generated?,
           template_id?, worker_id?, device_id?, meta?}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    channel = (body.get("channel") or "").strip()
    direction = (body.get("direction") or "").strip()
    content = body.get("content") or ""
    if not channel or not direction or not content:
        raise HTTPException(400, "channel / direction / content 必填")

    store = _safe_get_store()
    try:
        cid = store.record_chat(
            customer_id=customer_id, channel=channel, direction=direction,
            content=content,
            content_lang=body.get("content_lang"),
            ai_generated=bool(body.get("ai_generated", False)),
            template_id=body.get("template_id"),
            worker_id=body.get("worker_id"),
            device_id=body.get("device_id"),
            meta=body.get("meta"),
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Phase-3: incoming 消息 → SSE 广播 (不含 content, 只 customer_id + len)
    if direction == "incoming":
        try:
            from src.host.lead_mesh.events_stream import emit_event
            # 顺手查 primary_name (用于通知显示) — 失败不阻塞
            peer_name = ""
            try:
                with store._cursor() as cur:
                    cur.execute("SELECT primary_name FROM customers WHERE customer_id = %s",
                                (customer_id,))
                    row = cur.fetchone()
                    if row:
                        peer_name = row.get("primary_name") or ""
            except Exception:
                pass
            emit_event("chat_inbound", {
                "customer_id": customer_id,
                "peer_name": peer_name,
                "channel": channel,
                "content_len": len(content),
                "content_lang": body.get("content_lang") or "",
            })
        except Exception:
            pass

    return {"chat_id": cid}


@router.post("/cluster/customers/{customer_id}/handoff/initiate",
             dependencies=[Depends(verify_api_key)])
def cluster_customers_handoff_initiate(customer_id: str, body: dict):
    """worker 引流 handoff (例: messenger → line, 等人工接管).

    Body: {from_stage, to_stage, initiating_worker_id, initiating_device_id?,
           ai_summary?, meta?}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    from_stage = (body.get("from_stage") or "").strip()
    to_stage = (body.get("to_stage") or "").strip()
    iw = (body.get("initiating_worker_id") or "").strip()
    if not from_stage or not to_stage or not iw:
        raise HTTPException(400, "from_stage / to_stage / initiating_worker_id 必填")

    store = _safe_get_store()
    hid = store.initiate_handoff(
        customer_id=customer_id,
        from_stage=from_stage, to_stage=to_stage,
        initiating_worker_id=iw,
        initiating_device_id=body.get("initiating_device_id"),
        ai_summary=body.get("ai_summary"),
        meta=body.get("meta"),
    )
    return {"handoff_id": hid}


@router.post("/cluster/customers/handoff/{handoff_id}/accept",
             dependencies=[Depends(verify_api_key)])
def cluster_handoff_accept(handoff_id: str, body: dict):
    """L3 人工接管. body: {accepted_by_human}.

    Idempotent: 已接管返回 {accepted: false} 表已被别人抢.
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    abh = (body.get("accepted_by_human") or "").strip()
    if not abh:
        raise HTTPException(400, "accepted_by_human 必填")

    store = _safe_get_store()
    ok = store.accept_handoff(handoff_id=handoff_id, accepted_by_human=abh)
    return {"accepted": ok}


@router.post("/cluster/customers/handoff/{handoff_id}/complete",
             dependencies=[Depends(verify_api_key)])
def cluster_handoff_complete(handoff_id: str, body: dict):
    """完成 handoff. body: {outcome: 'converted'|'lost'|'timeout'}."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    outcome = (body.get("outcome") or "").strip()
    if not outcome:
        raise HTTPException(400, "outcome 必填")
    store = _safe_get_store()
    try:
        ok = store.complete_handoff(handoff_id=handoff_id, outcome=outcome)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"completed": ok}


@router.get("/cluster/customers", dependencies=[Depends(verify_api_key)])
def cluster_customers_list(
    status: str = "",
    country: str = "",
    worker_id: str = "",
    limit: int = 100,
    offset: int = 0,
):
    """L3 dashboard / 业务工具列表."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {
        "customers": store.list_customers(
            status=status or None,
            country=country or None,
            worker_id=worker_id or None,
            limit=min(max(1, limit), 500),
            offset=max(0, offset),
        ),
    }


@router.get("/cluster/customers/{customer_id}",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_detail(customer_id: str,
                             include_events: int = 50,
                             include_chats: int = 100):
    """L3 客户详情 (画像 + 事件 + 聊天 + handoffs)."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    cust = store.get_customer(
        customer_id=customer_id,
        include_events=min(max(0, include_events), 1000),
        include_chats=min(max(0, include_chats), 1000),
    )
    if not cust:
        raise HTTPException(404, "customer not found")
    return cust


# Phase-8: LLM 客户洞察 — 内存缓存 (customer_id, last_chat_id) → result, 1h TTL
_llm_insight_cache: dict = {}
_llm_insight_lock = __import__("threading").Lock()
_LLM_INSIGHT_TTL_SEC = 3600.0


def _build_llm_insight(customer: dict) -> dict:
    """Phase-8: 调 LLM 出客户洞察 (urgent / concerns / action / readiness / summary)."""
    chats = customer.get("chats") or []
    if not chats:
        return {"ok": False, "error": "no_chat_history",
                "summary": "尚无聊天记录, 不能给洞察",
                "urgent_signal": False, "key_concerns": [],
                "recommended_action": "等客户先回复一两轮", "conversion_readiness": 0.0}

    # 取最近 50 条 (chats 已按 ts ASC, 取 tail)
    recent = chats[-50:]
    persona = (customer.get("ai_profile") or {}).get("persona_key", "")
    primary_name = customer.get("primary_name") or "客户"
    chat_lines = []
    for ch in recent:
        who = "客户" if ch.get("direction") == "incoming" else "Bot"
        content = (ch.get("content") or "").replace("\n", " ").strip()
        if not content:
            continue
        chat_lines.append(f"{who}: {content[:200]}")
    history = "\n".join(chat_lines[-30:])  # LLM 上下文限制, 取最近 30 条

    system = (
        "你是日本中年女性情感陪伴 bot 的运营顾问. "
        "请基于聊天记录给出 JSON 格式的客户洞察, 不要任何 markdown / 解释, "
        "只输出 JSON 对象."
    )
    user = f"""客户名: {primary_name}
预设 persona: {persona}

聊天记录 (按时间从早到晚):
{history}

请按以下格式返回 JSON (全部字段必填):
{{
  "urgent_signal": <true|false>,
  "key_concerns": ["客户主要诉求 1", "诉求 2", "..."],
  "recommended_action": "<给客服一句话建议>",
  "conversion_readiness": <0.0-1.0 的小数, 引流到 LINE 的成熟度>,
  "summary": "<两到三句话总结这个客户>"
}}"""

    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
        text = client.chat_with_system(system, user, temperature=0.4, max_tokens=600)
        # 容错解析: LLM 可能裹 ```json ``` 或前后带空白
        import json as _j
        import re as _re
        cleaned = (text or "").strip()
        m = _re.search(r"\{.*\}", cleaned, _re.DOTALL)
        if not m:
            return {"ok": False, "error": "llm_no_json",
                    "raw": cleaned[:300], "summary": "LLM 未返回 JSON",
                    "urgent_signal": False, "key_concerns": [],
                    "recommended_action": "需要人工查看", "conversion_readiness": 0.5}
        data = _j.loads(m.group(0))
        return {
            "ok": True,
            "urgent_signal": bool(data.get("urgent_signal")),
            "key_concerns": list(data.get("key_concerns") or []),
            "recommended_action": str(data.get("recommended_action") or ""),
            "conversion_readiness": float(data.get("conversion_readiness") or 0.5),
            "summary": str(data.get("summary") or ""),
        }
    except Exception as exc:
        logger.warning("[llm_insight] failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200],
                "summary": f"LLM 调用失败: {exc}",
                "urgent_signal": False, "key_concerns": [],
                "recommended_action": "LLM 离线, 请人工查看", "conversion_readiness": 0.5}


@router.get("/cluster/customers/{customer_id}/llm-insight",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_llm_insight(customer_id: str, force: int = 0):
    """Phase-8: LLM 客户洞察 (urgent_signal / key_concerns / recommended_action /
    conversion_readiness / summary). 缓存 1h, force=1 强制重算."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    cust = store.get_customer(customer_id=customer_id, include_events=0, include_chats=50)
    if not cust:
        raise HTTPException(404, "customer not found")
    chats = cust.get("chats") or []
    last_chat_id = chats[-1].get("chat_id") if chats else "no_chat"
    cache_key = f"{customer_id}:{last_chat_id}"
    import time as _t
    now = _t.time()
    if not force:
        with _llm_insight_lock:
            rec = _llm_insight_cache.get(cache_key)
            if rec and (now - rec["fetched_at"]) < _LLM_INSIGHT_TTL_SEC:
                out = dict(rec["data"])
                out["cached"] = True
                return out
    data = _build_llm_insight(cust)
    with _llm_insight_lock:
        _llm_insight_cache[cache_key] = {"data": data, "fetched_at": now}
        # 简单 LRU: 超过 500 条删最旧的 100 条
        if len(_llm_insight_cache) > 500:
            for k in sorted(_llm_insight_cache.items(), key=lambda kv: kv[1]["fetched_at"])[:100]:
                _llm_insight_cache.pop(k[0], None)
    out = dict(data)
    out["cached"] = False
    return out


@router.get("/cluster/referral-decisions",
            dependencies=[Depends(verify_api_key)])
def cluster_referral_decisions_list(level: str = "",
                                     refer: str = "",
                                     reason: str = "",
                                     days: int = 30,
                                     limit: int = 50):
    """Phase-12: referral_decision 详情列表 (配 aggregate 看板钻取).

    Query: level (hard_block|hard_allow|soft_pass|soft_fail) / refer (true|false) /
           reason (子串过滤) / days / limit
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    refer_bool = None  # Optional[bool]
    rstr = (refer or "").strip().lower()
    if rstr == "true":
        refer_bool = True
    elif rstr == "false":
        refer_bool = False
    store = _safe_get_store()
    return {"decisions": store.list_referral_decisions(
        level=(level or "").strip(),
        refer=refer_bool,
        reason=(reason or "").strip(),
        days=days,
        limit=limit,
    )}


@router.get("/cluster/referral-decisions/aggregate",
            dependencies=[Depends(verify_api_key)])
def cluster_referral_decisions_aggregate(days: int = 30):
    """Phase-11: referral_decision 事件聚合 (admin 调 gate 阈值看板)."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return store.referral_decisions_aggregate(days=days)


@router.get("/cluster/customers/top/high-priority",
            dependencies=[Depends(verify_api_key)])
def cluster_top_high_priority(limit: int = 10):
    """Phase-10: 高 priority 客户 Top N (运营主动出击)."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"customers": store.list_top_high_priority(limit=limit)}


@router.get("/cluster/customers/top/frustrated",
            dependencies=[Depends(verify_api_key)])
def cluster_top_frustrated(days: int = 7, limit: int = 10):
    """Phase-10: 高 frustration 客户 Top N (运营主动安抚)."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"customers": store.list_top_frustrated(days=days, limit=limit)}


@router.get("/cluster/customers/handoff/pending",
            dependencies=[Depends(verify_api_key)])
def cluster_handoff_pending(limit: int = 100):
    """L3 人工接管面板: 待接管 handoff 列表."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {
        "handoffs": store.list_pending_handoffs(limit=min(max(1, limit), 500)),
    }


@router.get("/cluster/customers/funnel/stats",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_funnel(days: int = 7):
    """L3 dashboard 漏斗统计."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return store.funnel_stats(days=min(max(1, days), 90))


@router.get("/cluster/customers-export.csv",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_export_csv(limit: int = 1000):
    """Phase-4: 导出客户 CSV 给运营 / 财务做报表."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    import csv
    import io
    from fastapi.responses import StreamingResponse
    store = _safe_get_store()
    customers = store.list_customers(limit=min(max(1, limit), 5000))
    buf = io.StringIO()
    # Excel BOM, 让中文正确
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow([
        "customer_id", "primary_name", "status", "priority_tag",
        "ab_variant", "country", "last_worker_id", "last_device_id",
        "created_at", "updated_at", "interests",
    ])
    for c in customers:
        ai_profile = c.get("ai_profile") or {}
        writer.writerow([
            c.get("customer_id", ""),
            c.get("primary_name", ""),
            c.get("status", ""),
            c.get("priority_tag", ""),
            ai_profile.get("ab_variant", ""),
            c.get("country", ""),
            c.get("last_worker_id", ""),
            c.get("last_device_id", ""),
            str(c.get("created_at", "")),
            str(c.get("updated_at", "")),
            ", ".join(c.get("interests") or []),
        ])
    csv_str = buf.getvalue()
    filename = f"customers_{int(_time_c.time())}.csv"
    return StreamingResponse(
        iter([csv_str.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/cluster/customers/funnel/timeseries",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_funnel_timeseries(days: int = 30):
    """Phase-3: 历史漏斗时序图 — 按天统计 events / outcomes."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"days": days, "series": store.funnel_timeseries(days=min(max(1, days), 365))}


@router.post("/cluster/customers/priority/recompute",
             dependencies=[Depends(verify_api_key)])
def cluster_customers_priority_recompute():
    """Phase-3: 重算所有客户 priority_tag (启发式: status 映射)."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return store.recompute_priority_tags()


@router.put("/cluster/customers/{customer_id}/tags",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_add_tag(customer_id: str, body: dict):
    """Phase-6: 加自定义标签. Body: {tag: str}."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    tag = (body.get("tag") or "").strip()
    if not tag:
        raise HTTPException(400, "tag 必填")
    store = _safe_get_store()
    return {"ok": store.add_custom_tag(customer_id, tag), "tag": tag}


@router.delete("/cluster/customers/{customer_id}/tags/{tag}",
               dependencies=[Depends(verify_api_key)])
def cluster_customers_remove_tag(customer_id: str, tag: str):
    """Phase-6: 删自定义标签."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"ok": store.remove_custom_tag(customer_id, tag), "tag": tag}


@router.post("/cluster/customers/{customer_id}/priority",
             dependencies=[Depends(verify_api_key)])
def cluster_customers_update_priority(customer_id: str, body: dict):
    """Phase-4: 实时更新单个客户 priority_tag.

    Body: {priority_tag: high|medium|low}
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    tag = (body.get("priority_tag") or "").strip().lower()
    if tag not in ("high", "medium", "low"):
        raise HTTPException(400, "priority_tag 必须是 high|medium|low")
    store = _safe_get_store()
    ok = store.update_priority(customer_id, tag)
    return {"customer_id": customer_id, "priority_tag": tag, "updated": ok}


@router.get("/cluster/customers/sla/agents",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_sla_agents(days: int = 30):
    """Phase-2: 主管 SLA 看板 — 按客服 username 统计接管 / 转化 / 平均处理时长."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"days": days, "agents": store.agent_sla_stats(days=min(max(1, days), 365))}


@router.get("/cluster/chats-search",
            dependencies=[Depends(verify_api_key)])
def cluster_chats_search(q: str = "", limit: int = 50):
    """Phase-6: 全文搜聊天内容."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"q": q, "chats": store.search_chats(q=q, limit=limit)}


@router.get("/cluster/customers-search",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_search(q: str = "", priority: str = "", status: str = "",
                              ab_variant: str = "", limit: int = 50):
    """Phase-5: 客户搜索 / 多维过滤."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"customers": store.search_customers(
        q=q, priority=priority, status=status, ab_variant=ab_variant,
        limit=limit,
    )}


@router.get("/cluster/ab/experiment/running",
            dependencies=[Depends(verify_api_key)])
def cluster_ab_running():
    """Phase-7: 返当前 running A/B 实验."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return store.get_running_experiment() or {}


@router.get("/cluster/ab/experiments",
            dependencies=[Depends(verify_api_key)])
def cluster_ab_list(limit: int = 20):
    """Phase-7: 列所有 A/B 实验历史."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"experiments": store.list_experiments(limit=limit)}


@router.post("/cluster/ab/graduate-and-restart",
             dependencies=[Depends(requires_role("admin"))])
def cluster_ab_graduate_restart(body: dict):
    """Phase-7: 手动 graduate 当前实验 + 启新实验.

    Body: {new_name, new_variants: list, note?}
    自动用当前实验的 winner 作为新实验的对照基线.
    """
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    current = store.get_running_experiment()
    if current:
        winner_state = store.compute_ab_winner()
        if winner_state.get("winner"):
            store.archive_experiment_with_winner(
                experiment_id=current["experiment_id"],
                winner=winner_state["winner"],
                samples=winner_state.get("samples", {}),
            )
    new_id = store.start_new_experiment(
        name=(body.get("new_name") or "").strip() or "auto_restart",
        variants=body.get("new_variants") or ["v1", "v2"],
        note=body.get("note") or "",
    )
    return {"new_experiment_id": new_id, "previous_winner": (current or {}).get("winner")}


@router.get("/cluster/customer-views",
            dependencies=[Depends(verify_api_key)])
def cluster_views_list(owner: str = ""):
    """Phase-7: 列保存的客户视图."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"views": store.list_views(owner=owner or None)}


@router.post("/cluster/customer-views",
             dependencies=[Depends(verify_api_key)])
def cluster_views_save(body: dict):
    """Phase-7: 保存客户视图. Body: {name, params, owner?}."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name 必填")
    store = _safe_get_store()
    vid = store.save_view(
        name=name,
        owner=(body.get("owner") or "admin").strip(),
        params=body.get("params") or {},
    )
    return {"view_id": vid, "name": name}


@router.delete("/cluster/customer-views/{view_id}",
               dependencies=[Depends(verify_api_key)])
def cluster_views_delete(view_id: str, owner: str = ""):
    """Phase-7: 删客户视图."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"deleted": store.delete_view(view_id, owner=owner or None)}


@router.get("/cluster/customers/ab/winner",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_ab_winner(days: int = 30):
    """Phase-5: A/B 实验 winner 自动判定."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return store.compute_ab_winner(days=min(max(1, days), 365))


@router.get("/cluster/customers/sla/variants",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_sla_variants(days: int = 30):
    """Phase-4: 按 ab_variant 切片转化率, 给主管做 A/B 决策."""
    if not _is_coordinator_role():
        raise HTTPException(400, "central store 仅在 coordinator 节点可用")
    store = _safe_get_store()
    return {"days": days, "variants": store.variant_sla_stats(days=min(max(1, days), 365))}


@router.get("/cluster/customers/push/metrics",
            dependencies=[Depends(verify_api_key)])
def cluster_customers_push_metrics():
    """worker 侧 push counter + retry queue 状态 + drain thread 状态.

    每个 worker 节点都暴露自己的 metrics (per-process). coordinator 想要
    集群级聚合可以遍历各 worker 拉.
    """
    try:
        from src.host.central_push_client import get_push_metrics
        from src.host.central_push_drain import get_drain_status
        return {
            "metrics": get_push_metrics(),
            "drain": get_drain_status(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("[push_metrics] %s", exc)
        raise HTTPException(500, str(exc))
