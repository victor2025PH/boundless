# -*- coding: utf-8 -*-
"""
fb_playbook.py — Facebook 日本女性获客 Playbook 编排（W3）

统一入口，按节奏执行：
  1. 健康检查（account_health 熔断）
  2. 获客任务（facebook_acquire_from_keyword）
  3. 打招呼任务（facebook_jp_female_greet）
  4. 记录执行日志

节奏设计（防风控）:
  - 每台设备每天：获客 ≤ 30 搜索、≤ 5 加好友、≤ 3 打招呼
  - 任务间随机等待 30-120 秒
  - account_health.score < 60 → 只打招呼不获客
  - account_health.phase = frozen → 全停
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 每日配额（防风控保守值）
_DEFAULT_DAILY_LIMITS = {
    "max_searches": 30,
    "max_adds": 5,
    "max_greets": 3,
}


def run_fb_jp_playbook(
    device_id: str,
    persona_key: str = "jp_female_midlife",
    dry_run: bool = False,
    daily_limits: Optional[Dict[str, int]] = None,
    skip_acquire: bool = False,
    skip_greet: bool = False,
) -> Dict[str, Any]:
    """
    执行 Facebook 日本女性获客 Playbook（一次完整运行）。

    参数:
        device_id     - ADB 设备序列号
        persona_key   - 目标画像键
        dry_run       - True 时只分类不加好友不发 DM
        daily_limits  - 覆盖默认每日配额
        skip_acquire  - 跳过获客阶段
        skip_greet    - 跳过打招呼阶段

    返回:
        {acquire: {...}, greet: {...}, health: {...}}
    """
    limits = {**_DEFAULT_DAILY_LIMITS, **(daily_limits or {})}
    result: Dict[str, Any] = {
        "device_id": device_id,
        "dry_run": dry_run,
        "started_at": datetime.now().isoformat(),
        "acquire": None,
        "greet": None,
        "health": None,
        "abort_reason": None,
    }

    # ── 1. 健康检查 ─────────────────────────────────────────────────
    health = _check_and_update_health(device_id)
    result["health"] = health
    phase = health.get("phase", "active")
    score = int(health.get("score", 100) or 100)

    if phase == "frozen":
        result["abort_reason"] = f"account_frozen (until={health.get('frozen_until')})"
        logger.warning("[playbook] 账号冻结，全停: %s", device_id)
        return result

    if score < 30:
        result["abort_reason"] = f"health_score_critical ({score})"
        logger.warning("[playbook] 健康分极低 score=%d，全停: %s", score, device_id)
        return result

    # ── 2. 获客任务 ─────────────────────────────────────────────────
    if not skip_acquire:
        # 健康分低时减少获客强度
        actual_searches = limits["max_searches"]
        actual_adds = limits["max_adds"]
        if score < 60:
            actual_searches = max(5, actual_searches // 3)
            actual_adds = 0  # 健康分低时只搜不加
            logger.info("[playbook] 健康分低 score=%d，减量获客 searches=%d", score, actual_searches)

        logger.info("[playbook] 开始获客: device=%s searches=%d adds=%d",
                    device_id, actual_searches, actual_adds)

        from src.app_automation.fb_acquire_task import facebook_acquire_from_keyword
        try:
            acquire_result = facebook_acquire_from_keyword(
                device_id=device_id,
                persona_key=persona_key,
                max_searches=actual_searches,
                max_adds=actual_adds,
                dry_run=dry_run,
            )
            result["acquire"] = acquire_result
            logger.info("[playbook] 获客完成: searches=%d nav_ok=%d l2_match=%d add_ok=%d",
                        acquire_result.get("searches", 0),
                        acquire_result.get("nav_ok", 0),
                        acquire_result.get("l2_match", 0),
                        acquire_result.get("add_friend_ok", 0))
        except Exception as e:
            logger.error("[playbook] 获客任务异常: %s", e)
            result["acquire"] = {"error": str(e)}

        # 获客与打招呼之间随机等待
        if not skip_greet:
            wait_s = random.uniform(30, 120)
            logger.info("[playbook] 等待 %.0f 秒后进行打招呼...", wait_s)
            time.sleep(wait_s)
    else:
        logger.info("[playbook] 跳过获客阶段")

    # ── 3. 打招呼任务 ───────────────────────────────────────────────
    if not skip_greet:
        logger.info("[playbook] 开始打招呼: device=%s max=%d",
                    device_id, limits["max_greets"])

        from src.app_automation.fb_greet_task import facebook_jp_female_greet
        try:
            greet_result = facebook_jp_female_greet(
                device_id=device_id,
                persona_key=persona_key,
                max_greets=limits["max_greets"],
                dry_run=dry_run,
            )
            result["greet"] = greet_result
            logger.info("[playbook] 打招呼完成: queue=%d greeted=%d no_dm=%d",
                        greet_result.get("queue_size", 0),
                        greet_result.get("greeted", 0),
                        greet_result.get("no_dm", 0))
        except Exception as e:
            logger.error("[playbook] 打招呼任务异常: %s", e)
            result["greet"] = {"error": str(e)}
    else:
        logger.info("[playbook] 跳过打招呼阶段")

    result["ended_at"] = datetime.now().isoformat()
    logger.info("[playbook] 完成: %s", {k: v for k, v in result.items() if k not in ("acquire", "greet")})
    return result


def _check_and_update_health(device_id: str) -> Dict[str, Any]:
    """检查账号健康状态，不存在则初始化"""
    try:
        from src.host.fb_targets_store import get_account_health, update_account_health
        health = get_account_health(device_id)
        if not health:
            # 首次运行，初始化为 active
            update_account_health(
                device_id=device_id,
                score=100,
                phase="active",
            )
            health = {"score": 100, "phase": "active"}
        return health
    except Exception as e:
        logger.warning("[playbook] health check 失败（继续）: %s", e)
        return {"score": 100, "phase": "active"}
