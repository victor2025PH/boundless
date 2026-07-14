# -*- coding: utf-8 -*-
"""统一多平台 API — TikTok / Telegram / WhatsApp / Facebook / LinkedIn / IG / X。"""

import logging

import yaml
from fastapi import APIRouter, HTTPException

from src.host.device_registry import config_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/platforms", tags=["platforms"])


def _find_worker_for_device(device_id: str):
    """查找设备所在的 Worker，返回 {ip, port} 或 None。"""
    try:
        from .cluster import _get_best_worker_url
        return _get_best_worker_url(device_id)
    except Exception:
        return None


def _dispatch_to_worker(worker: dict, task_type: str, device_id: str,
                        params: dict, platform: str):
    """将任务转发到 Worker 执行。"""
    import json as _json
    import urllib.request
    url = f"http://{worker['ip']}:{worker['port']}/tasks"
    payload = _json.dumps({
        "type": task_type,
        "device_id": device_id,
        "params": params,
        "created_via": (params or {}).get("_created_via"),
    }).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    resp = urllib.request.urlopen(req, timeout=10)
    result = _json.loads(resp.read().decode())
    return {"ok": True, "task_id": result.get("task_id", ""),
            "platform": platform, "task_type": task_type,
            "dispatched_to": worker["ip"]}

_ROADMAP_PATH = config_file("global_growth_roadmap.yaml")

