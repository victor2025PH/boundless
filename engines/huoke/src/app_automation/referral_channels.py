# -*- coding: utf-8 -*-
"""多渠道引流抽象层 (2026-04-23, A 机 Phase 4)。

动机
----
旧架构 ``fb_referral_contact`` + ``get_referral_snippet`` 散装处理各渠道,
LINE / WhatsApp / Telegram / Messenger / Instagram 5 个渠道的**差异化能力**
(账号格式、deep link 拼接、意图识别词) 靠 if-elif 散落在各处。

新架构把每个渠道抽象成 ``ReferralChannel`` 子类, 注册到中央 registry:

    REFERRAL_REGISTRY["line"] = LineChannel()
    REFERRAL_REGISTRY["whatsapp"] = WhatsAppChannel()
    ...

调用方只需:

    from src.app_automation.referral_channels import pick_channel_smart
    channel, value = pick_channel_smart(
        incoming_text=对方消息,
        persona_key="jp_female_midlife",
        available_accounts=parse_referral_channels(raw),
    )
    if channel:
        snippet = channel.format_snippet(value, persona_key=pk, name="花子")
        # 走 B 的 send 链路发出

三段式选渠道策略
----------------
1. **意图感知**(detect_intent): 对方消息里问"LINE 有吗?" → 置信度高 → 直接回 LINE
2. **persona 优先级**(referral_priority): 对方没问 → 按 persona 偏好的渠道排序
3. **默认**: 任意可用渠道兜底

核心原则
--------
* **向后兼容**: ``fb_referral_contact.parse_referral_channels`` /
  ``pick_referral_for_persona`` 保留不动, B 机现有代码零改动仍能跑
* **渠道特化**: LINE 默认不做 deep link(FB 风控敏感), WA/TG 用 wa.me/t.me 短链接
* **纯函数**: 所有 channel 方法纯函数, 不持有设备/网络状态, 易单测
"""
from __future__ import annotations

import logging
import re
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ─── 基础抽象 ────────────────────────────────────────────────────────

class ReferralChannel:
    """引流渠道抽象基类。子类覆盖 channel_key / display_name / 验证 /
    deep_link / 意图词等策略。"""

    channel_key: str = ""          # "line" / "whatsapp" / ... (小写)
    display_name: str = ""         # "LINE" / "WhatsApp" / ...
    use_deep_link_in_snippet: bool = False  # 发送时是否在文案里拼 deep link
    # 对方消息里能触发本渠道的关键词(多语);任一命中即 intent_score>=0.85
    intent_keywords: Tuple[str, ...] = ()

    def validate_account(self, raw_value: str) -> Tuple[bool, str]:
        """验证并规范化 raw 账号; 返回 (ok, cleaned_value)。

        默认实现: 只 strip, 不校验格式(允许任意字符串)。
        子类覆盖做严格校验。
        """
        v = (raw_value or "").strip()
        return bool(v), v

    def build_deep_link(self, value: str) -> Optional[str]:
        """拼接可点击跳转链接; 不支持则返回 None。

        例:
            * WhatsApp: +8190... → https://wa.me/8190...
            * Telegram: @foo     → https://t.me/foo
            * LINE: @x           → None (FB 屏蔽 line.me 链接)
        """
        return None

    def format_snippet(self, value: str,
                       persona_key: Optional[str] = None,
                       name: str = "") -> str:
        """生成发送用的文案。默认调 fb_content_assets.get_referral_snippet
        的本地化池; 子类可覆盖加 deep link 或特化。"""
        try:
            from .fb_content_assets import get_referral_snippet
            snippet = get_referral_snippet(self.channel_key, value,
                                           persona_key=persona_key)
        except Exception as e:
            logger.debug("[referral] get_referral_snippet 失败: %s", e)
            snippet = f"{self.display_name}: {value}"
        if self.use_deep_link_in_snippet:
            dl = self.build_deep_link(value)
            if dl and dl not in snippet:
                snippet = f"{snippet}\n{dl}"
        return snippet

    def detect_intent(self, incoming_text: str) -> float:
        """返回对方消息对本渠道的兴趣置信度 0-1。

        默认实现: 对 ``intent_keywords`` 做 word-boundary 匹配,
        命中任一返回 0.9, 否则 0.0。子类可覆盖做更复杂判断。
        """
        if not incoming_text or not self.intent_keywords:
            return 0.0
        text_low = incoming_text.lower()
        for kw in self.intent_keywords:
            if not kw:
                continue
            # 非字母渠道(日文/韩文)直接 substring; 字母渠道 word-boundary
            kw_low = kw.lower()
            if any(ord(ch) > 127 for ch in kw):
                if kw_low in text_low:
                    return 0.9
            else:
                # \b 对下划线/中英文混排不够稳,用前后非字母数字检测
                pattern = r"(?<![A-Za-z0-9])" + re.escape(kw_low) + r"(?![A-Za-z0-9])"
                if re.search(pattern, text_low):
                    return 0.9
        return 0.0

    def event_meta(self, value: str) -> Dict[str, object]:
        """返回给 fb_contact_events.meta_json 的额外字段。

        默认含:
          * channel: 渠道 key
          * account_masked: 脱敏账号(日志/审计用)
        """
        return {
            "channel": self.channel_key,
            "account_masked": self.mask(value),
        }

    def mask(self, value: str) -> str:
        """脱敏账号, 留首末各 2 字符 + 中间 *。

        用于 log / event 审计, 避免 raw 账号被 dump 到文件里。
        """
        v = (value or "").strip()
        if len(v) <= 4:
            return "*" * len(v)
        return v[:2] + "*" * (len(v) - 4) + v[-2:]


