# -*- coding: utf-8 -*-
"""种子账号质量追踪 — 记录每个种子账号的关注/回复/引流/转化数据。"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_LOW_HIT_RATE = 0.03   # 命中率 < 3% → 降权
_HIGH_HIT_RATE = 0.15  # 命中率 > 15% → 升权


def record_seed_usage(seed_username: str, device_id: str, country: str,
                       follows: int = 0) -> None:
    """在 smart_follow 完成后调用，记录种子使用次数。"""
    try:
        from .database import get_conn
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO seed_quality (seed_username, device_id, country, follows_count, last_used_at, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(seed_username, device_id) DO UPDATE SET
                     follows_count = follows_count + excluded.follows_count,
                     last_used_at = excluded.last_used_at
                """,
                (seed_username, device_id, country, follows, now, now),
            )
        logger.debug("[种子追踪] %s: +%d follows", seed_username, follows)
    except Exception as e:
        logger.debug("[种子追踪] record_seed_usage失败: %s", e)


def record_seed_reply(seed_username: str, device_id: str) -> None:
    """当从某种子关注的用户回复时调用（attribution）。"""
    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute(
                """UPDATE seed_quality SET replies_count = replies_count + 1
                   WHERE seed_username = ? AND device_id = ?""",
                (seed_username, device_id),
            )
    except Exception as e:
        logger.debug("[种子追踪] record_seed_reply失败: %s", e)


def record_seed_referral(seed_username: str, device_id: str) -> None:
    """当从某种子关注的用户完成引流时调用。"""
    try:
        from .database import get_conn
        with get_conn() as conn:
            conn.execute(
                """UPDATE seed_quality SET referrals_count = referrals_count + 1
                   WHERE seed_username = ? AND device_id = ?""",
                (seed_username, device_id),
            )
    except Exception as e:
        logger.debug("[种子追踪] record_seed_referral失败: %s", e)


def get_seed_quality_ranking(country: str, limit: int = 10) -> list:
    """
    返回按命中率排序的种子账号列表。
    hit_rate = replies_count / follows_count
    权重分级：hit_rate < 3% → weight=0.3，3-15% → weight=1.0，> 15% → weight=2.0
    """
    try:
        from .database import get_conn
        with get_conn() as conn:
            rows = conn.execute(
                """SELECT seed_username, device_id, follows_count, replies_count,
                          referrals_count, conversions_count,
                          CAST(replies_count AS REAL) / NULLIF(follows_count, 0) AS hit_rate
                   FROM seed_quality
                   WHERE country = ? AND follows_count >= 5
                   ORDER BY hit_rate DESC NULLS LAST
                   LIMIT ?""",
                (country, limit),
            ).fetchall()
        return [
            {
                "seed": r[0], "device_id": r[1],
                "follows": r[2], "replies": r[3],
                "referrals": r[4], "conversions": r[5],
                "hit_rate": round(r[6] or 0, 4),
                "weight": (2.0 if (r[6] or 0) >= _HIGH_HIT_RATE
                           else 0.3 if (r[6] or 0) < _LOW_HIT_RATE
                           else 1.0),
            }
            for r in rows
        ]
    except Exception as e:
        logger.debug("[种子追踪] get_seed_quality_ranking失败: %s", e)
        return []


def get_best_seeds(country: str, limit: int = 5) -> list:
    """返回该国家命中率最高的种子账号名列表（用于轮转优先）。"""
    ranking = get_seed_quality_ranking(country, limit=20)
    high_quality = [r["seed"] for r in ranking if r["weight"] >= 1.0]
    return high_quality[:limit]
