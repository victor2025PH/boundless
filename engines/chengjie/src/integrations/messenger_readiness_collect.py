# -*- coding: utf-8 -*-
"""Messenger 就绪度**进程内**采集器（P2 2026-08-13）。

与 ``tools/diagnose_messenger.py`` 的分工：CLI 的采集面向「服务宕着也能跑」
（sqlite 直读 + 磁盘配置），本模块面向**活进程**（注册表单例 + 会话健康单例 +
sidecar HTTP 探针），两者共享 ``messenger_readiness.evaluate_messenger_readiness``
同一判定纯函数——消费方（/api/workspace/metrics、watchdog 对账）零口径分裂。

探针纪律：60s TTL 模块级缓存 + 3s 超时（与 audio_probe_target 同哲学）——
metrics 轮询与 watchdog tick 共享同一次探针，sidecar 零压力。

保守边界：/health 通但 /accounts 拉取失败 → 按 sidecar 不可达对待（宁可说
「sidecar 有问题」也不虚构「账号全丢了」——后者会喂给 not_restored 告警）。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from src.integrations.messenger_readiness import (
    evaluate_messenger_readiness,
    extract_config_gates,
)

_PROBE_TIMEOUT_SEC = 3.0
_CACHE_TTL_SEC = 60.0

_lock = threading.Lock()
_cache: Dict[str, Any] = {"ts": 0.0, "snap": None}


def _http_get_json(url: str, timeout: float = _PROBE_TIMEOUT_SEC) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _probe_sidecar(web_url: str,
                   http_get: Callable[[str], Dict[str, Any]]) -> Dict[str, Any]:
    base = str(web_url or "").rstrip("/")
    out: Dict[str, Any] = {"url": base, "reachable": False, "accounts": []}
    if not base:
        return out
    try:
        h = http_get(f"{base}/health")
        if not h.get("ok"):
            return out
    except Exception as exc:
        out["error"] = str(exc)[:200]
        return out
    try:
        data = http_get(f"{base}/accounts")
        out["accounts"] = list(data.get("accounts") or [])
        out["reachable"] = True
    except Exception as exc:
        # /health 通但清单拉不到：按不可达处理（见模块 docstring 保守边界）
        out["error"] = f"/accounts: {str(exc)[:180]}"
    return out


def _registry_messenger_rows() -> List[Dict[str, Any]]:
    try:
        from src.integrations.account_registry import get_account_registry
        rows = get_account_registry().list() or []
    except Exception:
        return []
    out = []
    for r in rows:
        if str(r.get("platform") or "") != "messenger":
            continue
        out.append({
            "account_id": str(r.get("account_id") or ""),
            "status": str(r.get("status") or ""),
            "label": str(r.get("label") or ""),
        })
    return out


def _session_registry_snapshot() -> Optional[Dict[str, Any]]:
    try:
        from src.integrations.platform_session_health import (
            ensure_seeded_from_registry,
            get_platform_session_health,
        )
        ensure_seeded_from_registry()
        d = get_platform_session_health().dump()
        return {"sessions": d.get("sessions") or {},
                "inbox_health": d.get("inbox_health") or {}}
    except Exception:
        return None


def collect_messenger_readiness(
    cfg: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    ttl_sec: float = _CACHE_TTL_SEC,
    http_get: Optional[Callable[[str], Dict[str, Any]]] = None,
    registry_rows: Optional[List[Dict[str, Any]]] = None,
    session_registry: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """采集 + 判定（带 TTL 缓存）。``http_get``/``registry_rows``/
    ``session_registry`` 可注入（测试零网络零单例）；注入任一即绕过缓存
    （测试语义），生产调用全走缓存。
    """
    ts = time.time() if now is None else float(now)
    injected = (http_get is not None or registry_rows is not None
                or session_registry is not None)
    if not injected and ttl_sec > 0:
        with _lock:
            snap = _cache.get("snap")
            if snap is not None and (ts - float(_cache.get("ts") or 0)) < ttl_sec:
                return snap

    gates = extract_config_gates(cfg)
    sidecar = _probe_sidecar(gates.get("web_url") or "",
                             http_get or _http_get_json)
    registry = registry_rows if registry_rows is not None \
        else _registry_messenger_rows()
    sess = session_registry if session_registry is not None \
        else _session_registry_snapshot()

    verdict = evaluate_messenger_readiness(
        registry_accounts=registry,
        sidecar=sidecar,
        session_registry=sess,
        config_gates=gates,
        now=ts,
    )
    if not injected and ttl_sec > 0:
        with _lock:
            _cache["ts"] = ts
            _cache["snap"] = verdict
    return verdict


def _reset_cache_for_tests() -> None:
    with _lock:
        _cache["ts"] = 0.0
        _cache["snap"] = None
