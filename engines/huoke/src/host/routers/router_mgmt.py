# -*- coding: utf-8 -*-
"""路由器管理 API — GL.iNet 软路由统一控制接口。

端点列表:
  GET    /routers                   # 所有路由器列表 + 状态
  POST   /routers                   # 添加路由器
  GET    /routers/{id}              # 单台路由器详情
  PUT    /routers/{id}              # 更新路由器配置
  DELETE /routers/{id}              # 删除路由器
  POST   /routers/{id}/deploy       # 生成并推送 Clash 配置
  POST   /routers/deploy-all        # 批量部署所有路由器
  GET    /routers/{id}/status       # 实时检测状态（出口IP）
  GET    /routers/status-all        # 批量状态检测
  POST   /routers/{id}/assign-proxy # 分配代理账号
  POST   /routers/{id}/assign-device# 分配设备
  GET    /routers/{id}/clash-config # 预览 Clash 配置
  GET    /routers/countries         # 支持的国家列表 + GPS信息
"""

import logging
from fastapi import APIRouter, HTTPException, Depends, Request
from .auth import verify_api_key
from src.device_control.router_manager import (
    RouterInfo, get_router_manager,
)

router = APIRouter(tags=["routers"])
log = logging.getLogger(__name__)

from src.host.device_registry import PROJECT_ROOT, config_dir, config_file


@router.get("/routers", dependencies=[Depends(verify_api_key)])
def list_routers():
    """获取所有路由器列表（含缓存状态，不触发实时检测）。"""
    mgr = get_router_manager()
    routers = mgr.list_routers()
    pool_map = _get_pool_map()

    result = []
    for r in routers:
        proxy_labels = [pool_map.get(pid, {}).get("label", pid)
                        for pid in r.proxy_ids]
        result.append({
            "router_id": r.router_id,
            "name": r.name,
            "ip": r.ip,
            "port": r.port,
            "country": r.country,
            "city": r.city,
            "online": r.online,
            "current_exit_ip": r.current_exit_ip,
            "last_check": r.last_check,
            "proxy_ids": r.proxy_ids,
            "proxy_labels": proxy_labels,
            "proxy_count": len(r.proxy_ids),
            "device_ids": r.device_ids,
            "device_count": len(r.device_ids),
            "clash_config_pushed": r.clash_config_pushed,
            "notes": r.notes,
        })
    return {"routers": result, "total": len(result)}


@router.post("/routers", dependencies=[Depends(verify_api_key)])
def add_router(body: dict):
    """添加一台 GL.iNet 路由器。

    Body:
    {
        "name": "美国组A",
        "ip": "192.168.0.201",
        "port": 80,
        "password": "router_password",
        "country": "us",
        "city": "New York",
        "notes": "20台手机，01-20号"
    }
    """
    import time
    name = body.get("name", "").strip()
    ip = body.get("ip", "").strip()
    if not name or not ip:
        raise HTTPException(400, "name 和 ip 为必填项")

    mgr = get_router_manager()
    # 自动生成路由器 ID
    router_id = f"router-{int(time.time()*1000) % 100000:05d}"

    info = RouterInfo(
        router_id=router_id,
        name=name,
        ip=ip,
        port=int(body.get("port", 80)),
        password=body.get("password", ""),
        country=body.get("country", "").strip(),
        city=body.get("city", "").strip(),
        ssh_user=body.get("ssh_user", "root"),
        ssh_port=int(body.get("ssh_port", 22)),
        notes=body.get("notes", "").strip(),
    )
    mgr.add_router(info)
    return {"ok": True, "router_id": router_id, "name": name, "ip": ip}


@router.get("/routers/countries", dependencies=[Depends(verify_api_key)])
def list_countries():
    """获取支持的国家列表及 GPS/时区/语言信息。"""
    from src.device_control.router_manager import COUNTRY_GPS
    result = {}
    for country, info in COUNTRY_GPS.items():
        result[country] = {
            "language": info.get("language", ""),
            "country_code": info.get("country_code", ""),
            "cities": [c["city"] for c in info.get("cities", [])],
        }
    return {"countries": result}


