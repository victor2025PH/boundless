# -*- coding: utf-8 -*-
"""
告警通知外推 — 通过 Webhook 将重要告警推送到外部系统。

支持:
  - Telegram Bot API
  - 通用 Webhook (Discord / Slack / 企业微信 / 钉钉)
  - 可配置级别过滤 (只推 critical/error，或全部)
  - 去重: 同一条告警 5 分钟内不重复推送
"""

import hashlib
import html
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from src.host.alert_message_context import (
    approximate_english_message,
    resolve_device_alert_context,
)
from src.host.alert_templates import dedup_fingerprint, render_alert_pair

logger = logging.getLogger(__name__)

_DEDUP_WINDOW = 300  # 5 minutes


class AlertNotifier:
    """Webhook-based alert notification with dedup and rate limiting."""

    _instance: Optional["AlertNotifier"] = None

    def __init__(self):
        self._config: dict = {}
        self._recent_hashes: dict = {}
        self._lock = threading.Lock()

    @classmethod
    def get(cls) -> "AlertNotifier":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def configure(self, config: dict):
        """Load notification config.

        Expected format:
            notifications:
              enabled: true
              min_level: warning        # info/warning/error/critical
              device_disconnect_telegram: true   # 单机/Worker 上某台手机 adb 掉线
              worker_online_drop_telegram: true # 仅主控：某 Worker 在线台数下降
              telegram:
                bot_token: "123:ABC"
                chat_id: "-100123456"
              webhook:
                url: "https://hooks.slack.com/..."
                method: POST
        """
        self._config = config or {}
        if self._config.get("enabled"):
            logger.info("告警通知已启用 (最低级别: %s)",
                        self._config.get("min_level", "warning"))

    def device_disconnect_telegram_enabled(self) -> bool:
        """是否向 Telegram 推送「单台设备掉线」。"""
        if not self._config.get("enabled"):
            return False
        return bool(self._config.get("device_disconnect_telegram", True))

    def worker_online_drop_telegram_enabled(self) -> bool:
        """是否向 Telegram 推送「某 Worker 在线台数减少」（仅主控集群）。"""
        if not self._config.get("enabled"):
            return False
        return bool(self._config.get("worker_online_drop_telegram", True))

    def notify(
        self,
        level: str,
        device_id: str,
        message: str = "",
        *,
        alert_code: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        """Send an alert notification if configured and level matches.

        若提供 ``alert_code`` + ``params``，正文由 ``alert_templates.render_alert_pair`` 生成（中英一致）。
        """
        if not self._config.get("enabled"):
            return

        level_order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        min_level = self._config.get("min_level", "warning")
        if level_order.get(level, 0) < level_order.get(min_level, 1):
            return

        msg_zh, msg_en_override = message, None
        if alert_code:
            msg_zh, msg_en_override = render_alert_pair(alert_code, params)

        fp = dedup_fingerprint(level, device_id, alert_code, params, msg_zh)
        msg_hash = hashlib.md5(fp.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock:
            if msg_hash in self._recent_hashes:
                if now - self._recent_hashes[msg_hash] < _DEDUP_WINDOW:
                    return
            self._recent_hashes[msg_hash] = now
            stale = [k for k, v in self._recent_hashes.items()
                     if now - v > _DEDUP_WINDOW * 2]
            for k in stale:
                del self._recent_hashes[k]

        text = self._format_message(
            level, device_id, msg_zh, message_en_override=msg_en_override,
        )
        threading.Thread(target=self._send_all, args=(text,),
                         daemon=True).start()

    def notify_event(
        self,
        event_type: str,
        title: str = "",
        body: str = "",
        level: str = "warning",
        *,
        alert_code: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
    ):
        """集群/系统事件（非单设备）。可提供 alert_code+params 以使用固定中英模板。"""
        if not self._config.get("enabled"):
            return
        level_order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        min_level = self._config.get("min_level", "warning")
        if level_order.get(level, 0) < level_order.get(min_level, 1):
            return

        zh_block, en_block = title, body
        if alert_code:
            zh_block, en_block = render_alert_pair(alert_code, params)
            fp = dedup_fingerprint(
                level, f"event:{event_type}", alert_code, params, zh_block,
            )
        else:
            fp = f"{event_type}:{title}:{body}"

        msg_hash = hashlib.md5(fp.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock:
            if msg_hash in self._recent_hashes:
                if now - self._recent_hashes[msg_hash] < _DEDUP_WINDOW:
                    return
            self._recent_hashes[msg_hash] = now
        icons = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}
        icon = icons.get(level, "📢")
        ts = time.strftime("%m/%d %H:%M")
        et_esc = html.escape(event_type)
        if alert_code:
            zh_esc = html.escape(zh_block)
            en_esc = html.escape(en_block)
            text = (
                f"{icon} <b>OpenClaw</b> · <code>{et_esc}</code>\n{zh_esc}\n<i>{ts}</i>\n"
                f"────────\n{en_esc}\n<i>{ts}</i>"
            )
        else:
            title_esc = html.escape(title)
            body_esc = html.escape(body)
            title_en = approximate_english_message(title)
            body_en = approximate_english_message(body)
            title_en_esc = html.escape(title_en) if title_en else title_esc
            body_en_esc = html.escape(body_en) if body_en else body_esc
            text = (
                f"{icon} <b>{title_esc}</b>\n{body_esc}\n<i>{ts}</i>\n"
                f"────────\n"
                f"{icon} <b>{title_en_esc}</b>\n{body_en_esc}\n<i>{ts}</i>"
            )
        threading.Thread(target=self._send_all, args=(text,), daemon=True).start()

    def notify_daily_report(self, date_str: str, totals: dict, leads: dict,
                             ai_summary: str = "",
                             extra_sections: Optional[List[str]] = None):
        """Push daily report summary to Telegram."""
        if not self._config.get("enabled"):
            return
        follows = totals.get("follows", 0)
        dms = totals.get("dms_sent", 0)
        new_leads = leads.get("new_leads", 0)
        converted = leads.get("converted", 0)
        lines = [
            f"📊 <b>运营日报 {date_str}</b>",
            f"关注: <b>{follows}</b>  私信: <b>{dms}</b>",
            f"新线索: <b>{new_leads}</b>  转化: <b>{converted}</b>",
        ]
        if ai_summary:
            lines.append(f"\n💡 {html.escape(ai_summary[:200])}")
        # W1: 附加段落 (top leads / score digest / auto-lost 等)
        if extra_sections:
            for sec in extra_sections:
                if sec and sec.strip():
                    lines.append(sec)
        lines.append(
            "────────\n"
            f"📊 <b>Daily ops {html.escape(date_str)}</b>\n"
            f"Follows: <b>{follows}</b>  DMs: <b>{dms}</b>\n"
            f"New leads: <b>{new_leads}</b>  Converted: <b>{converted}</b>"
        )
        text = "\n".join(lines)
        threading.Thread(target=self._send_all, args=(text,), daemon=True).start()

    def _format_message(
        self,
        level: str,
        device_id: str,
        message: str,
        *,
        message_en_override: Optional[str] = None,
    ) -> str:
        icons = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🚨"}
        icon = icons.get(level, "📢")
        lv = level.upper()
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        safe_msg = html.escape(message)
        if message_en_override is not None:
            msg_en = message_en_override
        else:
            msg_en = approximate_english_message(message)
        safe_msg_en = html.escape(msg_en) if msg_en else safe_msg

        if not device_id or device_id == "system":
            return (
                f"{icon} <b>OpenClaw 告警</b> / <b>OpenClaw Alert</b>\n"
                f"范围 / Scope: <code>system</code>\n"
                f"级别 / Level: <b>{lv}</b>\n"
                f"内容 / Message:\n{safe_msg}\n"
                f"────────\n"
                f"{safe_msg_en}\n"
                f"时间 / Time: {ts}"
            )

        ctx = resolve_device_alert_context(device_id)
        did_esc = html.escape(ctx["device_id"])
        phone_esc = html.escape(str(ctx["phone_label"]))
        slot_esc = html.escape(str(ctx["phone_number"]))
        host_cn_esc = html.escape(str(ctx["host_pc_cn"]))
        host_en_esc = html.escape(str(ctx["host_pc_en"]))
        node_esc = html.escape(str(ctx["service_node"]))
        link_cn_esc = html.escape(str(ctx["link_cn"]))
        link_en_esc = html.escape(str(ctx["link_en"]))

        return (
            f"{icon} <b>OpenClaw 告警</b>\n"
            f"📱 手机编号: <b>{phone_esc}</b> · 槽位 <code>{slot_esc}</code>\n"
            f"🖥 所在电脑: {host_cn_esc}\n"
            f"📡 告警节点: <code>{node_esc}</code>\n"
            f"🔌 连接: {link_cn_esc} · <code>{did_esc}</code>\n"
            f"级别: <b>{lv}</b>\n"
            f"内容: {safe_msg}\n"
            f"时间: {ts}\n"
            f"────────\n"
            f"{icon} <b>OpenClaw Alert</b>\n"
            f"📱 Phone #: <b>{phone_esc}</b> · slot <code>{slot_esc}</code>\n"
            f"🖥 Host PC: {host_en_esc}\n"
            f"📡 Alert node: <code>{node_esc}</code>\n"
            f"🔌 Link: {link_en_esc} · <code>{did_esc}</code>\n"
            f"Level: <b>{lv}</b>\n"
            f"Details: {safe_msg_en}\n"
            f"Time: {ts}"
        )

    def _send_all(self, text: str):
        tg = self._config.get("telegram", {})
        if tg.get("bot_token") and tg.get("chat_id"):
            from src.host.telegram_destinations import expand_telegram_notify_targets
            for cid in expand_telegram_notify_targets(tg):
                self._send_telegram(tg["bot_token"], cid, text)

        wh = self._config.get("webhook", {})
        if wh.get("url"):
            self._send_webhook(wh["url"], text, wh.get("method", "POST"))

    def _send_telegram(self, token: str, chat_id: str, text: str):
        try:
            import urllib.request
            import json
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": text,
                               "parse_mode": "HTML"}).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    logger.debug("Telegram 告警已发送")
        except Exception as e:
            logger.debug("Telegram 告警发送失败: %s", e)

    def _send_webhook(self, url: str, text: str, method: str = "POST"):
        try:
            import urllib.request
            import json
            data = json.dumps({"text": text, "content": text,
                               "msg_type": "text",
                               "msgtype": "text"}).encode()
            req = urllib.request.Request(url, data=data, method=method,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.debug("Webhook 告警已发送: %d", resp.status)
        except Exception as e:
            logger.debug("Webhook 告警发送失败: %s", e)


# 兼容 strategy_optimizer / tiktok_escalation 等旧引用
get_alert_notifier = AlertNotifier.get
