# -*- coding: utf-8 -*-
"""Leads CRM、转化漏斗、获客工作流路由。"""
import json
import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Leads"])

_WORKER03_BASE = "http://192.168.0.103:8000"


def _proxy_worker03(path: str, method: str = "GET", body=None, timeout: int = 8):
    """直接代理到 Worker-03（用于写操作或缓存未命中的兜底）。"""
    try:
        import urllib.request as _ur
        url = f"{_WORKER03_BASE}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = _ur.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        resp = _ur.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except Exception:
        return None


def _get_w03_cache():
    """返回缓存对象；Worker-03 上返回 None（不自我代理）。"""
    try:
        from src.host.leads_cache import get_w03_cache, _IS_WORKER03
        if _IS_WORKER03:
            return None
        return get_w03_cache()
    except Exception:
        return None


# ── 转化漏斗 ──

@router.get("/funnel")
def conversion_funnel(platform: str = "tiktok", days: int = 30):
    """P9-B: CRM 漏斗 — 合并本地 + Worker-03 缓存数据。"""
    from src.leads.store import get_leads_store
    local = get_leads_store().get_conversion_funnel(platform, days)
    # P9-B: 从 SWR 缓存合并 Worker-03 漏斗数据
    cache = _get_w03_cache()
    w03 = cache.get_funnel() if cache else None
    if w03 and isinstance(w03, dict):
        merged = dict(local)
        _FUNNEL_NUMERIC = ("total_discovered", "total_followed", "total_follow_back",
                           "total_chatted", "total_replied", "total_converted")
        for k in _FUNNEL_NUMERIC:
            merged[k] = merged.get(k, 0) + w03.get(k, 0)
        # 嵌套漏斗 stages（list of dicts）暂不合并，保留本地
        merged["_sources"] = ["local", "worker03"]
        return merged
    return local


@router.get("/funnel/daily")
def daily_funnel(platform: str = "tiktok", days: int = 7):
    """Per-day funnel data for trend visualization."""
    from src.leads.store import get_leads_store
    store = get_leads_store()
    return {"daily": store.get_daily_funnel(platform, days)}


@router.get("/funnel/devices")
def device_funnel(platform: str = "tiktok"):
    """Per-device performance breakdown."""
    from src.host.device_state import get_device_state_store
    from src.device_control.device_manager import get_device_manager
    from src.host.device_registry import DEFAULT_DEVICES_YAML

    _config_path = DEFAULT_DEVICES_YAML

    ds = get_device_state_store("tiktok")
    manager = get_device_manager(_config_path)
    devices = [{"device_id": d.device_id} for d in manager.get_all_devices()]

    result = []
    for dev in devices:
        did = dev.get("device_id", "")
        summary = ds.get_device_summary(did)
        algo_score = summary.get("algorithm_score", 0)

        result.append({
            "device_id": did,
            "phase": summary.get("phase", "unknown"),
            "algorithm_score": algo_score,
            "total_watched": summary.get("total_watched", 0),
            "total_followed": summary.get("total_followed", 0),
            "total_dms_sent": summary.get("total_dms_sent", 0),
            "sessions_today": summary.get("sessions_today", 0),
            "efficiency": round(
                summary.get("total_dms_sent", 0) /
                max(summary.get("total_followed", 0), 1), 4),
        })

    result.sort(key=lambda x: x["total_followed"], reverse=True)
    return {"devices": result}


# ── 线索管理 ──

@router.get("/leads")
def leads_list(status: str = "", platform: str = "",
               min_score: float = 0, search: str = "",
               order_by: str = "score DESC",
               limit: int = 50, offset: int = 0):
    """P9-A: 瞬时响应 — 本地 + Worker-03 缓存合并，无 HTTP 阻塞。"""
    from src.leads.store import get_leads_store
    local = get_leads_store().list_leads(
        status=status or None,
        platform=platform or None,
        min_score=min_score if min_score > 0 else None,
        search=search or None,
        order_by=order_by,
        limit=limit, offset=offset,
    )
    # P9-A: 从 SWR 缓存读取 Worker-03 数据（无网络延迟）
    cache = _get_w03_cache()
    w03_all = cache.get_leads() if cache else None
    if w03_all and isinstance(w03_all, list):
        # 在内存中应用过滤条件
        filtered = cache.filter_leads(
            w03_all, status=status, platform=platform, min_score=min_score,
            search=search, order_by=order_by, limit=limit, offset=offset,
        )
        _local_names = {str(l.get("name", "")).lower() for l in local}
        for lead in filtered:
            lead["_source"] = "worker03"
        new_w03 = [l for l in filtered if str(l.get("name", "")).lower() not in _local_names]
        return local + new_w03
    return local