# ─── 具体渠道实现 ────────────────────────────────────────────────────

class LineChannel(ReferralChannel):
    """LINE 引流。账号形式:
      * ``@username``  (加好友 ID)
      * ``username``   (纯字符)
      * ``https://line.me/ti/p/xxx``  (深链, FB 会屏蔽)
      * ``https://qr-server.line-apps.com/...``  (QR URL)

    **重要**: FB 风控对 ``line.me`` 链接敏感, 发 @id 文本成功率高于发链接。
    因此 ``use_deep_link_in_snippet=False``。
    """
    channel_key = "line"
    display_name = "LINE"
    use_deep_link_in_snippet = False
    intent_keywords = (
        "line", "LINE", "ライン", "라인",   # 英/日/韩
        "라인",                              # 韩文
        "@line", "line id", "lineid",
    )

    _LINE_ID_RE = re.compile(r"^@?[A-Za-z0-9][\w.\-]{2,19}$")

    def validate_account(self, raw_value: str) -> Tuple[bool, str]:
        v = (raw_value or "").strip()
        if not v:
            return False, ""
        # URL 形式: line.me / qr
        if v.startswith(("http://", "https://")):
            if "line" in v.lower() or "qr" in v.lower():
                return True, v
            return False, v
        # 纯 ID 形式: 3-20 字符, 字母数字/下划线/点/连字符
        cleaned = v if v.startswith("@") else "@" + v
        return bool(self._LINE_ID_RE.match(cleaned)), cleaned

    def build_deep_link(self, value: str) -> Optional[str]:
        """LINE 有深链但 FB 常屏蔽, 返回 None 走纯文本 @ID。

        如果运营确实想发深链, 可在 value 里直接传 ``https://line.me/ti/p/xxx``,
        validate_account 会识别并原样保留。
        """
        return None


class WhatsAppChannel(ReferralChannel):
    """WhatsApp 引流。账号形式:
      * ``+8190...``  (国际号码)
      * ``8190...``   (不带 +)
      * ``wa.me/8190...`` / ``https://wa.me/...``

    使用 ``wa.me`` 短链接, FB 消息里点击可直接跳转 WhatsApp 启动对话。
    """
    channel_key = "whatsapp"
    display_name = "WhatsApp"
    use_deep_link_in_snippet = True
    intent_keywords = (
        "whatsapp", "WhatsApp", "WA",
        "ワッツアップ", "ワッツ", "ﾜｯﾂ",
        "واتساب", "وتساب",   # 阿拉伯
        "wa.me",
    )

    _WA_DIGITS_RE = re.compile(r"^\+?[1-9]\d{6,14}$")

    def validate_account(self, raw_value: str) -> Tuple[bool, str]:
        v = (raw_value or "").strip()
        if not v:
            return False, ""
        # wa.me 链接保留原串 (格式: https://wa.me/<digits>?text=...)
        if "wa.me" in v.lower():
            return True, v
        # 纯号码: 去掉空格/连字符/括号
        digits = re.sub(r"[\s\-()（）]", "", v)
        if self._WA_DIGITS_RE.match(digits):
            # 统一格式: 保留 +
            return True, digits if digits.startswith("+") else "+" + digits
        return False, v

    def build_deep_link(self, value: str) -> Optional[str]:
        v = value.strip()
        if v.startswith("http"):
            return v
        # 去 + 给 wa.me
        digits = v.lstrip("+").replace("-", "").replace(" ", "")
        if digits and digits.isdigit():
            return f"https://wa.me/{digits}"
        return None


