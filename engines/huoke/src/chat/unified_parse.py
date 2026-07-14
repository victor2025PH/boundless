# -*- coding: utf-8 -*-
"""
单次 LLM 合并解析：routing（分流）+ execute 时的 intent/params/targeting/goals。

与 ChatAI._system_prompt 拼接为完整 system 消息；查询类结果仍由后端 API 拉取，模型只填 query_subtype。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

ROUTING_PREFIX = """## 统一解析（单条 JSON 输出）

你必须输出**一个** JSON 对象（不要 markdown），且包含下列顶层字段：

| 字段 | 说明 |
|------|------|
| schema_version | 固定为 1 |
| routing | 必选：execute / query / converse / ambiguous 四选一 |
| confidence | 0～1 小数，表示你对分类的把握 |
| query_subtype | 当 routing=query 时必选：device_list / stats / daily_report / health / leads / schedule_list / task_errors / general |
| multi_task | 布尔：当 routing=execute 且用户一句话包含多个独立任务时为 true |
| intents | 当 multi_task=true 时：子任务数组，每项含 action（与下表 intent 名对应的执行名，如 tiktok_warmup、tiktok_follow）与 params |
| intent | routing=execute 且 multi_task=false 时的单意图名（warmup、follow、help 等，与下表一致） |
| devices | 设备序列号数组或 ["all"] |
| params | 意图参数对象 |
| targeting | 可选人群定向 |
| goals | 可选目标 |

**routing 含义**
- **execute**：用户要执行自动化、控制设备、查看帮助列表、使用明确口令。
- **query**：只读查询（在线几台、漏斗、日报、任务失败原因等），不要编造数字。
- **converse**：寒暄、在吗、谢谢、怎么用产品等，不执行任务。
- **ambiguous**：无法判断时选此项。

**多任务**：若用户用逗号/顿号连接多个操作（如「01养号，再关注」），设 multi_task=true，并在 intents 里列出；action 使用 tiktok_warmup、tiktok_follow、tiktok_live_engage、tiktok_comment_engage、tiktok_check_inbox、tiktok_chat 等与系统任务类型一致的名字。

"""

JSON_SUFFIX = """
## 统一 JSON 示例

仅查询：
{{"schema_version":1,"routing":"query","confidence":0.9,"query_subtype":"device_list","multi_task":false,"intent":"help","devices":[],"params":{{}},"targeting":{{}},"goals":{{}},"intents":[]}}

单任务执行：
{{"schema_version":1,"routing":"execute","confidence":0.9,"query_subtype":"general","multi_task":false,"intent":"warmup","devices":["all"],"params":{{"duration_minutes":30,"target_country":"italy"}},"targeting":{{}},"goals":{{}},"intents":[]}}

闲聊：
{{"schema_version":1,"routing":"converse","confidence":0.95,"query_subtype":"general","multi_task":false,"intent":"help","devices":[],"params":{{}},"targeting":{{}},"goals":{{}},"intents":[]}}
"""


def build_unified_system_prompt(base_intent_prompt: str) -> str:
    """base_intent_prompt 为已 format 设备映射后的 ChatAI._system_prompt。"""
    return ROUTING_PREFIX + "\n" + base_intent_prompt + "\n" + JSON_SUFFIX


_VALID_ROUTING = frozenset({"execute", "query", "converse", "ambiguous"})
_VALID_QUERY_SUB = frozenset({
    "device_list", "stats", "daily_report", "health", "leads",
    "schedule_list", "task_errors", "general",
})


def normalize_unified_payload(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验并补全默认值；不合法返回 None。"""
    if not isinstance(raw, dict):
        return None
    ver = raw.get("schema_version", 1)
    try:
        if int(ver) != 1:
            return None
    except (TypeError, ValueError):
        return None

    r = raw.get("routing", "")
    if r not in _VALID_ROUTING:
        return None

    out: Dict[str, Any] = {
        "schema_version": 1,
        "routing": r,
        "confidence": float(raw.get("confidence", 0.8) or 0.8),
        "query_subtype": str(raw.get("query_subtype", "general") or "general"),
        "multi_task": bool(raw.get("multi_task", False)),
        "intent": str(raw.get("intent", "help") or "help"),
        "devices": raw.get("devices") if isinstance(raw.get("devices"), list) else [],
        "params": raw.get("params") if isinstance(raw.get("params"), dict) else {},
        "targeting": raw.get("targeting") if isinstance(raw.get("targeting"), dict) else {},
        "goals": raw.get("goals") if isinstance(raw.get("goals"), dict) else {},
        "intents": [],
    }

    if out["query_subtype"] not in _VALID_QUERY_SUB:
        out["query_subtype"] = "general"

    intents_raw = raw.get("intents")
    if isinstance(intents_raw, list):
        cleaned: List[Dict[str, Any]] = []
        for it in intents_raw:
            if not isinstance(it, dict):
                continue
            act = it.get("action", "")
            pr = it.get("params") if isinstance(it.get("params"), dict) else {}
            if act:
                cleaned.append({"action": str(act), "params": pr})
        out["intents"] = cleaned

    if out["multi_task"] and not out["intents"]:
        out["multi_task"] = False

    return out
