# -*- coding: utf-8 -*-
"""
消息分流 — 「回复 / 只读查询 / 执行任务」路由。

**推荐（生产）**：`config/chat.yaml` 中 `triage.strategy: llm_first`，
使用与 OpenClaw 其它模块相同的 `get_llm_client()`（即 `chat.yaml` 的 `ai` 配置）
**一次 LLM 调用**输出 JSON，决定 route；不再依赖大量手写关键词。

**兜底**：无 API Key、或 LLM 解析失败时，可走 `rules_fallback` 使用轻量规则（仅保底，非主路径）。

环境变量：
- OPENCLAW_CHAT_TRIAGE_DISABLE=1 — 关闭分流，全部走意图执行（旧行为）
- OPENCLAW_CHAT_TRIAGE_STRATEGY=llm_first|rules_first — 覆盖 yaml 中的 strategy
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

import yaml

from src.host.device_registry import config_file

log = logging.getLogger(__name__)

_CONFIG_PATH = config_file("chat.yaml")


class ChatRoute(str, Enum):
    EXECUTE = "execute"
    QUERY = "query"
    CONVERSE = "converse"
    AMBIGUOUS = "ambiguous"


@dataclass
class TriageResult:
    route: ChatRoute
    query_subtype: str = ""
    confidence: float = 1.0
    reason: str = ""
    llm_used: bool = False


def _load_triage_cfg() -> Dict[str, Any]:
    out: Dict[str, Any] = {"strategy": "llm_first", "rules_fallback": True}
    try:
        if _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            tri = raw.get("triage") or {}
            s = (tri.get("strategy") or "").strip().lower()
            if s in ("llm_first", "rules_first"):
                out["strategy"] = s
            if "rules_fallback" in tri:
                out["rules_fallback"] = bool(tri["rules_fallback"])
    except Exception as e:
        log.debug("[triage] config read: %s", e)
    env = os.environ.get("OPENCLAW_CHAT_TRIAGE_STRATEGY", "").strip().lower()
    if env in ("llm_first", "rules_first"):
        out["strategy"] = env
    return out


def triage_disabled() -> bool:
    env = os.environ.get("OPENCLAW_CHAT_TRIAGE_DISABLE", "").strip()
    if env == "1":
        return True
    if env == "0":
        return False
    try:
        if _CONFIG_PATH.exists():
            raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if raw.get("triage", {}).get("enabled") is False:
                return True
    except Exception:
        pass
    return False


# ── LLM 分类（与 OpenClaw 统一 LLM 客户端）──────────────────────────────


_LLM_SYSTEM = """你是 OpenClaw 控制台的消息类型分类器。用户通过「AI 指令」框输入中文。
请只输出一个 JSON 对象，不要 markdown，不要其它文字。

字段说明：
- route 必须是以下之一：
  - "execute"：用户要**下任务、改自动化、控设备**（养号、关注、停止、VPN、收件箱、帮助指令列表等）
  - "query"：用户只想**查状态/看数据**，不创建任务（在线设备数、漏斗、日报、任务失败原因、健康检查等）
  - "converse"：寒暄、确认在不在、问怎么用、闲聊、感谢等**不需要调接口执行自动化**的对话
  - "ambiguous"：无法判断时选这个

若 route 为 "query"，必须给出 query_subtype，取值之一：
device_list | stats | daily_report | health | leads | schedule_list | task_errors | general

