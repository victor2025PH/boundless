# -*- coding: utf-8 -*-
"""
跨系统数据分析路由 — 将 Content Studio + TikTok 引流数据打通

端点:
  GET /analytics/studio-funnel   — 内容发布 → leads 增长 → DM → 转化 完整链路
  GET /analytics/studio-timeline — 内容发布时间线（按平台/天聚合）
  GET /analytics/leads-timeline  — lead 获取时间线（按平台/天聚合）
  GET /analytics/cross-overview  — 综合大盘（各系统 KPI 汇总）
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from src.host.device_registry import data_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics-studio"])

_STUDIO_DB = data_file("studio/studio.db")
_LEADS_DB = data_file("leads.db")
_OPENCLAW_DB = data_file("openclaw.db")


# ─────────────────────────────────────────────────────────────────
# DB 工具
# ─────────────────────────────────────────────────────────────────

def _query(db_path: Path, sql: str, params: tuple = ()) -> List[Dict]:
    """只读查询，返回 dict 列表。DB 不存在时返回空列表。"""
    if not db_path.exists():
        return []
    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("DB 查询失败 %s: %s", db_path.name, e)
        return []


def _days_ago(n: int) -> str:
    """返回 n 天前的 UTC ISO 日期字符串前缀（YYYY-MM-DD）。"""
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────
# 端点 1: 完整转化漏斗
# ─────────────────────────────────────────────────────────────────

@router.get("/studio-funnel")
def get_studio_funnel(days: int = Query(30, ge=1, le=365)):
    """
    Content Studio → TikTok 引流 完整转化漏斗。

    返回各阶段数量:
      内容发布数 → 预估触达人数 → 新增 Lead 数 → DM 发送数 → 回复数 → 转化（估算）
    """
    cutoff = _days_ago(days)

    # ── Content Studio 数据 ──────────────────────────────────────
    posts = _query(_STUDIO_DB,
        "SELECT platform, COUNT(*) as cnt FROM studio_posts "
        "WHERE published_at >= ? GROUP BY platform",
        (cutoff,)
    )
    total_posts  = sum(r["cnt"] for r in posts)
    posts_by_plat = {r["platform"]: r["cnt"] for r in posts}

    # 总浏览/点赞（如果有数据）
    post_stats = _query(_STUDIO_DB,
        "SELECT SUM(views) as views, SUM(likes) as likes, "
        "SUM(comments) as comments FROM studio_posts WHERE published_at >= ?",
        (cutoff,)
    )
    total_views    = (post_stats[0]["views"]    or 0) if post_stats else 0
    total_likes    = (post_stats[0]["likes"]    or 0) if post_stats else 0
    total_comments = (post_stats[0]["comments"] or 0) if post_stats else 0

    # ── TikTok 引流数据（leads.db）──────────────────────────────
    leads_total = _query(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM leads WHERE created_at >= ?", (cutoff,)
    )
    total_leads = (leads_total[0]["cnt"] or 0) if leads_total else 0

    interactions_by_action = _query(_LEADS_DB,
        "SELECT action, COUNT(*) as cnt FROM interactions "
        "WHERE created_at >= ? GROUP BY action", (cutoff,)
    )
    action_map = {r["action"]: r["cnt"] for r in interactions_by_action}

    total_follows    = action_map.get("follow", 0) + action_map.get("followed", 0)
    total_dms_sent   = action_map.get("send_dm", 0) + action_map.get("dm_sent", 0)
    total_dms_reply  = action_map.get("reply", 0) + action_map.get("received_reply", 0)

    # ── CRM 转化（openclaw.db）──────────────────────────────────
    crm_rows = _query(_OPENCLAW_DB,
        "SELECT COUNT(*) as cnt FROM crm_interactions WHERE ts >= ? AND direction='inbound'",
        (cutoff + "T00:00:00",)
    )
    total_crm_inbound = (crm_rows[0]["cnt"] or 0) if crm_rows else 0

    # 转化率计算
    def rate(num, den):
        return round(num / den * 100, 1) if den > 0 else 0.0

    return {
        "period_days": days,
        "cutoff_date": cutoff,
        "funnel": {
            "content_published":    total_posts,
            "total_views":          total_views,
            "total_likes":          total_likes,
            "new_leads_followed":   total_follows,
            "dms_sent":             total_dms_sent,
            "dms_replied":          total_dms_reply,
            "crm_inbound_msgs":     total_crm_inbound,
        },
        "conversion_rates": {
            "views_to_follow":   rate(total_follows, total_views),
            "follow_to_dm":      rate(total_dms_sent, total_follows),
            "dm_to_reply":       rate(total_dms_reply, total_dms_sent),
            "reply_to_crm":      rate(total_crm_inbound, total_dms_reply),
        },
        "posts_by_platform": posts_by_plat,
    }


# ─────────────────────────────────────────────────────────────────
# 端点 2: 内容发布时间线
# ─────────────────────────────────────────────────────────────────

@router.get("/studio-timeline")
def get_studio_timeline(days: int = Query(14, ge=1, le=90)):
    """
    内容发布时间线（按天聚合），用于和 leads 时间线对比。

    返回: [{date, posts_count, platform}]
    """
    cutoff = _days_ago(days)
    rows = _query(_STUDIO_DB,
        "SELECT substr(published_at,1,10) as date, platform, COUNT(*) as posts "
        "FROM studio_posts WHERE published_at >= ? "
        "GROUP BY date, platform ORDER BY date",
        (cutoff,)
    )
    # 生成每日汇总
    daily_total: Dict[str, int] = {}
    for r in rows:
        daily_total[r["date"]] = daily_total.get(r["date"], 0) + r["posts"]

    return {
        "days": days,
        "by_platform": rows,
        "daily_total": [{"date": d, "posts": c} for d, c in sorted(daily_total.items())],
    }


# ─────────────────────────────────────────────────────────────────
# 端点 3: Lead 获取时间线
# ─────────────────────────────────────────────────────────────────

@router.get("/leads-timeline")
def get_leads_timeline(days: int = Query(14, ge=1, le=90)):
    """
    Lead 获取时间线（按天聚合），与内容发布时间线对比可发现相关性。
    """
    cutoff = _days_ago(days)
    rows = _query(_LEADS_DB,
        "SELECT substr(created_at,1,10) as date, COUNT(*) as leads "
        "FROM leads WHERE created_at >= ? GROUP BY date ORDER BY date",
        (cutoff,)
    )
    interactions = _query(_LEADS_DB,
        "SELECT substr(created_at,1,10) as date, action, COUNT(*) as cnt "
        "FROM interactions WHERE created_at >= ? "
        "GROUP BY date, action ORDER BY date",
        (cutoff,)
    )

    return {
        "days": days,
        "leads_by_day": rows,
        "interactions_by_day": interactions,
    }


# ─────────────────────────────────────────────────────────────────
# 端点 4: 综合大盘
# ─────────────────────────────────────────────────────────────────

@router.get("/cross-overview")
def get_cross_overview():
    """
    两系统 KPI 综合大盘（今日 + 本周 + 本月）。
    """
    today  = _days_ago(0)
    week   = _days_ago(7)
    month  = _days_ago(30)

    def _count(db, sql, cutoff):
        rows = _query(db, sql, (cutoff,))
        return (rows[0]["cnt"] or 0) if rows else 0

    studio_posts_today = _count(_STUDIO_DB,
        "SELECT COUNT(*) as cnt FROM studio_posts WHERE published_at >= ?", today)
    studio_posts_week  = _count(_STUDIO_DB,
        "SELECT COUNT(*) as cnt FROM studio_posts WHERE published_at >= ?", week)
    studio_jobs_today  = _count(_STUDIO_DB,
        "SELECT COUNT(*) as cnt FROM studio_jobs WHERE created_at >= ?", today)

    leads_today = _count(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM leads WHERE created_at >= ?", today)
    leads_week  = _count(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM leads WHERE created_at >= ?", week)
    leads_month = _count(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM leads WHERE created_at >= ?", month)

    dms_today = _count(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM interactions WHERE action='send_dm' AND created_at >= ?",
        today)
    dms_week = _count(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM interactions WHERE action='send_dm' AND created_at >= ?",
        week)

    replies_week = _count(_LEADS_DB,
        "SELECT COUNT(*) as cnt FROM interactions WHERE direction='inbound' AND created_at >= ?",
        week)

    # Content Studio pending/generating
    pending_review = _query(_STUDIO_DB,
        "SELECT COUNT(*) as cnt FROM studio_content WHERE status='ready' AND approved=0", ())
    generating = _query(_STUDIO_DB,
        "SELECT COUNT(*) as cnt FROM studio_jobs WHERE status='generating'", ())

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "content_studio": {
            "jobs_today":          studio_jobs_today,
            "posts_today":         studio_posts_today,
            "posts_this_week":     studio_posts_week,
            "pending_review":      (pending_review[0]["cnt"] or 0) if pending_review else 0,
            "currently_generating":(generating[0]["cnt"] or 0) if generating else 0,
        },
        "tiktok_funnel": {
            "new_leads_today":   leads_today,
            "new_leads_week":    leads_week,
            "new_leads_month":   leads_month,
            "dms_sent_today":    dms_today,
            "dms_sent_week":     dms_week,
            "replies_this_week": replies_week,
        },
        "health": {
            "studio_db_exists":  _STUDIO_DB.exists(),
            "leads_db_exists":   _LEADS_DB.exists(),
            "openclaw_db_exists":_OPENCLAW_DB.exists(),
        },
    }