@router.get("/routers/status-all", dependencies=[Depends(verify_api_key)])
def status_all_routers():
    """并发检测所有路由器的实时状态（含出口IP）。"""
    mgr = get_router_manager()
    statuses = mgr.get_all_status()
    return {
        "routers": [
            {
                "router_id": s.router_id,
                "name": s.name,
                "online": s.online,
                "exit_ip": s.exit_ip,
                "proxy_count": s.proxy_count,
                "device_count": s.device_count,
                "error": s.error,
            }
            for s in statuses
        ],
        "online_count": sum(1 for s in statuses if s.online),
        "total": len(statuses),
    }


@router.post("/routers/deploy-all", dependencies=[Depends(verify_api_key)])
def deploy_all_routers():
    """批量部署所有路由器的 Clash 配置。"""
    mgr = get_router_manager()
    results = mgr.deploy_all()
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_count > 0,
        "total": len(results),
        "success": ok_count,
        "results": results,
    }


@router.get("/routers/{router_id}", dependencies=[Depends(verify_api_key)])
def get_router(router_id: str):
    """获取单台路由器详情。"""
    mgr = get_router_manager()
    r = mgr.get_router(router_id)
    if not r:
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    pool_map = _get_pool_map()
    return {
        "router_id": r.router_id,
        "name": r.name,
        "ip": r.ip,
        "port": r.port,
        "country": r.country,
        "city": r.city,
        "online": r.online,
        "current_exit_ip": r.current_exit_ip,
        "last_check": r.last_check,
        "proxy_ids": r.proxy_ids,
        "proxies": [pool_map.get(pid, {"id": pid}) for pid in r.proxy_ids],
        "device_ids": r.device_ids,
        "clash_config_pushed": r.clash_config_pushed,
        "ssh_user": r.ssh_user,
        "ssh_port": r.ssh_port,
        "notes": r.notes,
    }


@router.put("/routers/{router_id}", dependencies=[Depends(verify_api_key)])
def update_router(router_id: str, body: dict):
    """更新路由器配置（部分更新）。"""
    mgr = get_router_manager()
    allowed = {"name", "ip", "port", "password", "country", "city",
               "ssh_user", "ssh_port", "notes"}
    updates = {k: v for k, v in body.items() if k in allowed}
    r = mgr.update_router(router_id, updates)
    if not r:
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    return {"ok": True, "router_id": router_id}


@router.delete("/routers/{router_id}", dependencies=[Depends(verify_api_key)])
def delete_router(router_id: str):
    """删除路由器。"""
    mgr = get_router_manager()
    if not mgr.delete_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    return {"ok": True}


@router.get("/routers/{router_id}/status", dependencies=[Depends(verify_api_key)])
def get_router_status(router_id: str):
    """实时检测单台路由器状态（会触发实际网络请求）。"""
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    s = mgr.get_status(router_id)
    return {
        "router_id": s.router_id,
        "name": s.name,
        "online": s.online,
        "exit_ip": s.exit_ip,
        "proxy_count": s.proxy_count,
        "device_count": s.device_count,
        "error": s.error,
    }


@router.post("/routers/{router_id}/deploy", dependencies=[Depends(verify_api_key)])
def deploy_router(router_id: str, body: dict = None):
    """生成 Clash 配置并推送到指定路由器。

    Body (可选):
    {
        "skip_proxy_check": false  // 是否跳过代理连通性预检（默认false）
    }
    新增：推送前备份旧配置、推送后等待30s验证出口IP。
    """
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    skip_check = (body or {}).get("skip_proxy_check", False)
    return mgr.deploy_router(router_id, skip_proxy_check=skip_check)


