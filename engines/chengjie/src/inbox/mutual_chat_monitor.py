# -*- coding: utf-8 -*-
"""AI 对聊「标记 + 提醒」监测（P0-6，2026-08-09）。

背景（198↔104 实录）：两台坐席机的受管账号互为对话对象、两侧都开全自动 →
AI 对 AI 无限往返。**运营方针（2026-08-09 老板拍板）：账号对聊是允许的测试
手段——只标记、只提醒，绝不拦截、绝不限速。** 风险不在行为本身，在「没人
知道它在跑」：双边烧 LLM 配额、互聊文本进记忆库，都应该发生在有人知情的
前提下。

判定＝**音量启发式 + 同机注册表确证** 两层：

- 音量层（跨机也能抓）：全自动会话在窗口内**双向都高频**（min_each_side 防
  把「AI 单方面主动触达轰炸」误判成对聊）且总量 ≥ min_total。198↔104 这类
  跨机对，本机注册表看不见对端，行为量是唯一可靠信号；真人超活跃会话若达
  同样音量，提醒同样有价值（全自动 + 高频 = 该有人看一眼的配额消耗）。
- 确证层（同机对）：对端 chat_key 就在本机账号注册表 → ``managed_peer=True``
  写进提醒（与 reply_diagnosis 的 managed_peer 诊断码同一判定源
  ``account_registry.peek_account``，两处口径永远一致）。

消费面：HealthWatchdog._check_mutual_chat（小时级扫描 + 每会话 24h 重提去
抖）→ EventBus ``ai_mutual_chat_alert``（订阅别名 ``ai_mutual_chat``，business
受众——收到的人能行动：是测试就忽略，不是就把任一侧切手动）。

刻意不做：任何形式的发送拦截/降速/断路（运营明确要求）；语义级「像不像
AI 在说话」判定（误伤面大且不可解释——音量与注册表都是可解释的硬信号）。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "window_hours": 24,
    "min_total": 80,          # 窗口内双向总量下限
    "min_each_side": 25,      # 每个方向各自的下限（单向广播不算对聊）
    "interval_min": 60,       # 扫描节流（分钟）
    "remind_interval_hours": 24,  # 同一会话重提间隔
    "max_list": 6,            # 单次提醒最多点名的会话数
}


def mutual_chat_cfg(cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``health_watchdog.mutual_chat_remind``（缺键回默认；类型宽容）。

    与 watchdog 提醒家族（session_stale_remind / avatar_voice_remind …）同
    约定：**默认开**——它是纯观测提醒，不改变任何发送行为。
    """
    raw: Any = {}
    try:
        raw = ((cfg or {}).get("health_watchdog") or {}).get(
            "mutual_chat_remind") or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    out = dict(_DEFAULTS)
    out["enabled"] = bool(raw.get("enabled", _DEFAULTS["enabled"]))
    for k in ("window_hours", "min_total", "min_each_side",
              "interval_min", "remind_interval_hours", "max_list"):
        try:
            v = float(raw.get(k, _DEFAULTS[k]))
        except (TypeError, ValueError):
            v = float(_DEFAULTS[k])
        out[k] = max(1, int(v)) if k != "window_hours" else max(1.0, v)
    return out


def _managed_peer(platform: str, chat_key: str) -> bool:
    """对端是否本机受管账号——与 reply_diagnosis.managed_peer 同一判定源。"""
    try:
        from src.integrations.account_registry import peek_account
        return bool(chat_key) and peek_account(platform, chat_key) is not None
    except Exception:
        return False


def scan_mutual_chat(
    store: Any, cfg: Dict[str, Any], *, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """扫描当前满足「全自动 + 双向高频」的会话，返回提醒条目列表。

    只读、fail-open：任何一段取数失败 → 该会话静默跳过，绝不抛。
    条目：``{conversation_id, platform, account_id, chat_key,
    n_in, n_out, managed_peer}``，按总量降序、截断 max_list。
    """
    ts = float(now if now is not None else time.time())
    since = ts - float(cfg["window_hours"]) * 3600.0
    try:
        volume = store.message_volume_map(
            since, min_total=int(cfg["min_total"]),
            limit=int(cfg["max_list"]) * 4)
    except Exception:
        return []
    items: List[Dict[str, Any]] = []
    for cid, v in (volume or {}).items():
        try:
            n_in, n_out = int(v.get("in") or 0), int(v.get("out") or 0)
            if min(n_in, n_out) < int(cfg["min_each_side"]):
                continue
            parts = str(cid).split(":", 2)
            if len(parts) != 3:
                continue
            platform, account_id, chat_key = parts
            # 群聊天然高音量且不是「对聊」语义——chat_key 为负数（TG 群）或
            # 会话档位非显式 auto_ai 的一律跳过。
            if chat_key.startswith("-"):
                continue
            if store.get_automation_mode_if_set(cid) != "auto_ai":
                continue
            items.append({
                "conversation_id": cid,
                "platform": platform,
                "account_id": account_id,
                "chat_key": chat_key,
                "n_in": n_in,
                "n_out": n_out,
                "managed_peer": _managed_peer(platform, chat_key),
            })
        except Exception:
            continue
    items.sort(key=lambda x: -(x["n_in"] + x["n_out"]))
    return items[: int(cfg["max_list"])]


def filter_due_reminders(
    items: List[Dict[str, Any]], ledger: Dict[str, float], *,
    now: float, remind_interval_hours: float,
) -> List[Dict[str, Any]]:
    """去抖：同一会话在重提间隔内只提醒一次；命中即写账（调用方持有 ledger）。

    纯函数式副作用（只写传入的 ledger dict）——测试可注入、进程内存活即可：
    重启后重提一次是可接受语义（提醒本来就该在「还在互聊」时重复出现）。
    """
    due: List[Dict[str, Any]] = []
    gap = float(remind_interval_hours) * 3600.0
    for it in items or []:
        cid = str(it.get("conversation_id") or "")
        if not cid:
            continue
        last = float(ledger.get(cid) or 0.0)
        if last and (now - last) < gap:
            continue
        ledger[cid] = now
        due.append(it)
    return due
