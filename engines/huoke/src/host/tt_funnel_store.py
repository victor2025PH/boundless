# -*- coding: utf-8 -*-
"""TikTok 漏斗事件 store(Sprint 4 P1)。

复制 FB 的 fb_store 埋点模式,让 TK 也有真实的 6 阶段漏斗数据。
使用方式:automation 层在关键动作处调 `record_tt_event(...)`:
  - exposure:      视频曝光(browse_feed 每条 video_seen)
  - interest:      点赞/收藏(like_video / save_video)
  - engagement:    关注/回关(follow / follow_back)
  - direct_msg:    私信发出(send_dm / comment_with_bio)
  - guidance:      收件箱回复(check_inbox 发现新回复)
  - conversion:    引流转化(whatsapp 点击 / bio_link 点击)

设计考量:
  1. 所有写入幂等;同 (device_id, stage, target_key, day) 去重靠
     调用方自保(表结构允许重复,便于统计"总动作量";但 dedupe 查询
     有 distinct target_key 版本可用)
  2. 调用失败不影响主流程(store 所有 API 吃异常,仅 log debug)
  3. 与 FB 的 stage 命名对齐,/dashboard/cross-platform-funnel 直接聚合
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from .database import _connect

logger = logging.getLogger(__name__)

# 与 cross-platform-funnel 的 6 个 UNIFIED_FUNNEL_STEPS 对齐
VALID_STAGES = (
    "exposure", "interest", "engagement",
    "direct_msg", "guidance", "conversion",
)


def record_tt_event(device_id: str, stage: str, *,
                    target_key: str = "",
                    preset_key: str = "",
                    meta: Optional[Dict[str, Any]] = None) -> int:
    """记录一个 TikTok 漏斗事件。返回 row id;失败返回 0。"""
    if not device_id or stage not in VALID_STAGES:
        return 0
    try:
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else ""
    except Exception:
        meta_json = ""
    try:
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO tiktok_funnel_events "
                "(device_id, stage, target_key, preset_key, meta_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (device_id, stage, target_key or "", preset_key or "", meta_json),
            )
            return cur.lastrowid or 0
    except Exception as e:
        logger.debug("[tt_funnel] record failed device=%s stage=%s: %s",
                     device_id, stage, e)
        return 0


def get_tt_funnel_metrics(device_id: Optional[str] = None,
                          since_iso: Optional[str] = None,
                          preset_key: Optional[str] = None) -> Dict[str, Any]:
    """聚合 6 阶段事件计数,结构与 FB 的 get_funnel_metrics 对称。"""
    where = []
    args: list = []
    if device_id:
        where.append("device_id = ?")
        args.append(device_id)
    if since_iso:
        where.append("at >= ?")
        args.append(since_iso)
    if preset_key:
        where.append("preset_key = ?")
        args.append(preset_key)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    counts = {s: 0 for s in VALID_STAGES}
    unique_counts = {s: 0 for s in VALID_STAGES}
    try:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT stage, COUNT(*), COUNT(DISTINCT target_key) "
                f"FROM tiktok_funnel_events{where_sql} GROUP BY stage",
                args,
            ).fetchall()
            for stage, cnt, uniq in rows:
                if stage in counts:
                    counts[stage] = int(cnt or 0)
                    unique_counts[stage] = int(uniq or 0)
    except Exception as e:
        logger.debug("[tt_funnel] aggregate failed: %s", e)
        return {f"stage_{s}": 0 for s in VALID_STAGES} | {
            f"unique_{s}": 0 for s in VALID_STAGES
        }

    out: Dict[str, Any] = {}
    for s in VALID_STAGES:
        out[f"stage_{s}"] = counts[s]
        out[f"unique_{s}"] = unique_counts[s]
    # 派生转化率,除零保护
    def _rate(a: int, b: int) -> float:
        return round(a / b, 4) if b > 0 else 0.0
    out["rate_exposure_to_interest"] = _rate(counts["interest"], counts["exposure"])
    out["rate_interest_to_engage"] = _rate(counts["engagement"], counts["interest"])
    out["rate_engage_to_dm"] = _rate(counts["direct_msg"], counts["engagement"])
    out["rate_dm_to_guidance"] = _rate(counts["guidance"], counts["direct_msg"])
    out["rate_guidance_to_conv"] = _rate(counts["conversion"], counts["guidance"])
    return out