@router.post("/routers/{router_id}/test-proxy", dependencies=[Depends(verify_api_key)])
def test_router_proxies(router_id: str):
    """测试路由器已分配的所有代理账号连通性（TCP握手，不需要推送配置）。

    用于在 deploy 前验证代理账号是否可达。
    Returns:
        {total, ok, failed, results: [{host, port, latency_ms, ok, error}]}
    """
    from src.device_control.router_manager import _load_pool, test_all_proxy_connections
    mgr = get_router_manager()
    r = mgr.get_router(router_id)
    if not r:
        raise HTTPException(404, f"路由器 {router_id} 不存在")

    pool = _load_pool()
    proxy_accounts = [c for c in pool.get("configs", []) if c["id"] in r.proxy_ids]
    if not proxy_accounts:
        return {"ok": False, "error": "未分配代理账号", "total": 0, "ok_count": 0, "failed": 0}

    result = test_all_proxy_connections(proxy_accounts)
    result["router_id"] = router_id
    result["ok_count"] = result.pop("ok")
    result["ok"] = result["all_ok"]
    return result


@router.post("/routers/{router_id}/assign-proxy", dependencies=[Depends(verify_api_key)])
def assign_proxy_to_router(router_id: str, body: dict):
    """将代理账号分配给路由器。

    Body: {"proxy_ids": ["proxy_123", "proxy_456"]}
    """
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")

    proxy_ids = body.get("proxy_ids", [])
    if not isinstance(proxy_ids, list):
        raise HTTPException(400, "proxy_ids 必须是数组")

    mgr.assign_proxies(router_id, proxy_ids)
    return {"ok": True, "router_id": router_id, "proxy_count": len(proxy_ids)}


@router.post("/routers/{router_id}/assign-device", dependencies=[Depends(verify_api_key)])
def assign_device_to_router(router_id: str, body: dict):
    """记录哪些手机连接了这台路由器。

    Body: {"device_ids": ["device01", "device02"]}
    """
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")

    device_ids = body.get("device_ids", [])
    mgr.assign_devices(router_id, device_ids)
    return {"ok": True, "router_id": router_id, "device_count": len(device_ids)}


@router.get("/routers/{router_id}/clash-config", dependencies=[Depends(verify_api_key)])
def preview_clash_config(router_id: str):
    """预览路由器的 Clash 配置文件内容（不推送）。"""
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    yaml_content = mgr.preview_clash_config(router_id)
    if not yaml_content:
        raise HTTPException(400, "无法生成配置，请检查代理账号是否已分配")
    return {"router_id": router_id, "clash_yaml": yaml_content}


# ── Clash 配置备份历史 ──

@router.get("/routers/{router_id}/backups", dependencies=[Depends(verify_api_key)])
def list_clash_backups(router_id: str):
    """列出路由器的 Clash 配置备份文件（按时间倒序）。"""
    backup_dir = config_dir() / "clash_backups"

    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")

    backups = []
    if backup_dir.exists():
        for f in sorted(backup_dir.glob(f"{router_id}_*.yaml"), reverse=True):
            stat = f.stat()
            backups.append({
                "filename": f.name,
                "size_bytes": stat.st_size,
                "created_at": stat.st_mtime,
                "created_at_str": __import__("time").strftime(
                    "%Y-%m-%d %H:%M:%S", __import__("time").localtime(stat.st_mtime)
                ),
            })
    return {"router_id": router_id, "backups": backups, "total": len(backups)}


@router.post("/routers/{router_id}/restore", dependencies=[Depends(verify_api_key)])
def restore_clash_backup(router_id: str, body: dict):
    """从备份文件恢复 Clash 配置（一键回滚）。

    Body: {"filename": "router-01_20260411_143000.yaml"}
    """
    from pathlib import Path
    from src.device_control.router_manager import _glinet_login, _restore_clash_config

    filename = body.get("filename", "").strip()
    if not filename or ".." in filename or "/" in filename:
        raise HTTPException(400, "非法的文件名")

    backup_dir = config_dir() / "clash_backups"
    backup_path = backup_dir / filename

    if not backup_path.exists():
        raise HTTPException(404, f"备份文件不存在: {filename}")

    mgr = get_router_manager()
    router = mgr.get_router(router_id)
    if not router:
        raise HTTPException(404, f"路由器 {router_id} 不存在")

    sid = _glinet_login(router)
    if not sid:
        raise HTTPException(503, "无法连接路由器，请确认路由器在线且密码正确")

    ok = _restore_clash_config(router, sid, str(backup_path))
    if not ok:
        raise HTTPException(500, "回滚失败，请检查路由器连接")

    return {"ok": True, "router_id": router_id, "restored_from": filename,
            "message": "配置已恢复，Clash 正在重启（约30秒后生效）"}


