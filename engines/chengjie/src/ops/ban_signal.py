"""G2 封号信号自动急停（反封号护栏三件套之二）。

发送命中平台风控错误时，按类型**分级处置**，不再硬怼到死：

| kind     | 触发（pyrogram/通用异常）                                   | 处置 |
|----------|------------------------------------------------------------|------|
| backoff  | FloodWait / SlowmodeWait（限速，非封号）                   | 仅退避冷却，**不**停号 |
| pause    | PeerFlood（被判垃圾邀约，可恢复）                          | 账号级 Kill-Switch + TTL 自动恢复 |
| ban      | UserDeactivated(Ban) / Unauthorized / AuthKeyUnregistered  | 账号级 Kill-Switch 永久 + 注册表 meta.banned |
| none     | 其它（含我们自己的 send_gate/kill_switch RuntimeError）     | 不处置（交既有熔断） |

**关键设计（复用 G1，不另造拦截路径）**：
- pause/ban 的「停发」直接用 ``kill_switch.set(account:<p>:<id>, ttl=...)`` 落地——
  G1 已把 is_blocked 接到 A/B（Phase C 起含 RPA）全发送路径、持久化、TTL 自动恢复、
  API 可见。故 G2 只需「分类 → set 账号闸」，无需教 gate 认 paused、也无视 gate 是否开。
- ``classify`` 是**纯函数**（按异常类名+属性，零 pyrogram 硬依赖），可注入假异常单测。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

# 异常类名 → 处置类别（按 pyrogram.errors 类名；用类名字符串避免硬依赖）
_BAN_NAMES = frozenset({
    "UserDeactivated", "UserDeactivatedBan", "UserBannedInChannel",
    "Unauthorized", "AuthKeyUnregistered", "AuthKeyDuplicated",
    "SessionRevoked", "SessionExpired", "UserBlocked",
})
# 对端侧错误：**对方**账号注销（400 INPUT_USER_DEACTIVATED）≠ 我方被风控。
# 2026-07-27 实锤：主动触达发到已注销测试号，撞下方 "DEACTIVAT" 关键词兜底被判
# kind=ban → 主账号被永久 kill-switch + 注册表误标 banned，全线停发 2h。
# 对端侧一律 none（peer 级拉黑由发送方自理，如 proactive _bad_peers）。
_PEER_SIDE_NAMES = frozenset({"InputUserDeactivated"})
_PEER_SIDE_MARKERS = ("INPUT_USER_DEACTIVATED", "INPUTUSERDEACTIVATED")
_PAUSE_NAMES = frozenset({"PeerFlood", "PeerIdInvalid", "ChatWriteForbidden"})
_BACKOFF_NAMES = frozenset({"FloodWait", "SlowmodeWait", "FloodPremiumWait"})

# 自己抛的控制流异常（不是平台风控）——明确归 none，避免误停
_OWN_PREFIXES = ("send_gate_blocked", "kill_switch_blocked")

DEFAULT_PAUSE_MINUTES = 60.0

# ── auth 族（会话/鉴权失效）与「登录变更窗口」降档（P0-1 2026-08-23，B57+B59）──
#
# 2026-08-23 升级风暴实锤：更新器换代期间旧后端未完全退出，新旧双进程抢同一份
# pyrogram 会话 → Telegram 强制注销（AuthKeyDuplicated/SessionRevoked）→ 发送异常
# 被本模块按 kind=ban **永久**冻结 + meta.banned。可 auth 族错误的含义只是
# 「这份会话凭证失效了」，与「账号被平台封禁」是两回事——凭证失效重登即活，
# 永久冻结反而把「重登后明明能用」的账号继续摁死（客户包 09:50 全账号发不出）。
#
# 三道疏导（都不影响 UserDeactivated* 等真封禁信号的处置）：
# 1. `in_login_flux_window`：进程刚启动（升级/重启后所有客户端重新鉴权，此时的
#    auth 错误大概率是自伤而非封号）或该账号刚登出/重登过 → auth 族降档为
#    pause + TTL（自动恢复），不判永久 ban、不标 meta.banned。
# 2. `note_login_change`：登录路由在登录成功/登出时打点，开启该账号的降档窗口。
# 3. `clear_auth_ban_on_login`：登录成功=最强的「没被封」证据 → 自动解除该账号
#    auth 族 auto_ban 冻结 + 清 meta.banned（真封禁的账号根本登不进来）。
_AUTH_FAMILY_NAMES = frozenset({
    "Unauthorized", "AuthKeyUnregistered", "AuthKeyDuplicated",
    "SessionRevoked", "SessionExpired",
})
_AUTH_FAMILY_MARKERS = (
    "UNAUTHORIZED", "AUTH_KEY", "AUTHKEY",
    "SESSION_REVOKED", "SESSIONREVOKED", "SESSION_EXPIRED", "SESSIONEXPIRED",
)
DEFAULT_LOGIN_FLUX_WINDOW_MIN = 15.0

_BOOT_TS = time.time()
_login_changes: Dict[str, float] = {}


def _flux_key(platform: str, account_id: str) -> str:
    return f"{str(platform or '').lower()}:{str(account_id or '')}"


def is_auth_family(reason: Any) -> bool:
    """纯函数：分类 reason / 异常名是否属会话鉴权失效族（≠真封禁）。"""
    r = str(reason or "")
    if not r:
        return False
    if r in _AUTH_FAMILY_NAMES:
        return True
    up = r.upper()
    return any(m in up for m in _AUTH_FAMILY_MARKERS)


def note_login_change(platform: str, account_id: str, *,
                      now: Optional[float] = None) -> None:
    """登录成功/登出打点：开启该账号的 auth 族降档窗口（进程内，绝不抛异常）。"""
    try:
        ts = float(now if now is not None else time.time())
        _login_changes[_flux_key(platform, account_id)] = ts
        if len(_login_changes) > 512:  # 防长期运行无界增长
            cutoff = ts - DEFAULT_LOGIN_FLUX_WINDOW_MIN * 60.0 * 4
            for k in [k for k, v in _login_changes.items() if v < cutoff]:
                _login_changes.pop(k, None)
    except Exception:
        pass


def in_login_flux_window(platform: str, account_id: str, *,
                         now: Optional[float] = None,
                         window_min: float = DEFAULT_LOGIN_FLUX_WINDOW_MIN) -> bool:
    """该账号是否处于「登录变更窗口」：进程刚启动 或 刚登出/重登。"""
    try:
        ts = float(now if now is not None else time.time())
        win = float(window_min) * 60.0
        if win <= 0:
            return False
        if 0 <= ts - _BOOT_TS < win:
            return True
        mark = _login_changes.get(_flux_key(platform, account_id))
        return mark is not None and 0 <= ts - mark < win
    except Exception:
        return False


def _exc_seconds(exc: Any) -> float:
    """从 FloodWait 类异常取需等待秒数（pyrogram 常见属性 value/x/seconds）。"""
    for attr in ("value", "x", "seconds"):
        try:
            v = getattr(exc, attr, None)
            if v is not None:
                return max(0.0, float(v))
        except (TypeError, ValueError):
            continue
    return 0.0


def classify(exc: Any) -> Dict[str, Any]:
    """纯函数：异常 → ``{kind, cooldown_sec, reason}``。

    kind ∈ {backoff, pause, ban, none}。未知/自家控制流异常 → none。
    """
    name = type(exc).__name__
    msg = str(exc or "")
    if any(msg.startswith(p) for p in _OWN_PREFIXES):
        return {"kind": "none", "cooldown_sec": 0.0, "reason": "own_control_flow"}
    _low_all = (name + " " + msg).upper()
    if name in _PEER_SIDE_NAMES or any(m in _low_all for m in _PEER_SIDE_MARKERS):
        return {"kind": "none", "cooldown_sec": 0.0, "reason": f"peer_side:{name}"}
    if name in _BACKOFF_NAMES:
        return {"kind": "backoff", "cooldown_sec": _exc_seconds(exc),
                "reason": f"{name}:{_exc_seconds(exc):.0f}s"}
    if name in _PAUSE_NAMES:
        return {"kind": "pause", "cooldown_sec": 0.0, "reason": name}
    if name in _BAN_NAMES:
        return {"kind": "ban", "cooldown_sec": 0.0, "reason": name}
    # 名字未精确命中时，按消息关键词兜底（不同 pyrogram 版本命名差异）
    low = (name + " " + msg).upper()
    if "FLOOD" in low and "WAIT" in low:
        return {"kind": "backoff", "cooldown_sec": _exc_seconds(exc), "reason": name}
    if "PEER_FLOOD" in low or "PEERFLOOD" in low:
        return {"kind": "pause", "cooldown_sec": 0.0, "reason": name}
    if "DEACTIVAT" in low or "UNAUTHORIZED" in low or "AUTH_KEY" in low or "BANNED" in low:
        return {"kind": "ban", "cooldown_sec": 0.0, "reason": name}
    return {"kind": "none", "cooldown_sec": 0.0, "reason": name}


def _audit_auto_freeze(platform: str, account_id: str, *, reason: str,
                       ttl_sec: float) -> None:
    """自动置位落 ops_events 审计（P1 2026-08-23，与手动置位同 kind）。

    此前自动冻结只有瞬时告警（webhook 默认 0 通道＝报进虚空），事后查不到
    「这号哪天被风控冻过」。best-effort：审计失败绝不影响急停本身。
    """
    try:
        from src.ops.ops_events import get_ops_event_store
        store = get_ops_event_store()
        if store is not None:
            store.record("kill_switch_set", platform=str(platform or ""),
                         account_id=str(account_id or ""), reason=str(reason or ""),
                         detail=f"scope=account:{str(platform or '').lower()}:"
                                f"{account_id} actor=ban_signal ttl_sec={ttl_sec:g}")
    except Exception:
        pass


def apply_action(
    platform: str,
    account_id: str,
    action: Dict[str, Any],
    *,
    kill_switch: Any,
    registry: Any = None,
    alert: Optional[Callable[..., Any]] = None,
    pause_minutes: float = DEFAULT_PAUSE_MINUTES,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """按分类结果落地处置（pause/ban 写账号级 Kill-Switch；ban 另标注册表）。

    返回 ``{applied, scope?}``。``kind=none/backoff`` 不动账号（backoff 交既有限速/熔断）。
    """
    kind = str((action or {}).get("kind") or "none")
    reason = str((action or {}).get("reason") or kind)
    scope = f"account:{str(platform or '').lower()}:{account_id}"
    out: Dict[str, Any] = {"applied": kind, "scope": scope}

    if kind in ("none", "backoff"):
        out["applied"] = kind
        return out

    if kind == "pause":
        ttl = float((action or {}).get("cooldown_sec") or 0) or pause_minutes * 60.0
        kill_switch.set(scope, reason=f"auto_pause:{reason}", actor="ban_signal",
                        ttl_sec=ttl, now=now)
        _audit_auto_freeze(platform, account_id,
                           reason=f"auto_pause:{reason}", ttl_sec=ttl)
        if alert:
            try:
                alert("account_paused",
                      {"platform": platform, "account_id": account_id},
                      f"自动暂停（{reason}），{ttl/60:.0f} 分钟后自动恢复")
            except Exception:
                pass
        out["ttl_sec"] = ttl
        return out

    # kind == "ban"：永久停 + 注册表标记（机群看板可见，待人工核查）
    kill_switch.set(scope, reason=f"auto_ban:{reason}", actor="ban_signal", ttl_sec=0,
                    now=now)
    _audit_auto_freeze(platform, account_id,
                       reason=f"auto_ban:{reason}", ttl_sec=0)
    if registry is not None:
        try:
            # merge_meta：锁内原子合并，防「读出→写回」窗口与其它写者互踩
            registry.upsert(
                platform, account_id,
                meta={"banned": True, "ban_reason": reason}, merge_meta=True)
        except Exception:
            pass
    if alert:
        try:
            alert("account_banned",
                  {"platform": platform, "account_id": account_id},
                  f"检测到封禁信号（{reason}），已永久冻结，待人工核查")
        except Exception:
            pass
    return out


def risk_kind_for(kind: str, reason: str) -> Optional[str]:
    """分类结果 → 24h 滚动计数的风控类别（喂 account_health 扣分轴）；无则 None。

    - backoff（FloodWait/Slowmode）/ pause（PeerFlood 家族）→ ``flood``（限频信号）。
    - none 且为**真实发送异常** → ``error``；我方控制流 / 对端注销一律不计（不误伤）。
    - ban → None：``meta.banned`` 已让健康分直接红灯 0 分，无需再计。
    """
    from src.ops.risk_events import KIND_ERROR, KIND_FLOOD
    if kind in ("backoff", "pause"):
        return KIND_FLOOD
    if kind == "none":
        r = str(reason or "")
        if r == "own_control_flow" or r.startswith("peer_side"):
            return None
        return KIND_ERROR
    return None


def handle_send_exception(
    platform: str,
    account_id: str,
    exc: Any,
    *,
    kill_switch: Any = None,
    registry: Any = None,
    alert: Optional[Callable[..., Any]] = None,
    pause_minutes: float = DEFAULT_PAUSE_MINUTES,
    now: Optional[float] = None,
    risk_recorder: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """发送异常的便捷处置入口：classify → 记 24h 风控计数 → apply。

    ``kill_switch`` 缺省时取进程单例。``risk_recorder`` 缺省走
    ``risk_events.record_risk_event``（可注入假对象单测）。反馈闭环：本函数是 A/B 两线
    发送异常的唯一入口，在此记 flood/error → ``build_account_signals`` 读回 →
    ``account_health`` 扣分 → 自动降 ``recommended_cap``。绝不抛异常（处置失败不应
    掩盖原始发送错误，记账失败更不应拖垮发送主链）。
    """
    try:
        action = classify(exc)
        # P0-1 登录变更窗口降档：升级/重启后所有客户端重新鉴权、账号刚登出/重登时，
        # auth 族错误九成是「会话凭证失效」而非封号 → pause+TTL 自动恢复，绝不永久
        # 冻结 + meta.banned（2026-08-23 升级风暴根子②）。真封禁族（UserDeactivated*）
        # 不在 auth 族，照旧永久处置。
        try:
            if (action.get("kind") == "ban"
                    and is_auth_family(action.get("reason", ""))
                    and in_login_flux_window(platform, account_id, now=now)):
                action = {"kind": "pause", "cooldown_sec": 0.0,
                          "reason": f"auth_flux:{action.get('reason', '')}"}
        except Exception:
            pass
        # 反封号反馈：先记 24h 滚动计数（含 none/backoff 早退分支，那正是 FloodWait 所在）
        try:
            rk = risk_kind_for(action["kind"], action.get("reason", ""))
            if rk:
                rec = risk_recorder
                if rec is None:
                    from src.ops.risk_events import record_risk_event as rec
                rec(platform, account_id, rk, now=now)
        except Exception:
            pass
        if action["kind"] in ("none", "backoff"):
            return {"applied": action["kind"], "kind": action["kind"]}
        ks = kill_switch
        if ks is None:
            from src.ops.kill_switch import get_kill_switch
            ks = get_kill_switch()
        res = apply_action(platform, account_id, action, kill_switch=ks,
                           registry=registry, alert=alert,
                           pause_minutes=pause_minutes, now=now)
        res["kind"] = action["kind"]
        res["reason"] = action["reason"]
        return res
    except Exception:
        return {"applied": "error", "kind": "none"}


def clear_auth_ban_on_login(
    platform: str,
    account_id: str,
    *,
    kill_switch: Any = None,
    registry: Any = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """账号登录成功后调用：解除该账号 **auth 族**自动冻结 + 清 meta.banned。

    登录成功=最强的「没被封」证据（真被平台封禁的账号根本登不进来）。只解
    **自动置位**（actor=ban_signal / reason 带 auto_ban:/auto_pause: 前缀）且原因
    属 auth 族（会话凭证失效）的冻结；管理员手动冻结与真封禁信号
    （UserDeactivated* 等）一律不动。顺带 ``note_login_change`` 开启降档窗口
    （重登后残留客户端可能还会再抛几个旧会话错误）。绝不抛异常。
    """
    scope = f"account:{str(platform or '').lower()}:{str(account_id or '')}"
    out: Dict[str, Any] = {"cleared": False, "meta_cleared": False, "scope": scope}
    note_login_change(platform, account_id, now=now)
    try:
        ks = kill_switch
        if ks is None:
            from src.ops import kill_switch as ks_mod
            ks = ks_mod._singleton  # 只读既有单例：登录路径绝不按 CWD 新建库
        if ks is not None:
            rec = None
            for r in ks.status(now=now):
                if r.get("scope") == scope:
                    rec = r
                    break
            if rec is not None:
                actor = str(rec.get("actor") or "").strip().lower()
                reason = str(rec.get("reason") or "")
                low = reason.lower()
                is_auto = (actor == "ban_signal"
                           or low.startswith("auto_ban:")
                           or low.startswith("auto_pause:"))
                if is_auto and is_auth_family(reason):
                    ks.clear(scope)
                    out["cleared"] = True
                    out["was_reason"] = reason
                    try:
                        from src.ops.ops_events import get_ops_event_store
                        store = get_ops_event_store()
                        if store is not None:
                            store.record(
                                "kill_switch_clear",
                                platform=str(platform or ""),
                                account_id=str(account_id or ""),
                                reason="login_success_auto_unban",
                                detail=f"scope={scope} was={reason}")
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        reg = registry
        if reg is None:
            from src.integrations.account_registry import get_account_registry
            reg = get_account_registry()
        row = reg.get(platform, account_id) if reg is not None else None
        meta = (row or {}).get("meta") or {}
        if meta.get("banned") and is_auth_family(meta.get("ban_reason", "")):
            reg.upsert(platform, account_id,
                       meta={"banned": False, "ban_reason": ""}, merge_meta=True)
            out["meta_cleared"] = True
    except Exception:
        pass
    return out


__all__ = ["classify", "apply_action", "handle_send_exception",
           "risk_kind_for", "is_auth_family", "note_login_change",
           "in_login_flux_window", "clear_auth_ban_on_login",
           "DEFAULT_PAUSE_MINUTES", "DEFAULT_LOGIN_FLUX_WINDOW_MIN"]
