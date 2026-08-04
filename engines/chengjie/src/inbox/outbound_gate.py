"""统一「能不能对这条会话主动外呼」资格闸（``may_contact``）。

背景（2026-08-04 智拓机 / 192.168.0.198 事故）
================================================
一个做业务的 Telegram 新号刚登录，「目录同步」把手机上的历史会话（同行、bot、
群）全部灌进收件箱，主动触达调度器把这些「沉默几个月的老会话」当成「好久没聊的老
朋友」，从登录后第一个 15 分钟 tick 起挨个群发问候（一半带克隆语音）——陌生业务
联系人三句话识破 AI（"你成Ai了吗"）、向 Telegram 风控表演自动化行为、把试用字符
额度烧到 180%。根因之一：系统里**「账号接入多久」这个维度根本不存在**，任何「沉默
N 小时」判据对一个刚导入一堆老关系的新号都天然成立。

本模块是纯函数，收敛「新号冷启动 / 只有导入历史 / 额度耗尽」这三条**新增**准入判据，
主动话题 / 仪式 / 关怀 / 沉默回访共用。每个否决带稳定 ``reason`` 码，便于观测与门禁。

与既有护栏的关系（刻意不重复造轮子）
------------------------------------
``proactive_topic._conversations`` 已内联：群/频道过滤、系统 peer（Saved Messages/
官方服务号）、死 peer、占位会话（无消息方向）、opt-out、账号可发性。另一条线在建
bot / 舰队自嗨护栏（``peer_bot_guard`` / ``proactive_peer_hygiene``，尚未合并）。本闸
**只补它们没覆盖的三条新判据**，并留出委托点：下一阶段把上述散落判据逐步收进
``may_contact`` 成为唯一「出站前门」时，本文件即那个前门的骨架。

判据（本批 P0）
--------------
- ``cold_start_warming``：账号接入不足预热窗（默认 72h）→ 压该账号一切主动外呼。
  这是新号群发事故的**根因闸**。
- ``imported_no_inbound``：账号接入后对方从未在本系统开口（``last_in_ts`` 早于
  ``connected_at``）→ 只有导入的历史关系，要求对方先开口才允许冷开场。
- ``quota_exhausted``：授权/试用字符额度用尽 → 停**系统自主外呼**（人工发送不受限）。
  额度状态由调用方注入；给不出时按「未耗尽」放行（fail-open，对齐现网「未强制」语义）。

设计取舍
--------
- **只压主动外呼**：本闸只被主动触达链消费，绝不拦用户手动发送、也不拦对入站消息的
  自动回复（那两条是用户/对方明确意图，不是系统自作主张）。
- **fail-open**：``may_contact`` 只做布尔判定；调用点用 try/except 包裹，任何异常一律
  放行——安全闸自身不能变成新的「静默不发」故障源。主动外呼「漏一轮零代价」，但要
  拦住的是「新号群发」这种不可逆的封号/穿帮风险。
"""
from __future__ import annotations

import threading
from collections import namedtuple
from typing import Any, Dict, Mapping, Optional

# ── 稳定 reason 码 ──────────────────────────────────────────────────────────
OK = "ok"
DISABLED = "disabled"
COLD_START_WARMING = "cold_start_warming"
IMPORTED_NO_INBOUND = "imported_no_inbound"
QUOTA_EXHAUSTED = "quota_exhausted"

Verdict = namedtuple("Verdict", ["ok", "reason"])

_HOUR_SEC = 3600.0

# 默认值：安全优先。enabled 默认 True，即便部署 overlay（如 198 的
# config.local.internal.yaml）完全没提 cold_start，本闸也生效——安全floor 不依赖配置。
_DEFAULTS = {
    "enabled": True,
    "warmup_hours": 72.0,
    "require_inbound_since_connect": True,
    "quota_gate": True,
}


