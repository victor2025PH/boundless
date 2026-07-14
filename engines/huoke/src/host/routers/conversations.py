# -*- coding: utf-8 -*-
"""会话 FSM 与智能调度路由。"""
from fastapi import APIRouter, HTTPException, Depends
from .auth import verify_api_key

from src.device_control.device_manager import get_device_manager
from src.behavior.compliance_guard import get_compliance_guard
from src.host.device_registry import DEFAULT_DEVICES_YAML

router = APIRouter(tags=["conversations"], dependencies=[Depends(verify_api_key)])

_config_path = DEFAULT_DEVICES_YAML


def _resolve_device(device_id: str) -> str:
    from ..executor import _get_device_id
    manager = get_device_manager(_config_path)
    manager.discover_devices()
    resolved = _get_device_id(manager, device_id, _config_path)
    if not resolved:
        raise HTTPException(status_code=404, detail="无可用设备")
    return resolved


# ── Conversation FSM ──


@router.get("/conversations/follow-ups")
def pending_follow_ups(platform: str = "tiktok", max_leads: int = 30):
    """List conversations that need follow-up messages."""
    from src.workflow.conversation_fsm import check_all_follow_ups
    return {"follow_ups": check_all_follow_ups(platform, max_leads)}


@router.get("/conversations/{lead_id}")
def conversation_detail(lead_id: int, platform: str = "tiktok"):
    """Get conversation state and history for a lead."""
    from src.workflow.conversation_fsm import get_conversation_summary
    return get_conversation_summary(lead_id, platform)


@router.get("/conversations/stats/pipeline")
def conversation_pipeline(platform: str = "tiktok"):
    """Get conversation state distribution across all leads."""
    from src.workflow.conversation_fsm import ConvState, ConversationFSM
    from src.leads.store import get_leads_store

    store = get_leads_store()
    counts = {s.value: 0 for s in ConvState}

    for status in ("contacted", "responded", "qualified", "new"):
        leads = store.list_leads(status=status, platform=platform, limit=500)
        for lead in leads:
            fsm = ConversationFSM(lead["id"], platform)
            state = fsm.get_state()
            counts[state.value] += 1

    total = sum(counts.values())
    return {
        "pipeline": counts,
        "total_active": total - counts.get("idle", 0) - counts.get("converted", 0) - counts.get("rejected", 0),
        "conversion_rate": round(counts.get("converted", 0) / max(total, 1), 4),
    }


# ── Timezone-Aware Scheduling ──


@router.post("/schedule/best_send_time")
def api_best_send_time(body: dict):
    """Calculate optimal send time for a lead based on their timezone."""
    from src.workflow.smart_schedule import best_send_time
    tz = body.get("timezone", "")
    if not tz:
        raise HTTPException(status_code=400, detail="timezone required")
    result = best_send_time(tz, body.get("platform", ""))
    if result:
        return {"send_at": result.isoformat(), "timezone": tz}
    return {"send_at": None, "timezone": tz}


@router.post("/schedule/batch")
def api_schedule_batch(body: dict):
    """Schedule optimal times for a batch of leads."""
    from src.workflow.smart_schedule import schedule_for_leads
    leads = body.get("leads", [])
    if not leads:
        raise HTTPException(status_code=400, detail="leads list required")
    return {"scheduled": schedule_for_leads(leads, body.get("platform", ""))}


# ── 合规配额查询 ──


@router.get("/compliance/{platform}/{action}/remaining")
def compliance_action_remaining(platform: str, action: str, account: str = ""):
    """获取指定平台/动作的剩余配额。"""
    guard = get_compliance_guard()
    remaining = guard.get_remaining(platform, action, account)
    return remaining


@router.get("/compliance/{platform}")
def compliance_status(platform: str, account: str = ""):
    guard = get_compliance_guard()
    status = guard.get_platform_status(platform, account)
    if "error" in status:
        raise HTTPException(status_code=400, detail=status["error"])
    return status


# ── 账号管理 (同步直接调用，不走任务队列) ──


@router.post("/accounts/telegram/list")
def telegram_list_accounts(body: dict):
    device_id = body.get("device_id", "")
    resolved = _resolve_device(device_id)
    manager = get_device_manager(_config_path)
    from src.app_automation.telegram import TelegramAutomation
    tg = TelegramAutomation(manager)
    tg.set_current_device(resolved)
    accounts = tg.list_accounts(resolved)
    return {"device_id": resolved, "accounts": accounts}


@router.post("/accounts/telegram/switch")
def telegram_switch_account(body: dict):
    device_id = body.get("device_id", "")
    account_name = body.get("account_name", "")
    if not account_name:
        raise HTTPException(status_code=400, detail="account_name 必填")
    resolved = _resolve_device(device_id)
    manager = get_device_manager(_config_path)
    from src.app_automation.telegram import TelegramAutomation
    tg = TelegramAutomation(manager)
    tg.set_current_device(resolved)
    ok = tg.switch_account(account_name, resolved)
    return {"success": ok, "device_id": resolved, "account": account_name}


@router.post("/accounts/telegram/current")
def telegram_current_account(body: dict):
    device_id = body.get("device_id", "")
    resolved = _resolve_device(device_id)
    manager = get_device_manager(_config_path)
    from src.app_automation.telegram import TelegramAutomation
    tg = TelegramAutomation(manager)
    tg.set_current_device(resolved)
    info = tg.get_current_account(resolved)
    return {"device_id": resolved, "account": info}
