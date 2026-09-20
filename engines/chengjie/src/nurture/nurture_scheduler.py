"""智能养号 · 纯调度核心（P1，2026-08-21）。

给定「每号养护方案 + 账号信号 + 账本快照 + 当前时刻 + 全局配置」→ 产出**本 tick 到期的
养护动作清单**。全部纯函数（零 IO / 零副作用 / 确定性），engine 层负责真执行。

设计（对齐 proactive 节奏哲学，但更克制——养号是主动自动化，风险高）：
- **只在活动时段窗内动作**（默认 9-11 / 13-14 / 18-21，模拟真人分时段上线），安静时段不动；
- **档位定日预算**（保守/平衡/激进），warming 账号按天龄线性缩放（新号更少动作）；
- **生命周期风险态跳过**（banned/restricted/pending/offline 一律不排）；
- **动作间隔闸**（min_gap，按档位）+ 确定性抖动（crc32，同号同日恒定、跨 tick 稳定）；
- **行为按启用开关加权轮换**（read/self_chat/browse/react/online），确定性选取。

engine 只需：为每个 tick 调 ``plan_due_actions``，对返回的每条动作做 dry_run 影子记录或
go_live 真派发。scheduler 不碰网络、不碰账号、不知道 dry_run/go_live——那是 engine 的事。
"""
from __future__ import annotations

import zlib
from datetime import datetime
from typing import Any, Dict, List, Optional

# 档位节奏表：日动作预算 + 动作最小间隔（分钟）。养号刻意比 proactive 更慢更少。
PROFILE_CADENCE: Dict[str, Dict[str, int]] = {
    "conservative": {"daily_budget": 8, "min_gap_min": 60},
    "balanced": {"daily_budget": 15, "min_gap_min": 40},
    "aggressive": {"daily_budget": 25, "min_gap_min": 25},
}
_DEFAULT_PROFILE = "balanced"

# 行为→默认权重（启用的行为里按权重确定性轮换）。read 最高频（最安全、纯行为信号），
# self_chat 低频（会真发消息，最谨慎）。
_BEHAVIOR_WEIGHT: Dict[str, int] = {
    "read": 5, "browse": 3, "react": 2, "online": 2, "self_chat": 1,
}
_BEHAVIOR_ORDER = ("read", "browse", "react", "online", "self_chat")

# 生命周期风险态：一律不排养护动作（正在受限/封禁的号别再去撩平台）。
_SKIP_STAGES = frozenset({"banned", "restricted", "pending", "offline"})


def _crc(*parts: Any) -> int:
    return zlib.crc32(("#".join(str(p) for p in parts)).encode("utf-8")) & 0xFFFFFFFF


def parse_hours_windows(cfg: Optional[Dict[str, Any]]) -> List[List[int]]:
    """解析活动时段窗 ``hours``（形如 ["9-11","13-14","18-21"]）→ [[9,11],...]。

    缺省=真人三时段（上午/午间/晚间）。非法项跳过；空=全天允许（[[0,24]]）。
    """
    raw = (cfg or {}).get("hours")
    if raw is None:
        raw = ["9-11", "13-14", "18-21"]
    out: List[List[int]] = []
    for item in raw or []:
        try:
            s, e = str(item).split("-", 1)
            a, b = int(s), int(e)
            if 0 <= a < b <= 24:
                out.append([a, b])
        except Exception:
            continue
    return out or [[0, 24]]


def in_active_window(now: float, windows: List[List[int]]) -> bool:
    """当前本地小时是否落在任一活动时段窗内。"""
    try:
        h = datetime.fromtimestamp(float(now)).hour
    except Exception:
        return False
    return any(a <= h < b for a, b in (windows or []))


def profile_of(plan: Dict[str, Any]) -> str:
    p = str((plan or {}).get("profile") or _DEFAULT_PROFILE).lower()
    return p if p in PROFILE_CADENCE else _DEFAULT_PROFILE


