# -*- coding: utf-8 -*-
"""Phase 11 (2026-04-25): /line-pool 管理 API.

提供:
  * GET  /line-pool                列表 (filter: status/region/persona_key/owner)
  * POST /line-pool                单条新增
  * PUT  /line-pool/{id}           更新 (status/region/persona/cap/notes/line_id)
  * DELETE /line-pool/{id}         删除
  * POST /line-pool/bulk-import    批量导入 (CSV text 或 JSON array)
  * POST /line-pool/allocate       轮循分配 (内部/dispatcher 调用)
  * GET  /line-pool/dispatch-log   最近分发审计
"""
from __future__ import annotations

import csv
import io
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query, UploadFile, File

from src.host import line_pool as lp

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/line-pool", tags=["line-pool"])


# ─── 查 ─────────────────────────────────────────────────────────────────

@router.get("")
def api_list_line_pool(
        status: Optional[str] = Query(default=None),
        region: Optional[str] = Query(default=None),
        persona_key: Optional[str] = Query(default=None),
        owner_device_id: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
        includes_24h_stats: bool = Query(default=False)):
    rows = lp.list_accounts(status=status, region=region,
                              persona_key=persona_key,
                              owner_device_id=owner_device_id,
                              limit=limit,
                              includes_24h_stats=includes_24h_stats)
    return {"count": len(rows), "results": rows}


@router.get("/dispatch-log")
def api_dispatch_log(limit: int = Query(default=100, ge=1, le=1000)):
    return {"results": lp.recent_dispatch_log(limit=limit)}


@router.get("/stats/referral-funnel")
def api_referral_funnel(hours_window: int = Query(default=168, ge=1, le=720),
                          region: Optional[str] = Query(default=None),
                          persona_key: Optional[str] = Query(default=None)):
    """Phase 13: referral 闭环漏斗 4 层数字."""
    return lp.referral_funnel(hours_window=hours_window,
                                region=region, persona_key=persona_key)


@router.get("/stats/account-ranking")
def api_account_ranking(hours_window: int = Query(default=168, ge=1, le=720),
                          limit: int = Query(default=50, ge=1, le=500)):
    """Phase 13: per-LINE-account 成功率排名."""
    return {"results": lp.account_ranking(hours_window=hours_window,
                                             limit=limit)}


@router.get("/stats/peer-name-rejects")
def api_peer_name_reject_metrics():
    """Phase 17.1: peer_name sanitize reject 计数 + by_event_type + 最近样本.

    数据为进程内 (counter 模块级, 重启清零). 跨重启历史查
    /stats/peer-name-rejects/history.
    """
    from src.host.fb_store import get_peer_name_reject_metrics
    return get_peer_name_reject_metrics()


@router.get("/stats/peer-name-rejects/history")
def api_peer_name_reject_history(
        hours_window: int = Query(default=168, ge=1, le=720),
        limit: int = Query(default=200, ge=1, le=5000),
        by_day: bool = Query(default=True)):
    """Phase 18: 跨进程持久化 reject 历史 (DB peer_name_reject_log).

    by_day=true 时返 by_day dict {date: count} 看每日趋势.
    """
    from src.host.fb_store import get_peer_name_reject_history
    return get_peer_name_reject_history(
        hours_window=hours_window, limit=limit, by_day=by_day)


@router.post("/stats/peer-name-rejects/reset")
def api_peer_name_reject_reset():
    """Phase 17.1: 重置 reject 计数器 (运维 / 测试)."""
    from src.host.fb_store import reset_peer_name_reject_count
    reset_peer_name_reject_count()
    return {"ok": True}


@router.post("/stats/peer-name-blacklist/reload")
def api_blacklist_reload():
    """Phase 17.1: force reload config/peer_name_blacklist.yaml (绕开 5min TTL)."""
    from src.app_automation.facebook import FacebookAutomation
    n = FacebookAutomation.reload_extra_blacklist()
    return {"ok": True, "extra_count": n}


@router.get("/stats/stale-leads")
def api_stale_leads(limit: int = Query(default=200, ge=1, le=1000),
                       offset: int = Query(default=0, ge=0),
                       include_dead: bool = Query(default=True)):
    """Phase 20.2.x.3: 当前 referral_stale tagged 的 lead 列表.

    include_dead=false 只列尚未升级 dead 的 (运营手动复查最有用的子集).
    """
    from src.host.fb_store import list_stale_leads
    return list_stale_leads(limit=limit, offset=offset,
                              include_dead=include_dead)


@router.get("/stats/alert-history")
def api_alert_history(
        hours_window: int = Query(default=168, ge=1, le=720),
        alert_type: Optional[str] = Query(default=None),
        severity: Optional[str] = Query(default=None),
        region: Optional[str] = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
        by_day: bool = Query(default=True)):
    """Phase 20.1.9.1: 跨进程 alert 触发历史 (DB fb_alert_history).

    by_day=true 返 by_day dict 看每日 alert 频率趋势.
    """
    from src.host.fb_store import get_alert_history
    return get_alert_history(
        hours_window=hours_window, alert_type=alert_type,
        severity=severity, region=region, limit=limit, by_day=by_day)