@router.get("/leads/stats")
def leads_stats():
    """P9-A: 合并统计（缓存驱动，瞬时响应）。"""
    from src.leads.store import get_leads_store
    local = get_leads_store().pipeline_stats()
    cache = _get_w03_cache()
    w03 = cache.get_stats() if cache else None
    if w03 and isinstance(w03, dict):
        merged = dict(local)
        for k, v in w03.items():
            if k == "_sources":
                continue
            if isinstance(v, (int, float)):
                merged[k] = merged.get(k, 0) + v
            elif isinstance(v, dict) and isinstance(merged.get(k), dict):
                for sk, sv in v.items():
                    if isinstance(sv, (int, float)):
                        merged[k][sk] = merged[k].get(sk, 0) + sv
        merged["_sources"] = ["local", "worker03"]
        merged["_cache_info"] = cache.info().get("stats", {})
        return merged
    return local


@router.get("/leads/cache-info")
def leads_cache_info():
    """P9-A: 返回 Worker-03 SWR 缓存状态（年龄、项目数量、是否失效）。"""
    cache = _get_w03_cache()
    if not cache:
        return {"available": False}
    return {"available": True, "entries": cache.info()}


@router.post("/leads")
def leads_create(body: dict):
    """Create a new lead."""
    name = body.get("name", "")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    from src.leads.store import get_leads_store
    lead_id = get_leads_store().add_lead(
        name=name,
        email=body.get("email", ""),
        phone=body.get("phone", ""),
        company=body.get("company", ""),
        title=body.get("title", ""),
        industry=body.get("industry", ""),
        location=body.get("location", ""),
        source_platform=body.get("source_platform", ""),
        tags=body.get("tags"),
        notes=body.get("notes", ""),
    )
    return {"lead_id": lead_id}


@router.get("/leads/{lead_id}")
def leads_get(lead_id: int):
    """Get lead details with platform profiles. P8-A: fallback to Worker-03 if not found locally."""
    from src.leads.store import get_leads_store
    store = get_leads_store()
    lead = store.get_lead(lead_id)
    if lead:
        lead["profiles"] = store.get_platform_profiles(lead_id)
        lead["interactions"] = store.get_interactions(lead_id, limit=20)
        return lead
    # P8-A: 本地不存在，尝试从 Worker-03 拉取
    w03 = _proxy_worker03(f"/leads/{lead_id}")
    if w03:
        w03["_source"] = "worker03"
        return w03
    raise HTTPException(status_code=404, detail="Lead not found")


@router.post("/leads/cleanup-test-data")
def leads_cleanup_test_data():
    """P9-C: 删除 coordinator 上的测试 lead（name LIKE 'Test Lead %'，score=0，无交互）。
    安全：只删除自动生成的测试数据，不影响真实线索。
    """
    from src.leads.store import get_leads_store
    store = get_leads_store()
    # 只删除明确是测试数据的 leads（score=0, status=new, name LIKE 'Test Lead %'）
    conn = store._conn()
    rows = conn.execute(
        "SELECT id FROM leads WHERE name LIKE 'Test Lead %' AND score=0 AND status='new'"
    ).fetchall()
    ids = [r[0] for r in rows]
    deleted = 0
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM leads WHERE id IN ({placeholders})", ids)
        conn.commit()
        deleted = len(ids)
    conn.close()
    return {"ok": True, "deleted_test_leads": deleted}


@router.post("/leads/cleanup-position-keys")
def leads_cleanup_position_keys():
    """P8-B: 删除 name 以 'newfollower_' 开头的垃圾 lead（位置键记录）。
    同时清理 Worker-03 上的同类记录。
    """
    from src.leads.store import get_leads_store
    local_count = get_leads_store().cleanup_position_key_leads()
    w03_result = _proxy_worker03("/leads/cleanup-position-keys", method="POST", body={})
    w03_count = w03_result.get("deleted_local", 0) if w03_result else 0
    return {
        "ok": True,
        "deleted_local": local_count,
        "deleted_worker03": w03_count,
        "total_deleted": local_count + w03_count,
    }


@router.put("/leads/{lead_id}")
def leads_update(lead_id: int, body: dict):
    """Update lead fields."""
    from src.leads.store import get_leads_store
    if not get_leads_store().update_lead(lead_id, **body):
        raise HTTPException(status_code=404, detail="Lead not found or no valid fields")
    return {"ok": True}


