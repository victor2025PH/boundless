# -*- coding: utf-8 -*-
"""LeadDossier — 聚合视图 (read path)。

把散落的 leads_canonical / lead_identities / lead_journey / lead_handoffs
拼成一个完整的"卷宗", 人类 Dashboard 和 AI Agent 都能读。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from src.host.database import _connect

logger = logging.getLogger(__name__)


def get_dossier(canonical_id: str,
                 journey_limit: int = 50) -> Optional[Dict[str, Any]]:
    """返回指定 lead 的完整 dossier。

    结构:
        {
          "canonical": {...},           # leads_canonical 行
          "identities": [...],           # lead_identities 各平台
          "journey": [...],              # 最近 N 条 lead_journey (时间升序)
          "handoffs": [...],             # 相关 lead_handoffs
          "current_owner": str,          # 推导: 最近一条非 'system' 动作的 actor
          "last_action_at": str,
          "journey_summary": {           # 统计
              "total_events": int,
              "by_action": {action: count},
              "platforms": [list of platforms touched]
          }
        }

    * 若 canonical 被合并, 自动跟随 merged_into 返回 target 的 dossier
    * 不存在返回 None
    """
    if not canonical_id:
        return None
    # 跟随 merged_into
    chain = [canonical_id]
    current = canonical_id
    for _ in range(5):  # 防止循环引用, 最多 5 级
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT merged_into FROM leads_canonical WHERE canonical_id=?",
                    (current,)).fetchone()
            if not row:
                return None
            if row[0]:
                current = row[0]
                chain.append(current)
                continue
            break
        except Exception as e:
            logger.debug("[dossier] merged_into 查询失败: %s", e)
            return None
    effective_id = current

    # 主记录
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            canonical_row = conn.execute(
                "SELECT * FROM leads_canonical WHERE canonical_id=?",
                (effective_id,)).fetchone()
    except Exception:
        return None
    if not canonical_row:
        return None

    canonical = dict(canonical_row)
    try:
        canonical["metadata"] = json.loads(canonical.pop("metadata_json") or "{}")
    except Exception:
        canonical["metadata"] = {}

    # identities (跨平台身份, 含所有合并链上的 identity)
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            # 因为合并后 identities 的 canonical_id 已经改到 target, 直接查 target 即可
            rows = conn.execute(
                "SELECT * FROM lead_identities WHERE canonical_id=?"
                " ORDER BY discovered_at ASC", (effective_id,)).fetchall()
        identities = []
        for r in rows:
            d = dict(r)
            try:
                d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            except Exception:
                d["metadata"] = {}
            identities.append(d)
    except Exception:
        identities = []

    # journey (聚合合并链上所有 canonical_id 的 journey)
    try:
        placeholders = ",".join(["?"] * len(chain))
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                f"SELECT * FROM lead_journey WHERE canonical_id IN ({placeholders})"
                f" ORDER BY at ASC, id ASC LIMIT ?",
                (*chain, journey_limit),
            ).fetchall()
        journey = []
        for r in rows:
            d = dict(r)
            try:
                d["data"] = json.loads(d.pop("data_json") or "{}")
            except Exception:
                d["data"] = {}
            journey.append(d)
    except Exception:
        journey = []

    # handoffs
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(
                "SELECT * FROM lead_handoffs WHERE canonical_id=?"
                " ORDER BY created_at DESC", (effective_id,)).fetchall()
        handoffs = []
        for r in rows:
            d = dict(r)
            try:
                d["conversation_snapshot"] = json.loads(
                    d.pop("conversation_snapshot_json") or "[]")
            except Exception:
                d["conversation_snapshot"] = []
            handoffs.append(d)
    except Exception:
        handoffs = []

    # 聚合字段
    last_action_at = journey[-1]["at"] if journey else None
    current_owner = "unclaimed"
    for ev in reversed(journey):
        if ev["actor"] not in ("system", ""):
            current_owner = ev["actor"]
            break
    # 按 action 分组统计
    by_action: Dict[str, int] = {}
    platforms_seen = set()
    for ev in journey:
        a = ev.get("action") or ""
        by_action[a] = by_action.get(a, 0) + 1
        if ev.get("platform"):
            platforms_seen.add(ev["platform"])

    return {
        "canonical": canonical,
        "effective_canonical_id": effective_id,
        "canonical_chain": chain,
        "identities": identities,
        "journey": journey,
        "handoffs": handoffs,
        "current_owner": current_owner,
        "last_action_at": last_action_at,
        "journey_summary": {
            "total_events": len(journey),
            "by_action": by_action,
            "platforms": sorted(platforms_seen),
        },
    }


def search_leads(*,
                  name_like: str = "",
                  platform: str = "",
                  account_id_like: str = "",
                  lifecycle_stage: str = "",
                  tags_include: str = "",
                  score_min: int = -1,
                  score_max: int = -1,
                  sort_by: str = "",
                  limit: int = 50) -> List[Dict[str, Any]]:
    """搜索 leads (Dashboard 用)。

    name_like / account_id_like 都用 LIKE '%X%'。
    lifecycle_stage: 按生命周期阶段过滤 (N1)。
    tags_include: 逗号分隔 tag (AND, V1)。
    score_min/score_max: lead_score 区间过滤 (V1, -1=不过滤)。
    sort_by: updated_at(默认) / score_desc / score_asc / created_at (V1)。
    """
    import json as _json
    sql = ("SELECT DISTINCT c.canonical_id, c.primary_name, c.primary_language,"
           " c.primary_persona_key, c.created_at, c.metadata_json, c.tags,"
           " COALESCE(c.lifecycle_stage, 'new') AS lifecycle_stage,"
           " c.lifecycle_updated_at"
           " FROM leads_canonical c LEFT JOIN lead_identities i"
           " ON c.canonical_id = i.canonical_id"
           " WHERE c.merged_into IS NULL")
    params: list = []
    if name_like:
        sql += " AND c.primary_name LIKE ?"
        params.append(f"%{name_like}%")
    if platform:
        sql += " AND i.platform=?"
        params.append(platform)
    if account_id_like:
        sql += " AND i.account_id LIKE ?"
        params.append(f"%{account_id_like}%")
    if lifecycle_stage:
        sql += " AND COALESCE(c.lifecycle_stage, 'new')=?"
        params.append(lifecycle_stage)
    if tags_include:
        for tag in tags_include.split(","):
            tag = tag.strip()
            if tag:
                sql += " AND c.tags LIKE ?"
                params.append(f"%{tag}%")
    _sort_map = {
        "created_at": "c.created_at DESC",
        "score_desc": "c.updated_at DESC",
        "score_asc": "c.updated_at ASC",
    }
    order = _sort_map.get(sort_by, "c.updated_at DESC")
    sql += f" ORDER BY {order} LIMIT ?"
    params.append(int(limit))
    try:
        with _connect() as conn:
            conn.row_factory = __import__("sqlite3").Row
            rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # V1: 解析 lead_score 用于过滤和前端展示
            try:
                meta = _json.loads(d.pop("metadata_json", "") or "{}")
            except Exception:
                meta = {}
            d["lead_score"] = meta.get("lead_score")
            # score 区间过滤 (Python 层, metadata_json 非 SQL 可查)
            sc = d["lead_score"]
            if score_min >= 0 and (sc is None or sc < score_min):
                continue
            if score_max >= 0 and (sc is None or sc > score_max):
                continue
            results.append(d)
        # V1: 按 score 排序 (SQL 无法直接按 JSON 字段排序)
        if sort_by == "score_desc":
            results.sort(key=lambda x: -(x.get("lead_score") or 0))
        elif sort_by == "score_asc":
            results.sort(key=lambda x: (x.get("lead_score") or 0))
        return results
    except Exception as e:
        logger.debug("[dossier] search_leads 失败: %s", e)
        return []


def export_leads(*, lifecycle_stage: str = "",
                 tags_include: str = "",
                 limit: int = 5000) -> List[Dict[str, Any]]:
    """R1: 导出 lead 数据 (含 lifecycle + identities 计数 + 停留天数).

    Args:
        lifecycle_stage: 过滤阶段 (空=全部)
        tags_include: 逗号分隔的 tags (AND 过滤)
        limit: 最多导出条数
    """
    import sqlite3
    from datetime import datetime as _dt
    sql = ("SELECT c.canonical_id, c.primary_name, c.primary_language,"
           " c.primary_persona_key,"
           " COALESCE(c.lifecycle_stage, 'new') AS lifecycle_stage,"
           " c.lifecycle_updated_at, c.created_at, c.tags,"
           " (SELECT COUNT(*) FROM lead_identities i"
           "  WHERE i.canonical_id = c.canonical_id) AS identity_count"
           " FROM leads_canonical c"
           " WHERE c.merged_into IS NULL")
    params: list = []
    if lifecycle_stage:
        sql += " AND COALESCE(c.lifecycle_stage, 'new')=?"
        params.append(lifecycle_stage)
    if tags_include:
        for tag in tags_include.split(","):
            tag = tag.strip()
            if tag:
                sql += " AND c.tags LIKE ?"
                params.append(f"%{tag}%")
    sql += " ORDER BY c.created_at DESC LIMIT ?"
    params.append(min(int(limit), 10000))
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        now = _dt.utcnow()
        results = []
        for r in rows:
            row_dict = dict(r)
            # 计算当前阶段停留天数
            lc_updated = row_dict.get("lifecycle_updated_at") or row_dict.get("created_at") or ""
            dwell_days = None
            if lc_updated:
                try:
                    ts = _dt.fromisoformat(lc_updated.replace("Z", "").replace("T", " ").split("+")[0])
                    dwell_days = round((now - ts).total_seconds() / 86400, 1)
                except Exception:
                    pass
            row_dict["dwell_days"] = dwell_days
            results.append(row_dict)
        return results
    except Exception as e:
        logger.debug("[dossier] export_leads 失败: %s", e)
        return []


def get_lead_summary() -> Dict[str, Any]:
    """Z2: 轻量 lead 摘要 — 6 阶段计数 + 7 天新增趋势. 用于首页卡片."""
    import sqlite3
    result: Dict[str, Any] = {"stages": {}, "total": 0, "trend_7d": []}
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            # 按阶段计数
            for row in conn.execute(
                "SELECT COALESCE(lifecycle_stage,'new') AS stg, COUNT(*) AS cnt"
                " FROM leads_canonical WHERE merged_into IS NULL"
                " GROUP BY stg"):
                result["stages"][row["stg"]] = row["cnt"]
                result["total"] += row["cnt"]
            # 7 天新增趋势
            rows = conn.execute(
                "SELECT DATE(created_at) AS d, COUNT(*) AS cnt"
                " FROM leads_canonical"
                " WHERE merged_into IS NULL"
                "   AND created_at >= DATE('now','-7 days')"
                " GROUP BY d ORDER BY d").fetchall()
            result["trend_7d"] = [{"date": r["d"], "count": r["cnt"]} for r in rows]
    except Exception as e:
        logger.debug("[dossier] get_lead_summary 失败: %s", e)
    return result


def audit_data_integrity(*, auto_fix: bool = False) -> Dict[str, Any]:
    """Z1: 数据完整性审计 — 检测孤儿/异常/重复, 可选自动修复.

    检测项:
      1. orphan_identities — identity.canonical_id 不在 leads_canonical
      2. empty_journey_leads — canonical 无任何 journey 记录
      3. invalid_lifecycle — lifecycle_stage 不在合法集合
      4. duplicate_identities — 同 platform+account_id 绑定到多个 canonical
    """
    import sqlite3
    VALID_STAGES = {"new", "contacted", "engaged", "qualified", "converted", "lost"}
    result: Dict[str, Any] = {
        "orphan_identities": [], "empty_journey_leads": [],
        "invalid_lifecycle": [], "duplicate_identities": [],
        "summary": {}, "fixed": {},
    }
    fixed = {"orphan_deleted": 0, "lifecycle_reset": 0}
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            # 1. 孤儿 identity
            rows = conn.execute(
                "SELECT i.id, i.canonical_id, i.platform, i.account_id"
                " FROM lead_identities i"
                " LEFT JOIN leads_canonical c ON c.canonical_id = i.canonical_id"
                " WHERE c.canonical_id IS NULL"
                " LIMIT 200").fetchall()
            result["orphan_identities"] = [dict(r) for r in rows]
            if auto_fix and rows:
                ids = [r["id"] for r in rows]
                conn.execute(
                    "DELETE FROM lead_identities WHERE id IN (%s)"
                    % ",".join("?" * len(ids)), ids)
                fixed["orphan_deleted"] = len(ids)

            # 2. 空 journey lead
            rows = conn.execute(
                "SELECT c.canonical_id, c.primary_name, c.lifecycle_stage"
                " FROM leads_canonical c"
                " WHERE c.merged_into IS NULL"
                "   AND NOT EXISTS (SELECT 1 FROM lead_journey j"
                "                   WHERE j.canonical_id = c.canonical_id)"
                " LIMIT 200").fetchall()
            result["empty_journey_leads"] = [dict(r) for r in rows]

            # 3. 非法 lifecycle_stage
            rows = conn.execute(
                "SELECT canonical_id, primary_name, lifecycle_stage"
                " FROM leads_canonical"
                " WHERE merged_into IS NULL"
                "   AND lifecycle_stage IS NOT NULL"
                "   AND lifecycle_stage NOT IN (%s)"
                % ",".join("?" * len(VALID_STAGES)),
                list(VALID_STAGES)).fetchall()
            result["invalid_lifecycle"] = [dict(r) for r in rows]
            if auto_fix and rows:
                cids = [r["canonical_id"] for r in rows]
                conn.execute(
                    "UPDATE leads_canonical SET lifecycle_stage='new'"
                    " WHERE canonical_id IN (%s)"
                    % ",".join("?" * len(cids)), cids)
                fixed["lifecycle_reset"] = len(cids)

            # 4. 重复 identity (同 platform+account_id → 多 canonical)
            rows = conn.execute(
                "SELECT platform, account_id, COUNT(DISTINCT canonical_id) AS cnt,"
                "   GROUP_CONCAT(DISTINCT canonical_id) AS cids"
                " FROM lead_identities"
                " GROUP BY platform, account_id"
                " HAVING cnt > 1"
                " LIMIT 100").fetchall()
            result["duplicate_identities"] = [dict(r) for r in rows]

        result["summary"] = {
            "orphan_count": len(result["orphan_identities"]),
            "empty_journey_count": len(result["empty_journey_leads"]),
            "invalid_lifecycle_count": len(result["invalid_lifecycle"]),
            "duplicate_identity_count": len(result["duplicate_identities"]),
            "has_issues": any([
                result["orphan_identities"], result["empty_journey_leads"],
                result["invalid_lifecycle"], result["duplicate_identities"],
            ]),
        }
        result["fixed"] = fixed
    except Exception as e:
        logger.debug("[dossier] audit 失败: %s", e)
        result["error"] = str(e)
    return result
