# -*- coding: utf-8 -*-
"""上下文/记忆深度四档（``ai.context_depth``）——单一事实源。

回复设置页是**云端主链**的全局档：标准 12k / 深度 32k / 最大 128k / 超大 900k。
composer「模型 ▾」按当前模型重画（Cursor 式）：

  · 标准档（云端）→ 四档全放。
  · 无限制（本机 chatx）→ 只放进得去的档，并永远有一档「满窗」＝端点
    ``max_model_len``（现役 24576 ≈ 24k）。发送路径再按端点封顶，超窗不会 400。

一档改全部相关旋钮：

  · ``prompt_budget_tokens``      发送前预算裁剪上限（ai_client._apply_prompt_budget）
  · ``history_msgs``              喂给模型的历史条数上限（ai_client max_hist；策略 context_rounds
                                  只能往上抬不往下压，0 仍表示「本策略不带历史」）
  · ``verbatim_rounds/msgs``      本地先裁：最近多少轮逐字保留、更早的压成摘要
  · ``memory_items/chars``        情景记忆注入条数 / 字数（memory.inject_max_*）
  · ``history_fetch``             从 inbox 库取多少行历史（各拟稿入口的 list_recent_messages limit）

语义：
  · 键缺席 / ``standard`` = **零行为变化**：全部返回调用方传入的 legacy 值。
  · deep / max / ultra = 各旋钮取 ``max(legacy, 档位值)``——只抬地板，不压手调的更大值。
  · 无限制会话的预算还要过 :func:`src.ai.conv_route.endpoint_prompt_cap`（本机窗口）。

消费方全部**活读** config，改完即生效，无需重启。

会话级覆盖（2026-09-12，composer 模型选择器）：:func:`override_scope` 用 contextvar 把某一次拟稿
的档位换成会话自选值（``conv_route.depth_scope``），作用域内所有消费方（ai_client 预算 /
skill_manager 历史与记忆 / history_fetch）自动跟随，无需逐处穿参数；``skip_economy=True``
时经济档 overlay 不再压帽（本地模型不走钱包）。作用域外逐字节旧行为。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Tuple

# (tier_key, skip_economy)；None＝无覆盖
_OVERRIDE: contextvars.ContextVar[Optional[Tuple[str, bool]]] = contextvars.ContextVar(
    "context_depth_override", default=None)


@contextmanager
def override_scope(tier: Any, *, skip_economy: bool = False) -> Iterator[None]:
    """作用域内 :func:`resolve` 返回 ``tier`` 档（非法值 → 不覆盖）。可嵌套，退出即还原。"""
    key = normalize_tier(tier)
    if not str(tier or "").strip():
        yield
        return
    token = _OVERRIDE.set((key, bool(skip_economy)))
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


def override_active() -> Optional[str]:
    """当前作用域覆盖的档位键；无 → None。"""
    cur = _OVERRIDE.get()
    return cur[0] if cur else None


def _economy_bypassed() -> bool:
    cur = _OVERRIDE.get()
    return bool(cur and cur[1])

TIER_KEYS: Tuple[str, ...] = ("standard", "deep", "max", "ultra")
DEFAULT_TIER = "standard"
# 173 chatx 现役窗口；无限制「满窗」档的标签与发送封顶对齐这个数，不是档位预算本身。
LAN_MAX_CTX = 24_000

# 同义写法（UI / 口述 / 中文键）→ 规范键
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
    """当前生效档位（会话级 :func:`override_scope` > ``ai.context_depth``；缺席/非法 → standard）。"""
    ov = override_active()
    if ov:
        return TIERS.get(ov, TIERS[DEFAULT_TIER])
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
    """预算：standard → legacy 原值（含 0=不裁）；最大档 → max(legacy, 档位)。legacy=0（不裁）保持 0。
    无限制会话在返回前按端点窗口封顶（手调更大值 / 不裁 也压），避免 vLLM 超窗 400。
    """
    d = resolve(config)
    if d.is_standard or not legacy:
        n = int(legacy or 0)
    else:
        n = _lift(legacy, d.prompt_budget_tokens)
    if not _economy_bypassed():
        try:
            from src.ai.usage_economy import cap_prompt_budget
            n = cap_prompt_budget(n, config)
        except Exception:
            pass
    return _clamp_unrestricted_endpoint(n, config)


def _clamp_unrestricted_endpoint(n: int, config: Any) -> int:
    """无限制会话：预算不得超过端点窗口（手调更大值 / 0=不裁 也压）。标准档不碰。"""
    try:
        from src.ai.conv_route import active_unrestricted, endpoint_prompt_cap
        if not active_unrestricted():
            return n
        cap = int(endpoint_prompt_cap(config) or 0)
        if cap <= 0:
            return n
        if not n or n > cap:
            return cap
    except Exception:
        return n
    return n


def history_limit(config: Any, legacy: int, *, strategy_rounds: Optional[int] = None) -> int:
    """喂模型的历史条数。strategy_rounds=0 尊重（该策略刻意不带历史）；否则深档只抬不压。"""
    base = legacy if strategy_rounds is None else strategy_rounds
    if strategy_rounds == 0:
        return 0
    d = resolve(config)
    n = _lift(base, None if d.is_standard else d.history_msgs)
    if _economy_bypassed():
        return n
    try:
        from src.ai.usage_economy import cap_history
        return cap_history(n, config)
    except Exception:
        return n


def verbatim_rounds(config: Any, legacy_rounds: int) -> int:
    """本地逐字保留的轮数（TG 直答路径按轮计）。"""
    d = resolve(config)
    n = _lift(legacy_rounds, None if d.is_standard else d.verbatim_rounds)
    if _economy_bypassed():
        return n
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
    if _economy_bypassed():
        return n
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
    if _economy_bypassed():
        return items, chars
    try:
        from src.ai.usage_economy import cap_memory
        return cap_memory(items, chars, config)
    except Exception:
        return items, chars


def stale_keep_msgs(config: Any, legacy: int = 3) -> int:
    """时间断层之前最多保留多少条旧历史（``persona_reply.trim_stale_history`` 的 keep_stale）。

    2026-09-18 「脑子有点空」事故：最大档从库里取 200 行，但断层修剪一刀只留 3 条旧消息，
    深度档等于白开——客户隔 5 天回来问「还记得我们第一次聊什么」，窗口里只剩 3 条旧话。
    旧消息**已带「[N天前]」时间标**，多留不会再造「把旧话当刚才」的幻觉（那是时间标 +
    【时间提示】的活）；真正的上限交给发送前的 prompt 预算裁剪（最旧先丢）。
    standard → legacy 原值（零行为变化）；deep/max/ultra → max(legacy, history_msgs)。
    """
    d = resolve(config)
    n = _lift(legacy, None if d.is_standard else d.history_msgs)
    if _economy_bypassed():
        return n
    try:
        from src.ai.usage_economy import cap_history
        return cap_history(n, config)
    except Exception:
        return n


def history_fetch_limit(config: Any, legacy: int = 30) -> int:
    """从库里取多少行历史（各拟稿入口 list_recent_messages 的 limit）。"""
    d = resolve(config)
    n = _lift(legacy, None if d.is_standard else d.history_fetch)
    if _economy_bypassed():
        return n
    try:
        from src.ai.usage_economy import cap_history
        return cap_history(n, config)
    except Exception:
        return n


def describe(config: Any = None) -> Dict[str, Any]:
    """给 UI / prompt-inspect 用的档位说明（四档全表、经济档 overlay）。

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
        "override": override_active() or "",
        "tiers": [TIERS[k].as_dict() for k in TIER_KEYS],
        "effective": {
            "prompt_budget_tokens": cur.prompt_budget_tokens,
            "history_msgs": cur.history_msgs,
            "memory_items": cur.memory_items,
        },
        "economy": econ,
    }
