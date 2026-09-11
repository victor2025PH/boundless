# -*- coding: utf-8 -*-
"""上下文/记忆深度四档（``ai.context_depth``）——单一事实源（2026-09-11）。

老板口径：「上下文与记忆做 标准 / 深度 / 最大 / 超大，也可以调到与 DeepSeek 平齐的 1M」。

一档改全部相关旋钮（此前它们散在四处、各有各的缺省，单改任何一个都「失忆」）：

  · ``prompt_budget_tokens``      发送前预算裁剪上限（ai_client._apply_prompt_budget）
  · ``history_msgs``              喂给模型的历史条数上限（ai_client max_hist；策略 context_rounds
                                  只能往上抬不往下压，0 仍表示「本策略不带历史」）
  · ``verbatim_rounds/msgs``      本地先裁：最近多少轮逐字保留、更早的压成摘要
                                  （skill_manager 两条路径：TG 直答按轮、收件箱拟稿按条）
  · ``memory_items/chars``        情景记忆注入条数 / 字数（memory.inject_max_*）
  · ``history_fetch``             从 inbox 库取多少行历史（各拟稿入口的 list_recent_messages limit）

语义：
  · 键缺席 / ``standard`` = **零行为变化**：全部返回调用方传入的 legacy 值（老配置照旧生效）。
  · deep / max / ultra = 各旋钮取 ``max(legacy, 档位值)``——只抬地板，绝不把用户手调的更大值压回去。
  · ``ultra`` 预算 900k：对齐 DeepSeek V4.1-Flash / 硅基 V4-Flash 的 1M 窗口，留 10% 给输出与估算误差；
    LAN chatx（64k）有自己的 num_ctx 硬上限裁剪，不受此影响。

成本提示（DeepSeek 官方 峰时 ¥2/M 未命中 · ¥0.04/M 缓存命中 · ¥8/M 输出）：
  standard ≈ 12k → 每轮 ≤ ¥0.024；deep 32k → ≤ ¥0.064；max 128k → ≤ ¥0.26；ultra 900k → ≤ ¥1.8。
  人设/规则前缀稳定即吃缓存价，实际远低于上限；UI 必须把这条讲给用户。

消费方全部**活读** config（本模块每次从 runtime_config() / 传入 config 取），改完即生效，无需重启。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

TIER_KEYS: Tuple[str, ...] = ("standard", "deep", "max", "ultra")
DEFAULT_TIER = "standard"

# 同义写法（UI/老板口述/中文键）→ 规范键
_ALIASES = {
    "标准": "standard", "std": "standard", "normal": "standard", "default": "standard",
    "深度": "deep", "deeper": "deep",
    "最大": "max", "maximum": "max", "large": "max",
    "超大": "ultra", "xl": "ultra", "1m": "ultra", "huge": "ultra",
}


@dataclass(frozen=True)
class Depth:
    key: str
    label_zh: str
    prompt_budget_tokens: Optional[int]   # None = 跟随 legacy
    history_msgs: Optional[int]
    verbatim_rounds: Optional[int]
    memory_items: Optional[int]
    memory_chars: Optional[int]
    history_fetch: Optional[int]
    cost_hint_cny_per_turn_max: float     # 峰时未命中上限（仅提示）

    @property
    def is_standard(self) -> bool:
        return self.key == DEFAULT_TIER

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key, "label_zh": self.label_zh,
            "prompt_budget_tokens": self.prompt_budget_tokens,
            "history_msgs": self.history_msgs, "verbatim_rounds": self.verbatim_rounds,
            "memory_items": self.memory_items, "memory_chars": self.memory_chars,
            "history_fetch": self.history_fetch,
            "cost_hint_cny_per_turn_max": self.cost_hint_cny_per_turn_max,
        }


TIERS: Dict[str, Depth] = {
    "standard": Depth("standard", "标准", None, None, None, None, None, None, 0.024),
    "deep":     Depth("deep", "深度", 32_000, 40, 20, 16, 3_000, 60, 0.064),
    "max":      Depth("max", "最大", 128_000, 160, 80, 30, 8_000, 200, 0.26),
    "ultra":    Depth("ultra", "超大", 900_000, 1_000, 500, 40, 16_000, 1_100, 1.8),
}


def normalize_tier(value: Any) -> str:
    """任意写法 → 规范档位键；认不出 → standard（绝不抛）。"""
    s = str(value or "").strip().lower()
    if not s:
        return DEFAULT_TIER
    s = _ALIASES.get(s, s)
    return s if s in TIERS else DEFAULT_TIER


def _root(config: Any) -> Dict[str, Any]:
    if config is None:
        try:
            from src.compliance.runtime import runtime_config
            return runtime_config() or {}
        except Exception:
            return {}
    if isinstance(config, dict):
        return config
    inner = getattr(config, "config", None)   # ConfigManager
    return inner if isinstance(inner, dict) else {}


def resolve(config: Any = None) -> Depth:
    """当前生效档位（读 ``ai.context_depth``；缺席/非法 → standard）。"""
    try:
        ai = _root(config).get("ai") or {}
        return TIERS[normalize_tier(ai.get("context_depth"))]
    except Exception:
        return TIERS[DEFAULT_TIER]


def _lift(legacy: Any, tier_val: Optional[int]) -> int:
    try:
        base = int(legacy or 0)
    except (TypeError, ValueError):
        base = 0
    if tier_val is None:
        return base
    return max(base, int(tier_val))


def prompt_budget(config: Any, legacy: int) -> int:
    """预算：standard → legacy 原值（含 0=不裁）；深档 → max(legacy, 档位)。legacy=0（不裁）保持 0。"""
    d = resolve(config)
    if d.is_standard or not legacy:
        n = int(legacy or 0)
    else:
        n = _lift(legacy, d.prompt_budget_tokens)
    try:
        from src.ai.usage_economy import cap_prompt_budget
        return cap_prompt_budget(n, config)
    except Exception:
        return n


def history_limit(config: Any, legacy: int, *, strategy_rounds: Optional[int] = None) -> int:
    """喂模型的历史条数。strategy_rounds=0 尊重（该策略刻意不带历史）；否则深档只抬不压。"""
    base = legacy if strategy_rounds is None else strategy_rounds
    if strategy_rounds == 0:
        return 0
    d = resolve(config)
    n = _lift(base, None if d.is_standard else d.history_msgs)
    try:
        from src.ai.usage_economy import cap_history
        return cap_history(n, config)
    except Exception:
        return n


def verbatim_rounds(config: Any, legacy_rounds: int) -> int:
    """本地逐字保留的轮数（TG 直答路径按轮计）。"""
    d = resolve(config)
    n = _lift(legacy_rounds, None if d.is_standard else d.verbatim_rounds)
    try:
        from src.ai.usage_economy import cap_verbatim_rounds
        return cap_verbatim_rounds(n, config)
    except Exception:
        return n


def verbatim_msgs(config: Any, legacy_msgs: int) -> int:
    """本地逐字保留的条数（收件箱拟稿路径按条计）= 轮数 × 2。"""
    d = resolve(config)
    n = _lift(legacy_msgs, None if d.is_standard or d.verbatim_rounds is None
              else d.verbatim_rounds * 2)
    try:
        from src.ai.usage_economy import cap_verbatim_msgs
        return cap_verbatim_msgs(n, config)
    except Exception:
        return n


def compress_threshold(keep: int, legacy_threshold: int) -> int:
    """开始摘要压缩的阈值：至少比逐字保留量多出一截，避免刚够保留就反复重算摘要。"""
    try:
        k = int(keep or 0)
        t = int(legacy_threshold or 0)
    except (TypeError, ValueError):
        return legacy_threshold
    return max(t, k + max(3, k // 3))


def memory_limits(config: Any, legacy_items: int, legacy_chars: int) -> Tuple[int, int]:
    d = resolve(config)
    if d.is_standard:
        items, chars = int(legacy_items), int(legacy_chars)
    else:
        items, chars = _lift(legacy_items, d.memory_items), _lift(legacy_chars, d.memory_chars)
    try:
        from src.ai.usage_economy import cap_memory
        return cap_memory(items, chars, config)
    except Exception:
        return items, chars


def history_fetch_limit(config: Any, legacy: int = 30) -> int:
    """从库里取多少行历史（各拟稿入口 list_recent_messages 的 limit）。"""
    d = resolve(config)
    n = _lift(legacy, None if d.is_standard else d.history_fetch)
    try:
        from src.ai.usage_economy import cap_history
        return cap_history(n, config)
    except Exception:
        return n


def describe(config: Any = None) -> Dict[str, Any]:
    """给 UI / prompt-inspect 用的档位说明（含四档全表、成本提示、经济档 overlay）。

    ``economy`` **不是**第五档：TIER_KEYS 仍只有 standard/deep/max/ultra。
    overlay 单独挂 ``economy`` 字段，开了才压 history/budget，档位键不变。
    """
    cur = resolve(config)
    try:
        from src.ai.usage_economy import describe as _econ_describe
        econ = _econ_describe(config)
    except Exception:
        econ = {"on": False, "reason": ""}
    return {
        "current": cur.key,
        "label_zh": cur.label_zh,
        "tiers": [TIERS[k].as_dict() for k in TIER_KEYS],
        "effective": {
            "prompt_budget_tokens": cur.prompt_budget_tokens,
            "history_msgs": cur.history_msgs,
            "memory_items": cur.memory_items,
        },
        "economy": econ,
    }
