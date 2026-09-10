# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 形态与红线策略（纯函数，实施97 线 B）。

三档形态（``platform_login.wechat_pc.tier``，默认最保守）：
- ``copilot``   副驾：只读会话 → 翻译/意图/建议回复进工作台；**永不发送**（连出站队列都不拉）。
- ``semi``      半自动：只发「人审通过」的命令（草稿 approve 后入队的 kind=approved/manual），
                自动生成但未经人确认的一律不拉。
- ``auto_reply`` 全自动·仅回复：可发自动链命令，但**只对已有会话的入站回复**（队列命令必须能对上
                一条最近入站），绝不主动开新会话；需 ``risk_ack=true``（用户确认风险声明）。

永不清单（:data:`NEVER_ACTIONS`）写进代码而不是配置：加好友/通过好友申请/群发/朋友圈/红包转账/
多开/自动登录换号/协议或注入。这些是把工具从「辅助」变成「群控」的分界线（也是判例里被认定
不正当竞争的特征），任何路径请求这些动作一律 :func:`action_allowed` → False。

日配额随账号年龄爬坡（新号更保守）+ 工作时间闸；全部为**上限**语义，小于官方任何限制都不会
「更危险」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

TIER_COPILOT = "copilot"
TIER_SEMI = "semi"
TIER_AUTO_REPLY = "auto_reply"
TIERS = (TIER_COPILOT, TIER_SEMI, TIER_AUTO_REPLY)

#: 永不执行的动作（代码红线；不可配置）
NEVER_ACTIONS = frozenset({
    "add_friend", "accept_friend_request", "broadcast", "mass_send", "moments_post",
    "red_packet", "transfer", "multi_instance", "auto_login", "switch_account",
    "protocol", "inject", "join_group", "create_group", "invite_to_group",
})

#: 微信内置/官方会话：不是客户，永不读、永不回（读了会把新闻卡片当客户消息进收件箱，实锤）
SYSTEM_SESSION_NAMES = frozenset({
    "文件传输助手", "微信团队", "微信支付", "微信运动", "腾讯新闻", "订阅号", "订阅号消息", "服务通知",
    "折叠的群聊", "QQ邮箱提醒", "朋友推荐消息", "语音记事本", "微信游戏", "腾讯游戏", "视频号",
    "微信安全中心", "微信客服", "公众号", "小程序客服消息", "File Transfer", "WeChat Team", "Weixin Team",
})


def is_system_session(display_name: str) -> bool:
    return str(display_name or "").strip() in SYSTEM_SESSION_NAMES


#: 允许的动作（白名单；不在此即拒）
ALLOWED_ACTIONS = frozenset({
    "read_sessions", "read_messages", "read_profile", "open_session",
    "send_text_reply", "mark_read_by_open",
})

#: 出站队列命令 kind → 哪些档位可执行
_KIND_TIERS = {
    "manual": (TIER_SEMI, TIER_AUTO_REPLY),          # 坐席亲手发 / 人审通过
    "approved": (TIER_SEMI, TIER_AUTO_REPLY),
    "text": (TIER_AUTO_REPLY,),                        # 自动链（L2 autosend）
    "auto": (TIER_AUTO_REPLY,),
}


@dataclass(frozen=True)
class PcPolicy:
    tier: str = TIER_COPILOT
    risk_ack: bool = False
    daily_cap_new: int = 20          # 接入 < warmup_days 的账号日上限
    daily_cap: int = 80              # 成熟账号日上限
    warmup_days: int = 7
    per_peer_daily_cap: int = 15     # 对同一联系人每日上限（防「机关枪」）
    min_gap_sec: float = 20.0        # 同一联系人两条出站最小间隔
    work_hours: Tuple[int, int] = (8, 23)   # [start, end) 本地小时；全天 (0, 24)
    reply_only: bool = True          # 全自动档只回有入站的会话
    reply_window_hours: float = 72.0  # 「有入站」的时效：最近 N 小时内有客户消息
    groups_reply: str = "mention_only"  # 群：never | mention_only
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def sends_allowed(self) -> bool:
        return self.tier in (TIER_SEMI, TIER_AUTO_REPLY)