def daily_budget(plan: Dict[str, Any], signals: Optional[Dict[str, Any]]) -> int:
    """该号今日动作预算：档位基准 × warming 天龄缩放（active/成熟号不缩放）。"""
    cad = PROFILE_CADENCE[profile_of(plan)]
    base = int(cad["daily_budget"])
    sig = signals or {}
    stage = str(sig.get("stage") or "").lower()
    if stage == "warming":
        try:
            age = float(sig.get("age_days") or 0.0)
            ramp = float((plan or {}).get("ramp_days") or 0) or 14.0
            frac = max(0.15, min(1.0, age / max(1.0, ramp)))
        except Exception:
            frac = 0.5
        return max(1, int(round(base * frac)))
    return base


def min_gap_sec(plan: Dict[str, Any], key: str, now: float) -> float:
    """动作最小间隔（秒）：档位基准 + 确定性抖动 ±25%（同号同日恒定，防机械等间隔）。"""
    base_min = float(PROFILE_CADENCE[profile_of(plan)]["min_gap_min"])
    try:
        day = datetime.fromtimestamp(float(now)).strftime("%Y%m%d")
    except Exception:
        day = "0"
    jf = (_crc(key, day, "gap") % 1000) / 1000.0  # 0..1
    factor = 0.75 + jf * 0.5  # 0.75..1.25
    return base_min * 60.0 * factor


def enabled_behaviors(plan: Dict[str, Any], cfg: Optional[Dict[str, Any]]) -> List[str]:
    """该号启用的行为（plan.behaviors 里为真的）；self_chat 额外受全局
    ``self_chat.enabled`` 总闸约束（未开则从池中剔除——只规划不含 self_chat）。"""
    beh = (plan or {}).get("behaviors") or {}
    self_chat_on = bool(((cfg or {}).get("self_chat") or {}).get("enabled", False))
    out = []
    for b in _BEHAVIOR_ORDER:
        if not bool(beh.get(b, False)):
            continue
        if b == "self_chat" and not self_chat_on:
            continue
        out.append(b)
    return out


def choose_behavior(behaviors: List[str], key: str, now: float, seq: int) -> str:
    """确定性加权轮换选一个行为（同号同日同序号恒定，跨 tick 稳定可复现）。"""
    pool: List[str] = []
    for b in behaviors:
        pool.extend([b] * max(1, int(_BEHAVIOR_WEIGHT.get(b, 1))))
    if not pool:
        return ""
    try:
        day = datetime.fromtimestamp(float(now)).strftime("%Y%m%d")
    except Exception:
        day = "0"
    return pool[_crc(key, day, "beh", seq) % len(pool)]


