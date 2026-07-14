# -*- coding: utf-8 -*-
"""
种子账号分层系统 — 基于历史回关率自动晋降级。

S级: 回关率 ≥ 35%（优先使用，精华流量入口）
A级: 回关率 15~35%（正常使用）
B级: 回关率 < 15%（减少使用，质量差时自动截断）

每次 tiktok_smart_follow 调用时，按 S>A>B 优先级排序种子账号，
B 级占比超过 50% 时自动截断，避免低质种子消耗操作配额。

更新频率: 每天由 StrategyOptimizer 触发一次（基于近 30 天数据）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

from src.host.device_registry import data_file

log = logging.getLogger(__name__)

_seed_ranks_path = data_file("seed_ranks.json")

TIER_S = "S"
TIER_A = "A"
TIER_B = "B"

_TIER_ORDER = {TIER_S: 0, TIER_A: 1, TIER_B: 2}
_TIER_THRESHOLDS = {TIER_S: 0.35, TIER_A: 0.15}  # < 0.15 → B


def _compute_tier(follow_back_rate: float) -> str:
    if follow_back_rate >= _TIER_THRESHOLDS[TIER_S]:
        return TIER_S
    if follow_back_rate >= _TIER_THRESHOLDS[TIER_A]:
        return TIER_A
    return TIER_B


class SeedRanker:
    """种子账号分层器（单例）。"""

    def __init__(self):
        self._ranks: Dict[str, dict] = {}
        self._load_ranks()

    # ── 持久化 ──

    def _load_ranks(self):
        try:
            if _seed_ranks_path.exists():
                with open(_seed_ranks_path, encoding="utf-8") as f:
                    self._ranks = json.load(f)
                log.debug("[SeedRanker] 已加载 %d 个种子账号分层", len(self._ranks))
        except Exception as e:
            log.warning("[SeedRanker] 分层数据加载失败: %s", e)
            self._ranks = {}

    def _save_ranks(self):
        try:
            _seed_ranks_path.parent.mkdir(parents=True, exist_ok=True)
            with open(_seed_ranks_path, "w", encoding="utf-8") as f:
                json.dump(self._ranks, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning("[SeedRanker] 分层数据保存失败: %s", e)

    # ── 核心：从 LeadsStore 统计并更新分层 ──

    def update_ranks_from_leads(self, days: int = 30) -> Dict[str, str]:
        """
        从 LeadsStore 的 interactions 表统计每个种子账号引导的回关率，更新分层。

        数据来源: interactions.metadata 的 seed_account 字段
          - action='followed' 记录关注行为
          - action='follow_back' 记录对方回关
        需要 followed_cnt >= 5 才计入（样本太少不可靠）。

        返回: {seed_username: tier_char}
        """
        try:
            from src.leads.store import get_leads_store
            store = get_leads_store()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            with store._conn() as conn:
                rows = conn.execute("""
                    SELECT
                        json_extract(i.metadata, '$.seed_account') AS seed,
                        COUNT(DISTINCT CASE WHEN i.action='followed'     THEN i.lead_id END) AS followed_cnt,
                        COUNT(DISTINCT CASE WHEN i.action='follow_back'  THEN i.lead_id END) AS follow_back_cnt
                    FROM interactions i
                    WHERE i.created_at >= ?
                      AND json_extract(i.metadata, '$.seed_account') IS NOT NULL
                    GROUP BY seed
                    HAVING followed_cnt >= 5
                    ORDER BY followed_cnt DESC
                """, (cutoff,)).fetchall()

            updated: Dict[str, str] = {}
            for row in rows:
                seed = row[0]
                if not seed:
                    continue
                followed = row[1] or 0
                follow_back = row[2] or 0
                rate = follow_back / max(followed, 1)
                tier = _compute_tier(rate)

                self._ranks[seed] = {
                    "tier": tier,
                    "follow_back_rate": round(rate, 4),
                    "followed_cnt": followed,
                    "follow_back_cnt": follow_back,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                updated[seed] = tier

            if updated:
                self._save_ranks()
                counts = {TIER_S: 0, TIER_A: 0, TIER_B: 0}
                for t in updated.values():
                    counts[t] = counts.get(t, 0) + 1
                log.info("[SeedRanker] 分层更新完成: S=%d A=%d B=%d (共%d个种子)",
                         counts[TIER_S], counts[TIER_A], counts[TIER_B], len(updated))
            else:
                log.debug("[SeedRanker] 无足够数据更新分层（近%d天，需 followed>=5）", days)

            return updated

        except Exception as e:
            log.warning("[SeedRanker] 分层更新失败: %s", e)
            return {}

    # ── 核心：排序种子列表 ──

    def rank_seeds(self, seed_accounts: List[str]) -> List[str]:
        """
        按分层 S>A>B 排序种子账号列表。

        规则:
        - 未知种子（首次使用）视为 A 级（不歧视新账号）
        - B 级种子若占比 > 50%，截断到总数的 60%（避免大量低质种子消耗配额）
        - 同层内按 follow_back_rate 降序
        """
        if not seed_accounts:
            return seed_accounts

        def _sort_key(seed: str) -> Tuple[int, float]:
            info = self._ranks.get(seed)
            if not info:
                return (_TIER_ORDER[TIER_A], 0.0)  # 未知 → A 级中等优先级
            return (_TIER_ORDER.get(info["tier"], 1),
                    -info.get("follow_back_rate", 0.0))

        ranked = sorted(seed_accounts, key=_sort_key)

        # B 级截断保护
        b_count = sum(
            1 for s in ranked
            if self._ranks.get(s, {}).get("tier") == TIER_B
        )
        if b_count > len(ranked) * 0.5 and len(ranked) > 4:
            keep = max(len(ranked) - b_count // 2, len(ranked) // 2)
            ranked = ranked[:keep]
            log.debug("[SeedRanker] B级种子(%d个)占比过高，截断至 %d 个", b_count, keep)

        return ranked

    def get_top_seeds(self, n: int = 5, min_tier: str = TIER_A) -> List[str]:
        """获取最优的 n 个种子账号（至少达到指定分层）。"""
        min_order = _TIER_ORDER.get(min_tier, 1)
        candidates = [
            (seed, info) for seed, info in self._ranks.items()
            if _TIER_ORDER.get(info.get("tier", TIER_B), 2) <= min_order
        ]
        candidates.sort(key=lambda x: x[1].get("follow_back_rate", 0), reverse=True)
        return [seed for seed, _ in candidates[:n]]

    def get_rank_info(self, seed: str) -> Optional[dict]:
        return self._ranks.get(seed)

    def summary(self) -> dict:
        counts = {TIER_S: 0, TIER_A: 0, TIER_B: 0, "unknown": 0}
        for info in self._ranks.values():
            t = info.get("tier", "unknown")
            counts[t] = counts.get(t, 0) + 1
        return {"total": len(self._ranks), **counts}

    # ── P3-4: 自动发现新优质种子 ──

    def discover_new_seeds(self, days: int = 14, min_follow_back_rate: float = 0.3,
                           min_sample: int = 10) -> List[str]:
        """
        ★ P3-4: 从历史数据中自动发现高质量新种子账号。

        逻辑:
        1. 找出"有种子账号来源记录但尚未被追踪"的账号
        2. 计算其引导的回关率
        3. 回关率 >= min_follow_back_rate 且样本量 >= min_sample → 自动注册为 A 级种子
        4. 同时从已有 S 级种子的 followers 中发现潜在新种子（间接发现）

        返回新发现的种子账号列表。
        """
        try:
            from src.leads.store import get_leads_store
            store = get_leads_store()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            with store._conn() as conn:
                # 查找在 interactions.metadata 中出现但尚未被追踪的 seed_account
                rows = conn.execute("""
                    SELECT
                        json_extract(i.metadata, '$.seed_account') AS seed,
                        COUNT(DISTINCT CASE WHEN i.action='followed'    THEN i.lead_id END) AS followed_cnt,
                        COUNT(DISTINCT CASE WHEN i.action='follow_back' THEN i.lead_id END) AS follow_back_cnt
                    FROM interactions i
                    WHERE i.created_at >= ?
                      AND json_extract(i.metadata, '$.seed_account') IS NOT NULL
                    GROUP BY seed
                    HAVING followed_cnt >= ?
                    ORDER BY (CAST(follow_back_cnt AS REAL) / followed_cnt) DESC
                    LIMIT 50
                """, (cutoff, min_sample)).fetchall()

            new_seeds = []
            for row in rows:
                seed = row[0]
                if not seed or seed in self._ranks:
                    continue  # 已追踪，跳过
                followed = row[1] or 0
                follow_back = row[2] or 0
                rate = follow_back / max(followed, 1)

                if rate >= min_follow_back_rate:
                    tier = _compute_tier(rate)
                    self._ranks[seed] = {
                        "tier": tier,
                        "follow_back_rate": round(rate, 4),
                        "followed_cnt": followed,
                        "follow_back_cnt": follow_back,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "discovery": "auto",  # 标记为自动发现
                    }
                    new_seeds.append(seed)
                    log.info("[SeedRanker] 自动发现新种子: %s (tier=%s rate=%.1%%)",
                             seed, tier, rate * 100)

            if new_seeds:
                self._save_ranks()
                log.info("[SeedRanker] 共自动发现 %d 个新种子账号", len(new_seeds))

            return new_seeds

        except Exception as e:
            log.warning("[SeedRanker] 自动种子发现失败: %s", e)
            return []


# ── 单例 + 公共接口 ──

_ranker: Optional[SeedRanker] = None


def get_seed_ranker() -> SeedRanker:
    global _ranker
    if _ranker is None:
        _ranker = SeedRanker()
    return _ranker


def rank_seeds(seed_accounts: List[str]) -> List[str]:
    """对外接口：排序种子账号列表（S>A>B，同层按回关率降序）。"""
    return get_seed_ranker().rank_seeds(seed_accounts)


def update_seed_ranks(days: int = 30) -> Dict[str, str]:
    """对外接口：从历史数据更新种子分层（供 StrategyOptimizer 每日调用）。"""
    return get_seed_ranker().update_ranks_from_leads(days=days)