class TelegramChannel(ReferralChannel):
    """Telegram 引流。
      * ``@username``
      * ``username``
      * ``https://t.me/username`` / ``t.me/username``
      * ``+8190...``  (手机号也可)
    """
    channel_key = "telegram"
    display_name = "Telegram"
    use_deep_link_in_snippet = True
    intent_keywords = (
        "telegram", "Telegram", "TG", "tg",
        "テレグラム", "텔레그램",
        "t.me",
    )

    _TG_USER_RE = re.compile(r"^@?[A-Za-z][\w]{4,31}$")

    def validate_account(self, raw_value: str) -> Tuple[bool, str]:
        v = (raw_value or "").strip()
        if not v:
            return False, ""
        if "t.me" in v.lower():
            return True, v
        # 手机号
        digits = re.sub(r"[\s\-()（）]", "", v)
        if digits.startswith("+") and digits[1:].isdigit() and len(digits) >= 8:
            return True, digits
        # username
        cleaned = v if v.startswith("@") else "@" + v
        return bool(self._TG_USER_RE.match(cleaned)), cleaned

    def build_deep_link(self, value: str) -> Optional[str]:
        v = value.strip()
        if v.startswith("http"):
            return v
        if "t.me" in v:
            return "https://" + v if not v.startswith("http") else v
        if v.startswith("@"):
            return f"https://t.me/{v[1:]}"
        if v.startswith("+") and v[1:].isdigit():
            return f"https://t.me/{v[1:]}"
        return f"https://t.me/{v}"


class MessengerChannel(ReferralChannel):
    """Messenger 引流。大部分场景用户已经在 Messenger 里 DM 我们了,
    这个渠道主要用于**跨账号引流**(换号或商家主页)。
      * ``m.me/username`` / ``https://m.me/xxx``
      * ``username``
    """
    channel_key = "messenger"
    display_name = "Messenger"
    use_deep_link_in_snippet = True
    intent_keywords = (
        "messenger", "Messenger",
        "メッセンジャー",
        "m.me",
    )

    def validate_account(self, raw_value: str) -> Tuple[bool, str]:
        v = (raw_value or "").strip()
        if not v:
            return False, ""
        if "m.me" in v.lower() or "messenger.com" in v.lower():
            return True, v
        # 纯 username
        if re.match(r"^[A-Za-z][\w.]{1,49}$", v):
            return True, v
        return False, v

    def build_deep_link(self, value: str) -> Optional[str]:
        v = value.strip()
        if v.startswith("http"):
            return v
        if "m.me" in v:
            return "https://" + v if not v.startswith("http") else v
        return f"https://m.me/{v}"


class InstagramChannel(ReferralChannel):
    """Instagram 引流。
      * ``@username``
      * ``instagram.com/username``
    """
    channel_key = "instagram"
    display_name = "Instagram"
    use_deep_link_in_snippet = True
    intent_keywords = (
        "instagram", "Instagram", "IG", "ig", "insta",
        "インスタ", "인스타",
    )

    def validate_account(self, raw_value: str) -> Tuple[bool, str]:
        v = (raw_value or "").strip()
        if not v:
            return False, ""
        if "instagram.com" in v.lower():
            return True, v
        cleaned = v if v.startswith("@") else "@" + v
        if re.match(r"^@[A-Za-z][\w.]{0,29}$", cleaned):
            return True, cleaned
        return False, v

    def build_deep_link(self, value: str) -> Optional[str]:
        v = value.strip().lstrip("@")
        if v.startswith("http"):
            return v
        if "instagram.com" in v:
            return "https://" + v if not v.startswith("http") else v
        return f"https://instagram.com/{v}"


# ─── 注册表 ──────────────────────────────────────────────────────────

