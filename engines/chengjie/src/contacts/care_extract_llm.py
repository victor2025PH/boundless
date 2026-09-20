"""P2 2026-08-01：主动关怀 LLM 抽取层（多语种召回升级的核心件）。

正则抽取器（``care_commitment.extract_commitments``）只认中文显式时间锚点 + 零星英文，
泰/越/印尼/西/日/韩等出海主力语种召回≈0。本模块提供 LLM 结构化抽取三件套：

- ``time_like(text)``            —— **廉价门**（纯函数）：多语种时间词/日期数字启发式，
                                    不过门的消息绝不烧 LLM（影子模式预算保护第一层）。
- ``build_llm_extract_prompt``   —— 单条消息 → 严格 JSON 抽取 prompt（带今日锚点与硬规则：
                                    只认「发消息者自己的未来事」，模糊/过去/他人事一律 found=false，
                                    对齐正则层「宁缺毋滥」哲学与 Phase8 记忆接地教训）。
- ``parse_llm_extract(raw)``     —— 容错 JSON 解析 + 字段校验（剥代码围栏/找平衡花括号/
                                    日期合法性/置信度夹取），坏输出返回 None 绝不抛。
- ``commitment_from_llm``        —— LLM 结果 → ``CareCommitment``。**跟进时刻的计算保持
                                    确定性**（事件日 20:00 / 道贺类 09:00，与正则层同一套
                                    规则常量）——LLM 只负责「识别」，「调度」永远是代码说了算
                                    （OTTO 承诺引擎同款分工：LLM detects, deterministic core schedules）。

设计纪律：全部纯函数、零 I/O、可单测；LLM 调用本身在 ``care_shadow_scan``（异步、带预算）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from src.contacts import care_commitment as _cc
from src.contacts.care_commitment import CareCommitment

logger = logging.getLogger(__name__)

_FOLLOWUP_HOUR = getattr(_cc, "_FOLLOWUP_HOUR", 20)
_BIRTHDAY_HOUR = getattr(_cc, "_BIRTHDAY_HOUR", 9)

# ── 廉价门：多语种时间词表（全小写比较；宁可放过进 LLM、不可整类漏掉）──────────
_TIME_WORDS = (
    # zh
    "明天", "明晚", "明早", "后天", "大后天", "下周", "下週", "这周", "這週", "本周",
    "周末", "週末", "月底", "天后", "星期", "礼拜", "禮拜", "生日", "纪念日", "紀念日",
    # en
    "tomorrow", "next week", "weekend", "tonight", "next month", "birthday",
    "anniversary", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    # ja
    "明日", "あした", "来週", "週末", "誕生日",
    # ko
    "내일", "다음 주", "다음주", "주말", "생일",
    # th
    "พรุ่งนี้", "สัปดาห์หน้า", "วันเกิด", "เสาร์", "อาทิตย์",
    # vi
    "ngày mai", "tuần sau", "tuần tới", "sinh nhật", "cuối tuần",
    # id/ms
    "besok", "minggu depan", "akhir pekan", "ulang tahun",
    # es
    "mañana", "próxima semana", "proxima semana", "fin de semana", "cumpleaños",
)

# 数字型日期锚点：3月5日 / 8/3 / 8-3 / 15号 / 3pm / 15:00（裸数字不算，防价格误触发）
_DATE_DIGIT_RE = re.compile(
    r"(\d{1,2}\s*月\s*\d{1,2}\s*[日号號]?)|(?<!\d)(\d{1,2})[/\-](\d{1,2})(?!\d)"
    r"|(\d{1,2}\s*[日号號])|(\d{1,2}\s*(?:am|pm))|(\d{1,2}:\d{2})",
    re.IGNORECASE,
)
# 「X天后 / in 3 days」
_REL_DAYS_RE = re.compile(r"(\d{1,3}\s*天[后後])|(\bin\s+\d{1,3}\s+days?\b)", re.IGNORECASE)
# 周X / 週X（正则层已认，门也要认——门必须是正则层的超集才有对照意义）
_ZH_WEEKDAY_RE = re.compile(r"[周週星礼禮][期拜]?\s*[一二三四五六日天1-7]")


def time_like(text: str) -> bool:
    """消息是否含「未来时间」信号（多语种启发式，LLM 抽取的准入门）。"""
    t = (text or "").strip()
    if not t or len(t) < 2:
        return False
    low = t.lower()
    if any(w in low for w in _TIME_WORDS):
        return True
    if _DATE_DIGIT_RE.search(t) or _REL_DAYS_RE.search(t) or _ZH_WEEKDAY_RE.search(t):
        return True
    return False


_WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_EXTRACT_PROMPT = """你是信息抽取器。判断下面这条聊天消息里，**发消息的人自己**是否提到了未来要做/要经历的具体事（如：面试、体检、复查、考试、旅行、搬家、生日、见客户、提车、开庭等）。