# ── 代理自动轮换 ──

@router.post("/routers/{router_id}/rotate-proxy", dependencies=[Depends(verify_api_key)])
def rotate_router_proxy(router_id: str, body: dict = None):
    """手动触发代理账号轮换（选取备用代理重新部署）。

    Body (可选): {"reason": "手动触发"}

    使用场景：代理账号到期/IP变更/封号后，快速切换到备用代理。
    注意：30分钟内只能轮换1次（速率限制），可通过 clear-blacklist 清除黑名单。
    """
    from src.device_control.proxy_rotator import rotate_proxy
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")

    reason = (body or {}).get("reason", "手动触发")
    result = rotate_proxy(router_id, reason=reason)
    return result


@router.get("/routers/{router_id}/rotation-history", dependencies=[Depends(verify_api_key)])
def get_rotation_history(router_id: str):
    """获取路由器的代理轮换历史和黑名单。"""
    from src.device_control.proxy_rotator import get_rotation_history
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    return get_rotation_history(router_id)


@router.post("/routers/{router_id}/clear-blacklist", dependencies=[Depends(verify_api_key)])
def clear_proxy_blacklist(router_id: str):
    """清除路由器的代理账号黑名单（允许重新使用曾失败的代理）。"""
    from src.device_control.proxy_rotator import clear_blacklist
    mgr = get_router_manager()
    if not mgr.get_router(router_id):
        raise HTTPException(404, f"路由器 {router_id} 不存在")
    clear_blacklist(router_id)
    return {"ok": True, "router_id": router_id, "message": "黑名单已清除"}


# ── 代理健康评分 ──

@router.get("/proxy/scores", dependencies=[Depends(verify_api_key)])
def get_proxy_scores():
    """获取所有代理账号的健康评分（连通率/延迟/测试次数）。"""
    from src.device_control.proxy_rotator import get_all_proxy_scores
    scores = get_all_proxy_scores()
    pool_map = _get_pool_map()

    # 合并代理标签信息
    result = []
    for proxy_id, s in scores.items():
        pool_info = pool_map.get(proxy_id, {})
        s["label"] = pool_info.get("label", proxy_id)
        s["server"] = pool_info.get("server", "")
        result.append(s)

    result.sort(key=lambda x: x["score"], reverse=True)
    return {"scores": result, "total": len(result)}


@router.get("/proxy/affinity", dependencies=[Depends(verify_api_key)])
def get_proxy_affinity(router_id: str = None):
    """获取代理亲和力评分（按路由器分组的历史成功率）。

    Query params:
      router_id: 指定路由器（不填则返回所有）
    """
    from src.device_control.proxy_rotator import _load_affinity, get_affinity_score
    data = _load_affinity()
    pool_map = _get_pool_map()
    result = {}
    for rid, proxy_map in data.items():
        if router_id and rid != router_id:
            continue
        router_scores = []
        for pid, entry in proxy_map.items():
            total = entry.get("success", 0) + entry.get("fail", 0)
            pool_info = pool_map.get(pid, {})
            router_scores.append({
                "proxy_id": pid,
                "label": pool_info.get("label", pid),
                "affinity_score": get_affinity_score(rid, pid),
                "success": entry.get("success", 0),
                "fail": entry.get("fail", 0),
                "total": total,
                "last_used": entry.get("last_used", 0),
            })
        router_scores.sort(key=lambda x: x["affinity_score"], reverse=True)
        result[rid] = router_scores
    return {"affinity": result, "routers": list(result.keys())}


