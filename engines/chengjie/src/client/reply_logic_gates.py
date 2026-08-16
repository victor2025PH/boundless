"""回复逻辑闸门（纯函数，零第三方依赖）。

对应 Web 设置页「Telegram → 回复逻辑」的配置项
（config: ``telegram.reply_logic.{cooldown_seconds, max_consecutive_replies,
ignore_edited}``，PUT /api/telegram/settings/reply-logic 保存即落盘）：

- ``cooldown_remaining``          — 回复冷却（同一用户两次自动回复最小间隔）
- ``consecutive_limit_reached``   — 最大连续回复次数（连发 N 条后暂停）
- ``should_ignore_edited``        — 忽略被编辑的消息（缺省开）

消费点在 ``telegram_client._process_message_async``（每条消息重读 config →
保存即生效，无需重启）与 ``sender._send_reply`` 成功路径（记账）。
本模块不 import pyrogram / 项目内其它模块，便于独立单测。
"""

from typing import Any, Optional, Tuple

# 连续计数自动复位窗口（秒）：同一用户静默超过该时长后，连续自动回复计数归零。
DEFAULT_STREAK_RESET_AFTER = 1800.0


def _to_num(value: Any, default: float = 0.0) -> float:
    """宽容数值化：config 里的值可能是 int/float/数字字符串，坏值回退 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def cooldown_remaining(
    reply_logic_cfg: dict, last_reply_ts: Optional[float], now: float
) -> float:
    """回复冷却剩余秒数（0 表示可回复）。

    语义与 UI 提示一致：冷却 = 同一用户两次**自动回复**之间的最小间隔秒数；
    ``cooldown_seconds`` ≤0 或缺省 = 不限制（返回 0）。

    Args:
        reply_logic_cfg: ``telegram.reply_logic`` 配置节（dict，可为空/None）。
        last_reply_ts: 上次对该用户自动回复的时间戳；None = 从未回复过 → 不受冷却。
        now: 当前时间戳（调用方传入，便于单测）。

    Returns:
        剩余冷却秒数；0.0 表示不在冷却期、可以回复。
    """
    cooldown = _to_num((reply_logic_cfg or {}).get("cooldown_seconds"), 0)
    if cooldown <= 0 or last_reply_ts is None:
        return 0.0
    remaining = cooldown - (now - float(last_reply_ts))
    return remaining if remaining > 0 else 0.0


def effective_streak(
    count: int,
    last_reply_ts: Optional[float],
    now: float,
    reset_after: float = DEFAULT_STREAK_RESET_AFTER,
) -> int:
    """连续自动回复的「生效计数」（过期自动复位的单一语义源）。

    距上次自动回复超过 ``reset_after`` 秒（或从未回复过）→ 计数视为 0；
    否则原样返回（负数/坏值按 0 处理）。闸门判定与发送后记账共用本函数，
    保证两处的复位语义永远一致。
    """
    if last_reply_ts is None or (now - float(last_reply_ts)) > float(reset_after):
        return 0
    return max(0, int(_to_num(count, 0)))


def consecutive_limit_reached(
    reply_logic_cfg: dict,
    count: int,
    last_reply_ts: Optional[float],
    now: float,
    reset_after: float = DEFAULT_STREAK_RESET_AFTER,
) -> Tuple[bool, int]:
    """同一用户连续自动回复是否已达上限。

    语义与 UI 提示一致：对同一用户连续自动回复 N 条后暂停回复；
    静默超过 ``reset_after``（默认 30 分钟）后计数自动复位（视为 0）。
    ``max_consecutive_replies`` ≤0 或缺省 = 不限制。

    Args:
        reply_logic_cfg: ``telegram.reply_logic`` 配置节。
        count: 当前记录的连续自动回复计数。
        last_reply_ts: 上次自动回复时间戳（None = 从未回复过）。
        now: 当前时间戳。
        reset_after: 静默多少秒后计数复位（默认 30 分钟）。

    Returns:
        ``(是否已达上限, 生效计数)``——生效计数是过期复位后的值，
        调用方可用它把「过期归零」落地回自己的计数字典。
    """
    eff = effective_streak(count, last_reply_ts, now, reset_after)
    limit = _to_num((reply_logic_cfg or {}).get("max_consecutive_replies"), 0)
    if limit <= 0:
        return False, eff
    return eff >= limit, eff


def should_ignore_edited(reply_logic_cfg: dict, edit_date: Any) -> bool:
    """被编辑的消息是否应忽略（不触发自动回复）。

    ``ignore_edited`` 缺省 True（编辑消息通常是改错字，重新回复既扰人又易重复）。
    ``edit_date`` 为真（消息确实被编辑过）且开关开 → True（忽略）；
    非编辑消息（edit_date 为空）恒 False。
    """
    if not edit_date:
        return False
    return bool((reply_logic_cfg or {}).get("ignore_edited", True))
