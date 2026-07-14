# -*- coding: utf-8 -*-
"""Lead Mesh HTTP API (Phase 5)。

暴露:
  * /lead-mesh/leads/{cid}         GET   — 拉 dossier
  * /lead-mesh/leads/search        GET   — 按名字/平台搜 lead
  * /lead-mesh/leads/{cid}/journey GET   — 单独拿事件流 (便于时间轴)
  * /lead-mesh/leads/{cid}/merge-candidates GET — 合并候选
  * /lead-mesh/leads/merge         POST  — 手动合并
  * /lead-mesh/leads/merges/{id}/revert POST — 撤销合并

  * /lead-mesh/handoffs            GET   — 队列 (带状态/接收方过滤)
  * /lead-mesh/handoffs            POST  — 创建
  * /lead-mesh/handoffs/{id}       GET   — 单详情
  * /lead-mesh/handoffs/{id}/acknowledge POST
  * /lead-mesh/handoffs/{id}/complete    POST
  * /lead-mesh/handoffs/{id}/reject      POST
  * /lead-mesh/handoffs/check-duplicate  GET (query: canonical_id, channel)

  * /lead-mesh/agents/messages     POST  — send_message (HTTP 通道)
  * /lead-mesh/agents/messages     GET   — 拉自己的队列
  * /lead-mesh/agents/messages/{id}/deliver POST
  * /lead-mesh/agents/messages/{id}/ack     POST
  * /lead-mesh/agents/query-sync   POST  — 同步 query-reply (阻塞)

  * /lead-mesh/webhooks/flush      POST  — 触发 webhook dispatcher
  * /lead-mesh/webhooks/dead-letters GET — 死信查询
  * /lead-mesh/webhooks/{id}/retry POST — 重置死信

所有端点都可以被人 (curl) 或 AI Agent 直接调, 统一 JSON 接口。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from src.host import lead_mesh as lm

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/lead-mesh", tags=["lead-mesh"])


# ─── Leads / Dossier ─────────────────────────────────────────────────

# ⚠ 路由顺序: 静态路径(/leads/search, /leads/resolve, /leads/merge*) 必须
# 声明在 /leads/{canonical_id} 之前, 否则静态片段会被当成 path param 吃掉。
@router.get("/leads/search")
def api_search_leads(name_like: str = "",
                       platform: str = "",
                       account_id_like: str = "",
                       lifecycle_stage: str = Query(default="",
                           description="N1: 按生命周期过滤 (new/contacted/engaged/qualified/converted/lost)"),
                       tags: str = Query(default="",
                           description="V1: 逗号分隔 tags (AND 过滤)"),
                       score_min: int = Query(default=-1, ge=-1, le=100,
                           description="V1: 最低分 (0-100, -1=不限)"),
                       score_max: int = Query(default=-1, ge=-1, le=100,
                           description="V1: 最高分 (0-100, -1=不限)"),
                       sort_by: str = Query(default="",
                           description="V1: 排序 updated_at/score_desc/score_asc/created_at"),
                       limit: int = Query(default=50, ge=1, le=500)):
    return {"results": lm.search_leads(
        name_like=name_like, platform=platform,
        account_id_like=account_id_like,
        lifecycle_stage=lifecycle_stage.strip(),
        tags_include=tags.strip(),
        score_min=score_min, score_max=score_max,
        sort_by=sort_by.strip(),
        limit=limit)}


@router.get("/leads/export")
def api_export_leads(lifecycle_stage: str = Query(default="",
                         description="R1: 按阶段过滤"),
                     tags: str = Query(default="",
                         description="R1: 逗号分隔 tags (AND)"),
                     format: str = Query(default="csv",
                         description="csv 或 json"),
                     limit: int = Query(default=5000, ge=1, le=10000)):
    """R1: 导出 lead 数据 (含 lifecycle + 停留时长)."""
    from src.host.lead_mesh.dossier import export_leads
    rows = export_leads(lifecycle_stage=lifecycle_stage.strip(),
                        tags_include=tags.strip(), limit=limit)
    if format == "json":
        return {"count": len(rows), "results": rows}
    # CSV
    import csv, io, time as _time
    from fastapi.responses import StreamingResponse
    if not rows:
        return StreamingResponse(
            io.StringIO("no data\n"), media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=leads_export.csv"})
    fields = ["canonical_id", "primary_name", "lifecycle_stage", "dwell_days",
              "identity_count", "primary_language", "primary_persona_key",
              "tags", "created_at", "lifecycle_updated_at"]
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    out.seek(0)
    fname = f"leads_export_{_time.strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        iter([out.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"})


@router.post("/leads/resolve")
def api_resolve_identity(body: Dict[str, Any] = Body(...)):
    """按 (platform, account_id) 拿 canonical_id (不存在则创建)。

    请求体: {platform, account_id, display_name?, language?,
             discovered_via?, discovered_by_device?, extra_metadata?}
    """
    platform = (body.get("platform") or "").strip().lower()
    account_id = (body.get("account_id") or "").strip()
    if not platform or not account_id:
        raise HTTPException(400, "platform / account_id 必填")
    try:
        cid = lm.resolve_identity(
            platform=platform, account_id=account_id,
            display_name=body.get("display_name") or "",
            language=body.get("language") or "",
            persona_key=body.get("persona_key") or "",
            extra_metadata=body.get("extra_metadata") or {},
            discovered_via=body.get("discovered_via") or "",
            discovered_by_device=body.get("discovered_by_device") or "",
            auto_merge=bool(body.get("auto_merge", True)),
        )
        return {"canonical_id": cid}
    except Exception as e:
        raise HTTPException(500, f"resolve 失败: {e}")


@router.post("/leads/merge")
def api_merge_manually(body: Dict[str, Any] = Body(...)):
    """手动合并 {source_canonical_id, target_canonical_id, merged_by, reason}。"""
    from src.host.lead_mesh.canonical import merge_manually
    src = (body.get("source_canonical_id") or "").strip()
    tgt = (body.get("target_canonical_id") or "").strip()
    if not src or not tgt:
        raise HTTPException(400, "source/target 必填")
    ok = merge_manually(src, tgt,
                         merged_by=body.get("merged_by") or "human",
                         reason=body.get("reason") or "")
    return {"ok": ok, "source": src, "target": tgt}


@router.post("/leads/merges/{merge_id}/revert")
def api_revert_merge(merge_id: int, body: Dict[str, Any] = Body(default={})):
    from src.host.lead_mesh.canonical import revert_merge
    ok = revert_merge(merge_id,
                       reverted_by=body.get("reverted_by") or "human",
                       reason=body.get("reason") or "")
    return {"ok": ok, "merge_id": merge_id}


@router.post("/leads/merges/{merge_id}/review")
def api_review_merge(merge_id: int, body: Dict[str, Any] = Body(default={})):
    """Phase I3: 标记合并的审核状态 (safe/suspect)。"""
    status = (body.get("audit_status") or "safe").strip()
    if status not in ("safe", "suspect", ""):
        raise HTTPException(400, "audit_status 必须为 safe/suspect/空")
    reviewed_by = body.get("reviewed_by") or "human_dashboard"
    try:
        from src.host.database import _connect
        import datetime
        with _connect() as conn:
            conn.execute(
                "UPDATE lead_merges SET audit_status=?, reviewed_by=?,"
                " reviewed_at=? WHERE id=?",
                (status, reviewed_by,
                 datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                 merge_id),
            )
            conn.commit()
        return {"ok": True, "merge_id": merge_id, "audit_status": status}
    except Exception as e:
        raise HTTPException(500, f"审核失败: {e}")


@router.get("/leads/merges")
def api_list_merges(
    limit: int = Query(50, ge=1, le=200),
    include_reverted: bool = Query(True),
    canonical_id: Optional[str] = Query(None),
):
    """Phase E2: 合并审计历史 — 查看最近的合并/撤销操作。"""
    from src.host.lead_mesh.canonical import list_recent_merges
    merges = list_recent_merges(
        limit=limit,
        include_reverted=include_reverted,
        canonical_id=canonical_id or "",
    )
    return {
        "merges": merges,
        "count": len(merges),
        "include_reverted": include_reverted,
    }


@router.get("/leads/merges/suspects")
def api_suspect_merges(
    limit: int = Query(50, ge=1, le=200),
    name_sim_threshold: float = Query(0.25, ge=0.0, le=1.0),
    identity_count_threshold: int = Query(6, ge=2, le=50),
):
    """Phase H2: 可疑误合并检测 — 扫描最近合并, 标记名字差异大或身份数异常的记录。"""
    from src.host.lead_mesh.canonical import detect_suspect_merges
    suspects = detect_suspect_merges(
        limit=limit,
        name_sim_threshold=name_sim_threshold,
        identity_count_threshold=identity_count_threshold,
    )
    # H2: 条件性告警 (依靠 AlertNotifier 内建 5min 去重防刷)
    if suspects:
        try:
            from src.host.alert_notifier import AlertNotifier
            AlertNotifier.get().notify_event(
                "merge_quality_suspects",
                level="warning",
                alert_code="MERGE_QUALITY_SUSPECTS",
                params={"suspect_count": len(suspects)},
            )
        except Exception:
            pass
    return {"suspects": suspects, "count": len(suspects)}


_kpi_cache: Dict[str, Any] = {}  # V4: {key: {"ts": float, "data": dict}}
_KPI_CACHE_TTL = 60  # seconds


@router.get("/leads/identity-kpi")
def api_identity_kpi(
    since_days: int = Query(30, ge=1, le=365),
):
    """Phase H1+K2+L1: 跨平台统一身份解析 KPI + 生命周期分布 + 跨设备去重效果."""
    import time as _t
    cache_key = f"kpi_{since_days}"
    cached = _kpi_cache.get(cache_key)
    if cached and (_t.time() - cached["ts"]) < _KPI_CACHE_TTL:
        return cached["data"]
    from src.host.lead_mesh.canonical import (
        get_unified_identity_kpi, get_lifecycle_summary,
        get_cross_device_dedup_stats, get_lifecycle_trend,
        check_lifecycle_alerts, get_lifecycle_dwell_stats,
        check_lifecycle_sla, get_score_leaderboard)
    result = get_unified_identity_kpi(since_days=since_days)
    result["lifecycle"] = get_lifecycle_summary()
    result["lifecycle_trend"] = get_lifecycle_trend(days=min(since_days, 14))
    result["cross_device_dedup"] = get_cross_device_dedup_stats(since_days=since_days)
    result["lifecycle_alerts"] = check_lifecycle_alerts()
    result["lifecycle_dwell"] = get_lifecycle_dwell_stats()
    result["lifecycle_sla"] = check_lifecycle_sla(limit=10)
    result["score_leaderboard"] = get_score_leaderboard(top_n=10)
    result["_cached_at"] = _t.strftime("%H:%M:%S")
    _kpi_cache[cache_key] = {"ts": _t.time(), "data": result}
    return result


@router.post("/leads/lifecycle/backfill")
def api_backfill_lifecycle(body: Dict[str, Any] = Body(default={})):
    """O2: 从历史事件表回填 lifecycle_stage. body: {dry_run: true/false}"""
    from src.host.lead_mesh.canonical import backfill_lifecycle_from_history
    dry_run = body.get("dry_run", True)
    return backfill_lifecycle_from_history(dry_run=dry_run)


@router.post("/leads/lifecycle/auto-lost")
def api_auto_mark_lost(body: Dict[str, Any] = Body(default={})):
    """Q4: 自动标记长期无活动 lead 为 lost. body: {inactive_days?, dry_run?}"""
    from src.host.lead_mesh.canonical import auto_mark_lost_leads
    days = int(body.get("inactive_days", 30) or 30)
    dry_run = body.get("dry_run", True)
    return auto_mark_lost_leads(inactive_days=days, dry_run=dry_run)


@router.post("/leads/tags/batch")
def api_batch_tags(body: Dict[str, Any] = Body(...)):
    """S3: 批量添加/移除 tags. body: {canonical_ids:[...], add_tags:[], remove_tags:[]}"""
    from src.host.lead_mesh.canonical import update_canonical_metadata, remove_canonical_tags
    ids = body.get("canonical_ids") or []
    add = body.get("add_tags") or []
    remove = body.get("remove_tags") or []
    if not ids or len(ids) > 500:
        raise HTTPException(400, "canonical_ids 必填且不超过 500")
    if not add and not remove:
        raise HTTPException(400, "add_tags 或 remove_tags 至少填一项")
    ok = 0
    for cid in ids:
        if add:
            update_canonical_metadata(cid, {}, tags=add)
        if remove:
            remove_canonical_tags(cid, remove)
        ok += 1
    return {"updated": ok}


@router.post("/leads/scores/recompute")
def api_recompute_scores(body: Dict[str, Any] = Body(default={})):
    """S1: 手动触发 lead_score 批量重算. body: {limit?}"""
    from src.host.lead_mesh.canonical import batch_recompute_scores
    limit = int(body.get("limit", 2000) or 2000)
    return batch_recompute_scores(limit=limit)


@router.post("/leads/lifecycle/batch")
def api_batch_advance_lifecycle(body: Dict[str, Any] = Body(...)):
    """N1: 批量推进生命周期. body: {canonical_ids: [...], stage, force?}"""
    from src.host.lead_mesh.canonical import batch_advance_lifecycle
    ids = body.get("canonical_ids") or []
    stage = (body.get("stage") or "").strip()
    force = bool(body.get("force", False))
    if not stage:
        raise HTTPException(400, "stage 必填")
    if not ids or len(ids) > 500:
        raise HTTPException(400, "canonical_ids 必填且不超过 500")
    return batch_advance_lifecycle(ids, stage, force=force)


@router.post("/leads/{canonical_id}/notes")
def api_add_note(canonical_id: str, body: Dict[str, Any] = Body(...)):
    """W4: 添加运营备注. body: {text, author?}"""
    from src.host.lead_mesh.journey import append_journey
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 必填")
    author = (body.get("author") or "dashboard_user").strip()
    try:
        append_journey(canonical_id, actor=author,
                       action="note_added",
                       data={"text": text})
        return {"ok": True, "canonical_id": canonical_id}
    except Exception as e:
        raise HTTPException(500, f"添加备注失败: {e}")


@router.post("/leads/{canonical_id}/quick-action")
def api_quick_action(canonical_id: str,
                     body: Dict[str, Any] = Body(...)):
    """U1: 统一快捷操作入口. body: {action: str, params?: {}}

    支持的 action:
      - advance_lifecycle: 推进阶段 (params.stage)
      - create_handoff: 创建引流交接 (params.channel, params.source_agent)
      - allocate_line: 分配 LINE 账号
      - send_greeting: (仅记录 journey, 实际发送需要设备端执行)
    """
    action = (body.get("action") or "").strip()
    params = body.get("params") or {}
    if not action:
        raise HTTPException(400, "action 必填")

    if action == "advance_lifecycle":
        from src.host.lead_mesh.canonical import advance_lifecycle
        stage = (params.get("stage") or "").strip()
        if not stage:
            raise HTTPException(400, "params.stage 必填")
        ok = advance_lifecycle(canonical_id, stage,
                               force=bool(params.get("force", False)))
        return {"ok": ok, "action": action, "detail": {"stage": stage}}

    elif action == "create_handoff":
        channel = (params.get("channel") or "whatsapp").strip()
        src_agent = (params.get("source_agent") or "dashboard_user").strip()
        hid = lm.create_handoff(
            canonical_id=canonical_id, source_agent=src_agent,
            channel=channel, source_device="",
            target_agent=params.get("target_agent") or "",
            receiver_account_key=params.get("receiver_account_key") or "",
        )
        if not hid:
            return {"ok": False, "action": action, "error": "create_handoff 失败"}
        return {"ok": True, "action": action, "detail": {"handoff_id": hid}}

    elif action == "allocate_line":
        try:
            from src.host.line_pool import allocate
            result = allocate(canonical_id=canonical_id,
                              purpose=params.get("purpose") or "quick_action")
            return {"ok": bool(result), "action": action,
                    "detail": {"line_account": result.get("line_id", "") if result else ""}}
        except Exception as e:
            return {"ok": False, "action": action, "error": str(e)[:120]}

    elif action == "log_intent":
        # 记录运营意图到 journey (实际执行需设备端)
        try:
            from src.host.lead_mesh.journey import append_journey
            intent = params.get("intent") or action
            append_journey(canonical_id, actor="dashboard_user",
                           action=f"intent_{intent}",
                           data={"source": "quick_action", **params})
            return {"ok": True, "action": action, "detail": {"intent": intent}}
        except Exception as e:
            return {"ok": False, "action": action, "error": str(e)[:120]}
    else:
        raise HTTPException(400, f"未知 action: {action}")


@router.post("/leads/{canonical_id}/lifecycle")
def api_advance_lifecycle(canonical_id: str,
                          body: Dict[str, Any] = Body(default={})):
    """K2: 手动/自动推进 lead 生命周期阶段."""
    from src.host.lead_mesh.canonical import advance_lifecycle
    stage = (body.get("stage") or "").strip()
    force = bool(body.get("force", False))
    if not stage:
        raise HTTPException(400, "stage 必填 (new/contacted/engaged/qualified/converted/lost)")
    ok = advance_lifecycle(canonical_id, stage, force=force)
    return {"ok": ok, "canonical_id": canonical_id, "stage": stage}


@router.get("/leads/l2-verified")
def api_list_l2_verified_leads(
        age_band: Optional[str] = Query(default=None,
                                         description="例如 '40s' / '50s'"),
        gender: Optional[str] = Query(default=None,
                                       description="'female' / 'male'"),
        is_japanese: Optional[bool] = Query(default=None),
        persona_key: Optional[str] = Query(default=None,
                                            description="L2 匹配用的 persona"),
        platform: Optional[str] = Query(default=None,
                                         description="'facebook' / ..."),
        min_score: float = Query(default=0, ge=0, le=100),
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0, description="Phase 12.4 分页"),
        with_total: bool = Query(default=False,
            description="Phase 12.5: True 时多返 total (SQL COUNT, 仅按 tag)"),
        include_tags: Optional[List[str]] = Query(
            default=None,
            description="tags 必须全部包含, 例 ['line_referred']"),
        exclude_tags: Optional[List[str]] = Query(
            default=None,
            description="含任一此 tag 的 lead 排除, 例 ['referral_dead']")):
    """Phase 10.3 + 12.2: 查 L2 VLM 验证过的精准用户.

    只返回 tags 里带 ``l2_verified`` 的 lead, 按 l2_score 降序. 所有过滤 AND.
    Phase 12.2 新增 include/exclude tags: 例如查"已引流的":
      /leads/l2-verified?include_tags=line_referred
    查"L2 通过但 referral 已死不再骚扰的":
      /leads/l2-verified?include_tags=referral_dead
    """
    from src.host.lead_mesh.canonical import list_l2_verified_leads
    rows = list_l2_verified_leads(
        age_band=age_band, gender=gender,
        is_japanese=is_japanese, persona_key=persona_key,
        platform=platform, min_score=min_score, limit=limit,
        offset=offset,
        include_tags=include_tags, exclude_tags=exclude_tags,
    )
    out = {"count": len(rows), "results": rows, "offset": offset,
           "limit": limit}
    if with_total:
        from src.host.lead_mesh.canonical import count_l2_verified_leads
        out["total"] = count_l2_verified_leads(
            include_tags=include_tags, exclude_tags=exclude_tags)
    return out


@router.post("/leads/{canonical_id}/revive-referral")
def api_revive_referral(canonical_id: str,
                         body: Dict[str, Any] = Body(default={})):
    """Phase 12.3: 给 peer 第二次机会 — 去 referral_dead tag + 清 fail counters.

    Body 可选 ``actor`` (默认 ``operator_ui``, 写 journey 审计).
    """
    from src.host.lead_mesh import revive_referral
    actor = (body.get("actor") or "operator_ui").strip() or "operator_ui"
    ok = revive_referral(canonical_id, actor=actor)
    return {"ok": ok, "canonical_id": canonical_id}


@router.post("/leads/revive-referral-batch")
def api_revive_referral_batch(body: Dict[str, Any] = Body(...)):
    """Phase 12.4: 批量给多个 peer revive referral. 单条异常不中断.

    Body: {canonical_ids: [...], actor?: "operator_ui"}
    Returns: {revived: N, skipped: M, errors: [...], revived_ids: [...]}.
    """
    cids = body.get("canonical_ids") or []
    if not isinstance(cids, list):
        raise HTTPException(400, "canonical_ids 必须是 list")
    if not cids:
        return {"revived": 0, "skipped": 0, "errors": [],
                "revived_ids": []}
    # 去重 + 去空
    seen = []
    seen_set = set()
    for c in cids:
        if not isinstance(c, str):
            continue
        c2 = c.strip()
        if not c2 or c2 in seen_set:
            continue
        seen_set.add(c2)
        seen.append(c2)

    actor = (body.get("actor") or "operator_ui").strip() or "operator_ui"
    from src.host.lead_mesh import revive_referral
    from src.host.lead_mesh.canonical import _connect

    # Phase 12.5: 短 token (< 36 字符 UUID 长度) 视为 prefix, 自动 LIKE 展开.
    # 用 parameterized query 防 SQL injection. 至少 3 字符避免 'a' 匹配过多.
    expanded: List[str] = []
    errors: List[Dict[str, str]] = []
    UUID_LEN = 36
    PREFIX_MIN = 3
    for token in seen:
        if len(token) >= UUID_LEN:
            # 完整 canonical_id, 直传
            expanded.append(token)
            continue
        if len(token) < PREFIX_MIN:
            errors.append({"canonical_id": token,
                            "reason": f"prefix 太短 (<{PREFIX_MIN} 字符)"})
            continue
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT canonical_id FROM leads_canonical"
                    " WHERE canonical_id LIKE ? LIMIT 5",
                    (token + "%",),
                ).fetchall()
        except Exception as e:
            errors.append({"canonical_id": token,
                            "reason": f"prefix 查询异常: {str(e)[:100]}"})
            continue
        if not rows:
            errors.append({"canonical_id": token,
                            "reason": "prefix 无匹配"})
            continue
        if len(rows) > 1:
            errors.append({
                "canonical_id": token,
                "reason": (f"prefix 歧义 ({len(rows)} 个匹配, "
                            f"e.g. {rows[0]['canonical_id'][:12]}, "
                            f"{rows[1]['canonical_id'][:12]}, ...)"
                            " — 请扩展 prefix 长度"),
            })
            continue
        expanded.append(rows[0]["canonical_id"])

    # 去重 expanded (展开后可能两个 prefix 指向同一 cid)
    seen2 = []
    seen2_set = set()
    for c in expanded:
        if c not in seen2_set:
            seen2_set.add(c)
            seen2.append(c)

    revived_ids: List[str] = []
    skipped = 0
    for cid in seen2:
        try:
            ok = revive_referral(cid, actor=actor)
            if ok:
                revived_ids.append(cid)
            else:
                skipped += 1
        except Exception as e:
            errors.append({"canonical_id": cid, "reason": str(e)[:200]})
    return {
        "revived": len(revived_ids), "skipped": skipped,
        "errors": errors[:50], "revived_ids": revived_ids,
        "expanded_count": len(seen2),
    }


@router.post("/leads/{canonical_id}/untag")
def api_untag(canonical_id: str, body: Dict[str, Any] = Body(...)):
    """Phase 12.3 通用 untag: body {tags: [...]}. 返 {ok: bool}."""
    from src.host.lead_mesh import remove_canonical_tags
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        raise HTTPException(400, "tags 必须是 list")
    ok = remove_canonical_tags(canonical_id, tags)
    return {"ok": ok, "canonical_id": canonical_id, "removed": tags}


# 动态路径(path param) 必须在所有同前缀静态路径之后
@router.get("/leads/{canonical_id}")
def api_get_dossier(canonical_id: str, journey_limit: int = 100):
    d = lm.get_dossier(canonical_id, journey_limit=journey_limit)
    if not d:
        raise HTTPException(404, "lead not found")
    return d


@router.get("/leads/{canonical_id}/journey")
def api_get_journey(canonical_id: str,
                     limit: int = Query(default=100, ge=1, le=1000),
                     action_prefix: str = "",
                     since_iso: str = ""):
    return {"journey": lm.get_journey(canonical_id, limit=limit,
                                        action_prefix=action_prefix,
                                        since_iso=since_iso)}


@router.get("/leads/{canonical_id}/merge-candidates")
def api_merge_candidates(canonical_id: str,
                          min_confidence: float = 0.70):
    return {"candidates": lm.auto_merge_candidates(canonical_id,
                                                      min_confidence=min_confidence)}


# ─── Handoffs ────────────────────────────────────────────────────────

@router.get("/handoffs/check-duplicate")
def api_check_duplicate(canonical_id: str, channel: str, since_days: int = 30):
    """B 发引流前的去重检查端点。

    ⚠ 注意: 此路由必须在 ``/handoffs/{handoff_id}`` 之前注册, 否则
    ``check-duplicate`` 会被当成 handoff_id 匹配走。
    """
    from src.host.lead_mesh.handoff import check_duplicate_handoff
    dup = check_duplicate_handoff(canonical_id, channel, since_days=since_days)
    return {"is_duplicate": dup is not None, "existing": dup}


@router.get("/handoffs")
def api_list_handoffs(state: str = "",
                       receiver_account_key: str = "",
                       canonical_id: str = "",
                       channel: str = "",
                       limit: int = Query(default=100, ge=1, le=500)):
    return {"handoffs": lm.list_handoffs(
        state=state, receiver_account_key=receiver_account_key,
        canonical_id=canonical_id, channel=channel, limit=limit)}


@router.post("/handoffs")
def api_create_handoff(body: Dict[str, Any] = Body(...)):
    cid = (body.get("canonical_id") or "").strip()
    src_agent = (body.get("source_agent") or "").strip()
    channel = (body.get("channel") or "").strip()
    if not cid or not src_agent or not channel:
        raise HTTPException(400, "canonical_id / source_agent / channel 必填")
    hid = lm.create_handoff(
        canonical_id=cid, source_agent=src_agent, channel=channel,
        source_device=body.get("source_device") or "",
        target_agent=body.get("target_agent") or "",
        receiver_account_key=body.get("receiver_account_key") or "",
        conversation_snapshot=body.get("conversation_snapshot") or [],
        snippet_sent=body.get("snippet_sent") or "",
        enqueue_webhook=bool(body.get("enqueue_webhook", True)),
    )
    if not hid:
        raise HTTPException(500, "create handoff 失败")
    return {"handoff_id": hid}


@router.get("/handoffs/{handoff_id}")
def api_get_handoff(handoff_id: str):
    h = lm.get_handoff(handoff_id)
    if not h:
        raise HTTPException(404, "handoff not found")
    return h


@router.post("/handoffs/{handoff_id}/acknowledge")
def api_ack_handoff(handoff_id: str, body: Dict[str, Any] = Body(default={})):
    by = (body.get("by") or "").strip() or "human"
    ok = lm.acknowledge_handoff(handoff_id, by=by, notes=body.get("notes") or "")
    if not ok:
        raise HTTPException(409, "状态转移失败(可能已非 pending)")
    return {"ok": True, "new_state": "acknowledged"}


@router.post("/handoffs/{handoff_id}/complete")
def api_complete_handoff(handoff_id: str, body: Dict[str, Any] = Body(default={})):
    by = (body.get("by") or "").strip() or "human"
    ok = lm.complete_handoff(handoff_id, by=by, notes=body.get("notes") or "")
    if not ok:
        raise HTTPException(409, "状态转移失败")
    return {"ok": True, "new_state": "completed"}


@router.post("/handoffs/{handoff_id}/reject")
def api_reject_handoff(handoff_id: str, body: Dict[str, Any] = Body(default={})):
    by = (body.get("by") or "").strip() or "human"
    ok = lm.reject_handoff(handoff_id, by=by, notes=body.get("notes") or "")
    if not ok:
        raise HTTPException(409, "状态转移失败")
    return {"ok": True, "new_state": "rejected"}


# ─── Agent Mesh ──────────────────────────────────────────────────────

@router.post("/agents/messages")
def api_send_message(body: Dict[str, Any] = Body(...)):
    """SQLite + HTTP 双通道的 HTTP 入口。"""
    frm = (body.get("from_agent") or "").strip()
    to = (body.get("to_agent") or "").strip()
    if not frm or not to:
        raise HTTPException(400, "from_agent / to_agent 必填")
    cid = lm.send_message(
        from_agent=frm, to_agent=to,
        message_type=body.get("message_type") or "notification",
        canonical_id=body.get("canonical_id") or "",
        payload=body.get("payload") or {},
        correlation_id=body.get("correlation_id") or "",
    )
    return {"correlation_id": cid}


@router.get("/agents/messages")
def api_poll_messages(to_agent: str,
                        message_type: str = "",
                        status: str = "pending",
                        limit: int = Query(default=50, ge=1, le=200)):
    msgs = lm.poll_messages(to_agent, message_type=message_type,
                              status=status, limit=limit)
    return {"messages": msgs, "count": len(msgs)}


@router.post("/agents/messages/{message_id}/deliver")
def api_mark_delivered(message_id: int):
    ok = lm.mark_delivered(message_id)
    return {"ok": ok}


@router.post("/agents/messages/{message_id}/ack")
def api_mark_ack(message_id: int, body: Dict[str, Any] = Body(default={})):
    ok = lm.mark_acknowledged(message_id, error=body.get("error") or "")
    return {"ok": ok}


@router.post("/agents/query-sync")
def api_query_sync(body: Dict[str, Any] = Body(...)):
    """HTTP 同步 query-reply。阻塞等 reply 或超时。

    ⚠ 慎用: 阻塞 FastAPI worker thread. 对于不需实时的场景仍推荐异步 poll 模式。
    """
    frm = (body.get("from_agent") or "").strip()
    to = (body.get("to_agent") or "").strip()
    if not frm or not to:
        raise HTTPException(400, "from_agent / to_agent 必填")
    reply = lm.query_sync(
        from_agent=frm, to_agent=to,
        payload=body.get("payload") or {},
        canonical_id=body.get("canonical_id") or "",
        timeout_sec=float(body.get("timeout_sec", 30)),
        poll_interval=float(body.get("poll_interval", 1.0)),
    )
    return {"reply": reply, "timed_out": reply is None}


# ─── Receivers (接收方账号管理, Phase 6.B) ──────────────────────────

@router.get("/receivers")
def api_list_receivers(channel: str = "",
                         enabled_only: bool = False,
                         with_load: bool = True):
    """列所有接收方, with_load=True 时附每个的今日负载。"""
    from src.host.lead_mesh.receivers import (list_receivers, receiver_load,
                                                 all_loads)
    items = list_receivers(channel=channel or None,
                             enabled_only=enabled_only)
    if with_load:
        # 按 key 合并 load 信息
        loads = {l["key"]: l for l in all_loads()}
        for it in items:
            ld = loads.get(it["key"], {})
            for k in ("current", "cap", "remaining",
                       "percent_used", "at_cap"):
                if k in ld:
                    it[k] = ld[k]
            it["account_id_masked"] = ld.get("account_id_masked", "")
    return {"receivers": items, "count": len(items)}


@router.get("/receivers/{key}")
def api_get_receiver(key: str):
    from src.host.lead_mesh.receivers import get_receiver, receiver_load
    r = get_receiver(key)
    if not r:
        raise HTTPException(404, "receiver not found")
    r.update({"load": receiver_load(key)})
    return r


@router.post("/receivers/{key}")
def api_upsert_receiver(key: str, body: Dict[str, Any] = Body(...)):
    """新建或更新一个 receiver。"""
    from src.host.lead_mesh.receivers import upsert_receiver
    if not body.get("channel") and not body.get("account_id"):
        # 允许只改部分字段(如只 toggle enabled), 但至少得有 1 个字段
        if not any(k in body for k in ("enabled", "daily_cap",
                                          "backup_key", "persona_filter",
                                          "display_name", "tags",
                                          "webhook_url")):
            raise HTTPException(400, "body 至少包含一个字段")
    try:
        r = upsert_receiver(key, body)
        return {"ok": True, "receiver": r}
    except Exception as e:
        raise HTTPException(500, f"upsert 失败: {e}")


@router.delete("/receivers/{key}")
def api_delete_receiver(key: str):
    from src.host.lead_mesh.receivers import delete_receiver
    ok = delete_receiver(key)
    if not ok:
        raise HTTPException(404, "receiver not found")
    return {"ok": True, "deleted": key}


@router.get("/receivers-pick")
def api_pick_receiver(channel: str,
                       persona_key: str = "",
                       preferred_key: str = ""):
    """按 channel + persona 模拟 pick_receiver(不实际占位,只返回谁会被选)。

    给 Dashboard 看"当前引流到某渠道会路由到哪个账号"用。
    """
    from src.host.lead_mesh.receivers import pick_receiver
    picked = pick_receiver(channel, persona_key=persona_key or None,
                             preferred_key=preferred_key or None)
    return {"channel": channel, "persona_key": persona_key,
            "picked": picked,
            "all_at_cap": picked is None}


# ─── Webhooks ─────────────────────────────────────────────────────────

@router.post("/webhooks/flush")
def api_flush_webhooks(max_batch: int = Query(default=50, ge=1, le=500)):
    """手动触发 webhook dispatcher (也可由定时任务周期调)。"""
    stats = lm.flush_pending_webhooks(max_batch=max_batch)
    return {"ok": True, "stats": stats}


@router.get("/leads/summary")
def api_lead_summary():
    """Z2: 轻量 lead 摘要 (首页卡片用)."""
    from src.host.lead_mesh.dossier import get_lead_summary
    return get_lead_summary()


@router.get("/leads/audit")
def api_audit(auto_fix: bool = Query(default=False)):
    """Z1: 数据完整性审计 (auto_fix=true 自动修复孤儿+非法状态)."""
    from src.host.lead_mesh.dossier import audit_data_integrity
    return audit_data_integrity(auto_fix=auto_fix)


@router.get("/webhooks/stats")
def api_webhook_stats(since_days: int = Query(default=7, ge=1, le=90)):
    """Y1: Webhook dispatch 监控统计."""
    from src.host.lead_mesh.webhook_dispatcher import get_webhook_stats
    return get_webhook_stats(since_days=since_days)


@router.get("/webhooks/dead-letters")
def api_list_dead_letters(limit: int = Query(default=100, ge=1, le=500)):
    from src.host.lead_mesh.webhook_dispatcher import list_dead_letters
    return {"dead_letters": list_dead_letters(limit)}


@router.post("/webhooks/{dispatch_id}/retry")
def api_retry_dead(dispatch_id: int):
    from src.host.lead_mesh.webhook_dispatcher import retry_dead_letter
    ok = retry_dead_letter(dispatch_id)
    return {"ok": ok}


# ── Phase 8h: Blocklist (运营一键 skip 骚扰保护) ────────────────────
@router.post("/peers/{canonical_id}/blocklist")
def api_add_blocklist(canonical_id: str, body: Dict[str, Any] = Body(default={})):
    """加入 blocklist. body 可传 {reason, note, created_by}."""
    from src.host.lead_mesh import add_to_blocklist
    created = add_to_blocklist(
        canonical_id,
        reason=str(body.get("reason") or ""),
        note=str(body.get("note") or ""),
        created_by=str(body.get("created_by") or "operator"))
    return {"ok": True, "canonical_id": canonical_id,
             "created": created, "was_already_blocklisted": not created}


@router.delete("/peers/{canonical_id}/blocklist")
def api_remove_blocklist(canonical_id: str):
    from src.host.lead_mesh import remove_from_blocklist
    removed = remove_from_blocklist(canonical_id)
    return {"ok": True, "canonical_id": canonical_id, "removed": removed}


@router.get("/blocklist")
def api_list_blocklist(limit: int = Query(default=50, ge=1, le=200)):
    from src.host.lead_mesh import list_blocklist, count_blocklist
    items = list_blocklist(limit=limit)
    return {"total": count_blocklist(), "count": len(items), "items": items}


# ── Phase 8b: 漏斗报告 (给 Command Center Dashboard 卡片用) ─────────
@router.get("/funnel")
def api_funnel_report(days: int = Query(default=7, ge=1, le=90),
                       actor: str = Query(default=""),
                       date: str = Query(default="")):
    """A 端 add_friend → greeting 漏斗统计. 从 lead_journey 聚合.

    Args:
        days: 时间窗口 (1-90 天; date 未提供时生效)
        actor: 可选过滤 agent_a / agent_b; 空 = 不限
        date: Phase 8g 下钻参数, YYYY-MM-DD 单日过滤 (优先于 days).
              非法格式降级到 days.
    """
    from src.host.lead_mesh.funnel_report import compute_funnel
    stats = compute_funnel(days=days, actor=actor or None,
                             date=date or None)
    return stats.to_dict()


# ── Phase 8e: 近 N 天按日时序 (Dashboard sparkline 用) ──────────────
@router.get("/funnel/timeseries")
def api_funnel_timeseries(days: int = Query(default=7, ge=1, le=90),
                            actor: str = Query(default="")):
    """近 N 天按日分桶的漏斗时序. 缺失日填 0 避免 sparkline 断线."""
    from src.host.lead_mesh.funnel_report import compute_funnel_timeseries
    series = compute_funnel_timeseries(days=days, actor=actor or None)
    return {"days": days, "actor": actor or "", "series": series}


# ── Phase 8d: 点击某 blocked reason 看具体 peer 列表 ────────────────
@router.get("/funnel/blocked-peers")
def api_blocked_peers(reason: str = Query(...),
                       days: int = Query(default=7, ge=1, le=90),
                       limit: int = Query(default=50, ge=1, le=200),
                       date: str = Query(default="")):
    """被某 greeting_blocked.reason 挡住的 peer 列表, 按最近时间倒序.

    供 Dashboard 点击 top_blocked_reason 子 modal 展示, 帮运营定位个案.
    date 参数 (Phase 8g): YYYY-MM-DD 单日过滤, 优先于 days.
    """
    from src.host.lead_mesh.funnel_report import list_blocked_peers
    peers = list_blocked_peers(reason=reason, days=days, limit=limit,
                                 date=date or None)
    return {"reason": reason, "days": days, "date": date or "",
             "count": len(peers), "peers": peers}


# ─── PR-6 真人客服接管 ───────────────────────────────────────────────

@router.post("/handoffs/{handoff_id}/assign")
def api_handoff_assign(handoff_id: str, body: Dict[str, Any] = Body(...)):
    """真人按"我接手".

    Body: {username, peer_name?, device_id?, takeover_ttl_sec?}
    peer_name + device_id 给了, 同时调 ai_takeover_state.mark_taken_over
    暂停 worker AI 自动回 (PR-7 ai_takeover_state 模块).
    """
    username = (body.get("username") or "").strip()
    if not username:
        raise HTTPException(400, "username 必填")
    try:
        from src.host.lead_mesh.customer_service import assign_to_human
        return assign_to_human(
            handoff_id, username,
            peer_name_hint=body.get("peer_name") or "",
            device_id_hint=body.get("device_id") or "",
            takeover_ttl_sec=float(body.get("takeover_ttl_sec") or 3600.0),
        )
    except KeyError:
        raise HTTPException(404, f"handoff {handoff_id} not found")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/handoffs/{handoff_id}/reply")
def api_handoff_reply(handoff_id: str, body: Dict[str, Any] = Body(...)):
    """真人后台输入消息 (PR-6 阶段先记录, PR-6.5 接 worker 实发).

    Body: {username, text, sent_via_worker?, meta?}
    """
    username = (body.get("username") or "").strip()
    text = (body.get("text") or "").strip()
    if not username or not text:
        raise HTTPException(400, "username / text 必填")
    try:
        from src.host.lead_mesh.customer_service import record_human_reply
        return record_human_reply(
            handoff_id, username, text,
            sent_via_worker=bool(body.get("sent_via_worker", False)),
            extra_meta=body.get("meta"),
        )
    except KeyError:
        raise HTTPException(404, f"handoff {handoff_id} not found")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/handoffs/{handoff_id}/note")
def api_handoff_note(handoff_id: str, body: Dict[str, Any] = Body(...)):
    """真人加内部备注 (不发给客户).

    Body: {username, note}
    """
    username = (body.get("username") or "").strip()
    note = (body.get("note") or "").strip()
    if not username or not note:
        raise HTTPException(400, "username / note 必填")
    try:
        from src.host.lead_mesh.customer_service import record_internal_note
        return record_internal_note(handoff_id, username, note)
    except KeyError:
        raise HTTPException(404, f"handoff {handoff_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/handoffs/{handoff_id}/outcome")
def api_handoff_outcome(handoff_id: str, body: Dict[str, Any] = Body(...)):
    """真人标结果. converted/lost 终态会释放 ai 接管.

    Body: {username, outcome (converted|lost|pending_followup),
           notes?, peer_name?, device_id?}
    """
    username = (body.get("username") or "").strip()
    outcome = (body.get("outcome") or "").strip()
    if not username or not outcome:
        raise HTTPException(400, "username / outcome 必填")
    try:
        from src.host.lead_mesh.customer_service import record_outcome
        return record_outcome(
            handoff_id, username, outcome,
            notes=body.get("notes") or "",
            peer_name_hint=body.get("peer_name") or "",
            device_id_hint=body.get("device_id") or "",
        )
    except KeyError:
        raise HTTPException(404, f"handoff {handoff_id} not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/handoffs/{handoff_id}/full")
def api_handoff_full(handoff_id: str):
    """读 handoff 全字段 (含 replies / notes / outcome). 给真人后台详情页用."""
    from src.host.lead_mesh.customer_service import get_handoff_full
    rec = get_handoff_full(handoff_id)
    if not rec:
        raise HTTPException(404, f"handoff {handoff_id} not found")
    return rec


@router.get("/events/stream")
def api_events_stream():
    """Phase-2: SSE 实时事件流. 浏览器 EventSource 订阅.

    事件类型: handoff_pending_changed / handoff_assigned / handoff_outcome /
    handoff_replied / handoff_note. 详见 events_stream.py.
    """
    from fastapi.responses import StreamingResponse
    from src.host.lead_mesh.events_stream import event_stream
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/handoffs/assigned/{username}")
def api_handoffs_assigned_to(username: str, limit: int = Query(50, ge=1, le=500)):
    """列出某 username 当前接管中的 handoff (outcome 还没标的)."""
    from src.host.lead_mesh.customer_service import list_assigned_to_user
    rows = list_assigned_to_user(username, limit=limit)
    return {"username": username, "count": len(rows), "handoffs": rows}
