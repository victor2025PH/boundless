# -*- coding: utf-8 -*-
"""主控（coordinator）本地无 USB 时，将 ``/devices/{device_id}/...`` 转发到实际挂载设备的 Worker。

TikTok device-grid 会展示仅在 Worker 在线的设备；若请求只走本机 ``DeviceManager``，会得到
``404 设备不存在``。本模块根据 ``device_aliases`` 中的 ``host_name`` 与 ``cluster_state.json``
中的节点匹配，必要时对在线 Worker 做轻量探测，再把 REST 请求透明转到对应节点。
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import quote, unquote
from urllib.request import Request as UrlRequest, urlopen

from starlette.requests import Request
from starlette.responses import Response

from src.host.device_registry import DEFAULT_DEVICES_YAML, config_file

logger = logging.getLogger(__name__)

_devices_yaml = DEFAULT_DEVICES_YAML
_aliases_path = config_file("device_aliases.json")
_cluster_state_path = config_file("cluster_state.json")
_cluster_yaml = config_file("cluster.yaml")

# 正缓存：device_id -> (worker_base, ts)
_worker_base_cache: dict[str, tuple[str, float]] = {}
# 负缓存：近期探测失败，避免每次请求打满 Worker
_negative_cache: dict[str, float] = {}
_CACHE_POS_TTL = 90.0
_CACHE_NEG_TTL = 12.0

# 环境变量可覆盖：OPENCLAW_WORKER_BASES=http://192.168.0.103:8000,http://...
# 2026-05-04: 删除硬编码 192.168.0.103 fallback (W03 实际 IP 已变 .101).
# IP 自适应路径: HeartbeatSender._collect_status 每次心跳用 _get_local_ip()
# 实时取出口 IP, 主控写 cluster_state.json. _list_worker_bases() 从
# cluster_state.json 读最新 host_ip (line 88-104). worker IP 变后下次心跳
# (10s 间隔) 自动同步. 这里 fallback 只在心跳未开始 (主控刚启动 < 10s)
# 或 OPENCLAW_WORKER_BASES env 显式指定时用.
_EXTRA_BASES = [
    b.strip().rstrip("/")
    for b in (os.environ.get("OPENCLAW_WORKER_BASES", "") or "").split(",")
    if b.strip()
]


def _is_coordinator_host() -> bool:
    """仅在集群主控上启用转发，避免 Worker 节点误把请求转到其它机器。"""
    try:
        if not _cluster_yaml.exists():
            return True
        import yaml
        with open(_cluster_yaml, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return str(cfg.get("role", "standalone")).lower() == "coordinator"
    except Exception:
        return True


def _device_on_local_manager(device_id: str) -> bool:
    try:
        from src.device_control.device_manager import get_device_manager
        from src.host.executor import _resolve_serial_from_config

        mgr = get_device_manager(_devices_yaml)
        if mgr.get_device_info(device_id):
            return True
        serial = _resolve_serial_from_config(_devices_yaml, device_id)
        return bool(mgr.get_device_info(serial))
    except Exception:
        return False


def list_worker_api_bases() -> list[str]:
    """供探针等模块复用：集群内 Worker 的 API 根 URL 列表。"""
    return _list_worker_bases()


def _list_worker_bases() -> list[str]:
    """候选 Worker API 根 URL（去重，集群心跳优先）。"""
    seen: set[str] = set()
    out: list[str] = []
    now = time.time()
    try:
        if _cluster_state_path.exists():
            with open(_cluster_state_path, encoding="utf-8") as f:
                st = json.load(f) or {}
            for _k, h in st.items():
                if not isinstance(h, dict):
                    continue
                hb = float(h.get("last_heartbeat") or 0)
                if now - hb > 180:
                    continue
                ip = h.get("host_ip")
                if not ip:
                    continue
                port = int(h.get("port") or 8000)
                base = f"http://{ip}:{port}"
                if base not in seen:
                    seen.add(base)
                    out.append(base)
    except Exception as e:
        logger.debug("读取 cluster_state 失败: %s", e)
    for b in _EXTRA_BASES:
        if b not in seen:
            seen.add(b)
            out.append(b)
    return out


def _api_headers_for_probe() -> dict[str, str]:
    h: dict[str, str] = {}
    key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
    if key:
        h["X-API-Key"] = key
    return h


def _probe_worker_has_device(base: str, device_id: str) -> bool:
    """在该 Worker 上探测 ``GET /devices/{id}/status`` 是否可用（404=不在此机）。"""
    safe = quote(device_id, safe="")
    url = f"{base.rstrip('/')}/devices/{safe}/status"
    req = UrlRequest(url, method="GET", headers=_api_headers_for_probe())
    try:
        urlopen(req, timeout=3.0)
        return True
    except HTTPError as e:
        if e.code == 404:
            return False
        logger.debug("Worker 探测 HTTP %s %s", base, e.code)
        return False
    except Exception as e:
        logger.debug("Worker 探测失败 %s: %s", base, e)
        return False


def _resolve_base_from_alias(device_id: str) -> Optional[str]:
    """根据 ``device_aliases.json`` 的 host_name 与 cluster_state 匹配 Worker 地址。"""
    try:
        if not _aliases_path.exists():
            return None
        with open(_aliases_path, encoding="utf-8") as f:
            aliases = json.load(f) or {}
        info = aliases.get(device_id) or {}
        hn = (info.get("host_name") or "").strip().lower()
        if not hn or hn in ("主控", "coordinator", "local"):
            return None
        if not _cluster_state_path.exists():
            return None
        with open(_cluster_state_path, encoding="utf-8") as f:
            st = json.load(f) or {}
        for _k, h in (st or {}).items():
            if not isinstance(h, dict):
                continue
            wn = (h.get("host_name") or "").strip().lower()
            if wn and wn == hn:
                ip = h.get("host_ip")
                if not ip:
                    continue
                port = int(h.get("port") or 8000)
                return f"http://{ip}:{port}"
    except Exception as e:
        logger.debug("alias 匹配 Worker 失败: %s", e)
    return None


def resolve_worker_base_for_device(device_id: str) -> Optional[str]:
    """若设备不在主控本机 USB 上，返回其所在 Worker 的 API 根 URL；否则返回 None。"""
    if _device_on_local_manager(device_id):
        return None
    now = time.time()
    hit = _worker_base_cache.get(device_id)
    if hit and now - hit[1] < _CACHE_POS_TTL:
        return hit[0]
    neg_ts = _negative_cache.get(device_id)
    if neg_ts and now - neg_ts < _CACHE_NEG_TTL:
        return None

    base = _resolve_base_from_alias(device_id)
    if base and _probe_worker_has_device(base, device_id):
        _worker_base_cache[device_id] = (base, now)
        _negative_cache.pop(device_id, None)
        logger.info("设备 %s 转发目标(别名): %s", device_id[:16], base)
        return base
    for b in _list_worker_bases():
        if base and b == base:
            continue
        if _probe_worker_has_device(b, device_id):
            _worker_base_cache[device_id] = (b, now)
            _negative_cache.pop(device_id, None)
            logger.info("设备 %s 转发目标(探测): %s", device_id[:16], b)
            return b
    _negative_cache[device_id] = now
    return None


# 第二段路径为这些前缀时，不是 ``/devices/{device_id}/...`` 设备子资源（勿转发）
_DEVICES_PATH_STATIC_SEG2 = frozenset({
    "aliases", "tags", "batch-delete", "cleanup", "rescan", "conflicts", "registry",
    "health-scores", "install-helper", "usb-diagnostics", "recovery-timeline",
    "health-trends", "recovery-status", "scheduling-scores", "recovery-stats",
    "predictive-health", "account-health-alerts", "isolated", "reconnection-status",
    "performance", "wallpaper", "group", "refresh-all", "auto-number", "batch-number",
    "batch-reconnect", "self-fix-conflicts", "fix-conflicts", "auto-assign-segments",
    "renumber-all", "anomaly",
})


def _normalize_worker_response_body(
    data: bytes, headers: dict[str, str]
) -> tuple[bytes, dict[str, str]]:
    """解压 gzip 体并去掉压缩相关头。

    Worker 常经 GZipMiddleware 返回 ``Content-Encoding: gzip``。若主控转发时用 urllib
    读到**仍为压缩**的字节，却又去掉 ``Content-Encoding``，浏览器会把二进制当 JSON，
    前端 ``r.json()`` 报 ``Unexpected token``。此处统一解压为明文再回传。
    """
    h: dict[str, str] = {str(k): str(v) for k, v in headers.items()}
    for name in list(h.keys()):
        if name.lower() in ("content-encoding", "content-length", "transfer-encoding"):
            del h[name]
    if data and len(data) >= 2 and data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as e:
            logger.warning("Worker 响应 gzip 解压失败: %s", e)
    return data, h


async def _forward_to_worker(base: str, request: Request) -> Response:
    target = base.rstrip("/") + request.url.path
    if request.url.query:
        target += "?" + str(request.url.query)
    body = await request.body()
    hdrs: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in ("host", "connection", "content-length", "accept-encoding"):
            continue
        hdrs[k] = v
    # 避免向 Worker 转发浏览器的 gzip/br，防止 urllib 与响应头组合导致「压缩体 + 无 Content-Encoding」
    hdrs["Accept-Encoding"] = "identity"
    method = request.method.upper()
    payload = None
    if body and method not in ("GET", "HEAD", "OPTIONS"):
        payload = body

    def _sync_forward() -> tuple[int, bytes, dict[str, str]]:
        req = UrlRequest(target, data=payload, method=method, headers=hdrs)
        try:
            resp = urlopen(req, timeout=120)
            code = resp.getcode()
            data = resp.read()
            hdict = {k: v for k, v in resp.headers.items()}
            return code, data, hdict
        except HTTPError as e:
            try:
                raw = e.read() or b""
            except Exception:
                raw = b""
            hdict = dict(e.headers.items()) if e.headers else {}
            return e.code, raw, hdict

    code, data, hdrs_out = await asyncio.to_thread(_sync_forward)
    data, hdrs_out = _normalize_worker_response_body(data, hdrs_out)
    try:
        if code == 404 and "contacts/enriched" in request.url.path:
            logger.warning(
                "Worker 对增强通讯录返回 404（请确认节点已部署同版本 /devices/.../contacts/enriched）: %s",
                target[:180],
            )
    except Exception:
        pass
    out_h: dict[str, str] = {}
    for k, v in hdrs_out.items():
        kl = k.lower()
        if kl in ("transfer-encoding", "connection", "content-encoding", "content-length"):
            continue
        out_h[k] = v
    return Response(content=data, status_code=code, headers=out_h)


def install_coordinator_device_proxy(app) -> None:
    """注册中间件：主控上非本机设备请求转发到 Worker。"""

    @app.middleware("http")
    async def coordinator_remote_device_proxy(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        if not _is_coordinator_host():
            return await call_next(request)
        path = request.url.path
        if not path.startswith("/devices"):
            return await call_next(request)
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2 or parts[0] != "devices":
            return await call_next(request)
        seg2 = unquote(parts[1])
        if seg2 in _DEVICES_PATH_STATIC_SEG2:
            return await call_next(request)
        if _device_on_local_manager(seg2):
            return await call_next(request)
        base = resolve_worker_base_for_device(seg2)
        if not base:
            return await call_next(request)
        try:
            return await _forward_to_worker(base, request)
        except Exception as e:
            logger.warning("转发 Worker 失败 device=%s: %s", seg2[:16], e)
            return await call_next(request)
