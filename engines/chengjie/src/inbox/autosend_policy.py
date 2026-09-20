# -*- coding: utf-8 -*-
"""Autosend 放行决策**单一入口**（#160 I-1-A，2026-09-04 老板拍板 v2）。

决策原文：**风险扣稿全部放行，一条硬底线都不留。触发只写台账，不改变发送行为。**
攒满一个月真实流量数据后再做深度分析、定新拦截规则。理由：拦截的代价对用户
不可见——用户只看到「全自动不工作了」，不知道为什么、也不知道去哪看。

配置键 ``inbox.l2_autosend.policy_mode``：``shadow``（默认）| ``enforce``。

``shadow`` 档语义（本模块的全部内容就是这一句）：
    **先按旧规则算出 would_hold_level / hold_reason，然后无条件把 level 覆写成 L2、
    hold_reason 覆写成空，把算出来的东西放进 shadow 字段返回。** 旧规则的计算逻辑
    保留（``legacy_level``）——那是 enforce 档一个月后的起点，也是台账的判据。

``enforce`` 档只留骨架（＝v1.0.71 旧表逐字真扣）、**不接线、不给默认值**——规则表
一个月后读完台账再定，现在不猜着写。测试用它做反向验证：证明旧规则逻辑没被删。

**会话自己的档位先于风险层**（shadow 档）：``automation_mode=review/manual`` 是用户
显式选的人审档，与风险无关，直接返回该档位的挂起且 ``shadow=None``（不是风险触发的，
不该进台账）。本条只取消**风险驱动**的降档。

结构不变量（最重要的一条）：``drafts.py`` 的 ``auto_generate_draft`` / ``enrich_draft`` /
``risk_to_autopilot`` / ``is_autosend_allowed`` / ``apply_analysis`` 与 worker 的捞稿逻辑
**全部只认本模块**——任何一处再自己算一遍档位，一个月后就会出现「台账说放行了、
实际还是被拦」这种无法归因的状态。

**09-04 放行的两条豁免（O-1 A · #252 #253 · D-O1，2026-09-08）**——不看 policy_mode：

1. ``stop_contact`` / ``self_harm`` 命中（:data:`HARD_STOP_REASONS`）→ **硬停**。auto_ai 且
   会话尚未冻结：不回客户（``farewell=False``，level L1，``review_required=True``），
   调用方 ``freeze_conversation`` 只提醒坐席（需人工 + 通知中心）。已冻结：stop_contact →
   L4，self_harm → L1 人审。review / manual 档保留其档位，同样 ``hard_stop`` 冻结、无出站。
   影子记录照常产出（台账要有这一行、值守群要收即时告警），去向行会如实写成未发。
2. ``risk=high``（非停联）在 auto_ai + shadow 档 → **转人工审**（``review_required=True``，
   level L1，hold_reason ``risk_high_review``），不再直发；medium 仍放行进台账。

放行初衷是敏感词误伤（「用户只看到全自动不工作了」），不是客户说了「别再写了」还连发
两条——那是投诉与封号的直接原因（XAM4KV 21:00:26 → 21:03:48）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

POLICY_SHADOW = "shadow"
POLICY_ENFORCE = "enforce"
POLICY_MODES = (POLICY_SHADOW, POLICY_ENFORCE)
DEFAULT_POLICY_MODE = POLICY_SHADOW
# 显式覆写（测试 / 反向验证 / 应急）——优先于配置。生产不设。
ENV_POLICY_MODE = "AITR_AUTOSEND_POLICY_MODE"

AUTOMATION_MODES = ("manual", "review", "multi_choice", "auto_ai")
HOLD_LEVELS = ("L3", "L4")

_RISK_RANK = {"high": 3, "medium": 2, "low": 1, "unknown": 0}

# hold_reason 单值化的优先级：要人看的两个排最前（stop_contact 即时推值守群）。
_REASON_PRIORITY = (
    "stop_contact", "self_harm", "credential_or_payment_request",
    "money", "privacy", "adult", "keyword", "negative_emotion",
)

# O-1 A（D-O1）：09-04 放行的豁免项——命中即硬停，不看 policy_mode。
# 与 ``src.inbox.stop_contact.FREEZE_REASONS`` 同值（测试钉住）。
HARD_STOP_REASONS = ("stop_contact", "self_harm")
# risk=high（非停联）在全自动下转人工审的 hold_reason（L1，草稿进人审队列）
REVIEW_HOLD_REASON = "risk_high_review"

# Q-18 B（#292，2026-09-11）：**让位 = 延后不是丢弃**。人工优先复检（worker
# ``_human_priority_gate``）返回这两个原因码时，batch / presend 两处一律 defer——稿留队、
# ``deferred_until = 坐席最后活动 + AGENT_YIELD_WINDOW_SEC``，窗过自动复检再发（仍是最新
# 入站且档位仍 auto → 发；期间有更新入站 → fresh_guard 让新稿覆盖）。其余原因码
# （mode_changed / risk_hold / needs_human）仍是放弃，语义一字不变。
#: Q-23（#303）：守卫触发的自动软回应走 ``decide(kind=SOFT_REPLY_KIND)``；它是对这些持有原因的
#: **回应**，不被它们按成 L1（其它持有原因照旧 L1）。
SOFT_REPLY_KIND = "soft_reply"
SOFT_REPLY_OWN_HOLDS = ("adult",)

YIELD_DEFER_REASONS = ("agent_sent", "agent_typing")
AGENT_YIELD_WINDOW_SEC = 60.0
# 连续让位上限（坐席一直在发 / 一直在打字 → 稿最多顺延这么多次，再多按放弃处理防无限滞留）
AGENT_YIELD_MAX_DEFERRALS = 10


def agent_yield_state(sent_ts: float, typing_ts: float, *, now: Optional[float] = None,
                      window: float = AGENT_YIELD_WINDOW_SEC) -> Dict[str, Any]:
    """纯函数：由坐席最后发送 / 打字时刻算「AI 让位」状态——worker 复检、诊断 finding
    ``agent_yield`` 与会话头 ``ay-`` chip 三处同一口径。

    返回 ``{active, by, since, until, remaining}``：``by`` ∈ agent_sent | agent_typing | ""
    （发送优先于打字，与复检顺序一致）；``until`` = 最后活动 + window；``remaining`` 秒
    （非负）。两个时刻都为 0 / 窗已过 → ``active=False``。
    """
    import time as _t
    _now = float(now if now is not None else _t.time())
    _sent = float(sent_ts or 0.0)
    _typ = float(typing_ts or 0.0)
    by, since = "", 0.0
    if _sent > 0 and _now - _sent <= window:
        by, since = "agent_sent", _sent
    elif _typ > 0 and _now - _typ <= window:
        by, since = "agent_typing", _typ
    if not by:
        return {"active": False, "by": "", "since": 0.0, "until": 0.0, "remaining": 0.0}
    until = since + float(window)
    return {"active": True, "by": by, "since": since, "until": until,
            "remaining": max(0.0, until - _now)}


@dataclass(frozen=True)
class ShadowRecord:
    """「本会被扣」的判据快照——台账每行的核心字段来源。"""
    would_hold_level: str            # L3 | L4
    hold_reason: str                 # 单值主因（优先级见 _REASON_PRIORITY）
    peer_risk: str
    peer_reasons: List[str] = field(default_factory=list)
    reply_risk: str = "low"
    reply_reasons: List[str] = field(default_factory=list)
    risk_hits: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "would_hold_level": self.would_hold_level,
            "hold_reason": self.hold_reason,
            "peer_risk": self.peer_risk,
            "peer_reasons": list(self.peer_reasons),
            "reply_risk": self.reply_risk,
            "reply_reasons": list(self.reply_reasons),
            "risk_hits": list(self.risk_hits),
        }


@dataclass(frozen=True)
class Decision:
    level: str                       # L0..L4（发送行为看这个）
    hold_reason: str                 # 空＝放行；非空＝挂起原因
    shadow: Optional[ShadowRecord]   # shadow 档下「本会被扣」的记录；None＝旧规则也放行/非风险挂起
    policy_mode: str = DEFAULT_POLICY_MODE
    automation_mode: str = "review"
    # O-1 A（D-O1）：硬停原因（stop_contact / self_harm / 空）——非空＝调用方须冻结会话
    hard_stop: str = ""
    # 历史字段：锁定硬停不再出站（恒 False）。保留给旧草稿 / 测试兼容。
    farewell: bool = False
    # risk=high 非停联 → 转人工审（level=L1）
    review_required: bool = False
    # Q-3（#264）：会话级风险持有（risk_hold.active）把本稿按成 L1 的原因码；空＝未触发
    risk_hold: str = ""

    @property
    def autosend_allowed(self) -> bool:
        return self.level == "L2"

    @property
    def held(self) -> bool:
        return bool(self.hold_reason)


def hard_stop_reason(peer_reasons: Iterable[str]) -> str:
    """入站风险原因里的硬停项（按 HARD_STOP_REASONS 优先级取首个）；无 → 空串。"""
    pr = [str(r) for r in (peer_reasons or [])]
    for r in HARD_STOP_REASONS:
        if r in pr:
            return r
    return ""


# ── 策略模式解析 ─────────────────────────────────────────────────────────

def normalize_policy_mode(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in POLICY_MODES else DEFAULT_POLICY_MODE


def resolve_policy_mode(config: Optional[Dict[str, Any]]) -> str:
    """从配置树读 ``inbox.l2_autosend.policy_mode``（缺省 shadow）。"""
    try:
        v = (((config or {}).get("inbox") or {}).get("l2_autosend") or {}).get("policy_mode")
    except Exception:
        v = None
    return normalize_policy_mode(v)


def current_policy_mode() -> str:
    """生效策略：env ``AITR_AUTOSEND_POLICY_MODE`` > 运行时配置 > 默认 shadow。

    运行时配置经 ``src.compliance.runtime.runtime_config``（bootstrap 注册的 provider，
    随 overlay 热重载）；测试环境无 provider → 空配置 → shadow。
    """
    env = os.environ.get(ENV_POLICY_MODE, "").strip().lower()
    if env in POLICY_MODES:
        return env
    try:
        from src.compliance.runtime import runtime_config
        return resolve_policy_mode(runtime_config())
    except Exception:
        return DEFAULT_POLICY_MODE


# ── 旧规则（保留：enforce 档起点 + 台账判据）────────────────────────────

def normalize_automation_mode(mode: Any) -> str:
    m = str(mode or "review").strip().lower()
    return m if m in AUTOMATION_MODES else "review"


def mode_level(automation_mode: str) -> str:
    """只看会话档位、不看风险的档：manual→L0 / auto_ai→L2 / 其余→L1。"""
    m = normalize_automation_mode(automation_mode)
    if m == "manual":
        return "L0"
    if m == "auto_ai":
        return "L2"
    return "L1"


def legacy_level(risk_level: str, automation_mode: str) -> str:
    """v1.0.71 及之前的 ``risk_to_autopilot`` 原表（**逐字保留**，勿改）：

    high → L4 / medium → L3（无条件，不看档位）；low：manual→L0 / auto_ai→L2 / 其余 L1。
    """
    risk = str(risk_level or "low").lower()
    if risk == "high":
        return "L4"
    if risk == "medium":
        return "L3"
    return mode_level(automation_mode)


def max_risk(a: Any, b: Any) -> str:
    ra = _RISK_RANK.get(str(a or "unknown").lower(), 0)
    rb = _RISK_RANK.get(str(b or "unknown").lower(), 0)
    return str(a or "unknown") if ra >= rb else str(b)


def hold_reason_for(
    peer_risk: str, peer_reasons: Iterable[str],
    reply_risk: str, reply_reasons: Iterable[str],
) -> str:
    """把「为什么会被扣」压成一个主因（台账分桶键）。

    优先级：stop_contact > self_harm > 索要凭证 > 主题词 > 关键词表 > 负面情绪；
    AI 稿自己的风险高于入站 → ``reply_risk``（出站侧才是老板说「该管的」那一侧）。
    """
    pr = list(peer_reasons or [])
    rr = list(reply_reasons or [])
    rank_peer = _RISK_RANK.get(str(peer_risk or "low").lower(), 0)
    rank_reply = _RISK_RANK.get(str(reply_risk or "low").lower(), 0)
    if rank_reply > rank_peer and rank_reply >= _RISK_RANK["medium"]:
        return "reply_risk"
    for r in _REASON_PRIORITY:
        if r in pr:
            return r
    if pr:
        return str(pr[0])
    if rank_reply >= _RISK_RANK["medium"]:
        return "reply_risk"
    return f"peer_risk_{str(peer_risk or 'unknown').lower()}"


# ── 单一入口 ─────────────────────────────────────────────────────────────

def decide(
    peer_risk: str,
    peer_reasons: Iterable[str] = (),
    reply_risk: str = "low",
    reply_reasons: Iterable[str] = (),
    risk_hits: Iterable[str] = (),
    automation_mode: str = "review",
    policy_mode: Optional[str] = None,
    platform: str = "",
    conversation_frozen: bool = False,
    conversation_id: str = "",
    store: Any = None,
    risk_hold_reason: Optional[str] = None,
    kind: str = "draft",
    ctx: Any = None,
) -> Decision:
    """Decision = (level, hold_reason, shadow[, hard_stop, farewell, review_required, risk_hold])。

    ``kind`` / ``ctx``（Q-23 #303）：``kind="soft_reply"`` = 守卫触发的自动软回应（成人软回应 /
    human 3 分钟补发）——它是**对**风险持有的回应，不再被自己刚设的 ``adult`` 持有按成 L1，
    但必须过场景闸（``ctx.skip_reason()`` → L0 ``scene:<reason>``）、冻结闸、档位闸（非
    auto_ai → 该档位 + ``review_required``＝候选进审核稿）、他因持有闸（非 adult 的 risk_hold → L1）。
    ``kind="draft"``（缺省）逐字旧行为，``ctx`` 不参与草稿判定。

    ``conversation_id`` + ``store`` / ``risk_hold_reason``（Q-3 #264 A）：会话级风险持有。
    调用方给了会话（``store`` 缺省时 ``risk_hold_reason`` 可直接传已知原因）→ 先问
    ``risk_hold.active(conv)``；活跃 → 非硬停分支一律**强制 L1**（人审）并**继承 shadow**
    （已有影子记录保留；没有则按持有原因造一条 would_hold_level=L3 的记录——重新起草
    不得归零），``hold_reason=risk_hold:<reason>``、``review_required=True``。不传会话＝
    逐字节旧行为（drafts.py 的调用点由 Q-2 补 ``conversation_id=``，见总账）。

    ``platform``（实施96 P0-3，可选）：未显式传 ``policy_mode`` 时，先问渠道策略层
    ``channel_policy.risk_policy_mode(platform)``——抖音/TikTok 这类以行为指纹与内容合规
    为主判据的平台声明 ``enforce``，全局 shadow 档对它们不生效；未声明的平台跟全局，
    不传 platform 逐字节旧行为。

    ``conversation_frozen``（O-1 A）：调用方读到的 ``stop_contact.frozen_reason`` 非空——
    已冻结的会话不再出站。

    - **硬停先于一切**（D-O1 / R88 锁定）：``peer_reasons`` 含 stop_contact / self_harm → 一律
      ``hard_stop=<reason>``（调用方据此冻结会话，不看 policy_mode / 档位）。level：
      shadow × auto_ai × 未冻结 → L1 人审（``farewell=False``，不回客户，坐席消息提醒）；
      shadow × auto_ai × 已冻结 → stop_contact L4 / self_harm L1 人审（带 shadow 记录）；
      enforce → 旧表逐字 L4（shadow=None）；非 auto_ai → 保留档位（人已在环，不进台账）。
    - 非 ``auto_ai`` 档（review / manual / multi_choice）：用户显式选的人审/手动档，
      **先于风险层**返回该档位（L1/L0/L1），``shadow=None``——不是风险触发的挂起，
      不进台账，也不受 policy_mode 影响。
    - ``auto_ai`` 档：按旧规则算 would_hold_level；
        · 旧规则也放行（low/low）→ L2，shadow=None；
        · 旧规则会扣（L3/L4）→ shadow 档：medium **L2 + 空 hold_reason + ShadowRecord**；
                                  high → **L1 + ``risk_high_review`` + ShadowRecord**
                                  （D-O1 豁免②：高风险稿转人工审，不直发）；
                                  enforce 档：would_hold_level + hold_reason，shadow=None。
    """
    if str(kind or "draft") == SOFT_REPLY_KIND:
        return _decide_soft_reply(automation_mode, ctx, policy_mode, platform, conversation_frozen,
                                  conversation_id, store, risk_hold_reason)
    unr = _decide_unrestricted(peer_reasons, automation_mode, policy_mode, platform,
                               conversation_id, store)
    if unr is not None:
        return unr
    base = _decide_base(
        peer_risk, peer_reasons, reply_risk, reply_reasons, risk_hits,
        automation_mode, policy_mode, platform, conversation_frozen)
    hold = _resolve_risk_hold(conversation_id, store, risk_hold_reason)
    if not hold or base.hard_stop:
        # 硬停分支（停联告别「最多一条」/ 自伤陪伴）语义不动——冻结本身已把会话按成 manual
        return base
    if base.level == "L1" and base.review_required:
        # 已是风险人审（risk_high_review）：只补原因码，影子记录原样继承
        return Decision(level="L1", hold_reason=base.hold_reason, shadow=base.shadow,
                        policy_mode=base.policy_mode, automation_mode=base.automation_mode,
                        review_required=True, risk_hold=hold)
    peer_reasons_l = [str(r) for r in (peer_reasons or [])]
    reply_reasons_l = [str(r) for r in (reply_reasons or [])]
    hits_l = [str(h) for h in (risk_hits or []) if str(h)]
    shadow = base.shadow or ShadowRecord(
        would_hold_level="L3", hold_reason=f"risk_hold:{hold}",
        peer_risk=str(peer_risk or "low"), peer_reasons=peer_reasons_l,
        reply_risk=str(reply_risk or "low"), reply_reasons=reply_reasons_l,
        risk_hits=hits_l,
    )
    logger.info("[policy] conv=%s risk_hold=%s forced=L1 was=%s mode=%s shadow=%s",
                conversation_id or "-", hold, base.level, base.automation_mode,
                shadow.hold_reason)
    return Decision(level="L1", hold_reason=f"risk_hold:{hold}", shadow=shadow,
                    policy_mode=base.policy_mode, automation_mode=base.automation_mode,
                    review_required=True, risk_hold=hold)


def _decide_unrestricted(peer_reasons: Iterable[str], automation_mode: str,
                         policy_mode: Optional[str], platform: str,
                         conversation_id: str, store: Any) -> Optional[Decision]:
    """会话级「无限制」（conv_route，2026-09-12）：风控分级层整段让路。

    - 非无限制会话 / 无 store / 无会话 id → None（逐字节走原判定）；
    - 硬停（stop_contact / self_harm）属安全刹车：未开 ``bypass_safety`` → None，让原判定
      按硬停语义处理（告别一条 + 冻结）；开了 → 与其他风险原因一样忽略；
    - 其余：peer/reply 风险、关键词命中、policy_mode、risk_hold 全不参与——
      ``auto_ai`` → L2 直发；review/manual/multi_choice 是坐席自己选的「谁来答」，
      不是拦截，保留档位（``mode:<mode>``），不进影子台账。
    """
    cid = str(conversation_id or "").strip()
    if not cid or store is None:
        return None
    try:
        from src.ai.conv_route import skip_for_conv
        if not skip_for_conv(store, cid, "risk_level"):
            return None
        bypass_hard = bool(skip_for_conv(store, cid, "hard_stop"))
    except Exception:
        logger.debug("[autosend_policy] conv_route lookup failed; standard decide", exc_info=True)
        return None
    hard = hard_stop_reason([str(r) for r in (peer_reasons or [])])
    if hard and not bypass_hard:
        return None
    mode = normalize_automation_mode(automation_mode)
    if not policy_mode and platform:
        try:
            from src.inbox.channel_policy import risk_policy_mode as _cp_risk_mode
            policy_mode = _cp_risk_mode(platform) or None
        except Exception:
            logger.debug("[autosend_policy] channel risk_policy_mode unavailable", exc_info=True)
            policy_mode = None
    pm = normalize_policy_mode(policy_mode) if policy_mode else current_policy_mode()
    if mode == "auto_ai":
        dec = Decision(level="L2", hold_reason="", shadow=None, policy_mode=pm,
                       automation_mode=mode)
    else:
        dec = Decision(level=mode_level(mode), hold_reason=f"mode:{mode}", shadow=None,
                       policy_mode=pm, automation_mode=mode)
    logger.info("[policy] conv=%s unrestricted → level=%s mode=%s (risk layer bypassed%s)",
                cid, dec.level, mode, ", hard_stop ignored" if hard else "")
    return dec


def _decide_soft_reply(automation_mode: str, ctx: Any, policy_mode: Optional[str], platform: str,
                       conversation_frozen: bool, conversation_id: str, store: Any,
                       risk_hold_reason: Optional[str]) -> Decision:
    """``kind="soft_reply"`` 判定（Q-23 #303）。只做负向闸：错场景 → 什么都不做。"""
    mode = normalize_automation_mode(getattr(ctx, "mode", None) or automation_mode)
    if not policy_mode and platform:
        try:
            from src.inbox.channel_policy import risk_policy_mode as _cp_risk_mode
            policy_mode = _cp_risk_mode(platform) or None
        except Exception:
            policy_mode = None
    pm = normalize_policy_mode(policy_mode) if policy_mode else current_policy_mode()
    skip = ""
    if ctx is not None:
        try:
            skip = str(ctx.skip_reason() or "")
        except Exception:
            skip = ""
    if skip:
        dec = Decision(level="L0", hold_reason=f"scene:{skip}", shadow=None,
                       policy_mode=pm, automation_mode=mode)
    elif conversation_frozen:
        dec = Decision(level="L0", hold_reason="frozen", shadow=None,
                       policy_mode=pm, automation_mode=mode)
    elif mode != "auto_ai":
        dec = Decision(level=mode_level(mode), hold_reason=f"mode:{mode}", shadow=None,
                       policy_mode=pm, automation_mode=mode, review_required=True)
    else:
        hold = _resolve_risk_hold(conversation_id, store, risk_hold_reason)
        if hold and hold not in SOFT_REPLY_OWN_HOLDS:
            dec = Decision(level="L1", hold_reason=f"risk_hold:{hold}", shadow=None,
                           policy_mode=pm, automation_mode=mode, review_required=True, risk_hold=hold)
        else:
            dec = Decision(level="L2", hold_reason="", shadow=None,
                           policy_mode=pm, automation_mode=mode, risk_hold=hold)
    logger.info("[policy] kind=%s conv=%s level=%s hold=%s mode=%s", SOFT_REPLY_KIND,
                conversation_id or "-", dec.level, dec.hold_reason or "-", mode)
    return dec


