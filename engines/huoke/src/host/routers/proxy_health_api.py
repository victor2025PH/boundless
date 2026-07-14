# -*- coding: utf-8 -*-
"""代理健康监控 API — 出口IP验证、熔断状态、GPS地理配置。

端点列表:
  GET    /proxy/health               # 所有设备代理健康状态
  POST   /proxy/health/{device_id}/check  # 立即检测指定设备
  POST   /proxy/health/check-all     # 批量检测所有注册设备
  GET    /proxy/health/summary       # 汇总统计（正常/熔断/失败数量）
  POST   /proxy/geo/{device_id}      # 为指定设备配置GPS/时区/语言
  POST   /proxy/geo-all              # 批量为所有设备配置地理信息
  POST   /proxy/circuit/{device_id}/reset  # 手动重置熔断器
"""

import logging
import threading
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from src.behavior.proxy_health import (
    get_proxy_health_monitor,
    configure_device_for_country,
)

router = APIRouter(prefix="/proxy", tags=["proxy-health"])
log = logging.getLogger(__name__)


# ── Pydantic Models ──

class GeoConfigRequest(BaseModel):
    country: str
    city: Optional[str] = ""


class GeoAllRequest(BaseModel):
    """批量地理配置 — 不传 device_ids 则配置所有已注册设备。"""
    device_ids: Optional[list] = None


# ── 健康状态端点 ──

@router.get("/health")
def get_all_proxy_health():
    """获取所有设备的代理健康状态（使用缓存，不触发实时检测）。"""
    monitor = get_proxy_health_monitor()
    statuses = monitor.get_all_status()
    total = len(statuses)
    ok_count = sum(1 for s in statuses.values() if s["ip_match"] and not s["circuit_open"])
    circuit_open_count = sum(1 for s in statuses.values() if s["circuit_open"])
    fail_count = sum(1 for s in statuses.values() if not s["ip_match"] and not s["circuit_open"])

    return {
        "total": total,
        "ok": ok_count,
        "circuit_open": circuit_open_count,
        "fail": fail_count,
        "devices": list(statuses.values()),
    }


@router.get("/health/summary")
def get_proxy_health_summary():
    """获取代理健康汇总统计（4态状态机）。"""
    monitor = get_proxy_health_monitor()
    # 使用新的 get_summary() 方法（4态聚合）
    summary = monitor.get_summary()

    # 按路由器分组（使用完整状态数据）
    statuses = monitor.get_all_status()
    by_router: dict = {}
    for s in statuses.values():
        rid = s.get("router_id", "unknown")
        if rid not in by_router:
            by_router[rid] = {"ok": 0, "leak": 0, "no_ip": 0,
                              "unverified": 0, "circuit_open": 0}
        state = s.get("state", "unverified")
        if s["circuit_open"]:
            by_router[rid]["circuit_open"] += 1
        by_router[rid][state] = by_router[rid].get(state, 0) + 1

    summary["by_router"] = by_router
    return summary