今天是 {today}（{weekday}）。消息原文：
「{text}」

只输出一行 JSON，不要任何解释：
- 提到了 → {{"found":true,"topic":"事情的简短名词(2-8字,用消息本身的语言)","date":"事件日期YYYY-MM-DD","greeting":false,"confidence":0.9}}
  （生日/纪念日/婚礼这类道贺场景 greeting 填 true）
- 没提到 / 不确定 / 是过去的事 / 是别人的事 / 只是闲聊或客套 → {{"found":false}}

硬规则：
1. date 必须严格晚于今天；「明天/后天/周五/下周三」都按今天换算成具体日期。
2. 只有「以后/有空/改天/最近」这类无法定日的模糊说法 → found=false。
3. 别人的事（"我朋友明天面试"）→ found=false。
4. confidence 按语气定：确定的计划 0.85-0.95，可能/大概 0.6-0.75。"""


def build_llm_extract_prompt(text: str, *, now: Optional[float] = None) -> str:
    """单条消息的抽取 prompt（带今日日期/星期锚点，防 LLM 徒手推算日历出错）。"""
    n = float(now if now is not None else time.time())
    base = datetime.fromtimestamp(n)
    return _EXTRACT_PROMPT.format(
        today=base.strftime("%Y-%m-%d"),
        weekday=_WEEKDAY_ZH[base.weekday()],
        text=(text or "").strip()[:500],
    )


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_llm_extract(raw: Any) -> Optional[Dict[str, Any]]:
    """容错解析 LLM 输出。返回规范化 dict（found=false 也是合法结果）；坏输出 → None。

    容错面：markdown 代码围栏 / 前后缀废话（取首个平衡 ``{...}``）/ 字段类型漂移。
    校验面：topic 非空且 ≤40 字、date 严格 ``YYYY-MM-DD`` 且真实存在、confidence 夹 [0,1]。
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # 剥代码围栏
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    # 取首个平衡花括号块
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return None
    try:
        obj = json.loads(s[start:end + 1])
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    found = bool(obj.get("found", False))
    if not found:
        return {"found": False}
    topic = str(obj.get("topic") or "").strip()
    date = str(obj.get("date") or "").strip()
    if not topic or len(topic) > 40 or not _DATE_RE.match(date):
        return None
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except Exception:
        return None
    try:
        conf = float(obj.get("confidence", 0.7))
    except Exception:
        conf = 0.7
    return {
        "found": True,
        "topic": topic,
        "date": date,
        "greeting": bool(obj.get("greeting", False)),
        "confidence": max(0.0, min(1.0, conf)),
    }


def commitment_from_llm(
    parsed: Optional[Dict[str, Any]],
    *,
    source_text: str = "",
    now: Optional[float] = None,
    max_snippet: int = 160,
) -> Optional[CareCommitment]:
    """LLM 抽取结果 → ``CareCommitment``；未命中/过去事/坏输入 → None（绝不抛）。

    跟进时刻确定性计算：事件日 {followup} 点回访（道贺类当日 {birthday} 点），
    与正则层同一规则；``due_at <= now`` 一律丢弃（只认未来，与正则层同不变量）。
    """
    if not parsed or not parsed.get("found"):
        return None
    n = float(now if now is not None else time.time())
    try:
        event_day = datetime.strptime(str(parsed["date"]), "%Y-%m-%d")
    except Exception:
        return None
    hour = _BIRTHDAY_HOUR if parsed.get("greeting") else _FOLLOWUP_HOUR
    due_dt = event_day.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    due_at = due_dt.timestamp()
    if due_at <= n:
        # 事件日=今天且已过跟进钟点 → 顺延一小时内仍不合理，直接丢弃保守处理
        return None
    sentiment = "neutral"
    try:
        sentiment = _cc._sentiment_of(source_text)  # noqa: SLF001（同包内复用，失败软降级）
    except Exception:
        sentiment = "neutral"
    return CareCommitment(
        due_at=due_at,
        event_at=event_day.timestamp(),
        topic=str(parsed["topic"])[:80],
        sentiment=sentiment,
        anchor_text="llm",
        source_text=(source_text or "").strip()[:max_snippet],
        confidence=float(parsed.get("confidence", 0.7)),
    )


__all__ = [
    "time_like", "build_llm_extract_prompt", "parse_llm_extract",
    "commitment_from_llm",
]
