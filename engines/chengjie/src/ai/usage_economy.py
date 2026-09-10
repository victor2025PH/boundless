# -*- coding: utf-8 -*-
"""用量经济档 overlay（B9，2026-09-11）——不改 A 线「记忆深度只抬不压」四档。

A 的 ``ai.context_depth`` = standard/deep/max/ultra，语义是**加深**（只抬地板）。
「额度用尽还要能聊」需要的是反向压帽：历史 4 条、compact 人设、少抽、关掉每轮
都变的深度人设块（也利于前缀缓存）。两套旋钮叠在同一枚举上会把 standard=零变化
的契约撕开，所以本模块独立：

  · ``ai.usage_mode: economy`` 运营手开；
  · 或钱包 ``should_degrade_action("ai_reply")`` 且 ``ai.economy_on_degrade`` 未关
    （默认开——用尽降级免费引擎的同时把 prompt 也收一圈，不是只换模型）。

默认 off：所有 cap_* 是恒等，A 线门禁与存量配置零行为变化。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

DEFAULT_HISTORY_CAP = 4
DEFAULT_MEMORY_ITEMS = 4
DEFAULT_MEMORY_CHARS = 600
DEFAULT_PER_CONV_EXTRACT = 3
DEFAULT_PROMPT_BUDGET = 6000
DEFAULT_VERBATIM_ROUNDS = 2   # 与 history 4 条对齐（一轮 ≈ 2 条）


@dataclass(frozen=True)
class Economy:
    on: bool
    history_cap: int = DEFAULT_HISTORY_CAP
    memory_items: int = DEFAULT_MEMORY_ITEMS
    memory_chars: int = DEFAULT_MEMORY_CHARS
    per_conv_extract: int = DEFAULT_PER_CONV_EXTRACT
    prompt_budget: int = DEFAULT_PROMPT_BUDGET
    verbatim_rounds: int = DEFAULT_VERBATIM_ROUNDS
    skip_deep_persona: bool = True
    persona_detail: str = "compact"


OFF = Economy(on=False)


def _root(config: Any) -> dict:
    if config is None:
        try:
            from src.compliance.runtime import runtime_config
            return runtime_config() or {}
        except Exception:
            return {}
    if isinstance(config, dict):
        return config
    inner = getattr(config, "config", None)
    return inner if isinstance(inner, dict) else {}


def _ai(config: Any) -> dict:
    ai = _root(config).get("ai") or {}
    return ai if isinstance(ai, dict) else {}


def _explicit_on(config: Any) -> bool:
    mode = str(_ai(config).get("usage_mode") or "").strip().lower()
    return mode in ("economy", "lite", "eco", "经济")


def _degrade_on(config: Any) -> bool:
    ai = _ai(config)
    if "economy_on_degrade" in ai and not bool(ai.get("economy_on_degrade")):
        return False
    try:
        from src.licensing.token_ledger import should_degrade_action
        return bool(should_degrade_action("ai_reply"))
    except Exception:
        return False


def resolve(config: Any = None) -> Economy:
    """当前是否压帽。绝不抛。"""
    try:
        if _explicit_on(config) or _degrade_on(config):
            return Economy(on=True)
        return OFF
    except Exception:
        return OFF


def cap_int(n: int, ceiling: int) -> int:
    try:
        v = int(n or 0)
    except (TypeError, ValueError):
        return ceiling
    if v <= 0:
        return v
    return min(v, int(ceiling))


def cap_history(n: int, config: Any = None) -> int:
    e = resolve(config)
    return cap_int(n, e.history_cap) if e.on else int(n or 0)


def cap_prompt_budget(n: int, config: Any = None) -> int:
    e = resolve(config)
    if not e.on:
        return int(n or 0)
    if not n:
        return n
    return cap_int(n, e.prompt_budget)


def cap_memory(items: int, chars: int, config: Any = None) -> Tuple[int, int]:
    e = resolve(config)
    if not e.on:
        return int(items or 0), int(chars or 0)
    return cap_int(items, e.memory_items), cap_int(chars, e.memory_chars)


def cap_verbatim_rounds(n: int, config: Any = None) -> int:
    e = resolve(config)
    return cap_int(n, e.verbatim_rounds) if e.on else int(n or 0)


def cap_verbatim_msgs(n: int, config: Any = None) -> int:
    e = resolve(config)
    return cap_int(n, e.verbatim_rounds * 2) if e.on else int(n or 0)


def cap_extract_daily(n: int, config: Any = None) -> int:
    e = resolve(config)
    if not e.on:
        return max(1, int(n or 1))
    return max(1, min(int(n or 1), e.per_conv_extract))


def persona_detail(config: Any, legacy: str = "full") -> str:
    e = resolve(config)
    if e.on:
        return e.persona_detail
    s = str(legacy or "full").strip().lower()
    return s or "full"


def skip_deep_persona(config: Any = None) -> bool:
    e = resolve(config)
    return bool(e.on and e.skip_deep_persona)


__all__ = [
    "DEFAULT_HISTORY_CAP", "DEFAULT_VERBATIM_ROUNDS", "Economy", "OFF",
    "cap_extract_daily", "cap_history", "cap_memory", "cap_prompt_budget",
    "cap_verbatim_msgs", "cap_verbatim_rounds", "persona_detail", "resolve",
    "skip_deep_persona",
]
