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
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

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

    @property
    def autosend_allowed(self) -> bool:
        return self.level == "L2"

    @property
    def held(self) -> bool:
        return bool(self.hold_reason)


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
) -> Decision:
    """Decision = (level, hold_reason, shadow)。

    ``platform``（实施96 P0-3，可选）：未显式传 ``policy_mode`` 时，先问渠道策略层
    ``channel_policy.risk_policy_mode(platform)``——抖音/TikTok 这类以行为指纹与内容合规
    为主判据的平台声明 ``enforce``，全局 shadow 档对它们不生效；未声明的平台跟全局，
    不传 platform 逐字节旧行为。

    - 非 ``auto_ai`` 档（review / manual / multi_choice）：用户显式选的人审/手动档，
      **先于风险层**返回该档位（L1/L0/L1），``shadow=None``——不是风险触发的挂起，
      不进台账，也不受 policy_mode 影响。
    - ``auto_ai`` 档：按旧规则算 would_hold_level；
        · 旧规则也放行（low/low）→ L2，shadow=None；
        · 旧规则会扣（L3/L4）→ shadow 档：**L2 + 空 hold_reason + ShadowRecord**；
                                  enforce 档：would_hold_level + hold_reason，shadow=None。
    """
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
    # 先按旧规则算出 would_hold_level / hold_reason，然后无条件覆写 L2 + 空 hold_reason
    return Decision(level="L2", hold_reason="", shadow=rec,
                    policy_mode=pm, automation_mode=mode)


__all__ = [
    "POLICY_SHADOW", "POLICY_ENFORCE", "POLICY_MODES", "DEFAULT_POLICY_MODE",
    "ENV_POLICY_MODE", "HOLD_LEVELS", "Decision", "ShadowRecord",
    "decide", "legacy_level", "mode_level", "max_risk", "hold_reason_for",
    "current_policy_mode", "resolve_policy_mode", "normalize_policy_mode",
    "normalize_automation_mode",
]
