"""通话上下文组装 —— 把散落的运行时信号拼成 ``CallContext``（注入型 lookup，可离线测）。

真机接线时，来电只带 ``(account_id, chat_id)``；决策所需的其余信号（会话语言/自动化档/亲密度、
通话用量、账号健康灯、kill-switch、记忆 bullets）散在 inbox / call_usage_store / account_health /
kill_switch / 记忆库。本模块把这些**查询**收敛成注入型 lookup 回调，组装出 ``CallContext``：
wiring 侧传真实 store 方法，测试传 fake。每个 lookup 缺失/异常 → 保守默认（绝不因某个信号源
抖动而拒接合法来电或误判绿灯）。

刻意**不**在这里直接 import 那些 store（保持可离线测 + import-safe）；``hour`` 由调用方按账号
时区算好传入（本模块不碰时钟，保持纯粹）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

from src.voicecall.bridge import CallContext

logger = logging.getLogger(__name__)


def call_account_key(platform: str, account_id: str) -> str:
    """账号键（与 call_usage_store / send_gate 同口径）：``platform:account_id``。"""
    return f"{str(platform or 'telegram')}:{str(account_id or '')}"


def call_memory_key(platform: str, chat_key: str) -> str:
    """记忆键（与草稿/浏览器通话同口径）：``platform:chat_key``。"""
    return f"{str(platform or 'telegram')}:{str(chat_key or '')}"


def _safe_call(fn: Optional[Callable], *args: Any, default: Any = None) -> Any:
    if fn is None:
        return default
    try:
        return fn(*args)
    except Exception:
        logger.debug("[voicecall] context lookup 失败（用默认）", exc_info=True)
        return default


def assemble_call_context(
    chat_id: int,
    account_id: str,
    *,
    platform: str = "telegram",
    conversation_lookup: Optional[Callable[[str, str], Optional[dict]]] = None,
    usage_lookup: Optional[Callable[[str], Tuple[int, float]]] = None,
    account_light_lookup: Optional[Callable[[str], str]] = None,
    kill_switch_lookup: Optional[Callable[[str, str], bool]] = None,
    memory_lookup: Optional[Callable[[str], str]] = None,
    host_warm: bool = True,
    hour: int = 12,
    concurrent_active: int = 0,
) -> CallContext:
    """组装 ``CallContext``（防御式，缺信号→保守默认）。

    - ``conversation_lookup(account_id, chat_key) -> dict|None``：会话画像，认这些键——
      ``language`` / ``automation_mode`` / ``intimacy`` / ``has_conversation`` / ``peer_known``；
      返回 None（查无此会话）→ has_conversation=False + peer_known=False（陌生人→静默拒接）。
    - ``usage_lookup(account_key) -> (calls_today, minutes_today)``：近 24h 通话用量（预算闸）；
    - ``account_light_lookup(account_key) -> "green|amber|red"``：账号健康灯（红灯停接）；
    - ``kill_switch_lookup(platform, account_id) -> bool``：是否被 kill-switch 冻结；
    - ``memory_lookup(memory_key) -> bullets``：长期记忆要点（注入通话大脑系统提示）。
    """
    chat_key = str(chat_id)
    acct_key = call_account_key(platform, account_id)
    mem_key = call_memory_key(platform, chat_key)

    conv = _safe_call(conversation_lookup, account_id, chat_key, default=None)
    if isinstance(conv, dict):
        has_conv = bool(conv.get("has_conversation", True))
        peer_known = bool(conv.get("peer_known", True))
        language = str(conv.get("language") or "zh")
        automation_mode = str(conv.get("automation_mode") or "auto_ai")
        intimacy = float(conv.get("intimacy") or 0.0)
    else:
        # 查无会话 = 陌生人：has_conversation/peer_known 全 False → decide 走静默拒接
        has_conv, peer_known = False, False
        language, automation_mode, intimacy = "zh", "auto_ai", 0.0

    usage = _safe_call(usage_lookup, acct_key, default=(0, 0.0)) or (0, 0.0)
    calls_today, minutes_today = int(usage[0] or 0), float(usage[1] or 0.0)
    account_light = str(_safe_call(account_light_lookup, acct_key, default="green") or "green")
    frozen = bool(_safe_call(kill_switch_lookup, platform, account_id, default=False))
    bullets = str(_safe_call(memory_lookup, mem_key, default="") or "")

    return CallContext(
        chat_id=int(chat_id),
        account_id=str(account_id),
        conversation_language=language,
        intimacy=intimacy,
        automation_mode=automation_mode,
        has_conversation=has_conv,
        peer_known=peer_known,
        kill_switch_frozen=frozen,
        hour=int(hour),
        host_warm=bool(host_warm),
        concurrent_active=int(concurrent_active),
        memory_bullets=bullets,
        calls_today=calls_today,
        minutes_today=minutes_today,
        account_light=account_light,
    )


__all__ = ["assemble_call_context", "call_account_key", "call_memory_key"]