PLATFORMS = {
    "tiktok": {
        "name": "TikTok",
        "package": "com.zhiliaoapp.musically",
        "task_types": [
            {"type": "tiktok_warmup", "label": "养号", "params": ["duration"]},
            {"type": "tiktok_watch", "label": "刷视频", "params": ["duration", "count"]},
            {"type": "tiktok_follow", "label": "关注用户", "params": ["keyword", "count"]},
            {"type": "tiktok_send_dm", "label": "发私信", "params": ["username", "message"]},
            {"type": "tiktok_check_inbox", "label": "查收件箱", "params": ["auto_reply"]},
            {"type": "tiktok_acquisition", "label": "全流程获客", "params": ["country"]},
        ],
    },
    "telegram": {
        "name": "Telegram",
        "package": "org.telegram.messenger",
        "task_types": [
            {"type": "telegram_send_message", "label": "发消息", "params": ["username", "message"]},
            {"type": "telegram_read_messages", "label": "读消息", "params": ["username", "count"]},
            {"type": "telegram_send_file", "label": "发文件", "params": ["username", "file_path"]},
            {"type": "telegram_auto_reply", "label": "自动回复", "params": ["duration"]},
            {"type": "telegram_join_group", "label": "加入群组", "params": ["group"]},
            {"type": "telegram_send_group", "label": "群组消息", "params": ["group", "message"]},
            {"type": "telegram_monitor_chat", "label": "监控聊天", "params": ["username", "duration"]},
        ],
    },
    "whatsapp": {
        "name": "WhatsApp",
        "package": "com.whatsapp",
        "task_types": [
            {"type": "whatsapp_send_message", "label": "发消息", "params": ["contact", "message"]},
            {"type": "whatsapp_read_messages", "label": "读消息", "params": ["contact", "count"]},
            {"type": "whatsapp_auto_reply", "label": "自动回复", "params": ["duration"]},
            {"type": "whatsapp_send_media", "label": "发媒体", "params": ["contact", "media_path"]},
            {"type": "whatsapp_list_chats", "label": "聊天列表", "params": []},
        ],
    },
    "facebook": {
        "name": "Facebook",
        "package": "com.facebook.katana",
        "task_types": [
            {"type": "facebook_browse_feed", "label": "浏览动态",
             "params": ["scroll_count", "like_probability"]},
            {"type": "facebook_browse_feed_by_interest", "label": "兴趣刷帖",
             "params": ["duration", "persona_key", "interest_hours", "max_topics", "like_boost"]},
            {"type": "facebook_search_leads", "label": "搜索潜客",
             "params": ["keyword", "max_leads"]},
            {"type": "facebook_join_group", "label": "加入群组",
             "params": ["group_name"]},
            {"type": "facebook_browse_groups", "label": "浏览我的群组",
             "params": ["max_groups"]},
            {"type": "facebook_group_engage", "label": "群组互动",
             "params": ["group_name", "max_posts", "comment_probability"]},
            {"type": "facebook_extract_members", "label": "群成员候选采集",
             "params": ["group_name", "max_members"]},
            {"type": "facebook_group_member_greet", "label": "群成员好友打招呼",
             "params": ["target_groups", "max_friends_per_run",
                        "verification_note", "greeting"]},
            {"type": "facebook_add_friend", "label": "加好友(安全)",
             "params": ["target", "note", "safe_mode"]},
            {"type": "facebook_send_message", "label": "发消息",
             "params": ["target", "message"]},
            {"type": "facebook_check_inbox", "label": "Messenger 收件箱",
             "params": ["auto_reply", "max_conversations"]},
            {"type": "facebook_check_message_requests", "label": "陌生人收件箱",
             "params": ["max_requests"]},
            {"type": "facebook_check_friend_requests", "label": "好友请求处理",
             "params": ["accept_all", "max_requests"]},
            {"type": "facebook_campaign_run", "label": "剧本任务",
             "params": ["steps", "target_country", "target_groups",
                        "max_friends_per_run", "verification_note"]},
        ],
    },
    "linkedin": {
        "name": "LinkedIn",
        "package": "com.linkedin.android",
        "task_types": [
            {"type": "linkedin_send_message", "label": "发消息", "params": ["username", "message"]},
            {"type": "linkedin_read_messages", "label": "读消息", "params": ["username", "count"]},
            {"type": "linkedin_post_update", "label": "发动态", "params": ["content"]},
            {"type": "linkedin_search_profile", "label": "搜人脉", "params": ["query"]},
            {"type": "linkedin_send_connection", "label": "发邀请", "params": ["name", "note"]},
            {"type": "linkedin_accept_connections", "label": "接受邀请", "params": ["max_accept"]},
            {"type": "linkedin_like_post", "label": "点赞", "params": []},
            {"type": "linkedin_comment_post", "label": "评论", "params": ["comment"]},
        ],
    },
    "instagram": {
        "name": "Instagram",
        "package": "com.instagram.android",
        "task_types": [
            {"type": "instagram_browse_feed", "label": "浏览首页", "params": ["scroll_count", "like_probability"]},
            {"type": "instagram_browse_hashtag", "label": "浏览标签", "params": ["hashtag", "scroll_count"]},
            {"type": "instagram_search_leads", "label": "搜用户入库", "params": ["keyword", "max_leads"]},
            {"type": "instagram_send_dm", "label": "发私信", "params": ["recipient", "message"]},
        ],
    },
    "twitter": {
        "name": "X (Twitter)",
        "package": "com.twitter.android",
        "task_types": [
            {"type": "twitter_browse_timeline", "label": "浏览时间线", "params": ["scroll_count", "like_probability"]},
            {"type": "twitter_search_leads", "label": "搜用户入库", "params": ["keyword", "max_leads"]},
            {"type": "twitter_search_and_engage", "label": "关键词互动", "params": ["keyword", "max_tweets"]},
            {"type": "twitter_send_dm", "label": "发私信", "params": ["recipient", "message"]},
        ],
    },
}


