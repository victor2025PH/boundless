"""player_sync（B3）：定时把活跃联系人的网关资料拉一遍写画像，充值 / 沉默事件起 goals。

仿 ``src/inbox/health_watchdog.py::_check_goal_order_pull``——巡检 ``_tick`` 里稀疏节流
调 ``run_player_sync``，一切失败吃掉不影响巡检主流程；story_matrix 实例不装本域 → 从不跑。

一轮做三件事：
1. ``dormant_sweep``：沉默 > N 天的画像置 dormant；每个**新**置入的起一个 ``player_reengage`` 目标；
2. 活跃联系人（last_seen 在 ``active_days`` 内、有手机号 / UID、上次 lookup 距今 ≥ interval）
   逐个 ``gateway.lookup`` → ``PlayerProfileService.record_sync``；事实里**新**出现充值记录 → 起
   ``player_after_deposit`` 目标；
3. 目标护栏：``companion.goals.enabled`` 关则一律不建；同会话有 active 目标不重建；每日预算
   ``max_goals_per_day``；模板 push_curve 全 none（见 goal_templates.py）。

配置 ``player_care.sync.{enabled,interval_min,active_days,batch,goals.{enabled,autonomy,max_per_day}}``。
网关数字只原样落 facts_text，不算、不推。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .commandbus import CommandOutbox, get_outbox, reengage_pool, resolve_commandbus_cfg, send_reengage
from .gateway import (
    PlayerGateway,
    extract_agent,
    extract_games,
    friend_facts_text,
    has_deposit,
    resolve_gateway_cfg,
)
from .goal_templates import (
    TEMPLATE_AFTER_DEPOSIT, TEMPLATE_REENGAGE, register_goal_templates,
)
from .profile import PlayerProfileService, get_profile_service

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MIN = 30.0
MIN_INTERVAL_MIN = 5.0
CREATED_BY = "player_sync"

_last_summary: Dict[str, Any] = {}


def last_sync_summary() -> Dict[str, Any]:
    """本进程最近一轮 ``run_player_sync`` 的摘要（含 ``ts``），没跑过则 ``{}``。"""
    return dict(_last_summary)


def resolve_sync_cfg(cfg_root: Any) -> Dict[str, Any]:
    root = cfg_root
    if hasattr(root, "config"):
        root = getattr(root, "config") or {}
    if not isinstance(root, dict):
        root = {}
    pc = root.get("player_care") if isinstance(root.get("player_care"), dict) else {}
    sc = pc.get("sync") if isinstance(pc.get("sync"), dict) else {}
    gc = sc.get("goals") if isinstance(sc.get("goals"), dict) else {}

    def _num(d: Dict[str, Any], k: str, default: float) -> float:
        try:
            v = float(d.get(k, default))
            return v if v > 0 else default
        except Exception:
            return default

    return {
        "enabled": bool(sc.get("enabled", True)),
        "interval_min": max(MIN_INTERVAL_MIN, _num(sc, "interval_min", DEFAULT_INTERVAL_MIN)),
        "active_days": _num(sc, "active_days", 7),
        "batch": int(_num(sc, "batch", 50)),
        "goals": {
            "enabled": bool(gc.get("enabled", True)),
            "autonomy": str(gc.get("autonomy") or "auto").strip().lower(),
            "max_per_day": int(_num(gc, "max_per_day", 20)),
            "reengage_days": _num(gc, "reengage_days", 7),
            "after_deposit_days": _num(gc, "after_deposit_days", 3),
        },
    }


def _goals_store(cfg_root: Any, config_path: Any):
    try:
        from src.companion.goals.service import get_configured_store, goals_enabled
    except Exception:
        return None
    root = getattr(cfg_root, "config", cfg_root)
    if not goals_enabled(root):
        return None
    try:
        return get_configured_store(root, config_path)
    except Exception:
        logger.debug("[player_sync] goals store 不可用", exc_info=True)
        return None


def _conv_id(row: Dict[str, Any]) -> str:
    return f"{row.get('platform') or ''}:{row.get('account_id') or ''}:{row.get('external_id') or ''}"


def _day_start(ts: float) -> float:
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d", time.localtime(ts)), "%Y-%m-%d"))


def maybe_create_player_goal(store: Any, row: Dict[str, Any], template: str, *,
                             gcfg: Dict[str, Any], now: float,
                             params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """给一条画像起目标（幂等 + 预算）；任何失败 → None。"""
    if store is None or not gcfg.get("enabled", True):
        return None
    platform = str(row.get("platform") or "")
    account_id = str(row.get("account_id") or "")
    chat_key = str(row.get("external_id") or "")
    if not (platform and chat_key):
        return None
    conv = _conv_id(row)
    try:
        if store.find_active_goal(conversation_id=conv, platform=platform,
                                  chat_key=chat_key, account_id=account_id):
            return None
        if store.count_created_by_since(CREATED_BY, _day_start(now)) >= int(gcfg.get("max_per_day") or 20):
            return None
        days = float(gcfg.get("reengage_days" if template == TEMPLATE_REENGAGE else "after_deposit_days") or 0)
        goal = store.create_goal(
            conversation_id=conv, platform=platform, account_id=account_id, chat_key=chat_key,
            template=template, params=dict(params or {}), autonomy=str(gcfg.get("autonomy") or "auto"),
            priority=1, deadline_days=days, created_by=CREATED_BY, now=now,
        )
        if goal:
            logger.info("[player_sync] 起目标 %s tmpl=%s conv=%s", goal.get("goal_id"), template, conv)
        return goal
    except Exception:
        logger.debug("[player_sync] 建目标失败（忽略）", exc_info=True)
        return None


def run_player_sync(cfg_root: Any, config_path: Any = None, *,
                    gateway: Optional[PlayerGateway] = None,
                    profile: Optional[PlayerProfileService] = None,
                    goal_store: Any = None,
                    outbox: Optional[CommandOutbox] = None,
                    now: Optional[float] = None,
                    sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    """跑一轮同步，返回摘要 ``{scanned, looked_up, found, deposits, dormant, goals, commands, errors, skipped}``。"""
    ts = float(now if now is not None else time.time())
    summary: Dict[str, Any] = {"scanned": 0, "looked_up": 0, "found": 0, "deposits": 0,
                               "dormant": 0, "goals": 0, "commands": 0, "errors": 0, "skipped": ""}
    try:
        return _run_player_sync(cfg_root, config_path, summary, ts, gateway=gateway, profile=profile,
                                goal_store=goal_store, outbox=outbox, sleep=sleep)
    finally:
        _last_summary.clear()
        _last_summary.update(summary, ts=int(ts))


def _run_player_sync(cfg_root: Any, config_path: Any, summary: Dict[str, Any], ts: float, *,
                     gateway: Optional[PlayerGateway], profile: Optional[PlayerProfileService],
                     goal_store: Any, outbox: Optional[CommandOutbox],
                     sleep: Callable[[float], None]) -> Dict[str, Any]:
    scfg = resolve_sync_cfg(cfg_root)
    if not scfg["enabled"]:
        summary["skipped"] = "disabled"
        return summary
    svc = profile or get_profile_service(cfg_root)
    if svc is None:
        summary["skipped"] = "no_profile_store"
        return summary
    register_goal_templates()
    store = goal_store if goal_store is not None else _goals_store(cfg_root, config_path)
    gcfg = scfg["goals"]
    cbcfg = resolve_commandbus_cfg(cfg_root)
    ob = outbox if outbox is not None else (get_outbox(cfg_root) if cbcfg["enabled"] else None)

    # 1) 沉默 → dormant → 轻触目标（+ B4：有手机号且 commandbus 开着 → 给手机侧发 reengage 指令）
    for row in svc.dormant_sweep(now=ts):
        summary["dormant"] += 1
        if maybe_create_player_goal(store, row, TEMPLATE_REENGAGE, gcfg=gcfg, now=ts,
                                    params={"stage_before": str(row.get("stage_before_dormant") or "")}):
            summary["goals"] += 1
        if ob is not None and cbcfg["reengage_on_dormant"] and str(row.get("phone_e164") or "").strip():
            env = send_reengage(ob, account=str(row.get("account_id") or ""),
                                phone=str(row.get("phone_e164") or ""),
                                messages=reengage_pool(str(row.get("reply_lang") or "")),
                                reason="dormant", now=ts)
            if env is not None:
                summary["commands"] += 1

    # 2) 活跃联系人刷网关事实
    gw = gateway
    if gw is None:
        root = getattr(cfg_root, "config", cfg_root)
        gw = PlayerGateway(resolve_gateway_cfg(root))
    if not gw.configured:
        summary["skipped"] = "gateway_unconfigured"
        return summary
    since = int(ts - scfg["active_days"] * 86400)
    gap = scfg["interval_min"] * 60.0
    rows = svc.store.list_player_profiles(seen_since=since, limit=max(1, scfg["batch"]) * 4)
    done = 0
    for row in rows:
        if done >= scfg["batch"]:
            break
        phone, uid = str(row.get("phone_e164") or ""), str(row.get("uid") or "")
        if not (phone or uid):
            continue
        if ts - float(row.get("last_lookup_at") or 0) < gap:
            continue
        summary["scanned"] += 1
        try:
            res = gw.lookup("player info", phone=phone, uid=uid)
        except Exception:
            logger.debug("[player_sync] lookup 异常", exc_info=True)
            summary["errors"] += 1
            continue
        done += 1
        summary["looked_up"] += 1
        found = bool(res.usable)
        if not found and not res.ok:
            summary["errors"] += 1
        facts = {"found": found, "text": friend_facts_text(res) if found else "",
                 "games": extract_games(res) if found else [],
                 "error": res.error if not found else ""}
        if found:
            facts["deposit"] = has_deposit(res)
            agent = extract_agent(res)
            if agent:
                facts["agent"] = agent
        try:
            out = svc.record_sync(row["profile_key"], facts, now=ts)
        except Exception:
            logger.debug("[player_sync] record_sync 失败", exc_info=True)
            summary["errors"] += 1
            continue
        if found:
            summary["found"] += 1
        if "deposit" in out["events"]:
            summary["deposits"] += 1
            if maybe_create_player_goal(store, out["row"] or row, TEMPLATE_AFTER_DEPOSIT, gcfg=gcfg, now=ts):
                summary["goals"] += 1
        if done < scfg["batch"] and len(rows) > 1:
            sleep(0.05)  # 轻微错峰，别打爆网关
    return summary
