# -*- coding: utf-8 -*-
"""通知中心 — Webhook + 静默时段 + 历史记录。"""
import json
import time
import logging
import threading

from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_notify_config_path = config_file("notify_config.json")


def load_notify_config() -> dict:
    if _notify_config_path.exists():
        with open(_notify_config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "webhook_url": "",
        "webhook_type": "generic",  # generic, dingtalk, feishu, slack
        "enabled_events": ["device.disconnected", "task.failed", "watchdog.captcha_detected"],
        "quiet_hours": {"start": "23:00", "end": "07:00"},
        "history": [],
    }


def save_notify_config(data: dict):
    _notify_config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_notify_config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_notification(event_type: str, title: str, message: str, level: str = "warning"):
    """Send notification through configured channels."""
    config = load_notify_config()
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "type": event_type,
        "title": title,
        "message": message,
        "level": level,
        "sent": False,
    }

    if event_type not in config.get("enabled_events", []):
        entry["skipped"] = "event not enabled"
        config.setdefault("history", []).append(entry)
        if len(config["history"]) > 200:
            config["history"] = config["history"][-200:]
        save_notify_config(config)
        return

    quiet = config.get("quiet_hours", {})
    if quiet.get("start") and quiet.get("end"):
        now_hm = time.strftime("%H:%M")
        start, end = quiet["start"], quiet["end"]
        if start > end:
            in_quiet = now_hm >= start or now_hm < end
        else:
            in_quiet = start <= now_hm < end
        if in_quiet:
            entry["skipped"] = "quiet hours"
            config.setdefault("history", []).append(entry)
            if len(config["history"]) > 200:
                config["history"] = config["history"][-200:]
            save_notify_config(config)
            return

    url = config.get("webhook_url", "")
    wtype = config.get("webhook_type", "generic")

    def _do_send():
        import requests
        if not url:
            return
        try:
            if wtype == "dingtalk":
                payload = {"msgtype": "text", "text": {"content": f"[OpenClaw] {title}\n{message}"}}
            elif wtype == "feishu":
                payload = {"msg_type": "text", "content": {"text": f"[OpenClaw] {title}\n{message}"}}
            elif wtype == "slack":
                payload = {"text": f"*[OpenClaw]* {title}\n{message}"}
            else:
                payload = {"event": event_type, "title": title, "message": message,
                           "level": level, "timestamp": entry["ts"]}
            requests.post(url, json=payload, timeout=10)
            entry["sent"] = True
        except Exception as exc:
            entry["error"] = str(exc)[:100]

    t = threading.Thread(target=_do_send, daemon=True)
    t.start()

    config.setdefault("history", []).append(entry)
    if len(config["history"]) > 200:
        config["history"] = config["history"][-200:]
    save_notify_config(config)
