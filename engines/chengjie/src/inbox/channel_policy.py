# -*- coding: utf-8 -*-
"""按渠道的出站策略层（实施96 P0-3，2026-09-07）——「这个平台允许我们怎么发」的单一声明处。

**为什么要有这个模块**：字数上限、能不能带外链、回复时间窗、每轮几条、能拆几个气泡、
能发什么媒体，此前散在 ``reply_split`` / ``cta_links`` / ``media_limits`` 里，且按全局或
按人设配置——没有「平台」这个维度。抖音（文本 ≤ 1000 字、**禁外链**、24h 内 ≤ 6 条、
进私事件 30 秒内 ≤ 3 条）、TikTok（48h ≤ 10 条）、微信客服（48h ≤ 5 条）、QQ 机器人
（60 分钟 ≤ 4 条）都是**平台侧会直接拒收**的硬规则；不落成策略层，System Z 的多气泡分段
会把配额打光，CTA 短链会撞平台的「不允许 HTTP 超链接」。

**分工（与实施97 的 ``kf_window_guard`` 不重复造轮子）**：
- 本模块是**声明层 + 无状态规则**：字数 / 外链 / 媒体类型 / 气泡上限 / 风险策略档位，
  以及窗口配额的**数值声明**（窗长、每轮条数、人工预留）；
- 窗口配额的**有状态执行**（记账 / 关窗 / 判定）在 ``src/inbox/kf_window_guard.py``——
  它今天只认 ``wechat_kf``，数值与这里的声明一致（门禁钉住）；把它按 ``window_rule()``
  参数化以覆盖抖音/TikTok 是下一步，不在本批。

**接线点（全部默认零行为变化——未登记平台一律「无限制」）**：
- ``AccountOrchestrator.send``：``text_block_reason()``（人工与自动同守——拒收的是平台）；
- ``AccountOrchestrator.send_media``：``media_block_reason()``；
- ``reply_split.should_split_for_delivery`` / ``cap_max_parts()``：气泡数封顶；
- ``autosend_policy.decide(platform=…)``：``risk_policy_mode()`` 按平台覆盖 shadow/enforce。

运营可按平台覆写数值：``config.channel_policy.<platform>.<字段>``（只放宽/收紧数值，不新增语义）。
全程纯函数、绝不抛：策略层自身故障按「无限制」处理（broken policy 不得把发送卡死）。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

LINKS_ALLOW = "allow"
LINKS_DENY = "deny"
LINKS_FIRST_MESSAGE_DENY = "first_message_deny"   # 首条禁链（需轮次状态，本层按 allow 放行）
_LINK_POLICIES = (LINKS_ALLOW, LINKS_DENY, LINKS_FIRST_MESSAGE_DENY)

REASON_TEXT_TOO_LONG = "policy_text_too_long"
REASON_LINK_DENIED = "policy_link_denied"
REASON_MEDIA_DENIED = "policy_media_type_denied"


@dataclass(frozen=True)
class ChannelPolicy:
    platform: str
    mode: str = ""
    max_text_len: Optional[int] = None            # None＝不限
    links: str = LINKS_ALLOW
    reply_window_sec: Optional[float] = None      # 对方最后一条后的可回复窗；None＝无窗
    per_window_cap: Optional[int] = None          # 窗内最多可发条数；None＝不限
    reserve_for_manual: int = 0                   # 自动链给坐席预留的条数（kf_window_guard 口径）
    enter_scene_window_sec: Optional[float] = None  # 抖音「用户进入私信页」事件后的快路径窗
    enter_scene_cap: Optional[int] = None
    max_bubbles: Optional[int] = None             # 一轮回复最多拆几条；None＝跟全局 reply_split
    media_types: Optional[FrozenSet[str]] = None  # None＝不限；空集＝禁一切媒体
    buttons: bool = True                          # 平台是否支持交互按钮（无则问题引导走纯文本）
    risk_policy_mode: str = ""                    # ""＝跟全局 autosend_policy；shadow / enforce
    note: str = ""

    @property
    def has_window(self) -> bool:
        return bool(self.reply_window_sec and self.per_window_cap)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "platform": self.platform, "mode": self.mode,
            "max_text_len": self.max_text_len, "links": self.links,
            "reply_window_sec": self.reply_window_sec, "per_window_cap": self.per_window_cap,
            "reserve_for_manual": self.reserve_for_manual,
            "enter_scene_window_sec": self.enter_scene_window_sec,
            "enter_scene_cap": self.enter_scene_cap, "max_bubbles": self.max_bubbles,
            "media_types": (sorted(self.media_types) if self.media_types is not None else None),
            "buttons": self.buttons, "risk_policy_mode": self.risk_policy_mode,
            "note": self.note,
        }


UNLIMITED = ChannelPolicy(platform="*", note="未登记平台：无限制（零行为变化）")

# ── 策略表：键 ``platform`` 或 ``platform:mode``（后者优先）────────────────────────
# 数值来源见 docs/实施96 §2.1 / §2.3 与 指令_TK-1 §2；改数值请连同来源日期一起改注释。
_POLICIES: Dict[str, ChannelPolicy] = {
    # 抖音小程序 IM（2025-05-08 版文档）：文本 ≤1000 字、禁外部 URL；24h 内 ≤6 条；
    # 进私事件 30s 内 ≤3 条；无按钮（问题引导卡是独立消息类型，不是按钮）；行为指纹严 → enforce。
    "douyin": ChannelPolicy(
        platform="douyin", max_text_len=1000, links=LINKS_DENY,
        reply_window_sec=24 * 3600.0, per_window_cap=6, reserve_for_manual=1,
        enter_scene_window_sec=30.0, enter_scene_cap=3, max_bubbles=2,
        media_types=frozenset({"image", "video"}), buttons=False, risk_policy_mode="enforce",
        note="抖音开放平台 /im/send/msg/ 规则（2026-09 核实）",
    ),
    # TikTok Business Messaging（2026-09 核实）：文本 ≤6000；48h ≤10 条；用户先发；图片按地区
    # （本层只声明类型，地区门控在 tiktok_regions）；无按钮/语音/视频；首条禁链为端内风控口径。
    "tiktok": ChannelPolicy(
        platform="tiktok", max_text_len=6000, links=LINKS_FIRST_MESSAGE_DENY,
        reply_window_sec=48 * 3600.0, per_window_cap=10, reserve_for_manual=1,
        max_bubbles=3, media_types=frozenset({"image"}), buttons=False,
        risk_policy_mode="enforce", note="TikTok Business Messaging API（Open Beta）",
    ),
    # 微信客服（实施97 线 A）：48h ≤5 条、预留 1 给坐席——**与 kf_window_guard 默认值同源**
    # （门禁 test_channel_policy 钉住）；整段一条（拆条＝白烧配额）。
    "wechat_kf": ChannelPolicy(
        platform="wechat_kf", reply_window_sec=48 * 3600.0, per_window_cap=5,
        reserve_for_manual=1, max_bubbles=1, buttons=False,
        note="企业微信客服官方规则；状态执行在 kf_window_guard",
    ),
    # QQ 机器人（实施97）：单聊每条来话 60 分钟内最多回 4 条；不支持主动消息；整段一条。
    "qqbot": ChannelPolicy(
        platform="qqbot", reply_window_sec=60 * 60.0, per_window_cap=4,
        reserve_for_manual=1, max_bubbles=1, buttons=False,
        note="QQ 开放平台被动回复规则（实施97 事实卡口径）",
    ),
    # WhatsApp Cloud API（官方形态）：客户最后一条后 24h 客服窗，窗外只能发模板消息。
    # 个人号协议（protocol）无此窗——键带 mode，只对 official 生效。
    "whatsapp:official": ChannelPolicy(
        platform="whatsapp", mode="official", reply_window_sec=24 * 3600.0,
        note="Meta WhatsApp Cloud API 24h customer service window（窗外需模板消息）",
    ),
}

_URL_RE = re.compile(
    r"(?:https?://\S+|www\.\S+|\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"\.(?:com|cn|net|org|io|me|co|app|shop|tv|cc|xyz|top|vip|info|link|site|online|store|"
    r"ph|sg|my|th|vn|id|jp|kr|hk|tw|uk|de|fr|es|it|ru|br|mx)\b(?:/\S*)?)",
    re.IGNORECASE,
)

_NUM_FIELDS = ("max_text_len", "per_window_cap", "reserve_for_manual", "enter_scene_cap",
               "max_bubbles")
_FLOAT_FIELDS = ("reply_window_sec", "enter_scene_window_sec")


def _norm(platform: Any) -> str:
    p = str(platform or "").strip().lower()
    if not p:
        return ""
    try:
        from src.integrations.platform_registry import normalize_platform_id
        return normalize_platform_id(p)
    except Exception:
        return p


def _apply_overrides(pol: ChannelPolicy, config: Optional[Dict[str, Any]]) -> ChannelPolicy:
    """``config.channel_policy.<platform>`` 覆写数值字段（只认已知字段，坏值忽略）。"""
    try:
        blk = ((config or {}).get("channel_policy") or {}).get(pol.platform) or {}
    except Exception:
        blk = {}
    if not isinstance(blk, dict) or not blk:
        return pol
    changes: Dict[str, Any] = {}
    for k in _NUM_FIELDS:
        if k in blk:
            try:
                changes[k] = (None if blk[k] is None else max(0, int(blk[k])))
            except (TypeError, ValueError):
                pass
    for k in _FLOAT_FIELDS:
        if k in blk:
            try:
                changes[k] = (None if blk[k] is None else max(0.0, float(blk[k])))
            except (TypeError, ValueError):
                pass
    if "links" in blk and str(blk["links"]) in _LINK_POLICIES:
        changes["links"] = str(blk["links"])
    if "risk_policy_mode" in blk and str(blk["risk_policy_mode"]) in ("", "shadow", "enforce"):
        changes["risk_policy_mode"] = str(blk["risk_policy_mode"])
    if "media_types" in blk and isinstance(blk["media_types"], (list, tuple, set, frozenset)):
        changes["media_types"] = frozenset(str(x).lower() for x in blk["media_types"])
    if "buttons" in blk:
        changes["buttons"] = bool(blk["buttons"])
    return replace(pol, **changes) if changes else pol


def policy_for(platform: Any, mode: str = "",
               config: Optional[Dict[str, Any]] = None) -> ChannelPolicy:
    """取 (platform, mode) 的策略；``platform:mode`` 键优先于 ``platform``；未登记 → UNLIMITED。"""
    try:
        p = _norm(platform)
        if not p:
            return UNLIMITED
        m = str(mode or "").strip().lower()
        pol = _POLICIES.get(f"{p}:{m}") if m else None
        if pol is None:
            pol = _POLICIES.get(p)
        if pol is None:
            return UNLIMITED
        return _apply_overrides(pol, config)
    except Exception:
        logger.debug("[channel_policy] policy_for 异常（按无限制）", exc_info=True)
        return UNLIMITED


def registered_platforms() -> Tuple[str, ...]:
    return tuple(sorted({v.platform for v in _POLICIES.values()}))


def has_link(text: Any) -> bool:
    return bool(_URL_RE.search(str(text or "")))


def text_block_reason(platform: Any, text: Any, *, mode: str = "",
                      config: Optional[Dict[str, Any]] = None,
                      first_message: bool = False) -> str:
    """平台硬规则拦截：超长 → ``policy_text_too_long:<len>/<max>``；禁链命中 → ``policy_link_denied``。

    只判**平台会拒收**的事（与风险/敏感词无关），所以人工与自动同守；空串＝放行。
    ``first_message_deny``（TikTok 端内风控：给陌生人的**第一条**带链即拦）由调用方传
    ``first_message=True``（``window_guard.is_first_message`` 从收件箱事实推导）才生效。
    """
    try:
        pol = policy_for(platform, mode, config)
        s = str(text or "")
        if pol.max_text_len is not None and pol.max_text_len > 0 and len(s) > pol.max_text_len:
            return f"{REASON_TEXT_TOO_LONG}:{len(s)}/{pol.max_text_len}"
        if pol.links == LINKS_DENY and has_link(s):
            return REASON_LINK_DENIED
        if pol.links == LINKS_FIRST_MESSAGE_DENY and first_message and has_link(s):
            return REASON_LINK_DENIED
        return ""
    except Exception:
        logger.debug("[channel_policy] text_block_reason 异常（放行）", exc_info=True)
        return ""


def media_block_reason(platform: Any, media_type: Any, *, mode: str = "",
                       config: Optional[Dict[str, Any]] = None) -> str:
    """媒体类型不在平台白名单 → ``policy_media_type_denied:<type>``；未登记/不限 → 空串。"""
    try:
        pol = policy_for(platform, mode, config)
        if pol.media_types is None:
            return ""
        mt = str(media_type or "").strip().lower()
        base = mt.split("/", 1)[0] if mt else ""
        if mt in pol.media_types or base in pol.media_types:
            return ""
        return f"{REASON_MEDIA_DENIED}:{mt or 'unknown'}"
    except Exception:
        logger.debug("[channel_policy] media_block_reason 异常（放行）", exc_info=True)
        return ""


def max_bubbles(platform: Any, mode: str = "",
                config: Optional[Dict[str, Any]] = None) -> Optional[int]:
    return policy_for(platform, mode, config).max_bubbles


def cap_max_parts(platform: Any, max_parts: int, mode: str = "",
                  config: Optional[Dict[str, Any]] = None) -> int:
    """全局 ``reply_split.max_parts`` 按平台封顶（无声明则原值）。"""
    try:
        mb = max_bubbles(platform, mode, config)
        mp = max(1, int(max_parts))
        return mp if mb is None else max(1, min(mp, int(mb)))
    except Exception:
        return max(1, int(max_parts or 1))


def risk_policy_mode(platform: Any, mode: str = "",
                     config: Optional[Dict[str, Any]] = None) -> str:
    """平台级风险策略档位覆写（"" ＝跟全局 ``inbox.l2_autosend.policy_mode``）。"""
    return policy_for(platform, mode, config).risk_policy_mode


def window_rule(platform: Any, mode: str = "",
                config: Optional[Dict[str, Any]] = None) -> Optional[Tuple[float, int, int]]:
    """``(window_sec, per_window_cap, reserve_for_manual)``；无窗口配额 → None。"""
    pol = policy_for(platform, mode, config)
    if not pol.has_window:
        return None
    return (float(pol.reply_window_sec), int(pol.per_window_cap), int(pol.reserve_for_manual))


def is_quota_platform(platform: Any, mode: str = "") -> bool:
    """有「窗口 + 条数配额」声明的平台（reply_split / holding_reply 据此整段一条）。"""
    return window_rule(platform, mode) is not None


def snapshot(platform: Any, mode: str = "", config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给 UI/诊断的一屏：该平台生效的策略（未登记 → ``{"unlimited": True}``）。"""
    pol = policy_for(platform, mode, config)
    if pol is UNLIMITED:
        return {"platform": _norm(platform), "unlimited": True}
    d = pol.as_dict()
    d["unlimited"] = False
    return d


__all__ = [
    "LINKS_ALLOW", "LINKS_DENY", "LINKS_FIRST_MESSAGE_DENY",
    "REASON_TEXT_TOO_LONG", "REASON_LINK_DENIED", "REASON_MEDIA_DENIED",
    "ChannelPolicy", "UNLIMITED", "policy_for", "registered_platforms", "has_link",
    "text_block_reason", "media_block_reason", "max_bubbles", "cap_max_parts",
    "risk_policy_mode", "window_rule", "is_quota_platform", "snapshot",
]