def _resolve_risk_hold(conversation_id: str, store: Any,
                       risk_hold_reason: Optional[str]) -> str:
    """会话级持有原因：显式传入优先；否则有 store + conv 时查 ``risk_hold.active``。绝不抛。"""
    if risk_hold_reason:
        return str(risk_hold_reason)
    cid = str(conversation_id or "").strip()
    if not cid or store is None:
        return ""
    try:
        from src.inbox import risk_hold as _rh
        return str(_rh.active(store, cid) or "")
    except Exception:
        return ""


def _decide_base(
    peer_risk: str,
    peer_reasons: Iterable[str],
    reply_risk: str,
    reply_reasons: Iterable[str],
    risk_hits: Iterable[str],
    automation_mode: str,
    policy_mode: Optional[str],
    platform: str,
    conversation_frozen: bool,
) -> Decision:
    """``decide`` 的原判定（#160 v2 单一入口正文，Q-3 之前逐字）。"""
    mode = normalize_automation_mode(automation_mode)
    if not policy_mode and platform:
        try:
            from src.inbox.channel_policy import risk_policy_mode as _cp_risk_mode
            policy_mode = _cp_risk_mode(platform) or None
        except Exception:
            policy_mode = None
    pm = normalize_policy_mode(policy_mode) if policy_mode else current_policy_mode()
    peer_reasons_l = [str(r) for r in (peer_reasons or [])]
    reply_reasons_l = [str(r) for r in (reply_reasons or [])]
    hits_l = [str(h) for h in (risk_hits or []) if str(h)]
    effective = max_risk(peer_risk or "low", reply_risk or "low")

    # ── 硬停（D-O1 豁免① / R88 锁定）：``hard_stop`` 冻结不看 policy_mode / 档位；
    #    未冻结也不回客户（无告别、无陪伴稿），只 L1 + 坐席提醒。enforce 档＝旧表逐字 L4，
    #    review/manual 档＝人已在环，同样不代人出站。
    hard = hard_stop_reason(peer_reasons_l)
    if hard:
        if pm == POLICY_ENFORCE:
            # 旧表逐字（含 review+high → L4 主管闸），不写影子台账；只多带冻结信号
            lvl = legacy_level(effective, mode)
            lvl = lvl if lvl in HOLD_LEVELS else "L4"
            return Decision(level=lvl, hold_reason=hard, shadow=None,
                            policy_mode=pm, automation_mode=mode, hard_stop=hard)
        if mode != "auto_ai":
            # 用户显式选的人审/手动档：保留档位、不进台账（非风险触发的挂起），只带冻结信号
            lvl = mode_level(mode)
            return Decision(level=lvl, hold_reason=f"mode:{mode}", shadow=None,
                            policy_mode=pm, automation_mode=mode, hard_stop=hard)
        would = legacy_level(effective, mode)
        rec = ShadowRecord(
            would_hold_level=(would if would in HOLD_LEVELS else "L4"),
            hold_reason=hard,
            peer_risk=str(peer_risk or "high"), peer_reasons=peer_reasons_l,
            reply_risk=str(reply_risk or "low"), reply_reasons=reply_reasons_l,
            risk_hits=hits_l,
        )
        if not conversation_frozen:
            # 锁定命中：不回客户（无告别、无固定稿），L1 挂起 + 调用方冻结/提醒坐席。
            return Decision(level="L1", hold_reason=hard, shadow=rec,
                            policy_mode=pm, automation_mode=mode,
                            hard_stop=hard, farewell=False, review_required=True)
        lvl = "L4" if hard == "stop_contact" else "L1"
        return Decision(level=lvl, hold_reason=hard, shadow=rec,
                        policy_mode=pm, automation_mode=mode, hard_stop=hard)

    if pm == POLICY_ENFORCE:
        # 骨架＝v1.0.71 旧表**逐字**（含 review+high→L4 主管闸）。规则表一个月后读完
        # 台账再定，这里不加新逻辑；它同时是「旧规则仍在」的反向验证基线。
        lvl = legacy_level(effective, mode)
        if lvl in HOLD_LEVELS:
            reason = hold_reason_for(peer_risk, peer_reasons_l, reply_risk, reply_reasons_l)
            return Decision(level=lvl, hold_reason=reason, shadow=None,
                            policy_mode=pm, automation_mode=mode)
        return Decision(level=lvl, hold_reason=("" if lvl == "L2" else f"mode:{mode}"),
                        shadow=None, policy_mode=pm, automation_mode=mode)

    # ── shadow ──
    if mode != "auto_ai":
        # 用户显式选的人审/手动档：先于风险层，挂起不是风险触发的 → 不进台账
        lvl = mode_level(mode)
        return Decision(level=lvl, hold_reason=f"mode:{mode}", shadow=None,
                        policy_mode=pm, automation_mode=mode)

    would = legacy_level(effective, mode)
    if would not in HOLD_LEVELS:
        return Decision(level="L2", hold_reason="", shadow=None,
                        policy_mode=pm, automation_mode=mode)

    reason = hold_reason_for(peer_risk, peer_reasons_l, reply_risk, reply_reasons_l)
    rec = ShadowRecord(
        would_hold_level=would, hold_reason=reason,
        peer_risk=str(peer_risk or "low"), peer_reasons=peer_reasons_l,
        reply_risk=str(reply_risk or "low"), reply_reasons=reply_reasons_l,
        risk_hits=hits_l,
    )
    if would == "L4":
        # D-O1 豁免②：risk=high（非停联）在全自动下转人工审——L1 进人审队列，不直发；
        # 影子记录照常（台账仍要看到「本会被扣」+ 命中词），去向行会写成人工处置结果。
        return Decision(level="L1", hold_reason=REVIEW_HOLD_REASON, shadow=rec,
                        policy_mode=pm, automation_mode=mode, review_required=True)
    # medium：先按旧规则算出 would_hold_level / hold_reason，然后覆写 L2 + 空 hold_reason
    return Decision(level="L2", hold_reason="", shadow=rec,
                    policy_mode=pm, automation_mode=mode)


__all__ = [
    "POLICY_SHADOW", "POLICY_ENFORCE", "POLICY_MODES", "DEFAULT_POLICY_MODE",
    "ENV_POLICY_MODE", "HOLD_LEVELS", "HARD_STOP_REASONS", "REVIEW_HOLD_REASON",
    "Decision", "ShadowRecord", "hard_stop_reason",
    "decide", "legacy_level", "mode_level", "max_risk", "hold_reason_for",
    "current_policy_mode", "resolve_policy_mode", "normalize_policy_mode",
    "normalize_automation_mode",
    "YIELD_DEFER_REASONS", "AGENT_YIELD_WINDOW_SEC", "AGENT_YIELD_MAX_DEFERRALS",
    "agent_yield_state",
    "SOFT_REPLY_KIND", "SOFT_REPLY_OWN_HOLDS",
]