def resolve_policy(config: Optional[Dict[str, Any]]) -> PcPolicy:
    """``platform_login.wechat_pc`` 块 → :class:`PcPolicy`（坏值回默认，绝不抛）。"""
    try:
        blk = dict((((config or {}).get("platform_login") or {}).get("wechat_pc")) or {})
    except Exception:
        blk = {}

    def _i(key: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(blk.get(key, default))))
        except (TypeError, ValueError):
            return default

    def _f(key: str, default: float, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(blk.get(key, default))))
        except (TypeError, ValueError):
            return default

    tier = str(blk.get("tier") or TIER_COPILOT).strip().lower()
    if tier not in TIERS:
        tier = TIER_COPILOT
    risk_ack = bool(blk.get("risk_ack", False))
    # 全自动档没有风险确认 → 降到半自动（不是拒绝启动：坐席人审路径仍可用）
    if tier == TIER_AUTO_REPLY and not risk_ack:
        tier = TIER_SEMI
    wh = blk.get("work_hours")
    hours: Tuple[int, int] = (8, 23)
    if isinstance(wh, (list, tuple)) and len(wh) == 2:
        try:
            a, b = int(wh[0]), int(wh[1])
            if 0 <= a < b <= 24:
                hours = (a, b)
        except (TypeError, ValueError):
            pass
    groups = str(blk.get("groups_reply") or "mention_only").lower()
    if groups not in ("never", "mention_only"):
        groups = "mention_only"
    return PcPolicy(
        tier=tier, risk_ack=risk_ack,
        daily_cap_new=_i("daily_cap_new", 20, 1, 200),
        daily_cap=_i("daily_cap", 80, 1, 500),
        warmup_days=_i("warmup_days", 7, 0, 90),
        per_peer_daily_cap=_i("per_peer_daily_cap", 15, 1, 100),
        min_gap_sec=_f("min_gap_sec", 20.0, 3.0, 600.0),
        work_hours=hours,
        reply_only=bool(blk.get("reply_only", True)) or tier == TIER_AUTO_REPLY,
        reply_window_hours=_f("reply_window_hours", 72.0, 1.0, 24 * 30),
        groups_reply=groups,
    )


def action_allowed(action: str) -> bool:
    """动作白名单判定：永不清单恒 False；不在白名单也 False。"""
    a = str(action or "").strip().lower()
    if not a or a in NEVER_ACTIONS:
        return False
    return a in ALLOWED_ACTIONS


def kind_allowed(policy: PcPolicy, kind: str) -> bool:
    """出站命令 kind 在当前档位能否执行（副驾恒 False）。未知 kind 按自动链处理。"""
    if not policy.sends_allowed:
        return False
    tiers = _KIND_TIERS.get(str(kind or "text").lower(), _KIND_TIERS["text"])
    return policy.tier in tiers


def daily_cap_for(policy: PcPolicy, connected_at: float, now: float) -> int:
    """按账号接入年龄取日上限（预热期用 daily_cap_new）。"""
    if connected_at <= 0:
        return policy.daily_cap_new
    age_days = max(0.0, (now - connected_at) / 86400.0)
    return policy.daily_cap_new if age_days < policy.warmup_days else policy.daily_cap


def within_work_hours(policy: PcPolicy, now: datetime) -> bool:
    a, b = policy.work_hours
    if (a, b) == (0, 24):
        return True
    return a <= now.hour < b


@dataclass(frozen=True)
class SendVerdict:
    allowed: bool
    reason: str = ""


def may_send(
    policy: PcPolicy,
    *,
    kind: str,
    now: datetime,
    connected_at: float,
    sent_today: int,
    sent_today_to_peer: int,
    last_sent_to_peer_ts: float,
    last_inbound_from_peer_ts: float,
    is_group: bool = False,
    mentioned: bool = False,
) -> SendVerdict:
    """一条出站命令的综合裁决（纯函数）。判序：档位 → 工作时间 → 仅回复 → 群策略 → 配额 → 间隔。"""
    if not kind_allowed(policy, kind):
        return SendVerdict(False, "tier_forbids_kind")
    if not within_work_hours(policy, now):
        return SendVerdict(False, "outside_work_hours")
    now_ts = now.timestamp()
    if policy.reply_only:
        if last_inbound_from_peer_ts <= 0:
            return SendVerdict(False, "no_inbound_from_peer")
        if now_ts - last_inbound_from_peer_ts > policy.reply_window_hours * 3600.0:
            return SendVerdict(False, "inbound_too_old")
    if is_group:
        if policy.groups_reply == "never":
            return SendVerdict(False, "group_replies_disabled")
        if policy.groups_reply == "mention_only" and not mentioned:
            return SendVerdict(False, "group_not_mentioned")
    if sent_today >= daily_cap_for(policy, connected_at, now_ts):
        return SendVerdict(False, "daily_cap")
    if sent_today_to_peer >= policy.per_peer_daily_cap:
        return SendVerdict(False, "per_peer_daily_cap")
    if last_sent_to_peer_ts > 0 and now_ts - last_sent_to_peer_ts < policy.min_gap_sec:
        return SendVerdict(False, "min_gap")
    return SendVerdict(True, "")


__all__ = [
    "TIER_COPILOT", "TIER_SEMI", "TIER_AUTO_REPLY", "TIERS", "NEVER_ACTIONS", "ALLOWED_ACTIONS",
    "SYSTEM_SESSION_NAMES", "is_system_session",
    "PcPolicy", "SendVerdict", "resolve_policy", "action_allowed", "kind_allowed",
    "daily_cap_for", "within_work_hours", "may_send",
]
