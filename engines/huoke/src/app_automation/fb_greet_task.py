# -*- coding: utf-8 -*-
"""
fb_greet_task.py — Facebook 日本女性精准打招呼任务（W2）

主流程：
  1. 查询 fb_targets_global WHERE status='friended' AND friended_at + 36h < now
  2. 读取 insights_json（禁止再开 profile）
  3. 调用 fb_jp_greeting 生成个性化日文问候语
  4. 二次过滤（禁词表）
  5. send_message 发送 DM
  6. 成功→ status=greeted；UI 不可达→ status=friended_no_dm

合规约束：
  - 只引 bio/兴趣（层A），禁引近期帖子
  - 禁词：投資/副業/LINE/稼ぐ/儲かる/副収入
  - friended_at + uniform(36, 72) 小时后才发
  - 同一 target 只发一次（greeted 状态后不再发）
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 禁词表（双重保险）
_FORBIDDEN_WORDS = [
    "投資", "副業", "LINE", "稼ぐ", "儲かる", "儲け", "副収入",
    "http://", "https://", "www.", ".com", ".jp/",
]

# DM 间最小等待（秒）
_DM_MIN_INTERVAL_S = 60
_DM_MAX_INTERVAL_S = 120


def _has_forbidden_word(text: str) -> bool:
    for w in _FORBIDDEN_WORDS:
        if w in text:
            return True
    return False


def generate_greeting(
    display_name: str,
    insights: Dict[str, Any],
    greeting_from_library: Optional[str] = None,
    use_llm: bool = True,
) -> str:
    """
    生成个性化日文打招呼语（层A：只引 bio/兴趣）。

    优先使用话术库随机取 + 个性化插入名字；
    若话术库为空则用 LLM 生成。
    """
    # 提取兴趣
    topics = insights.get("topics") or insights.get("interests") or []
    if isinstance(topics, str):
        topics = [t.strip() for t in topics.split(",") if t.strip()]
    age_band = insights.get("age_band", "")
    gender = insights.get("gender", "female")

    # 优先使用话术库
    if greeting_from_library:
        text = greeting_from_library
        # 个性化：插入对方名字（如果是英文名）
        first_name = display_name.split()[0] if display_name else ""
        if first_name and first_name.isascii() and len(first_name) < 15:
            # 在开头加名字称呼
            text = f"{first_name}さん、{text}"
        if not _has_forbidden_word(text):
            return text

    # LLM 生成备用
    if use_llm:
        try:
            return _llm_generate_greeting(display_name, topics, age_band)
        except Exception as e:
            logger.warning("[greet] LLM 生成失败: %s", e)

    # 兜底模板
    topic_str = topics[0] if topics else "日常"
    return f"はじめまして！{topic_str}がお好きなんですね。私もとても興味があります。よろしくお願いします。"


def _llm_generate_greeting(
    display_name: str,
    topics: List[str],
    age_band: str,
) -> str:
    """用 LLM 生成针对该 profile 的个性化问候语"""
    from src.ai.llm_client import get_llm_client
    client = get_llm_client()

    topic_hint = "、".join(topics[:3]) if topics else "日常生活"
    name_part = f"{display_name.split()[0]}さん" if display_name else "こんにちは"

    prompt = f"""あなたは日本語で自然な挨拶メッセージを作成するアシスタントです。
以下の条件で、Facebookで初めて知り合った女性への挨拶メッセージを日本語で1つ作成してください。

条件:
- 相手の興味: {topic_hint}
- 文体: 丁寧かつ親しみやすい敬体
- 文字数: 30〜60文字
- 禁止: URL、連絡先、商品勧誘、副業の話
- 一人称は「私」か「わたし」で
- 最初に相手の名前を呼びかける

