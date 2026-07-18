# -*- coding: utf-8 -*-
"""执行器：领任务后通过 DeviceManager + 各平台 Automation 在指定设备上执行。"""

import logging
import subprocess
import time

from src.utils.subprocess_text import run as _sp_run_text
import yaml
from pathlib import Path
from typing import Optional, List, Dict, Any

from src.device_control.device_manager import get_device_manager, DeviceManager
from src.app_automation.telegram import TelegramAutomation
from src.app_automation.whatsapp import WhatsAppAutomation
from src.app_automation.linkedin import LinkedInAutomation
from src.behavior.compliance_guard import get_compliance_guard, QuotaExceeded
from src.utils.retry import retry, run_with_timeout, TaskTimeout
from src.utils.log_config import set_task_context, clear_task_context
from src.host.health_monitor import metrics

from .task_store import (get_task, set_task_running, set_task_result,
                         create_task, update_task_progress)
from .device_registry import DEFAULT_DEVICES_YAML, PROJECT_ROOT, config_file, logs_dir

logger = logging.getLogger(__name__)

_project_root = PROJECT_ROOT


def _quota_exceeded_response(qe: QuotaExceeded) -> tuple[str, Dict[str, Any]]:
    """Build the dashboard-friendly quota message/meta used by FB tasks."""
    eta_sec = 0.0
    try:
        from src.behavior.compliance_guard import get_compliance_guard as _get_guard
        eta_sec = _get_guard().get_next_slot_eta(
            qe.platform, qe.action, qe.account)
    except Exception:
        pass
    eta_min = max(1, int((eta_sec + 30) // 60)) if eta_sec > 0 else 0
    eta_hint = f", 约 {eta_min} 分钟后可派" if eta_min > 0 else ""
    msg = (f"[quota] {qe.platform}/{qe.action} 已达 {qe.window} 上限 "
           f"({qe.current}/{qe.limit}){eta_hint}.")
    meta = {"quota": {"platform": qe.platform, "action": qe.action,
                      "window": qe.window, "current": qe.current,
                      "limit": qe.limit, "eta_seconds": int(eta_sec),
                      "eta_minutes": eta_min}}
    return msg, meta


def _phase10_task_extras(params: Dict[str, Any]) -> Dict[str, Any]:
    """Phase 10/10.2/10.3 opt-in kwargs 组装 (walk_candidates / l2_gate_shots /
    do_l2_gate / max_l2_calls).

    只在显式 True/>1/非默认 时加 key, 避免未升级的 automation 分支撞 TypeError.
    """
    out: Dict[str, Any] = {}
    if params.get("walk_candidates"):
        out["walk_candidates"] = True
    shots = int(params.get("l2_gate_shots", 0) or 0)
    if shots > 1:
        out["l2_gate_shots"] = shots
    if params.get("do_l2_gate"):
        out["do_l2_gate"] = True
    if params.get("strict_persona_gate") or params.get("require_high_match"):
        out["strict_persona_gate"] = True
    budget = int(params.get("max_l2_calls", 0) or 0)
    # walk 启用时才传 budget, 且非默认 3 才值得发 kwarg.
    if out.get("walk_candidates") and budget and budget != 3:
        out["max_l2_calls"] = budget
    return out


def _fb_filter_high_match_targets(targets: Any, params: Dict[str, Any]) -> tuple[List[Any], Dict[str, Any]]:
    """Filter manual name-hunter targets before any add/greet UI action.

    ``require_high_match`` means two gates:
      1. seed gate here: only names with seed_score >= min_seed_score are searched.
      2. profile gate later: ``strict_persona_gate`` forces L2 profile match before touch.
    Targets without seed_score are kept for non-manual sources (e.g. extracted members).
    """
    rows = list(targets or [])
    if not params.get("require_high_match"):
        return rows, {"enabled": False, "input": len(rows), "kept": len(rows), "skipped": 0}
    min_score = float(params.get("min_seed_score") or 80)
    kept: List[Any] = []
    skipped: List[Dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        if item.get("seed_score") is None:
            kept.append(item)
            continue
        try:
            seed_score = float(item.get("seed_score") or 0)
        except Exception:
            seed_score = 0.0
        if seed_score >= min_score:
            kept.append(item)
        else:
            skipped.append({
                "name": item.get("name") or "",
                "seed_score": seed_score,
                "reason": "seed_score_below_high_match_threshold",
            })
    return kept, {
        "enabled": True,
        "input": len(rows),
        "kept": len(kept),
        "skipped": len(skipped),
        "min_seed_score": min_score,
        "skipped_targets": skipped[:20],
    }


def _vpn_required() -> bool:
    """读取 config/devices.yaml 中 connection.vpn_required (默认 True)。"""
    try:
        cfg_path = Path(DEFAULT_DEVICES_YAML)
        with open(cfg_path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return bool((data.get("connection") or {}).get("vpn_required", True))
    except Exception:
        return True


def _check_network(device_id: str) -> tuple:
    """检查设备网络连通性（外网可达性）。返回 (ok: bool, msg: str)。与 preflight 同源。"""
    try:
        from src.host.preflight import check_device_network_connectivity

        return check_device_network_connectivity(device_id)
    except Exception as e:
        logger.warning("[Network] %s: 检查异常: %s", device_id[:12], e)
        return False, f"网络检查异常: {e}"


def _ensure_vpn(device_id: str) -> tuple:
    """检查VPN并尝试静默重连。返回 (ok: bool, msg: str)。
    当 config/devices.yaml vpn_required=true 时，VPN失败则中止任务。
    当 vpn_required=false 时，仅警告，任务继续执行。
    """
    try:
        from src.behavior.vpn_manager import check_vpn_status, reconnect_vpn_silent
        vpn_s = check_vpn_status(device_id)
        if vpn_s.connected:
            return True, "connected"
        logger.info("[VPN] %s: VPN 未连接，尝试静默重连...", device_id[:12])
        ok = reconnect_vpn_silent(device_id)
        if ok:
            logger.info("[VPN] %s: 静默重连成功", device_id[:12])
            return True, "reconnected"
        if _vpn_required():
            logger.error("[VPN] %s: VPN 未连接且重连失败，任务中止", device_id[:12])
            return False, f"VPN 未连接（{device_id[:8]}），重连失败，任务已中止。请手动检查 V2RayNG"
        logger.warning("[VPN] %s: VPN 未连接，vpn_required=false，继续执行任务", device_id[:12])
        return True, f"vpn_not_connected_but_optional ({device_id[:8]})"
    except Exception as e:
        logger.warning("[VPN] %s: VPN 检查异常: %s，跳过检查继续执行", device_id[:12], e)
        return True, f"check_skipped ({e})"


def _ensure_italy_ip(device_id: str) -> tuple:
    """P4-C: 验证设备 IP 是否在意大利。失败时 fail-open（不阻断任务），只有明确检测到非意大利时才中止。"""
    try:
        from src.behavior.geo_check import check_device_geo
        result = check_device_geo(device_id, "italy")
        if result.error:
            # 无法完成检查 → fail-open
            logger.debug("[GEO] %s: IP 检查跳过 (%s)", device_id[:12], result.error)
            return True, f"geo_check_skipped ({result.error})"
        if result.matches:
            logger.info("[GEO] %s: IP=%s 国家=%s ✓ 符合意大利",
                        device_id[:12], result.public_ip, result.detected_country)
            return True, f"geo_ok ({result.detected_country})"
        # 明确检测到非意大利
        logger.warning("[GEO] %s: IP 不在意大利！IP=%s 国家=%s，任务中止",
                       device_id[:12], result.public_ip, result.detected_country)
        return False, (f"IP 国家不匹配：期望意大利，实际 {result.detected_country}（{result.public_ip}）。"
                       f"请检查 VPN 节点是否为意大利出口。")
    except Exception as e:
        logger.debug("[GEO] %s: geo 检查异常 %s，跳过", device_id[:12], e)
        return True, f"geo_check_skipped ({e})"


TASK_TIMEOUT_SEC = 120
TIKTOK_TIMEOUT_SEC = 3600

# Per-task-type timeouts (seconds) — override TIKTOK_TIMEOUT_SEC
_TASK_TYPE_TIMEOUTS = {
    "tiktok_warmup": 3600,                         # 60 min max (interest_building phase can take 50m)
    "tiktok_browse_feed": 900,                     # 15 min max
    "tiktok_follow": 900,                          # 15 min max
    "tiktok_check_inbox": 1200,                    # 20 min max (50 conversations × ~15s each)
    "tiktok_chat": 600,                            # 10 min max
    "tiktok_check_and_chat_followbacks": 600,      # 10 min max (same as tiktok_chat)
    "tiktok_test_follow": 120,                     # 2 min max
    "tiktok_follow_up": 600,                       # 10 min max (30 leads × ~20s each)
    "tiktok_auto": 7200,                           # 120 min max (warmup 50m + follow + extra warmup + chat)
    "tiktok_send_dm": 120,                         # 2 min max
    "tiktok_status": 60,                           # 1 min max
    "tiktok_scan_username": 120,                   # 2 min max
    "tiktok_follow_user": 300,                     # 5 min max
    "tiktok_interact_user": 600,                   # 10 min max
    "tiktok_keyword_search": 1200,             # 20 min (keyword search + comment warmup)
    "tiktok_live_engage": 900,                  # 15 min (live stream engagement)
    # ★ P0 新增任务类型
    "tiktok_contact_discovery": 600,            # 10 min (通讯录好友发现)
    "tiktok_drip_followup": 120,                # 2 min (drip campaign 跟进)
    "contacts_sync_leads": 60,                  # 1 min (通讯录→LeadsStore 同步)
    "tiktok_priority_outreach": 300,            # 5 min (P3-2 高分Lead优先触达)
    # ── Facebook 任务超时 (Sprint 3 真机测试发现 默认 120s 不够) ──
    # browse_feed: 每次 scroll 含风控检测 + hierarchy dump,真机平均 30s/scroll
    # join_group / extract_members: 含搜索 + 等待加载,大概 5-8 分钟
    # campaign_run: 完整养号→入群→提取→好友→DM 一条龙
    "facebook_browse_feed": 1200,               # 20 min (真机 VPN 延迟下 20scroll×40-50s=800-1000s + buffer)
    # 多 topic deep-link + 分段 scroll，略高于普通 browse_feed
    "facebook_browse_feed_by_interest": 1200,  # 20 min (4 topic × 分段 scroll + VPN 延迟 buffer)
    "facebook_profile_hunt": 3600,              # 60 min (100 候选 × 30s 上限)
    "facebook_name_hunter_prescreen": 3600,     # 60 min (search + profile classify only)
    "facebook_name_hunter_touch_qualified": 3600,  # 60 min (qualified candidate outreach)
    "facebook_join_group": 600,                 # 10 min
    "facebook_browse_groups": 900,              # 15 min
    "facebook_group_engage": 1200,              # 20 min
    "facebook_extract_members": 1500,           # 25 min (一个群提 30 成员)
    "facebook_group_member_greet": 7200,        # 120 min (群成员筛选→加好友→打招呼)
    "facebook_search_leads": 1200,              # 20 min
    "facebook_add_friend": 300,                 # 5 min
    "facebook_add_friend_and_greet": 480,       # 8 min (加好友 + profile 页打招呼一体)
    "facebook_send_greeting": 420,              # 7 min (search → profile → Message → 发送)
    "facebook_send_message": 300,               # 5 min
    "facebook_check_inbox": 900,                # 15 min
    "facebook_check_message_requests": 600,     # 10 min
    "facebook_check_friend_requests": 600,      # 10 min
    "facebook_campaign_run": 7200,              # 120 min (一条龙)
}


def _smart_timeout(task_type: str, device_id: str = "") -> int:
    """2026-05-08 P1-2: 智能超时 — 查最近 20 次同类型成功任务的 P95 耗时。

    公式: timeout = max(静态值, P95 × 1.5 + 60s buffer)
    无历史数据时回退到静态表。避免长尾任务误杀正常执行。
    """
    static = _TASK_TYPE_TIMEOUTS.get(
        task_type,
        TIKTOK_TIMEOUT_SEC if task_type.startswith("tiktok_") else TASK_TIMEOUT_SEC,
    )
    try:
        from .database import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                "SELECT CAST((julianday(updated_at) - julianday(created_at)) * 86400 AS INTEGER) "
                "AS duration_sec FROM tasks "
                "WHERE type=? AND status='completed' AND updated_at IS NOT NULL "
                "AND created_at IS NOT NULL "
                "ORDER BY updated_at DESC LIMIT 20",
                (task_type,),
            ).fetchall()
        if len(rows) >= 5:
            durations = sorted(r["duration_sec"] for r in rows if r["duration_sec"] and r["duration_sec"] > 0)
            if durations:
                p95_idx = min(len(durations) - 1, int(len(durations) * 0.95))
                p95 = durations[p95_idx]
                smart = int(p95 * 1.5) + 60
                result = max(static, smart)
                if result != static:
                    logger.debug("[smart_timeout] %s: static=%ds, P95=%ds, smart=%ds → %ds",
                                 task_type, static, p95, smart, result)
                return result
    except Exception:
        pass
    return static


def _resolve_serial_from_config(config_path: str, device_id: str) -> str:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        devices = (config or {}).get("devices") or {}
        if device_id in devices and isinstance(devices[device_id], dict):
            return (devices[device_id].get("device_id") or device_id)
    except Exception:
        pass
    return device_id


def _get_device_id(manager: DeviceManager, preferred: Optional[str],
                   config_path: str, task_type: str = "") -> Optional[str]:
    connected = manager.get_connected_devices()
    if not connected:
        return None

    if preferred:
        serial = _resolve_serial_from_config(config_path, preferred)
        for d in connected:
            if d.device_id == serial or d.device_id == preferred or d.display_name == preferred:
                if metrics.is_isolated(d.device_id):
                    logger.warning("指定设备 %s 已被隔离，仍然使用", preferred[:8])
                return d.device_id
        logger.warning("指定设备 %s 当前不可用，拒绝自动切换到其他设备",
                       preferred[:12])
        return None

    try:
        from .smart_scheduler import get_smart_scheduler
        scheduler = get_smart_scheduler()
        best = scheduler.select_device(task_type, preferred=None)
        if best:
            return best
    except Exception:
        pass

    available = [d for d in connected if not metrics.is_isolated(d.device_id)]
    if not available:
        available = connected
    return available[0].device_id


def _fresh_telegram(manager, resolved) -> TelegramAutomation:
    """创建 TelegramAutomation 实例并重启 app 确保干净状态"""
    telegram = TelegramAutomation(manager)
    telegram.set_current_device(resolved)
    d = manager.get_u2(resolved)
    if d:
        d.app_stop("org.telegram.messenger")
        time.sleep(1)
    return telegram


@retry(max_attempts=2, delay=3.0, backoff=1.5,
       exceptions=(ConnectionError, OSError, TimeoutError, subprocess.TimeoutExpired,
                   BrokenPipeError, IOError),
       on_retry=lambda a, e: logger.info("任务重试 attempt %d: %s", a, e))
def _execute_with_retry(manager, resolved, task_type, params, config_path=None, _task_id: str = ""):
    """带重试的任务执行入口。"""
    # 在新线程中重新设置 task_context，以便 _make_progress_cb 能读取到 task_id
    if _task_id:
        try:
            set_task_context(task_id=_task_id, device_id=resolved)
        except Exception:
            pass

    # ★ P0 新增: 通讯录→LeadsStore 同步（不需要 UI 操作，只读 DB）
    if task_type == "contacts_sync_leads":
        try:
            from src.app_automation.contacts_manager import get_discovery_results
            from src.workflow.platform_actions_bridge import _sync_contact_discoveries_to_leads
            results = get_discovery_results(device_id=resolved)
            synced = _sync_contact_discoveries_to_leads(
                device_id=resolved,
                discovered_names=[r["tiktok_username"] for r in results
                                  if r.get("tiktok_username")],
                source="contacts_sync_periodic",
            )
            return True, "", {"synced": synced, "total_discovered": len(results)}
        except Exception as e:
            return False, f"通讯录同步失败: {e}", None

    if task_type == "telegram_send_message":
        username = params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not username:
            return False, "params.username 或 params.target 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(username, resolved):
            return False, f"搜索用户 {username} 失败", None
        ok = tg.send_text_message(message, resolved)
        return ok, ("" if ok else "消息发送失败"), None

    elif task_type == "telegram_read_messages":
        username = params.get("username") or params.get("target") or ""
        count = params.get("count", 20)
        if not username:
            return False, "params.username 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(username, resolved):
            return False, f"搜索用户 {username} 失败", None
        msgs = tg.read_messages(resolved, count=count)
        # 去掉 raw 字段（太长），返回干净的消息列表
        clean = [{k: v for k, v in m.items() if k != "raw"} for m in msgs]
        return True, "", {"messages": clean, "count": len(clean)}

    elif task_type == "telegram_send_file":
        username = params.get("username") or params.get("target") or ""
        file_path = params.get("file_path", "")
        caption = params.get("caption", "")
        if not username:
            return False, "params.username 必填", None
        if not file_path:
            return False, "params.file_path 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(username, resolved):
            return False, f"搜索用户 {username} 失败", None
        ok = tg.send_file(file_path, resolved, caption=caption)
        return ok, ("" if ok else "文件发送失败"), None

    elif task_type == "telegram_workflow":
        username = params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not username:
            return False, "params.username 必填", None
        tg = _fresh_telegram(manager, resolved)
        include_screenshot = params.get("include_screenshot", True)
        ok = tg.complete_workflow(username, message, include_screenshot=include_screenshot)
        return ok, ("" if ok else "workflow 执行失败"), None

    elif task_type == "whatsapp_send_message":
        contact = params.get("contact") or params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not contact:
            return False, "params.contact 必填", None
        wa = WhatsAppAutomation(manager)
        wa.set_current_device(resolved)
        d = manager.get_u2(resolved)
        if d:
            d.app_stop("com.whatsapp")
            time.sleep(1)
        if not wa.search_and_open_user(contact, resolved):
            return False, f"搜索 WhatsApp 联系人 {contact} 失败", None
        ok = wa.send_text_message(message, resolved)
        return ok, ("" if ok else "WhatsApp 消息发送失败"), None

    elif task_type == "whatsapp_read_messages":
        contact = params.get("contact") or params.get("username") or params.get("target") or ""
        count = params.get("count", 20)
        if not contact:
            return False, "params.contact 必填", None
        wa = WhatsAppAutomation(manager)
        wa.set_current_device(resolved)
        d = manager.get_u2(resolved)
        if d:
            d.app_stop("com.whatsapp")
            time.sleep(1)
        if not wa.search_and_open_user(contact, resolved):
            return False, f"搜索 WhatsApp 联系人 {contact} 失败", None
        msgs = wa.read_messages(resolved, count=count)
        return True, "", {"messages": msgs, "count": len(msgs)}

    elif task_type == "linkedin_send_message":
        recipient = params.get("recipient") or params.get("username") or params.get("target") or ""
        message = params.get("message", "")
        if not recipient:
            return False, "params.recipient 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        ok = li.send_message(recipient, message, resolved)
        return ok, ("" if ok else "LinkedIn 消息发送失败（可能对方不是1度连接）"), None

    elif task_type == "linkedin_read_messages":
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        msgs = li.read_messages(resolved, count=params.get("count", 20))
        return True, "", {"messages": msgs, "count": len(msgs)}

    elif task_type == "linkedin_post_update":
        content = params.get("content") or params.get("message") or ""
        if not content:
            return False, "params.content 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        ok = li.post_update(content, resolved)
        return ok, ("" if ok else "LinkedIn 动态发布失败"), None

    elif task_type == "linkedin_search_profile":
        query = params.get("query") or params.get("name") or ""
        if not query:
            return False, "params.query 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        profiles = li.search_profiles(query, resolved, max_results=params.get("max_results", 10))
        return True, "", {"profiles": profiles, "count": len(profiles)}

    elif task_type == "linkedin_send_connection":
        name = params.get("name") or params.get("query") or params.get("target") or ""
        note = params.get("note", "")
        if not name:
            return False, "params.name 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        ok = li.send_connection_request(name, resolved, note=note)
        return ok, ("" if ok else "LinkedIn 连接请求发送失败"), None

    elif task_type == "linkedin_accept_connections":
        max_accept = params.get("max_accept", 10)
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        count = li.accept_connections(resolved, max_accept=max_accept)
        return True, "", {"accepted_count": count}

    elif task_type == "telegram_switch_account":
        account_name = params.get("account_name", "")
        if not account_name:
            return False, "params.account_name 必填", None
        tg = TelegramAutomation(manager)
        tg.set_current_device(resolved)
        ok = tg.switch_account(account_name, resolved)
        return ok, ("" if ok else f"切换到账号 {account_name} 失败"), {"account": account_name}

    elif task_type == "telegram_list_accounts":
        tg = TelegramAutomation(manager)
        tg.set_current_device(resolved)
        accounts = tg.list_accounts(resolved)
        return True, "", {"accounts": accounts}

    elif task_type == "telegram_forward":
        from_user = params.get("from_user", "")
        to_user = params.get("to_user", "")
        count = params.get("count", 1)
        if not from_user or not to_user:
            return False, "params.from_user 和 params.to_user 必填", None
        tg = _fresh_telegram(manager, resolved)
        if not tg.search_and_open_user(from_user, resolved):
            return False, f"搜索用户 {from_user} 失败", None
        return True, "", {"forwarded": 0, "note": "forward功能开发中"}

    elif task_type == "linkedin_like_post":
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        guard = get_compliance_guard()
        try:
            guard.check_and_record("linkedin", "like_post")
        except QuotaExceeded as e:
            return False, str(e), None
        return True, "", {"note": "like_post 功能开发中"}

    elif task_type == "linkedin_comment_post":
        comment = params.get("comment", "")
        if not comment:
            return False, "params.comment 必填", None
        li = LinkedInAutomation(manager)
        li.set_current_device(resolved)
        li.start_app(resolved)
        guard = get_compliance_guard()
        try:
            guard.check_and_record("linkedin", "comment_post")
        except QuotaExceeded as e:
            return False, str(e), None
        return True, "", {"note": "comment_post 功能开发中"}

    elif task_type.startswith("tiktok_"):
        return _execute_tiktok(manager, resolved, task_type, params)

    elif task_type == "batch_send":
        return _execute_batch_send(manager, resolved, params, config_path)

    elif task_type.startswith("facebook_"):
        return _execute_facebook(manager, resolved, task_type, params)

    elif task_type == "whatsapp_auto_reply":
        wa = _fresh_whatsapp(manager, resolved)
        duration = params.get("duration", 120)
        ok = wa.auto_reply_chat(resolved, duration=duration)
        return ok, ("" if ok else "WhatsApp 自动回复失败"), None

    elif task_type == "whatsapp_send_media":
        wa = _fresh_whatsapp(manager, resolved)
        contact = params.get("contact") or params.get("target", "")
        media_path = params.get("media_path", "")
        if not contact or not media_path:
            return False, "params.contact 和 params.media_path 必填", None
        if not wa.search_and_open_user(contact, resolved):
            return False, f"搜索联系人 {contact} 失败", None
        ok = wa.send_media(media_path, resolved)
        return ok, ("" if ok else "发送媒体失败"), None

    elif task_type == "whatsapp_list_chats":
        wa = _fresh_whatsapp(manager, resolved)
        chats = wa.list_chats(resolved)
        return True, "", {"chats": chats, "count": len(chats)}

    elif task_type == "telegram_auto_reply":
        tg = _fresh_telegram(manager, resolved)
        duration = params.get("duration", 120)
        ok = tg.auto_reply_monitor(resolved, duration=duration)
        return ok, ("" if ok else "Telegram 自动回复失败"), None

    elif task_type == "telegram_join_group":
        tg = _fresh_telegram(manager, resolved)
        group = params.get("group") or params.get("group_name", "")
        if not group:
            return False, "params.group 必填", None
        ok = tg.join_group(group, resolved)
        return ok, ("" if ok else "加入群组失败"), None

    elif task_type == "telegram_send_group":
        tg = _fresh_telegram(manager, resolved)
        group = params.get("group") or params.get("group_name", "")
        message = params.get("message", "")
        if not group:
            return False, "params.group 必填", None
        ok = tg.send_group_message(group, message, resolved)
        return ok, ("" if ok else "群组消息发送失败"), None

    elif task_type == "telegram_monitor_chat":
        tg = _fresh_telegram(manager, resolved)
        username = params.get("username") or params.get("target", "")
        duration = params.get("duration", 120)
        if not username:
            return False, "params.username 必填", None
        ok = tg.monitor_chat(username, resolved, duration=duration)
        return ok, ("" if ok else "监控聊天失败"), None

    elif task_type == "instagram_browse_feed":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        st = ig.browse_feed(
            scroll_count=int(params.get("scroll_count", 8)),
            like_probability=float(params.get("like_probability", 0.2)),
            device_id=resolved,
        )
        return True, "", {"browse_stats": st}

    elif task_type == "instagram_search_leads":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        q = params.get("keyword") or params.get("query", "")
        if not q:
            return False, "params.keyword 或 params.query 必填", None
        ids = ig.search_and_collect_leads(
            q, device_id=resolved, max_leads=int(params.get("max_leads", 10)))
        return True, "", {"lead_ids": ids, "count": len(ids)}

    elif task_type == "instagram_send_dm":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        to = params.get("recipient") or params.get("username", "")
        msg = params.get("message", "")
        if not to or not msg:
            return False, "params.recipient 与 params.message 必填", None
        ok = ig.send_dm(to, msg, device_id=resolved)
        return ok, ("" if ok else "Instagram DM 失败"), None

    elif task_type == "instagram_browse_hashtag":
        ig = _fresh_instagram(manager, resolved)
        if not ig.launch(resolved):
            return False, "Instagram 启动失败", None
        tag = params.get("hashtag") or params.get("keyword", "")
        if not tag:
            return False, "params.hashtag 必填", None
        st = ig.browse_hashtag(
            tag,
            scroll_count=int(params.get("scroll_count", 5)),
            like_probability=float(params.get("like_probability", 0.25)),
            device_id=resolved,
        )
        return True, "", {"browse_stats": st}

    elif task_type == "twitter_browse_timeline":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        st = tw.browse_timeline(
            scroll_count=int(params.get("scroll_count", 8)),
            like_probability=float(params.get("like_probability", 0.2)),
            retweet_probability=float(params.get("retweet_probability", 0.05)),
            device_id=resolved,
        )
        return True, "", {"browse_stats": st}

    elif task_type == "twitter_search_leads":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        q = params.get("keyword") or params.get("query", "")
        if not q:
            return False, "params.keyword 或 params.query 必填", None
        ids = tw.search_and_collect_leads(
            q, device_id=resolved, max_leads=int(params.get("max_leads", 10)))
        return True, "", {"lead_ids": ids, "count": len(ids)}

    elif task_type == "twitter_search_and_engage":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        kw = params.get("keyword", "")
        if not kw:
            return False, "params.keyword 必填", None
        st = tw.search_and_engage(
            kw,
            max_tweets=int(params.get("max_tweets", 5)),
            reply_probability=float(params.get("reply_probability", 0.3)),
            like_probability=float(params.get("like_probability", 0.5)),
            device_id=resolved,
        )
        return True, "", {"engage_stats": st}

    elif task_type == "twitter_send_dm":
        tw = _fresh_twitter(manager, resolved)
        if not tw.launch(resolved):
            return False, "X/Twitter 启动失败", None
        to = params.get("recipient") or params.get("username", "")
        msg = params.get("message", "")
        if not to or not msg:
            return False, "params.recipient 与 params.message 必填", None
        ok = tw.send_dm(to, msg, device_id=resolved)
        return ok, ("" if ok else "Twitter DM 失败"), None

    elif task_type == "proxy_pool_sync":
        # Phase 7 P0: 922S5 代理池定时巡检（无需设备）
        try:
            from src.device_control.proxy_pool_manager import run_proxy_pool_sync
            result = run_proxy_pool_sync(params)
            return True, "", result
        except Exception as e:
            logger.error("[Executor] proxy_pool_sync 失败: %s", e)
            return False, f"代理池巡检失败: {e}", None

    else:
        return False, f"不支持的任务类型: {task_type}", None


def _fresh_tiktok(manager, resolved):
    """Create TikTokAutomation instance (lazy import to avoid circular deps)."""
    from src.app_automation.tiktok import TikTokAutomation
    tt = TikTokAutomation(device_manager=manager)
    tt.set_current_device(resolved)
    return tt


def _fresh_facebook(manager, resolved):
    from src.app_automation.facebook import FacebookAutomation
    fb = FacebookAutomation(manager)
    fb.set_current_device(resolved)
    fb.launch(resolved)
    return fb


def _execute_facebook(manager, resolved, task_type, params):
    """Facebook 任务统一分发器 — 处理所有 facebook_* 任务类型。

    设计目标:
      - 与 TikTok 的 _execute_tiktok 风格对齐;
      - 单点封装 launch + 异常包装,避免每个 task_type 重复样板;
      - 支持 facebook_campaign_run 串行多步骤剧本（5 套预设的服务端实现）。
    """
    fb = _fresh_facebook(manager, resolved)

    try:
        from src.host.fb_add_friend_gate import check_add_friend_gate as _fb_add_friend_gate

        if task_type == "facebook_browse_feed":
            # P0-1: duration 是主驱动（每分钟 X 屏），scroll_count 仍可显式覆盖
            # P1-2: 读账号 phase，自动按 playbook 档位调整节奏
            duration = int(params.get("duration") or params.get("duration_minutes") or 15)
            scroll_count = params.get("scroll_count")
            like_prob = params.get("like_probability")
            phase = params.get("phase")
            if not phase:
                try:
                    from src.host.fb_account_phase import get_phase as _fb_get_phase
                    phase = (_fb_get_phase(resolved) or {}).get("phase") or "cold_start"
                except Exception:
                    phase = "cold_start"
            try:
                from src.app_automation.facebook import FbWarmupError
                stats = fb.browse_feed(
                    scroll_count=int(scroll_count) if scroll_count else None,
                    like_probability=float(like_prob) if like_prob is not None else None,
                    duration_minutes=duration,
                    phase=phase,
                    device_id=resolved,
                )
                return True, "", stats
            except FbWarmupError as e:
                return False, str(e), {
                    "card_type": "fb_warmup",
                    "phase": phase,
                    "error_code": e.code,
                    "error_hint": e.hint,
                    "scrolls": 0,
                    "duration_minutes": duration,
                }

        if task_type == "facebook_browse_feed_by_interest":
            # Sprint F: 读 fb_content_exposure 热榜 → m.facebook.com/search deep-link
            # → 分段 scroll+like（MIUI 不依赖 uiautomator dump）；无数据时退化为 browse_feed
            duration = int(params.get("duration") or params.get("duration_minutes") or 15)
            phase = params.get("phase")
            if not phase:
                try:
                    from src.host.fb_account_phase import get_phase as _fb_get_phase
                    phase = (_fb_get_phase(resolved) or {}).get("phase") or "cold_start"
                except Exception:
                    phase = "cold_start"
            if not hasattr(fb, "browse_feed_by_interest"):
                return False, "facebook.browse_feed_by_interest 尚未实现", None
            try:
                from src.app_automation.facebook import FbWarmupError
                stats = fb.browse_feed_by_interest(
                    persona_key=params.get("persona_key") or "",
                    interest_hours=int(params.get("interest_hours", 168)),
                    max_topics=int(params.get("max_topics", 4)),
                    like_boost=float(params.get("like_boost", 0.12)),
                    scroll_count=(int(params["scroll_count"])
                                  if params.get("scroll_count") else None),
                    duration_minutes=duration,
                    phase=phase,
                    device_id=resolved,
                )
                return True, "", stats
            except FbWarmupError as e:
                return False, str(e), {
                    "card_type": "fb_interest_feed",
                    "phase": phase,
                    "error_code": e.code,
                    "error_hint": e.hint,
                    "fallback_no_topics": False,
                    "duration_minutes": duration,
                }

        if task_type == "facebook_send_message":
            target = params.get("target") or params.get("username") or params.get("recipient", "")
            message = params.get("message", "")
            if not target:
                return False, "params.target 必填", None
            ok = fb.send_message(target, message, device_id=resolved)
            return ok, ("" if ok else "Facebook 消息发送失败"), None

        if task_type == "facebook_add_friend":
            target = params.get("target") or params.get("username") or params.get("profile_name", "")
            if not target:
                return False, "params.target 必填", None
            _gate_err, _gate_meta = _fb_add_friend_gate(resolved, params)
            if _gate_err:
                return False, _gate_err, _gate_meta
            note = params.get("note") or params.get("verification_note", "")
            safe_mode = params.get("safe_mode", True)
            # P0-2: 透传 persona_key 让 add_friend_with_note 空 note 时按 persona
            # 自动填日文/意大利文验证语；并把 phase override 带过去（router 层可能预计算）。
            _persona_key = params.get("persona_key") or ""
            _phase_override = params.get("phase") or params.get("phase_override") or ""
            if hasattr(fb, "add_friend_with_note"):
                _extra = _phase10_task_extras(params)
                try:
                    from src.host.fb_playbook import local_rules_disabled
                    _force_add = local_rules_disabled() or bool(params.get("force_add_friend"))
                except Exception:
                    _force_add = bool(params.get("force_add_friend"))
                ok = fb.add_friend_with_note(target, note=note,
                                             safe_mode=safe_mode,
                                             device_id=resolved,
                                             persona_key=_persona_key or None,
                                             phase=_phase_override or None,
                                             source=params.get("source", "") or params.get("group_name", ""),
                                             preset_key=(params.get("_preset_key", "") or params.get("preset_key", "")),
                                             force=_force_add,
                                             **_extra)
            else:
                ok = fb.add_friend(target, device_id=resolved)

            # 2026-04-23 P3-1: add_friend_with_note 内部已在锁内 record,
            # executor 层不再重复写入。仅失败时补一条细分状态记录供分析,
            # 因为 automation 失败路径不会 record(它只在 UI 成功时写 sent)。
            # 2026-05-11: 细分 risk → failed_no_button / failed_persona / risk
            if not ok:
                try:
                    from src.host.fb_store import record_friend_request
                    source = params.get("source", "") or params.get("group_name", "")
                    preset_key = (params.get("_preset_key", "") or
                                  params.get("preset_key", ""))
                    # 从 automation 层获取最后失败原因 (如有)
                    _fail_status = "risk"
                    try:
                        _last_reason = getattr(fb, "_last_greet_skip_reason", "") or ""
                        if "persona" in _last_reason or "l1" in _last_reason:
                            _fail_status = "failed_persona"
                        elif "no_add_button" in _last_reason or "无加好友按钮" in _last_reason:
                            _fail_status = "failed_no_button"
                    except Exception:
                        pass
                    record_friend_request(
                        resolved, target,
                        note=note,
                        source=source,
                        status=_fail_status,
                        preset_key=preset_key,
                    )
                except Exception:
                    pass
            return ok, ("" if ok else "添加好友失败"), None

        # 2026-04-23: 搜索 → 加好友 → 打招呼(一体化,方案 A2)
        if task_type == "facebook_add_friend_and_greet":
            target = (params.get("target") or params.get("username")
                      or params.get("profile_name", ""))
            if not target:
                return False, "params.target 必填", None
            # 先过加好友 gate（phase / daily_cap）
            _gate_err, _gate_meta = _fb_add_friend_gate(resolved, params)
            if _gate_err:
                return False, _gate_err, _gate_meta
            note = params.get("note") or params.get("verification_note", "")
            greeting = params.get("greeting") or params.get("greeting_message", "")
            _persona_key = params.get("persona_key") or ""
            _phase_override = params.get("phase") or params.get("phase_override") or ""
            _preset_key = (params.get("_preset_key", "")
                           or params.get("preset_key", ""))
            if not hasattr(fb, "add_friend_and_greet"):
                return False, "facebook.add_friend_and_greet 尚未实现", None
            # 把 source / preset_key 下推,automation 层锁内 record 时用
            _src_val = params.get("source", "") or params.get("group_name", "")
            _extra = _phase10_task_extras(params)
            try:
                from src.host.fb_playbook import local_rules_disabled
                _relaxed_local_rules = local_rules_disabled()
            except Exception:
                _relaxed_local_rules = False
            if params.get("force_add_friend") or _relaxed_local_rules:
                _extra["force"] = True
            if params.get("ai_dynamic_greeting") is not None:
                _extra["ai_dynamic_greeting"] = bool(params.get("ai_dynamic_greeting"))
            if params.get("force_send_greeting") is not None or _relaxed_local_rules:
                _extra["force_send_greeting"] = (
                    True if _relaxed_local_rules else bool(params.get("force_send_greeting"))
                )
            res = fb.add_friend_and_greet(
                target,
                note=note,
                greeting=greeting,
                device_id=resolved,
                persona_key=_persona_key or None,
                phase=_phase_override or None,
                preset_key=_preset_key,
                source=_src_val,
                greet_on_failure=bool(params.get("greet_on_failure", False)),
                **_extra,
            ) or {}
            # 2026-04-23 P3-1: automation 层已在锁内 record(ok 时 sent;
            # 不 ok 时不写)。这里补细分状态, 便于失败漏斗分析。
            # 2026-05-11: 细分 risk → failed_no_button / failed_persona / risk
            if not res.get("add_friend_ok"):
                try:
                    from src.host.fb_store import record_friend_request
                    _fail_status = "risk"
                    try:
                        _last_reason = getattr(fb, "_last_greet_skip_reason", "") or ""
                        if "persona" in _last_reason or "l1" in _last_reason:
                            _fail_status = "failed_persona"
                        elif "no_add_button" in _last_reason or "无加好友按钮" in _last_reason:
                            _fail_status = "failed_no_button"
                    except Exception:
                        pass
                    record_friend_request(
                        resolved, target,
                        note=note,
                        source=_src_val,
                        status=_fail_status,
                        preset_key=_preset_key,
                    )
                except Exception:
                    pass
            ok = bool(res.get("add_friend_ok"))
            return ok, ("" if ok else "加好友+打招呼失败"), res

        if task_type == "facebook_send_greeting":
            # 独立打招呼任务(不配合 add_friend,用于老朋友主动问候 / 手动触发)
            target = (params.get("target") or params.get("username")
                      or params.get("profile_name", ""))
            if not target:
                return False, "params.target 必填", None
            if not hasattr(fb, "send_greeting_after_add_friend"):
                return False, "facebook.send_greeting_after_add_friend 尚未实现", None
            greeting = params.get("greeting") or params.get("greeting_message", "")
            _persona_key = params.get("persona_key") or ""
            _phase_override = params.get("phase") or params.get("phase_override") or ""
            _preset_key = (params.get("_preset_key", "")
                           or params.get("preset_key", ""))
            try:
                from src.host.fb_playbook import local_rules_disabled
                _force_greeting = local_rules_disabled() or bool(params.get("force_send_greeting"))
            except Exception:
                _force_greeting = bool(params.get("force_send_greeting"))
            ok = fb.send_greeting_after_add_friend(
                target,
                greeting=greeting,
                device_id=resolved,
                persona_key=_persona_key or None,
                phase=_phase_override or None,
                # 独立调用时默认不在 profile 页 → 自动跑 search_people 链路
                assume_on_profile=bool(params.get("assume_on_profile", False)),
                preset_key=_preset_key,
                ai_decision=params.get("ai_decision") or "greeting",
                # 2026-04-26 fix: 透传 force_send_greeting 让 prob gate / phase
                # 都能被 caller (B2B 客户测试 / E2E) 一键绕过
                force=_force_greeting,
            )
            return ok, ("" if ok else "打招呼发送失败"), None

        if task_type == "facebook_search_leads":
            keyword = params.get("keyword") or params.get("query", "")
            if not keyword:
                # FIX5: 兜底 persona seed_group_keywords
                try:
                    from src.host.fb_target_personas import get_persona_display, get_default_persona_key
                    _pk = params.get("persona_key") or get_default_persona_key()
                    _pd = get_persona_display(_pk) or {}
                    _seeds = _pd.get("seed_group_keywords") or []
                    if _seeds:
                        keyword = _seeds[0]
                        logger.info("[search_leads] keyword 未填, 使用 persona seed: %s", keyword)
                except Exception:
                    pass
            if not keyword:
                return False, "params.keyword 必填（且 persona 无 seed_group_keywords 可兜底）", None
            max_leads = int(params.get("max_leads", 10))
            search_type = (params.get("search_type") or
                           params.get("target_type") or "").strip().lower()
            if search_type in ("groups", "group", "群组", "小组"):
                if not hasattr(fb, "discover_groups_by_keyword"):
                    return False, "facebook.discover_groups_by_keyword 尚未实现", None
                groups = fb.discover_groups_by_keyword(
                    keyword,
                    device_id=resolved,
                    max_groups=int(params.get("max_groups") or max_leads),
                    skip_visited=bool(params.get("skip_visited", True)),
                    persona_key=params.get("persona_key") or None,
                    target_country=params.get("target_country", ""),
                )
                return True, "", {"groups": groups, "count": len(groups),
                                  "keyword": keyword}
            leads = fb.search_and_collect_leads(keyword, device_id=resolved,
                                                max_leads=max_leads)
            return True, "", {"leads": leads, "count": len(leads)}

        if task_type == "facebook_join_group":
            group = params.get("group_name") or params.get("group", "")
            if not group:
                return False, "params.group_name 必填", None
            ok = fb.join_group(group, device_id=resolved)
            join_outcome = getattr(fb, "last_join_group_outcome", "") or ""
            if ok:
                try:
                    from src.host.fb_store import upsert_group
                    preset_key = (params.get("_preset_key", "") or
                                  params.get("preset_key", ""))
                    upsert_group(resolved, group,
                                 country=params.get("target_country", ""),
                                 language=params.get("language", ""),
                                 status="joined",
                                 preset_key=preset_key)
                except Exception:
                    pass
                return True, "", {"group_name": group,
                                  "outcome": join_outcome or "joined"}
            if join_outcome in (
                "join_requested_pending_approval",
                "membership_questions_required",
            ):
                try:
                    from src.host.fb_store import upsert_group
                    preset_key = (params.get("_preset_key", "") or
                                  params.get("preset_key", ""))
                    upsert_group(
                        resolved, group,
                        country=params.get("target_country", ""),
                        language=params.get("language", ""),
                        status=("question_required"
                                if join_outcome == "membership_questions_required"
                                else "join_requested"),
                        preset_key=preset_key,
                    )
                except Exception:
                    pass
                return True, "", {
                    "group_name": group,
                    "outcome": join_outcome,
                    "next_action": (
                        "answer_membership_questions"
                        if join_outcome == "membership_questions_required"
                        else "wait_for_group_approval"
                    ),
                }
            return False, (f"加入群组失败 ({join_outcome or 'unknown'})"), {
                "group_name": group,
                "outcome": join_outcome or "unknown",
            }

        # 群组相关 — Sprint 1 新增
        if task_type == "facebook_browse_groups":
            if not hasattr(fb, "browse_groups"):
                return False, "facebook.browse_groups 尚未实现（待 Sprint 1 完成）", None
            stats = fb.browse_groups(
                max_groups=int(params.get("max_groups", 5)),
                device_id=resolved,
            )
            return True, "", stats

        if task_type == "facebook_group_engage":
            if not hasattr(fb, "group_engage_session"):
                return False, "facebook.group_engage_session 尚未实现", None
            stats = fb.group_engage_session(
                group_name=params.get("group_name", ""),
                max_posts=int(params.get("max_posts", 5)),
                comment_probability=float(params.get("comment_probability", 0.2)),
                like_probability=float(params.get("like_probability", 0.4)),
                device_id=resolved,
                persona_key=params.get("persona_key") or None,
                phase=params.get("phase") or params.get("phase_override") or None,
            )
            return True, "", stats

        if task_type == "facebook_extract_members":
            if not hasattr(fb, "extract_group_members"):
                return False, "facebook.extract_group_members 尚未实现", None
            group_name = (params.get("group_name") or "").strip()
            # P0-1: group_name 是核心必填 — 缺失则手机端不会进任何群组,
            # 在 FB 当前页(常为首页)空跑提取循环, 必然 0 结果。
            # 历史上 executor 直接返回 True 导致"虚假成功",此处强校验。
            if not group_name:
                return False, (
                    "缺少必填参数 group_name（目标群组名）。请在启动方案时填写群组列表，"
                    "或在 persona 的 seed_group_keywords 中配置默认值。"
                ), {"members": [], "count": 0, "outcome": "missing_param:group_name"}
            preset_key = str(
                params.get("_preset_key") or params.get("preset_key") or ""
            ).strip()
            preset_keyword_flow = (
                preset_key in ("friend_growth",)
                and not bool(params.get("exact_group"))
            )
            broad_keyword = bool(
                params.get("broad_keyword")
                or params.get("discover_groups")
                or int(params.get("max_groups") or params.get("max_groups_to_extract") or 1) > 1
                or preset_keyword_flow
            )
            if broad_keyword and hasattr(fb, "discover_groups_by_keyword"):
                max_groups = int(params.get("max_groups_to_extract")
                                 or params.get("max_groups") or 3)
                max_members_per_group = int(params.get("max_members_per_group")
                                            or params.get("max_members") or 30)
                discovered = fb.discover_groups_by_keyword(
                    group_name,
                    device_id=resolved,
                    max_groups=max_groups * 3,
                    skip_visited=bool(params.get("skip_visited", True)),
                    persona_key=params.get("persona_key") or None,
                    target_country=params.get("target_country", ""),
                )
                if not discovered:
                    try:
                        from src.host.fb_store import list_unvisited_groups
                        cached_groups = list_unvisited_groups(
                            resolved, keyword=group_name, limit=max_groups * 3)
                        discovered = [
                            {
                                "group_name": row.get("group_name") or "",
                                "keyword": group_name,
                                "member_count": int(row.get("member_count") or 0),
                                "meta": "cached:facebook_groups",
                                "requires_join": (
                                    (row.get("status") or "") == "pending"
                                ),
                                "already_visited": False,
                                "cached": True,
                            }
                            for row in cached_groups
                            if row.get("group_name")
                        ]
                        if discovered:
                            logger.info(
                                "[facebook_extract_members] keyword=%r "
                                "实时发现为空，使用本地候选 %d 个",
                                group_name, len(discovered),
                            )
                    except Exception as _cache_e:
                        logger.debug(
                            "[facebook_extract_members] 读取本地群组候选失败: %s",
                            _cache_e,
                        )
                all_members = []
                group_results = []
                join_if_needed = bool(
                    params.get("join_if_needed")
                    or params.get("auto_join_groups")
                    or params.get("auto_join")
                    or preset_keyword_flow
                )
                candidate_groups = (discovered or [])[:max_groups]
                join_quota_meta: Optional[Dict[str, Any]] = None
                join_quota_msg = ""
                for g in candidate_groups:
                    exact_group = (g.get("group_name") or "").strip()
                    if not exact_group:
                        continue
                    requires_join = bool(g.get("requires_join"))
                    if requires_join and not join_if_needed:
                        group_results.append({
                            "group_name": exact_group,
                            "members": 0,
                            "status": "pending_join",
                            "requires_join": True,
                        })
                        continue
                    if requires_join and join_if_needed:
                        if join_quota_meta is not None:
                            group_results.append({
                                "group_name": exact_group,
                                "members": 0,
                                "status": "join_quota_blocked",
                                "requires_join": True,
                                "join_outcome": "quota_exceeded",
                                "next_action": "retry_after_join_group_quota",
                                "quota": join_quota_meta.get("quota", {}),
                            })
                            continue
                        join_ok = False
                        join_outcome = ""
                        if hasattr(fb, "join_group"):
                            try:
                                join_ok = bool(fb.join_group(
                                    exact_group, device_id=resolved))
                                join_outcome = (
                                    getattr(fb, "last_join_group_outcome", "") or ""
                                )
                            except QuotaExceeded as qe:
                                join_quota_msg, join_quota_meta = (
                                    _quota_exceeded_response(qe)
                                )
                                logger.info(
                                    "[facebook_extract_members] keyword=%r "
                                    "group=%r join quota blocked: %s",
                                    group_name, exact_group, join_quota_msg,
                                )
                                try:
                                    from src.host.fb_store import upsert_group
                                    upsert_group(
                                        resolved, exact_group,
                                        member_count=int(g.get("member_count") or 0),
                                        country=params.get("target_country", ""),
                                        status="pending",
                                        preset_key=params.get("persona_key") or "",
                                    )
                                except Exception:
                                    pass
                                group_results.append({
                                    "group_name": exact_group,
                                    "members": 0,
                                    "status": "join_quota_blocked",
                                    "requires_join": True,
                                    "join_outcome": "quota_exceeded",
                                    "next_action": "retry_after_join_group_quota",
                                    "quota": join_quota_meta.get("quota", {}),
                                    "message": join_quota_msg,
                                })
                                continue
                        if join_ok:
                            try:
                                from src.host.fb_store import upsert_group
                                upsert_group(
                                    resolved, exact_group,
                                    member_count=int(g.get("member_count") or 0),
                                    country=params.get("target_country", ""),
                                    status="joined",
                                    preset_key=params.get("persona_key") or "",
                                )
                            except Exception:
                                pass
                        else:
                            if join_outcome == "membership_questions_required":
                                join_status = "membership_questions_required"
                                store_status = "question_required"
                                next_action = "answer_membership_questions"
                            elif join_outcome == "join_requested_pending_approval":
                                join_status = "join_requested_pending_approval"
                                store_status = "join_requested"
                                next_action = "wait_for_group_approval"
                            else:
                                join_status = "join_failed"
                                store_status = ""
                                next_action = "diagnose_join_group"
                            if store_status:
                                try:
                                    from src.host.fb_store import upsert_group
                                    upsert_group(
                                        resolved, exact_group,
                                        member_count=int(g.get("member_count") or 0),
                                        country=params.get("target_country", ""),
                                        status=store_status,
                                        preset_key=params.get("persona_key") or "",
                                    )
                                except Exception:
                                    pass
                            group_results.append({
                                "group_name": exact_group,
                                "members": 0,
                                "status": join_status,
                                "requires_join": True,
                                "join_outcome": join_outcome or "unknown",
                                "next_action": next_action,
                            })
                            continue
                    members_g = fb.extract_group_members(
                        group_name=exact_group,
                        max_members=max_members_per_group,
                        use_llm_scoring=bool(params.get("use_llm_scoring", False)),
                        target_country=params.get("target_country", ""),
                        device_id=resolved,
                        persona_key=params.get("persona_key") or None,
                        phase=params.get("phase") or params.get("phase_override") or None,
                        join_if_needed=join_if_needed,
                    )
                    count_g = len(members_g or [])
                    extract_error = None
                    if count_g == 0:
                        try:
                            from src.app_automation.facebook import (
                                consume_last_extract_error as _cle,
                            )
                            extract_error = _cle(resolved)
                        except Exception:
                            extract_error = None
                    if count_g:
                        group_status = "extracted"
                    elif extract_error == "group_requires_join":
                        group_status = "pending_join"
                    elif extract_error == "group_join_blocked":
                        group_status = "join_blocked"
                    elif extract_error == "group_join_failed":
                        group_status = "join_failed"
                    elif extract_error == "members_tab_not_found":
                        group_status = "members_tab_not_found"
                    else:
                        group_status = "zero_members"
                    all_members.extend(members_g or [])
                    group_results.append({
                        "group_name": exact_group,
                        "members": count_g,
                        "status": group_status,
                        "requires_join": requires_join or (
                            extract_error in ("group_requires_join", "group_join_blocked")
                        ),
                        "error_step": extract_error,
                    })
                    try:
                        from src.host.fb_store import mark_group_visit
                        mark_group_visit(resolved, exact_group,
                                         extracted_count=count_g)
                    except Exception:
                        pass
                count_all = len(all_members)
                if count_all == 0:
                    join_quota_blocked = [
                        r for r in group_results
                        if r.get("status") == "join_quota_blocked"
                    ]
                    if join_quota_blocked and len(join_quota_blocked) == len(group_results):
                        msg = join_quota_msg or (
                            f"宽关键词 {group_name!r} 已发现 {len(discovered or [])} 个群组，"
                            "但 facebook/join_group 入群额度已满，等额度释放后再继续。"
                        )
                        meta: Dict[str, Any] = {
                            "members": [],
                            "count": 0,
                            "groups": group_results,
                            "discovered_groups": discovered or [],
                            "join_quota_blocked_count": len(join_quota_blocked),
                            "outcome": "join_quota_blocked_after_discovery",
                            "group_name": group_name,
                            "keyword": group_name,
                            "next_action": "retry_after_join_group_quota",
                        }
                        if join_quota_meta:
                            meta.update(join_quota_meta)
                        return False, msg, meta
                    pending_join = [
                        r for r in group_results
                        if r.get("status") == "pending_join"
                    ]
                    if pending_join and len(pending_join) == len(group_results):
                        return True, "", {
                            "members": [],
                            "count": 0,
                            "groups": group_results,
                            "discovered_groups": discovered or [],
                            "pending_join_count": len(pending_join),
                            "outcome": "groups_discovered_pending_join",
                            "group_name": group_name,
                            "keyword": group_name,
                            "next_action": "join_group_then_prepare_member_greetings",
                        }
                    join_blocked = [
                        r for r in group_results
                        if r.get("status") in (
                            "join_requested_pending_approval",
                            "membership_questions_required",
                            "join_blocked",
                        )
                    ]
                    if join_blocked and len(join_blocked) == len(group_results):
                        return True, "", {
                            "members": [],
                            "count": 0,
                            "groups": group_results,
                            "discovered_groups": discovered or [],
                            "join_blocked_count": len(join_blocked),
                            "outcome": "groups_join_blocked_no_extract",
                            "group_name": group_name,
                            "keyword": group_name,
                            "next_action": (
                                "answer_membership_questions_or_wait_approval"
                            ),
                        }
                    join_failed = [
                        r for r in group_results
                        if r.get("status") == "join_failed"
                    ]
                    if join_failed and len(join_failed) == len(group_results):
                        return False, (
                            f"宽关键词 {group_name!r} 已发现 {len(discovered or [])} 个群组，"
                            "但自动入群失败，暂无法准备群成员招呼对象；点击徽章查看失败现场。"
                        ), {"members": [], "count": 0,
                            "groups": group_results,
                            "discovered_groups": discovered or [],
                            "outcome": "automation_group_join_failed",
                            "group_name": group_name,
                            "keyword": group_name,
                            "next_action": "diagnose_join_group"}
                    return False, (
                        f"宽关键词 {group_name!r} 已发现 {len(discovered or [])} 个群组，"
                        "但未提取到成员；点击徽章查看具体失败现场。"
                    ), {"members": [], "count": 0,
                        "groups": group_results,
                        "discovered_groups": discovered or [],
                        "outcome": "automation_extract_zero_after_discovery",
                        "group_name": group_name}
                return True, "", {"members": all_members,
                                  "count": count_all,
                                  "groups": group_results,
                                  "discovered_groups": discovered or [],
                                  "outcome": "ok",
                                  "keyword": group_name}
            members = fb.extract_group_members(
                group_name=group_name,
                max_members=int(params.get("max_members", 30)),
                use_llm_scoring=bool(params.get("use_llm_scoring", False)),
                target_country=params.get("target_country", ""),
                device_id=resolved,
                persona_key=params.get("persona_key") or None,
                phase=params.get("phase") or params.get("phase_override") or None,
                join_if_needed=bool(
                    params.get("join_if_needed")
                    or params.get("auto_join_groups")
                    or params.get("auto_join")
                    or preset_keyword_flow
                ),
            )
            count = len(members or [])
            # P0-2: 零结果不再视为成功 — 区分进群失败 / 选择器失效 / 群被删 等真实问题
            if count == 0:
                # P2.X (2026-04-30): outcome 三段细化 —— 从 automation 层 consume
                # 具体失败步骤, 让运营从徽章直接看出"根本没进对群"vs"进群但 0 成员"。
                _err_step = None
                try:
                    from src.app_automation.facebook import (
                        consume_last_extract_error as _cle,
                    )
                    _err_step = _cle(resolved)
                except Exception:
                    pass
                _outcome_map = {
                    "enter_group_failed": (
                        "automation_enter_group_failed",
                        "未能进入目标群组——可能：群名拼写错/被风控屏蔽/已被删除/FB 搜索误点了人主页。",
                    ),
                    "members_tab_not_found": (
                        "automation_members_tab_not_found",
                        "已进群但找不到 Members 标签——FB UI 改版可能性极高，建议运维检查选择器。",
                    ),
                    "group_requires_join": (
                        "automation_group_requires_join",
                        "已进入群组但尚未加入，成员列表不可见；需要先入群或等待审批。",
                    ),
                    "group_join_blocked": (
                        "automation_group_join_blocked",
                        "目标群组需要回答入群问题或等待管理员审批，暂无法准备群成员招呼对象。",
                    ),
                    "group_join_failed": (
                        "automation_group_join_failed",
                        "已尝试自动加入目标群组但未成功，暂无法准备群成员招呼对象。",
                    ),
                    "zero_after_enter": (
                        "automation_extract_zero_after_enter",
                        "已进群但未提取到任何成员——可能群组开启隐私/成员列表加载失败。",
                    ),
                }
                _oc, _msg = _outcome_map.get(_err_step, _outcome_map["zero_after_enter"])
                return False, (
                    f"{_msg} (group={group_name!r}) 点击徽章可查看失败现场截图。"
                ), {"members": [], "count": 0,
                    "outcome": _oc,
                    "error_step": _err_step or "zero_after_enter",
                    "group_name": group_name}
            try:
                from src.host.fb_store import mark_group_visit
                mark_group_visit(resolved, group_name, extracted_count=count)
            except Exception:
                pass
            return True, "", {"members": members, "count": count, "outcome": "ok",
                              "group_name": group_name}

        # ── P2-4 Sprint B: 目标画像 Profile Hunt ─────────────────────────
        # 接收候选名字列表 → 挨个 search → snapshot → L1+L2 VLM 识别 →
        # 命中者可选 follow / add_friend。配额由 fb_target_personas.yaml 控制。
        if task_type == "facebook_profile_hunt":
            if not hasattr(fb, "profile_hunt"):
                return False, "facebook.profile_hunt 尚未实现", None
            candidates = params.get("candidates") or []
            if isinstance(candidates, str):
                # 兼容前端传字符串（换行 / 逗号分隔）
                import re as _re
                candidates = [c.strip() for c in _re.split(r"[,\n;]", candidates) if c.strip()]
            if not candidates:
                # 若未传 candidates，尝试从上游任务产出拉（如群成员打招呼准备）
                upstream = params.get("candidates_from_task_id") or ""
                if upstream:
                    try:
                        from src.host.task_store import get_task as _gt
                        t = _gt(upstream)
                        if t and t.get("result"):
                            import json as _j
                            _r = _j.loads(t["result"]) if isinstance(t["result"], str) else t["result"]
                            names = []
                            for m in (_r.get("members") or []):
                                if isinstance(m, dict):
                                    n = m.get("name") or m.get("display_name") or ""
                                    if n:
                                        names.append(n)
                                elif isinstance(m, str):
                                    names.append(m)
                            candidates = names
                    except Exception as _e:
                        logger.warning("[profile_hunt] 拉取上游候选失败: %s", _e)
            if not candidates:
                return False, "params.candidates 必填（或提供 candidates_from_task_id）", None

            max_targets = params.get("max_targets")
            if max_targets is not None:
                max_targets = int(max_targets)
            lo = float(params.get("inter_target_min_sec", 20.0))
            hi = float(params.get("inter_target_max_sec", 34.0))
            if hi < lo:
                hi = lo + 10.0
            stats = fb.profile_hunt(
                candidates=candidates,
                persona_key=params.get("persona_key"),
                action_on_match=(params.get("action_on_match") or "none"),
                note=(params.get("note") or ""),
                max_targets=max_targets,
                inter_target_sec=(lo, hi),
                shot_count=int(params.get("shot_count", 3)),
                task_id=(_get_current_task_id() or ""),
                device_id=resolved,
            )
            ok = bool(stats.get("processed", 0) > 0)
            return ok, ("" if ok else "未处理任何候选"), stats

        if task_type == "facebook_name_hunter_prescreen":
            if not hasattr(fb, "profile_hunt"):
                return False, "facebook.profile_hunt 尚未实现", None
            persona_key = params.get("persona_key") or "jp_female_midlife"
            raw_candidates = params.get("candidates") or params.get("add_friend_targets") or []
            candidates: List[str] = []
            if raw_candidates:
                for item in raw_candidates:
                    if isinstance(item, dict):
                        if float(item.get("seed_score") or 0) >= float(params.get("min_seed_score") or 80):
                            candidates.append(str(item.get("name") or "").strip())
                    else:
                        candidates.append(str(item).strip())
            else:
                try:
                    from src.host.fb_targets_store import list_name_hunter_candidates
                    rows = list_name_hunter_candidates(
                        persona_key=persona_key,
                        status=params.get("status") or "seeded",
                        min_seed_score=float(params.get("min_seed_score") or 80),
                        limit=int(params.get("max_targets") or 20),
                    )
                    candidates = [str(r.get("display_name") or "").strip() for r in rows]
                except Exception as _e:
                    return False, f"读取点名候选池失败: {_e}", None
            candidates = [c for c in candidates if c]
            if not candidates:
                return False, "没有待预筛的高置信点名候选", {
                    "card_type": "fb_name_hunter_prescreen",
                    "processed": 0,
                    "matched": 0,
                }
            stats = fb.profile_hunt(
                candidates=candidates,
                persona_key=persona_key,
                action_on_match="none",
                max_targets=int(params.get("max_targets") or len(candidates)),
                inter_target_sec=(
                    float(params.get("inter_target_min_sec", 20.0)),
                    float(params.get("inter_target_max_sec", 34.0)),
                ),
                shot_count=int(params.get("shot_count", 3)),
                task_id=(_get_current_task_id() or ""),
                device_id=resolved,
                candidate_source="name_hunter",
            )
            stats["card_type"] = "fb_name_hunter_prescreen"
            return bool(stats.get("processed", 0) > 0), "", stats

        if task_type == "facebook_name_hunter_touch_qualified":
            persona_key = params.get("persona_key") or "jp_female_midlife"
            try:
                from src.host.fb_targets_store import name_hunter_touch_targets
                rows = name_hunter_touch_targets(
                    persona_key=persona_key,
                    limit=int(params.get("max_targets") or params.get("max_friends_per_run") or 5),
                )
            except Exception as _e:
                return False, f"读取 qualified 点名候选失败: {_e}", None
            targets = [
                {
                    "name": r.get("display_name") or "",
                    "seed_score": (r.get("insights") or {}).get("seed_score", 100),
                    "candidate_id": r.get("id"),
                }
                for r in rows
                if r.get("display_name")
            ]
            if not targets:
                return False, "没有 qualified 点名候选可触达", {
                    "card_type": "fb_name_hunter_touch_qualified",
                    "processed": 0,
                }
            campaign_params = dict(params)
            campaign_params.update({
                "steps": ["add_friends"],
                "add_friend_targets": targets,
                "persona_key": persona_key,
                "require_high_match": True,
                "min_seed_score": float(params.get("min_seed_score") or 80),
                "do_l2_gate": True,
                "strict_persona_gate": True,
                "send_greeting_inline": bool(params.get("send_greeting_inline", True)),
                "_preset_key": params.get("_preset_key") or "name_hunter",
            })
            return _run_facebook_campaign(fb, resolved, campaign_params)

        # 收件箱(Sprint 2 P0 完整实现 + Sprint 3 P0 preset_key 透传)
        _preset_key = params.get("_preset_key", "") or params.get("preset_key", "")
        _persona_key = params.get("persona_key") or None
        _phase_override = params.get("phase") or params.get("phase_override") or None
        if task_type == "facebook_check_inbox":
            if not hasattr(fb, "check_messenger_inbox"):
                return False, "facebook.check_messenger_inbox 尚未实现", None
            referral = params.get("referral_contact", "")
            stats = fb.check_messenger_inbox(
                auto_reply=bool(params.get("auto_reply", False)),
                max_conversations=int(params.get("max_conversations", 20)),
                referral_contact=referral,
                preset_key=_preset_key,
                device_id=resolved,
                persona_key=_persona_key,
                phase=_phase_override,
            )
            return True, "", stats

        if task_type == "facebook_check_message_requests":
            if not hasattr(fb, "check_message_requests"):
                return False, "facebook.check_message_requests 尚未实现", None
            stats = fb.check_message_requests(
                auto_review=bool(params.get("auto_review", True)),
                max_requests=int(params.get("max_requests", 20)),
                preset_key=_preset_key,
                device_id=resolved,
                persona_key=_persona_key,
                phase=_phase_override,
            )
            return True, "", stats

        if task_type == "facebook_check_friend_requests":
            if not hasattr(fb, "check_friend_requests_inbox"):
                return False, "facebook.check_friend_requests_inbox 尚未实现", None
            stats = fb.check_friend_requests_inbox(
                accept_all=bool(params.get("accept_all", False)),
                safe_accept=bool(params.get("safe_accept", True)),
                max_requests=int(params.get("max_requests", 20)),
                min_mutual_friends=int(params.get("min_mutual_friends", 1)),
                device_id=resolved,
                persona_key=_persona_key,
                phase=_phase_override,
            )
            return True, "", stats

        if task_type == "facebook_group_member_greet":
            greet_params = dict(params or {})
            greet_params.setdefault("steps", ["extract_members", "add_friends"])
            greet_params.setdefault("_preset_key", "friend_growth")
            greet_params.setdefault("preset_key", "friend_growth")
            greet_params.setdefault("broad_keyword", True)
            greet_params.setdefault("discover_groups", True)
            greet_params.setdefault("auto_join_groups", True)
            greet_params.setdefault("join_if_needed", True)
            greet_params.setdefault("skip_visited", True)
            greet_params.setdefault("send_greeting_inline", True)
            greet_params.setdefault("require_verification_note", True)
            greet_params.setdefault("require_outreach_goal", True)
            # 2026-05-10: 新版 FB 已移除非管理员 Members 列表入口,
            # mutual_members/contributors 路径长期 0 产出 + 浪费 5min+.
            # 默认改 feed_authors (帖子作者法), 兜底链在 _campaign_member_sources.
            greet_params.setdefault("member_sources", ["general", "feed_authors"])
            if not greet_params.get("extract_max_members"):
                greet_params["extract_max_members"] = int(
                    greet_params.get("max_members") or 20)
            return _run_facebook_campaign(fb, resolved, greet_params)

        # 串行剧本（与 TikTok 的 tiktok_campaign_run 同构）
        if task_type == "facebook_campaign_run":
            return _run_facebook_campaign(fb, resolved, params)

        # Phase 12.3 (2026-04-25): facebook_recycle_dead_peers
        # 扫 canonical 含 referral_dead tag 且 referral_dead_at 早于 now - days,
        # 去 tag + 清 counter → peer 再次可被 dispatcher plan.
        # 前缀用 facebook_ 让 executor 路由把它送到 Facebook 分支 (函数本身不依赖 fb).
        if task_type == "facebook_recycle_dead_peers":
            return _line_pool_recycle_dead_peers(params)

        # Phase 18 (2026-04-25): facebook_daily_referral_summary
        # 组装 funnel + ranking + reject metrics, 写 logs/daily_summary_YYYYMMDD.json
        # 可选 webhook (env SLACK_WEBHOOK_URL).
        if task_type == "facebook_daily_referral_summary":
            return _fb_daily_referral_summary(params)

        # Phase 11 (2026-04-25): fb_line_dispatch_from_reply
        # 扫近 N 小时 contact_events (greeting_replied/message_received) → 按
        # canonical.metadata (l2_verified / persona 匹配) 过滤 → allocate LINE
        # pool → 写 line_dispatch_log + 返 "派发计划" 给调用方 (B 机/运营).
        if task_type == "facebook_line_dispatch_from_reply":
            return _fb_line_dispatch_from_reply(resolved, params)

        # Phase 12 Alpha (2026-04-25): A 自立消费 line_dispatch_planned → 用
        # Messenger 直接把 referral 话术发给对方, 不等 B 协同就位.
        if task_type == "facebook_send_referral_replies":
            return _fb_send_referral_replies(fb, resolved, params)

        # Phase 19.x.1 (2026-04-25): facebook_alert_check_hourly
        # 轻量 alert 检测 + 状态抑制 (24h 内同 alert 不重发).
        if task_type == "facebook_alert_check_hourly":
            return _fb_alert_check_hourly(params)

        # Phase 20.1 (2026-04-25): facebook_check_referral_replies
        # 扫 wa_referral_sent 后 24-48h 内未 replied 的 peer, 走 B 侧 Messenger
        # inbox 检测器, 命中关键词写 wa_referral_replied event.
        if task_type == "facebook_check_referral_replies":
            return _fb_check_referral_replies(fb, resolved, params)

        # Phase 20.2 (2026-04-25): facebook_mark_stale_referrals
        # 扫 sent 但 stale_hours 内仍未 replied 的 peer, 标 referral_stale,
        # 超过 escalate_to_dead_days 天升级 referral_dead.
        if task_type == "facebook_mark_stale_referrals":
            return _fb_mark_stale_referrals(params)

        return False, f"不支持的 Facebook 任务类型: {task_type}", None

    except QuotaExceeded as qe:
        # 2026-04-27 P4: quota 满是预期的限流保护, 不该走 exception 路径污染日志.
        # 转友好中文 + 推算下次可派的 ETA, dashboard 直接显示 "X 分钟后可派下一个".
        msg, meta = _quota_exceeded_response(qe)
        # info level 而非 exception (不告警, 不刷 traceback)
        logger.info("[quota] %s/%s blocked: %s", qe.platform, qe.action, msg)
        return False, msg, meta

    except Exception as e:
        logger.exception("Facebook 任务执行异常: %s", task_type)
        return False, f"{task_type} 异常: {e}", None


# Phase 19.x.1 (2026-04-25): alert detection + state suppression
# 抽出来给 daily_summary + hourly_check 共用.

_ALERT_STATE_PATH = "logs/alert_state.json"
_ALERT_DEFAULT_COOLDOWN_HOURS = 24
# Phase 19.x.3.1: severity-based cooldown (critical 重要应较早重发)
_ALERT_DEFAULT_SEVERITY_COOLDOWNS = {
    "critical": 4,
    "warning": 24,
    "info": 48,
}


def _load_alert_state() -> Dict[str, str]:
    """logs/alert_state.json: {alert_type: last_fired_iso}."""
    import json as _json
    from pathlib import Path as _Path
    p = _Path(_ALERT_STATE_PATH)
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            data = _json.load(f) or {}
        return {k: str(v) for k, v in data.items() if isinstance(k, str)}
    except Exception as e:
        logger.debug("[alert_state] load 失败: %s", e)
        return {}


def _save_alert_state(state: Dict[str, str]) -> None:
    """原子写 (tmp + rename) 避免半写."""
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    p = _Path(_ALERT_STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            _json.dump(state, f, ensure_ascii=False, indent=2)
        # atomic rename (Windows 可能要先删旧)
        if p.exists():
            _os.replace(str(tmp), str(p))
        else:
            tmp.rename(p)
    except Exception as e:
        logger.debug("[alert_state] save 失败: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _alert_state_key(a: Dict[str, Any]) -> str:
    """Phase 20.1.9.2: state key = type + (":region" if region 非空).

    region 非空时 cooldown / history 按 (type, region) 独立, 同 type 不同 region
    互不抑制. 用于 per-region replied_rate_low 等场景.
    """
    rg = (a.get("region") or "").strip()
    base = a.get("type") or ""
    return f"{base}:{rg}" if rg else base


def _detect_referral_alerts(funnel: Dict[str, Any],
                              reject_total: int,
                              alert_send_threshold: float = 0.3,
                              alert_reject_threshold: int = 10,
                              alert_reply_threshold: float = 0.2,
                              alert_reply_min_sent: int = 10,
                              alert_stale_threshold: float = 0.5,
                              alert_stale_min_sent: int = 10,
                              *,
                              region_label: str = ""
                              ) -> List[Dict[str, Any]]:
    """共用 alert 检测逻辑 (4 类规则). daily_summary + hourly_check 都调.

    Phase 20.1.8.2: 加 replied_rate_low — sent 多但 reply rate < 阈值, 文案
    /persona 信号失效预警.
    Phase 20.1.9.2: region_label 非空时, 每条 alert 自动 tag region 字段;
    cooldown / history 按 (type, region) 独立 state_key.

    返 list of {type, severity, message, region?}. 无 alert 返空 list.
    """
    alerts: List[Dict[str, Any]] = []
    if (funnel.get("planned", 0) >= 5
            and funnel.get("send_rate", 0) < alert_send_threshold):
        alerts.append({
            "type": "send_rate_low",
            "severity": "warning",
            "message": (f"send_rate={funnel['send_rate']*100:.1f}% "
                          f"< {alert_send_threshold*100:.0f}% "
                          f"(planned={funnel['planned']})"),
        })
    if reject_total >= alert_reject_threshold:
        alerts.append({
            "type": "reject_rate_high",
            "severity": "warning",
            "message": (f"reject_history={reject_total} "
                          f"(阈值={alert_reject_threshold})"),
        })
    if funnel.get("planned", 0) >= 5 and funnel.get("sent", 0) == 0:
        alerts.append({
            "type": "no_dispatched",
            "severity": "critical",
            "message": (f"planned={funnel['planned']} 但 sent=0, "
                          "检查 send_referral_replies 任务/账号"),
        })
    # Phase 20.1.8.2: replied_rate_low — sent 够多但 reply 比率低
    sent_n = int(funnel.get("sent", 0) or 0)
    replied_n = int(funnel.get("replied", 0) or 0)
    if sent_n >= alert_reply_min_sent:
        reply_rate = replied_n / sent_n if sent_n else 0.0
        if reply_rate < alert_reply_threshold:
            alerts.append({
                "type": "replied_rate_low",
                "severity": "warning",
                "message": (f"reply_rate={reply_rate*100:.1f}% "
                              f"< {alert_reply_threshold*100:.0f}% "
                              f"(sent={sent_n}, replied={replied_n}); "
                              "文案/persona 可能失效"),
            })
    # Phase 20.2.x.2: stale_rate_high — sent 多但 stale 占比高 (受众失活)
    stale_n = int(funnel.get("stale", 0) or 0)
    if sent_n >= alert_stale_min_sent and stale_n > 0:
        stale_rate = stale_n / sent_n
        if stale_rate >= alert_stale_threshold:
            alerts.append({
                "type": "stale_rate_high",
                "severity": "warning",
                "message": (f"stale_rate={stale_rate*100:.1f}% "
                              f">= {alert_stale_threshold*100:.0f}% "
                              f"(sent={sent_n}, stale={stale_n}); "
                              "受众/时段可能不匹配"),
            })
    # Phase 20.1.9.2: region_label 非空时给所有 alerts 打 region 标签
    if region_label:
        for a in alerts:
            a["region"] = region_label
            # message 加前缀方便人眼区分
            a["message"] = f"[{region_label}] {a['message']}"
    return alerts


def _filter_alerts_by_cooldown(alerts: List[Dict[str, Any]],
                                  state: Dict[str, str],
                                  cooldown_hours: int = _ALERT_DEFAULT_COOLDOWN_HOURS,
                                  severity_cooldowns: Optional[Dict[str, int]] = None
                                  ) -> List[Dict[str, Any]]:
    """Phase 19.x.1 / 19.x.3.1: 根据 alert_state + severity 抑制重复.

    Phase 19.x.3.1: cooldown 按 severity 分级 (critical 4h / warning 24h /
    info 48h). 没指定 severity 退化用 cooldown_hours 兜底.

    severity_cooldowns 可被 caller 完全覆盖默认 dict.

    只返 "应该真发 webhook" 的 alerts. cooldown 内同 type 跳过.
    """
    import datetime as _dt
    now = _dt.datetime.utcnow()
    sev_map = (severity_cooldowns
                if severity_cooldowns is not None
                else _ALERT_DEFAULT_SEVERITY_COOLDOWNS)
    out: List[Dict[str, Any]] = []
    for a in alerts:
        # Phase 20.1.9.2: state_key = type + (:region if any) 区分 per-region
        last_iso = state.get(_alert_state_key(a), "")
        # 选 cooldown: severity 优先, 没匹配用全局 cooldown_hours
        sev = (a.get("severity") or "").lower()
        eff_cd = sev_map.get(sev, cooldown_hours)
        if last_iso:
            try:
                last_dt = _dt.datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ")
                if (now - last_dt).total_seconds() < eff_cd * 3600:
                    continue
            except Exception:
                pass
        out.append(a)
    return out


def _record_alerts_fired(alerts: List[Dict[str, Any]],
                            state: Dict[str, str],
                            *,
                            history_context: Optional[Dict[str, Any]] = None
                            ) -> Dict[str, str]:
    """更新 alert_state.json + 写 fb_alert_history (Phase 20.1.9.1).

    state file 用于 cooldown 抑制 (in-memory + 单文件); history 表用于跨进程
    历史回顾 / 趋势分析. 两路并存, 失败互不影响.

    history_context 是触发时 funnel 等上下文 dict, 写入 fb_alert_history.context_json.
    """
    import datetime as _dt
    now_iso = _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    new_state = dict(state)
    for a in alerts:
        # Phase 20.1.9.2: state_key 含 region 后缀
        new_state[_alert_state_key(a)] = now_iso
        # Phase 20.1.9.1: 同步写 history (失败 silent)
        try:
            from src.host.fb_store import record_alert_fired
            record_alert_fired(a, region=str(a.get("region") or ""),
                                 context=history_context)
        except Exception as e:
            logger.debug("[alert_history] hook 失败: %s", e)
    return new_state


def _fb_alert_check_hourly(params: Dict[str, Any]) -> tuple:
    """Phase 19.x.1: 每小时 lightweight alert 检测.

    比 daily summary 轻 — 只跑 funnel + reject_history, 不写 daily_summary 文件.
    检测出 alerts 后用 alert_state.json 抑制 24h 内重复, 仅新触发才 webhook.

    Params:
      hours_window: int = 24
      cooldown_hours: int = 24 (重复抑制)
      alert_send_rate_threshold: float = 0.3
      alert_reject_threshold: int = 10
    """
    hours_window = int(params.get("hours_window", 24) or 24)
    cooldown = int(params.get("cooldown_hours", 24) or 24)
    send_thr = float(params.get("alert_send_rate_threshold", 0.3) or 0.3)
    reject_thr = int(params.get("alert_reject_threshold", 10) or 10)
    # Phase 20.1.8.2: replied_rate alert
    reply_thr = float(params.get("alert_reply_threshold", 0.2) or 0.2)
    reply_min_sent = int(params.get("alert_reply_min_sent", 10) or 10)
    # Phase 20.2.x.2: stale_rate alert
    stale_thr = float(params.get("alert_stale_threshold", 0.5) or 0.5)
    stale_min_sent = int(params.get("alert_stale_min_sent", 10) or 10)
    severity_cd = params.get("severity_cooldowns")  # 可选 dict 覆盖默认

    from src.host.line_pool import referral_funnel
    from src.host.fb_store import get_peer_name_reject_history

    funnel = referral_funnel(hours_window=hours_window)
    rej_hist = get_peer_name_reject_history(hours_window=hours_window)
    rej_total = rej_hist.get("total", 0)

    all_alerts = _detect_referral_alerts(
        funnel, rej_total, send_thr, reject_thr,
        alert_reply_threshold=reply_thr,
        alert_reply_min_sent=reply_min_sent,
        alert_stale_threshold=stale_thr,
        alert_stale_min_sent=stale_min_sent)

    state = _load_alert_state()
    fire_now = _filter_alerts_by_cooldown(
        all_alerts, state, cooldown_hours=cooldown,
        severity_cooldowns=severity_cd if isinstance(severity_cd, dict) else None)

    webhook_sent = False
    if fire_now:
        # 发 webhook
        import os as _os
        webhook_url = _os.environ.get("OPENCLAW_SLACK_WEBHOOK_URL", "")
        if webhook_url:
            try:
                import urllib.request
                import json as _json
                lines = [
                    "🚨 *Referral Alert (hourly)*",
                    f"hours_window: {hours_window}h",
                    f"funnel: planned={funnel['planned']} sent={funnel['sent']} replied={funnel['replied']}",
                ]
                for a in fire_now:
                    lines.append(f"  - [{a['severity']}] {a['type']}: {a['message']}")
                req = urllib.request.Request(
                    webhook_url,
                    data=_json.dumps({"text": "\n".join(lines)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(req, timeout=10)
                webhook_sent = True
            except Exception as e:
                logger.warning("[hourly_alert] webhook 失败: %s", e)
        # 状态写文件 + 历史 DB (即使 webhook 失败也写, 避免反复尝试)
        hist_ctx = {
            "hours_window": hours_window,
            "funnel": {"planned": funnel.get("planned", 0),
                          "sent": funnel.get("sent", 0),
                          "replied": funnel.get("replied", 0),
                          "send_rate": funnel.get("send_rate", 0)},
            "reject_total": rej_total,
            "source": "hourly_check",
        }
        new_state = _record_alerts_fired(fire_now, state,
                                            history_context=hist_ctx)
        _save_alert_state(new_state)

    return True, "", {
        "all_alerts": all_alerts,
        "fired_now": fire_now,
        "suppressed": len(all_alerts) - len(fire_now),
        "webhook_sent": webhook_sent,
        "funnel_summary": {
            "planned": funnel.get("planned", 0),
            "sent": funnel.get("sent", 0),
            "replied": funnel.get("replied", 0),
        },
    }


def _compute_reply_latency_stats(hours_window: int = 24) -> Dict[str, Any]:
    """Phase 20.1.7.2 (2026-04-25): 从 wa_referral_replied 事件 meta 算 latency 统计.

    返:
      {
        samples: int,                 # 有 latency_seconds 的样本数
        avg_min: float | None,
        median_min: float | None,
        p95_min: float | None,
        max_min: float | None,
      }
    全 None 表示窗口内没数据.
    """
    out: Dict[str, Any] = {
        "samples": 0, "avg_min": None, "median_min": None,
        "p95_min": None, "max_min": None,
    }
    try:
        from src.host.fb_store import (list_recent_contact_events_by_types,
                                          CONTACT_EVT_WA_REFERRAL_REPLIED)
        import json as _j
        rows = list_recent_contact_events_by_types(
            [CONTACT_EVT_WA_REFERRAL_REPLIED],
            hours=hours_window, limit=2000)
        latencies_min: List[float] = []
        for r in rows:
            try:
                m = _j.loads(r.get("meta_json") or "{}")
            except Exception:
                continue
            lm = m.get("latency_min")
            if isinstance(lm, (int, float)) and lm >= 0:
                latencies_min.append(float(lm))
        if not latencies_min:
            return out
        latencies_min.sort()
        n = len(latencies_min)
        # p95: 最少 20 样本才有意义, 否则用 max
        if n >= 20:
            p95_idx = max(0, min(n - 1, int(round(0.95 * (n - 1)))))
            p95 = latencies_min[p95_idx]
        else:
            p95 = latencies_min[-1]
        median_idx = n // 2
        median = (latencies_min[median_idx]
                   if n % 2 == 1
                   else (latencies_min[median_idx - 1]
                          + latencies_min[median_idx]) / 2.0)
        out.update({
            "samples": n,
            "avg_min": round(sum(latencies_min) / n, 2),
            "median_min": round(median, 2),
            "p95_min": round(p95, 2),
            "max_min": round(latencies_min[-1], 2),
        })
    except Exception as e:
        logger.debug("[reply_latency] 计算失败: %s", e)
    return out


def _compute_latency_anomaly(today_latency: Dict[str, Any],
                                lookback_days: int = 7,
                                min_baseline_samples: int = 3,
                                z_threshold: float = 2.0
                                ) -> Optional[Dict[str, Any]]:
    """Phase 20.1.9.3 (2026-04-25): 7d daily_summary 历史 vs 今天 latency.

    读 logs/daily_summary_YYYYMMDD.json 历史 (排除今天), 拿
    summary.reply_latency.avg_min 序列, 算 stdev + z-score.

    返:
      {avg_baseline, stdev, samples, z, anomaly}  -- 有数据时
      None -- 历史样本数 < min_baseline_samples 或 today 没数据
    """
    try:
        today_avg = today_latency.get("avg_min") if today_latency else None
        if today_avg is None:
            return None
        import datetime as _dt
        import json as _j
        from pathlib import Path as _Path
        import statistics as _stats
        baseline: List[float] = []
        for i in range(1, lookback_days + 1):
            d = (_dt.datetime.utcnow() - _dt.timedelta(days=i)).strftime("%Y%m%d")
            p = _Path("logs") / f"daily_summary_{d}.json"
            if not p.exists():
                continue
            try:
                with p.open(encoding="utf-8") as f:
                    data = _j.load(f) or {}
                avg = data.get("reply_latency", {}).get("avg_min")
                if isinstance(avg, (int, float)) and avg >= 0:
                    baseline.append(float(avg))
            except Exception:
                continue
        if len(baseline) < min_baseline_samples:
            return None
        avg_b = sum(baseline) / len(baseline)
        std = _stats.pstdev(baseline) if len(baseline) >= 2 else 0.0
        z: Optional[float] = None
        anomaly = False
        if std > 0:
            z = (today_avg - avg_b) / std
            anomaly = abs(z) > z_threshold
        return {
            "samples": len(baseline),
            "avg_baseline": round(avg_b, 2),
            "stdev": round(std, 3),
            "z": round(z, 3) if z is not None else None,
            "anomaly": anomaly,
            "today_avg": today_avg,
        }
    except Exception as e:
        logger.debug("[latency_anomaly] 失败: %s", e)
        return None


def _fb_daily_referral_summary(params: Dict[str, Any]) -> tuple:
    """Phase 18 / 19 (2026-04-25): 每日 referral 闭环健康摘要 + 趋势 + 告警.

    Phase 19 新增:
      - trend: vs 昨天 daily_summary_*.json diff (planned/sent/replied 增减)
      - alerts: send_rate < 30% (planned >= 5) / reject_rate > 20% (rejects >= 10)
      - by_region: jp / it 分别跑一次 funnel 拿 per-region 数字

    Params:
      hours_window: int = 24
      write_file: bool = True
      send_webhook: bool = True
      regions: list[str] = ["jp", "it"]   # Phase 19.3: per-region funnel
      alert_send_rate_threshold: float = 0.3
      alert_reject_threshold: int = 10
    """
    import datetime as _dt
    import json as _json
    from pathlib import Path as _Path

    hours_window = int(params.get("hours_window", 24) or 24)
    write_file = bool(params.get("write_file", True))
    send_webhook = bool(params.get("send_webhook", True))
    regions = params.get("regions", ["jp", "it"])
    if not isinstance(regions, list):
        regions = ["jp", "it"]
    alert_send_threshold = float(
        params.get("alert_send_rate_threshold", 0.3) or 0.3)
    alert_reject_threshold = int(
        params.get("alert_reject_threshold", 10) or 10)
    # Phase 20.1.8.2: replied_rate alert
    alert_reply_threshold = float(
        params.get("alert_reply_threshold", 0.2) or 0.2)
    alert_reply_min_sent = int(
        params.get("alert_reply_min_sent", 10) or 10)
    # Phase 20.2.x.2: stale_rate alert
    alert_stale_threshold = float(
        params.get("alert_stale_threshold", 0.5) or 0.5)
    alert_stale_min_sent = int(
        params.get("alert_stale_min_sent", 10) or 10)

    from src.host.line_pool import (referral_funnel, account_ranking,
                                      recent_dispatch_log)
    from src.host.fb_store import (get_peer_name_reject_history,
                                     get_peer_name_reject_metrics)

    funnel = referral_funnel(hours_window=hours_window)
    ranking = account_ranking(hours_window=hours_window, limit=5)
    rej_history = get_peer_name_reject_history(
        hours_window=hours_window, limit=20, by_day=True)
    rej_live = get_peer_name_reject_metrics()
    log_recent = recent_dispatch_log(limit=200)
    log_status_count: Dict[str, int] = {}
    for r in log_recent:
        st = r.get("status") or "?"
        log_status_count[st] = log_status_count.get(st, 0) + 1

    # Phase 19.3: per-region funnel
    by_region: Dict[str, Dict[str, Any]] = {}
    for rg in regions:
        try:
            by_region[rg] = referral_funnel(
                hours_window=hours_window, region=rg)
        except Exception as e:
            logger.debug("[daily_summary] per-region %s 失败: %s", rg, e)
            by_region[rg] = {}

    # Phase 19.2 / 19.x.1 / 20.1.8.2: 共用 alert detection helper
    rej_history_total = rej_history.get("total", 0)
    alerts = _detect_referral_alerts(
        funnel, rej_history_total,
        alert_send_threshold, alert_reject_threshold,
        alert_reply_threshold=alert_reply_threshold,
        alert_reply_min_sent=alert_reply_min_sent,
        alert_stale_threshold=alert_stale_threshold,
        alert_stale_min_sent=alert_stale_min_sent)

    # Phase 20.1.9.2: per-region alert 检测 (jp/it 各自跑 detection)
    # reject 是全 region 共用的, 只查 overall, region 维度不重复算
    for rg, rg_funnel in by_region.items():
        if not rg_funnel:
            continue
        try:
            rg_alerts = _detect_referral_alerts(
                rg_funnel, 0,  # reject 不重复
                alert_send_threshold, alert_reject_threshold,
                alert_reply_threshold=alert_reply_threshold,
                alert_reply_min_sent=alert_reply_min_sent,
                alert_stale_threshold=alert_stale_threshold,
                alert_stale_min_sent=alert_stale_min_sent,
                region_label=rg)
            # 滤掉 reject_rate_high (全局 alert 已含, region 层不重复)
            rg_alerts = [a for a in rg_alerts
                          if a.get("type") != "reject_rate_high"]
            alerts.extend(rg_alerts)
        except Exception as e:
            logger.debug("[daily_summary] region %s alert 失败: %s", rg, e)

    # Phase 20.1.7.2: reply latency 统计 (从 wa_referral_replied.meta.latency_seconds)
    latency_stats = _compute_reply_latency_stats(hours_window=hours_window)

    # Phase 20.1.9.3: latency anomaly z-score (read 7d daily_summary history)
    latency_anomaly = _compute_latency_anomaly(latency_stats)
    if latency_anomaly and latency_anomaly.get("anomaly"):
        alerts.append({
            "type": "latency_anomaly",
            "severity": "warning",
            "message": (
                f"reply latency avg={latency_stats.get('avg_min')}min, "
                f"|z|={abs(latency_anomaly['z']):.2f} > 2 vs 7d avg "
                f"{latency_anomaly['avg_baseline']:.1f}min "
                f"(stdev={latency_anomaly['stdev']:.1f})"),
        })

    # Phase 20.1.9.1: 把 daily summary 检出的 alerts 也写到 fb_alert_history,
    # 不参与 cooldown (daily 一天就触发一次, 没必要抑制), 只持久化便于回溯.
    if alerts:
        try:
            from src.host.fb_store import record_alert_fired as _rec_alert
            ds_ctx = {
                "hours_window": hours_window,
                "funnel": funnel,
                "reject_total": rej_history_total,
                "source": "daily_summary",
            }
            for a in alerts:
                _rec_alert(a, region=str(a.get("region") or ""),
                            context=ds_ctx)
        except Exception as e:
            logger.debug("[alert_history] daily_summary 写入失败: %s", e)

    summary: Dict[str, Any] = {
        "generated_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hours_window": hours_window,
        "funnel": funnel,
        "by_region": by_region,
        "top_5_accounts": ranking,
        "reply_latency": latency_stats,
        "latency_anomaly": latency_anomaly,
        "peer_name_rejects": {
            "live_total": rej_live.get("total", 0),
            "live_by_event_type": rej_live.get("by_event_type", {}),
            "history_total": rej_history_total,
            "history_by_day": rej_history.get("by_day", {}),
            "history_by_event": rej_history.get("by_event_type", {}),
        },
        "dispatch_log_status": log_status_count,
        "dispatch_log_total_24h": len(log_recent),
        "alerts": alerts,
    }

    # Phase 19.1: trend vs 昨天
    trend: Optional[Dict[str, Any]] = None
    try:
        yest_str = (_dt.datetime.utcnow() - _dt.timedelta(days=1)).strftime("%Y%m%d")
        yest_path = _Path("logs") / f"daily_summary_{yest_str}.json"
        if yest_path.exists():
            with yest_path.open(encoding="utf-8") as f:
                yest_data = _json.load(f)
            yest_funnel = yest_data.get("funnel", {})
            trend = {
                "yesterday_date": yest_str,
                "planned_delta": funnel.get("planned", 0) - yest_funnel.get("planned", 0),
                "sent_delta": funnel.get("sent", 0) - yest_funnel.get("sent", 0),
                "replied_delta": funnel.get("replied", 0) - yest_funnel.get("replied", 0),
                "send_rate_delta": round(
                    funnel.get("send_rate", 0) - yest_funnel.get("send_rate", 0), 4),
                "yesterday_planned": yest_funnel.get("planned", 0),
                "yesterday_sent": yest_funnel.get("sent", 0),
                "yesterday_replied": yest_funnel.get("replied", 0),
            }
    except Exception as e:
        logger.debug("[daily_summary] trend 计算失败: %s", e)
    summary["trend"] = trend

    # Phase 19.x.2: trend_7d (7 天滚动平均)
    trend_7d: Optional[Dict[str, Any]] = None
    try:
        days_data = []
        for i in range(1, 8):
            d_str = (_dt.datetime.utcnow() - _dt.timedelta(days=i)).strftime("%Y%m%d")
            d_path = _Path("logs") / f"daily_summary_{d_str}.json"
            if d_path.exists():
                try:
                    with d_path.open(encoding="utf-8") as f:
                        days_data.append(_json.load(f).get("funnel", {}))
                except Exception:
                    pass
        if len(days_data) >= 3:
            import statistics as _stats
            n = len(days_data)
            planned_vals = [d.get("planned", 0) for d in days_data]
            sent_vals = [d.get("sent", 0) for d in days_data]
            replied_vals = [d.get("replied", 0) for d in days_data]
            avg_planned = sum(planned_vals) / n
            avg_sent = sum(sent_vals) / n
            avg_replied = sum(replied_vals) / n
            avg_send_rate = sum(d.get("send_rate", 0) or 0 for d in days_data) / n
            today_planned = funnel.get("planned", 0)
            today_sent = funnel.get("sent", 0)
            today_replied = funnel.get("replied", 0)

            # Phase 19.x.3.2: stdev + z-score (population stdev; n=1 时 0)
            def _safe_stdev(vals):
                try:
                    return _stats.pstdev(vals) if len(vals) >= 2 else 0.0
                except Exception:
                    return 0.0

            std_p = _safe_stdev(planned_vals)
            std_s = _safe_stdev(sent_vals)
            std_r = _safe_stdev(replied_vals)

            def _z(today, avg, std):
                if std and std > 0:
                    return round((today - avg) / std, 3)
                return None

            z_p = _z(today_planned, avg_planned, std_p)
            z_s = _z(today_sent, avg_sent, std_s)
            z_r = _z(today_replied, avg_replied, std_r)
            # 异常: 任一 |z| > 2 (≈ 95% 置信区间外)
            anomaly = any(z is not None and abs(z) > 2
                           for z in (z_p, z_s, z_r))

            trend_7d = {
                "samples": len(days_data),
                "avg_planned": round(avg_planned, 2),
                "avg_sent": round(avg_sent, 2),
                "avg_replied": round(avg_replied, 2),
                "avg_send_rate": round(avg_send_rate, 4),
                "stdev_planned": round(std_p, 3),
                "stdev_sent": round(std_s, 3),
                "stdev_replied": round(std_r, 3),
                "z_planned": z_p,
                "z_sent": z_s,
                "z_replied": z_r,
                "anomaly": anomaly,
                "ratio_planned": (round(today_planned / avg_planned, 3)
                                    if avg_planned else None),
                "ratio_sent": (round(today_sent / avg_sent, 3)
                                 if avg_sent else None),
                "ratio_replied": (round(today_replied / avg_replied, 3)
                                    if avg_replied else None),
            }
    except Exception as e:
        logger.debug("[daily_summary] trend_7d 计算失败: %s", e)
    summary["trend_7d"] = trend_7d

    written_to = ""
    if write_file:
        try:
            today = _dt.datetime.utcnow().strftime("%Y%m%d")
            logs_dir = _Path("logs")
            logs_dir.mkdir(exist_ok=True)
            fpath = logs_dir / f"daily_summary_{today}.json"
            with fpath.open("w", encoding="utf-8") as f:
                _json.dump(summary, f, ensure_ascii=False, indent=2)
            written_to = str(fpath)
            logger.info("[daily_summary] 写文件: %s", fpath)
        except Exception as e:
            logger.warning("[daily_summary] 写文件失败: %s", e)

    webhook_sent = False
    if send_webhook:
        import os as _os
        webhook_url = _os.environ.get("OPENCLAW_SLACK_WEBHOOK_URL", "")
        if webhook_url:
            try:
                import urllib.request
                # 标题: 有 alerts 加 🚨
                title_prefix = "🚨 " if alerts else ""
                # Phase 20.2: 加 stale 一行 (有 stale 才展示)
                stale_n = funnel.get("stale", 0)
                stale_line = ""
                if stale_n > 0:
                    stale_line = (f"stale_24h: {stale_n} "
                                    f"({funnel.get('stale_rate', 0)*100:.1f}% of sent)")
                lines = [
                    f"{title_prefix}*Referral 闭环 24h 摘要* ({summary['generated_at']})",
                    f"funnel: planned={funnel['planned']} sent={funnel['sent']} replied={funnel['replied']}",
                    f"send_rate={funnel['send_rate']*100:.1f}% conv_rate={funnel['conversion_rate']*100:.1f}%",
                ]
                if stale_line:
                    lines.append(stale_line)
                # Phase 19.1: trend
                if trend:
                    arrow = lambda v: ("+" if v > 0 else "") + str(v)
                    lines.append(
                        f"vs 昨天 ({trend['yesterday_date']}): "
                        f"planned {arrow(trend['planned_delta'])}, "
                        f"sent {arrow(trend['sent_delta'])}, "
                        f"replied {arrow(trend['replied_delta'])}")
                # Phase 19.x.2 / 19.x.3.2: trend_7d + anomaly z-score
                if trend_7d:
                    rs = trend_7d.get("ratio_sent")
                    rp = trend_7d.get("ratio_planned")
                    anomaly_mark = "⚠ " if trend_7d.get("anomaly") else ""
                    lines.append(
                        f"{anomaly_mark}vs 7d avg ({trend_7d['samples']} samples): "
                        f"planned ratio={rp if rp is not None else 'n/a'}, "
                        f"sent ratio={rs if rs is not None else 'n/a'} "
                        f"(avg={trend_7d['avg_planned']:.1f}/{trend_7d['avg_sent']:.1f})")
                    if trend_7d.get("anomaly"):
                        z_p = trend_7d.get("z_planned")
                        z_s = trend_7d.get("z_sent")
                        lines.append(
                            f"  └ anomaly detected: z_planned={z_p}, z_sent={z_s} "
                            f"(|z| > 2 = 95% 置信区间外)")
                # Phase 19.3: per-region
                if by_region:
                    rg_parts = []
                    for rg, rf in by_region.items():
                        if rf:
                            rg_parts.append(
                                f"{rg}={rf.get('planned',0)}/{rf.get('sent',0)}")
                    if rg_parts:
                        lines.append("by_region (planned/sent): " + ", ".join(rg_parts))
                # Phase 20.1.7.2: reply latency
                if latency_stats and latency_stats.get("samples", 0) > 0:
                    lines.append(
                        f"reply_latency: n={latency_stats['samples']} "
                        f"avg={latency_stats['avg_min']:.1f}min "
                        f"median={latency_stats['median_min']:.1f}min "
                        f"p95={latency_stats['p95_min']:.1f}min")
                lines.append(
                    f"reject_24h: history={rej_history_total} (live={rej_live.get('total', 0)})")
                lines.append(
                    "top accounts: " + ", ".join(
                        f"{a['line_id']}={a['success_rate']*100:.0f}%"
                        for a in ranking[:3]))
                lines.append(f"dispatch_log_status: {log_status_count}")
                # Phase 19.2: alerts
                if alerts:
                    lines.append("")
                    lines.append("⚠ *ALERTS:*")
                    for a in alerts:
                        lines.append(f"  - [{a['severity']}] {a['type']}: {a['message']}")

                # Phase 19.x.3.3: dashboard URL link (env 没设就跳过)
                dash_base = _os.environ.get("OPENCLAW_DASHBOARD_BASE_URL", "").rstrip("/")
                if dash_base:
                    today_str = _dt.datetime.utcnow().strftime("%Y%m%d")
                    lines.append("")
                    lines.append(
                        f"📊 详情: {dash_base}/line-pool/stats/daily-summary?date={today_str}")

                req = urllib.request.Request(
                    webhook_url,
                    data=_json.dumps({"text": "\n".join(lines)}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST")
                urllib.request.urlopen(req, timeout=10)
                webhook_sent = True
                logger.info("[daily_summary] webhook 发送成功 (alerts=%d)",
                              len(alerts))
            except Exception as e:
                logger.warning("[daily_summary] webhook 发送失败: %s", e)

    return True, "", {
        "summary": summary,
        "written_to": written_to,
        "webhook_sent": webhook_sent,
    }


def _line_pool_recycle_dead_peers(params: Dict[str, Any]) -> tuple:
    """Phase 12.3 (2026-04-25): 把"死太久"的 referral_dead peer 自动复活.

    扫所有带 referral_dead tag 的 canonical, metadata.referral_dead_at 早于
    ``now - days`` 的 → revive_referral (去 tag + 清 counter). 给 peer
    第二次机会 (FB 可能已经解开对该 peer 的发消息限制).

    Params:
      days: int = 7           # 多少天前标 dead 的才 recycle
      dry_run: bool = False   # 只列, 不真做
      limit: int = 500        # 每轮最多处理多少条 (防 DB 爆)
    """
    days = max(1, int(params.get("days", 7) or 7))
    dry_run = bool(params.get("dry_run", False))
    limit = max(1, min(int(params.get("limit", 500) or 500), 5000))

    from src.host.lead_mesh import (list_l2_verified_leads,
                                     revive_referral)
    import datetime as _dt

    # list_l2_verified_leads 只返 l2_verified 的; dead peers 大部分也是 L2 verified
    # (因为只有 L2 过的才会被 dispatcher plan + 之后被标 dead). 这里
    # include_tags=['referral_dead'] 精准定位, limit 放大一点.
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=days)

    rows = list_l2_verified_leads(
        include_tags=["referral_dead"], limit=limit,
    )

    revived_ids: List[str] = []
    skipped_young: int = 0
    for r in rows:
        dead_at_iso = r.get("metadata", {}).get("referral_dead_at") or ""
        if dead_at_iso:
            try:
                dead_dt = _dt.datetime.strptime(
                    dead_at_iso, "%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                dead_dt = None
            if dead_dt and dead_dt > cutoff:
                # 还没到 days 天, skip
                skipped_young += 1
                continue
        # 没 dead_at_iso (老数据) 或已够久 → revive
        if dry_run:
            revived_ids.append(r["canonical_id"])
            continue
        try:
            if revive_referral(r["canonical_id"],
                                actor="scheduled_7d_auto"):
                revived_ids.append(r["canonical_id"])
        except Exception as e:
            logger.debug("[recycle] revive %s 失败: %s",
                          r["canonical_id"][:12], e)

    return True, "", {
        "scanned": len(rows), "revived": len(revived_ids),
        "skipped_young": skipped_young,
        "revived_canonical_ids": revived_ids[:50],  # cap response size
        "days_threshold": days, "dry_run": dry_run,
    }


def _fb_line_dispatch_from_reply(resolved: str,
                                  params: Dict[str, Any]
                                  ) -> tuple:
    """Phase 11 (2026-04-25) dispatcher.

    Flow:
      1. 扫 hours_window 内 greeting_replied + message_received events
      2. 去重: 每个 canonical_id 在 dedupe_hours 内最多派发一次
      3. 按 persona / l2_verified / is_japanese / min_score 过滤
      4. line_pool.allocate(region, persona_key, canonical_id, peer_name, ...)
      5. 组装 referral 话术 (chat_messages.yaml referral_line[] 模板 + {line_id})
      6. 写 line_dispatch_log (planned) + 可选 contact_event (line_dispatch_planned
         / wa_referral_sent, 可配)
      7. 返 stats {scanned, filtered_out, dispatched, no_account, dispatches}

    Params:
      hours_window: int = 6           # 扫近几小时事件
      dedupe_hours: int = 24          # 同一 canonical 去重窗口
      require_l2_verified: bool = True
      persona_key: str = ""           # 只对匹配 persona 的 lead 分配
      region: str = ""                # 按区域过滤 line pool
      min_score: float = 0
      limit: int = 20                 # 单次最多派发几条
      dispatch_mode: str = "messenger_text"  # messenger_text | line_direct_send
      write_contact_event: bool = False  # True 写 event 供 B 消费
      event_type: str = "line_dispatch_planned"  # 或 "wa_referral_sent" 保兼容
    """
    hours_window = int(params.get("hours_window", 6) or 6)
    dedupe_hours = int(params.get("dedupe_hours", 24) or 24)
    require_l2 = bool(params.get("require_l2_verified", True))
    persona_key = (params.get("persona_key") or "").strip()
    region = (params.get("region") or "").strip()
    min_score = float(params.get("min_score", 0) or 0)
    limit = max(1, min(int(params.get("limit", 20) or 20), 200))
    write_ce = bool(params.get("write_contact_event", False))
    dispatch_mode = (params.get("dispatch_mode") or "messenger_text").strip()
    if dispatch_mode not in {"messenger_text", "line_direct_send"}:
        dispatch_mode = "messenger_text"
    event_type = (params.get("event_type")
                   or "line_dispatch_planned").strip()
    # Phase 12.3: 通用 tags 过滤 (AND 包含 / NOT 排除)
    include_tags = {t.strip() for t in (params.get("include_tags") or [])
                     if isinstance(t, str) and t.strip()}
    exclude_tags = {t.strip() for t in (params.get("exclude_tags") or [])
                     if isinstance(t, str) and t.strip()}
    # Phase 12.3: dry_run — 不 allocate 不写 log 不写 event, 只列候选
    dry_run = bool(params.get("dry_run", False))
    # Phase 13: verbose_dry_run — 逐 event 诊断, stats.per_event_decisions
    # 列每条 event 为何 dispatched / skipped
    verbose_dry_run = bool(params.get("verbose_dry_run", False))
    per_event_decisions: List[Dict[str, Any]] = []

    def _rec_decision(ev_, reason_, extra_=None):
        """Phase 13: 只在 verbose_dry_run=True 时追加决策记录."""
        if not verbose_dry_run:
            return
        d = {
            "event_id": ev_.get("id"),
            "peer_name": ev_.get("peer_name"),
            "event_type": ev_.get("event_type"),
            "decision": reason_,
        }
        if extra_:
            d.update(extra_)
        per_event_decisions.append(d)

    from src.host.fb_store import (list_recent_contact_events_by_types,
                                    CONTACT_EVT_GREETING_REPLIED,
                                    CONTACT_EVT_MESSAGE_RECEIVED)
    from src.host import line_pool as _lp
    try:
        from src.host.fb_store import record_contact_event
    except Exception:
        record_contact_event = None  # type: ignore
    try:
        from src.app_automation.fb_content_assets import get_referral_snippet
    except Exception:
        get_referral_snippet = None  # type: ignore

    events = list_recent_contact_events_by_types(
        [CONTACT_EVT_GREETING_REPLIED, CONTACT_EVT_MESSAGE_RECEIVED],
        hours=hours_window, limit=limit * 5,
    )

    dispatches: List[Dict[str, Any]] = []
    seen_canonical: set = set()
    filtered_out = 0
    no_account = 0

    from src.host.lead_mesh import resolve_identity
    from src.host.lead_mesh.canonical import _connect as _lm_connect
    import json as _json

    # line_dispatch_log.created_at 用 datetime('now') 空格分隔格式, 用 SQL 原生
    # datetime('now', '-N hours') 做 cutoff 避免字符串格式不一致.
    dedupe_sql_offset = f"-{int(dedupe_hours)} hours"

    for ev in events:
        if len(dispatches) >= limit:
            break
        peer_name = ev.get("peer_name") or ""
        device_id = ev.get("device_id") or ""
        event_id = str(ev.get("id") or "")
        if not peer_name:
            filtered_out += 1
            _rec_decision(ev, "skipped_no_peer_name")
            continue

        try:
            cid = resolve_identity(
                platform="facebook",
                account_id=f"fb:{peer_name}",
                display_name=peer_name)
        except Exception:
            filtered_out += 1
            _rec_decision(ev, "skipped_resolve_identity_fail")
            continue

        if cid in seen_canonical:
            filtered_out += 1
            _rec_decision(ev, "skipped_seen_canonical_in_batch",
                           {"canonical_id": cid})
            continue

        # 去重: dedupe_hours 内已有 line_dispatch_log 就跳
        try:
            with _lm_connect() as conn:
                prev = conn.execute(
                    "SELECT 1 FROM line_dispatch_log WHERE canonical_id=?"
                    " AND status != 'skipped'"
                    " AND created_at >= datetime('now', ?) LIMIT 1",
                    (cid, dedupe_sql_offset),
                ).fetchone()
            if prev:
                seen_canonical.add(cid)
                filtered_out += 1
                _rec_decision(ev, "skipped_dedupe_24h",
                               {"canonical_id": cid})
                continue
        except Exception:
            pass

        # 读 canonical metadata 决定是否值得引流
        meta: Dict[str, Any] = {}
        try:
            with _lm_connect() as conn:
                row = conn.execute(
                    "SELECT metadata_json, tags FROM leads_canonical"
                    " WHERE canonical_id=?", (cid,),
                ).fetchone()
                if row:
                    try:
                        meta = _json.loads(row["metadata_json"] or "{}")
                    except Exception:
                        meta = {}
                    tags_set = {t.strip() for t in
                                 (row["tags"] or "").split(",") if t.strip()}
                else:
                    tags_set = set()
        except Exception:
            tags_set = set()

        if require_l2 and "l2_verified" not in tags_set:
            filtered_out += 1
            _rec_decision(ev, "skipped_not_l2_verified",
                           {"canonical_id": cid, "tags": sorted(tags_set)})
            continue
        # Phase 12.2: peer 被标 referral_dead 跳过 (永久 fail 过, 再试浪费)
        if "referral_dead" in tags_set:
            filtered_out += 1
            _rec_decision(ev, "skipped_referral_dead",
                           {"canonical_id": cid,
                            "dead_reason": meta.get("referral_dead_reason")})
            continue
        # Phase 12.3: 通用 include/exclude_tags 过滤
        if include_tags and not include_tags.issubset(tags_set):
            filtered_out += 1
            _rec_decision(ev, "skipped_include_tags_miss",
                           {"required": sorted(include_tags),
                            "actual": sorted(tags_set)})
            continue
        if exclude_tags and (exclude_tags & tags_set):
            filtered_out += 1
            _rec_decision(ev, "skipped_exclude_tags_hit",
                           {"excluded_hit": sorted(exclude_tags & tags_set)})
            continue
        try:
            if float(meta.get("l2_score", 0) or 0) < min_score:
                filtered_out += 1
                _rec_decision(ev, "skipped_l2_score_below_min",
                               {"score": meta.get("l2_score"),
                                "min_score": min_score})
                continue
        except (TypeError, ValueError):
            pass
        if persona_key and meta.get("l2_persona_key") != persona_key:
            filtered_out += 1
            _rec_decision(ev, "skipped_persona_mismatch",
                           {"lead_persona": meta.get("l2_persona_key"),
                            "filter_persona": persona_key})
            continue

        # 分配 LINE 账号 — dry_run 模式只预览选中账号, 不更新 DB.
        # NB: list_accounts 的 owner_device_id 是精确匹配, allocate 实际用
        # "OR empty (通用池)" 语义. 预览时不过滤 owner, 再在 Python 层筛一次
        # 保持与 allocate 行为一致.
        if dry_run:
            from src.host.line_pool import list_accounts as _lp_list
            cand_all = _lp_list(
                status="active", region=region or None,
                persona_key=persona_key or None,
                limit=20,
            )
            candidates = [
                c for c in cand_all
                if not device_id
                    or c.get("owner_device_id", "") == ""
                    or c.get("owner_device_id") == device_id
            ]
            if not candidates:
                no_account += 1
                seen_canonical.add(cid)
                _rec_decision(ev, "skipped_no_account_dry",
                               {"canonical_id": cid, "filter_region": region,
                                "filter_persona": persona_key})
                continue
            acc = candidates[0]
        else:
            acc = _lp.allocate(
                region=region or None,
                persona_key=persona_key or None,
                owner_device_id=device_id or None,
                canonical_id=cid, peer_name=peer_name,
                source_device_id=device_id, source_event_id=event_id,
            )
            if acc is None:
                no_account += 1
                seen_canonical.add(cid)
                _rec_decision(ev, "skipped_no_account_allocate",
                               {"canonical_id": cid,
                                "reason": "no_match_or_all_capped"})
                continue

        seen_canonical.add(cid)

        # Phase 11.2: 组装 messenger 引流话术 (dispatch_mode=messenger_text)
        # line_direct_send 模式下 B 机/LINE automation 用自己的话术, 这里只传 line_id.
        # Phase 12.1: persona fallback 优先级反转 — lead.metadata 优先于 task.persona_key
        # (lead 自身 L2 分类的 persona 比 task-level 全局 persona 更贴 lead 真实画像).
        effective_persona = (meta.get("l2_persona_key") or persona_key or "")
        message_template = ""
        if dispatch_mode == "messenger_text" and get_referral_snippet:
            try:
                message_template = get_referral_snippet(
                    channel="line",
                    value=acc["line_id"],
                    persona_key=effective_persona,
                    peer_name=peer_name,
                    age_band=meta.get("age_band") or "",
                    gender=meta.get("gender") or "",
                )
            except Exception as e:
                logger.debug("[phase11] referral_snippet 失败: %s", e)

        dispatch = {
            "canonical_id": cid,
            "peer_name": peer_name,
            "source_device_id": device_id,
            "source_event_id": event_id,
            "source_event_type": ev.get("event_type"),
            "line_account_id": acc["id"],
            "line_id": acc["line_id"],
            "dispatch_mode": dispatch_mode,
            "message_template": message_template,
            "metadata": {
                "age_band": meta.get("age_band"),
                "gender": meta.get("gender"),
                "is_japanese": meta.get("is_japanese"),
                "l2_score": meta.get("l2_score"),
            },
        }
        dispatches.append(dispatch)
        _rec_decision(ev, "dispatched",
                       {"canonical_id": cid,
                        "line_account_id": acc["id"],
                        "line_id": acc["line_id"]})

        if write_ce and record_contact_event is not None and not dry_run:
            try:
                record_contact_event(
                    device_id or resolved, peer_name,
                    event_type,
                    preset_key=f"line_pool:{acc['id']}",
                    meta={"line_id": acc["line_id"],
                          "line_account_id": acc["id"],
                          "dispatch_mode": dispatch_mode,
                          "message_template": message_template,
                          "dispatched_by": "agent_a_phase11",
                          "source_event_id": event_id,
                          "original_device_id": device_id,  # Phase 11.1 溯源
                          "canonical_id": cid})
            except Exception as e:
                logger.debug("[phase11] write %s 失败: %s", event_type, e)

    stats = {
        "scanned": len(events),
        "dispatched": len(dispatches),
        "filtered_out": filtered_out,
        "no_account": no_account,
        "dispatches": dispatches,
        "dry_run": dry_run,
    }
    if verbose_dry_run:
        stats["per_event_decisions"] = per_event_decisions
    return True, "", stats


# Phase 12.0.1: MessengerError code 分类 — 哪些值得 retry
_REFERRAL_TRANSIENT_CODES = frozenset({
    "messenger_unavailable",    # app 启动中
    "search_ui_missing",         # UI 加载慢
    "send_button_missing",       # UI glitch
})
_REFERRAL_PERMANENT_CODES = frozenset({
    "risk_detected",             # 风控
    "xspace_blocked",            # MIUI 双开
    "recipient_not_found",       # peer 搜不到
    "send_blocked_by_content",   # 内容被 FB 挡
})

# Phase 12.2: peer 级失败阈值 — 达阈值给 canonical 打 referral_dead tag,
# dispatcher 下次扫到会跳过. 按错误严重度分级:
#   recipient_not_found: peer 账号不存在/改名 → 1 次即 dead (retry 永远搜不到)
#   send_blocked_by_content: peer 过往封禁本文案 → 2 次 dead (换模板可能解)
# risk_detected/xspace_blocked 是 device/global 问题不计 peer 账.
_REFERRAL_PEER_FAIL_THRESHOLDS = {
    "recipient_not_found": 1,
    "send_blocked_by_content": 2,
}


def _referral_peer_fail_record(canonical_id: str,
                                err_code: str,
                                peer_name: str = "") -> bool:
    """Phase 12.2: 累加 canonical.metadata.referral_fail_count, 达到该
    err_code 对应阈值时打 referral_dead tag + 存 dead_reason/at.

    返回 True 表示本次调用触发了 referral_dead (状态由"活"转"死").
    """
    if not canonical_id or err_code not in _REFERRAL_PEER_FAIL_THRESHOLDS:
        return False
    threshold = _REFERRAL_PEER_FAIL_THRESHOLDS[err_code]
    try:
        from src.host.lead_mesh import update_canonical_metadata
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        import json as _json
        import time as _tt
        with _lm_connect() as conn:
            row = conn.execute(
                "SELECT metadata_json, tags FROM leads_canonical"
                " WHERE canonical_id=?", (canonical_id,),
            ).fetchone()
            if not row:
                return False
            try:
                existing = _json.loads(row["metadata_json"] or "{}")
            except Exception:
                existing = {}
            existing_tags = {t.strip() for t in
                              (row["tags"] or "").split(",") if t.strip()}
            if "referral_dead" in existing_tags:
                return False  # 已经 dead, 不重复
        # 构造增量
        counter_key = f"referral_fail_count_{err_code}"
        prev_n = int(existing.get(counter_key, 0) or 0)
        new_n = prev_n + 1
        meta_patch = {counter_key: new_n}
        tags = []
        if new_n >= threshold:
            meta_patch["referral_dead_reason"] = err_code
            meta_patch["referral_dead_at"] = _tt.strftime(
                "%Y-%m-%dT%H:%M:%SZ", _tt.gmtime())
            meta_patch["referral_dead_peer_name"] = peer_name or ""
            tags.append("referral_dead")
        update_canonical_metadata(canonical_id, meta_patch,
                                   tags=tags or None)
        return new_n >= threshold
    except Exception as e:
        logger.debug("[phase12.2.peer_fail] canonical=%s err=%s 异常: %s",
                       canonical_id[:12] if canonical_id else "", err_code, e)
        return False


def _fb_send_referral_replies(fb, resolved: str,
                               params: Dict[str, Any]) -> tuple:
    """Phase 12 Alpha (2026-04-25): A 自立消费 line_dispatch_planned event.

    为什么需要这个 task (而不仅仅是 dispatcher 写事件等 B 消费):
      * B 机实装 line_dispatch_planned 消费逻辑需要协调, 不知何时就绪
      * A 端已有 facebook.send_message, 能直接把 message_template 发出去
      * 闭环立刻跑通, 不阻塞生产. B 将来就位后作为 fallback 路径

    Flow:
      1. 扫近 hours_window 小时 line_dispatch_planned events
      2. dispatch_mode=messenger_text 直发; line_direct_send 若 LINE auto 未就位,
         fallback 为 messenger_text (Phase 12.0.1)
      3. strict_device_match=True: 只发 original_device_id 匹配本机
      4. 24h 去重: 若该 peer 已有 wa_referral_sent → skip
      5. 发送前 random.uniform(min, max) sleep 做速率平滑 (防风控)
      6. 调 facebook.send_message(raise_on_error=True), 按 MessengerError.code
         分瞬时/永久: 瞬时 retry max_retry 次 interval retry_interval_sec
      7. 成功 → 写 wa_referral_sent + mark_dispatch_outcome(sent)
         失败 → mark_dispatch_outcome(failed, note=<err_code>)

    Params:
      hours_window: int = 2
      dedupe_hours: int = 24
      strict_device_match: bool = True
      limit: int = 10
      min_interval_sec: float = 30   # 两条 send 之间最短 sleep
      max_interval_sec: float = 90   # 最长 sleep (random 0-1 混合)
      max_retry: int = 2             # 瞬时错误 retry 次数 (不含首发)
      retry_interval_sec: float = 10
      fallback_line_direct_send: bool = True  # True 时 line_direct_send 降级为
                                                # messenger_text 发 (避免死信)
    """
    import random as _r
    import time as _time

    hours_window = int(params.get("hours_window", 2) or 2)
    dedupe_hours = int(params.get("dedupe_hours", 24) or 24)
    strict_device = bool(params.get("strict_device_match", True))
    limit = max(1, min(int(params.get("limit", 10) or 10), 100))
    min_iv = float(params.get("min_interval_sec", 30) or 0)
    max_iv = float(params.get("max_interval_sec", 90) or 0)
    if max_iv < min_iv:
        max_iv = min_iv
    max_retry = max(0, int(params.get("max_retry", 2) or 0))
    retry_iv = float(params.get("retry_interval_sec", 10) or 0)
    fallback_direct_send = bool(params.get("fallback_line_direct_send", True))
    # Phase 12.3: dry_run — 不真发, 只列 matched planned events + 会发什么 template.
    dry_run = bool(params.get("dry_run", False))

    from src.host.fb_store import (list_recent_contact_events_by_types,
                                    record_contact_event,
                                    count_contact_events,
                                    CONTACT_EVT_LINE_DISPATCH_PLANNED,
                                    CONTACT_EVT_WA_REFERRAL_SENT)
    from src.host import line_pool as _lp
    try:
        from src.app_automation.facebook import MessengerError
    except Exception:
        MessengerError = Exception  # 兜底, 不应命中
    import json as _json

    events = list_recent_contact_events_by_types(
        [CONTACT_EVT_LINE_DISPATCH_PLANNED],
        hours=hours_window, limit=limit * 5,
    )

    sent = 0
    failed = 0
    skipped_dedup = 0
    skipped_device = 0
    skipped_mode = 0
    outcomes: List[Dict[str, Any]] = []

    processed_count = 0  # 已真正尝试发送的条数 (用于速率平滑间隔判断)

    for ev in events:
        if sent + failed >= limit:
            break
        try:
            meta = _json.loads(ev.get("meta_json") or "{}") or {}
        except Exception:
            meta = {}

        dispatch_mode = meta.get("dispatch_mode") or "messenger_text"
        # Phase 12.0.1: line_direct_send 若无 LINE auto 消费者会成死信,
        # fallback 为 messenger_text 直发 LINE ID 文本.
        effective_mode = dispatch_mode
        if dispatch_mode == "line_direct_send":
            if fallback_direct_send:
                effective_mode = "messenger_text"
            else:
                skipped_mode += 1
                continue
        if effective_mode != "messenger_text":
            skipped_mode += 1
            continue

        peer_name = ev.get("peer_name") or ""
        message_template = meta.get("message_template") or ""
        line_account_id = int(meta.get("line_account_id") or 0)
        original_device_id = (meta.get("original_device_id")
                                or ev.get("device_id") or "")

        if not peer_name or not message_template:
            skipped_mode += 1
            continue

        # 设备匹配: 只在同一台 FB 账号的设备上继续发, 避免串线
        if strict_device and original_device_id and resolved != original_device_id:
            skipped_device += 1
            continue

        # 24h 去重 peer 级 (可能多次 planned 但只发一次 referral)
        try:
            n_sent = count_contact_events(
                device_id=resolved, peer_name=peer_name,
                event_type=CONTACT_EVT_WA_REFERRAL_SENT,
                hours=dedupe_hours,
            )
            if n_sent > 0:
                skipped_dedup += 1
                continue
        except Exception:
            pass

        # Phase 12.0.1 速率平滑: 第二条起 random sleep [min, max]
        if processed_count > 0 and max_iv > 0:
            sleep_for = _r.uniform(min_iv, max_iv)
            logger.debug("[referral.send] rate-limit sleep %.1fs", sleep_for)
            _time.sleep(sleep_for)
        processed_count += 1

        # Phase 12.0.1 in-task retry: 瞬时错误 retry 最多 max_retry 次,
        # 永久错误立即 failed. raise_on_error=True 拿到 MessengerError.code.
        # Phase 12.3 dry_run: 不调 fb.send_message, 只记"会发" 不实发, 不 mark outcome
        if dry_run:
            outcomes.append({
                "peer_name": peer_name, "line_account_id": line_account_id,
                "line_id": meta.get("line_id"),
                "planned_event_id": ev.get("id"),
                "sent": None, "err_code": "dry_run",
                "note": "dry_run_would_send",
                "effective_mode": effective_mode,
                "would_send_template": message_template,
            })
            processed_count += 1
            continue

        ok = False
        err_note = ""
        err_code = ""
        attempts = max_retry + 1
        for attempt in range(attempts):
            try:
                ok = bool(fb.send_message(
                    recipient=peer_name, message=message_template,
                    device_id=resolved, raise_on_error=True,
                ))
                err_code = ""
                break
            except MessengerError as me:
                err_code = getattr(me, "code", "") or ""
                err_note = f"code={err_code}|{str(me)[:80]}"
                if err_code in _REFERRAL_PERMANENT_CODES:
                    logger.info("[referral.send] %s 永久错误 %s, 不 retry",
                                 peer_name, err_code)
                    break
                # 瞬时错误 / 未分类 → retry
                if attempt + 1 < attempts:
                    logger.info("[referral.send] %s 瞬时错误 %s, retry %d/%d",
                                 peer_name, err_code, attempt + 1,
                                 max_retry)
                    _time.sleep(retry_iv)
                    continue
                # 耗尽 retry
                logger.info("[referral.send] %s 瞬时错误 %s retry 耗尽",
                             peer_name, err_code)
                break
            except Exception as e:
                err_note = f"send_message_exception:{type(e).__name__}:{str(e)[:80]}"
                err_code = "unknown_exception"
                logger.warning("[referral.send] %s 未知异常: %s",
                                 peer_name, e)
                break

        canonical_id_of_peer = meta.get("canonical_id") or ""
        became_dead = False

        if ok:
            sent += 1
            try:
                record_contact_event(
                    resolved, peer_name,
                    CONTACT_EVT_WA_REFERRAL_SENT,
                    preset_key=f"line_pool:{line_account_id}",
                    meta={"line_id": meta.get("line_id"),
                          "line_account_id": line_account_id,
                          "sent_by": "agent_a_phase12_alpha",
                          "effective_mode": effective_mode,
                          "original_mode": dispatch_mode,
                          "source_event_id": str(ev.get("id") or ""),
                          "source_planned_event_id": str(ev.get("id") or ""),
                          "canonical_id": canonical_id_of_peer})
            except Exception as e:
                logger.debug("[referral.send] write wa_referral_sent 失败: %s", e)
            if line_account_id:
                try:
                    _lp.mark_dispatch_outcome(
                        line_account_id, status="sent",
                        note=f"via=messenger peer={peer_name}")
                except Exception:
                    pass
            # Phase 12.2: success 写 canonical metadata + tag line_referred
            if canonical_id_of_peer:
                try:
                    import time as _t_phase12
                    from src.host.lead_mesh import update_canonical_metadata
                    update_canonical_metadata(
                        canonical_id_of_peer,
                        {
                            "line_referred_at": _t_phase12.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", _t_phase12.gmtime()),
                            "line_id": meta.get("line_id"),
                            "line_account_id": line_account_id,
                            "referral_sent_via": effective_mode,
                        },
                        tags=["line_referred"],
                    )
                except Exception as e:
                    logger.debug("[referral.send] canonical line_referred "
                                   "写回失败: %s", e)
        else:
            failed += 1
            if line_account_id:
                try:
                    _lp.mark_dispatch_outcome(
                        line_account_id, status="failed",
                        note=err_note or "send_message_returned_false")
                except Exception:
                    pass
            # Phase 12.2: peer 级失败计数 + 达阈值 tag referral_dead.
            # 只 PERMANENT code 计 peer 账 (transient/unknown 不算 peer 问题).
            if (canonical_id_of_peer
                    and err_code in _REFERRAL_PEER_FAIL_THRESHOLDS):
                try:
                    became_dead = _referral_peer_fail_record(
                        canonical_id_of_peer, err_code, peer_name)
                except Exception as e:
                    logger.debug("[referral.send] peer fail 累计失败: %s", e)

        outcomes.append({
            "peer_name": peer_name, "line_account_id": line_account_id,
            "line_id": meta.get("line_id"),
            "planned_event_id": ev.get("id"),
            "sent": ok, "err_code": err_code, "note": err_note,
            "effective_mode": effective_mode,
            "became_dead": became_dead,
        })

    # Phase 12.6: simulated_duration_ms 估算真跑预计耗时 (dry_run 也算, 给
    # 运营直观感受 "启用后每轮要多久"). 估算:
    #   (would_send - 1) * avg_interval_sec * 1000 ← rate-limit sleep
    #   + would_send * estimated_send_ms            ← 每条 send 估 8s 平均
    # 注: retry 开销省略 (transient 错误概率低, 估算偏保守下界).
    would_send_n = (sent + failed
                     if not dry_run
                     else sum(1 for o in outcomes
                               if o.get("err_code") == "dry_run"))
    avg_iv = (min_iv + max_iv) / 2.0
    # estimated_send_ms 允许 caller 覆盖 (真机测后调)
    est_send_ms = int(params.get("estimated_send_ms", 8000) or 8000)
    if would_send_n > 0:
        sim_ms = int(max(0, would_send_n - 1) * avg_iv * 1000
                      + would_send_n * est_send_ms)
    else:
        sim_ms = 0
    m, s = divmod(sim_ms // 1000, 60)
    sim_human = f"约 {m} 分 {s} 秒" if m else f"约 {s} 秒"

    stats = {
        "scanned": len(events),
        "sent": sent,
        "failed": failed,
        "skipped_dedup": skipped_dedup,
        "skipped_device": skipped_device,
        "skipped_mode": skipped_mode,
        "outcomes": outcomes,
        "dry_run": dry_run,
        "simulated_duration_ms": sim_ms,
        "simulated_duration_human": sim_human,
    }
    return True, "", stats


# ═══════════════════════════════════════════════════════════════════════
# Phase 20.1 (2026-04-25): facebook_check_referral_replies
# A 调度 + 关键词字典加载, 实际 UI 抓取由 B 侧 check_messenger_inbox
# (referral_mode=True) 提供.
# ═══════════════════════════════════════════════════════════════════════

_REFERRAL_KEYWORDS_CACHE: Dict[str, Any] = {"loaded_at": 0.0, "data": None}
_REFERRAL_KEYWORDS_TTL_SEC = 300  # 5 分钟


def _load_referral_keywords(force: bool = False) -> Dict[str, List[str]]:
    """读 config/referral_reply_keywords.yaml, 5min TTL 缓存.

    返 dict: {region: [keyword, ...]}, 含 "default" 兜底组. 文件不存在或解析
    失败返 {"default": []} (调用方应当 default 为 0 keyword → 全部不命中).
    """
    import time as _t
    now = _t.time()
    if (not force and _REFERRAL_KEYWORDS_CACHE["data"] is not None
            and (now - _REFERRAL_KEYWORDS_CACHE["loaded_at"])
            < _REFERRAL_KEYWORDS_TTL_SEC):
        return _REFERRAL_KEYWORDS_CACHE["data"]
    from pathlib import Path as _Path
    out: Dict[str, List[str]] = {"default": []}
    try:
        import yaml as _yaml
        # 项目根: src/host/executor.py → ../..
        here = _Path(__file__).resolve().parent.parent.parent
        ypath = here / "config" / "referral_reply_keywords.yaml"
        if ypath.exists():
            with ypath.open(encoding="utf-8") as f:
                data = _yaml.safe_load(f) or {}
            if isinstance(data, dict):
                for region, words in data.items():
                    if isinstance(words, list):
                        out[str(region).lower()] = [
                            str(w).strip().lower()
                            for w in words if str(w).strip()]
    except Exception as e:
        logger.warning("[referral_keywords] load 失败: %s", e)
    _REFERRAL_KEYWORDS_CACHE["data"] = out
    _REFERRAL_KEYWORDS_CACHE["loaded_at"] = now
    return out


def _match_referral_keyword(text: str, region: str = "") -> str:
    """在 text 中找首个匹配的关键词 (lowercased substring 匹配).

    region 优先级: <region> > default. 匹配返关键词字符串, 不匹配返 ""."""
    if not text:
        return ""
    txt = text.lower()
    kws_map = _load_referral_keywords()
    pools: List[List[str]] = []
    if region:
        pool = kws_map.get(region.lower())
        if pool:
            pools.append(pool)
    pools.append(kws_map.get("default", []))
    for pool in pools:
        for kw in pool:
            if kw and kw in txt:
                return kw
    return ""


# Phase 20.1.8.1: peer→region TTL 缓存 (减少 cron 重复 SQL).
# key = peer_name (str), value = (region_str, expires_at_epoch).
_PEER_REGION_CACHE: Dict[str, tuple] = {}
_PEER_REGION_CACHE_TTL_SEC = 300  # 5 分钟


def _peer_region_cache_clear() -> None:
    """运维 / 测试: 强清缓存."""
    _PEER_REGION_CACHE.clear()


def _resolve_peer_regions(peer_names: List[str],
                            use_cache: bool = True) -> Dict[str, str]:
    """Phase 20.1.7.1 / 20.1.8.1 (2026-04-25): batch peer_name → region map.

    实现:
      1. 先查缓存 (5min TTL): 命中的 peer 直接复用
      2. 未命中的批量查 lead_identities (一次 SQL) → canonical_id
      3. 对每个有 canonical_id 的 peer 调 line_pool._get_lead_region (3 级 fallback)
      4. 写入缓存

    use_cache=False 时绕开缓存 (测试 / 运维强刷).

    cache 设计:
      * "" (没找到 region) 也缓存, TTL 内不再重查 (避免 ghost peer 反复查)
      * cache 是进程内, 重启自动清除; 跨进程不共享 (cron 同进程跑, 够用)
    """
    if not peer_names:
        return {}
    import time as _t
    now = _t.time()
    out: Dict[str, str] = {}
    miss: List[str] = []
    if use_cache:
        for pn in peer_names:
            entry = _PEER_REGION_CACHE.get(pn)
            if entry and entry[1] > now:
                out[pn] = entry[0]
            else:
                miss.append(pn)
    else:
        miss = list(peer_names)

    if not miss:
        return out

    try:
        from src.host.lead_mesh.canonical import _connect as _lm_connect
        from src.host.line_pool import _get_lead_region
        placeholders = ",".join(["?"] * len(miss))
        fb_keys = [f"fb:{n}" for n in miss]
        with _lm_connect() as conn:
            rows = conn.execute(
                "SELECT account_id, canonical_id FROM lead_identities"
                f" WHERE platform='facebook' AND account_id IN ({placeholders})",
                fb_keys).fetchall()
        peer_to_cid: Dict[str, str] = {}
        for r in rows:
            aid = r["account_id"] or ""
            if aid.startswith("fb:"):
                peer_to_cid[aid[3:]] = r["canonical_id"]
        expires = now + _PEER_REGION_CACHE_TTL_SEC
        for pn in miss:
            cid = peer_to_cid.get(pn)
            rg = _get_lead_region(cid) if cid else ""
            out[pn] = rg
            if use_cache:
                _PEER_REGION_CACHE[pn] = (rg, expires)
    except Exception as e:
        logger.debug("[resolve_peer_regions] 失败: %s", e)
        # miss 全部填 "" 兜底
        for pn in miss:
            out.setdefault(pn, "")
    # 保 key 顺序一致
    return {pn: out.get(pn, "") for pn in peer_names}


def _parse_event_at(at_str: str) -> Optional[float]:
    """Phase 20.1.7.2: parse fb_contact_events.at 列回 epoch 秒.

    支持两种格式:
      * SQLite datetime('now') 默认: 'YYYY-MM-DD HH:MM:SS' (空格)
      * meta.sent_at 写的: 'YYYY-MM-DDTHH:MM:SSZ' (T+Z)

    解析失败返 None.
    """
    if not at_str:
        return None
    import datetime as _dt
    s = str(at_str).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(
                tzinfo=_dt.timezone.utc).timestamp()
        except Exception:
            continue
    return None


def _fb_check_referral_replies(fb, resolved: str,
                                  params: Dict[str, Any]) -> tuple:
    """Phase 20.1 / 20.1.7 (2026-04-25): 扫 Messenger inbox 找 referral 反馈.

    A 侧调度逻辑 (本函数):
      1. 拉 pending peers (wa_referral_sent 后 N 小时内未 replied)
      2. Phase 20.1.7.1: batch 解析 peer → region 用于关键词路由
      3. 调 fb.check_messenger_inbox(referral_mode=True, peers_filter=names) — B 实
      4. B 返 list of {peer_name, last_inbound_text, conv_id} (匹配 + 未匹配都返)
      5. 本函数遍历 inbound_text, 关键词匹配 (按该 peer 自己的 region) → 写 event
      6. Phase 20.1.7.2: 写 event 时计算 latency_seconds 入 meta

    Params:
      hours_back: int = 48           pending 时间窗
      limit: int = 50                单次最多扫 peer 数
      keyword_region: str = ""       OVERRIDE — 强制用此 region; 空 → 自动推断 per-peer
      max_messages_per_peer: int = 5  B 侧每个 peer 最多抓最近几条
    """
    import datetime as _dt
    hours_back = int(params.get("hours_back", 48) or 48)
    limit = max(1, min(int(params.get("limit", 50) or 50), 500))
    region_override = str(params.get("keyword_region", "") or "").lower()
    msgs_per_peer = max(1, min(
        int(params.get("max_messages_per_peer", 5) or 5), 20))

    from src.host.fb_store import (get_pending_referral_peers,
                                    record_contact_event,
                                    CONTACT_EVT_WA_REFERRAL_REPLIED)

    # 步骤 1: pending peers (限本设备)
    pending = get_pending_referral_peers(device_id=resolved,
                                          hours_back=hours_back,
                                          limit=limit)
    if not pending:
        return True, "", {
            "pending_count": 0, "scanned": 0,
            "replied_now": 0, "no_match": 0,
            "matches": [],
        }

    peer_names = [p["peer_name"] for p in pending]

    # Phase 20.1.7.1: 提前 batch 解析 region (一次 SQL + 多次 _get_lead_region)
    if region_override:
        # 全 force 用同一 region
        peer_regions = {pn: region_override for pn in peer_names}
    else:
        peer_regions = _resolve_peer_regions(peer_names)

    # 步骤 2: 调 B 侧 inbox 检测 (尚未实装时 graceful)
    inbox_results: List[Dict[str, Any]] = []
    if not hasattr(fb, "check_messenger_inbox"):
        return False, "facebook.check_messenger_inbox 尚未实现", {
            "pending_count": len(pending), "scanned": 0,
            "replied_now": 0, "no_match": 0,
        }
    try:
        # B 侧 referral_mode 接口契约见 docs/A_TO_B_PHASE20_INBOX.md
        scan_result = fb.check_messenger_inbox(
            auto_reply=False,
            referral_mode=True,
            peers_filter=peer_names,
            max_messages_per_peer=msgs_per_peer,
            device_id=resolved,
        )
        # B 应返 {"conversations": [{"peer_name", "last_inbound_text", ...}, ...]}
        # 旧版 B 不认这些 kwargs 时退化只返 messenger_active 状态
        if isinstance(scan_result, dict):
            inbox_results = scan_result.get("conversations") or []
    except TypeError:
        # B 还没扩 referral_mode 参数 → 直接报告未对齐
        return False, ("B 侧 check_messenger_inbox 还没支持 referral_mode 参数, "
                       "见 docs/A_TO_B_PHASE20_INBOX.md"), {
            "pending_count": len(pending),
            "scanned": 0,
            "replied_now": 0,
            "no_match": 0,
        }
    except Exception as e:
        logger.warning("[referral_replies] check_messenger_inbox 失败: %s", e)
        return False, f"check_messenger_inbox 异常: {e}", {
            "pending_count": len(pending),
            "scanned": 0,
            "replied_now": 0,
            "no_match": 0,
        }

    # 步骤 3-4: 遍历 + 关键词匹配 + 写 event
    pending_by_peer = {p["peer_name"]: p for p in pending}
    replied_now = 0
    no_match = 0
    matches: List[Dict[str, Any]] = []
    now_ts = _dt.datetime.now(_dt.timezone.utc).timestamp()
    for conv in inbox_results:
        peer_name = (conv.get("peer_name") or "").strip()
        if not peer_name or peer_name not in pending_by_peer:
            continue
        text = conv.get("last_inbound_text") or ""
        # Phase 20.1.7.1: per-peer region (override 已在上面 force 全填好)
        peer_region = peer_regions.get(peer_name, "")
        kw = _match_referral_keyword(text, region=peer_region)
        if not kw:
            no_match += 1
            continue
        sent_meta = pending_by_peer[peer_name]
        # Phase 20.1.7.2: latency = now - sent_at (秒)
        sent_ts = _parse_event_at(sent_meta.get("sent_at") or "")
        latency_sec = (round(now_ts - sent_ts, 1)
                        if sent_ts and now_ts >= sent_ts else None)
        latency_min = (round(latency_sec / 60.0, 2)
                        if latency_sec is not None else None)
        try:
            record_contact_event(
                resolved, peer_name, CONTACT_EVT_WA_REFERRAL_REPLIED,
                meta={
                    "platform": "facebook",  # TG R2 Q2 cross-repo namespace
                    "keyword_matched": kw,
                    "raw_excerpt": text[:200],
                    "sent_event_id": sent_meta.get("sent_event_id"),
                    "sent_at": sent_meta.get("sent_at"),
                    "conv_id": conv.get("conv_id") or "",
                    "region": peer_region or None,
                    "latency_seconds": latency_sec,
                    "latency_min": latency_min,
                    "matched_by": "agent_a_phase20_1",
                })
            replied_now += 1
            matches.append({
                "peer_name": peer_name,
                "keyword": kw,
                "region": peer_region,
                "latency_min": latency_min,
                "excerpt": text[:80],
            })
        except Exception as e:
            logger.warning("[referral_replies] 写 wa_referral_replied 失败 "
                            "peer=%s: %s", peer_name, e)

    return True, "", {
        "pending_count": len(pending),
        "scanned": len(inbox_results),
        "replied_now": replied_now,
        "no_match": no_match,
        "matches": matches,
    }


def _fb_mark_stale_referrals(params: Dict[str, Any]) -> tuple:
    """Phase 20.2 (2026-04-25): SLA 死信回收 task wrapper.

    扫整个 referral funnel, 把 sent N 小时未 replied 的 peer 标 referral_stale,
    超 M 天升级 referral_dead. 跑完后会被 Phase 14 daily 回收链消化.

    Params:
      stale_hours: int = 48          多久未 replied 算 stale (建议 24-72)
      escalate_to_dead_days: int = 7 多久 stale 后升级 dead
      device_id: str = ""            可限制单 device, 默认全部
      dry_run: bool = False          不写入只统计 (推预览安全)
      limit: int = 500
    """
    stale_hours = int(params.get("stale_hours", 48) or 48)
    esc_days = int(params.get("escalate_to_dead_days", 7) or 7)
    device_id = (params.get("device_id") or "").strip() or None
    dry_run = bool(params.get("dry_run", False))
    limit = max(1, min(int(params.get("limit", 500) or 500), 5000))

    from src.host.fb_store import mark_stale_referrals
    stats = mark_stale_referrals(
        stale_hours=stale_hours,
        escalate_to_dead_days=esc_days,
        device_id=device_id,
        dry_run=dry_run,
        limit=limit)
    return True, "", stats


_FB_CAMPAIGN_DEFAULT_STEPS = ["warmup", "group_engage", "extract_members",
                              "add_friends", "check_inbox"]


def _as_str_list(value: Any) -> List[str]:
    """Normalize comma/newline separated request values into a clean list."""
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace(";", ",").replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = [value]
    out: List[str] = []
    for item in raw:
        s = str(item or "").strip()
        if s:
            out.append(s)
    return out


def _rank_groups(candidates: List[Dict[str, Any]],
                  max_keep: int = 5) -> List[Dict[str, Any]]:
    """挑群智能评分 (P3-B 2026-05-04, forensics task 808adb92 教训驱动).

    背景: 旧逻辑 ``discovered[:max_groups]`` 取前 N 候选 — discovered list 顺序
    由 FB 搜索结果决定, 不一定最优. 用户截图 17:08 显示搜 '潮味' 返回 7 个
    Public 群但全是 ``Join`` 状态 (需审批), bot 任意挑一个 join_request 就
    pending — extract 全 0. 智能评分让 bot 先选最容易跑通的群.

    评分维度 (经验值, 真机迭代调整):
      already_joined  +1000  (无需 Join, 直接 enter, 100% 跑通)
      Public           +30   (开放群直接 Join 不需审批)
      Private          -20   (需审批, pending 概率高)
      members ∈ [100, 10k]   +50   (sweet spot: 活跃 + Join 通常无 question wall)
      members ∈ (10k, 100k]  +20   (中型群活跃但 question wall 概率上升)
      members > 100k         +10   (70K+ 大群 Join 时 question wall 几乎必现 -
                                    真机 task 5af81143 '香港燒味關注組' 70K 实测)
      members < 100          +5    (小群成员太少, extract 收益低)
      posts/day            +min(p, 10) * 3   (活跃度上限 30 加分)

    P4-B (2026-05-04) sweet spot 从 [100,100k] 缩到 [100,10k]: 真机
    task 5af81143 暴露 70K Public 大群 Join 时 FB 弹 'Write an answer'
    question wall, bot 卡住 extract 0 人. 评分降权大群让 bot 优先选小型
    Public 群 (不依赖 question wall 识别也能工作).

    候选 dict 接受字段 (兼容 discover_groups_by_keyword 输出 + UI 抓的字段):
      already_joined / joined / is_joined  → bool
      members / member_count               → int
      posts_per_day / posts_24h            → float
      is_public / privacy ('public'/'private')  → bool/str
    """
    def _score(g: Dict[str, Any]) -> float:
        s = 0.0
        if (g.get("already_joined") or g.get("joined")
                or g.get("is_joined")):
            s += 1000.0
        members = int(g.get("members") or g.get("member_count") or 0)
        if 100 <= members <= 10_000:
            s += 50.0
        elif 10_000 < members <= 100_000:
            s += 20.0
        elif members > 100_000:
            s += 10.0
        else:
            s += 5.0
        ppd = float(g.get("posts_per_day") or g.get("posts_24h") or 0)
        s += min(ppd, 10.0) * 3.0
        privacy = (g.get("privacy") or "").lower()
        if g.get("is_public") or privacy == "public":
            s += 30.0
        elif privacy == "private":
            s -= 20.0
        return s

    if not candidates:
        return []
    ranked = sorted(candidates, key=_score, reverse=True)
    return ranked[:max_keep]


def _dedup_groups_across_devices(
    discovered: List[Dict[str, Any]], device_id: str,
) -> List[Dict[str, Any]]:
    """Filter out groups already claimed by other devices in the last 2 hours.

    Uses a lightweight sqlite table ``device_group_claims`` for coordination.
    Each device claims the groups it will process; other devices skip them.
    """
    if not discovered:
        return discovered
    try:
        import sqlite3, time as _t
        db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "openclaw.db")
        conn = sqlite3.connect(db_path, timeout=5)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS device_group_claims (
                group_name TEXT NOT NULL,
                device_id  TEXT NOT NULL,
                claimed_at REAL NOT NULL,
                PRIMARY KEY (group_name, device_id)
            )
        """)
        # purge stale claims (> 2 hours)
        conn.execute("DELETE FROM device_group_claims WHERE claimed_at < ?",
                      (_t.time() - 7200,))

        # find groups claimed by OTHER devices
        rows = conn.execute(
            "SELECT DISTINCT group_name FROM device_group_claims WHERE device_id != ?",
            (device_id,),
        ).fetchall()
        taken = {r[0] for r in rows}

        # filter and claim
        result = []
        now = _t.time()
        for g in discovered:
            gn = (g.get("group_name") or "").strip()
            if not gn:
                continue
            if gn in taken:
                logger.info("[group-dedup] %s 跳过群 %r (已被其他设备认领)", device_id[:8], gn)
                continue
            result.append(g)
            conn.execute(
                "INSERT OR REPLACE INTO device_group_claims (group_name, device_id, claimed_at) VALUES (?, ?, ?)",
                (gn, device_id, now),
            )
        conn.commit()
        conn.close()
        if len(result) < len(discovered):
            logger.info("[group-dedup] %s: %d/%d 群通过去重",
                        device_id[:8], len(result), len(discovered))
        return result if result else discovered[:1]  # at least keep 1
    except Exception as e:
        logger.warning("[group-dedup] 跨设备去重失败 (继续用全部): %s", e)
        return discovered


def _campaign_member_sources(params: Dict[str, Any]) -> List[str]:
    sources = _as_str_list(params.get("member_sources") or params.get("member_source"))
    # 2026-05-03 v16: feed_authors 加入合法 source — 新版 FB 关闭了 Members
    # 列表入口, 帖子作者法是合法兜底.
    allowed = {"mutual_members", "contributors", "general", "feed_authors"}
    clean = [s for s in sources if s in allowed]
    # 2026-05-04: 真机验证新版 FB 公开群 Members tab/section 全部移除
    # (Featured/You/Photos/Events 顶 tab bar, 三点菜单也无 Members 选项).
    # 把 feed_authors 加入默认 fallback 链, 让前 3 路径全 miss 后还有出路.
    return clean or ["mutual_members", "contributors", "general", "feed_authors"]


def _campaign_extract_members(fb, resolved: str, params: Dict[str, Any],
                              group_name: str) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Extract members inside one campaign task, preserving candidates for later steps.

    This is deliberately campaign-local: the friend_growth workflow should not
    create a separate extract task and then hope a second task can rediscover its
    output. The returned members are written directly to result.last_extracted_members.
    """
    group_name = (group_name or "").strip()
    if not group_name:
        return [], {
            "outcome": "missing_param:group_name",
            "groups": [],
            "hint": "缺少目标群组，无法采集群成员候选。",
        }

    total_cap = int(
        params.get("extract_max_members")
        or params.get("max_members")
        or params.get("max_members_per_group")
        or 30
    )
    per_group_cap = int(params.get("max_members_per_group") or total_cap or 30)
    max_groups = int(params.get("max_groups_to_extract") or params.get("max_groups") or 1)
    broad_keyword = bool(
        params.get("broad_keyword")
        or params.get("discover_groups")
        or max_groups > 1
    )
    join_if_needed = bool(
        params.get("join_if_needed")
        or params.get("auto_join_groups")
        or params.get("auto_join")
    )

    discovered: List[Dict[str, Any]] = []
    if broad_keyword and hasattr(fb, "discover_groups_by_keyword"):
        try:
            discovered = fb.discover_groups_by_keyword(
                group_name,
                device_id=resolved,
                max_groups=max_groups * 3,
                skip_visited=bool(params.get("skip_visited", True)),
                persona_key=params.get("persona_key") or None,
                target_country=params.get("target_country", ""),
            ) or []
        except Exception as e:
            logger.warning("[FB Campaign] discover groups failed keyword=%r: %s",
                           group_name, e)
            discovered = []
    if not discovered:
        discovered = [{"group_name": group_name, "keyword": group_name,
                       "requires_join": False}]

    # P3-B (2026-05-04): 智能评分挑群 — already_joined > Public 中等规模 > 私群.
    # 旧逻辑直接 ``discovered[:max_groups]`` 取顺序前 N, 容易选到全是需审批
    # 的私群导致全 task pending. 评分让 bot 先跑最容易通的群.
    if len(discovered) > 1:
        try:
            _ranked = _rank_groups(discovered, max_keep=max_groups * 2)
            if _ranked:
                logger.info(
                    "[FB Campaign] 挑群评分: 候选 %d 取 top %d (top1=%r)",
                    len(discovered), len(_ranked),
                    _ranked[0].get("group_name"),
                )
                discovered = _ranked
        except Exception as _rk_e:
            logger.warning("[FB Campaign] _rank_groups 失败 (用原顺序): %s", _rk_e)

    # 2026-05-12: 跨设备群组去重 — 避免所有设备进入相同群组
    discovered = _dedup_groups_across_devices(discovered, resolved)

    all_members: List[Dict[str, Any]] = []
    group_results: List[Dict[str, Any]] = []
    source_order = _campaign_member_sources(params)
    # P1-A: 池级聚合统计 — 让 dashboard / 运营能直接看到三池产出比，
    # 而不需要从 group_results.sources 二次计算。键固定为 source_order
    # 三个池名（不存在的池保留 0），避免前端做 None 判定。
    pool_breakdown: Dict[str, Dict[str, int]] = {
        s: {"yielded": 0, "calls": 0, "cap_hits": 0}
        for s in source_order
    }

    for _gidx, g in enumerate(discovered[:max_groups]):
        exact_group = (g.get("group_name") or "").strip()
        if not exact_group:
            continue
        # P6-A (2026-05-04): 每群循环入口 wake screen + dump probe.
        # 真机 task ef707204 (2026-05-04 20:21+) 暴露第 4-5 群重新 enter_group
        # 时 5 路 IME silent no-op + atx-agent dump 偶发 0 (running_total
        # 17/40 而非 40/40 因此卡住). task 入口 P3-A wake 已生效但 task 跑 40+
        # 分钟后 atx-agent 偶发挂, 每群循环重新 wake + healthcheck 解决.
        # 第一群跳过 (task 入口刚 wake 过 < 1 min, 不重复触发).
        if _gidx > 0 and hasattr(fb, "_ensure_screen_awake"):
            try:
                fb._ensure_screen_awake(resolved)
            except Exception as _wake_e:
                logger.debug(
                    "[FB Campaign] per-group wake (gidx=%d) failed: %s",
                    _gidx, _wake_e,
                )
        group_members: List[Dict[str, Any]] = []
        source_results: List[Dict[str, Any]] = []
        for source in source_order:
            if len(all_members) >= total_cap:
                break
            source_cap = max(1, min(per_group_cap, total_cap - len(all_members)))
            kwargs = {
                "group_name": exact_group,
                "max_members": source_cap,
                "use_llm_scoring": bool(params.get("use_llm_scoring", False)),
                "target_country": params.get("target_country", ""),
                "device_id": resolved,
                "persona_key": params.get("persona_key") or None,
                "phase": params.get("phase") or params.get("phase_override") or None,
                "join_if_needed": join_if_needed,
            }
            if source:
                kwargs["member_source"] = source
            # 2026-05-03 v16: feed_authors 走独立 helper (从帖子流抓作者),
            # 绕过 FB 已限制的 Members 列表权限. 其它 source 仍走传统
            # extract_group_members.
            if source == "feed_authors" and hasattr(
                fb, "extract_group_feed_authors"
            ):
                _kw_authors = dict(kwargs)
                _kw_authors.setdefault(
                    "max_scrolls", int(params.get("feed_max_scrolls", 20))
                )
                # v19: 透传 L1 预筛阈值 + target_groups 给 scorer
                _kw_authors["min_l1_score"] = int(
                    params.get("min_l1_score", 0)
                )
                _kw_authors["target_groups"] = (
                    params.get("target_groups") or []
                )
                members_g = fb.extract_group_feed_authors(**_kw_authors) or []
            else:
                members_g = fb.extract_group_members(**kwargs) or []
            for m in members_g:
                if isinstance(m, dict):
                    m.setdefault("source_section", source or "general")
                    m.setdefault("source_group", exact_group)
            group_members.extend(members_g)
            all_members.extend(members_g)
            source_results.append({
                "source": source or "general",
                "members": len(members_g),
            })
            # P1-A: 池级日志/事件/聚合 — 让运营能立即看到「mutual=4 contributors=2 general=0」
            # 这种结构化产出，定位是 mutual 池没人还是 contributors 死了。
            _src_key = source or "general"
            _bd = pool_breakdown.setdefault(
                _src_key, {"yielded": 0, "calls": 0, "cap_hits": 0})
            _bd["yielded"] += len(members_g)
            _bd["calls"] += 1
            if len(members_g) >= source_cap:
                _bd["cap_hits"] += 1
            logger.info(
                "[FB Campaign] pool=%s group=%r yielded=%d/%d "
                "(running_total=%d/%d)",
                _src_key, exact_group, len(members_g), source_cap,
                len(all_members), total_cap,
            )
            try:
                from src.host.event_stream import push_event as _push_pool
                _push_pool("facebook.member_pool_yield", {
                    "device_id": resolved,
                    "source": _src_key,
                    "group_name": exact_group,
                    "yielded": len(members_g),
                    "cap": source_cap,
                    "running_total": len(all_members),
                    "total_cap": total_cap,
                }, resolved)
            except Exception:
                pass

            if len(all_members) >= total_cap:
                break

        error_step = None
        if not group_members:
            try:
                from src.app_automation.facebook import consume_last_extract_error
                error_step = consume_last_extract_error(resolved)
            except Exception:
                error_step = None
        status = "extracted" if group_members else (
            "members_tab_not_found" if error_step == "members_tab_not_found"
            else "zero_members"
        )
        group_results.append({
            "group_name": exact_group,
            "members": len(group_members),
            "status": status,
            "requires_join": bool(g.get("requires_join")),
            "error_step": error_step,
            "sources": source_results,
        })
        if len(all_members) >= total_cap:
            break

    meta = {
        "groups": group_results,
        "discovered_groups": discovered,
        "member_sources": source_order,
        "pool_breakdown": pool_breakdown,
        "outcome": "ok" if all_members else "automation_extract_zero_after_discovery",
        "keyword": group_name,
    }
    return all_members[:total_cap], meta


def _campaign_failure_message(result: Dict[str, Any]) -> str:
    failed = result.get("steps_failed") or []
    if failed:
        first = failed[0] or {}
        step = first.get("step") or "unknown"
        err = first.get("error") or "unknown_error"
        return f"Facebook 剧本未完成：{step} 阶段失败 ({err})"
    return "Facebook 剧本未完成"


def _run_facebook_campaign(fb, resolved, params):
    """Facebook 5 套预设串行剧本的服务端实现。

    与 TikTok 的 tiktok_campaign_run 同构,但步骤适配 FB 业务模型:
      warmup           — feed 浏览 + 点赞养号
      group_engage     — 进群浏览 + 评论
      extract_members  — 群成员打招呼准备名单入库
      add_friends      — 对入库成员发好友请求(带验证语)
      check_inbox      — 处理 Messenger / Message Requests / Friend Requests

    P1-3a: 断点续跑 —— run_id = 当前 task_id（或 params.resume_from_run_id）。
          已在 fb_campaign_runs 表里标记 completed 的步骤会被跳过，
          整段任务继续往下跑；state_json 里的累计指标会带过来。
    """
    import random as _r
    from src.host.fb_add_friend_gate import check_add_friend_gate as _fb_add_friend_gate

    steps = params.get("steps") or _FB_CAMPAIGN_DEFAULT_STEPS
    target_country = params.get("target_country", "")
    target_groups = params.get("target_groups") or params.get("group_names") or []
    if isinstance(target_groups, str):
        target_groups = [g.strip() for g in target_groups.split(",") if g.strip()]
    # 2026-05-10: chain 路径不经过 routers/facebook.py 的注入逻辑，
    # target_groups 可能为空。从 persona seed_group_keywords 兜底。
    if not target_groups:
        try:
            from src.host.fb_target_personas import get_persona_display, get_default_persona_key
            _pk = params.get("persona_key") or get_default_persona_key()
            _pd = get_persona_display(_pk) or {}
            _seeds = _pd.get("seed_group_keywords") or []
            if _seeds:
                target_groups = list(_seeds)[:3]
                logger.info("[FB Campaign] target_groups 为空, 使用 persona(%s) seed: %s",
                            _pk, target_groups)
        except Exception as _persona_e:
            logger.debug("[FB Campaign] persona seed 兜底失败: %s", _persona_e)

    # P1-3a: 读取/建立运行记录
    resume_from = params.get("resume_from_run_id") or ""
    try:
        from src.host.fb_campaign_store import (start_run as _cr_start,
                                                 update_step as _cr_update,
                                                 finish_run as _cr_finish)
        run_id = resume_from or (_get_current_task_id() or "")
    except Exception:
        _cr_start = _cr_update = _cr_finish = None
        run_id = ""

    resumed_state: Dict[str, Any] = {}
    if run_id and _cr_start:
        resumed_state = _cr_start(
            run_id=run_id,
            device_id=resolved,
            task_id=(_get_current_task_id() or ""),
            preset_key=str(params.get("preset_key") or ""),
            total_steps=len(steps),
        ) or {}

    already_done = set(resumed_state.get("steps_completed") or [])

    result = {
        "card_type": "fb_campaign",
        "run_id": run_id,
        "resumed": bool(resumed_state and already_done),
        "steps_completed": list(already_done),
        "steps_failed": list(resumed_state.get("steps_failed") or []),
        "target_country": target_country,
        "extracted_members": int(resumed_state.get("extracted_members") or 0),
        "friend_requests_sent": int(resumed_state.get("friend_requests_sent") or 0),
        "messages_replied": int(resumed_state.get("messages_replied") or 0),
    }
    # 透传已抽取成员，add_friends 可以接着发
    if resumed_state.get("last_extracted_members"):
        result["last_extracted_members"] = resumed_state["last_extracted_members"]

    # P3-A (2026-05-04): task 入口主动唤屏 + dump probe.
    # 防 IJ8H 类设备 uiautomator dump 偶发被 system SIGKILL 致 task 全链路
    # 误判失败 (forensics task 808adb92 2026-05-04 17:05 真实事故根因 —
    # 5 路 IME 实际成功了但 dump 拿空 hierarchy → bot 看不见 UI).
    if hasattr(fb, "_ensure_screen_awake"):
        try:
            _wake_ok = fb._ensure_screen_awake(resolved)
            result["screen_awake_probe"] = bool(_wake_ok)
            if not _wake_ok:
                logger.warning(
                    "[FB Campaign] %s screen/dump probe 失败 — task 仍继续, "
                    "但若后续步骤 hierarchy 拿空, 优先排查 OEM/system "
                    "kill uiautomator (forensics 区分依据)",
                    resolved,
                )
        except Exception as _wake_e:
            logger.warning(
                "[FB Campaign] _ensure_screen_awake exception: %s", _wake_e,
            )

    for idx, step in enumerate(steps):
        if step in already_done:
            logger.info("[FB Campaign] 跳过已完成步骤: %s (run=%s)", step, run_id[:12])
            continue
        # 2026-05-12: 每步开始前强制竖屏，防止运行期间自动旋转
        try:
            from src.device_control.device_manager import get_device_manager as _gdm
            _lock_portrait(_gdm(), resolved)
        except Exception:
            pass
        # 2026-05-12: 每步开始前确保 Facebook 在前台
        # 使用 app_stop + am start 确保 FB 干净启动到 Home，避免 MIUI Home
        # 的 Google 搜索栏被误触、或 FB 残留在非 Home 子页。
        try:
            import time as _t_fg
            _cur = (fb._adb("shell dumpsys window | grep mCurrentFocus",
                            device_id=resolved, timeout=5) or "")
            if "com.facebook.katana" not in _cur and "com.facebook.orca" not in _cur:
                logger.info("[FB Campaign] 步骤 %s: FB 不在前台(%s)，stop+start",
                            step, _cur.strip()[:60])
                fb._adb("shell am force-stop com.facebook.katana",
                         device_id=resolved, timeout=5)
                _t_fg.sleep(1.5)
                fb._adb("shell am start -n com.facebook.katana/.LoginActivity",
                         device_id=resolved, timeout=8)
                # 等待 FB 完全加载到 Home (最多 8 秒)
                for _wait_i in range(8):
                    _t_fg.sleep(1.0)
                    _chk = (fb._adb("shell dumpsys window | grep mCurrentFocus",
                                    device_id=resolved, timeout=5) or "")
                    if "com.facebook.katana" in _chk:
                        break
                else:
                    logger.warning("[FB Campaign] 步骤 %s: FB 启动 8s 后仍不在前台", step)
        except Exception as _fg_e:
            logger.warning("[FB Campaign] 步骤 %s 前拉起 FB 失败: %s", step, _fg_e)
        # 截图验证 UI 状态
        try:
            from src.host.task_forensics import capture_immediate
            capture_immediate(resolved, step_name=f"step_{step}_start",
                              hint=f"run={run_id[:8]}")
        except Exception:
            pass
        try:
            if step == "warmup":
                stats = fb.browse_feed(
                    scroll_count=int(params.get("warmup_scrolls", 15)),
                    like_probability=float(params.get("warmup_like_prob", 0.2)),
                    device_id=resolved,
                )
                result.setdefault("warmup_stats", stats)

            elif step == "group_engage":
                if not hasattr(fb, "group_engage_session"):
                    result["steps_failed"].append({"step": step,
                                                   "error": "未实现 group_engage_session"})
                    continue
                first_group = target_groups[0] if target_groups else params.get("group_name", "")
                stats = fb.group_engage_session(
                    group_name=first_group,
                    max_posts=int(params.get("group_max_posts", 5)),
                    comment_probability=float(params.get("comment_probability", 0.2)),
                    device_id=resolved,
                    persona_key=params.get("persona_key") or None,
                    phase=params.get("phase") or params.get("phase_override") or None,
                )
                result.setdefault("group_engage_stats", stats)

            elif step == "smart_engage":
                # ── 智能社群互动引擎 (2026-05-12) ──
                # 合并 group_engage + extract_members + add_friends 为一个步骤:
                # 浏览帖子 → 分析 → 点赞 → 进 profile → 加友
                import yaml as _se_yaml
                from .device_registry import config_file as _se_config_file
                _se_cfg_path = _se_config_file("smart_engage.yaml")
                _se_cfg = {}
                try:
                    if _se_cfg_path.exists():
                        with open(_se_cfg_path, encoding="utf-8") as _sf:
                            _se_all = _se_yaml.safe_load(_sf) or {}
                        _se_cfg = dict(_se_all.get("default") or {})
                        _persona_cfg_key = params.get("persona_key") or ""
                        if _persona_cfg_key and _persona_cfg_key in _se_all:
                            _se_cfg.update(_se_all[_persona_cfg_key])
                except Exception as _se_e:
                    logger.warning("[smart_engage] config load error: %s", _se_e)

                _se_total_friends = 0
                _se_total_likes = 0
                _se_all_stats = []
                _se_max = int(_se_cfg.get("max_friends_per_session", 6))
                _se_fallback_to_members = False

                # 遍历目标群组
                _se_groups = target_groups or [params.get("group_name", "")]
                for _g in _se_groups:
                    if not _g:
                        continue
                    if _se_total_friends >= _se_max:
                        break

                    logger.info("[smart_engage] entering group: %s", _g)
                    try:
                        _g_stats = fb.smart_engage_session(
                            group_name=_g,
                            device_id=resolved,
                            persona_key=params.get("persona_key"),
                            phase=params.get("phase") or params.get("phase_override"),
                            config=_se_cfg,
                            join_if_needed=bool(params.get("join_if_needed", True)),
                        )
                    except Exception as _g_err:
                        logger.warning("[smart_engage] group %s error: %s", _g, _g_err)
                        _g_stats = {"group": _g, "error": str(_g_err),
                                    "friend_requests_sent": 0, "likes": 0}
                    _se_all_stats.append(_g_stats)
                    _se_total_friends += _g_stats.get("friend_requests_sent", 0)
                    _se_total_likes += _g_stats.get("likes", 0)

                    # 风险检测 → 立即停止所有群
                    if _g_stats.get("risk_detected"):
                        logger.warning("[smart_engage] risk detected in %s: %s",
                                       _g, _g_stats["risk_detected"])
                        result["risk_detected"] = _g_stats["risk_detected"]
                        break

                    # 连续 C 类 → 换群 (已在 smart_engage_session 内处理)
                    if _g_stats.get("switch_reason") == "consecutive_c":
                        logger.info("[smart_engage] low quality feed in %s, next group", _g)
                        continue

                # 降级: 所有群都低质量且 0 好友 → fallback Members 列表
                # (须有至少 1 个群结果, 避免空列表时 all() 恒 True)
                if (_se_total_friends == 0
                        and _se_all_stats
                        and bool(_se_cfg.get("fallback_to_members_list", True))
                        and all(s.get("switch_reason") == "consecutive_c"
                                for s in _se_all_stats if s.get("switch_reason"))):
                    logger.info("[smart_engage] all groups low quality, "
                                "fallback to extract_members")
                    _se_fallback_to_members = True

                result.setdefault("smart_engage_stats", _se_all_stats)
                result["smart_engage_total_friends"] = _se_total_friends
                result["smart_engage_total_likes"] = _se_total_likes
                result["extracted_members"] = _se_total_friends

                # 如果需要降级, 跳到 extract_members 逻辑
                if _se_fallback_to_members:
                    first_group = target_groups[0] if target_groups else ""
                    if first_group:
                        members, extract_meta = _campaign_extract_members(
                            fb, resolved, params, first_group)
                        result["extracted_members"] += len(members)
                        result["last_extracted_members"] = members
                        result["fallback_used"] = "members_list"

            elif step == "extract_members":
                if not hasattr(fb, "extract_group_members"):
                    result["steps_failed"].append({"step": step,
                                                   "error": "未实现 extract_group_members"})
                    continue
                first_group = target_groups[0] if target_groups else params.get("group_name", "")
                members, extract_meta = _campaign_extract_members(
                    fb, resolved, params, first_group)
                result["extracted_members"] += len(members)
                result["last_extracted_members"] = members
                result["member_source_stats"] = extract_meta.get("groups", [])
                result["discovered_groups"] = extract_meta.get("discovered_groups", [])
                # P1-A: 跨群跨池的扁平产出聚合 — dashboard / 漏斗面板用一行就能渲染
                # 「有共同点 X / 小组贡献者 Y / 通用 Z」。
                if extract_meta.get("pool_breakdown"):
                    result["member_pool_breakdown"] = extract_meta["pool_breakdown"]
                # P1.5 (2026-04-30): 进群但 0 成员 → step-level 失败标记
                # 让 task_store 取证触发条件命中（result.steps_failed 非空 → forensics）
                if not members and first_group:
                    result["steps_failed"].append({
                        "step": step,
                        "error": extract_meta.get("outcome") or "automation_extract_zero_after_enter",
                        "meta": {
                            "group_name": first_group,
                            "outcome": extract_meta.get("outcome") or "automation_extract_zero_after_enter",
                            "groups": extract_meta.get("groups", []),
                            "hint": "进群后未提取到成员。可能 FB UI 改版/群隐私/选择器失效。"
                                    "点击徽章查看失败现场截图。",
                        },
                    })

            elif step == "add_friends":
                _gerr, _gmeta = _fb_add_friend_gate(resolved, params)
                if _gerr:
                    # 2026-05-10: playbook 阶段策略（cold_start 等）禁止 add_friends
                    # 是正常运营节奏，不应让整个 campaign 标记失败从而 abort 链。
                    # extract_members 产出的成员仍有价值（下次 campaign 可复用）。
                    if "cold_start" in _gerr or "playbook" in _gerr.lower():
                        logger.info("[FB Campaign] add_friends 策略跳过 (非关键): %s",
                                    _gerr[:100])
                        result.setdefault("add_friends_skipped_reason", _gerr[:120])
                    else:
                        result["steps_failed"].append({"step": step, "error": _gerr, "meta": _gmeta})
                    continue
                targets = params.get("add_friend_targets") \
                          or result.get("last_extracted_members") \
                          or []
                targets, _hm_meta = _fb_filter_high_match_targets(targets, params)
                if _hm_meta.get("enabled"):
                    result["high_match_filter"] = _hm_meta
                # 2026-04-24 P0 fail-fast: 上游 extract 步骤返回 0 人且没有手工
                # add_friend_targets 时, 后续 add_friends 空 loop 会让 result
                # 欺骗性地显示 success=True. 明确标记 skipped + 原因.
                if not targets:
                    _skip_meta = {
                        "extracted_members": result.get("extracted_members", 0),
                        "has_manual_targets": bool(params.get("add_friend_targets")),
                        "high_match_filter": _hm_meta,
                    }
                    result["steps_failed"].append({
                        "step": step,
                        "error": ("no_high_match_targets"
                                  if _hm_meta.get("enabled") and _hm_meta.get("input")
                                  else "no_targets_upstream_zero_members"),
                        "meta": _skip_meta,
                    })
                    logger.warning("[FB Campaign] add_friends skip — 0 targets "
                                    "(extracted=%s, manual=%s)",
                                    _skip_meta["extracted_members"],
                                    _skip_meta["has_manual_targets"])
                    continue
                note = (params.get("verification_note") or "").strip()
                # P1 Sprint C: require_verification_note=True 时必须非空 — 否则 FB 风控
                # 易把"无验证语 + 短时高频"判为机器人。preset 里默认 require=True。
                if not note and bool(params.get("require_verification_note", False)):
                    result["steps_failed"].append({
                        "step": step,
                        "error": "missing_verification_note",
                        "meta": {
                            "hint": "preset 声明 require_verification_note=True 但 verification_note 为空，"
                                    "已跳过以避免 FB 风控；请在启动方案时填写。",
                            "outcome": "missing_param:verification_note",
                        },
                    })
                    logger.warning("[FB Campaign] add_friends skip — empty verification_note "
                                   "(require=True)")
                    continue
                greeting = params.get("greeting") or params.get("greeting_message") or ""
                max_n = int(params.get("max_friends_per_run", 5))
                greet_inline = bool(params.get("send_greeting_inline", True))
                _pk = params.get("persona_key") or None
                _ph = params.get("phase") or None
                _pr = str(params.get("_preset_key", "") or params.get("preset_key", ""))

                # P2.1 (2026-04-30): 逐人 AI 话术 ── 默认开启
                # 每位目标用户调 personalized_message.generate_message 拿专属验证语 +
                # 打招呼, 强制 persona.language. 失败兜底到 params.verification_note。
                # 关闭方式: params.disable_ai_per_target_message=True (运营 A/B 测试用)。
                _ai_per_target = not bool(params.get("disable_ai_per_target_message"))
                _persona_ctx = None
                _output_lang = ""
                if _ai_per_target:
                    try:
                        from src.ai.personalized_message import (
                            PersonaContext as _PC,
                            generate_message as _gen_msg,
                            TargetUser as _TU,
                        )
                        _p_disp = {}
                        try:
                            from src.host.fb_target_personas import (
                                get_persona_display as _gpd,
                            )
                            _p_disp = _gpd(_pk) or {}
                        except Exception:
                            pass
                        _output_lang = (params.get("output_language") or "").strip()
                        if not _output_lang:
                            _lang_short = (_p_disp.get("language") or "ja").lower()
                            _country = (_p_disp.get("country_code") or "JP").upper()
                            _output_lang = (
                                "ja-JP" if _lang_short.startswith("ja")
                                else "zh-CN" if _lang_short.startswith("zh")
                                else "en-US" if _lang_short.startswith("en")
                                else f"{_lang_short}-{_country}"
                            )
                        _persona_ctx = _PC(
                            bio=_p_disp.get("short_label") or _p_disp.get("name") or "",
                            language=_output_lang,
                            interest_topics=list(_p_disp.get("interest_topics") or []),
                        )
                    except Exception as _ai_init_err:
                        logger.warning("[FB Campaign] AI 话术初始化失败, 退回 batch: %s",
                                        _ai_init_err)
                        _ai_per_target = False
                # 群上下文用作 target.group_context (per-target prompt 上下文)
                _group_ctx = ""
                _tg = params.get("target_groups")
                if isinstance(_tg, list) and _tg:
                    _group_ctx = str(_tg[0])
                elif isinstance(params.get("group_name"), str):
                    _group_ctx = params.get("group_name") or ""

                sent = 0
                greeted = 0
                greet_results: List[Dict[str, Any]] = []
                ai_stats = {"used": 0, "fallback": 0, "lang_verified": 0,
                            "audit_ok": 0}
                attempted = 0
                for t in targets:
                    if sent >= max_n:
                        break
                    attempted += 1
                    name = t.get("name") if isinstance(t, dict) else str(t)
                    if not name:
                        continue

                    # 2026-05-04: feed_authors source 已经在群帖子流抓到 author
                    # 头像 bounds. 直接 tap 进 profile 页, 避免 fb.search '斉藤正和'
                    # 在 FB 全局搜不到精确个人 (同名太多 + Page 噪音). 然后
                    # 透传 from_current_profile=True 让下游不再 search.
                    _profile_pic_bounds = (
                        t.get("profile_pic_bounds")
                        if isinstance(t, dict) else None
                    )
                    _from_current_profile = False
                    if _profile_pic_bounds and len(_profile_pic_bounds) == 4:
                        try:
                            # 2026-05-04: 不能用 _r 当 right 变量名 — 函数顶 import random as _r,
                            # 解包会把 random 模块覆盖成 int, 后面 _r.uniform() 炸 'int has no uniform'
                            _bx_l, _bx_t, _bx_r, _bx_b = _profile_pic_bounds
                            _cx = (int(_bx_l) + int(_bx_r)) // 2
                            _cy = (int(_bx_t) + int(_bx_b)) // 2
                            import time as _t_mod
                            fb._u2(resolved).click(_cx, _cy)
                            _t_mod.sleep(2.5)
                            _from_current_profile = True
                            logger.info(
                                "[FB Campaign] tap feed_authors profile pic "
                                "bounds=(%d,%d,%d,%d) center=(%d,%d) for %s",
                                int(_bx_l), int(_bx_t), int(_bx_r), int(_bx_b),
                                _cx, _cy, name,
                            )
                        except Exception as _tap_e:
                            logger.debug(
                                "[FB Campaign] tap profile pic 失败 %s: %s",
                                name, _tap_e,
                            )

                    # P2.1: 逐人生成 (AI on; 失败→兜底到 params 配的 batch 文本)
                    per_note = note
                    per_greet = greeting
                    if _ai_per_target and _persona_ctx is not None:
                        try:
                            _tgt_obj = _TU(
                                name=name,
                                bio=t.get("bio", "") if isinstance(t, dict) else "",
                                recent_posts=(t.get("recent_posts") or [])
                                    if isinstance(t, dict) else [],
                                group_context=_group_ctx,
                            )
                            _ai_note, _meta_n = _gen_msg(
                                _tgt_obj, _persona_ctx, "verification_note",
                                _output_lang,
                            )
                            if _ai_note:
                                per_note = _ai_note
                                ai_stats["used"] += 1
                                if _meta_n.get("fallback"):
                                    ai_stats["fallback"] += 1
                                if _meta_n.get("lang_verified"):
                                    ai_stats["lang_verified"] += 1
                                if _meta_n.get("audit_ok"):
                                    ai_stats["audit_ok"] += 1
                            if greet_inline:
                                _ai_greet, _meta_g = _gen_msg(
                                    _tgt_obj, _persona_ctx, "first_greeting",
                                    _output_lang,
                                )
                                if _ai_greet:
                                    per_greet = _ai_greet
                        except Exception as _ai_gen_err:
                            logger.debug("[FB Campaign] AI 话术生成失败 target=%s: %s",
                                          name, _ai_gen_err)

                    if greet_inline and hasattr(fb, "add_friend_and_greet"):
                        # campaign: 把 source 下推到 automation 层 record(锁内)
                        _camp_src = params.get("source", "") or ""
                        if not _camp_src:
                            _tg = params.get("target_groups")
                            if isinstance(_tg, list) and _tg:
                                _camp_src = str(_tg[0])
                            elif isinstance(params.get("group_name"), str):
                                _camp_src = params.get("group_name") or ""
                        _ai_g = params.get("ai_dynamic_greeting")
                        _fsg = params.get("force_send_greeting")
                        _p10_extra = _phase10_task_extras(params)
                        try:
                            from src.host.fb_playbook import local_rules_disabled
                            _relaxed_local_rules = local_rules_disabled()
                        except Exception:
                            _relaxed_local_rules = False
                        res = fb.add_friend_and_greet(
                            name,
                            note=per_note,
                            greeting=per_greet,
                            device_id=resolved,
                            persona_key=_pk,
                            phase=_ph,
                            preset_key=_pr,
                            source=_camp_src,
                            from_current_profile=_from_current_profile,
                            force=_relaxed_local_rules or bool(params.get("force_add_friend")),
                            ai_dynamic_greeting=(bool(_ai_g) if _ai_g is not None else None),
                            force_send_greeting=(
                                True if _relaxed_local_rules
                                else (bool(_fsg) if _fsg is not None else None)
                            ),
                            **_p10_extra,
                        ) or {}
                        ok = bool(res.get("add_friend_ok"))
                        # 方案 A (2026-05-04): dm_only_sent 同样计入 sent (业务
                        # 目标 "触达此人" 已达成, outcome 用 fallback marker 区分)
                        _dm_only = bool(res.get("dm_only_sent"))
                        if ok or _dm_only:
                            sent += 1
                        if res.get("greet_ok"):
                            greeted += 1
                        if params.get("require_high_match") or _pr == "name_hunter":
                            try:
                                from src.host.fb_targets_store import mark_name_hunter_touched
                                mark_name_hunter_touched(
                                    name=name,
                                    persona_key=_pk or "jp_female_midlife",
                                    status=("greeted" if res.get("greet_ok") else "friend_requested")
                                    if ok else "rejected",
                                    device_id=resolved,
                                )
                            except Exception as _cand_e:
                                logger.debug("[FB Campaign] candidate touch persist skipped: %s", _cand_e)
                        greet_results.append({
                            "name": name,
                            "add_friend_ok": ok,
                            "greet_ok": bool(res.get("greet_ok")),
                            "greet_skipped_reason": res.get("greet_skipped_reason", ""),
                        })
                    elif hasattr(fb, "add_friend_with_note"):
                        # campaign 场景: source 从 target_groups/group_name 推断
                        _camp_src = params.get("source", "") or ""
                        if not _camp_src:
                            _tg = params.get("target_groups")
                            if isinstance(_tg, list) and _tg:
                                _camp_src = str(_tg[0])
                            elif isinstance(params.get("group_name"), str):
                                _camp_src = params.get("group_name") or ""
                        _p10_extra2 = _phase10_task_extras(params)
                        try:
                            from src.host.fb_playbook import local_rules_disabled
                            _force_add = local_rules_disabled() or bool(params.get("force_add_friend"))
                        except Exception:
                            _force_add = bool(params.get("force_add_friend"))
                        ok = fb.add_friend_with_note(name, note=per_note,
                                                     safe_mode=True,
                                                     device_id=resolved,
                                                     persona_key=_pk,
                                                     phase=_ph,
                                                     source=_camp_src,
                                                     preset_key=_pr,
                                                     force=_force_add,
                                                     **_p10_extra2)
                        if ok:
                            sent += 1
                        if params.get("require_high_match") or _pr == "name_hunter":
                            try:
                                from src.host.fb_targets_store import mark_name_hunter_touched
                                mark_name_hunter_touched(
                                    name=name,
                                    persona_key=_pk or "jp_female_midlife",
                                    status="friend_requested" if ok else "rejected",
                                    device_id=resolved,
                                )
                            except Exception as _cand_e:
                                logger.debug("[FB Campaign] candidate touch persist skipped: %s", _cand_e)
                    else:
                        ok = fb.add_friend(name, device_id=resolved)
                        if ok:
                            sent += 1
                    # 2026-04-23 P3-1: automation 层(add_friend_with_note / add_friend_and_greet)
                    # 在锁内已 record sent 状态; 这里补细分失败状态。
                    # 2026-05-11: 细分 risk → failed_no_button / failed_persona / risk
                    if not ok:
                        try:
                            from src.host.fb_store import record_friend_request
                            _src = params.get("source", "") or ""
                            if not _src:
                                _tg = params.get("target_groups")
                                if isinstance(_tg, list) and _tg:
                                    _src = str(_tg[0])
                                elif isinstance(params.get("group_name"), str):
                                    _src = params.get("group_name") or ""
                            _fail_status = "risk"
                            try:
                                _last_reason = getattr(fb, "_last_greet_skip_reason", "") or ""
                                if "persona" in _last_reason or "l1" in _last_reason:
                                    _fail_status = "failed_persona"
                                elif "no_add_button" in _last_reason or "无加好友按钮" in _last_reason:
                                    _fail_status = "failed_no_button"
                            except Exception:
                                pass
                            record_friend_request(
                                resolved, name,
                                note=per_note,
                                source=_src,
                                status=_fail_status,
                                preset_key=_pr,
                            )
                        except Exception:
                            pass
                    try:
                        from src.host.fb_playbook import local_rules_disabled
                        _relaxed_gap = local_rules_disabled()
                    except Exception:
                        _relaxed_gap = False
                    if not _relaxed_gap:
                        time.sleep(_r.uniform(60, 180))
                result["friend_requests_sent"] += sent
                if greet_inline:
                    result["greetings_sent"] = result.get("greetings_sent", 0) + greeted
                    result["greet_details"] = greet_results
                    result["add_friend_attempted"] = attempted
                # P2.1: AI 话术统计 ── 让前端可视化「AI 使用多少/语言验证多少」
                if ai_stats["used"] > 0:
                    result["ai_message_stats"] = ai_stats

            elif step == "send_greeting":
                # 独立 send_greeting step —— 不配合 add_friends,按 params.targets
                # 逐个 search_people + 发打招呼(老朋友复访 / 手动触发场景)。
                # 日上限 / phase 由 send_greeting_after_add_friend 内部判定,
                # 这里不再叠 gate,避免与 add_friend 上限双扣。
                targets = params.get("greeting_targets") \
                          or params.get("add_friend_targets") \
                          or result.get("last_extracted_members") \
                          or []
                targets, _hm_meta = _fb_filter_high_match_targets(targets, params)
                if _hm_meta.get("enabled"):
                    result["high_match_filter"] = _hm_meta
                if not targets:
                    result["steps_failed"].append({
                        "step": step,
                        "error": "no_high_match_targets",
                        "meta": {"high_match_filter": _hm_meta},
                    })
                    continue
                greeting = params.get("greeting") or params.get("greeting_message") or ""
                _pk = params.get("persona_key") or None
                _ph = params.get("phase") or None
                _pr = str(params.get("_preset_key", "") or params.get("preset_key", ""))
                # 2026-04-23 优化: 间隔从 playbook.send_greeting.inter_greeting_sec 读
                # (回退 fallback 120~300s 保持旧行为)
                try:
                    from src.host.fb_playbook import resolve_send_greeting_params
                    _sg_cfg = resolve_send_greeting_params(phase=_ph) or {}
                    _gap = _sg_cfg.get("inter_greeting_sec") or (120, 300)
                    _max_n_cfg = int(_sg_cfg.get("max_greetings_per_run", 3))
                except Exception:
                    _gap = (120, 300)
                    _max_n_cfg = 3
                # params.max_greetings_per_run 优先（显式覆盖）否则走 playbook
                max_n = int(params.get("max_greetings_per_run") or _max_n_cfg or 3)
                greeted = 0
                for t in targets[:max_n]:
                    name = t.get("name") if isinstance(t, dict) else str(t)
                    if not name:
                        continue
                    if not hasattr(fb, "send_greeting_after_add_friend"):
                        result["steps_failed"].append({"step": step,
                                                       "error": "未实现 send_greeting_after_add_friend"})
                        break
                    try:
                        from src.host.fb_playbook import local_rules_disabled
                        _force_greeting = local_rules_disabled() or bool(params.get("force_send_greeting"))
                    except Exception:
                        _force_greeting = bool(params.get("force_send_greeting"))
                    ok = fb.send_greeting_after_add_friend(
                        name,
                        greeting=greeting,
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                        assume_on_profile=False,
                        preset_key=_pr,
                        force=_force_greeting,
                    )
                    if ok:
                        greeted += 1
                    try:
                        from src.host.fb_playbook import local_rules_disabled
                        _relaxed_gap = local_rules_disabled()
                    except Exception:
                        _relaxed_gap = False
                    if not _relaxed_gap:
                        time.sleep(_r.uniform(float(_gap[0]), float(_gap[1])))
                result["greetings_sent"] = result.get("greetings_sent", 0) + greeted

            elif step == "check_inbox":
                _pk = params.get("persona_key") or None
                _ph = params.get("phase") or None
                if hasattr(fb, "check_messenger_inbox"):
                    stats = fb.check_messenger_inbox(
                        auto_reply=bool(params.get("auto_reply", False)),
                        max_conversations=int(params.get("max_conversations", 20)),
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                    ) or {}
                    result["messages_replied"] += int(stats.get("replied", 0))
                if hasattr(fb, "check_message_requests"):
                    fb.check_message_requests(
                        max_requests=int(params.get("max_requests", 10)),
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                    )
                if hasattr(fb, "check_friend_requests_inbox"):
                    fb.check_friend_requests_inbox(
                        accept_all=bool(params.get("accept_friend_requests", False)),
                        max_requests=int(params.get("max_friend_requests", 20)),
                        device_id=resolved,
                        persona_key=_pk,
                        phase=_ph,
                    )
            else:
                result["steps_failed"].append({"step": step, "error": "未知步骤"})
                continue

            result["steps_completed"].append(step)
            # P1-3a: 每步落盘，任务中途挂掉也能续跑
            if run_id and _cr_update:
                try:
                    _cr_update(run_id, idx, step, result)
                except Exception:
                    pass
            time.sleep(_r.uniform(20, 45))

        except Exception as step_err:
            _err_str = str(step_err)
            # 2026-05-10: warmup 配额冲突是 soft failure — 编排链已在上游
            # 独立跑过 browse_feed, campaign_run 的 warmup 只是补充;
            # 配额满时跳过即可, 不应导致整个 campaign 标记失败.
            if step == "warmup" and "quota" in _err_str.lower():
                logger.info("[FB Campaign] warmup quota skip (非关键): %s", _err_str[:80])
                result.setdefault("warmup_skipped_reason", _err_str[:120])
            else:
                logger.error("[FB Campaign] 步骤 %s 失败: %s", step, step_err)
                result["steps_failed"].append({"step": step, "error": _err_str})
            if run_id and _cr_update:
                try:
                    _cr_update(run_id, idx, step, result)
                except Exception:
                    pass
            time.sleep(8)

    try:
        from src.host.event_stream import push_event as _push
        _push("facebook.campaign_done", {
            "device_id": resolved,
            "steps_completed": result["steps_completed"],
            "steps_failed": [s["step"] for s in result["steps_failed"]],
            "extracted_members": result["extracted_members"],
            "friend_requests_sent": result["friend_requests_sent"],
            "greetings_sent": result.get("greetings_sent", 0),
            "messages_replied": result["messages_replied"],
            "outcome": result.get("outcome", ""),
            "outreach_goal": result.get("outreach_goal"),
            "outreach_goal_progress": result.get("outreach_goal_progress"),
        }, resolved)
    except Exception:
        pass

    # P1-3a: 收尾 — 全成功 / 部分 / 全失败
    if run_id and _cr_finish:
        try:
            if result["steps_failed"] and not result["steps_completed"]:
                _cr_finish(run_id, "failed", result)
            elif result["steps_failed"]:
                _cr_finish(run_id, "partial", result)
            else:
                _cr_finish(run_id, "completed", result)
        except Exception:
            pass

    # P1 Sprint C: 把首个失败步骤的结构化 outcome 冒泡到 result.outcome，
    # 让前端任务卡片徽章 (_renderOutcomeBadge) 能直接识别"配置缺失"等类型并提供
    # 「点击重新配置」入口。无 outcome 字段时不覆盖（保持 ok 语义）。
    if result.get("steps_failed") and not result.get("outcome"):
        for _f in result["steps_failed"]:
            _oc = (_f.get("meta") or {}).get("outcome")
            if _oc:
                result["outcome"] = _oc
                break

    if result.get("steps_failed"):
        result.setdefault("outcome", "partial_failed")
        return False, _campaign_failure_message(result), result

    if bool(params.get("require_outreach_goal")):
        goal = int(
            params.get("outreach_goal")
            or params.get("max_friends_per_run")
            or 1
        )
        sent = int(result.get("friend_requests_sent") or 0)
        greeted = int(result.get("greetings_sent") or 0)
        if sent < goal:
            extracted = int(result.get("extracted_members") or 0)
            attempted = int(result.get("add_friend_attempted") or 0)
            # 区分卡点：候选源耗尽 / 候选都被高匹配过滤掉 / quota 节流 / 中途异常。
            # 让前端徽章可以显示具体原因，避免运营看到"未达成"还要再翻 logs。
            if extracted == 0:
                exhaust_reason = "no_candidates_extracted"
            elif attempted == 0:
                exhaust_reason = "all_candidates_filtered"
            elif sent == 0:
                exhaust_reason = "all_attempts_rejected"
            else:
                exhaust_reason = "quota_or_pool_exhausted"
            result["outcome"] = "outreach_goal_not_met"
            result["outreach_goal"] = goal
            result["outreach_goal_progress"] = {
                "friend_requests_sent": sent,
                "greetings_sent": greeted,
                "extracted_members": extracted,
                "add_friend_attempted": attempted,
                "exhaust_reason": exhaust_reason,
            }
            return False, (
                f"好友打招呼未达成本次目标：已发好友请求 {sent}/{goal}，"
                f"已打招呼 {greeted}（原因：{exhaust_reason}）。"
            ), result

    result.setdefault("outcome", "ok")
    return True, "", result


def _fresh_instagram(manager, resolved):
    from src.app_automation.instagram import InstagramAutomation
    ig = InstagramAutomation(device_manager=manager)
    ig.set_current_device(resolved)
    return ig


def _fresh_twitter(manager, resolved):
    from src.app_automation.twitter import TwitterAutomation
    tw = TwitterAutomation(device_manager=manager)
    tw.set_current_device(resolved)
    return tw


def _fresh_whatsapp(manager, resolved):
    from src.app_automation.whatsapp import WhatsAppAutomation
    wa = WhatsAppAutomation(manager)
    wa.set_current_device(resolved)
    d = manager.get_u2(resolved)
    if d:
        wa.start_whatsapp(resolved)
    return wa


def _get_leads_tracker():
    """Get the LeadsFollowTracker singleton."""
    from src.leads.follow_tracker import LeadsFollowTracker
    return LeadsFollowTracker()


def _make_progress_cb():
    """Build a progress callback using thread-local task context."""
    from src.utils.log_config import _task_context
    tid = getattr(_task_context, "task_id", "")
    if not tid:
        return None
    _last = [0]

    def _cb(pct: int, msg: str):
        if pct - _last[0] < 5 and pct < 99:
            return
        _last[0] = pct
        try:
            update_task_progress(tid, pct, msg)
        except Exception:
            pass
    return _cb


def _check_tiktok_version(manager, resolved) -> None:
    """
    检测 TikTok 应用版本，若版本变更则自动清除过期选择器缓存。
    使用 DeviceStateStore 存储上次已知版本。结果轻量缓存到进程内 dict 避免过频。
    """
    _KEY = "tiktok_app_version"
    try:
        # 进程内缓存：同一设备 30 分钟内不重复检测
        _cache = getattr(_check_tiktok_version, "_cache", {})
        _check_tiktok_version._cache = _cache
        import time as _time
        now = _time.time()
        if now - _cache.get(resolved, 0) < 1800:
            return

        result = manager.execute_command(
            resolved,
            "shell pm dump com.zhiliaoapp.musically | grep versionName | head -1"
        )
        if not result:
            result = manager.execute_command(
                resolved,
                "shell pm dump com.ss.android.ugc.trill | grep versionName | head -1"
            )
        if not result:
            return

        # 解析版本号：versionName=43.8.3
        import re as _re
        m = _re.search(r"versionName=([^\s]+)", result)
        if not m:
            return
        current_ver = m.group(1).strip()

        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        stored_ver = ds.get(resolved, _KEY)

        if stored_ver and stored_ver != current_ver:
            logger.info("[版本检测] 设备 %s TikTok 更新: %s → %s，清除过期选择器",
                        resolved[:8], stored_ver, current_ver)
            try:
                from src.vision.auto_selector import get_auto_selector
                sel = get_auto_selector()
                for pkg in ("com.zhiliaoapp.musically", "com.ss.android.ugc.trill"):
                    sel.sweep_stale_selectors(pkg, stale_days=0)  # 版本变更时强制清全部
            except Exception as e:
                logger.debug("[版本检测] 清除选择器失败: %s", e)

        if stored_ver != current_ver:
            ds.set(resolved, _KEY, current_ver)

        _cache[resolved] = now

    except Exception as e:
        logger.debug("[版本检测] 跳过: %s", e)


def _execute_tiktok(manager, resolved, task_type, params):
    """TikTok task dispatcher — handles all tiktok_* task types."""
    tt = _fresh_tiktok(manager, resolved)

    # 版本检测：TikTok 更新后自动清除过期选择器（静默，不阻断任务）
    _check_tiktok_version(manager, resolved)

    target_account = params.pop("account", None)

    if not target_account:
        try:
            from .account_scheduler import get_account_scheduler
            sched = get_account_scheduler()
            sched.auto_discover_accounts(resolved, manager)
            auto = sched.select_account(resolved, task_type)
            if auto:
                target_account = auto
                logger.info("[多账号] 自动选择账号 @%s (设备 %s)",
                            target_account, resolved[:8])
        except Exception as e:
            logger.debug("[多账号] 自动调度跳过: %s", e)

    if target_account:
        current = tt.get_current_account(resolved)
        if current != target_account:
            if not tt.switch_account(target_account, resolved):
                return False, f"无法切换到账号 @{target_account}", None
        try:
            from .account_scheduler import get_account_scheduler
            get_account_scheduler().start_session(resolved, target_account)
        except Exception:
            pass
        params["_active_account"] = target_account

    from src.host.device_state import DeviceStateStore
    state_device_id = DeviceStateStore.account_device_id(
        resolved, target_account) if target_account else resolved

    if task_type == "tiktok_warmup":
        # 前端部分入口传 duration，与 duration_minutes 统一
        if params.get("duration_minutes") is None and params.get("duration") is not None:
            try:
                params["duration_minutes"] = int(params["duration"])
            except (TypeError, ValueError):
                pass

        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device(state_device_id)

        # VPN 检查 + 静默重连 — 失败则中止任务
        vpn_ok, vpn_msg = _ensure_vpn(resolved)
        if not vpn_ok:
            return False, vpn_msg, {"vpn_status": "failed"}

        # Check if device is in recovery mode — override params
        try:
            from src.behavior.adaptive_compliance import get_adaptive_compliance
            ac = get_adaptive_compliance()
            if ac.is_recovering(state_device_id):
                recovery_params = ac.get_recovery_warmup_params(state_device_id)
                logger.info("[恢复模式] 设备 %s 使用恢复养号参数: %s",
                         state_device_id[:12], recovery_params)
                target_country = params.get("target_country", "italy")
                _rec_tid = _get_current_task_id()

                def _rec_ckpt(st, elapsed):
                    if _rec_tid:
                        _save_task_checkpoint(_rec_tid, {
                            "task_type": "tiktok_warmup",
                            "stats": st, "elapsed_sec": elapsed,
                            "phase": recovery_params["phase"],
                            "recovery_mode": True,
                        })

                stats = tt.warmup_session(
                    duration_minutes=recovery_params["duration_minutes"],
                    target_country=target_country,
                    phase=recovery_params["phase"],
                    checkpoint_callback=_rec_ckpt,
                )
                ds.record_warmup(state_device_id, stats)
                ac.record_recovery_session(state_device_id)
                return True, "", {
                    "warmup_stats": stats,
                    "phase": "recovery",
                    "recovery_mode": True,
                    "recovery_session": ac.get_risk_profile(state_device_id).get(
                        "recovery", {}),
                }
        except Exception:
            pass

        phase = params.get("phase", "auto")
        if phase == "auto":
            phase = ds.determine_phase(state_device_id)

        ab_params = None
        try:
            from src.behavior.ab_experiment import get_experiment_manager
            ab_params = get_experiment_manager().get_device_params(resolved)
        except Exception:
            pass

        if ab_params:
            for k, v in ab_params.items():
                if not k.startswith("_") and k not in params:
                    params[k] = v
            logger.info("[A/B] 实验 '%s' 变体 '%s' 应用于 %s",
                        ab_params.get("_experiment"),
                        ab_params.get("_variant"), resolved[:8])

        adaptive = None
        try:
            from src.behavior.account_profile import get_profile_manager
            adaptive = get_profile_manager().get_adaptive_params(
                resolved, target_account or "")
        except Exception:
            pass

        if adaptive and not params.get("_no_adaptive") and not ab_params:
            duration = params.get("duration_minutes") or adaptive.get("duration_minutes", 30)
            target_country = params.get("target_country", "italy")
            for k in ("like_probability", "comment_browse_prob",
                       "comment_post_prob", "search_prob"):
                if k not in params and k in adaptive:
                    params[k] = adaptive[k]
            logger.info("[画像] 使用自适应参数: algo=%.2f trend=%s dur=%d",
                        adaptive.get("_algo_score", 0),
                        adaptive.get("_trend", "?"), duration)
        else:
            duration = params.get("duration_minutes", 30)
            target_country = params.get("target_country", "italy")

        _tc = params.get("target_countries") or []
        if isinstance(_tc, str):
            _tc = [c.strip() for c in _tc.split(',') if c.strip()]
        _tl = params.get("target_languages") or []
        if isinstance(_tl, str):
            _tl = [l.strip() for l in _tl.split(',') if l.strip()]
        _geo_filter = bool(_tc) or bool(params.get("geo_filter", False))

        resume_cp = None
        cp_raw = params.get("_checkpoint")
        if cp_raw and cp_raw.get("task_type") == "tiktok_warmup":
            resume_cp = cp_raw

        task_id = _get_current_task_id()

        def _warmup_ckpt(st, elapsed):
            if task_id:
                _save_task_checkpoint(task_id, {
                    "task_type": "tiktok_warmup",
                    "stats": st,
                    "elapsed_sec": elapsed,
                    "phase": phase,
                    "duration_minutes": duration,
                    "target_country": target_country,
                    "geo_stats": st.get("geo_stats", {}),
                })

        stats = tt.warmup_session(
            duration_minutes=duration,
            target_country=target_country,
            phase=phase,
            target_countries=_tc,
            target_languages=_tl,
            geo_filter=_geo_filter,
            progress_callback=_make_progress_cb(),
            checkpoint_callback=_warmup_ckpt,
            resume_checkpoint=resume_cp,
        )
        ds.record_warmup(state_device_id, stats)

        try:
            from src.behavior.account_profile import get_profile_manager
            pm = get_profile_manager()
            pm.record_session(resolved, target_account or "", stats, duration)
            algo_score = ds.get_algorithm_learning_score(state_device_id)
            pm.record_algo_score(resolved, target_account or "", algo_score)
        except Exception:
            pass

        try:
            from src.behavior.ab_experiment import get_experiment_manager
            em = get_experiment_manager()
            a_score = ds.get_algorithm_learning_score(state_device_id)
            em.record_session(resolved, stats, algo_score=a_score)
        except Exception:
            pass

        return True, "", {"warmup_stats": stats, "phase": phase,
                          "resumed": bool(resume_cp)}

    elif task_type == "tiktok_browse_feed":
        video_count = params.get("video_count", 10)
        like_prob = params.get("like_probability", 0.25)
        target_country = params.get("target_country", "italy")
        minutes = int(video_count * 8 / 60) + 1

        resume_cp = None
        cp_raw = params.get("_checkpoint")
        if cp_raw and cp_raw.get("task_type") == "tiktok_browse_feed":
            resume_cp = cp_raw

        task_id = _get_current_task_id()

        def _feed_ckpt(st, elapsed):
            if task_id:
                _save_task_checkpoint(task_id, {
                    "task_type": "tiktok_browse_feed",
                    "stats": st,
                    "elapsed_sec": elapsed,
                    "video_count": video_count,
                })

        stats = tt.warmup_session(
            duration_minutes=minutes,
            like_probability=like_prob,
            target_country=target_country,
            phase="interest_building",
            progress_callback=_make_progress_cb(),
            checkpoint_callback=_feed_ckpt,
            resume_checkpoint=resume_cp,
        )
        return True, "", {"feed_stats": {
            "watched": stats["watched"], "likes": stats["liked"],
            "comments": 0, "follows": 0, "rewatches": 0,
        }, "resumed": bool(resume_cp)}

    elif task_type == "tiktok_test_follow":
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device(state_device_id)
        d = tt._u2(resolved)  # 使用 ADB fallback
        if not tt.launch(resolved):
            return False, "TikTok 启动失败", None
        can_follow = tt._random_test_follow(d, resolved)
        ds.mark_can_follow(state_device_id, can_follow)
        ds.record_follow_test(state_device_id)
        return True, "", {"can_follow": can_follow, "phase": ds.get_phase(state_device_id)}

    elif task_type == "tiktok_follow":
        # VPN 检查 — VPN 断连时仅警告，不中止（允许在本地网络继续运行）
        vpn_ok, vpn_msg = _ensure_vpn(resolved)
        if not vpn_ok:
            logger.warning("[关注] %s: VPN未连接，继续执行（无意大利出口）", resolved[:12])
            vpn_msg = "not_connected"

        # P4-C: 验证 IP 是否在意大利（仅 VPN 已连接时才做硬检查）
        if vpn_msg in ("connected", "reconnected"):
            geo_ok, geo_msg = _ensure_italy_ip(resolved)
            if not geo_ok:
                return False, geo_msg, {"geo_status": "wrong_country", "vpn_status": vpn_msg}

        from src.app_automation.target_filter import TargetProfile
        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        ds.init_device(state_device_id)
        # P4-A: 每日关注安全上限检查
        _daily_followed = ds.get_int(state_device_id, f"daily:{ds._today()}:followed")
        _follow_daily_limit = params.get("daily_limit", 50)
        if _daily_followed >= _follow_daily_limit:
            logger.warning("[SAFETY] %s: 今日关注 %d 已达上限 %d，中止任务",
                           state_device_id[:8], _daily_followed, _follow_daily_limit)
            return False, (f"今日关注已达安全上限 {_follow_daily_limit}"
                           f"（已关注 {_daily_followed}），任务中止保护账号"), {
                "daily_followed": _daily_followed, "limit": _follow_daily_limit}
        _tc_countries = params.get("target_countries") or []
        if isinstance(_tc_countries, str):
            _tc_countries = [c.strip() for c in _tc_countries.split(',') if c.strip()]
        _tc_languages = params.get("target_languages") or []
        if isinstance(_tc_languages, str):
            _tc_languages = [l.strip() for l in _tc_languages.split(',') if l.strip()]
        target = TargetProfile(
            country=params.get("country") or params.get("target_country", "italy"),
            language=params.get("language", "italian"),
            gender=params.get("gender", ""),
            min_age=params.get("min_age", 0),
            countries=_tc_countries,
            languages=_tc_languages,
        )
        max_follows = params.get("max_follows") or ds.get_follow_ramp_max(state_device_id)
        seed_accounts = params.get("seed_accounts") or None
        tracker = _get_leads_tracker()
        resume_cp = None
        cp_raw = params.get("_checkpoint")
        if cp_raw and cp_raw.get("task_type") == "tiktok_follow":
            resume_cp = cp_raw

        task_id = _get_current_task_id()

        def _follow_ckpt(res, seeds_tried):
            if task_id:
                _save_task_checkpoint(task_id, {
                    "task_type": "tiktok_follow",
                    "result": res,
                    "seeds_tried": seeds_tried,
                })

        result = tt.smart_follow(
            target=target,
            max_follows=max_follows,
            seed_accounts=seed_accounts,
            global_tracker=tracker,
            checkpoint_callback=_follow_ckpt,
            resume_checkpoint=resume_cp,
            progress_callback=_make_progress_cb(),
            comment_warmup=params.get("comment_warmup", False),
        )
        ds.record_follows(state_device_id, result.get("followed", 0))
        # J2: 批量注册 TikTok 关注目标到 lead_identities
        _register_tiktok_identities(result.get("users", []),
                                    state_device_id, "smart_follow")
        return True, "", {"follow_result": result, "resumed": bool(resume_cp)}

    elif task_type in ("tiktok_chat", "tiktok_check_and_chat_followbacks"):
        # P4-C: VPN + 意大利 IP 检查
        vpn_ok, vpn_msg = _ensure_vpn(resolved)
        if not vpn_ok:
            return False, vpn_msg, {"vpn_status": "failed"}
        if vpn_msg in ("connected", "reconnected"):
            geo_ok, geo_msg = _ensure_italy_ip(resolved)
            if not geo_ok:
                return False, geo_msg, {"geo_status": "wrong_country", "vpn_status": vpn_msg}

        from src.host.device_state import get_device_state_store
        ds = get_device_state_store("tiktok")
        # P4-A: 每日 DM 安全上限检查
        _daily_dms = ds.get_int(state_device_id, f"daily:{ds._today()}:dms")
        _dm_daily_limit = params.get("daily_dm_limit", 80)
        if _daily_dms >= _dm_daily_limit:
            logger.warning("[SAFETY] %s: 今日私信 %d 已达上限 %d，中止任务",
                           state_device_id[:8], _daily_dms, _dm_daily_limit)
            return False, (f"今日私信已达安全上限 {_dm_daily_limit}"
                           f"（已发送 {_daily_dms}），任务中止保护账号"), {
                "daily_dms": _daily_dms, "limit": _dm_daily_limit}
        messages = params.get("messages", [])
        if not messages:
            try:
                chat_path = config_file("chat_messages.yaml")
                if chat_path.exists():
                    with open(chat_path, "r", encoding="utf-8") as f:
                        data = yaml.safe_load(f)
                    # A/B variant selection
                    variants = data.get("message_variants", [])
                    if variants:
                        try:
                            from src.host.ab_stats import select_variant, record_sent
                            selected = select_variant(variants)
                            messages = selected.get("greeting_messages", selected.get("messages", []))
                            _ab_variant_id = selected.get("id", "default")
                            record_sent(_ab_variant_id, state_device_id)
                            logger.info("[A/B] Selected variant: %s for device %s", _ab_variant_id, state_device_id[:8])
                        except Exception:
                            messages = data.get("messages", [])
                            _ab_variant_id = "default"
                    else:
                        messages = data.get("messages", [])
                        _ab_variant_id = "default"
            except Exception:
                pass
        if not messages:
            return False, "未配置聊天话术 (messages)", None

        max_chats = params.get("max_chats", 10)
        _tl_chat = params.get("target_languages", [])
        tracker = _get_leads_tracker()
        # P8-C: 把当前 A/B 变体 ID 挂到 tracker 上，tiktok.py 的 record_dm 读取
        tracker._current_ab_variant = _ab_variant_id
        result = tt.check_and_chat_followbacks(
            messages, max_chats, target_languages=_tl_chat or None,
            global_tracker=tracker,
            progress_callback=_make_progress_cb(),
        )
        ds.record_chats(state_device_id, result.get("messaged", 0))
        # J2: 注册 TikTok 聊天目标到 lead_identities
        _register_tiktok_identities(result.get("users", []),
                                    state_device_id, "chat_followback")
        return True, "", {"chat_result": result}

    elif task_type == "tiktok_send_dm":
        recipient = params.get("recipient") or params.get("username") or ""
        message = params.get("message", "")
        if not recipient:
            return False, "params.recipient 必填", None
        if not message:
            return False, "params.message 必填", None

        # Timezone-aware scheduling: defer if outside optimal window
        defer_if_outside = params.get("smart_timing", False)
        if defer_if_outside:
            from src.workflow.smart_schedule import best_send_time
            best_time = best_send_time(
                params.get("lead_timezone", "IT"),
                platform="tiktok",
            )
            if best_time:
                from datetime import datetime, timezone as tz
                now = datetime.now(tz.utc)
                delta = (best_time - now).total_seconds()
                if delta > 300:
                    from .task_store import create_task as _ct
                    deferred_params = dict(params)
                    deferred_params.pop("smart_timing", None)
                    _ct(task_type="tiktok_send_dm", device_id=resolved,
                        params=deferred_params)
                    return True, "", {
                        "deferred": True,
                        "best_send_time": best_time.isoformat(),
                        "reason": f"Deferred by {delta/60:.0f}min for optimal timing",
                    }

        # P5: 跨设备 DM 去重检查（可通过 params.force=true 绕过）
        if not params.get("force"):
            _tracker = _get_leads_tracker()
            if _tracker.was_dm_sent(recipient):
                logger.info("[DM去重] %s 已收过 DM，send_dm 跳过（传 force=true 可强制）", recipient)
                return True, "", {"skipped_dedup": True, "recipient": recipient}
        ok = tt.send_dm(recipient, message)
        return ok, ("" if ok else f"发送DM给 {recipient} 失败"), None

    elif task_type == "tiktok_check_inbox":
        # 收件箱检查不强制要求VPN（读写DM不依赖IP地理位置）
        _vpn_s = None
        try:
            from src.behavior.vpn_manager import check_vpn_status as _cvs
            _vpn_s = _cvs(resolved)
        except Exception:
            pass
        if _vpn_s and not _vpn_s.connected:
            logger.warning("[收件箱] %s: VPN未连接，仍继续执行（收件箱不强制VPN）", resolved[:12])

        # 从 config/ai.yaml 读取全局默认值（如果 yaml 中 auto_reply.enabled=true 则默认开启）
        _ai_default_reply = False
        try:
            _ai_cfg_path = config_file("ai.yaml")
            with open(_ai_cfg_path, encoding="utf-8") as _f:
                import yaml as _yaml
                _ai_yaml = _yaml.safe_load(_f) or {}
            _ai_default_reply = bool((_ai_yaml.get("auto_reply") or {}).get("enabled", False))
        except Exception:
            pass
        auto_reply = params.get("auto_reply", _ai_default_reply)
        max_conversations = params.get("max_conversations", 20)
        _tl_inbox = params.get("target_languages", [])
        if isinstance(_tl_inbox, str):
            _tl_inbox = [x.strip() for x in _tl_inbox.split(",") if x.strip()]
        result = tt.check_inbox(
            auto_reply=auto_reply,
            max_conversations=max_conversations,
            target_languages=_tl_inbox or None,
            progress_callback=_make_progress_cb(),
        )
        # Push real-time event so frontend refreshes conversation list
        try:
            from src.host.event_stream import push_event as _push_ev
            new_msgs = result.get("new_messages", 0) if isinstance(result, dict) else 0
            replied = result.get("auto_replied", 0) if isinstance(result, dict) else 0
            _push_ev("tiktok.inbox_checked", {
                "device_id": resolved,
                "new_messages": new_msgs,
                "auto_replied": replied,
            }, resolved)
            # Adaptive scheduling: if new messages found, schedule a quick follow-up check in 3 minutes
            _allow_followup = True
            try:
                from src.host.task_policy import policy_blocks_executor_inbox_followup
                _allow_followup = not policy_blocks_executor_inbox_followup()
            except Exception:
                pass
            if (
                _allow_followup
                and new_msgs > 0
                and params.get("auto_reply")
                and not params.get("_is_followup")
            ):
                try:
                    import threading as _threading
                    import time as _time
                    from src.host.api import task_store as _ts2, get_worker_pool as _gwp
                    def _quick_followup():
                        _time.sleep(180)  # 3 minutes
                        try:
                            from src.host.task_origin import with_origin as _wo

                            _tid = _ts2.create_task(
                                task_type="tiktok_check_inbox",
                                device_id=resolved,
                                params=_wo(
                                    {
                                        "auto_reply": True,
                                        "max_conversations": 50,
                                        "_is_followup": True,
                                    },
                                    "executor_followup_inbox",
                                ),
                            )
                            _gwp().submit(_tid, resolved, run_task, _tid, config_path or "")
                        except Exception:
                            pass
                    _threading.Thread(target=_quick_followup, daemon=True).start()
                except Exception:
                    pass
        except Exception:
            pass
        # 将 auto_reply DM 数量同步到 device_state（修复 dms_today 低估问题）
        if isinstance(result, dict):
            _auto_replied_n = result.get("auto_replied", 0)
            if _auto_replied_n > 0:
                try:
                    from src.host.device_state import get_device_state_store as _get_ds_inbox
                    _get_ds_inbox("tiktok").record_chats(state_device_id, _auto_replied_n)
                except Exception:
                    pass
        # N1 自动升级：NEEDS_REPLY 意图 → qualified + 话术补发（从 conversations 结果提取）
        try:
            _convs = result.get("conversations", []) if isinstance(result, dict) else []
            _needs_reply_contacts = [
                c.get("contact", "") for c in _convs
                if (c.get("intent") or "").upper() == "NEEDS_REPLY"
                and c.get("contact")
            ]
            if _needs_reply_contacts:
                from ..leads.store import get_leads_store as _gls_inbox
                _lstore = _gls_inbox()
                _qualified_n = 0
                _pitch_scheduled = 0
                # K2: 推进生命周期 → engaged (对方回复了)
                try:
                    from src.host.lead_mesh.canonical import (
                        is_contacted_globally, advance_lifecycle)
                    for _nr_c in _needs_reply_contacts:
                        _nr_dup = is_contacted_globally("tiktok", _nr_c)
                        if _nr_dup.get("canonical_id"):
                            advance_lifecycle(_nr_dup["canonical_id"], "engaged")
                except Exception:
                    pass
                for _contact in _needs_reply_contacts:
                    _lid_inbox = _lstore.find_by_platform_username("tiktok", _contact)
                    if _lid_inbox:
                        _lead_cur = _lstore.get_lead(_lid_inbox)
                        if _lead_cur and _lead_cur.get("status") in ("responded", "new", "contacted"):
                            _lstore.update_lead(_lid_inbox, status="qualified")
                            _qualified_n += 1
                            # K2: 推进生命周期 → qualified
                            try:
                                from src.host.lead_mesh.canonical import (
                                    is_contacted_globally as _icg_q,
                                    advance_lifecycle as _al_q)
                                _cq = _icg_q("tiktok", _contact)
                                if _cq.get("canonical_id"):
                                    _al_q(_cq["canonical_id"], "qualified")
                            except Exception:
                                pass
                        # 话术补发：如无已发 referral/auto_reply，补发引流话术
                        try:
                            _intrs = _lstore.get_interactions(_lid_inbox, platform="tiktok")
                            _has_pitch = any(
                                ix.get("direction") == "outbound" and
                                ix.get("action") in ("referral_sent", "auto_reply", "pitch", "send_dm")
                                for ix in _intrs
                            )
                            if not _has_pitch:
                                # ── 通用引流话术：支持任意 app + 语言检测 + AI生成 ──
                                _ref_msg = ""
                                try:
                                    import yaml as _yaml2
                                    _cfg2 = _yaml2.safe_load(open(
                                        config_file("chat_messages.yaml"),
                                        encoding="utf-8"))
                                    _dev_refs = (_cfg2 or {}).get("device_referrals", {}).get(resolved, {})
                                    # 通用：支持所有 app（不限 TG/WA）
                                    _all_refs_ib = {k: v for k, v in _dev_refs.items()
                                                    if v and not k.startswith("_")}
                                    _AP_P_IB = ["telegram","whatsapp","instagram","line",
                                                "wechat","viber","signal","facebook"]
                                    _AP_L_IB = {"telegram":"Telegram","whatsapp":"WhatsApp",
                                                "instagram":"Instagram","line":"Line","wechat":"WeChat",
                                                "viber":"Viber","signal":"Signal","facebook":"Facebook"}
                                    _c_p_ib = []
                                    for _ap in _AP_P_IB:
                                        if _ap in _all_refs_ib:
                                            _c_p_ib.append(f"{_AP_L_IB[_ap]}: {_all_refs_ib[_ap]}")
                                    for _ap, _v in _all_refs_ib.items():
                                        if _ap not in _AP_P_IB:
                                            _c_p_ib.append(f"{_ap.capitalize()}: {_v}")
                                    _contact_ib = " / ".join(_c_p_ib[:2])

                                    if _contact_ib:
                                        # 语言检测（基于对方最后一条入站消息）
                                        _last_in_ib = next(
                                            (ix for ix in _intrs if ix.get("direction") == "inbound"),
                                            None)
                                        _last_msg_ib = (_last_in_ib or {}).get("content", "")

                                        def _det_lang_ib(text):
                                            if not text: return "it"
                                            if any('\u4e00' <= c <= '\u9fff' for c in text): return "zh"
                                            if any('\u0600' <= c <= '\u06ff' for c in text): return "ar"
                                            _w = set(text.lower().split())
                                            _sc = {
                                                "it": len(_w & {"ciao","grazie","buongiorno","come","cosa","dove","ho","mi","ti","sono","che","per","sì","si","bene","ok","voglio","posso"}),
                                                "es": len(_w & {"hola","gracias","buenos","como","que","donde","quiero","puedo","por","me","te","no","hay","tengo"}),
                                                "en": len(_w & {"hello","hi","thanks","thank","you","the","is","what","how","where","can","my","your","have","yes","no","ok","hey","great"}),
                                                "fr": len(_w & {"bonjour","merci","salut","oui","non","comment","veux","peux","mon","ton","je","tu","est"}),
                                                "de": len(_w & {"hallo","danke","bitte","ja","nein","wie","was","ich","du","wir","ist","sind"}),
                                            }
                                            _b = max(_sc, key=lambda k: _sc[k])
                                            return _b if _sc[_b] > 0 else "it"

                                        _lang_ib = _det_lang_ib(_last_msg_ib)
                                        # 多语言模板（兜底，AI生成失败时使用）
                                        _ib_tpls = {
                                            "it": f"Ciao! Se vuoi restare in contatto, scrivimi: {_contact_ib} 📲",
                                            "en": f"Hey! Let's connect — reach me here: {_contact_ib} 📲",
                                            "es": f"¡Hola! Escríbeme aquí para mantenernos en contacto: {_contact_ib} 📲",
                                            "fr": f"Salut! Pour rester en contact, écris-moi: {_contact_ib} 📲",
                                            "de": f"Hallo! Schreib mir hier: {_contact_ib} 📲",
                                        }
                                        _ref_msg = _ib_tpls.get(_lang_ib, _ib_tpls["it"])

                                        # AI 生成更自然的引流消息（有上下文或非意大利语）
                                        if _last_msg_ib or _lang_ib != "it":
                                            try:
                                                from ..ai.llm_client import get_llm_client as _gllm_ib
                                                _llm_ib = _gllm_ib()
                                                _ln_map_ib = {"it":"Italian","en":"English",
                                                              "es":"Spanish","fr":"French",
                                                              "de":"German","zh":"Chinese"}
                                                _ln_ib = _ln_map_ib.get(_lang_ib, "the same language as the user")
                                                _sys_ib = "You generate short, natural TikTok DM referrals. Human tone. No emoji overuse."
                                                _usr_ib = (
                                                    f"User @{_contact} just showed strong interest on TikTok.\n"
                                                    + (f"Their message: 「{_last_msg_ib[:150]}」\n" if _last_msg_ib else "")
                                                    + f"Our contact: {_contact_ib}\n"
                                                    f"Language: {_ln_ib}\n"
                                                    "Write a 10-25 word referral DM. Include the contact info naturally. Friendly. Output only the message."
                                                )
                                                _ai_ib = _llm_ib.chat_with_system(
                                                    _sys_ib, _usr_ib, temperature=0.8,
                                                    max_tokens=60, use_cache=False)
                                                _ai_ib = (_ai_ib or "").strip().strip('"').strip("「」")
                                                if len(_ai_ib) > 8:
                                                    _ref_msg = _ai_ib
                                            except Exception:
                                                pass  # 保留模板兜底
                                except Exception:
                                    pass
                                if _ref_msg:
                                    from .task_store import create_task as _ct_pitch
                                    _tid_pitch = _ct_pitch(
                                        task_type="tiktok_send_dm",
                                        device_id=resolved,
                                        params={"recipient": _contact, "message": _ref_msg,
                                                "lead_id": _lid_inbox, "smart_timing": True},
                                        priority=80)
                                    from .api import get_worker_pool as _gwp_pitch
                                    from src.host.executor import run_task as _rt_pitch
                                    _gwp_pitch().submit(_tid_pitch, resolved, _rt_pitch, _tid_pitch, config_path or "")
                                    _pitch_scheduled += 1
                        except Exception:
                            pass
                if _qualified_n:
                    logger.info("[收件箱] 自动升级 %d 条 NEEDS_REPLY 线索为 qualified，补发话术 %d 条",
                                _qualified_n, _pitch_scheduled)
        except Exception:
            pass
        return True, "", {"inbox_result": result}

    elif task_type == "tiktok_follow_up":
        from ..workflow.conversation_fsm import check_all_follow_ups
        from src.host.device_state import get_device_state_store as _ds_fu
        _ds_follow_up = _ds_fu("tiktok")
        target_country = params.get("target_country", "italy")
        pending = check_all_follow_ups("tiktok",
                                        max_leads=params.get("max_leads", 30),
                                        target_country=target_country)

        # ── 语言检测工具（基于词表，零外部依赖，~0ms）──
        def _detect_lang_fu(text: str) -> str:
            if not text: return "it"
            if any('\u4e00' <= c <= '\u9fff' for c in text): return "zh"
            if any('\u0600' <= c <= '\u06ff' for c in text): return "ar"
            if any('\u0400' <= c <= '\u04ff' for c in text): return "ru"
            _w = set(text.lower().split())
            _scores = {
                "it": len(_w & {"ciao", "grazie", "buongiorno", "prego", "come", "cosa",
                                "dove", "quando", "voglio", "posso", "ho", "mi", "ti",
                                "sono", "che", "per", "del", "sì", "si", "bene", "ok"}),
                "es": len(_w & {"hola", "gracias", "buenos", "como", "que", "donde",
                                "quiero", "puedo", "por", "del", "me", "te", "hay", "tengo"}),
                "en": len(_w & {"hello", "hi", "thanks", "thank", "you", "the", "is",
                                "are", "what", "how", "where", "can", "my", "your",
                                "have", "has", "yes", "no", "ok", "hey", "great"}),
                "fr": len(_w & {"bonjour", "merci", "salut", "oui", "non", "comment",
                                "quoi", "veux", "peux", "mon", "ton", "nous", "vous", "je"}),
                "de": len(_w & {"hallo", "danke", "bitte", "ja", "nein", "wie", "was",
                                "ich", "du", "wir", "ist", "sind", "mein"}),
            }
            _best = max(_scores, key=lambda k: _scores[k])
            return _best if _scores[_best] > 0 else "it"

        # ── 加载设备引流配置（所有 app）──
        import yaml as _fu_yaml
        _fu_cfg = config_file("chat_messages.yaml")
        _fu_refs = {}
        try:
            if _fu_cfg.exists():
                _fu_refs = (_fu_yaml.safe_load(_fu_cfg.read_text(encoding="utf-8")) or {}).get(
                    "device_referrals", {}).get(resolved, {})
        except Exception: pass
        _AP_PRIO_FU = ["telegram", "whatsapp", "instagram", "line", "wechat", "viber", "signal"]
        _AP_LABELS_FU = {"telegram": "Telegram", "whatsapp": "WhatsApp", "instagram": "Instagram",
                         "line": "Line", "wechat": "WeChat", "viber": "Viber", "signal": "Signal"}
        _all_refs = {k: v for k, v in _fu_refs.items() if v and not k.startswith("_")}
        _contact_parts_fu = []
        for _ap in _AP_PRIO_FU:
            if _ap in _all_refs:
                _contact_parts_fu.append(f"{_AP_LABELS_FU[_ap]}: {_all_refs[_ap]}")
        for _ap, _v in _all_refs.items():
            if _ap not in _AP_PRIO_FU:
                _contact_parts_fu.append(f"{_ap.capitalize()}: {_v}")
        _contact_str_fu = " / ".join(_contact_parts_fu[:2])

        sent = 0
        for fu in pending:
            template = fu.get("template", "")
            lead_name = fu.get("lead_name", "")
            if not (template and lead_name):
                continue
            msg = template.replace("{name}", lead_name)
            # 注入联系方式（模板占位符 {contact} 或追加）
            if _contact_str_fu:
                if "{contact}" in msg:
                    msg = msg.replace("{contact}", _contact_str_fu)
                else:
                    msg = msg + " " + _contact_str_fu
            try:
                from ..leads.store import get_leads_store
                store = get_leads_store()

                # ── 获取线索最近入站消息（用于语言检测）──
                _recent_ixs = store.get_interactions(fu["lead_id"], platform="tiktok", limit=10)
                _last_inbound = next(
                    (ix for ix in _recent_ixs if ix.get("direction") == "inbound"), None)
                _last_msg_txt = (_last_inbound or {}).get("content", "")
                _detected_lang = _detect_lang_fu(_last_msg_txt)

                # ── AI 协同生成（语言自适应）──
                _ai_msg = ""
                _lang_names = {"it": "Italian", "en": "English", "es": "Spanish",
                               "fr": "French", "de": "German", "zh": "Chinese",
                               "ar": "Arabic", "ru": "Russian"}
                _lang_name = _lang_names.get(_detected_lang, "the same language as the user")
                try:
                    from ..ai.llm_client import get_llm_client as _get_llm_fu
                    _llm_fu = _get_llm_fu()
                    _sys_fu = (
                        "You are a TikTok DM specialist sending follow-up messages to social media leads. "
                        "Your job: write natural, friendly follow-up DMs that feel human, not robotic. "
                        "Always write in the specified language. Include the contact info naturally."
                    )
                    _usr_fu = (
                        f"Lead name: {lead_name}\n"
                        f"Their last message: 「{_last_msg_txt[:150]}」\n" if _last_msg_txt else
                        f"Lead name: {lead_name}\n"
                    ) + (
                        f"Our contact: {_contact_str_fu}\n"
                        f"Follow-up template (use as style guide, rewrite naturally): {template[:200]}\n"
                        f"Language: {_lang_name}\n"
                        f"Rules: 15-40 words, friendly, include contact info, no hashtags, no quotes.\n"
                        f"Output only the message."
                    )
                    _ai_msg = _llm_fu.chat_with_system(
                        _sys_fu, _usr_fu, temperature=0.82, max_tokens=90, use_cache=False)
                    _ai_msg = (_ai_msg or "").strip().strip('"').strip("「」")
                    if len(_ai_msg) < 5: _ai_msg = ""
                except Exception as _ai_e:
                    logger.debug("[follow_up] AI生成失败: %s", _ai_e)

                # 最终消息：AI > 模板+联系方式（同语言使用模板；非默认语言优先 AI）
                if _ai_msg and (_detected_lang != "it" or _last_msg_txt):
                    msg = _ai_msg
                # 否则保留模板消息（已注入联系方式）

                profiles = store.get_platform_profiles(fu["lead_id"])
                tt_profile = next(
                    (p for p in profiles if p.get("platform") == "tiktok"), None)
                if tt_profile and tt_profile.get("username"):
                    uname = tt_profile["username"]
                    # P5: 跨设备 DM 去重 — 已发过的跳过
                    _tracker = _get_leads_tracker()
                    if _tracker.was_dm_sent(uname):
                        logger.info("[DM去重] %s 已收过 DM，跳过 follow_up", uname)
                        continue
                    ok = tt.send_dm(uname, msg)
                    if ok:
                        sent += 1
                        # P6: 记入每日 DM 配额（follow_up 也消耗名额）
                        _ds_follow_up.record_chats(state_device_id, 1)
                        store.add_interaction(
                            fu["lead_id"], "tiktok", "follow_up",
                            direction="outbound", content=msg[:500],
                            metadata={"follow_up_number": fu["follow_up_number"],
                                      "state": fu["state"],
                                      "detected_lang": _detected_lang,
                                      "ai_generated": bool(_ai_msg)},
                        )
            except Exception as e:
                logger.debug("[follow_up] Failed for lead #%d: %s", fu["lead_id"], e)
        return True, "", {"pending": len(pending), "sent": sent}

    elif task_type == "tiktok_auto":
        result = _execute_tiktok_auto(tt, manager, resolved, params)
        return True, "", result

    elif task_type == "tiktok_status":
        tracker = _get_leads_tracker()
        stats = tracker.get_stats()
        device_follows = tracker.get_device_follows(resolved)
        return True, "", {
            "device_id": resolved,
            "device_follows": device_follows,
            "platform_stats": stats,
        }

    elif task_type == "tiktok_workflow":
        return False, "tiktok_workflow 由 WorkflowExecutor 处理", None

    elif task_type == "tiktok_scan_username":
        username = tt.scan_own_username(resolved)
        if username:
            from src.host.device_state import get_device_state_store
            ds = get_device_state_store("tiktok")
            ds.set(resolved, "tiktok_username", username)
        return True, "", {"username": username or "", "device_id": resolved}

    elif task_type == "tiktok_follow_user":
        target = params.get("target_username", "")
        if not target:
            return False, "target_username 必填", None
        ok = tt.follow_user(target, resolved)
        if ok:
            _register_tiktok_identities(
                [{"username": target, "name": target}],
                state_device_id, "follow_user")
        return ok, ("" if ok else f"关注 {target} 失败"), {"target": target, "followed": ok}

    elif task_type == "tiktok_interact_user":
        target = params.get("target_username", "")
        if not target:
            return False, "target_username 必填", None
        result = tt.interact_with_user(
            target, resolved,
            watch_seconds=params.get("watch_seconds", 15),
            do_like=params.get("do_like", True),
            do_comment=params.get("do_comment", False),
        )
        # 互动成功后记录时间戳，用于去重保护
        if result.get("ok"):
            try:
                import time as _t
                from src.host.device_state import get_device_state_store
                _ds = get_device_state_store("tiktok")
                _key = f"last_interact_{target.lstrip('@').lower()}"
                _ds.set(resolved, _key, str(int(_t.time())))
            except Exception:
                pass
        return True, "", result

    elif task_type == "tiktok_keyword_search":
        # 关键词搜索获客：搜索目标关键词 → 评论预热 → 关注精准用户
        _tc_kw = params.get("target_countries", [])
        if isinstance(_tc_kw, str):
            _tc_kw = [x.strip() for x in _tc_kw.split(",") if x.strip()]
        _tl_kw = params.get("target_languages", [])
        if isinstance(_tl_kw, str):
            _tl_kw = [x.strip() for x in _tl_kw.split(",") if x.strip()]
        _kw_list = params.get("keywords", None)
        if isinstance(_kw_list, str):
            _kw_list = [x.strip() for x in _kw_list.split(",") if x.strip()]
        _max_kw_follows = params.get("max_follows", 20)
        _comment_warmup_kw = params.get("comment_warmup", True)

        result = tt.keyword_search_session(
            target_countries=_tc_kw or None,
            target_languages=_tl_kw or None,
            keywords=_kw_list,
            max_follows=_max_kw_follows,
            comment_warmup=_comment_warmup_kw,
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.device_state import get_device_state_store as _gdss_kw
            _ds_kw = _gdss_kw("tiktok")
            _ds_kw.record_follows(resolved, result.get("followed", 0))
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event as _push_kw
            _push_kw("tiktok.keyword_search_done", {
                "device_id": resolved,
                "followed": result.get("followed", 0),
                "comment_warmed": result.get("comment_warmed", 0),
                "keywords_used": result.get("keywords_used", []),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_live_engage":
        # 直播间互动：进入直播间发评论 → 关注活跃观众（P2-3: 支持 targeting 过滤）
        _tc_live = params.get("target_countries", [])
        if isinstance(_tc_live, str):
            _tc_live = [x.strip() for x in _tc_live.split(",") if x.strip()]
        # 兜底：target_country 单值转数组
        if not _tc_live and params.get("target_country"):
            _tc_live = [params["target_country"]]
        _tl_live = params.get("target_languages", [])
        if isinstance(_tl_live, str):
            _tl_live = [x.strip() for x in _tl_live.split(",") if x.strip()]

        result = tt.live_engage_session(
            target_countries=_tc_live or None,
            target_languages=_tl_live or None,
            max_live_rooms=params.get("max_live_rooms", 3),
            comments_per_room=params.get("comments_per_room", 2),
            follow_active_viewers=params.get("follow_active_viewers", True),
            # ★ P2-3: targeting 透传
            gender=params.get("gender", ""),
            min_age=int(params.get("min_age", 0) or 0),
            max_age=int(params.get("max_age", 0) or 0),
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.device_state import get_device_state_store as _gdss_live
            _ds_live = _gdss_live("tiktok")
            total_followed = result.get("hosts_followed", 0) + result.get("viewers_followed", 0)
            if total_followed > 0:
                _ds_live.record_follows(resolved, total_followed)
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event as _push_live
            _push_live("tiktok.live_engage_done", {
                "device_id": resolved,
                "rooms_visited": result.get("rooms_visited", 0),
                "comments_sent": result.get("comments_sent", 0),
                "hosts_followed": result.get("hosts_followed", 0),
                "viewers_followed": result.get("viewers_followed", 0),
                "viewers_filtered": result.get("viewers_filtered", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_comment_engage":
        # ★ P2-1: 评论区互动 — 搜索热门视频→评论→关注活跃评论者
        result = tt.comment_engage_session(
            target_country=params.get("target_country", "italy"),
            keyword=params.get("keyword", ""),
            max_videos=int(params.get("max_videos", 5)),
            comments_per_video=int(params.get("comments_per_video", 2)),
            follow_commenters=params.get("follow_commenters", True),
            gender=params.get("gender", ""),
            min_age=int(params.get("min_age", 0) or 0),
            max_age=int(params.get("max_age", 0) or 0),
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.device_state import get_device_state_store as _gdss_ce
            _ds_ce = _gdss_ce("tiktok")
            if result.get("followed", 0) > 0:
                _ds_ce.record_follows(resolved, result["followed"])
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event as _push_ce
            _push_ce("tiktok.comment_engage_done", {
                "device_id": resolved,
                "videos_visited": result.get("videos_visited", 0),
                "comments_sent": result.get("comments_sent", 0),
                "followed": result.get("followed", 0),
                "filtered": result.get("filtered", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_check_comment_replies":
        # ★ P3-3: 检查我方视频的评论回复并发送DM（定时调度触发）
        result = tt.check_comment_replies_session(
            max_replies=int(params.get("max_replies", 20)),
            target_languages=params.get("target_languages", None),
            device_id=resolved,
        )
        try:
            from src.host.event_stream import push_event as _push_ccr
            _push_ccr("tiktok.comment_replies_checked", {
                "device_id": resolved,
                "replies_found": result.get("replies_found", 0),
                "dms_sent": result.get("dms_sent", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    elif task_type == "tiktok_campaign_run":
        # ★ P2-4: 完整获客剧本串行执行（在单一任务内顺序执行所有步骤）
        # 比 campaign_playbook 创建多个独立任务更可靠，保证顺序且中途失败可记录
        steps = params.get("steps") or ["warmup", "live_engage", "follow", "check_inbox"]
        tc = params.get("target_country", "italy")
        gender = params.get("gender", "")
        min_age = int(params.get("min_age", 0) or 0)
        max_age = int(params.get("max_age", 0) or 0)

        campaign_result = {
            "steps_completed": [],
            "steps_failed": [],
            "target_country": tc,
            "gender": gender,
            "min_age": min_age,
            "max_age": max_age,
        }

        cb = _make_progress_cb()
        step_count = len(steps)

        for step_idx, step_name in enumerate(steps):
            if cb:
                pct = int((step_idx / step_count) * 90) + 5
                cb(pct, f"剧本步骤 {step_idx + 1}/{step_count}: {step_name}")

            log.info("[Campaign] 执行步骤 %d/%d: %s", step_idx + 1, step_count, step_name)
            try:
                if step_name == "warmup":
                    tt.warmup_session(
                        duration_minutes=params.get("warmup_minutes", 20),
                        target_country=tc,
                        device_id=resolved,
                        progress_callback=None,
                    )
                elif step_name == "live_engage":
                    tt.live_engage_session(
                        target_countries=[tc],
                        max_live_rooms=params.get("max_live_rooms", 3),
                        comments_per_room=2,
                        follow_active_viewers=True,
                        gender=gender,
                        min_age=min_age,
                        max_age=max_age,
                        device_id=resolved,
                    )
                elif step_name == "comment_engage":
                    tt.comment_engage_session(
                        target_country=tc,
                        max_videos=params.get("max_videos", 4),
                        comments_per_video=2,
                        follow_commenters=True,
                        gender=gender,
                        min_age=min_age,
                        max_age=max_age,
                        device_id=resolved,
                    )
                elif step_name == "follow":
                    tt.keyword_search_and_follow_session(
                        target_country=tc,
                        max_follows=params.get("max_follows", 30),
                        gender=gender,
                        min_age=min_age,
                        max_age=max_age,
                        device_id=resolved,
                        progress_callback=None,
                    )
                elif step_name in ("check_inbox", "inbox"):
                    tt.check_inbox_session(
                        auto_reply=True,
                        max_conversations=params.get("max_conversations", 20),
                        device_id=resolved,
                        progress_callback=None,
                    )
                campaign_result["steps_completed"].append(step_name)
                log.info("[Campaign] 步骤 %s 完成", step_name)
                # 步骤间休息 30-60s
                time.sleep(random.uniform(30, 60))

            except Exception as step_err:
                log.error("[Campaign] 步骤 %s 失败: %s", step_name, step_err)
                campaign_result["steps_failed"].append({"step": step_name, "error": str(step_err)})
                # 步骤失败后继续执行下一步（不中断整个剧本）
                time.sleep(10)

        if cb:
            cb(100, f"剧本完成: {len(campaign_result['steps_completed'])}/{step_count} 步骤")
        try:
            from src.host.event_stream import push_event as _push_campaign
            _push_campaign("tiktok.campaign_done", {
                "device_id": resolved,
                "steps_completed": campaign_result["steps_completed"],
                "steps_failed": [s["step"] for s in campaign_result.get("steps_failed", [])],
                "target_country": tc,
            }, resolved)
        except Exception:
            pass
        return True, "", campaign_result

    elif task_type == "tiktok_check_comment_replies":
        # P2-2: 检查评论回复通知，对回复者发DM
        _tl_cmtr = params.get("target_languages", [])
        if isinstance(_tl_cmtr, str):
            _tl_cmtr = [x.strip() for x in _tl_cmtr.split(",") if x.strip()]
        result = tt.check_comment_replies_session(
            max_replies=params.get("max_replies", 20),
            target_languages=_tl_cmtr or None,
            device_id=resolved,
            progress_callback=_make_progress_cb(),
        )
        try:
            from src.host.event_stream import push_event as _push_cmtr
            _push_cmtr("tiktok.comment_replies_checked", {
                "device_id": resolved,
                "checked": result.get("checked", 0),
                "dmed": result.get("dmed", 0),
            }, resolved)
        except Exception:
            pass
        return True, "", result

    # ★ P0 新增: 通讯录好友发现
    elif task_type == "tiktok_contact_discovery":
        from src.app_automation.contacts_manager import tiktok_find_contact_friends
        result = tiktok_find_contact_friends(
            tiktok_automation=tt,
            device_id=resolved,
            max_friends=params.get("max_friends", 15),
            auto_follow=params.get("auto_follow", True),
            auto_message=params.get("auto_message", False),
            target_language=params.get("target_language", "italian"),
        )
        # ★ 自动同步到 LeadsStore（闭环核心）
        if result.get("discovered_names"):
            try:
                from src.workflow.platform_actions_bridge import _sync_contact_discoveries_to_leads
                synced = _sync_contact_discoveries_to_leads(
                    device_id=resolved,
                    discovered_names=result["discovered_names"],
                )
                result["synced_to_leads"] = synced
            except Exception as e:
                logger.warning("[通讯录发现] LeadsStore 同步失败: %s", e)
        logger.info("[通讯录发现] 完成: found=%d followed=%d synced=%s",
                    result.get("found", 0), result.get("followed", 0),
                    result.get("synced_to_leads", 0))
        return True, "", result

    # ★ P0 新增: Drip Campaign 跟进任务
    elif task_type == "tiktok_drip_followup":
        username = params.get("username", "")
        lead_id = params.get("lead_id")
        context = params.get("context", "followup_day1")

        if not username:
            return False, "params.username 必填", None

        # 检查用户是否已回复（如已回复则跳过，不打扰）
        if params.get("cancel_if_replied") and lead_id:
            try:
                from src.leads.store import get_leads_store
                store = get_leads_store()
                interactions = store.get_interactions(lead_id, platform="tiktok", limit=50)
                inbound = [ix for ix in interactions if ix.get("direction") == "inbound"]
                if inbound:
                    logger.info("[Drip] 用户 %s 已回复，跳过 %s 跟进任务", username, context)
                    return True, "already_replied_skip", {"skipped": True, "reason": "user_replied"}
            except Exception:
                pass

        # 生成对应阶段的消息
        try:
            from src.ai.tiktok_chat_ai import TikTokChatAI
            ai = TikTokChatAI()
            if context == "referral":
                # Day3: 发引流消息
                message = ai.generate_natural_message(context="referral", language="italian")
            elif context == "last_chance":
                # Day7: 最后机会
                message = ai.generate_natural_message(context="followup_gentle", language="italian")
            else:
                # Day1: 互动型内容
                message = ai.generate_natural_message(context="engage_question", language="italian")
        except Exception:
            message = "Ciao! Come stai? 😊"

        ok = tt.send_dm(username=username, message=message)
        if ok and lead_id:
            try:
                from src.leads.store import get_leads_store
                get_leads_store().add_interaction(
                    lead_id, "tiktok", "drip_followup",
                    direction="outbound",
                    device_id=resolved,
                    metadata={"context": context, "message": message[:100]},
                )
            except Exception:
                pass

        logger.info("[Drip] %s 发送给 %s: %s", context, username, "✓" if ok else "✗")
        return ok, ("" if ok else "Drip 消息发送失败"), {"context": context, "sent": ok}

    # ★ P3-2: 高分 Lead 优先触达任务
    elif task_type == "tiktok_priority_outreach":
        max_leads = params.get("max_leads", 10)
        min_score = float(params.get("min_score", 50.0))
        try:
            from src.host.lead_priority_outreach import run_priority_outreach
            stats = run_priority_outreach(
                device_id=resolved,
                max_leads=max_leads,
                min_score=min_score,
            )
            logger.info("[PriorityOutreach] 完成: %s", stats)
            return True, "", stats
        except Exception as e:
            logger.error("[PriorityOutreach] 执行失败: %s", e)
            return False, str(e), None

    # AI 线索重评：与 job_scheduler 中 tiktok_ai_rescore 一致；tiktok_ai_restore 为任务中心/旧版别名
    elif task_type in ("tiktok_ai_rescore", "tiktok_ai_restore"):
        try:
            import json as _json
            import os
            import urllib.error
            import urllib.request

            limit = int(params.get("limit", 30))
            platform = params.get("platform", "tiktok")
            payload = _json.dumps({"limit": limit, "platform": platform}).encode("utf-8")
            from src.openclaw_env import local_api_base

            req = urllib.request.Request(
                f"{local_api_base()}/tiktok/leads/ai-rescore",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            key = (os.environ.get("OPENCLAW_API_KEY") or "").strip()
            if key:
                req.add_header("X-API-Key", key)
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = _json.loads(resp.read().decode())
            return True, "", data
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:300]
            logger.error("[tiktok_ai_rescore] HTTP %s: %s", e.code, err_body)
            return False, f"ai-rescore HTTP {e.code}: {err_body}", None
        except Exception as e:
            logger.error("[tiktok_ai_rescore/restore] %s", e)
            return False, str(e)[:200], None

    return False, f"不支持的 TikTok 任务类型: {task_type}", None


def _execute_tiktok_auto(tt, manager, resolved, params):
    """Full auto mode: VPN检查 → warmup → test follow → smart follow → chat + 引流。"""
    from src.app_automation.target_filter import TargetProfile
    from src.host.device_state import get_device_state_store
    import random

    def _auto_progress(pct: int, msg: str):
        tid = _get_current_task_id()
        if tid:
            try:
                update_task_progress(tid, pct, msg)
            except Exception:
                pass

    target_country = params.get("target_country", "italy")

    # 自动检测阶段（而不是从参数读）
    ds = get_device_state_store("tiktok")
    state_did = resolved  # 用真实设备 ID
    ds.init_device(state_did)
    phase = ds.determine_phase(state_did) if not params.get("phase") else params["phase"]
    can_follow = ds.can_follow(state_did)

    result = {"phase": phase, "warmup": None, "follow_test": None,
              "follows": None, "chat": None, "vpn": None}

    # Step 0: VPN 检查 + 静默重连 — 失败则中止任务
    vpn_ok, vpn_msg = _ensure_vpn(resolved)
    result["vpn"] = "connected" if vpn_ok else "failed"
    if not vpn_ok:
        raise RuntimeError(vpn_msg)  # 让上层 _execute_with_retry 捕获并标记任务失败

    _auto_progress(5, "VPN 已连接，开始养号")

    if phase == "cold_start":
        duration = random.randint(20, 40)
    elif phase == "interest_building":
        duration = random.randint(30, 50)
    else:
        duration = random.randint(30, 45)

    warmup_stats = tt.warmup_session(
        duration_minutes=duration,
        target_country=target_country,
        phase=phase,
    )
    result["warmup"] = warmup_stats
    _auto_progress(40, f"养号完成 ({duration}min)")

    if phase == "interest_building" and not can_follow:
        if random.random() < 0.3:
            d = tt._u2(resolved)  # ADB fallback
            if tt.launch(resolved):
                ok = tt._random_test_follow(d, resolved)
                result["follow_test"] = {"can_follow": ok}
                if ok:
                    can_follow = True
                    phase = "active"
                    result["phase"] = "active"
        _auto_progress(50, "关注测试完成")

    if phase == "active" and can_follow:
        target = TargetProfile(
            country=params.get("country") or params.get("target_country", "italy"),
            language=params.get("language", "italian"),
            gender=params.get("gender", ""),      # "" = 不限性别
            min_age=params.get("min_age", 0),     # 0 = 不限年龄
        )
        max_follows = params.get("max_follows", 15)
        tracker = _get_leads_tracker()
        follow_result = tt.smart_follow(
            target=target, max_follows=max_follows,
            global_tracker=tracker,
            progress_callback=_make_progress_cb(),
        )
        result["follows"] = follow_result
        _auto_progress(75, f"关注完成 ({follow_result.get('followed', 0)}人)")

        if random.random() < 0.6:
            tt.warmup_session(
                duration_minutes=random.randint(5, 15),
                target_country=target_country,
                phase="active",
            )
        _auto_progress(85, "二次养号完成")

        try:
            chat_path = config_file("chat_messages.yaml")
            messages = []
            if chat_path.exists():
                with open(chat_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                # 分阶段消息: 回关时用纯问候，引流消息交给 follow_up 定时发
                msg_type = params.get("message_type", "greeting")
                if msg_type == "referral":
                    messages = data.get("messages", [])  # 含引流链接
                else:
                    messages = data.get("greeting_messages", data.get("messages", []))
            if messages:
                chat_result = tt.check_and_chat_followbacks(
                    messages, params.get("max_chats", 10),
                    global_tracker=tracker,
                )
                result["chat"] = chat_result
                _auto_progress(98, f"聊天完成 ({chat_result.get('messaged', 0)}条)")
        except Exception as e:
            logger.warning("TikTok auto chat failed: %s", e)

    return result


def _execute_batch_send(manager, resolved, params, config_path):
    """
    批量发送: 同一条消息发给多个用户。支持断点续传。
    params: {targets: ["@user1", "@user2"], message: "...", _checkpoint: {...}}
    """
    targets = params.get("targets") or []
    message = params.get("message", "")
    if not targets:
        return False, "params.targets 列表为空", None
    if not message:
        return False, "params.message 不能为空", None

    checkpoint = params.get("_checkpoint") or {}
    start_idx = checkpoint.get("completed_idx", 0)
    prior_results = checkpoint.get("results", [])

    if start_idx > 0:
        logger.info("批量发送从断点恢复: 跳过前 %d/%d 个目标",
                     start_idx, len(targets))

    results = list(prior_results)
    success_count = sum(1 for r in results if r.get("success"))

    task_id = _get_current_task_id()

    for i in range(start_idx, len(targets)):
        target = targets[i]
        tg = _fresh_telegram(manager, resolved)
        ok = False
        err = ""
        try:
            if tg.search_and_open_user(target, resolved):
                ok = tg.send_text_message(message, resolved)
                if not ok:
                    err = "发送失败"
            else:
                err = f"搜索用户 {target} 失败"
        except Exception as e:
            err = str(e)

        results.append({"target": target, "success": ok, "error": err})
        if ok:
            success_count += 1

        if task_id:
            _save_task_checkpoint(task_id, {
                "completed_idx": i + 1,
                "results": results,
                "total": len(targets),
            })
            update_task_progress(
                task_id,
                int((i + 1) / len(targets) * 100),
                f"已发送 {i + 1}/{len(targets)}",
            )

        time.sleep(1)

    all_ok = success_count == len(targets)
    summary = f"{success_count}/{len(targets)} 发送成功"
    if start_idx > 0:
        summary += f" (从第{start_idx + 1}个恢复)"
    return all_ok, ("" if all_ok else summary), {"batch_results": results, "summary": summary}


def _get_current_task_id() -> str:
    """Get the task_id of the currently executing task."""
    try:
        from src.utils.log_config import _task_context
        return getattr(_task_context, "task_id", "")
    except Exception:
        return ""


def _save_task_checkpoint(task_id: str, data: dict):
    """Save checkpoint for the current task."""
    try:
        from .task_store import save_checkpoint
        save_checkpoint(task_id, data)
    except Exception:
        pass


def _lock_portrait(manager, device_id: str) -> None:
    """Force portrait orientation and disable auto-rotate via ADB.

    Many phones switch to landscape during automation; this ensures
    all UI element coordinates remain valid throughout the task.
    """
    try:
        manager.execute_adb_command(
            "shell settings put system accelerometer_rotation 0", device_id)
        manager.execute_adb_command(
            "shell settings put system user_rotation 0", device_id)
        logger.debug("[portrait-lock] %s 已锁定竖屏", device_id[:12])
    except Exception as e:
        logger.warning("[portrait-lock] %s 设置失败: %s", device_id[:12], e)


# ── P4 全域埋点（智拓 zhituo → platform/observability spool，fail-silent） ──
# 任务结果里可折算为"对外触达发送量"的计数键。各任务类型的 stats 形状不同、
# 键不重叠：campaign 出 friend_requests_sent/messages_replied，收件箱任务出
# replied/replies_sent，打招呼出 greeted 等。逐键求和即该批次总触达量。
_TELE_OUTREACH_KEYS = (
    "friend_requests_sent",   # facebook campaign: 好友请求
    "greeted",                # 打招呼批次 / add_friend_and_greet
    "greetings_sent",
    "replied",                # facebook_check_inbox 自动回复
    "replies_sent",           # facebook_check_message_requests
    "messages_replied",       # facebook campaign 的 check_inbox 步骤
    "dms_sent",               # tiktok 私信
)


def _tele_platform_of(task_type: str) -> str:
    """task_type 前缀 → 平台名（facebook_add_friend → facebook）。"""
    if not task_type:
        return "unknown"
    if task_type == "batch_send":       # 历史遗留：telegram 批量发送无前缀
        return "telegram"
    return task_type.split("_", 1)[0] or "unknown"


def _telemetry_task_started(task: dict) -> None:
    """获客任务开始真机执行 → zhituo.task.started（重试不重复计数）。

    fail-silent：埋点异常绝不影响任务执行。
    """
    try:
        if int(task.get("retry_count") or 0) > 0:
            return   # 重试是同一业务事实，只在首次尝试发一次
        from src.telemetry import track
        task_type = task.get("type", "") or ""
        track("zhituo.task.started", {
            "task_id": str(task.get("task_id") or ""),
            "platform": _tele_platform_of(task_type),
            "task_type": task_type,
            "device_count": 1,
        })
    except Exception:
        pass


def _telemetry_task_finished(task_id: str, task_type: str,
                             started_ts: float) -> None:
    """任务到达终态 → zhituo.task.completed + 聚合触达量 sent_metered。

    只在 status 落定 completed/failed 时发（重试排队/取消不发）；触达量
    从任务结果的批次统计里折算增量，绝不逐条消息发事件。fail-silent。
    """
    try:
        from src.telemetry import track
        task = get_task(task_id) or {}
        status = task.get("status", "")
        if status not in ("completed", "failed"):
            return
        result = task.get("result")
        if not isinstance(result, dict):
            result = {}
        platform = _tele_platform_of(task_type)
        track("zhituo.task.completed", {
            "task_id": str(task_id),
            "platform": platform,
            "task_type": task_type,
            "success": status == "completed",
            "duration_s": max(int(time.time() - started_ts), 0),
        })
        sent = 0
        for key in _TELE_OUTREACH_KEYS:
            val = result.get(key)
            if isinstance(val, bool):
                sent += 1 if val else 0
            elif isinstance(val, (int, float)):
                sent += max(int(val), 0)
        if not sent and isinstance(result.get("batch_results"), list):
            sent = sum(1 for r in result["batch_results"]
                       if isinstance(r, dict) and r.get("success"))
        if sent > 0:
            track("zhituo.outreach.sent_metered", {
                "platform": platform,
                "task_id": str(task_id),
                "task_type": task_type,
                "sent": sent,
            })
    except Exception:
        pass


def run_task(task_id: str, config_path: Optional[str] = None) -> None:
    config_path = config_path or DEFAULT_DEVICES_YAML
    task = get_task(task_id)
    if not task or task.get("status") != "pending":
        return

    device_id = task.get("device_id")
    params = task.get("params") or {}
    task_type = task.get("type", "")
    resolved = None
    _tele_started_at = None   # P4 埋点：真正开始执行后才记，gate 拦截不算

    try:
        manager = get_device_manager(config_path)
        manager.discover_devices()
        resolved = _get_device_id(manager, device_id, config_path,
                                  task_type=task_type)
        if not resolved:
            set_task_result(task_id, False, error="无可用设备（请确认 ADB 已连接手机）")
            return

        # 统一门禁：tier × gate_mode 矩阵 + 预检/GEO（task_dispatch_gate）
        # 2026-05-07 fix: 主控已完成 gate 校验的转发任务（_coordinator_forwarded）
        # 在 Worker 本地执行时跳过重复 gate，避免 GEO 探测失败。
        _forwarded = bool(params.get("_coordinator_forwarded"))
        try:
            from src.host.task_dispatch_gate import evaluate_task_gate_detailed
            if _forwarded:
                logger.info("[gate] 跳过 run_task gate: _coordinator_forwarded=True task=%s device=%s",
                            task_type, (resolved or "")[:8])
                gate_ev = None
            else:
                gate_ev = evaluate_task_gate_detailed(task, resolved, config_path)
            if gate_ev is not None and not gate_ev.allowed:
                set_task_result(
                    task_id,
                    False,
                    error=gate_ev.reason,
                    extra={
                        "device_id": resolved,
                        "gate_evaluation": gate_ev.to_dict(),
                    },
                )
                metrics.inc_task(False)
                metrics.record_task_result(resolved, False)
                try:
                    from .event_stream import push_event
                    push_event(
                        "task.gate_blocked",
                        task_id=task_id,
                        device_id=resolved,
                        task_type=task_type,
                        reason=gate_ev.reason,
                        tier=gate_ev.tier,
                        gate_mode=gate_ev.gate_mode,
                        hint_code=getattr(gate_ev, "hint_code", "") or "",
                    )
                except Exception:
                    pass
                return
        except Exception as _ge:
            logger.exception("[gate] 评估异常，为安全起见中止任务: %s", _ge)
            set_task_result(task_id, False, error=f"[gate] 评估异常: {_ge}")
            metrics.inc_task(False)
            return

        set_task_running(task_id)
        set_task_context(task_id=task_id, device_id=task.get("device_id", ""))
        _tele_started_at = time.time()
        _telemetry_task_started(task)

        # 2026-05-12: 强制竖屏 — 防止运行期间横屏导致 UI 元素找不到
        _lock_portrait(manager, resolved)

        # 2026-05-08 P1-2: 智能超时 — 先查历史耗时，再 fallback 到静态表
        timeout = _smart_timeout(task_type, resolved)
        try:
            success, error_detail, extra_data = run_with_timeout(
                _execute_with_retry, timeout,
                manager, resolved, task_type, params, config_path, task_id
            )
        except TaskTimeout:
            # 2026-05-08: feed 类任务超时视为 partial success（已滑了部分屏）
            _is_feed = task_type in ("facebook_browse_feed",
                                     "facebook_browse_feed_by_interest",
                                     "tiktok_browse_feed")
            if _is_feed:
                set_task_result(task_id, True,
                               error=f"feed 浏览已达时间上限 ({timeout}s)，视为部分完成",
                               extra={"partial": True, "timeout_sec": timeout})
                metrics.inc_task(True, timeout=True)
            else:
                set_task_result(task_id, False, error=f"任务执行超时 ({timeout}s)")
                metrics.inc_task(False, timeout=True)
            return

        screenshot_path = None
        try:
            screenshots_dir = logs_dir() / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = str(screenshots_dir / f"task_{task_id}.png")
            manager.capture_screen(resolved, screenshot_path)
        except Exception as e:
            logger.warning("task screenshot failed: %s", e)

        extra = {"device_id": resolved}
        if extra_data:
            extra.update(extra_data)
        set_task_result(task_id, success, error=error_detail,
                        screenshot_path=screenshot_path, extra=extra)
        metrics.inc_task(success)
        metrics.record_task_result(resolved, success)
        metrics.record_task_type_result(task_type, success)
        try:
            from .smart_scheduler import get_smart_scheduler
            get_smart_scheduler().record_task_result(
                resolved, task_type, success,
                error_msg=error_detail or "")
        except Exception:
            pass
    except Exception as e:
        logger.exception("task %s run error", task_id)
        set_task_result(task_id, False, error=str(e))
        metrics.inc_task(False)
        if device_id:
            metrics.record_task_result(device_id, False)
            metrics.record_task_type_result(task_type, False)
            try:
                from .smart_scheduler import get_smart_scheduler
                get_smart_scheduler().record_task_result(
                    device_id, task_type, False, error_msg=str(e))
            except Exception:
                pass
    finally:
        if task_type.startswith("tiktok_"):
            try:
                from .account_scheduler import get_account_scheduler
                acct = params.get("_active_account") or params.get("account", "")
                if acct and resolved is not None:
                    get_account_scheduler().end_session(resolved, acct)
            except Exception:
                pass
        if _tele_started_at is not None:
            _telemetry_task_finished(task_id, task_type, _tele_started_at)
        clear_task_context()


def _register_tiktok_identities(users: list, device_id: str, via: str):
    """J2+K2: 批量注册 TikTok 用户到 lead_identities + 推进生命周期."""
    if not users:
        return
    try:
        from src.host.lead_mesh.canonical import resolve_identity, advance_lifecycle
        # chat_followback 意味着已发消息 → contacted
        stage = "contacted" if via in ("chat_followback", "follow_user") else "contacted"
        for u in users:
            uname = (u.get("username") or "").strip()
            dname = (u.get("name") or uname or "").strip()
            if not uname:
                continue
            try:
                cid = resolve_identity(
                    platform="tiktok",
                    account_id=uname,
                    display_name=dname,
                    discovered_via=f"tiktok_{via}",
                    discovered_by_device=device_id,
                    auto_merge=True,
                )
                # K2: 自动推进生命周期
                if cid:
                    advance_lifecycle(cid, stage)
            except Exception:
                pass
    except Exception:
        pass