@router.get("/geo/lookup/{ip}", dependencies=[Depends(verify_api_key)])
def lookup_ip_geo(ip: str):
    """查询指定 IP 的地理位置信息（带缓存）。"""
    from src.device_control.ip_geolocation import lookup_ip_country
    info = lookup_ip_country(ip)
    if not info:
        raise HTTPException(503, f"无法查询 IP {ip} 的地理位置（所有 API 均失败）")
    return info


@router.get("/geo/verify/{ip}", dependencies=[Depends(verify_api_key)])
def verify_ip_geo(ip: str, country: str = "us"):
    """验证 IP 是否在目标国家。

    Query params:
      country: 目标国家代码（如 us, italy, uk, br）
    """
    from src.device_control.ip_geolocation import verify_ip_for_country
    match, info = verify_ip_for_country(ip, country)
    return {**info, "ok": match}


# ══════════════════════════════════════════════
# Phase 6: 922S5 代理管理端点
# ══════════════════════════════════════════════

@router.get("/proxy/922s5/status", dependencies=[Depends(verify_api_key)])
def get_922s5_status():
    """获取 922S5 集成状态（余额 + 代理池统计）。"""
    from src.device_control.proxy_922s5 import get_922s5_status as _status
    return _status()


@router.post("/proxy/922s5/configure", dependencies=[Depends(verify_api_key)])
def configure_922s5(body: dict):
    """配置 922S5 API 凭证。

    Body:
      app_key: str
      app_secret: str
      auto_replenish: bool (default: true)
      min_pool_size: int (default: 3)
      preferred_countries: list[str] (default: ["US"])
    """
    from src.device_control.proxy_922s5 import configure_922s5 as _configure, save_922s5_config, load_922s5_config
    import time
    app_key = body.get("app_key", "")
    app_secret = body.get("app_secret", "")
    if not app_key or not app_secret:
        return {"ok": False, "error": "缺少 app_key 或 app_secret"}
    _configure(
        app_key=app_key,
        app_secret=app_secret,
        auto_replenish=body.get("auto_replenish", True),
        min_pool_size=body.get("min_pool_size", 3),
        low_balance_threshold=body.get("low_balance_threshold", 5.0),
    )
    # 保存额外配置项
    cfg = load_922s5_config() or {}
    if "preferred_countries" in body:
        cfg["preferred_countries"] = body["preferred_countries"]
        save_922s5_config(cfg)
    return {"ok": True, "message": "922S5 配置已保存"}


@router.get("/proxy/922s5/proxies", dependencies=[Depends(verify_api_key)])
def list_922s5_proxies(country: str = None, status: str = "active"):
    """列出 922S5 账户下的代理。"""
    from src.device_control.proxy_922s5 import get_922s5_client
    client = get_922s5_client()
    if not client:
        return {"ok": False, "error": "922S5 未配置", "proxies": []}
    proxies = client.list_proxies(country=country, status=status)
    return {
        "ok": True,
        "count": len(proxies),
        "proxies": [{"proxy_id": p.proxy_id, "server": p.server, "port": p.port,
                     "country": p.country, "city": p.city, "expire_time": p.expire_time,
                     "status": p.status} for p in proxies],
    }


@router.post("/proxy/922s5/sync", dependencies=[Depends(verify_api_key)])
def sync_922s5_to_pool(body: dict = None):
    """从 922S5 拉取代理列表并同步到本地代理池。"""
    from src.device_control.proxy_922s5 import get_922s5_client, sync_proxies_to_pool
    body = body or {}
    client = get_922s5_client()
    if not client:
        return {"ok": False, "error": "922S5 未配置"}
    country = body.get("country")
    proxies = client.list_proxies(country=country)
    added = sync_proxies_to_pool(proxies)
    return {"ok": True, "fetched": len(proxies), "added": added}


