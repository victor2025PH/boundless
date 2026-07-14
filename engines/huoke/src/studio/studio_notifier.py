# -*- coding: utf-8 -*-
"""
Content Studio Telegram 通知器

职责：
  1. 生成任务完成（semi_auto）→ 推送审核通知 + 内容预览
  2. 内容发布成功 → 推送发布确认
  3. 任务失败 → 推送错误告警

通知格式：支持图片预览（sendPhoto）+ 文字（sendMessage）
凭据来源：config/notifications.yaml  →  TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / recipients[]
  投递目标合并逻辑见 src.host.telegram_destinations.expand_telegram_notify_targets

副本：环境变量 TELEGRAM_CC_CHAT_ID（未设置则默认 6107037825；设为空字符串则关闭）并入投递列表并去重。
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import config_file
from src.host.telegram_destinations import expand_telegram_notify_targets

logger = logging.getLogger(__name__)

# ── 平台 emoji 映射 ────────────────────────────────────────────
_PLAT_EMOJI = {
    "tiktok": "🎵", "instagram": "📸", "telegram": "✈️",
    "facebook": "📘", "linkedin": "💼", "twitter": "🐦",
    "whatsapp": "💬", "xiaohongshu": "📕",
}


# ─────────────────────────────────────────────────────────────────
# 凭据加载
# ─────────────────────────────────────────────────────────────────

def _load_credentials() -> Dict[str, Any]:
    """
    加载 Telegram Bot 凭据。
    优先级: 环境变量 > notifications.yaml > studio_config.yaml
    recipients 始终尝试从 notifications.yaml 合并（与主 chat 并行投递）。
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHANNEL_ID", "")
    recipients: List[str] = []

    tg_yaml: Dict[str, Any] = {}
    try:
        import yaml

        notif_path = config_file("notifications.yaml")
        with open(notif_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        tg_yaml = cfg.get("notifications", cfg).get("telegram", {}) or {}
        rec = tg_yaml.get("recipients")
        if isinstance(rec, list):
            recipients = [str(x).strip() for x in rec if str(x).strip()]
    except Exception:
        pass

    token = token or tg_yaml.get("bot_token", "") or tg_yaml.get("token", "")
    chat_id = chat_id or tg_yaml.get("chat_id", "") or tg_yaml.get("channel_id", "")

    if not (token and chat_id):
        try:
            import yaml

            studio_path = config_file("studio_config.yaml")
            with open(studio_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            tg = cfg.get("studio", {}).get("telegram_notify", {}) or {}
            token = token or tg.get("bot_token", "")
            chat_id = chat_id or tg.get("chat_id", "")
        except Exception:
            pass

    return {"token": token, "chat_id": chat_id, "recipients": recipients}


def _norm_chat_id(s: str) -> str:
    x = str(s).strip().lower()
    return x[1:] if x.startswith("@") else x


def cc_telegram_chat_id() -> Optional[str]:
    """
    副本收件人 chat_id。
    TELEGRAM_CC_CHAT_ID 未设置 → 默认 6107037825；显式设为 "" → 不发送副本。
    """
    raw = os.getenv("TELEGRAM_CC_CHAT_ID")
    if raw is not None:
        r = raw.strip()
        return r if r else None
    return "6107037825"


def telegram_broadcast_chat_ids(primary_chat_id: str) -> List[str]:
    """兼容旧接口：等同 expand（无 recipients 时仅主目标 + 环境副本）。"""
    return expand_telegram_notify_targets(
        {"chat_id": primary_chat_id, "recipients": []},
    )


def _get_dashboard_url() -> str:
    """获取 Dashboard 公开 URL（用于通知中的跳转链接）。"""
    url = os.getenv("STUDIO_DASHBOARD_URL", "")
    if url:
        return url.rstrip("/")
    try:
        import yaml
        with open(config_file("studio_config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        url = cfg.get("studio", {}).get("dashboard_url", "")
        if url:
            return url.rstrip("/")
    except Exception:
        pass
    from src.openclaw_env import local_api_base

    return local_api_base("localhost")


# ─────────────────────────────────────────────────────────────────
# 低层发送函数（同步，在线程中调用）
# ─────────────────────────────────────────────────────────────────

def _send_message(token: str, chat_id: str, text: str,
                  parse_mode: str = "HTML",
                  reply_markup: Optional[dict] = None) -> bool:
    """发送 Telegram 文字消息（HTML格式）。"""
    try:
        import urllib.request, urllib.parse
        payload = {
            "chat_id":    chat_id,
            "text":       text[:4096],
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)

        data = json.dumps(payload).encode("utf-8")
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        req  = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("Telegram sendMessage 失败: %s", e)
        return False


def _send_photo(token: str, chat_id: str, image_path: str, caption: str = "",
                parse_mode: str = "HTML",
                reply_markup: Optional[dict] = None) -> bool:
    """发送 Telegram 图片消息（附带文字说明）。"""
    try:
        import urllib.request
        import mimetypes
        import email.mime.multipart
        import email.mime.base
        import email.mime.text

        # 使用 multipart/form-data 上传图片
        boundary = "----TelegramBoundary"
        body_parts = []

        def add_field(name, value):
            body_parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )

        add_field("chat_id", chat_id)
        add_field("parse_mode", parse_mode)
        if caption:
            add_field("caption", caption[:1024])
        if reply_markup:
            add_field("reply_markup", json.dumps(reply_markup))

        # 图片文件
        with open(image_path, "rb") as f:
            img_data = f.read()
        mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
        fname = Path(image_path).name
        body_parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="{fname}"\r\nContent-Type: {mime_type}\r\n\r\n'.encode()
            + img_data + b'\r\n'
        )
        body_parts.append(f"--{boundary}--\r\n".encode())

        body = b"".join(body_parts)
        url  = f"https://api.telegram.org/bot{token}/sendPhoto"
        req  = urllib.request.Request(url, data=body, method="POST",
                                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except Exception as e:
        logger.warning("Telegram sendPhoto 失败: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────
# 消息构建
# ─────────────────────────────────────────────────────────────────

def _build_review_inline_keyboard(content_items: List[dict], dashboard_url: str) -> dict:
    """构建审核通知的内联键盘（URL按钮）。"""
    buttons = [
        [{"text": "📋 打开审核界面", "url": f"{dashboard_url}/dashboard#studio"}]
    ]
    # 若只有一条内容，直接加快速审核链接
    if len(content_items) == 1:
        cid = content_items[0].get("content_id") or content_items[0].get("id")
        if cid:
            buttons.append([
                {"text": "✅ 直接通过发布", "url": f"{dashboard_url}/dashboard#studio"},
            ])
    return {"inline_keyboard": buttons}


def _build_review_message(persona_id: str, platforms: List[str],
                           content_items: List[dict]) -> str:
    """构建半自动审核通知消息文字（HTML）。"""
    plat_str = " ".join(_PLAT_EMOJI.get(p, p) for p in platforms)
    count = len(content_items)

    lines = [
        f"🎨 <b>Content Studio — 内容待审核</b>",
        f"",
        f"📌 人设: <code>{persona_id}</code>",
        f"📱 平台: {plat_str}",
        f"📦 内容数量: {count} 条",
        f"",
    ]

    # 展示第一条内容的脚本预览
    if content_items:
        item = content_items[0]
        script  = (item.get("script") or item.get("voiceover_text") or "")[:200]
        caption = (item.get("caption") or "")[:120]
        plat    = item.get("platform", "")
        hook    = script.split("\n")[0][:80] if script else "(无脚本)"

        lines += [
            f"🔤 <b>Hook 预览</b> ({_PLAT_EMOJI.get(plat, plat)}{plat}):",
            f"<i>{_esc(hook)}</i>",
        ]
        if caption:
            lines += ["", f"💬 <b>文案</b>: {_esc(caption[:80])}…"]

        hashtags = _safe_json(item.get("hashtags", []))
        if hashtags:
            lines += ["", "🏷 " + " ".join(hashtags[:5])]

    lines += [
        f"",
        f"⏰ 请尽快审核，内容已就绪等待发布",
    ]
    return "\n".join(lines)


def _build_published_message(platform: str, persona_id: str,
                              content_item: Optional[dict] = None) -> str:
    """构建发布成功通知消息（HTML）。"""
    emoji = _PLAT_EMOJI.get(platform, "📱")
    lines = [
        f"{emoji} <b>内容已发布</b>",
        f"",
        f"平台: {platform}  |  人设: <code>{persona_id}</code>",
    ]
    if content_item:
        cap = (content_item.get("caption") or "")[:80]
        if cap:
            lines.append(f"文案: {_esc(cap)}…")
    return "\n".join(lines)


def _build_failed_message(job_id: str, persona_id: str, error: str) -> str:
    """构建任务失败告警消息（HTML）。"""
    return (
        f"❌ <b>Content Studio 任务失败</b>\n\n"
        f"任务: <code>{job_id[:12]}</code>\n"
        f"人设: <code>{persona_id}</code>\n"
        f"错误: <code>{_esc(error[:300])}</code>"
    )


# ─────────────────────────────────────────────────────────────────
# 公开接口（异步化：在后台线程发送，不阻塞主流程）
# ─────────────────────────────────────────────────────────────────

def notify_job_ready(job_id: str, persona_id: str, platforms: List[str],
                     content_items: List[dict]) -> None:
    """
    半自动模式任务完成 → 推送审核通知。

    在后台线程执行，不阻塞 StudioManager。
    若未配置 Telegram 凭据，静默忽略。
    """
    def _send():
        creds = _load_credentials()
        if not creds["token"] or not creds["chat_id"]:
            logger.debug("Telegram 未配置，跳过审核通知")
            return

        dashboard_url = _get_dashboard_url()
        text   = _build_review_message(persona_id, platforms, content_items)
        markup = _build_review_inline_keyboard(content_items, dashboard_url)

        first_image = _find_first_image(content_items)
        any_ok = False
        used_photo = False
        for cid in expand_telegram_notify_targets(
            {"chat_id": creds["chat_id"], "recipients": creds.get("recipients", [])},
        ):
            if first_image and Path(first_image).exists():
                if _send_photo(creds["token"], cid,
                               first_image, caption=text[:1024],
                               reply_markup=markup):
                    any_ok = True
                    used_photo = True
                    continue
            if _send_message(creds["token"], cid, text, reply_markup=markup):
                any_ok = True
        if any_ok:
            logger.info(
                "Studio 审核通知已发送（%s）persona=%s",
                "含图片" if used_photo else "纯文字",
                persona_id,
            )
        else:
            logger.warning("Studio 审核通知发送失败")

    threading.Thread(target=_send, daemon=True, name="studio-notify-ready").start()


def notify_published(platform: str, persona_id: str,
                     content_item: Optional[dict] = None) -> None:
    """发布成功通知（后台线程）。"""
    def _send():
        creds = _load_credentials()
        if not creds["token"] or not creds["chat_id"]:
            return
        text = _build_published_message(platform, persona_id, content_item)
        for cid in expand_telegram_notify_targets(
            {"chat_id": creds["chat_id"], "recipients": creds.get("recipients", [])},
        ):
            _send_message(creds["token"], cid, text)
        logger.info("Studio 发布通知已发送 platform=%s", platform)

    threading.Thread(target=_send, daemon=True, name="studio-notify-pub").start()


def notify_job_failed(job_id: str, persona_id: str, error: str) -> None:
    """任务失败告警（后台线程）。"""
    def _send():
        creds = _load_credentials()
        if not creds["token"] or not creds["chat_id"]:
            return
        text = _build_failed_message(job_id, persona_id, error)
        for cid in expand_telegram_notify_targets(
            {"chat_id": creds["chat_id"], "recipients": creds.get("recipients", [])},
        ):
            _send_message(creds["token"], cid, text)
        logger.warning("Studio 失败告警已发送 job_id=%s", job_id)

    threading.Thread(target=_send, daemon=True, name="studio-notify-fail").start()


# ─────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────

def _find_first_image(content_items: List[dict]) -> Optional[str]:
    """从内容列表中找第一张可用图片路径。"""
    for item in content_items:
        # image_paths 字段
        paths = _safe_json(item.get("image_paths", []))
        for p in paths:
            if p and Path(p).exists():
                return p
        # final_video_path（略，视频不适合 sendPhoto）
    return None


def _safe_json(val) -> list:
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val) or []
        except Exception:
            return []
    return []


def _esc(text: str) -> str:
    """HTML 转义。"""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))