@router.get("/stats/daily-summary")
def api_daily_summary_view(
        date: Optional[str] = Query(default=None,
                                       description="YYYYMMDD, 缺省=今天 UTC")):
    """Phase 19.x.3.3: 直接服务缓存的 logs/daily_summary_YYYYMMDD.json.

    用于 webhook → 链接打开看完整摘要 (含 trend / trend_7d / by_region /
    alerts). 文件不存在返 404.
    """
    import datetime as _dt
    import json as _json
    from pathlib import Path as _Path
    if not date:
        date = _dt.datetime.utcnow().strftime("%Y%m%d")
    if not (date.isdigit() and len(date) == 8):
        raise HTTPException(400, "date 必须是 YYYYMMDD 格式")
    fpath = _Path("logs") / f"daily_summary_{date}.json"
    if not fpath.exists():
        raise HTTPException(404, f"daily_summary_{date}.json 不存在")
    try:
        with fpath.open("r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception as e:
        raise HTTPException(500, f"读取失败: {e}")


@router.get("/{account_id}")
def api_get_line_account(account_id: int):
    row = lp.get_by_id(account_id)
    if not row:
        raise HTTPException(404, "not found")
    return row


# ─── 增 ─────────────────────────────────────────────────────────────────

@router.post("")
def api_add_line_account(body: Dict[str, Any] = Body(...)):
    try:
        aid = lp.add(
            line_id=(body.get("line_id") or "").strip(),
            owner_device_id=body.get("owner_device_id", "") or "",
            persona_key=body.get("persona_key", "") or "",
            region=body.get("region", "") or "",
            status=body.get("status", "active") or "active",
            daily_cap=int(body.get("daily_cap", 20) or 20),
            notes=body.get("notes", "") or "",
        )
        return {"id": aid}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/bulk-import")
def api_bulk_import_json(body: Dict[str, Any] = Body(...)):
    """JSON 体批量: {records: [{line_id, owner_device_id, ...}, ...]}.

    文件上传 (CSV/XLSX) 走另一个端点, 本端点接收前端 parse 后的 JSON.
    """
    records = body.get("records") or []
    if not isinstance(records, list):
        raise HTTPException(400, "records 必须是 list")
    return lp.add_many(records)


@router.post("/bulk-import-csv")
async def api_bulk_import_csv(file: UploadFile = File(...)):
    """直接上传 CSV 文件. 首行 header, 支持列: line_id (必填), owner_device_id,
    persona_key, region, status, daily_cap, notes.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "只支持 .csv 文件 (XLSX 请先前端转 CSV 或 JSON)")
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"读文件失败: {e}")
    try:
        reader = csv.DictReader(io.StringIO(text))
        records: List[Dict[str, Any]] = []
        for r in reader:
            records.append({
                "line_id": (r.get("line_id") or "").strip(),
                "owner_device_id": (r.get("owner_device_id") or "").strip(),
                "persona_key": (r.get("persona_key") or "").strip(),
                "region": (r.get("region") or "").strip(),
                "status": (r.get("status") or "active").strip(),
                "daily_cap": int((r.get("daily_cap") or "20").strip() or 20),
                "notes": (r.get("notes") or "").strip(),
            })
    except Exception as e:
        raise HTTPException(400, f"CSV 解析失败: {e}")
    return lp.add_many(records)


# ─── 改/删 ─────────────────────────────────────────────────────────────

@router.put("/{account_id}")
def api_update_line_account(account_id: int, body: Dict[str, Any] = Body(...)):
    if not lp.get_by_id(account_id):
        raise HTTPException(404, "not found")
    try:
        ok = lp.update(account_id, **{
            k: body[k] for k in body
            if k in {"line_id", "owner_device_id", "persona_key",
                     "region", "status", "daily_cap", "notes"}
        })
        return {"ok": ok, "id": account_id}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{account_id}")
def api_delete_line_account(account_id: int):
    ok = lp.delete(account_id)
    if not ok:
        raise HTTPException(404, "not found")
    return {"ok": True, "id": account_id}


# ─── 轮循分配 ───────────────────────────────────────────────────────────

@router.post("/allocate")
def api_allocate(body: Dict[str, Any] = Body(default={})):
    """分配一个 LINE id (供 dispatcher / B 机 messenger 回复调用).

    Body:
      region?, persona_key?, owner_device_id?
      canonical_id?, peer_name?, source_device_id?, source_event_id?

    返回: {allocated: bool, account?: {id, line_id, ...}, reason?: str}
    """
    acc = lp.allocate(
        region=body.get("region") or None,
        persona_key=body.get("persona_key") or None,
        owner_device_id=body.get("owner_device_id") or None,
        canonical_id=body.get("canonical_id", "") or "",
        peer_name=body.get("peer_name", "") or "",
        source_device_id=body.get("source_device_id", "") or "",
        source_event_id=body.get("source_event_id", "") or "",
    )
    if acc is None:
        return {"allocated": False,
                "reason": "no_matching_account_or_all_capped"}
    return {"allocated": True, "account": acc}


@router.post("/dispatch-log/{account_id}/outcome")
def api_mark_outcome(account_id: int, body: Dict[str, Any] = Body(...)):
    """B 机/dispatcher 完成发送后回写 status (sent/failed/skipped)."""
    status = (body.get("status") or "").strip()
    note = (body.get("note") or "").strip()
    try:
        ok = lp.mark_dispatch_outcome(account_id, status=status, note=note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": ok}