def _load_roadmap() -> dict:
    try:
        if _ROADMAP_PATH.is_file():
            with open(_ROADMAP_PATH, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception as e:
        logger.debug("roadmap load: %s", e)
    return {}


@router.get("")
def list_platforms():
    """List all supported platforms with their task types."""
    return {
        "platforms": [
            {"id": pid, "name": p["name"], "package": p["package"],
             "task_count": len(p["task_types"])}
            for pid, p in PLATFORMS.items()
        ],
        "roadmap": _load_roadmap(),
    }


@router.get("/roadmap")
def get_growth_roadmap():
    """分阶段全球化路线（只读配置）。"""
    data = _load_roadmap()
    if not data:
        return {"phases": [], "note": "config/global_growth_roadmap.yaml 缺失或为空"}
    return data


@router.get("/tiktok/warmup-progress")
def tiktok_warmup_progress():
    """返回每台设备的 TikTok 养号进度（阶段、天数、观看数等）。"""
    PHASE_LABELS = {
        "cold_start": "冷启动期",
        "interest_building": "兴趣建立期",
        "active": "活跃期",
    }
    try:
        from ..device_state import get_device_state_store
        store = get_device_state_store("tiktok")
    except Exception:
        return {"devices": []}

    try:
        device_ids = store.list_devices()
    except Exception:
        return {"devices": []}

    # Load aliases for display names
    try:
        from .devices_core import _load_aliases
        aliases = _load_aliases()
    except Exception:
        aliases = {}

    today = store._today()
    devices = []
    for did in device_ids:
        # Skip internal pseudo-devices
        if did.startswith("__"):
            continue

        phase = store.get_phase(did)
        alias_info = aliases.get(did, {})
        number = alias_info.get("number")
        alias = alias_info.get("alias", "")
        if not alias and number is not None:
            alias = f"{str(number).zfill(2)}号"

        start_date = store.get(did, "start_date")
        last_updated = store.get(did, f"daily:{today}:sessions")
        # Compute last warmup timestamp from updated_at of a recent key
        last_warmup = store.get(did, "updated_at") or start_date

        devices.append({
            "device_id": did,
            "alias": alias,
            "phase": phase,
            "phase_label": PHASE_LABELS.get(phase, phase),
            "days_active": store.get_device_day(did),
            "videos_watched": store.get_int(did, "total_watched"),
            "follows_today": store.get_int(did, f"daily:{today}:followed"),
            "follows_total": store.get_int(did, "total_followed"),
            "chats_total": store.get_int(did, "total_dms_sent"),
            "algorithm_score": round(store.get_algorithm_learning_score(did), 3),
            "sessions_today": store.get_int(did, f"daily:{today}:sessions"),
            "start_date": start_date,
        })

    # Filter out test/dummy devices
    _TEST_PREFIXES = ("TEST_", "BAD", "BLOCKED", "DECAY", "FAIL", "MANUAL",
                      "PROG", "REC", "RISKY", "SLOW", "WARMUP")
    devices = [d for d in devices
               if not any(d["device_id"].startswith(p) for p in _TEST_PREFIXES)
               and len(d["device_id"]) > 10]

    # If no real local devices, proxy to Worker-03 via cluster
    if not devices:
        try:
            import json as _j
            import urllib.request as _ur
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            for h in coord._hosts.values():
                if h.online and h.host_ip:
                    url = f"http://{h.host_ip}:{h.port}/tiktok/warmup-progress"
                    try:
                        req = _ur.Request(url, method="GET")
                        resp = _ur.urlopen(req, timeout=8)
                        return _j.loads(resp.read().decode())
                    except Exception:
                        pass
        except Exception:
            pass

    # Sort by alias number (devices with a number first, then by device_id)
    devices.sort(key=lambda d: (d["alias"] or "zzz", d["device_id"]))
    return {"devices": devices}


@router.get("/{platform}")
def get_platform(platform: str):
    """Get platform details and available task types."""
    p = PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404, f"Unknown platform: {platform}")
    return {"platform": platform, **p}


@router.get("/{platform}/tasks")
def platform_task_types(platform: str):
    """List task types available for this platform."""
    p = PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404, f"Unknown platform: {platform}")
    return {"task_types": p["task_types"]}


@router.post("/{platform}/tasks")
def create_platform_task(platform: str, body: dict):
    """Create a task for a specific platform (unified entry)."""
    p = PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404, f"Unknown platform: {platform}")
    task_type = body.get("task_type", "")
    valid_types = [t["type"] for t in p["task_types"]]
    if task_type not in valid_types:
        raise HTTPException(400,
                            f"Invalid task_type '{task_type}' for {platform}. "
                            f"Valid: {valid_types}")
    device_id = body.get("device_id", "")
    from src.host.task_param_rules import maybe_normalize_for_task

    from src.host.task_origin import with_origin

    params = maybe_normalize_for_task(task_type, body.get("params") or {})
    params = with_origin(params, "platform_console")
    from ..api import task_store, get_worker_pool, run_task, _config_path
    # 检查设备是否在集群 Worker 上
    worker = _find_worker_for_device(device_id) if device_id else None
    if worker:
        # 转发到 Worker 执行
        return _dispatch_to_worker(worker, task_type, device_id, params, platform)
    task_id = task_store.create_task(
        task_type=task_type, device_id=device_id, params=params)
    pool = get_worker_pool()
    pool.submit(task_id, device_id or "default", run_task,
                task_id, _config_path)
    try:
        from ..api import _audit
        _audit("platform_task", device_id or "",
               f"{platform}/{task_type}")
    except Exception:
        pass
    return {"ok": True, "task_id": task_id, "platform": platform,
            "task_type": task_type}


