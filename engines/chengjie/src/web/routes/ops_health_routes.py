"""反封号健康统一只读出口（实施31 追加 · 2026-07-20）。

把散在四处的账号安全状态聚合成「一个接口看全」，回答运营三问：
号现在在跑吗 / 健康吗（会不会被风控）/ 最近被风控过吗、还能发多少。

数据源（全部复用已有能力，不新造评分）：
- ``account_orchestrator``           → 运行态（worker running?）
- ``companion_send_gate`` + M7 健康   → 红黄绿灯 / 评分 / 预热建议上限
- ``protocol_autoreply_limits``      → 时/日配额已用与上限、熔断态
- ``ops_events``（实施31）            → 近 7 天运维事件史（暂停/封禁计数）
- ``kill_switch``                    → 是否被紧急冻结

端点：``GET /api/ops/account-health``（只读，api_auth）。
核心聚合 ``account_health_row`` 为纯函数（依赖注入）→ 不依赖 FastAPI/真号即可单测。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from fastapi import Request


def account_health_row(
    platform: str,
    account_id: str,
    *,
    cfg: Dict[str, Any],
    signals: Dict[str, Any],
    rate_snapshot: Optional[Dict[str, Any]] = None,
    worker_state: str = "",
    events_summary: Optional[Dict[str, Any]] = None,
    frozen: bool = False,
) -> Dict[str, Any]:
    """单账号健康行（纯函数）：把注入的信号/配额/事件汇成一行运营视图。

    ``signals`` = ``build_account_signals`` 结果；用 ``companion_send_gate.evaluate`` 判灯。
    绝不抛（聚合失败降级为 unknown），保证总览端点永不 500。
    """
    from src.skills.companion_send_gate import evaluate as _gate_eval, gate_enabled as _gate_on
    try:
        dec = _gate_eval(signals, cfg)
    except Exception:
        dec = {"allowed": True, "reason": "eval_error", "light": "unknown",
               "score": 0, "recommended_cap": 0}
    # 闸门未开时 evaluate 恒返回 green/100 —— 那是「未监控」不是「已验证健康」，
    # 显式标 gated=False 并把灯改成 ungated，避免体检单给假安全感（可观测性诚实性）。
    try:
        gated = bool(_gate_on(cfg))
    except Exception:
        gated = False
    if not gated:
        dec = {**dec, "light": "ungated", "reason": "gate_disabled"}
    running = worker_state == "running"
    rs = rate_snapshot or {}
    ev = events_summary or {"total": 0, "by_kind": {}}
    # 综合状态：冻结 > 不健康(红/被拦) > 未在跑 > 正常
    if frozen:
        overall = "frozen"
    elif dec.get("light") == "red" or not dec.get("allowed", True):
        overall = "at_risk"
    elif not running:
        overall = "stopped"
    else:
        overall = "ok"
    return {
        "platform": platform,
        "account_id": account_id,
        "overall": overall,
        "running": running,
        "worker_state": worker_state or "unknown",
        "frozen": frozen,
        "health": {
            "light": dec.get("light", "unknown"),
            "score": dec.get("score", 0),
            "allowed": dec.get("allowed", True),
            "reason": dec.get("reason", ""),
            "recommended_cap": dec.get("recommended_cap", 0),
            "sends_today": int(signals.get("sends_today") or 0),
            "age_days": round(float(signals.get("age_days") or 0.0), 2),
            "banned": bool(signals.get("banned", False)),
            "proxy_bound": bool(signals.get("proxy_bound", False)),
        },
        "rate": {
            "hour_used": rs.get("hour_used", 0),
            "hour_limit": rs.get("hour_limit", 0),
            "day_used": rs.get("day_used", 0),
            "day_limit": rs.get("day_limit", 0),
            "circuit_open": bool(rs.get("circuit_open", False)),
        },
        "events_7d": {"total": int(ev.get("total", 0)),
                      "by_kind": dict(ev.get("by_kind", {}))},
    }


def register_ops_health_routes(app, ctx) -> None:
    """挂载 GET /api/ops/account-health（只读账号反封号健康总览）。"""
    _api_auth = ctx.api_auth
    _config_manager = getattr(ctx, "config_manager", None)

    @app.get("/api/ops/account-health")
    async def api_ops_account_health(request: Request):
        """一个接口看全：每个受管账号的运行态 / 健康灯 / 配额 / 近7天事件 / 冻结态。"""
        _api_auth(request)
        cfg = (getattr(_config_manager, "config", None) or {}) if _config_manager else {}

        from src.integrations.account_registry import get_account_registry
        from src.skills.account_signals import build_account_signals
        reg = get_account_registry()

        # 限速快照（按账号有效上限）——limiter 单例，取不到则降级
        try:
            from src.integrations.protocol_autoreply_limits import get_autoreply_limiter
            from src.integrations.protocol_autoreply_settings import cfg_with_settings
            lim = get_autoreply_limiter(cfg_with_settings(cfg))
        except Exception:
            lim = None

        # 运行态（编排器已建才取；不创建单例避免空配置遮蔽）
        worker_states: Dict[str, str] = {}
        running_loop = False
        try:
            from src.integrations.account_orchestrator import get_orchestrator_if_running
            orch = get_orchestrator_if_running()
            if orch is not None:
                st = orch.status()
                running_loop = bool(st.get("running_loop"))
                for a in st.get("accounts", []):
                    worker_states[f"{a.get('platform')}:{a.get('account_id')}"] = str(a.get("state") or "")
        except Exception:
            pass

        # 事件史
        try:
            from src.ops.ops_events import get_ops_event_store
            ev_store = get_ops_event_store()
        except Exception:
            ev_store = None

        # 冻结态（kill-switch 生效作用域）
        frozen_scopes: set = set()
        global_frozen = False
        try:
            from src.ops.kill_switch import get_kill_switch
            for it in get_kill_switch().status():
                sc = str(it.get("scope") or "")
                frozen_scopes.add(sc)
                if sc == "global":
                    global_frozen = True
        except Exception:
            pass

        rows: List[Dict[str, Any]] = []
        for r in reg.list():
            platform = str(r.get("platform") or "")
            account_id = str(r.get("account_id") or "")
            sig = build_account_signals(
                platform, account_id, registry=reg, limiter=lim,
                extra={"proxy_bound": bool(r.get("proxy_id"))},
            )
            rate_snap = None
            if lim is not None:
                try:
                    rate_snap = lim.snapshot(f"{platform.lower()}:{account_id}")
                except Exception:
                    rate_snap = None
            ev_sum = None
            if ev_store is not None:
                try:
                    ev_sum = ev_store.summary(account_id=account_id, days=7)
                except Exception:
                    ev_sum = None
            frozen = global_frozen or (f"account:{platform.lower()}:{account_id}" in frozen_scopes)
            rows.append(account_health_row(
                platform, account_id, cfg=cfg, signals=sig,
                rate_snapshot=rate_snap,
                worker_state=worker_states.get(f"{platform}:{account_id}", ""),
                events_summary=ev_sum, frozen=frozen,
            ))

        # 汇总灯：任一 at_risk/frozen → 红；任一 stopped → 黄；全 ok → 绿
        overalls = [x["overall"] for x in rows]
        if any(o in ("at_risk", "frozen") for o in overalls):
            fleet = "red"
        elif any(o == "stopped" for o in overalls):
            fleet = "amber"
        elif rows:
            fleet = "green"
        else:
            fleet = "empty"

        return {
            "ok": True,
            "generated_at": time.time(),
            "orchestrator_running": running_loop,
            "global_frozen": global_frozen,
            "fleet_light": fleet,
            "total": len(rows),
            "accounts": rows,
        }


__all__ = ["account_health_row", "register_ops_health_routes"]