@router.post("/proxy/922s5/replenish", dependencies=[Depends(verify_api_key)])
def replenish_922s5(body: dict = None):
    """手动触发 922S5 代理补货。

    Body:
      target_count: int (default: 5)
      countries: list[str] (default: ["US"])
    """
    from src.device_control.proxy_922s5 import replenish_proxy_pool
    body = body or {}
    result = replenish_proxy_pool(
        target_count=body.get("target_count", 5),
        countries=body.get("countries"),
    )
    return result


@router.post("/proxy/922s5/refresh/{proxy_id}", dependencies=[Depends(verify_api_key)])
def refresh_922s5_proxy(proxy_id: str):
    """刷新指定 922S5 代理的出口 IP。"""
    from src.device_control.proxy_922s5 import get_922s5_client
    client = get_922s5_client()
    if not client:
        return {"ok": False, "error": "922S5 未配置"}
    result = client.refresh_proxy(proxy_id)
    if result:
        return {"ok": True, "proxy_id": proxy_id,
                "server": result.server, "port": result.port}
    return {"ok": False, "error": f"刷新失败: {proxy_id}"}


@router.get("/proxy/922s5/balance", dependencies=[Depends(verify_api_key)])
def get_922s5_balance():
    """查询 922S5 账户余额。"""
    from src.device_control.proxy_922s5 import get_922s5_client
    client = get_922s5_client()
    if not client:
        return {"ok": False, "error": "922S5 未配置"}
    balance = client.get_balance()
    if balance:
        return {"ok": True, **balance}
    return {"ok": False, "error": "余额查询失败"}


# ══════════════════════════════════════════════
# Phase 7 P0: 代理池管理端点
# ══════════════════════════════════════════════

@router.get("/proxy/pool/stats", dependencies=[Depends(verify_api_key)])
def get_proxy_pool_stats():
    """获取代理池统计信息（活跃/过期/按国家/按来源）。"""
    from src.device_control.proxy_pool_manager import get_pool_stats
    return get_pool_stats()


@router.get("/proxy/pool/list", dependencies=[Depends(verify_api_key)])
def list_proxy_pool(country: str = None, active_only: bool = True):
    """列出代理池中的代理。"""
    from src.device_control.proxy_pool_manager import load_pool, get_available_proxies
    if active_only:
        return {"proxies": get_available_proxies(country=country), "ok": True}
    pool = load_pool()
    if country:
        pool = [p for p in pool if p.get("country", "").lower() == country.lower()]
    return {"proxies": pool, "total": len(pool), "ok": True}


@router.post("/proxy/pool/sync", dependencies=[Depends(verify_api_key)])
def sync_proxy_pool(body: dict = None):
    """从 922S5 同步代理到本地池，并清理过期代理。"""
    from src.device_control.proxy_pool_manager import run_proxy_pool_sync
    body = body or {}
    result = run_proxy_pool_sync(params=body)
    return result


@router.post("/proxy/pool/cleanup", dependencies=[Depends(verify_api_key)])
def cleanup_proxy_pool():
    """清理过期代理（标记为 inactive）。"""
    from src.device_control.proxy_pool_manager import cleanup_expired, get_pool_stats
    count = cleanup_expired()
    stats = get_pool_stats()
    return {"ok": True, "marked_expired": count, "pool_stats": stats}


@router.post("/proxy/pool/schedule", dependencies=[Depends(verify_api_key)])
def register_pool_sync_schedule(body: dict = None):
    """注册代理池定时同步任务（默认每天 06:00）。"""
    from src.device_control.proxy_pool_manager import ensure_sync_schedule
    body = body or {}
    cron = body.get("cron_expr", "0 6 * * *")
    sid = ensure_sync_schedule(cron_expr=cron)
    if sid:
        return {"ok": True, "schedule_id": sid, "cron_expr": cron}
    return {"ok": False, "error": "注册失败（调度器可能未启动）"}


# ══════════════════════════════════════════════
# Phase 7 P1: 设备代理状态 API
# ══════════════════════════════════════════════