@router.post("/health/{device_id}/check")
def check_device_health(device_id: str):
    """立即检测指定设备的代理健康状态（触发实时IP查询）。"""
    monitor = get_proxy_health_monitor()
    # 自动注册（如果还没注册）
    if device_id not in monitor._device_router_map:
        monitor.register_all_from_routers()
    try:
        status = monitor.check_device(device_id)
        return {
            "ok": status.state == "ok",
            "device_id": status.device_id,
            "router_id": status.router_id,
            "state": status.state,
            "expected_ip": status.expected_ip,
            "actual_ip": status.actual_ip,
            "ip_match": status.ip_match,
            "circuit_open": status.circuit_open,
            "circuit_cooldown_remaining": max(
                0, 900 - (
                    __import__("time").time() - status.circuit_open_time
                )
            ) if status.circuit_open else 0,
            "consecutive_fails": status.consecutive_fails,
            "error": status.error,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/health/check-all")
def check_all_devices_health():
    """批量检测所有已注册设备（异步启动，立即返回任务ID）。"""
    monitor = get_proxy_health_monitor()
    monitor.register_all_from_routers()
    devices = list(monitor._device_router_map.keys())

    if not devices:
        return {"ok": True, "message": "没有已注册的设备", "device_count": 0}

    results = []

    def _run():
        for did in devices:
            try:
                monitor.check_device(did)
            except Exception as e:
                log.debug("[ProxyHealthAPI] %s 检测失败: %s", did[:8], e)

    t = threading.Thread(target=_run, daemon=True, name="proxy-health-check-all")
    t.start()

    return {
        "ok": True,
        "message": f"已启动批量检测，共 {len(devices)} 台设备",
        "device_count": len(devices),
        "devices": devices,
    }


@router.post("/circuit/{device_id}/reset")
def reset_circuit_breaker(device_id: str):
    """手动重置设备熔断器（清除连续失败计数和冷却期，允许重新检测）。"""
    monitor = get_proxy_health_monitor()
    ok = monitor.reset_circuit(device_id)
    if not ok:
        raise HTTPException(status_code=404, detail="设备状态不存在，请先执行检测")
    return {"ok": True, "device_id": device_id, "message": "熔断器已重置（consecutive_fails=0，冷却期清除）"}


# ── 地理配置端点 ──

@router.post("/geo/{device_id}")
def configure_device_geo(device_id: str, body: GeoConfigRequest):
    """为指定设备配置GPS位置/时区/系统语言（与代理IP国家匹配）。

    配置项:
    - GPS位置：使用 adb emu geo fix（或广播模拟位置）
    - 时区：setprop + settings put global
    - 系统语言：setprop persist.sys.language + country

    调用示例:
    POST /proxy/geo/device123 {"country": "us", "city": "new_york"}
    """
    result = configure_device_for_country(device_id, body.country, body.city or "")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "配置失败"))
    return result