def resolve_cold_start_cfg(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """从 ``companion.proactive_topic.cold_start`` 解析配置，缺项取安全默认。

    容错：config 为 None / 结构缺失 / 值类型不对，一律回落默认（不抛）。
    ``warmup_hours`` 允许 0（=关掉预热窗那一层，仍保留其它两条判据）。
    """
    cs: Mapping[str, Any] = {}
    try:
        node = (((config or {}).get("companion") or {})
                .get("proactive_topic") or {}).get("cold_start")
        if isinstance(node, Mapping):
            cs = node
    except Exception:
        cs = {}
    out = dict(_DEFAULTS)
    if "enabled" in cs:
        out["enabled"] = bool(cs.get("enabled"))
    try:
        if cs.get("warmup_hours") is not None:
            out["warmup_hours"] = max(0.0, float(cs.get("warmup_hours")))
    except (TypeError, ValueError):
        pass
    if "require_inbound_since_connect" in cs:
        out["require_inbound_since_connect"] = bool(
            cs.get("require_inbound_since_connect"))
    if "quota_gate" in cs:
        out["quota_gate"] = bool(cs.get("quota_gate"))
    return out


def account_warming(connected_at: float, now: float, warmup_hours: float) -> bool:
    """账号是否仍在冷启动预热窗内（接入不足 ``warmup_hours`` 小时）。

    ``connected_at <= 0``（未知接入时刻）→ 保守判为「预热中」（宁可少发不误发；主动
    外呼漏一轮零代价，而新号群发不可逆）。``warmup_hours <= 0`` → 该层关闭，恒 False。
    """
    if warmup_hours <= 0:
        return False
    if connected_at <= 0:
        return True
    return (now - connected_at) < warmup_hours * _HOUR_SEC


def imported_no_inbound(last_in_ts: float, connected_at: float) -> bool:
    """该会话是否「只有导入历史、账号接入后对方从未开口」。

    ``connected_at <= 0``（接入时刻未知）→ 无法判定「是否接入后开口过」，交由
    ``account_warming`` 的保守分支兜底，这里返回 False（不叠加拦截）。
    否则：对方最后一次入站早于账号接入时刻（含从未入站 ``last_in_ts==0``）→ True。
    对方在接入后真的开过口（``last_in_ts >= connected_at``）→ False，属真实关系，
    允许沉默回访（正是主动触达要服务的场景）。
    """
    if connected_at <= 0:
        return False
    return float(last_in_ts or 0.0) < connected_at


def account_earliest_created(rows: Any) -> Dict[Any, float]:
    """从会话行列表推每个 ``(platform, account_id)`` 的最早 ``created_at``（epoch 秒）。

    这是账号「接入时刻」自举的取数逻辑（见 account_connection）。抽成纯函数以便直接
    单测，避免把这段循环埋进 proactive_topic 的闭包里（那是并发热区且难测）。
    行缺 ``created_at`` / 值非法 / <=0 一律跳过；返回 ``{(platform, account_id): 最早秒}``。
    """
    out: Dict[Any, float] = {}
    for r in rows or []:
        try:
            c = float(r.get("created_at") or 0.0)
        except (TypeError, ValueError, AttributeError):
            continue
        if c <= 0:
            continue
        k = (str(r.get("platform") or "telegram"),
             str(r.get("account_id") or "default"))
        if k not in out or c < out[k]:
            out[k] = c
    return out


def may_contact(
    row: Mapping[str, Any],
    *,
    connected_at: float,
    now: float,
    cfg: Mapping[str, Any],
    quota_exhausted: bool = False,
) -> Verdict:
    """主动外呼资格判定（纯函数）。

    ``row`` 至少含 ``last_in_ts``（对方最后一次入站 epoch 秒，缺=0）。判据顺序：
    额度 → 预热窗 → 导入无入站。任一否决即返回 ``Verdict(False, reason)``。
    """
    if not cfg.get("enabled", True):
        return Verdict(True, DISABLED)
    if cfg.get("quota_gate", True) and quota_exhausted:
        return Verdict(False, QUOTA_EXHAUSTED)
    if account_warming(connected_at, now, float(cfg.get("warmup_hours", 72.0))):
        return Verdict(False, COLD_START_WARMING)
    if cfg.get("require_inbound_since_connect", True):
        if imported_no_inbound(float(row.get("last_in_ts") or 0.0), connected_at):
            return Verdict(False, IMPORTED_NO_INBOUND)
    return Verdict(True, OK)


# ── 进程级观测（best-effort，重启清零；与 proactive_stats 解耦，零热区改动）────────
_LOCK = threading.Lock()
_SUPPRESSED: Dict[str, int] = {}


def record_suppression(reason: str) -> None:
    """记一次被本闸拦下的主动外呼（按 reason 计数）。绝不抛。"""
    if not reason:
        return
    try:
        with _LOCK:
            _SUPPRESSED[reason] = _SUPPRESSED.get(reason, 0) + 1
    except Exception:
        pass


def suppression_snapshot() -> Dict[str, int]:
    """当前累计抑制计数快照（reason -> 次数）。"""
    with _LOCK:
        return dict(_SUPPRESSED)


def reset_suppression_stats() -> None:
    """清零（测试与手动重置用）。"""
    with _LOCK:
        _SUPPRESSED.clear()


__all__ = [
    "OK", "DISABLED", "COLD_START_WARMING", "IMPORTED_NO_INBOUND",
    "QUOTA_EXHAUSTED", "Verdict",
    "resolve_cold_start_cfg", "account_warming", "imported_no_inbound",
    "account_earliest_created", "may_contact",
    "record_suppression", "suppression_snapshot", "reset_suppression_stats",
]