@router.get("/devices/{device_id}/proxy-status", dependencies=[Depends(verify_api_key)])
def get_device_proxy_status(device_id: str):
    """查询设备的代理健康状态（含熔断状态，用于发布前检查）。"""
    from src.studio.publishers.base_publisher import _check_proxy_circuit_breaker
    result = _check_proxy_circuit_breaker(device_id)
    return {"device_id": device_id, **result}


# ══════════════════════════════════════════════
# Phase 6: MockLocation 设备状态端点
# ══════════════════════════════════════════════

@router.get("/devices/{device_id}/mock-location/status", dependencies=[Depends(verify_api_key)])
def get_device_mock_location_status(device_id: str):
    """查询设备的 MockLocation 应用状态。"""
    from src.device_control.mock_location_manager import get_device_mock_status
    return get_device_mock_status(device_id)


@router.post("/devices/{device_id}/mock-location/scan", dependencies=[Depends(verify_api_key)])
def scan_device_mock_apps(device_id: str, body: dict = None):
    """重新扫描设备上的 MockLocation 应用。"""
    from src.device_control.mock_location_manager import scan_mock_apps, clear_device_cache
    body = body or {}
    force = body.get("force_rescan", True)
    if force:
        clear_device_cache(device_id)
    app = scan_mock_apps(device_id, force_rescan=force)
    if app:
        return {"ok": True, "found": True, "app": app}
    return {"ok": True, "found": False,
            "message": "未找到 MockLocation 应用，请参考安装指引"}


@router.post("/devices/{device_id}/mock-location/set", dependencies=[Depends(verify_api_key)])
def set_device_mock_location(device_id: str, body: dict):
    """为设备设置 Mock GPS 位置。

    Body:
      latitude: float
      longitude: float
      altitude: float (optional, default: 0.0)
      country: str (optional, 用国家代码自动获取坐标)
    """
    from src.device_control.mock_location_manager import (
        set_mock_location, configure_mock_location_for_country, get_country_gps_for_mock
    )
    country = body.get("country")
    if country:
        ok = configure_mock_location_for_country(device_id, country)
        coords = get_country_gps_for_mock(country)
        return {"ok": ok, "country": country,
                "latitude": coords[0] if coords else None,
                "longitude": coords[1] if coords else None}

    lat = body.get("latitude")
    lon = body.get("longitude")
    if lat is None or lon is None:
        return {"ok": False, "error": "缺少 latitude 和 longitude 参数"}

    ok = set_mock_location(device_id, float(lat), float(lon),
                           float(body.get("altitude", 0.0)))
    return {"ok": ok, "latitude": lat, "longitude": lon}


@router.post("/devices/{device_id}/mock-location/install", dependencies=[Depends(verify_api_key)])
def install_mock_location_app(device_id: str):
    """尝试安装 MockLocation APK（需提前将 APK 放置到 config/apks/ 目录）。"""
    from src.device_control.mock_location_manager import ensure_mock_app, get_apk_install_instructions
    ok = ensure_mock_app(device_id)
    if ok:
        return {"ok": True, "message": "MockLocation 应用已就绪"}
    instructions = get_apk_install_instructions(device_id)
    return {"ok": False, "instructions": instructions}


# ══════════════════════════════════════════════
# Phase 8 P1: APK 构建与上传工具
# ══════════════════════════════════════════════