@router.get("/{platform}/stats")
def platform_stats(platform: str, days: int = 7):
    """Get task statistics for a specific platform."""
    p = PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404, f"Unknown platform: {platform}")
    prefixes = [t["type"] for t in p["task_types"]]
    from ..api import task_store
    from datetime import datetime, timedelta
    all_tasks = task_store.list_tasks()
    cutoff = datetime.now() - timedelta(days=days)
    total = success = failed = 0
    for t in all_tasks:
        tt = t.get("task_type") or t.get("type", "")
        if tt not in prefixes:
            continue
        created = t.get("created_at", "")
        if created:
            try:
                dt = datetime.fromisoformat(
                    created.replace("Z", "+00:00"))
                if dt.replace(tzinfo=None) < cutoff:
                    continue
            except Exception:
                pass
        total += 1
        if t.get("status") == "completed":
            success += 1
        elif t.get("status") == "failed":
            failed += 1
    return {
        "platform": platform,
        "days": days,
        "total": total,
        "success": success,
        "failed": failed,
        "success_rate": round(success / max(1, total) * 100, 1),
    }


@router.post("/{platform}/batch")
def platform_batch(platform: str, body: dict):
    """Create tasks for all online devices on this platform."""
    p = PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404, f"Unknown platform: {platform}")
    task_type = body.get("task_type", "")
    valid_types = [t["type"] for t in p["task_types"]]
    if task_type not in valid_types:
        raise HTTPException(400, f"Invalid task_type for {platform}")
    from src.host.task_param_rules import maybe_normalize_for_task

    from src.host.task_origin import with_origin

    params = maybe_normalize_for_task(task_type, body.get("params") or {})
    params = with_origin(params, "platform_batch")
    device_ids = body.get("device_ids", [])
    from ..api import (task_store, get_worker_pool, run_task,
                       _config_path, get_device_manager)
    if not device_ids:
        # 本地设备（discover后取在线设备）
        manager = get_device_manager(_config_path)
        manager.discover_devices()
        device_ids = [d.device_id for d in manager.get_all_devices()
                      if d.is_online]
        # 合并集群设备
        try:
            from ..multi_host import get_cluster_coordinator
            coord = get_cluster_coordinator()
            for d in coord.get_all_devices():
                if d.get("device_id") not in device_ids and d.get("status") in ("connected", "online", "busy"):
                    device_ids.append(d["device_id"])
        except Exception:
            pass
    pool = get_worker_pool()
    created = 0
    for did in device_ids:
        worker = _find_worker_for_device(did)
        if worker:
            try:
                _dispatch_to_worker(worker, task_type, did, params, platform)
                created += 1
            except Exception:
                pass
        else:
            tid = task_store.create_task(
                task_type=task_type, device_id=did, params=params)
            pool.submit(tid, did, run_task, tid, _config_path)
            created += 1
    try:
        from ..api import _audit
        _audit("platform_batch", platform,
               f"type={task_type} count={created}")
    except Exception:
        pass
    return {"ok": True, "created": created, "platform": platform}


# ═══════════════════════════════════════════════════════════
# 通用设备网格 API — 所有平台共用
# ═══════════════════════════════════════════════════════════

