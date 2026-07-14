# -*- coding: utf-8 -*-
"""告警通知配置路由。"""

import logging
from fastapi import APIRouter, Depends

from src.host.device_registry import config_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/notifications", tags=["notifications"])


def _normalize_notifications_payload(body: dict) -> dict:
    """保证 telegram.recipients 为字符串列表；支持多行字符串拆分。"""
    import copy

    from ..telegram_destinations import lines_to_recipients

    out = copy.deepcopy(body) if body else {}
    tg = out.get("telegram")
    if not isinstance(tg, dict):
        return out
    r = tg.get("recipients")
    if isinstance(r, str):
        tg["recipients"] = lines_to_recipients(r)
    elif r is None:
        tg["recipients"] = []
    elif isinstance(r, list):
        tg["recipients"] = [str(x).strip() for x in r if str(x).strip()]
    else:
        tg["recipients"] = []
    notes = tg.get("invite_link_notes")
    tg["invite_link_notes"] = str(notes).strip() if notes is not None else ""
    return out


def _get_verify():
    from ..api import verify_api_key
    return verify_api_key


@router.get("/config")
def get_notification_config():
    """Get current notification configuration (tokens redacted)."""
    from ..alert_notifier import AlertNotifier
    cfg = AlertNotifier.get()._config.copy()
    tg = cfg.get("telegram", {})
    if tg.get("bot_token"):
        token = tg["bot_token"]
        tg["bot_token"] = (token[:8] + "..." + token[-4:]
                           if len(token) > 12 else "***")
    return {"config": cfg}


@router.post("/config")
def set_notification_config(body: dict):
    """Update notification configuration and save to YAML."""
    import yaml
    from ..alert_notifier import AlertNotifier
    notifier = AlertNotifier.get()
    body = _normalize_notifications_payload(body or {})
    try:
        from ..api import _save_config_snapshot
        old_cfg = notifier._config.copy()
        if old_cfg:
            _save_config_snapshot("notifications", old_cfg)
    except Exception:
        pass
    notifier.configure(body)
    notif_path = config_file("notifications.yaml")
    try:
        with open(notif_path, "w", encoding="utf-8") as f:
            yaml.dump({"notifications": body}, f, allow_unicode=True,
                      default_flow_style=False)
    except Exception as e:
        logger.warning("保存通知配置失败: %s", e)
    try:
        from ..api import _audit
        _audit("configure_notifications", "",
               f"enabled={body.get('enabled')}")
    except Exception:
        pass
    return {"ok": True, "config": body}


@router.post("/test")
def test_notification(body: dict):
    """Send a test notification."""
    from ..alert_notifier import AlertNotifier
    notifier = AlertNotifier.get()
    notifier.notify("info", "system",
                    body.get("message", "OpenClaw 测试通知"))
    return {"ok": True, "message": "测试通知已发送"}


@router.post("/telegram/test")
def test_telegram(body: dict):
    """Send a test Telegram message directly."""
    from ..alert_notifier import AlertNotifier
    from ..telegram_destinations import (
        expand_telegram_notify_targets,
        has_user_telegram_destination,
    )

    import time
    notifier = AlertNotifier.get()
    cfg = notifier._config
    tg = cfg.get("telegram", {})
    token = tg.get("bot_token", "")
    if not token:
        return {"ok": False, "error": "Telegram 未配置 (需要 bot_token)"}
    if not has_user_telegram_destination(tg):
        return {"ok": False, "error": "请填写主 Chat ID 或至少一行额外接收方"}
    targets = expand_telegram_notify_targets(tg)
    msg = body.get("message", f"✅ OpenClaw Telegram 测试\n时间: {time.strftime('%H:%M:%S')}")
    errors = []
    for cid in targets:
        try:
            notifier._send_telegram(token, cid, msg)
        except Exception as e:
            errors.append(f"{cid}: {e}")
    if errors:
        return {
            "ok": False,
            "error": "; ".join(errors),
            "errors": errors,
            "sent": len(targets) - len(errors),
            "targets": targets,
        }
    return {"ok": True, "message": f"已发送到 {len(targets)} 个目标", "targets": targets}


@router.get("/telegram/status")
def telegram_status():
    """Return Telegram configuration status (token masked)."""
    from ..alert_notifier import AlertNotifier
    from ..telegram_destinations import (
        expand_telegram_notify_targets,
        has_user_telegram_destination,
    )

    cfg = AlertNotifier.get()._config
    tg = cfg.get("telegram", {})
    token = tg.get("bot_token", "")
    configured = bool(token and has_user_telegram_destination(tg))
    masked_token = (token[:8] + "..." + token[-4:]) if len(token) > 12 else ("***" if token else "")
    targets = expand_telegram_notify_targets(tg) if token and configured else []
    return {
        "enabled": cfg.get("enabled", False),
        "configured": configured,
        "bot_token_masked": masked_token,
        "chat_id": tg.get("chat_id", ""),
        "recipients": tg.get("recipients") or [],
        "invite_link_notes": tg.get("invite_link_notes") or "",
        "target_count": len(targets),
        "min_level": cfg.get("min_level", "warning"),
        "webhook_configured": bool(cfg.get("webhook", {}).get("url")),
    }


# ---------------------------------------------------------------------------
# Notify center endpoints (moved from api.py) — separate prefix "/notify"
# ---------------------------------------------------------------------------

notify_router = APIRouter(prefix="/notify", tags=["notify"])


@notify_router.get("/config")
def get_notify_config():
    from ..notification_center import load_notify_config
    config = load_notify_config()
    config.pop("history", None)
    return config


@notify_router.post("/config")
def update_notify_config(body: dict):
    from ..notification_center import load_notify_config, save_notify_config
    config = load_notify_config()
    if "webhook_url" in body:
        config["webhook_url"] = body["webhook_url"]
    if "webhook_type" in body:
        config["webhook_type"] = body["webhook_type"]
    if "enabled_events" in body:
        config["enabled_events"] = body["enabled_events"]
    if "quiet_hours" in body:
        config["quiet_hours"] = body["quiet_hours"]
    save_notify_config(config)
    return {"ok": True}


@notify_router.get("/history")
def get_notify_history():
    from ..notification_center import load_notify_config
    config = load_notify_config()
    return config.get("history", [])[-50:]


@notify_router.post("/test")
def test_notify():
    from ..notification_center import send_notification
    send_notification("test", "测试通知", "这是一条来自 OpenClaw 的测试通知", "info")
    return {"ok": True, "message": "测试通知已发送"}