@router.post("/tools/build-mock-location-apk", dependencies=[Depends(verify_api_key)])
def build_mock_location_apk():
    """构建 MockLocation APK。需要 Android SDK；Java JDK 17 已内置。"""
    import shutil
    import subprocess
    import os
    from pathlib import Path

    project_root = PROJECT_ROOT

    # 检测 Java 可用性
    java_available = False
    java_version = ""
    known_jdk_javac = Path("C:/Program Files/Microsoft/jdk-17.0.18.8-hotspot/bin/javac")
    if shutil.which("javac"):
        java_available = True
        try:
            result = subprocess.run(
                ["javac", "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
            java_version = (result.stdout.strip() or result.stderr.strip())
        except Exception:
            java_version = "unknown"
    elif known_jdk_javac.exists():
        java_available = True
        java_version = "jdk-17.0.18.8-hotspot"

    # 检测 Android SDK 是否已安装
    sdk_paths = [
        Path("C:/Users/Administrator/AppData/Local/Android/Sdk"),
    ]
    android_home = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if android_home:
        sdk_paths.insert(0, Path(android_home))

    sdk_found = any(p.exists() for p in sdk_paths)

    if not sdk_found:
        return {
            "ok": False,
            "sdk_missing": True,
            "java_available": java_available,
            "instructions": (
                "请安装 Android SDK。推荐步骤：\n"
                "1. 下载并安装 Android Studio（含 SDK）：https://developer.android.com/studio\n"
                "2. 或通过命令行工具单独安装 SDK command-line tools\n"
                "3. 确保 ANDROID_HOME 环境变量指向 SDK 根目录\n"
                "4. 安装完成后重新调用本接口即可触发构建"
            ),
        }

    # SDK 存在，执行构建
    build_script = project_root / "tools" / "mock_location_helper" / "build.py"
    if not build_script.exists():
        return {"ok": False, "error": f"构建脚本不存在：{build_script}"}

    try:
        proc = subprocess.run(
            ["python", str(build_script)],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
        output = proc.stdout + proc.stderr
        if proc.returncode != 0:
            return {"ok": False, "output": output, "java_version": java_version}

        # 尝试定位生成的 APK
        apk_path = ""
        apk_candidates = list((project_root / "config" / "apks").glob("*.apk"))
        if not apk_candidates:
            apk_candidates = list(project_root.rglob("*.apk"))
        if apk_candidates:
            apk_path = str(sorted(apk_candidates, key=lambda p: p.stat().st_mtime)[-1])

        return {"ok": True, "output": output, "apk_path": apk_path, "java_version": java_version}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "构建超时（300s）", "java_version": java_version}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "java_version": java_version}


@router.post("/tools/upload-apk", dependencies=[Depends(verify_api_key)])
async def upload_apk(request: Request):
    """上传预构建的 APK 文件到 config/apks/ 目录。

    支持两种方式：
    1. multipart/form-data，字段名 file
    2. JSON body：{"filename": "xxx.apk", "content_base64": "..."}
    """
    import base64
    from pathlib import Path

    apk_dir = config_dir() / "apks"
    apk_dir.mkdir(parents=True, exist_ok=True)

    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        file_field = form.get("file")
        if file_field is None:
            return {"ok": False, "error": "multipart 中未找到 file 字段"}
        filename = getattr(file_field, "filename", None) or "upload.apk"
        if not filename.lower().endswith(".apk"):
            return {"ok": False, "error": "文件名必须以 .apk 结尾"}
        data = await file_field.read()
    else:
        # 尝试解析 JSON body
        try:
            body = await request.json()
        except Exception:
            return {"ok": False, "error": "无法解析请求体，请使用 multipart/form-data 或 JSON"}
        filename = body.get("filename", "")
        if not filename.lower().endswith(".apk"):
            return {"ok": False, "error": "filename 必须以 .apk 结尾"}
        content_b64 = body.get("content_base64", "")
        if not content_b64:
            return {"ok": False, "error": "缺少 content_base64 字段"}
        try:
            data = base64.b64decode(content_b64)
        except Exception:
            return {"ok": False, "error": "content_base64 解码失败"}

    # 安全校验：禁止路径穿越
    safe_name = Path(filename).name
    if not safe_name.lower().endswith(".apk"):
        return {"ok": False, "error": "非法文件名"}

    dest = apk_dir / safe_name
    dest.write_bytes(data)
    return {"ok": True, "path": str(dest), "size_bytes": len(data)}


# ── 辅助函数 ──

def _get_pool_map() -> dict:
    """获取配置池的 ID → 配置 映射。"""
    try:
        import json
        pool_file = config_file("vpn_pool.json")
        if pool_file.exists():
            pool = json.loads(pool_file.read_text(encoding="utf-8"))
            return {c["id"]: c for c in pool.get("configs", [])}
    except Exception:
        pass
    return {}