@router.get("/{platform}/device-grid")
def platform_device_grid(platform: str):
    """通用设备为中心聚合视图 — 返回当前平台所有设备的状态、任务、统计。

    复用 TikTok device-grid 的设计理念，但对不同平台返回平台相关统计。
    """
    p = PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404, f"Unknown platform: {platform}")

    from ..api import task_store, get_device_manager, _config_path

    # 1. 获取设备列表 + 别名
    aliases = {}
    try:
        from .devices_core import _load_aliases
        aliases = _load_aliases()
    except Exception:
        pass

    manager = get_device_manager(_config_path)
    manager.discover_devices()
    local_devices = manager.get_all_devices()

    # 合并集群设备 + 远程别名
    cluster_devices = []
    remote_aliases = {}
    try:
        from ..multi_host import get_cluster_coordinator
        coord = get_cluster_coordinator()
        local_ids = {d.device_id for d in local_devices}
        for cd in coord.get_all_devices():
            if cd.get("device_id") not in local_ids:
                cluster_devices.append(cd)
        # 尝试获取 Worker 别名
        import json as _json, urllib.request as _ur
        for h in coord._hosts.values():
            if h.online and h.host_ip:
                try:
                    url = f"http://{h.host_ip}:{h.port}/devices/aliases"
                    req = _ur.Request(url, method="GET")
                    resp = _ur.urlopen(req, timeout=3)
                    remote_aliases.update(_json.loads(resp.read().decode()))
                except Exception:
                    pass
    except Exception:
        pass

    # 2. 获取运行中任务（按平台前缀过滤）
    prefixes = [t["type"] for t in p["task_types"]]
    all_tasks = task_store.list_tasks() or []
    running_tasks = {}  # device_id -> task info
    today_stats = {}    # device_id -> {任务类型计数}
    import time
    today_cutoff = time.time() - 86400

    for t in all_tasks:
        tt = t.get("task_type") or t.get("type", "")
        if tt not in prefixes:
            continue
        did = t.get("device_id", "")
        if not did:
            continue
        if t.get("status") == "running":
            running_tasks[did] = {"type": tt, "task_id": t.get("task_id", "")}
        created = t.get("created_at")
        if created:
            ts = created if isinstance(created, (int, float)) else 0
            if isinstance(created, str):
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(
                        created.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = 0
            if ts > today_cutoff:
                if did not in today_stats:
                    today_stats[did] = {"total": 0, "completed": 0, "failed": 0}
                today_stats[did]["total"] += 1
                if t.get("status") == "completed":
                    today_stats[did]["completed"] += 1
                elif t.get("status") == "failed":
                    today_stats[did]["failed"] += 1

    # 3. 尝试获取平台专属设备状态
    plat_states = {}
    try:
        from ..device_state import get_device_state_store
        ds = get_device_state_store(platform)
        today_key = ds._today()
        for did in (ds.list_devices() or []):
            plat_states[did] = ds.get_device_summary(did)
    except Exception:
        pass

    # 4. 组装设备列表
    devices = []
    online_count = 0
    configured_count = 0

    def _build_device(device_id, online, host_name=""):
        nonlocal online_count, configured_count
        alias_info = aliases.get(device_id, {})
        # 本地别名优先，其次尝试远程别名
        if not alias_info:
            ra = remote_aliases.get(device_id, {})
            if ra:
                alias_info = ra
        number = alias_info.get("number")
        alias = alias_info.get("alias", "")
        if not alias and number is not None:
            alias = f"{str(number).zfill(2)}号"
        elif not alias:
            alias = device_id[:8]

        if online:
            online_count += 1

        running = running_tasks.get(device_id)
        stats = today_stats.get(device_id, {})
        ps = plat_states.get(device_id, {})

        dev = {
            "device_id": device_id,
            "alias": alias,
            "online": online,
            "host": host_name or "主控",
            "running_task": running.get("type") if running else None,
            "today_tasks": stats.get("total", 0),
            "today_completed": stats.get("completed", 0),
            "today_failed": stats.get("failed", 0),
            "phase": ps.get("phase", ""),
            "algo_score": ps.get("algorithm_score", 0),
        }
        return dev

    for d in local_devices:
        online = d.is_online
        devices.append(_build_device(d.device_id, online, ""))

    for cd in cluster_devices:
        online = cd.get("status") in ("connected", "online", "busy")
        host = cd.get("host_name", cd.get("host_id", "")[:6] or "remote")
        devices.append(_build_device(cd["device_id"], online, host))

    # 排序：运行中 > 在线有任务 > 在线 > 离线
    def _sort_key(d):
        if d["running_task"]:
            return (3, d["alias"])
        if d["online"] and d["today_tasks"] > 0:
            return (2, d["alias"])
        if d["online"]:
            return (1, d["alias"])
        return (0, d["alias"])

    devices.sort(key=_sort_key, reverse=True)

    return {
        "platform": platform,
        "platform_name": p["name"],
        "devices": devices,
        "summary": {
            "total": len(devices),
            "online": online_count,
            "running_tasks": len(running_tasks),
        },
        "task_types": p["task_types"],
    }
