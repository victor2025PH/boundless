# -*- coding: utf-8 -*-
"""任务派发门禁 — 策略只读、预检探测。"""

from typing import Optional

from fastapi import APIRouter, Query

router = APIRouter(prefix="/task-dispatch", tags=["task-dispatch"])


@router.get("/policy")
def get_dispatch_policy():
    """返回当前门禁策略与高风险任务前缀（供控制台展示）。

    附加 ``policy_mtime`` / ``policy_mtime_iso`` 供 Dashboard 顶栏展示
    「YAML 最后加载时间」，方便运维确认改动是否生效。
    """
    import datetime as _dt

    from src.host.exit_profile import summary_for_api
    from src.host.task_dispatch_gate import last_gate_summary
    from src.host.task_policy import policy_mtime

    base = last_gate_summary()
    ex = summary_for_api()
    mt = policy_mtime()
    return {
        "ok": True,
        **base,
        "exit_profiles": ex.get("profiles", []),
        "policy_mtime": mt,
        "policy_mtime_iso": (
            _dt.datetime.fromtimestamp(mt).isoformat(timespec="seconds") if mt else None
        ),
    }


@router.post("/policy/reload")
def post_reload_dispatch_policy():
    """热加载 config/task_execution_policy.yaml。
    用于运维现场：改完 YAML 无需重启进程即可让 enforce_preflight / enforce_geo_for_risky
    等门禁开关立即生效（task_policy._cached 会被清空，下一次 evaluate_task_gate_detailed
    调用会重读磁盘）。
    """
    from src.host.task_dispatch_gate import last_gate_summary
    from src.host.task_policy import reload_policy

    before = last_gate_summary()
    reload_policy()
    after = last_gate_summary()
    return {
        "ok": True,
        "reloaded": True,
        "before": {
            "enforce_preflight": (before.get("manual_gate") or {}).get("enforce_preflight"),
            "enforce_geo_for_risky": (before.get("manual_gate") or {}).get("enforce_geo_for_risky"),
            "gate_mode": before.get("gate_mode"),
        },
        "after": {
            "enforce_preflight": (after.get("manual_gate") or {}).get("enforce_preflight"),
            "enforce_geo_for_risky": (after.get("manual_gate") or {}).get("enforce_geo_for_risky"),
            "gate_mode": after.get("gate_mode"),
        },
    }


@router.get("/exit-profiles")
def list_exit_profiles():
    """出口/VPN 形态档案（只读，来自 config/exit_profiles.yaml）。"""
    from src.host.exit_profile import summary_for_api

    return summary_for_api()


@router.post("/preflight-refresh/{device_id}")
def post_preflight_refresh(
    device_id: str,
    mode: str = "full",
    task_target_country: Optional[str] = Query(
        None,
        description="mode=full 时传入可与门禁一致，用于出口国比对/VPN 跳过（如 philippines）",
    ),
):
    """使缓存失效并立即重跑预检（与探测页「刷新」等效）。"""
    from src.host.preflight import invalidate_cache, run_preflight

    invalidate_cache(device_id)
    if mode not in ("full", "network_only", "none"):
        mode = "full"
    tgt = (task_target_country or "").strip().lower() or None
    r = run_preflight(
        device_id,
        skip_cache=True,
        mode=mode,  # type: ignore[arg-type]
        task_target_country=tgt if mode == "full" else None,
    )
    return {"ok": True, "refreshed": True, **r.to_dict()}


@router.get("/preflight/{device_id}")
def probe_preflight(
    device_id: str,
    skip_cache: bool = True,
    mode: str = "full",
    task_target_country: Optional[str] = Query(
        None,
        description="mode=full 时可选，任务目标国家（与 run_preflight.task_target_country 一致）",
    ),
):
    """对单台设备跑一次预检（不执行任务）。mode: full | network_only | none"""
    from src.host.preflight import run_preflight

    if mode not in ("full", "network_only", "none"):
        mode = "full"
    tgt = (task_target_country or "").strip().lower() or None
    r = run_preflight(
        device_id,
        skip_cache=skip_cache,
        mode=mode,  # type: ignore[arg-type]
        task_target_country=tgt if mode == "full" else None,
    )
    return {"ok": True, **r.to_dict()}
