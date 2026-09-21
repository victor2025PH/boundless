"""B6 看板数据：/player-care/overview 用的一份 JSON 摘要。

只读聚合，不查网关、不发指令；画像库 / 出箱打不开就给空计数 + ``degraded`` 说明，绝不抛。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from .commandbus import CommandOutbox, get_outbox, resolve_commandbus_cfg
from .gateway import resolve_gateway_cfg
from .profile import STAGES, PlayerProfileService, day_key, get_profile_service, resolve_profile_cfg
from .sync import last_sync_summary, resolve_sync_cfg

logger = logging.getLogger(__name__)

DAY_SEC = 86400
LOOKUP_WINDOW_SEC = DAY_SEC

GW_UNCONFIGURED = "unconfigured"
GW_IDLE = "idle"
GW_OK = "ok"
GW_DEGRADED = "degraded"
GW_DOWN = "down"


def gateway_status(configured: bool, recent: Dict[str, Any]) -> str:
    """按最近 24h 各画像最后一次 lookup 的结果判健康：
    未配置 → unconfigured；没查过 → idle；传输/鉴权错占比 ≥50% → down；有错 → degraded；否则 ok。"""
    if not configured:
        return GW_UNCONFIGURED
    total = int(recent.get("total") or 0)
    if total <= 0:
        return GW_IDLE
    errs = sum(int(v) for v in (recent.get("errors") or {}).values())
    if errs <= 0:
        return GW_OK
    return GW_DOWN if errs * 2 >= total else GW_DEGRADED


def _stage_totals(by_account: Dict[str, Dict[str, int]]) -> Dict[str, int]:
    out = {s: 0 for s in STAGES}
    for stages in by_account.values():
        for s, n in stages.items():
            out[s] = out.get(s, 0) + int(n)
    return out


def build_overview(cfg_root: Any, *, now: Optional[float] = None,
                   profile: Optional[PlayerProfileService] = None,
                   outbox: Optional[CommandOutbox] = None) -> Dict[str, Any]:
    ts = float(now if now is not None else time.time())
    day = day_key(ts)
    notes: List[str] = []

    gcfg = resolve_gateway_cfg(cfg_root)
    gw_configured = bool(gcfg["enabled"] and gcfg["url"] and gcfg["key"])
    scfg = resolve_sync_cfg(cfg_root)
    pcfg = resolve_profile_cfg(cfg_root)
    cbcfg = resolve_commandbus_cfg(cfg_root)

    out: Dict[str, Any] = {
        "ok": True,
        "ts": int(ts),
        "day": day,
        "contacts": {"total": 0, "with_phone": 0, "handoff": 0, "active_7d": 0, "by_account": {}},
        "stages": {"order": list(STAGES), "totals": {s: 0 for s in STAGES}, "by_account": {}},
        "today": {"inbound": 0, "lookups": 0, "found": 0, "visible": 0, "gate_hits": 0,
                  "new_profiles": 0, "stage_ups": 0, "accounts": []},
        "gateway": {
            "status": GW_UNCONFIGURED if not gw_configured else GW_IDLE,
            "enabled": bool(gcfg["enabled"]),
            "url": gcfg["url"],
            "key_set": bool(gcfg["key"]),
            "key_env": gcfg["key_env"],
            "timeout_sec": gcfg["timeout_sec"],
            "lookups_total": 0,
            "last_lookup_at": 0,
            "recent_24h": {"total": 0, "found": 0, "not_found": 0, "errors": {}},
        },
        "sync": {"enabled": bool(scfg["enabled"]), "interval_min": scfg["interval_min"],
                 "last": last_sync_summary()},
        "commandbus": {"enabled": bool(cbcfg["enabled"]), "stats": None},
        "profile_enabled": bool(pcfg["enabled"]),
        "notes": notes,
    }

    svc = profile
    if svc is None:
        try:
            svc = get_profile_service(cfg_root)
        except Exception:
            logger.debug("[player_care] overview: 画像服务不可用", exc_info=True)
            svc = None
    if svc is None:
        notes.append("profile_store_unavailable" if pcfg["enabled"] else "profile_disabled")
    else:
        try:
            counts = svc.store.player_overview_counts(
                active_since=int(ts - 7 * DAY_SEC), lookup_since=int(ts - LOOKUP_WINDOW_SEC))
            by_acct_stage = svc.store.count_player_profiles_by_stage()
            report = svc.daily_report(day, now=ts)
        except Exception:
            logger.warning("[player_care] overview: 读画像库失败", exc_info=True)
            notes.append("profile_query_failed")
        else:
            out["contacts"] = {
                "total": counts["total"], "with_phone": counts["with_phone"],
                "handoff": counts["handoff"], "active_7d": counts["active"],
                "by_account": counts["by_account"],
            }
            out["stages"]["totals"] = _stage_totals(by_acct_stage)
            out["stages"]["by_account"] = by_acct_stage
            t = report.get("totals") or {}
            out["today"] = {k: int(t.get(k) or 0) for k in
                            ("inbound", "lookups", "found", "visible", "gate_hits", "new_profiles", "stage_ups")}
            out["today"]["accounts"] = list(report.get("accounts") or [])
            recent = counts["recent_lookups"]
            out["gateway"].update({
                "lookups_total": counts["lookups_total"],
                "last_lookup_at": counts["last_lookup_at"],
                "recent_24h": recent,
                "status": gateway_status(gw_configured, recent),
            })

    ob = outbox
    if ob is None and cbcfg["enabled"]:
        try:
            ob = get_outbox(cfg_root)
        except Exception:
            logger.debug("[player_care] overview: 出箱不可用", exc_info=True)
            ob = None
    if ob is not None:
        try:
            out["commandbus"]["stats"] = ob.stats()
        except Exception:
            notes.append("commandbus_query_failed")
    return out


__all__ = ["build_overview", "gateway_status", "GW_UNCONFIGURED", "GW_IDLE", "GW_OK", "GW_DEGRADED", "GW_DOWN"]
