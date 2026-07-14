# -*- coding: utf-8 -*-
"""TikTok AI 聊天消息生成器。

基于上下文生成自然、多样、像真人的意大利语消息。
支持视觉分析（截图）和纯文本模式。
"""

import json
import logging
import random
import time
from pathlib import Path
from typing import Optional, List, Dict

from src.host.device_registry import data_file

log = logging.getLogger(__name__)

_CHAT_HISTORY_FILE = data_file("chat_history.json")
_FOLLOW_LOG_FILE = data_file("follow_log.json")


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ── 聊天历史管理 ──

def is_already_chatted(username: str) -> bool:
    """检查是否已给该用户发过消息。"""
    history = _load_json(_CHAT_HISTORY_FILE)
    return username in history.get("chatted_users", {})


def record_chat(username: str, device_id: str, message: str, success: bool = True):
    """记录聊天历史。"""
    history = _load_json(_CHAT_HISTORY_FILE)
    history.setdefault("chatted_users", {})
    history["chatted_users"][username] = {
        "device_id": device_id,
        "message": message[:200],
        "success": success,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_json(_CHAT_HISTORY_FILE, history)


# ── 关注记录管理 ──

def is_already_followed(seed: str, scroll: int, row: int, device_id: str = "") -> bool:
    """检查该位置是否已关注过（基于种子+滚动+行号）。"""
    log_data = _load_json(_FOLLOW_LOG_FILE)
    key = f"{seed}:s{scroll}r{row}"
    followed = log_data.get("followed_positions", {})
    return key in followed


def record_follow(seed: str, scroll: int, row: int, device_id: str):
    """记录关注位置。"""
    log_data = _load_json(_FOLLOW_LOG_FILE)
    log_data.setdefault("followed_positions", {})
    log_data.setdefault("follow_count", {})
    key = f"{seed}:s{scroll}r{row}"
    log_data["followed_positions"][key] = {
        "device_id": device_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 设备关注计数
    count = log_data["follow_count"].get(device_id, 0)
    log_data["follow_count"][device_id] = count + 1
    _save_json(_FOLLOW_LOG_FILE, log_data)


def get_follow_count(device_id: str) -> int:
    """获取设备今日关注总数。"""
    log_data = _load_json(_FOLLOW_LOG_FILE)
    return log_data.get("follow_count", {}).get(device_id, 0)


# ── AI 消息生成 ──

def generate_natural_message(context: str = "greeting",
                              username: str = "",
                              language: str = "italian",
                              referral_info: str = "") -> str:
    """用 AI 生成自然的聊天消息。

    Args:
        context: "greeting"(问候) / "referral"(引流) / "followup"(跟进)
        username: 对方用户名（用于个性化）
        language: 目标语言
        referral_info: 引流信息（如 Telegram: @xxx）

    Returns:
        自然的消息文本
    """
    try:
        # 直接调用智谱 API（不依赖 llm_client 的配置链路，避免 Worker 上 key 为空的问题）
        import urllib.request as _ur

        _ZHIPU_KEY = "${GLM_API_KEY}"
        _ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

        if context == "greeting":
            prompt = f"""You are a real Italian person on TikTok. Write ONE very short casual DM greeting in Italian.
Rules:
- Maximum 6 words
- Sound natural and human, NOT like a bot or template
- You can use 1 emoji max
- Do NOT mention "follow" or "follow back"
- Just be friendly and casual
- No quotes around the message
Examples of good messages: Ciao! Come stai?, Ehi! Bel profilo, Ciao tutto bene?"""

        elif context == "referral":
            prompt = f"""You are an Italian TikTok user who wants to continue a conversation on Telegram.
Write ONE short casual Italian message mentioning your {referral_info}.
Rules:
- Maximum 10 words + the contact info
- Sound natural, like you're sharing your contact casually
- Include {referral_info} naturally in the text
- No quotes
Example: Scrivimi su Telegram se vuoi, {referral_info}"""

        elif context == "followup":
            prompt = f"""You are an Italian person following up on TikTok. Write ONE short casual Italian follow-up message.
Rules:
- Maximum 8 words
- Sound natural and curious
- Ask about something or share interest
- No quotes"""

        else:
            prompt = "Write one short casual Italian greeting for TikTok DM. Max 6 words."

        # 直接调 API（绕过 Worker 上 llm_client 配置问题）
        import json as _j
        _body = _j.dumps({
            "model": "glm-4-flash",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 30,
            "temperature": 0.95,
        }).encode()
        _req = _ur.Request(_ZHIPU_URL, data=_body,
                           headers={"Content-Type": "application/json",
                                    "Authorization": f"Bearer {_ZHIPU_KEY}"})
        _resp = _ur.urlopen(_req, timeout=15)
        _r = _j.loads(_resp.read().decode())
        response = _r["choices"][0]["message"]["content"]
        msg = response.strip().strip('"').strip("'")

        # 安全检查：消息不能太长或包含敏感词
        if len(msg) > 100:
            msg = msg[:100]
        if not msg or len(msg) < 2:
            msg = _fallback_message(context, referral_info)

        return msg

    except Exception as e:
        log.debug("[AI Chat] LLM failed: %s, using fallback", e)
        return _fallback_message(context, referral_info)


def generate_message_from_screenshot(device_id: str, dm, context: str = "greeting",
                                      referral_info: str = "") -> str:
    """截图当前页面 → AI 视觉分析 → 生成基于内容的自然回复。

    用于在用户 Profile 页或视频页截图后，生成相关的个性化消息。
    """
    import urllib.request as _ur
    import json as _j
    import base64 as _b64

    _ZHIPU_KEY = "${GLM_API_KEY}"
    _ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    try:
        import subprocess as _sp, io as _io

        # 1. 截图
        dm.execute_adb_command("shell screencap -p /sdcard/openclaw_ai_sc.png", device_id)

        # 2. 读取完整截图 → PIL JPEG 压缩（解决旧版本只读前20KB导致AI看不到画面的问题）
        _pull = _sp.run(
            f"adb -s {device_id} exec-out cat /sdcard/openclaw_ai_sc.png",
            shell=True, capture_output=True, timeout=10)
        if _pull.returncode != 0 or not _pull.stdout:
            return generate_natural_message(context, referral_info=referral_info)

        _img_bytes = _pull.stdout
        _img_mime = "image/png"
        try:
            from PIL import Image as _PILImg
            _pimg = _PILImg.open(_io.BytesIO(_img_bytes))
            _pw, _ph = _pimg.size
            _psmall = _pimg.resize((_pw * 2 // 3, _ph * 2 // 3))
            if _psmall.mode in ("RGBA", "P", "LA"):
                _psmall = _psmall.convert("RGB")
            _pbuf = _io.BytesIO()
            _psmall.save(_pbuf, format="JPEG", quality=80, optimize=True)
            _img_bytes = _pbuf.getvalue()
            _img_mime = "image/jpeg"
        except Exception:
            pass  # PIL 失败则使用原始 PNG

        b64_data = _b64.b64encode(_img_bytes).decode()

        # 3. 发给 AI 视觉分析
        if context == "greeting":
            text_prompt = ("This is a TikTok user's profile. Write ONE very short, natural Italian DM "
                          "greeting (max 6 words) that feels personal based on what you see. "
                          "No quotes, no explanation. Just the message.")
        elif context == "referral":
            text_prompt = (f"This is a TikTok user's profile. Write ONE short Italian message "
                          f"naturally mentioning {referral_info}. Max 12 words. No quotes.")
        elif context == "comment":
            text_prompt = ("This is a TikTok video screenshot. Write ONE very short Italian comment "
                          "(3-6 words) that a real person would write about this video. "
                          "Be specific to what you see. Use casual Italian. "
                          "You can use 1 emoji. No quotes, no explanation. Just the comment.")
        else:
            text_prompt = ("Write ONE very short casual Italian message (max 6 words) "
                          "based on this TikTok screenshot. No quotes.")

        body = _j.dumps({
            "model": "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{_img_mime};base64,{b64_data.strip()}"}},
            ]}],
            "max_tokens": 30,
            "temperature": 0.9,
        }).encode()
        req = _ur.Request(_ZHIPU_URL, data=body,
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Bearer {_ZHIPU_KEY}"})
        resp = _ur.urlopen(req, timeout=20)
        r = _j.loads(resp.read().decode())
        msg = r["choices"][0]["message"]["content"].strip().strip('"').strip("'")
        if msg and 2 < len(msg) < 80:
            log.info("[AI Vision] Generated: %s", msg[:60])
            return msg
    except Exception as e:
        log.debug("[AI Vision] Failed: %s", e)

    return generate_natural_message(context, referral_info=referral_info)


def generate_message_with_username(device_id: str, dm, context: str = "greeting",
                                   referral_info: str = "") -> tuple:
    """P6-A: 单次 Vision AI 调用同时提取用户名 + 生成消息，避免双倍 API 开销。

    返回 (username, message)。username 格式 "@xxx"，无法提取时为空字符串。
    任何异常时 fallback 到 generate_message_from_screenshot。
    """
    import urllib.request as _ur
    import json as _j
    import base64 as _b64

    _ZHIPU_KEY = "${GLM_API_KEY}"
    _ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    try:
        import subprocess as _sp, io as _io

        dm.execute_adb_command("shell screencap -p /sdcard/openclaw_ai_sc.png", device_id)
        _pull = _sp.run(
            f"adb -s {device_id} exec-out cat /sdcard/openclaw_ai_sc.png",
            shell=True, capture_output=True, timeout=10)
        if _pull.returncode != 0 or not _pull.stdout:
            return "", generate_message_from_screenshot(device_id, dm, context, referral_info)

        _img_bytes = _pull.stdout
        _img_mime = "image/png"
        try:
            from PIL import Image as _PILImg
            _pimg = _PILImg.open(_io.BytesIO(_img_bytes))
            _pw, _ph = _pimg.size
            _psmall = _pimg.resize((_pw * 2 // 3, _ph * 2 // 3))
            if _psmall.mode in ("RGBA", "P", "LA"):
                _psmall = _psmall.convert("RGB")
            _pbuf = _io.BytesIO()
            _psmall.save(_pbuf, format="JPEG", quality=80, optimize=True)
            _img_bytes = _pbuf.getvalue()
            _img_mime = "image/jpeg"
        except Exception:
            pass

        b64_data = _b64.b64encode(_img_bytes).decode()

        text_prompt = (
            "This is a TikTok DM/chat page. "
            "1) Find the @username shown in the header at the top (e.g. '@mario_rossi'). "
            "2) Write ONE very short natural Italian DM greeting (max 6 words, casual, no quotes). "
            "Return ONLY valid JSON with exactly these keys: "
            "{\"u\":\"@username_or_empty\",\"m\":\"greeting\"} "
            "No markdown, no explanation, just the JSON."
        )

        body = _j.dumps({
            "model": "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{_img_mime};base64,{b64_data.strip()}"}},
            ]}],
            "max_tokens": 60,
            "temperature": 0.7,
        }).encode()
        req = _ur.Request(_ZHIPU_URL, data=body,
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Bearer {_ZHIPU_KEY}"})
        resp = _ur.urlopen(req, timeout=20)
        r = _j.loads(resp.read().decode())
        raw = r["choices"][0]["message"]["content"].strip()

        # 清理 markdown code fence（GLM 有时会包一层）
        if "```" in raw:
            parts = raw.split("```")
            for part in parts:
                stripped = part.strip().lstrip("json").strip()
                if stripped.startswith("{"):
                    raw = stripped
                    break

        data = _j.loads(raw.strip())
        raw_uname = str(data.get("u", "")).strip()
        # 清理用户名格式
        if raw_uname and raw_uname not in ("@username_or_empty", "@", ""):
            if not raw_uname.startswith("@"):
                raw_uname = "@" + raw_uname
        else:
            raw_uname = ""

        msg = str(data.get("m", "")).strip().strip('"').strip("'")
        if msg and 2 < len(msg) < 80:
            log.info("[AI Vision+名] username=%s msg=%s", raw_uname or "(unknown)", msg[:60])
            return raw_uname, msg

    except Exception as e:
        log.debug("[AI Vision+名] 失败: %s，回退到标准截图消息生成", e)

    return "", generate_message_from_screenshot(device_id, dm, context, referral_info)


def generate_personalized_dm_from_profile(device_id: str, dm,
                                           target_languages: list = None,
                                           referral_info: str = "") -> tuple:
    """P2-1: 在用户主页截图，AI分析其内容主题，生成个性化开场DM。

    比普通问候效果好 2-3x——DM中引用了用户的内容，让用户觉得被真实关注。
    返回 (username, personalized_message)。
    """
    import urllib.request as _ur
    import json as _j
    import base64 as _b64
    import subprocess as _sp
    import io as _io

    _ZHIPU_KEY = "${GLM_API_KEY}"
    _ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    # 确定目标语言（用于生成相应语言的DM）
    _lang_instruction = ""
    if target_languages:
        _lang_map = {
            "tl": "Tagalog (Filipino)", "id": "Indonesian (Bahasa Indonesia)",
            "ms": "Malay", "ar": "Arabic", "pt": "Portuguese",
            "hi": "Hindi", "es": "Spanish", "fr": "French",
            "de": "German", "it": "Italian", "en": "English",
        }
        for lc in target_languages:
            lang_name = _lang_map.get(lc[:2].lower(), "")
            if lang_name:
                _lang_instruction = f" Write the DM in {lang_name}."
                break

    try:
        dm.execute_adb_command("shell screencap -p /sdcard/openclaw_profile_sc.png", device_id)
        _pull = _sp.run(
            f"adb -s {device_id} exec-out cat /sdcard/openclaw_profile_sc.png",
            shell=True, capture_output=True, timeout=10)
        if _pull.returncode != 0 or not _pull.stdout:
            return "", generate_message_from_screenshot(device_id, dm, "greeting", referral_info)

        _img_bytes = _pull.stdout
        _img_mime = "image/png"
        try:
            from PIL import Image as _PILImg
            _pimg = _PILImg.open(_io.BytesIO(_img_bytes))
            _pw, _ph = _pimg.size
            _psmall = _pimg.resize((_pw * 2 // 3, _ph * 2 // 3))
            if _psmall.mode in ("RGBA", "P", "LA"):
                _psmall = _psmall.convert("RGB")
            _pbuf = _io.BytesIO()
            _psmall.save(_pbuf, format="JPEG", quality=80, optimize=True)
            _img_bytes = _pbuf.getvalue()
            _img_mime = "image/jpeg"
        except Exception:
            pass

        b64_data = _b64.b64encode(_img_bytes).decode()

        text_prompt = (
            "This is a TikTok user profile page. "
            "1) Find the @username shown (e.g. '@maria_rossi'). "
            "2) Look at their videos/bio and identify what topic they post about in 2-3 words. "
            "3) Write ONE short natural DM greeting (max 12 words) that mentions their content topic — "
            "make it feel like you genuinely watched their content, not salesy." +
            _lang_instruction +
            " Return ONLY valid JSON: "
            "{\"u\":\"@username_or_empty\",\"topic\":\"their_content_topic\",\"m\":\"personalized_greeting\"} "
            "No markdown, no explanation."
        )

        body = _j.dumps({
            "model": "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {"url": f"data:{_img_mime};base64,{b64_data.strip()}"}},
            ]}],
            "max_tokens": 80,
            "temperature": 0.8,
        }).encode()

        req = _ur.Request(_ZHIPU_URL, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {_ZHIPU_KEY}",
        })
        with _ur.urlopen(req, timeout=12) as resp:
            raw = _j.loads(resp.read())

        content = (raw.get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = _j.loads(content)
        username = str(parsed.get("u", "")).strip()
        message = str(parsed.get("m", "")).strip()
        topic = str(parsed.get("topic", "")).strip()

        if message:
            import logging as _log
            _log.getLogger(__name__).info(
                "[个性化DM] @%s 话题:'%s' 消息:'%s'", username, topic, message[:40])
            return username, message

    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).debug("[个性化DM] 失败，降级: %s", e)

    # 降级到普通消息生成
    return generate_message_with_username(device_id, dm, "greeting", referral_info)


def _fallback_message(context: str, referral_info: str = "") -> str:
    """AI 不可用时的 fallback 消息（多样化随机选择）。"""
    greetings = [
        "Ciao!", "Hey! " + random.choice(["👋", "😊", "✌️"]),
        "Ciao, come va?", "Ehi! Bel profilo",
        "Hey!", "Ciao " + random.choice(["😊", "🙂", "👋"]),
        "Come stai?", "Tutto bene?",
    ]
    referrals = [
        f"Scrivimi: {referral_info}",
        f"Ti aspetto su {referral_info}",
        f"Parliamo su {referral_info}",
    ]
    followups = [
        "Come va?", "Novita?", "Che fai di bello?",
        random.choice(["😊", "👋", "🙂"]),
    ]

    if context == "greeting":
        return random.choice(greetings)
    elif context == "referral" and referral_info:
        return random.choice(referrals)
    elif context == "followup":
        return random.choice(followups)
    return random.choice(greetings)
