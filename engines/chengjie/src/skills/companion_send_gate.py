"""共享「发送前反封号闸门」（N 线 核心3）。

A 线 (``TelegramClient._send_reply``) 与 B 线 (协议 autoreply ``_send``) 此前各管各的
限速，预热爬坡 / 健康红绿灯（M7 ``account_health``）没接到任一真实发送路径。本模块把
M7 纯函数**编排成一道两线共用的发送前决策**，不重复造代理/评分/爬坡逻辑：

    signals → account_health(M7) → 决策 allowed / reason / health

默认**关闭**（``companion_send_gate.enabled`` 缺省 false）→ 行为零变更；开启后两条线
共用同一门控（超预热上限 / 红灯 → 拒发或转人工）。机群概览复用 M7 ``fleet_health``。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.skills.account_health import account_health, fleet_health


def gate_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """读 ``companion_send_gate.enabled``（默认 False → 零破坏）。"""
    try:
        return bool(((config or {}).get("companion_send_gate") or {}).get("enabled", False))
    except Exception:
        return False


def peer_exempt(config: Optional[Dict[str, Any]], chat_key: str) -> bool:
    """测试白名单（2026-07-22）：``companion_send_gate.exempt_peers`` 里的对话对象
    免限额——联调测试不再与养号策略打架（真机两次事故：warmup_cap 拦下测试回复）。

    匹配宽松：白名单项是 chat_key 的子串即命中（号码常带/不带国家码、@suffix）。
    生产客户不在名单 → 完全不受影响。
    """
    ck = str(chat_key or "").strip()
    if not ck:
        return False
    try:
        peers = ((config or {}).get("companion_send_gate") or {}).get(
            "exempt_peers") or []
        for p in peers:
            ps = str(p or "").strip()
            if ps and (ps in ck or ck in ps):
                return True
    except Exception:
        pass
    return False


def _gate_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return dict((config or {}).get("companion_send_gate") or {})
    except Exception:
        return {}


def gate_decision(
    signals: Dict[str, Any],
    *,
    target_cap: int = 15,
    warmup_start_cap: int = 2,
    warmup_ramp_days: int = 14,
    block_on_red: bool = True,
    reserve_for_manual: int = 0,
    origin: str = "auto",
) -> Dict[str, Any]:
    """单账号发送前决策（纯函数，复用 M7 ``account_health``）。

    返回 ``{allowed, reason, light, score, recommended_cap, auto_cap,
    reserve_for_manual, health}``：
    - ``banned`` 或（``block_on_red`` 且红灯）→ allowed=False, reason=``health_red``/``banned``
    - 额度判定按 ``origin`` 分道（P1 2026-08-12 人工预留额度）：
      ``auto`` 比较 ``auto_cap = max(0, recommended_cap - reserve_for_manual)``；
      ``manual`` 比较完整 ``recommended_cap``。语义＝自动链在触顶前 N 条就让路，
      把最后的名额留给「客户在等」的人工回复——修当日实锤的优先级倒挂
      （深夜主动批量烧光 150 额度、坐席点发送反而被拦）。
      reserve=0（默认）→ 两道同值，行为与旧版逐字节一致。
    - 否则 allowed=True, reason=``ok``

    额度拦截 reason（P3 2026-08-13 更名）＝``daily_cap``：旧名 ``warmup_cap``
    把「满 ramp 后的日常额度」也说成「预热问题」，实测把排查方向带偏（老号
    被拦，运营去查预热配置）。**历史数据里旧值永远存在**（draft_audit_log /
    ops_events / 日志），所有分支消费方须同时认两个值——send_health.classify /
    send_gate_status / account_signals / 前端 fam 判定已同步；新消费方一律
    用「in {daily_cap, warmup_cap}」判断，勿只认单值。
    """
    # outbound.unlimited_mode：**预热期已满**的账号日额度是业务上限 → 放开
    # （target_cap 顶到哨兵值，over_cap 扣分随之消失，不会因「超额」把健康灯
    # 拖红再被 health_red 拦——那是同一条业务上限的连锁，不是风控信号）。
    # 预热期内账号仍按爬坡额度走（新号安全项），banned / health_red（限频、
    # 失败、代理、改资料等真风控信号）任何模式下都不放。
    unlimited_past_warmup = False
    orig_target_cap = int(target_cap)
    try:
        from src.ops.outbound_policy import UNLIMITED_CAP, is_unlimited
        if is_unlimited():
            age_days = float(signals.get("age_days") or 0.0)
            if age_days >= float(max(1, int(warmup_ramp_days))):
                unlimited_past_warmup = True
                target_cap = UNLIMITED_CAP
    except Exception:
        unlimited_past_warmup = False
    health = account_health(
        signals,
        target_cap=target_cap,
        warmup_start_cap=warmup_start_cap,
        warmup_ramp_days=warmup_ramp_days,
    )
    light = health["light"]
    sends_today = int(signals.get("sends_today") or 0)
    rec_cap = int(health["recommended_cap"])
    reserve = max(0, int(reserve_for_manual or 0))
    auto_cap = max(0, rec_cap - reserve)
    cap_for_origin = rec_cap if str(origin or "auto") == "manual" else auto_cap

    if bool(signals.get("banned", False)):
        reason, allowed = "banned", False
    elif block_on_red and light == "red":
        reason, allowed = "health_red", False
    elif sends_today >= cap_for_origin:
        reason, allowed = "daily_cap", False
    else:
        reason, allowed = "ok", True
    try:
        from src.ops.outbound_policy import record_block, record_unlimited_bypass
        if not allowed:
            record_block("safety" if reason != "daily_cap" else "business",
                         f"send_gate_{reason}")
        elif unlimited_past_warmup and sends_today >= max(0, orig_target_cap - reserve):
            # 只在「按原配置本会被拦」时才计一次 bypass，读数＝真实放行量
            record_unlimited_bypass("send_gate_daily_cap")
    except Exception:
        pass

    return {
        "allowed": allowed,
        "reason": reason,
        "light": light,
        "score": health["score"],
        "recommended_cap": rec_cap,
        "auto_cap": auto_cap,
        "reserve_for_manual": reserve,
        "health": health,
    }


def evaluate(
    signals: Dict[str, Any], config: Optional[Dict[str, Any]] = None,
    *, origin: str = "auto",
) -> Dict[str, Any]:
    """从 config 取阈值后做 ``gate_decision``（供两线发送路径调用的便捷入口）。

    闸门关闭时恒 ``allowed=True``（reason=``disabled``），保证零破坏。
    ``origin``：``auto``（缺省，A/B 自动链现有调用零改动自动绑定缩减额度）/
    ``manual``（人工路径用满额度；由收件箱发送路由与人工通过投递链显式传入）。
    """
    if not gate_enabled(config):
        return {"allowed": True, "reason": "disabled", "light": "green",
                "score": 100, "recommended_cap": 0, "auto_cap": 0,
                "reserve_for_manual": 0, "health": {}}
    gc = _gate_cfg(config)
    return gate_decision(
        signals,
        target_cap=int(gc.get("target_cap", 15) or 15),
        warmup_start_cap=int(gc.get("warmup_start_cap", 2) or 2),
        warmup_ramp_days=int(gc.get("warmup_ramp_days", 14) or 14),
        block_on_red=bool(gc.get("block_on_red", True)),
        reserve_for_manual=int(gc.get("reserve_for_manual", 0) or 0),
        origin=origin,
    )


def aggregate_fleet(
    accounts: List[Dict[str, Any]], config: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """两条线账号信号汇成机群健康概览（复用 M7 ``fleet_health``）。"""
    gc = _gate_cfg(config)
    return fleet_health(
        accounts,
        target_cap=int(gc.get("target_cap", 15) or 15),
        warmup_start_cap=int(gc.get("warmup_start_cap", 2) or 2),
        warmup_ramp_days=int(gc.get("warmup_ramp_days", 14) or 14),
    )


__all__ = ["gate_enabled", "gate_decision", "evaluate", "aggregate_fleet"]
