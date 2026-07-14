# -*- coding: utf-8 -*-
"""
统一运营大屏路由 — P2-4。

整合 leads / contacts / analytics / devices / alerts 多维度数据，
提供运营人员一站式实时监控视图。

端点:
  GET /dashboard/overview    — 顶层汇总（漏斗 + 设备 + 任务）
  GET /dashboard/funnel      — 引流漏斗详细数据（近N天）
  GET /dashboard/devices     — 设备状态 + 健康风险
  GET /dashboard/seeds       — 种子账号分层汇总
  GET /dashboard/ab          — A/B实验当前胜出变体
  GET /dashboard/alerts      — 最近告警记录
  GET /dashboard/params      — 当前策略优化参数
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Query

from src.host.device_registry import DEFAULT_DEVICES_YAML, data_file

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ── helpers ──

def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        logger.debug("[Dashboard] %s", e)
        return default


# ── 端点 ──

@router.get("/overview")
def dashboard_overview(days: int = Query(7, ge=1, le=90)):
    """
    一站式运营概览。返回:
    - 引流漏斗（近N天）
    - 设备状态（在线/离线/风险）
    - 任务统计（完成/失败/运行中）
    - 种子账号分层汇总
    - 当前策略参数
    - 最近 A/B 胜出变体
    """
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period_days": days,
        "funnel": _safe(lambda: _get_funnel(days)),
        "devices": _safe(lambda: _get_device_summary()),
        "tasks": _safe(lambda: _get_task_summary(days)),
        "seeds": _safe(lambda: _get_seed_summary()),
        "params": _safe(lambda: _get_strategy_params()),
        "ab_winners": _safe(lambda: _get_ab_winners()),
    }


@router.get("/funnel")
def dashboard_funnel(
    days: int = Query(7, ge=1, le=90),
    platform: str = Query("tiktok"),
):
    """引流漏斗详细数据，含各阶段转化率。"""
    funnel = _safe(lambda: _get_funnel(days, platform), {})
    if not funnel:
        return {"platform": platform, "days": days, "stages": {}}

    stages = funnel
    total = stages.get("discovered", 1)
    rates = {}
    prev_key = None
    ordered_keys = ["discovered", "followed", "follow_back",
                    "chatted", "replied", "converted"]
    for key in ordered_keys:
        val = stages.get(key, 0)
        if prev_key:
            prev_val = stages.get(prev_key, 0)
            rates[f"{prev_key}_to_{key}"] = round(val / max(prev_val, 1) * 100, 1)
        prev_key = key

    return {
        "platform": platform,
        "days": days,
        "stages": stages,
        "conversion_rates": rates,
        "overall_conversion": round(stages.get("converted", 0) / max(total, 1) * 100, 2),
    }


@router.get("/devices")
def dashboard_devices():
    """设备状态 + 健康风险 + 当前任务。"""
    try:
        from src.device_control.device_manager import get_device_manager
        mgr = get_device_manager(DEFAULT_DEVICES_YAML)
        mgr.discover_devices()
        devices = mgr.get_all_devices()
    except Exception as e:
        return {"error": str(e), "devices": []}

    result = []
    for dev in devices:
        dev_id = dev.device_id
        risk = _safe(lambda d=dev_id: _get_device_risk(d), {})
        result.append({
            "device_id": dev_id,
            "status": dev.status.value if hasattr(dev.status, "value") else str(dev.status),
            "health_risk": risk.get("risk", "unknown"),
            "risk_score": risk.get("score"),
            "risk_reasons": risk.get("reasons", []),
        })

    online = sum(1 for d in result if d["status"] in ("connected", "online"))
    high_risk = sum(1 for d in result if d["health_risk"] == "high")

    return {
        "total": len(result),
        "online": online,
        "offline": len(result) - online,
        "high_risk": high_risk,
        "devices": result,
    }


@router.get("/seeds")
def dashboard_seeds():
    """种子账号分层汇总 + Top种子账号列表。"""
    try:
        from src.host.seed_ranker import get_seed_ranker
        ranker = get_seed_ranker()
        summary = ranker.summary()
        top_s = ranker.get_top_seeds(n=10, min_tier="S")
        top_a = ranker.get_top_seeds(n=5, min_tier="A")

        all_ranks = ranker.get_all_ranks()
        top_details = []
        for seed in (top_s + [s for s in top_a if s not in top_s])[:10]:
            info = all_ranks.get(seed, {})
            top_details.append({
                "seed": seed,
                "tier": info.get("tier", "?"),
                "follow_back_rate": info.get("follow_back_rate", 0),
                "followed_cnt": info.get("followed_cnt", 0),
                "updated_at": info.get("updated_at", ""),
            })

        return {
            "summary": summary,
            "top_seeds": top_details,
        }
    except Exception as e:
        return {"error": str(e)}


@router.get("/ab")
def dashboard_ab():
    """A/B实验当前胜出变体及实验状态。"""
    import json

    winner_path = data_file("ab_winner.json")
    winners = {}
    if winner_path.exists():
        try:
            with open(winner_path, encoding="utf-8") as f:
                winners = json.load(f)
        except Exception:
            pass

    experiments = _safe(lambda: _get_ab_experiment_stats(), [])
    return {
        "current_winners": winners,
        "experiments": experiments,
    }


@router.get("/alerts")
def dashboard_alerts(limit: int = Query(20, ge=1, le=100)):
    """最近的告警记录（从 optimization_log.json 和 alert_notifier 历史）。"""
    import json

    log_path = data_file("optimization_log.json")
    entries = []
    if log_path.exists():
        try:
            with open(log_path, encoding="utf-8") as f:
                entries = json.load(f)
            entries = sorted(entries, key=lambda x: x.get("ts", ""), reverse=True)
            entries = entries[:limit]
        except Exception:
            pass
    return {"entries": entries, "total": len(entries)}


@router.get("/timezone")
def dashboard_timezone(countries: str = Query("italy,brazil,usa,uk,india")):
    """查询目标国家当前活跃状态（逗号分隔的国家列表）。"""
    from src.host.timezone_guard import is_country_active, minutes_until_active, best_send_utc_hour
    from datetime import datetime, timezone as tz_
    utc_now = datetime.now(tz_.utc)
    result = {}
    for country in [c.strip() for c in countries.split(",") if c.strip()]:
        active = is_country_active(country, utc_now)
        result[country] = {
            "active": active,
            "minutes_until_active": 0 if active else minutes_until_active(country, utc_now),
            "best_send_utc_hour": best_send_utc_hour(country),
        }
    return {"utc_now": utc_now.isoformat(), "countries": result}


@router.get("/params")
def dashboard_params():
    """当前策略优化器参数 + 历史变化记录。"""
    try:
        from src.host.strategy_optimizer import get_optimized_params
        params = get_optimized_params()
    except Exception as e:
        params = {"error": str(e)}

    import json

    log_path = data_file("optimization_log.json")
    recent_changes = []
    if log_path.exists():
        try:
            with open(log_path, encoding="utf-8") as f:
                all_logs = json.load(f)
            recent_changes = [
                e for e in all_logs
                if e.get("action") in ("params_optimized", "ab_winners_applied")
            ][-10:]
        except Exception:
            pass

    return {
        "current_params": params,
        "recent_changes": recent_changes,
    }


# ── 内部辅助函数 ──

def _get_funnel(days: int = 7, platform: str = "tiktok") -> dict:
    from src.leads.store import get_leads_store
    store = get_leads_store()
    return store.get_conversion_funnel(platform=platform, days=days) or {}


def _get_device_summary() -> dict:
    from src.device_control.device_manager import get_device_manager
    mgr = get_device_manager(DEFAULT_DEVICES_YAML)
    mgr.discover_devices()
    devices = mgr.get_all_devices()
    total = len(devices)
    online = sum(1 for d in devices
                 if (d.status.value if hasattr(d.status, "value") else str(d.status))
                 in ("connected", "online"))
    return {"total": total, "online": online, "offline": total - online}


def _get_task_summary(days: int = 7) -> dict:
    from src.host.task_store import get_task_store
    ts = get_task_store()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    tasks = [t for t in ts.list_tasks()
             if t.get("created_at", "") >= cutoff]
    return {
        "total": len(tasks),
        "completed": sum(1 for t in tasks if t.get("status") == "completed"),
        "failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "running": sum(1 for t in tasks if t.get("status") == "running"),
        "pending": sum(1 for t in tasks if t.get("status") == "pending"),
    }


def _get_seed_summary() -> dict:
    from src.host.seed_ranker import get_seed_ranker
    return get_seed_ranker().summary()


def _get_strategy_params() -> dict:
    from src.host.strategy_optimizer import get_optimized_params
    return get_optimized_params()


def _get_ab_winners() -> dict:
    import json
    p = data_file("ab_winner.json")
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _get_device_risk(device_id: str) -> dict:
    from src.host.health_monitor import metrics
    return metrics.predict_disconnect_risk(device_id)


def _get_ab_experiment_stats() -> list:
    from src.host.ab_testing import get_ab_store
    store = get_ab_store()
    results = []
    for exp_name in ["dm_template_style", "follow_timing",
                     "seed_selection_method", "dm_send_delay"]:
        best = _safe(lambda n=exp_name: store.best_variant(
            n, metric="reply_received", min_samples=10))
        results.append({"name": exp_name, "best_variant": best})
    return results


# ─────────────────────────────────────────────────────────────────────
# Sprint 3 P1: 跨平台漏斗总览 — 把 fb / tt 漏斗映射到统一的 6 步
# ─────────────────────────────────────────────────────────────────────

# 6 步统一漏斗的语义映射:
UNIFIED_FUNNEL_STEPS = [
    {"key": "exposure",      "label": "接触/曝光"},
    {"key": "interest",      "label": "兴趣信号"},
    {"key": "engagement",    "label": "互动建立"},
    {"key": "direct_msg",    "label": "直接对话"},
    {"key": "guidance",      "label": "引导(回复)"},
    {"key": "conversion",    "label": "转化(WA)"},
]


def _fb_funnel_unified(since_iso: Optional[str]) -> dict:
    """Facebook 6 步映射:
       extract_members → friend_request_sent → friend_accepted →
       inbox_incoming → outgoing_replies → wa_referrals
    """
    try:
        from src.host.fb_store import get_funnel_metrics, list_groups
        m = get_funnel_metrics(since_iso=since_iso)
        groups = list_groups(status="joined", limit=500)
    except Exception as e:
        logger.debug("[xplat_funnel] fb fetch failed: %s", e)
        return {"_error": str(e), "values": [0] * 6}
    return {
        "values": [
            int(m["stage_extracted_members"] or 0),
            int(m["stage_friend_request_sent"] or 0),
            int(m["stage_friend_accepted"] or 0),
            int(m["stage_inbox_incoming"] or 0),
            int(m["stage_outgoing_replies"] or 0),
            int(m["stage_wa_referrals"] or 0),
        ],
        "rates": {
            "interest": m["rate_extract_to_request"],
            "engagement": m["rate_accept"],
            "direct_msg": m["rate_request_to_inbox"],
            "conversion": m["rate_inbox_to_referral"],
        },
        "extra": {
            "groups_joined": len(groups),
        },
    }


def _tt_funnel_unified(since_iso: Optional[str]) -> dict:
    """TikTok 6 步映射(Sprint 4 P1 真实埋点版本)。

    优先从 `tiktok_funnel_events` 读真实埋点(record_tt_event 写入)。
    如果这张表没数据(新部署/未埋点时期),回退到 Sprint 3 的 tasks 表
    COUNT 占位估算,保证接口永远有数据。
    """
    real_values = [0, 0, 0, 0, 0, 0]
    real_rates: dict = {}
    real_has_data = False
    try:
        from src.host.tt_funnel_store import get_tt_funnel_metrics
        m = get_tt_funnel_metrics(since_iso=since_iso)
        real_values = [
            int(m.get("stage_exposure", 0)),
            int(m.get("stage_interest", 0)),
            int(m.get("stage_engagement", 0)),
            int(m.get("stage_direct_msg", 0)),
            int(m.get("stage_guidance", 0)),
            int(m.get("stage_conversion", 0)),
        ]
        real_rates = {
            "interest":   m.get("rate_exposure_to_interest", 0.0),
            "engagement": m.get("rate_interest_to_engage", 0.0),
            "direct_msg": m.get("rate_engage_to_dm", 0.0),
            "guidance":   m.get("rate_dm_to_guidance", 0.0),
            "conversion": m.get("rate_guidance_to_conv", 0.0),
        }
        real_has_data = any(v > 0 for v in real_values)
    except Exception as e:
        logger.debug("[xplat_funnel] tt real-events fetch failed: %s", e)

    if real_has_data:
        return {
            "values": real_values,
            "rates": real_rates,
            "extra": {"_status": "Sprint 4 真实埋点",
                      "source": "tiktok_funnel_events"},
        }

    # 回退:旧占位逻辑
    try:
        from src.host import task_store as ts
        cutoff = since_iso or "1970-01-01"
        all_t = ts.list_tasks(status="completed", limit=10000)
        recent = [t for t in all_t if (t.get("type") or "").startswith("tiktok_")
                  and t.get("created_at", "") >= cutoff]
        type_counts = {}
        for t in recent:
            tp = t.get("type", "")
            type_counts[tp] = type_counts.get(tp, 0) + 1
        exposure   = type_counts.get("tiktok_browse_home", 0) * 5
        interest   = type_counts.get("tiktok_like", 0)
        engagement = type_counts.get("tiktok_follow", 0)
        direct_msg = type_counts.get("tiktok_dm", 0) + type_counts.get("tiktok_send_dm", 0)
        guidance   = type_counts.get("tiktok_check_inbox", 0)
        conversion = 0
    except Exception as e:
        logger.debug("[xplat_funnel] tt fallback fetch failed: %s", e)
        return {"_error": str(e), "values": [0] * 6}
    return {
        "values": [exposure, interest, engagement, direct_msg, guidance, conversion],
        "rates": {},
        "extra": {"_status": "Sprint 4 回退占位 (埋点未启用或无数据)",
                  "source": "tasks_count"},
    }


@router.get("/cross-platform-funnel")
def cross_platform_funnel(since_hours: int = Query(168, ge=1, le=720),
                          platforms: str = Query("facebook,tiktok",
                                                 description="逗号分隔的平台列表")):
    """跨平台统一漏斗 — Sprint 3 P1。

    把 facebook / tiktok 的漏斗映射到 6 步统一漏斗,便于横向对比。

    响应:
      steps: [{key, label}]
      platforms: [
        {name, color, values: [6 阶段数字], rates: {...}, extra: {...}}
      ]
      sums: 各阶段 6 元素求和(总盘量)
    """
    import datetime as _dt
    since_iso = (
        _dt.datetime.utcnow() - _dt.timedelta(hours=since_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    plat_list = [p.strip().lower() for p in platforms.split(",") if p.strip()]
    plat_payload = []
    color_map = {
        "facebook": "#1877f2",
        "tiktok": "#ff0050",
        "linkedin": "#0a66c2",
        "twitter": "#1da1f2",
        "instagram": "#e4405f",
    }
    for pname in plat_list:
        if pname == "facebook":
            data = _fb_funnel_unified(since_iso)
        elif pname == "tiktok":
            data = _tt_funnel_unified(since_iso)
        else:
            data = {"values": [0] * 6,
                    "_error": f"平台 {pname} 暂不支持"}
        plat_payload.append({
            "name": pname,
            "color": color_map.get(pname, "#6b7280"),
            **data,
        })

    sums = [sum(p["values"][i] for p in plat_payload) for i in range(6)]

    return {
        "steps": UNIFIED_FUNNEL_STEPS,
        "platforms": plat_payload,
        "sums": sums,
        "scope_since_hours": since_hours,
        "scope_since_iso": since_iso,
        "_version": "sprint3.p1.7",
    }