def parse_risk_backoff_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``ops.nurture.risk_backoff``（默认**开**——安全默认）：正被平台节流/报错的号退避养护。"""
    rb = (cfg or {}).get("risk_backoff")
    rb = rb if isinstance(rb, dict) else {}
    return {
        "enabled": bool(rb.get("enabled", True)),
        "flood_threshold": int(rb.get("flood_threshold", 1) or 1),
        "error_threshold": int(rb.get("error_threshold", 5) or 5),
    }


def risk_backoff(signals: Optional[Dict[str, Any]], cfg: Optional[Dict[str, Any]]) -> Optional[str]:
    """账号正被平台节流(FLOOD_WAIT)/高频报错时**退避养护**——别给已受限的号再加活动
    （否则养号反而催封）。返回退避原因（risk_flood/risk_errors）或 None（可养）。

    信号来自 ``build_account_signals``：``flood_waits_24h`` / ``errors_24h``。默认开=安全默认；
    信号恢复（24h 窗滚过）→ 自动恢复养护（自愈，不写持久降档）。
    """
    rc = parse_risk_backoff_cfg(cfg)
    if not rc["enabled"]:
        return None
    sig = signals or {}
    try:
        if int(sig.get("flood_waits_24h") or 0) >= rc["flood_threshold"]:
            return "risk_flood"
    except Exception:
        pass
    try:
        if int(sig.get("errors_24h") or 0) >= rc["error_threshold"]:
            return "risk_errors"
    except Exception:
        pass
    return None


def account_status(
    acct: Dict[str, Any],
    ledger: Dict[str, Dict[str, Any]],
    signals: Optional[Dict[str, Any]],
    now: float,
    cfg: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """单号「此刻会不会被养 + 为什么」的可解释快照（纯函数，供 CLI/看板照单说明）。

    与 ``plan_due_actions`` 的闸门逐条同源；``reason=""`` 且 ``would_act=True`` 表示到期可养，
    附 ``next_kind``。其余 reason：disabled/off_hours/stage_*/banned/circuit_open/
    risk_flood/risk_errors/no_behaviors/budget_exhausted/min_gap。
    """
    plan = (acct or {}).get("plan") or {}
    key = str(acct.get("key") or "")
    sig = signals or {}
    led = (ledger or {}).get(key) or {}
    count_today = int(led.get("count_today") or 0)
    budget = daily_budget(plan, sig)
    out = {"key": key, "profile": profile_of(plan), "enabled": bool(plan.get("enabled", False)),
           "count_today": count_today, "budget": budget, "stage": str(sig.get("stage") or ""),
           "would_act": False, "reason": "", "next_kind": ""}

    def _set(reason: str, next_kind: str = "") -> Dict[str, Any]:
        out["reason"] = reason or "due"
        out["would_act"] = (reason == "")
        out["next_kind"] = next_kind
        return out

    if not bool(plan.get("enabled", False)):
        return _set("disabled")
    if not in_active_window(now, parse_hours_windows(cfg)):
        return _set("off_hours")
    stage = str(sig.get("stage") or "").lower()
    if stage in _SKIP_STAGES:
        return _set("stage_" + stage)
    if bool(sig.get("banned")):
        return _set("banned")
    if bool(sig.get("circuit_open")):
        return _set("circuit_open")
    rb = risk_backoff(sig, cfg)
    if rb:
        return _set(rb)
    behs = enabled_behaviors(plan, cfg)
    if not behs:
        return _set("no_behaviors")
    if count_today >= budget:
        return _set("budget_exhausted")
    last_ts = float(led.get("last_ts") or 0.0)
    if last_ts and (now - last_ts) < min_gap_sec(plan, key, now):
        return _set("min_gap")
    return _set("", choose_behavior(behs, key, now, count_today))


def lint_nurture_config(
    cfg: Optional[Dict[str, Any]],
    registry_keys: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """养号配置防呆（纯函数）：检出 go_live 常见误配，返回 findings 供 UI/CLI 照单提示。

    ``registry_keys``：在册账号 key 集（``platform:account_id``）——给了才查「陈旧配置号」
    （配了养护但号已不在册）。findings 只带机器码 ``code`` + ``severity`` + 可选 ``key``，
    文案由消费面本地化（前端 cp.nurture.warn_*）。检出项：

    - ``go_live_no_canary``（warn）：正式启用但金丝雀名单空 → 零真动作（点了以为在养其实没养）；
    - ``canary_stale``（warn）：金丝雀号未在 accounts 配养护方案；
    - ``canary_disabled``（warn）：金丝雀号方案未启用（enabled=false）→ 不会被养；
    - ``risk_backoff_off``（warn）：风险退避被关（受限号仍会被养，催封风险）；
    - ``no_enabled_accounts``（info）：引擎开着但无启用账号 → 无号可养；
    - ``self_chat_gated``（info）：有号勾了自号互聊但全局 self_chat 总闸关 → 不会自聊；
    - ``stale_account``（warn）：配了养护但号不在注册表（需 registry_keys）。
    """
    cfg = cfg or {}
    findings: List[Dict[str, Any]] = []
    enabled = bool(cfg.get("enabled", False))
    dry_run = bool(cfg.get("dry_run", True))
    accts = cfg.get("accounts") or {}
    accts = accts if isinstance(accts, dict) else {}
    canary = [str(x) for x in (cfg.get("canary_accounts") or [])]
    self_chat_on = bool((cfg.get("self_chat") or {}).get("enabled", False))
    rb_on = parse_risk_backoff_cfg(cfg)["enabled"]
    enabled_keys = {str(k) for k, v in accts.items()
                    if isinstance(v, dict) and v.get("enabled")}

    if enabled and not dry_run and not canary:
        findings.append({"code": "go_live_no_canary", "severity": "warn"})
    for c in canary:
        if c not in accts:
            findings.append({"code": "canary_stale", "severity": "warn", "key": c})
        elif c not in enabled_keys:
            findings.append({"code": "canary_disabled", "severity": "warn", "key": c})
    if not rb_on:
        findings.append({"code": "risk_backoff_off", "severity": "warn"})
    if enabled and not enabled_keys:
        findings.append({"code": "no_enabled_accounts", "severity": "info"})
    any_self = any(isinstance(v, dict) and (v.get("behaviors") or {}).get("self_chat")
                   for v in accts.values())
    if any_self and not self_chat_on:
        findings.append({"code": "self_chat_gated", "severity": "info"})
    if registry_keys is not None:
        known = {str(k) for k in registry_keys}
        for k in accts.keys():
            if str(k) not in known:
                findings.append({"code": "stale_account", "severity": "warn", "key": str(k)})
    return findings


def plan_due_actions(
    accounts: List[Dict[str, Any]],
    ledger: Dict[str, Dict[str, Any]],
    signals_map: Dict[str, Dict[str, Any]],
    now: float,
    cfg: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """本 tick 到期养护动作清单（纯函数）。

    ``accounts``：[{key, platform, account_id, plan{enabled,profile,ramp_days,behaviors}}]
    ``ledger``：{key: {"count_today": int, "last_ts": float}}（engine 从持久账本构造）
    ``signals_map``：{key: {stage, age_days, banned, circuit_open}}
    返回：[{key, platform, account_id, kind, profile, reason}]（engine 据此 dry/go_live）

    只排「到期且未超预算且过间隔且在活动窗」的号，每号每 tick 至多一条动作
    （细水长流，不爆发）。总量由 engine 的 max_actions_per_tick 再兜一层。
    """
    windows = parse_hours_windows(cfg)
    if not in_active_window(now, windows):
        return []
    out: List[Dict[str, Any]] = []
    for acct in accounts or []:
        plan = (acct or {}).get("plan") or {}
        if not bool(plan.get("enabled", False)):
            continue
        key = str(acct.get("key") or "")
        if not key:
            continue
        sig = signals_map.get(key) or {}
        if str(sig.get("stage") or "").lower() in _SKIP_STAGES:
            continue
        if bool(sig.get("banned")) or bool(sig.get("circuit_open")):
            continue
        if risk_backoff(sig, cfg):
            continue  # 正被平台节流/报错 → 退避（别催封）
        behs = enabled_behaviors(plan, cfg)
        if not behs:
            continue
        led = ledger.get(key) or {}
        count_today = int(led.get("count_today") or 0)
        budget = daily_budget(plan, sig)
        if count_today >= budget:
            continue
        last_ts = float(led.get("last_ts") or 0.0)
        if last_ts and (now - last_ts) < min_gap_sec(plan, key, now):
            continue
        kind = choose_behavior(behs, key, now, count_today)
        if not kind:
            continue
        out.append({
            "key": key,
            "platform": str(acct.get("platform") or ""),
            "account_id": str(acct.get("account_id") or ""),
            "kind": kind,
            "profile": profile_of(plan),
            "reason": f"nurture:{profile_of(plan)}:{count_today + 1}/{budget}",
        })
    return out


__all__ = [
    "PROFILE_CADENCE",
    "parse_hours_windows",
    "in_active_window",
    "profile_of",
    "daily_budget",
    "min_gap_sec",
    "enabled_behaviors",
    "choose_behavior",
    "parse_risk_backoff_cfg",
    "risk_backoff",
    "account_status",
    "lint_nurture_config",
    "plan_due_actions",
]