@router.delete("/leads/{lead_id}")
def leads_delete(lead_id: int):
    """Delete a lead and all associated data."""
    from src.leads.store import get_leads_store
    if not get_leads_store().delete_lead(lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    return {"ok": True}


@router.post("/leads/{lead_id}/profiles")
def leads_add_profile(lead_id: int, body: dict):
    """Add a platform profile to a lead."""
    platform = body.get("platform", "")
    if not platform:
        raise HTTPException(status_code=400, detail="platform is required")
    from src.leads.store import get_leads_store
    pid = get_leads_store().add_platform_profile(
        lead_id, platform,
        profile_id=body.get("profile_id", ""),
        profile_url=body.get("profile_url", ""),
        username=body.get("username", ""),
        bio=body.get("bio", ""),
        followers=body.get("followers", 0),
        following=body.get("following", 0),
        verified=body.get("verified", False),
    )
    return {"profile_id": pid}


@router.post("/leads/{lead_id}/interactions")
def leads_add_interaction(lead_id: int, body: dict):
    """Log an interaction with a lead."""
    from src.leads.store import get_leads_store
    iid = get_leads_store().add_interaction(
        lead_id,
        platform=body.get("platform", ""),
        action=body.get("action", ""),
        direction=body.get("direction", "outbound"),
        content=body.get("content", ""),
        status=body.get("status", "sent"),
        metadata=body.get("metadata"),
    )
    return {"interaction_id": iid}


@router.post("/leads/{lead_id}/score")
def leads_update_score(lead_id: int):
    """Recalculate lead score."""
    from src.leads.store import get_leads_store
    score = get_leads_store().update_score(lead_id)
    return {"lead_id": lead_id, "score": score}


@router.post("/leads/scores/bulk")
def leads_bulk_scores():
    """Recalculate scores for all active leads."""
    from src.leads.store import get_leads_store
    count = get_leads_store().bulk_update_scores()
    return {"updated": count}


@router.get("/leads/match")
def leads_find_match(email: str = "", phone: str = "", name: str = ""):
    """Find an existing lead by identity signals."""
    from src.leads.store import get_leads_store
    lead_id = get_leads_store().find_match(email=email, phone=phone, name=name)
    if lead_id:
        return {"matched": True, "lead_id": lead_id}
    return {"matched": False}


@router.post("/leads/referral-confirmed")
def leads_referral_confirmed(body: dict):
    """
    外部回调：确认 TikTok 用户已在 TG/WA 等渠道完成加好友/转化闭环。
    供 n8n、自建 Telegram 机器人或手工工具调用；将线索推进为 converted。
    """
    u = (body.get("tiktok_username") or "").lstrip("@").strip()
    if not u:
        raise HTTPException(status_code=400, detail="tiktok_username required")
    channel = str(body.get("channel") or "telegram").lower()
    ext = str(body.get("external_username") or "").strip()
    secret = str(body.get("secret") or "")
    # 可选：config/referral_webhook_secret.txt 存在则校验
    try:
        from src.host.device_registry import config_file

        p = config_file("referral_webhook_secret.txt")
        if p.exists():
            expected = p.read_text(encoding="utf-8").strip()
            if expected and secret != expected:
                raise HTTPException(status_code=403, detail="invalid secret")
    except HTTPException:
        raise
    except Exception:
        pass

    from src.leads.store import get_leads_store
    store = get_leads_store()
    lid = store.find_by_platform_username("tiktok", u)
    if not lid:
        raise HTTPException(status_code=404, detail="Lead not found for TikTok username")
    note = f"off-platform referral confirmed: channel={channel}"
    if ext:
        note += f" handle={ext}"
    ok = store.mark_conversion(
        lid,
        append_note=note,
        external_ref=f"referral_{channel}_{ext}"[:180] if ext else f"referral_{channel}",
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update lead")
    try:
        store.add_interaction(
            lid,
            platform="tiktok",
            action="referral_confirmed",
            direction="inbound",
            content=json.dumps({"channel": channel, "external": ext}, ensure_ascii=False),
            status="ok",
        )
    except Exception:
        pass
    return {"ok": True, "lead_id": lid, "tiktok_username": u}


@router.post("/leads/{lead_id}/conversion")
def leads_mark_conversion(lead_id: int, body: dict):
    """
    标记成交并可选写入金额（手工归因或对接订单系统回调）。
    无金额时仍可把线索推进到 converted，便于漏斗闭合率统计。
    """
    from src.leads.store import get_leads_store

    store = get_leads_store()
    val = body.get("value")
    if val is not None:
        try:
            val = float(val)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="value must be a number")
    ok = store.mark_conversion(
        lead_id,
        value=val,
        currency=str(body.get("currency") or "USD"),
        external_ref=str(body.get("external_ref") or ""),
        append_note=str(body.get("note") or ""),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        store.add_interaction(
            lead_id,
            platform=body.get("platform") or "manual",
            action="conversion_recorded",
            direction="outbound",
            content=json.dumps({
                "value": val,
                "currency": body.get("currency"),
                "external_ref": body.get("external_ref"),
            }, ensure_ascii=False),
            status="ok",
        )
    except Exception:
        pass
    return {"ok": True, "lead_id": lead_id}


@router.post("/leads/batch-conversion")
def leads_batch_conversion(body: dict):
    """
    批量标记成交金额 — 用于历史数据补录或外部订单系统回调。

    body: {
      "items": [
        {"lead_id": 1, "value": 50.0, "currency": "EUR", "note": "订单#123"},
        {"lead_id": 2, "value": 80.0}
      ]
    }
    """
    from src.leads.store import get_leads_store
    items = body.get("items", [])
    if not items:
        raise HTTPException(status_code=400, detail="items 不能为空")
    store = get_leads_store()
    results = []
    for item in items:
        lid = item.get("lead_id")
        if not lid:
            results.append({"lead_id": None, "ok": False, "reason": "missing lead_id"})
            continue
        val = item.get("value")
        if val is not None:
            try:
                val = float(val)
            except (TypeError, ValueError):
                val = None
        ok = store.mark_conversion(
            lid,
            value=val,
            currency=str(item.get("currency") or "EUR"),
            external_ref=str(item.get("external_ref") or ""),
            append_note=str(item.get("note") or ""),
        )
        results.append({"lead_id": lid, "ok": ok})
    updated = sum(1 for r in results if r.get("ok"))
    return {"ok": True, "results": results, "updated": updated, "total": len(items)}


# ── 获客工作流 ──

@router.get("/acquisition/status")
def acquisition_status():
    """Get acquisition pipeline status."""
    from src.workflow.acquisition import get_acquisition_pipeline
    return get_acquisition_pipeline().status()


@router.post("/acquisition/discover")
def acquisition_discover(body: dict):
    """Discover leads across platforms."""
    from src.workflow.acquisition import get_acquisition_pipeline
    keywords = body.get("keywords", [])
    if not keywords:
        raise HTTPException(status_code=400, detail="keywords list is required")
    results = get_acquisition_pipeline().discover(
        keywords=keywords,
        platforms=body.get("platforms"),
        max_per_keyword=body.get("max_per_keyword", 10),
        device_id=body.get("device_id"),
    )
    return {"discovered": {p: len(ids) for p, ids in results.items()},
            "lead_ids": {p: ids for p, ids in results.items()}}


@router.post("/acquisition/warmup")
def acquisition_warmup(body: dict):
    """Warm up leads with light engagement."""
    from src.workflow.acquisition import get_acquisition_pipeline
    lead_ids = body.get("lead_ids", [])
    if not lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids list is required")
    stats = get_acquisition_pipeline().warm_up(
        lead_ids=lead_ids,
        platforms=body.get("platforms"),
        device_id=body.get("device_id"),
    )
    return {"warmup_stats": stats}


@router.post("/acquisition/engage")
def acquisition_engage(body: dict):
    """Send direct messages to leads."""
    from src.workflow.acquisition import get_acquisition_pipeline
    lead_ids = body.get("lead_ids", [])
    if not lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids list is required")
    stats = get_acquisition_pipeline().engage(
        lead_ids=lead_ids,
        message_template=body.get("message_template", ""),
        platforms=body.get("platforms"),
        device_id=body.get("device_id"),
    )
    return {"engagement_stats": stats}


@router.post("/acquisition/pipeline")
def acquisition_full_pipeline(body: dict):
    """Run the complete acquisition pipeline: Discover → Warm-up → Engage."""
    from src.workflow.acquisition import get_acquisition_pipeline
    keywords = body.get("keywords", [])
    if not keywords:
        raise HTTPException(status_code=400, detail="keywords list is required")
    result = get_acquisition_pipeline().run_full_pipeline(
        keywords=keywords,
        platforms=body.get("platforms"),
        max_leads=body.get("max_leads", 20),
        device_id=body.get("device_id"),
    )
    return result


@router.post("/acquisition/workflow/load")
def acquisition_load_workflow(body: dict):
    """Load and activate an acquisition workflow from YAML."""
    from src.workflow.acquisition import get_acquisition_pipeline
    path = body.get("path", "")
    if not path:
        raise HTTPException(status_code=400, detail="path to YAML is required")
    pipeline = get_acquisition_pipeline()
    wf = pipeline.load_workflow(path)
    pipeline.set_active(wf.name)
    return {"loaded": wf.name, "stages": list(wf.stages.keys()),
            "escalation_rules": len(wf.escalation_rules)}