挨拶メッセージのみ出力（他の文章不要）:"""

    msgs = [{"role": "user", "content": prompt}]
    resp = client.chat_messages(msgs, temperature=0.8, max_tokens=100)
    text = (resp or "").strip()
    if not text or _has_forbidden_word(text):
        raise ValueError(f"LLM 生成内容不合规: {text!r}")
    return text


class GreetTask:
    """
    Facebook 日文打招呼任务。

    用法:
        task = GreetTask(device_id="8DWOF6CYY5R8YHX8", max_greets=5)
        result = task.run()
    """

    def __init__(
        self,
        device_id: str,
        persona_key: str = "jp_female_midlife",
        max_greets: int = 5,
        dry_run: bool = False,
        min_friended_hours: float = 36.0,
        max_friended_hours: float = 72.0,
    ):
        self.device_id = device_id
        self.persona_key = persona_key
        self.max_greets = max_greets
        self.dry_run = dry_run
        self.min_friended_hours = min_friended_hours
        self.max_friended_hours = max_friended_hours
        self._fb: Any = None

    def _get_fb(self):
        if self._fb is None:
            from src.app_automation.facebook import FacebookAutomation
            self._fb = FacebookAutomation()
        return self._fb

    def _get_greet_queue(self) -> List[Dict[str, Any]]:
        """查询待打招呼队列"""
        try:
            from src.host.fb_targets_store import list_greet_queue
            return list_greet_queue(
                persona_key=self.persona_key,
                min_delay_hours=self.min_friended_hours,
                max_delay_hours=self.max_friended_hours,
                limit=self.max_greets * 2,
            )
        except Exception as e:
            logger.warning("[greet] list_greet_queue 失败: %s", e)
            return []

    def _pick_greeting_from_library(self, persona_key: str) -> Optional[str]:
        """从话术库随机取一条"""
        try:
            from src.host.fb_targets_store import pick_greeting
            g = pick_greeting(persona_key)
            if g:
                return g.get("text_ja", "")
        except Exception as e:
            logger.debug("[greet] pick_greeting 失败: %s", e)
        return None

    def run(self) -> Dict[str, Any]:
        """执行打招呼任务"""
        stats = {
            "device_id": self.device_id,
            "queue_size": 0,
            "greeted": 0,
            "no_dm": 0,
            "skipped": 0,
            "errors": 0,
            "started_at": datetime.now().isoformat(),
        }

        queue = self._get_greet_queue()
        stats["queue_size"] = len(queue)

        if not queue:
            logger.info("[greet] 打招呼队列为空")
            return stats

        logger.info("[greet] 打招呼队列: %d 人", len(queue))

        greet_count = 0
        for target in queue:
            if greet_count >= self.max_greets:
                break

            target_id = target.get("id")
            display_name = target.get("display_name", "")
            friended_at_str = target.get("friended_at", "")

            # 验证 friended_at + 随机延迟窗口
            try:
                friended_at = datetime.fromisoformat(friended_at_str)
                delay_hours = random.uniform(self.min_friended_hours, self.max_friended_hours)
                send_after = friended_at + timedelta(hours=delay_hours)
                if datetime.now() < send_after:
                    logger.info("[greet] 太早，跳过: %r  send_after=%s",
                                display_name, send_after)
                    stats["skipped"] += 1
                    continue
            except Exception:
                pass  # friended_at 格式异常则继续

            # 解析 insights
            insights_raw = target.get("insights_json", "{}")
            try:
                insights = json.loads(insights_raw) if isinstance(insights_raw, str) else (insights_raw or {})
            except Exception:
                insights = {}

            logger.info("[greet] 处理: %r  id=%s", display_name, target_id)

            # 从话术库取一条 + 个性化生成
            library_text = self._pick_greeting_from_library(self.persona_key)
            greeting_text = generate_greeting(
                display_name=display_name,
                insights=insights,
                greeting_from_library=library_text,
                use_llm=True,
            )

            logger.info("[greet] 生成话术: %r", greeting_text[:60])

            # 二次禁词检查
            if _has_forbidden_word(greeting_text):
                logger.warning("[greet] 话术含禁词，跳过: %r", greeting_text[:60])
                stats["skipped"] += 1
                continue

            if self.dry_run:
                logger.info("[greet] dry_run: 跳过发送 %r → %r", display_name, greeting_text[:40])
                greet_count += 1
                stats["greeted"] += 1
                # 记录到 DM 审计表（dry_run 标记）
                self._log_outbound(target_id, display_name, greeting_text, sent_ok=False, dry_run=True)
                time.sleep(2)
                continue

            # 实际发送
            fb = self._get_fb()
            try:
                ok = fb.send_message(
                    recipient=display_name,
                    message=greeting_text,
                    device_id=self.device_id,
                )
            except Exception as e:
                logger.warning("[greet] send_message 失败: %s  name=%r", e, display_name)
                ok = False

            # 更新状态
            from src.host.fb_targets_store import mark_status, record_greeting_sent
            if ok:
                greet_count += 1
                stats["greeted"] += 1
                try:
                    mark_status(
                        target_id=target_id,
                        status="greeted",
                        device_id=self.device_id,
                        extra_fields={"greeted_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                    )
                    self._log_outbound(target_id, display_name, greeting_text, sent_ok=True)
                except Exception as e:
                    logger.warning("[greet] 写入状态失败: %s", e)
            else:
                stats["no_dm"] += 1
                try:
                    mark_status(
                        target_id=target_id,
                        status="friended_no_dm",
                        device_id=self.device_id,
                    )
                    self._log_outbound(target_id, display_name, greeting_text, sent_ok=False)
                except Exception as e:
                    logger.warning("[greet] 写入 no_dm 状态失败: %s", e)

            # DM 间隔
            if greet_count < self.max_greets:
                time.sleep(random.uniform(_DM_MIN_INTERVAL_S, _DM_MAX_INTERVAL_S))

        stats["ended_at"] = datetime.now().isoformat()
        logger.info("[greet] 完成: %s", stats)
        return stats

    def _log_outbound(
        self,
        target_id: Optional[int],
        display_name: str,
        text: str,
        sent_ok: bool,
        dry_run: bool = False,
    ):
        """写入 fb_outbound_messages 审计表"""
        try:
            from src.host.database import get_conn
            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO fb_outbound_messages
                       (target_id, target_identity, device_id, generated_text,
                        reference_layer, sent_ok, risk_flags_json, sent_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        target_id,
                        display_name,
                        self.device_id,
                        text,
                        "A",
                        1 if (sent_ok and not dry_run) else 0,
                        json.dumps({"dry_run": dry_run}, ensure_ascii=False),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug("[greet] log_outbound 失败: %s", e)


# ── 模块级快捷函数 ──────────────────────────────────────────────────

def facebook_jp_female_greet(
    device_id: str,
    persona_key: str = "jp_female_midlife",
    max_greets: int = 5,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    任务入口：对已加好友的日本女性发送个性化日文问候。

    参数:
        device_id   - ADB 设备序列号
        persona_key - 目标画像键
        max_greets  - 本次最多发送次数
        dry_run     - True 时只生成不发送

    返回:
        {queue_size, greeted, no_dm, skipped, errors}
    """
    task = GreetTask(
        device_id=device_id,
        persona_key=persona_key,
        max_greets=max_greets,
        dry_run=dry_run,
    )
    return task.run()