判断要点：
- 「在吗」「你好」「谢谢」「怎么用」→ converse
- 「有几台手机在线」「主控多少设备」「今日日报」「为什么任务失败」→ query
- 「01号养号30分钟」「全部停止」「关注菲律宾」→ execute
- 「帮助」「你能做什么」→ execute（会展示能力列表）"""


def _llm_classify_route(msg: str) -> Optional[TriageResult]:
    try:
        from src.ai.llm_client import get_llm_client
    except Exception:
        return None
    client = get_llm_client()
    if not getattr(client.config, "api_key", ""):
        return None
    user = f"用户输入：\n{msg.strip()}"
    try:
        raw = client.chat_with_system(_LLM_SYSTEM, user, temperature=0.0, max_tokens=200, use_cache=False)
        raw = (raw or "").strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```\w*\n?", "", raw)
            raw = re.sub(r"\n?```\s*$", "", raw)
        data = json.loads(raw)
        r = data.get("route", "execute")
        if r not in ("execute", "query", "converse", "ambiguous"):
            r = "execute"
        sub = str(data.get("query_subtype", "general") or "general")
        return TriageResult(
            route=ChatRoute(r),
            query_subtype=sub,
            confidence=float(data.get("confidence", 0.85) or 0.85),
            reason="llm_classify",
            llm_used=True,
        )
    except Exception as e:
        log.debug("[triage] LLM classify failed: %s", e)
        return None


# ── 规则兜底（无 Key / LLM 失败时使用，保持可测、不依赖网络）──────────────

_EXECUTE_HINTS = (
    "养号", "刷视频", "关注", "直播间", "直播", "评论", "剧本", "获客", "全流程",
    "停止所有", "全部停止", "紧急停止", "取消任务", "重连", "vpn", "收件箱", "私信", "引流",
    "定时任务", "切换", "战役", "确认", "执行", "开始任务", "批量",
    "壁纸", "预览", "dry_run",
)
_EXECUTE_VERBS = ("停止", "取消", "暂停")

_QUERY_STATUS_WORDS = (
    "设备", "手机", "电脑", "主控", "在线", "连接", "几台", "几个", "多少",
    "状态", "统计", "漏斗", "失败", "错误", "健康", "掉线", "线索", "crm",
    "任务", "昨日", "昨天", "今日", "今天",
)
_QUERY_TRIGGERS = (
    "几", "多少", "几个", "哪台", "哪些", "有没有", "是否", "查询", "查看一下",
    "查一下", "看下", "看看",
    "为什么", "怎么回事", "什么原因", "怎么样",
)

_GREETING_RE = re.compile(
    r"^(你好|您好|嗨|哈喽|早上好|晚上好|下午好|谢谢|多谢|辛苦了|再见|拜拜|Hi|Hello|hey)[!！。.…\s]*$",
    re.IGNORECASE,
)
_CONVERSATION_PING_RE = re.compile(
    r"^(在吗|在么|在嘛|在不在|还在吗|还在不|有人吗|有人不|在没|在不)[!！？?。.…\s]*$",
    re.IGNORECASE,
)


def _has_strong_execute(msg: str) -> bool:
    ml = msg.lower()
    if any(h in ml for h in _EXECUTE_HINTS):
        return True
    if any(v in ml for v in _EXECUTE_VERBS):
        return True
    if re.match(r"^(所有|全部|每台|\d{1,2}\s*号)", msg.strip()):
        return True
    if msg.strip() in ("帮助", "help", "功能", "你能做什么"):
        return True
    return False


def _looks_like_query(msg: str) -> bool:
    ml = msg.strip()
    if not ml:
        return False
    if "数据" in ml and any(x in ml for x in ("今天", "今日", "昨日", "昨天", "最近", "怎么样", "如何")):
        return True
    has_status = any(w in ml for w in _QUERY_STATUS_WORDS)
    has_trigger = any(t in ml for t in _QUERY_TRIGGERS)
    if has_status and (has_trigger or "？" in ml or "?" in ml):
        return True
    if re.search(r"(主控|控制台).{0,6}(几|多少|几个)", ml):
        return True
    if re.search(r"(在线|连接).{0,4}(几|多少|几个)", ml):
        return True
    return False


def _infer_query_subtype(msg: str) -> str:
    ml = msg.lower()
    if any(w in ml for w in ("失败", "错误率", "报错", "为什么失败", "任务失败")):
        return "task_errors"
    if any(w in ml for w in ("日报", "今日汇总", "今天数据", "今日数据")) and "执行" not in ml:
        return "daily_report"
    if any(w in ml for w in ("漏斗", "转化", "funnel")):
        return "stats"
    if any(w in ml for w in ("健康", "掉线", "heartbeat", "health")):
        return "health"
    if any(w in ml for w in ("线索", "crm", "leads")):
        return "leads"
    if any(w in ml for w in ("定时", "cron", "计划任务")):
        return "schedule_list"
    if any(w in ml for w in ("设备", "手机", "电脑", "主控", "在线", "连接", "几台")):
        return "device_list"
    return "general"


def _rules_triage(msg: str) -> TriageResult:
    """无 LLM 时的轻量规则（单测与离线兜底）。"""
    if _GREETING_RE.match(msg) or _CONVERSATION_PING_RE.match(msg):
        return TriageResult(route=ChatRoute.CONVERSE, reason="rules_greeting_or_ping")

    if _looks_like_query(msg):
        sub = _infer_query_subtype(msg)
        return TriageResult(route=ChatRoute.QUERY, query_subtype=sub, reason="rules_query_pattern")

    if _has_strong_execute(msg):
        return TriageResult(route=ChatRoute.EXECUTE, reason="rules_execute_keywords")

    if re.match(r"^(什么|怎么|如何)", msg) and not _has_strong_execute(msg):
        return TriageResult(route=ChatRoute.CONVERSE, reason="rules_howto")

    return TriageResult(route=ChatRoute.EXECUTE, reason="rules_default_execute")


def triage_message(user_message: str) -> TriageResult:
    if triage_disabled():
        return TriageResult(route=ChatRoute.EXECUTE, reason="disabled")

    msg = (user_message or "").strip()
    if not msg:
        return TriageResult(route=ChatRoute.EXECUTE, reason="empty")

    cfg = _load_triage_cfg()
    strategy = cfg.get("strategy", "llm_first")
    rules_fallback = cfg.get("rules_fallback", True)

    if strategy == "llm_first":
        llm = _llm_classify_route(msg)
        if llm is not None:
            return llm
        if rules_fallback:
            r = _rules_triage(msg)
            r.reason = r.reason + "_after_llm_failed"
            return r
        return TriageResult(route=ChatRoute.EXECUTE, reason="llm_failed_no_fallback")

    # rules_first
    return _rules_triage(msg)