@router.post("/geo-all")
def configure_all_devices_geo(body: GeoAllRequest):
    """批量为所有设备配置地理信息（根据其绑定的路由器国家自动推断）。

    流程:
    1. 从路由器管理器获取每台设备绑定的路由器
    2. 读取路由器的 country 字段
    3. 调用 configure_device_for_country(device_id, country)
    """
    monitor = get_proxy_health_monitor()
    monitor.register_all_from_routers()

    try:
        from src.device_control.router_manager import get_router_manager
        mgr = get_router_manager()
        router_map = {r.router_id: r for r in mgr.list_routers()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"无法获取路由器信息: {e}")

    device_ids = body.device_ids or list(monitor._device_router_map.keys())
    if not device_ids:
        return {"ok": True, "message": "没有已注册的设备", "results": []}

    results = []
    errors = []

    for did in device_ids:
        router_id = monitor._device_router_map.get(did, "")
        r = router_map.get(router_id)
        if not r:
            errors.append({"device_id": did, "error": f"未找到路由器: {router_id}"})
            continue
        country = r.country
        if not country:
            errors.append({"device_id": did, "error": "路由器未设置国家"})
            continue
        try:
            res = configure_device_for_country(did, country, r.city or "")
            results.append(res)
        except Exception as e:
            errors.append({"device_id": did, "error": str(e)})

    return {
        "ok": True,
        "total": len(device_ids),
        "success": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


# ── P2-① 代理质量看板 ──

@router.get("/quality")
def get_proxy_quality(hours: int = 24):
    """代理质量聚合看板 — 单设备维度合并 proxy_health 当前状态 + 任务窗口失败归因.

    数据源 (read-only, 不做新埋点):
      A. proxy_health_monitor.get_all_status() — 4 态（ok/leak/no_ip/unverified）+ circuit
      B. task_store SQL — 近 N 小时各设备 total/failed task 数
      C. error_classifier.classify_task_error — 把每个失败 error 归到 layer (infra/business/...)

    输出 per_device 数组：每设备一行，含
      - current_state, current_ip, expected_country (来自 A)
      - tasks_total, tasks_failed, infra_failed, business_failed (来自 B+C)
      - infra_health_score = 100 × (1 - infra_failed / total)
      - top_error_code = error_classifier 归类最频繁的 code（如 proxy_hijack）

    用途：dashboard 顶部「代理质量」tab 一次性展示哪台手机代理出问题、是基建还是业务挂.
    """
    import json as _j
    import time as _t
    from src.host.error_classifier import classify_task_error
    from ..database import get_conn
    from ..task_store import _alive_sql

    monitor = get_proxy_health_monitor()
    statuses = monitor.get_all_status()  # device_id → status dict

    cutoff = _t.strftime("%Y-%m-%dT%H:%M:%SZ", _t.gmtime(_t.time() - hours * 3600))
    _aq = _alive_sql()
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT device_id, status, result FROM tasks "
            f"WHERE updated_at > ? AND {_aq}",
            (cutoff,),
        ).fetchall()

    # 聚合 per device
    per_device_agg: dict = {}
    layer_totals = {"infra": 0, "business": 0, "quota": 0, "timing": 0, "safety": 0, "unknown": 0}
    for row in rows:
        did = row[0] or "unknown"
        status = row[1]
        agg = per_device_agg.setdefault(did, {
            "total": 0, "failed": 0, "infra_failed": 0,
            "business_failed": 0, "code_counts": {},
        })
        agg["total"] += 1
        if status != "failed":
            continue
        agg["failed"] += 1
        result_raw = row[2]
        result = {}
        if isinstance(result_raw, str):
            try:
                result = _j.loads(result_raw)
            except Exception:
                pass
        elif isinstance(result_raw, dict):
            result = result_raw
        err = result.get("error", "") or ""
        cls = classify_task_error(err)
        if not cls:
            continue
        layer = cls.get("layer", "unknown")
        code = cls.get("code", "unclassified")
        layer_totals[layer] = layer_totals.get(layer, 0) + 1
        agg["code_counts"][code] = agg["code_counts"].get(code, 0) + 1
        if layer == "infra":
            agg["infra_failed"] += 1
        elif layer == "business":
            agg["business_failed"] += 1

    # 装配 per_device 列表（含未在 task 窗口里出现但 proxy_health 注册了的设备）
    all_dids = set(per_device_agg.keys()) | set(statuses.keys())
    per_device_out = []
    for did in sorted(all_dids):
        agg = per_device_agg.get(did, {"total": 0, "failed": 0, "infra_failed": 0,
                                       "business_failed": 0, "code_counts": {}})
        ph = statuses.get(did) or {}
        score = 100
        if agg["total"] > 0:
            score = max(0, round((1 - agg["infra_failed"] / agg["total"]) * 100))
        top_code = ""
        if agg["code_counts"]:
            top_code = max(agg["code_counts"], key=lambda k: agg["code_counts"][k])
        per_device_out.append({
            "device_id": did,
            "router_id": ph.get("router_id", ""),
            "current_state": ph.get("state", "unknown"),
            "current_ip": ph.get("actual_ip", ""),
            "expected_ip": ph.get("expected_ip", ""),
            "ip_match": ph.get("ip_match"),
            "circuit_open": ph.get("circuit_open", False),
            "tasks_total": agg["total"],
            "tasks_failed": agg["failed"],
            "infra_failed": agg["infra_failed"],
            "business_failed": agg["business_failed"],
            "infra_health_score": score,
            "top_error_code": top_code,
        })

    # 顶部汇总
    state_counts = {"ok": 0, "leak": 0, "no_ip": 0, "unverified": 0, "circuit_open": 0}
    for s in statuses.values():
        st = s.get("state", "unverified")
        state_counts[st] = state_counts.get(st, 0) + 1
        if s.get("circuit_open"):
            state_counts["circuit_open"] += 1

    return {
        "window_hours": hours,
        "summary": {
            "devices_total": len(statuses),
            "tasks_total": sum(d["tasks_total"] for d in per_device_out),
            "tasks_failed": sum(d["tasks_failed"] for d in per_device_out),
            "infra_failed": sum(d["infra_failed"] for d in per_device_out),
            "business_failed": sum(d["business_failed"] for d in per_device_out),
            **state_counts,
        },
        "by_error_layer": layer_totals,
        "per_device": per_device_out,
    }