# ⚠ 新增渠道: 1) 写子类 2) 在 REFERRAL_REGISTRY 里 register
#         3) fb_target_personas.yaml 里允许在 referral_priority 里用这个 key
#         4) chat_messages.yaml 的 referral_<key>_templates 放文案模板
REFERRAL_REGISTRY: Dict[str, ReferralChannel] = {
    "line": LineChannel(),
    "whatsapp": WhatsAppChannel(),
    "telegram": TelegramChannel(),
    "messenger": MessengerChannel(),
    "instagram": InstagramChannel(),
}


def register_channel(channel: ReferralChannel) -> None:
    """允许运行时注册第三方渠道(比如 B 未来加 KakaoTalk)。"""
    if not channel.channel_key:
        raise ValueError("ReferralChannel.channel_key 不能为空")
    REFERRAL_REGISTRY[channel.channel_key] = channel


def get_channel(channel_key: str) -> Optional[ReferralChannel]:
    """按 key 取渠道对象; 未知 key 返回 None。"""
    if not channel_key:
        return None
    return REFERRAL_REGISTRY.get(channel_key.lower())


def registered_channels() -> List[str]:
    """调试/诊断: 列出所有已注册的 channel_key。"""
    return sorted(REFERRAL_REGISTRY.keys())


# ─── 智能选渠道 ──────────────────────────────────────────────────────

def detect_channel_intent(incoming_text: str,
                           among: Optional[Iterable[str]] = None
                           ) -> Tuple[Optional[str], float]:
    """扫 incoming 消息找最强意图匹配的渠道。

    Args:
        incoming_text: 对方最新消息
        among: 可选, 限定只在这些 channel_key 里挑(通常是运营配置了账号的渠道)

    Returns:
        (channel_key, score): score=0 表示无匹配
    """
    if not incoming_text:
        return None, 0.0
    candidates = list(among) if among else list(REFERRAL_REGISTRY.keys())
    best_key: Optional[str] = None
    best_score = 0.0
    for key in candidates:
        ch = REFERRAL_REGISTRY.get(key)
        if not ch:
            continue
        score = ch.detect_intent(incoming_text)
        if score > best_score:
            best_score = score
            best_key = key
    return best_key, best_score


def pick_channel_smart(incoming_text: str = "",
                        persona_key: Optional[str] = None,
                        available_accounts: Optional[Dict[str, str]] = None,
                        ) -> Tuple[Optional[ReferralChannel], str]:
    """三段式智能选渠道 (intent → persona priority → 默认)。

    Args:
        incoming_text: 对方最新消息(用于意图识别)
        persona_key: 客群 key (查 referral_priority 用)
        available_accounts: 运营配置的账号 {channel_key: value}; 通常来自
            ``fb_referral_contact.parse_referral_channels(raw)``

    Returns:
        (channel_obj, account_value) — 未命中任何可用渠道返回 (None, "")
    """
    avail = dict(available_accounts or {})
    # 剔除 "_default" 伪渠道 (parse_referral_channels 的兜底项)
    default_blob = avail.pop("_default", "").strip()
    avail_keys = [k for k, v in avail.items() if v and get_channel(k) is not None]

    # Step 1: 意图感知
    if incoming_text:
        key, score = detect_channel_intent(incoming_text, among=avail_keys)
        if key and score >= 0.7:
            ch = get_channel(key)
            if ch:
                ok, cleaned = ch.validate_account(avail[key])
                return ch, (cleaned if ok else avail[key])

    # Step 2: persona referral_priority
    try:
        from src.host.fb_target_personas import get_referral_priority
        priority = list(get_referral_priority(persona_key) or [])
    except Exception as e:
        logger.debug("[referral] get_referral_priority 失败: %s", e)
        priority = ["whatsapp", "telegram", "instagram", "line"]
    for key in priority:
        key = (key or "").lower()
        if key in avail and avail[key]:
            ch = get_channel(key)
            if ch:
                ok, cleaned = ch.validate_account(avail[key])
                return ch, (cleaned if ok else avail[key])

    # Step 3: 任意可用渠道兜底
    for key in avail_keys:
        ch = get_channel(key)
        if ch:
            ok, cleaned = ch.validate_account(avail[key])
            return ch, (cleaned if ok else avail[key])

    # Step 4: 最裸兜底 — 有 _default 但无任何已知渠道配置
    if default_blob:
        # 取 persona 首推渠道当 fallback 对象
        key = priority[0].lower() if priority else "whatsapp"
        ch = get_channel(key)
        return ch, default_blob

    return None, ""
