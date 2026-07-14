# -*- coding: utf-8 -*-
"""
高分 Lead 优先触达引擎 — P3-2。

当前问题:
  所有 follow-up 任务按固定 cron 调度，高分 lead（已明确表现兴趣）
  和低分 lead 享受同等待遇。限量的设备时间被均摊稀释。

解决方案:
  每4小时扫描一次 LeadsStore，对满足条件的高分 lead 主动发起优先 DM：
  - score >= min_score（默认50分）
  - status 在 followed / responded
  - 最近 48h 内无出站 DM
  - 目标国家当前处于活跃时段（避免3am发消息）

任务类型: tiktok_priority_outreach（在 executor.py 注册）
调度: tiktok_schedules.yaml 每4小时
"""

from __future__ import annotations

import json
import logging
import urllib.request as _ur
from datetime import datetime, timezone, timedelta

from src.openclaw_env import local_api_base
from typing import Dict, List, Optional

from src.host.device_registry import data_file

log = logging.getLogger(__name__)


def _load_ab_template() -> Optional[str]:
    """读取 A/B 实验胜出的 DM 模板风格（供消息生成参考）。"""
    try:
        winner_path = data_file("ab_winner.json")
        if winner_path.exists():
            with open(winner_path, encoding="utf-8") as f:
                return json.load(f).get("dm_template_style")
    except Exception:
        pass
    return None


def _generate_dm_message(lead: dict, template_style: Optional[str] = None) -> str:
    """
    生成个性化 DM 消息。

    优先使用 A/B 实验确定的最优风格；
    回退到通用模板。消息保持简短（<100字）避免被过滤。
    """
    name = (lead.get("first_name") or lead.get("name") or "").split()[0]
    greeting = f"Hey {name}!" if name else "Hey!"

    style = template_style or "warm_greeting"

    templates = {
        "warm_greeting": (
            f"{greeting} Thanks for following back 😊 "
            "I've been sharing some cool content lately — "
            "would love to connect more. Drop me a message anytime!"
        ),
        "question_opener": (
            f"{greeting} Quick question — what kind of content do you enjoy most? "
            "I'm curious 😊 Always looking to connect with like-minded people!"
        ),
        "compliment_first": (
            f"{greeting} Love your vibe! "
            "I noticed you followed back and wanted to say hi. "
            "Let's stay connected! 🙌"
        ),
        "direct_referral": (
            f"{greeting} Great to connect! "
            "I share exclusive content on Telegram too — "
            "feel free to check it out when you have a moment 👋"
        ),
    }

    return templates.get(style, templates["warm_greeting"])


def run_priority_outreach(
    device_id: str,
    max_leads: int = 10,
    min_score: float = 50.0,
    no_dm_hours: int = 48,
) -> Dict[str, int]:
    """
    主入口: 找出高分 lead 并提交优先 DM 任务。

    Args:
        device_id: 执行设备 ID
        max_leads: 最多触达的 lead 数量（避免单次过多）
        min_score: 最低分数阈值
        no_dm_hours: 该时间窗口内没有发过 DM 才触达

    Returns:
        {"submitted": N, "skipped_timezone": N, "skipped_recent_dm": N}
    """
    stats = {"submitted": 0, "skipped_timezone": 0, "skipped_recent_dm": 0}

    try:
        from src.leads.store import get_leads_store
        from src.host.timezone_guard import is_country_active

        store = get_leads_store()
        utc_now = datetime.now(timezone.utc)
        cutoff_dm = (utc_now - timedelta(hours=no_dm_hours)).isoformat()

        # 查询高分 lead（直接用 SQL 避免全量加载）
        with store._conn() as conn:
            rows = conn.execute("""
                SELECT l.id, l.first_name, l.last_name, l.score, l.status,
                       l.location,
                       pp.username AS tiktok_username
                FROM leads l
                LEFT JOIN platform_profiles pp
                       ON pp.lead_id = l.id AND pp.platform = 'tiktok'
                WHERE l.score >= ?
                  AND l.status IN ('followed', 'responded')
                  AND pp.username IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM interactions i
                      WHERE i.lead_id = l.id
                        AND i.direction = 'outbound'
                        AND i.action IN ('send_dm', 'auto_reply', 'follow_up')
                        AND i.created_at >= ?
                  )
                ORDER BY l.score DESC
                LIMIT ?
            """, (min_score, cutoff_dm, max_leads * 2)).fetchall()  # 2x buffer for timezone filter

        template_style = _load_ab_template()
        submitted_count = 0

        for row in rows:
            if submitted_count >= max_leads:
                break

            lead_id = row[0]
            username = row[6]  # tiktok_username
            if not username:
                continue

            # 时区守卫：检查用户所在国家是否活跃
            country = (row[5] or "italy").lower()  # location field
            if not is_country_active(country, utc_now):
                log.debug("[PriorityOutreach] 跳过 %s（%s 非活跃时段）", username, country)
                stats["skipped_timezone"] += 1
                continue

            lead = {
                "id": lead_id,
                "first_name": row[1] or "",
                "name": f"{row[1] or ''} {row[2] or ''}".strip(),
                "score": row[3],
                "status": row[4],
            }
            message = _generate_dm_message(lead, template_style)

            # 提交 tiktok_send_dm 任务（优先级70，高于普通任务50）
            try:
                payload = json.dumps({
                    "type": "tiktok_send_dm",
                    "device_id": device_id,
                    "params": {
                        "username": username,
                        "message": message,
                        "lead_id": lead_id,
                        "source": "priority_outreach",
                        "ab_variant": template_style,
                    },
                    "priority": 70,
                }).encode()

                req = _ur.Request(
                    f"{local_api_base()}/tasks",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = _ur.urlopen(req, timeout=5)
                task_id = json.loads(resp.read().decode()).get("task_id", "?")
                log.info("[PriorityOutreach] 提交DM任务: lead#%d @%s score=%.0f task=%s",
                         lead_id, username, lead["score"], task_id[:8])
                submitted_count += 1
                stats["submitted"] += 1

            except Exception as e:
                log.debug("[PriorityOutreach] 提交失败 %s: %s", username, e)

    except Exception as e:
        log.error("[PriorityOutreach] 优先触达执行失败: %s", e)

    log.info("[PriorityOutreach] 完成: 提交=%d 时区跳过=%d",
             stats["submitted"], stats["skipped_timezone"])
    return stats
