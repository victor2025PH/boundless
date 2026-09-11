"""P6-4：LLM 成本 & token 观测（2026-09-08 升级为可对账计量）。

单例进程级计数器，按 ``(model, tier, account_id, purpose)`` 聚合 tokens 与估算成本。
无新增依赖；Prometheus 文本由 Web 路由读 ``dump_prom()`` 拼接。

2026-09-08 起的三个变化（成本对账实施 P0）：
- ``purpose`` 维度：客户回复 / 演练 / 记忆抽取 / 翻译 / 探针 / 评测…——没有它就回答
  不了「钱花在哪」（0908 实锤：账单里 60% 是夜间演练，看板却把它算成客户回复）。
- 价格表按 **CNY / 1K tokens** 解释（``ai.pricing`` 与硅基账单同币种），``cost_usd``
  字段名为兼容旧看板保留，其值即 ``cost``（币种由 ``currency`` 字段说明）。
- ``sink``：每笔记录同步写给持久层（``cost_ledger``），进程重启不再清零。

隐私：绝不记录任何 prompt/reply 原文，只存元数据。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# 用途 API 的单一实现在 llm_purpose（B2 可独立上 HEAD）。此处 re-export，
# 成本对账线与计量出口读到的是同一套 ContextVar。
from src.ai.llm_purpose import (  # noqa: F401
    KNOWN_PURPOSES,
    PURPOSE_ASSISTANT,
    PURPOSE_COLLOQUIAL,
    PURPOSE_CUSTOMER_REPLY,
    PURPOSE_DRILL,
    PURPOSE_EVAL,
    PURPOSE_KB,
    PURPOSE_MEMORY_EXTRACT,
    PURPOSE_PERSONA,
    PURPOSE_PROBE,
    PURPOSE_TOOL,
    PURPOSE_TRANSLATE,
    PURPOSE_UNKNOWN,
    PURPOSE_VISION,
    _PURPOSE_VAR,
    is_drill_id,
    purpose_for_reply,
    purpose_scope,
)


def provider_from_base_url(base_url: Any) -> str:
    """base_url → 厂商短名（账单主体）。LAN/私网一律 ``lan``。"""
    b = str(base_url or "").lower()
    if not b:
        return ""
    if "siliconflow" in b:
        return "siliconflow"
    if "deepseek.com" in b:
        return "deepseek"
    if "dashscope" in b or "aliyun" in b:
        return "dashscope"
    if "bigmodel" in b:
        return "zhipu"
    if "openai.com" in b:
        return "openai"
    if "volces" in b or "ark" in b:
        return "volcengine"
    if "moonshot" in b:
        return "moonshot"
    if "://192.168." in b or "://10." in b or "://127." in b or "localhost" in b:
        return "lan"
    return "other"


class LlmCostTracker:
    """按 (model, tier, account_id, purpose) 累积 tokens + 估算成本。

    - ``record(...)`` → 原子自增，并把这一笔同步交给 ``sink``（持久层）
    - ``dump()`` → 返回嵌套字典（运维 API 用）
    - ``dump_prom()`` → Prometheus 文本（/metrics 拼接用）
    - 价格表由 ``set_pricing(model_prices, currency)`` 注入；格式：
      ``{"deepseek-ai/DeepSeek-V3.2": {"prompt": 0.004, "completion": 0.006}}``
      （单位：currency / 1K tokens；生产按 CNY 填，与硅基账单同币种）
    """

    __slots__ = (
        "_lock", "_counters", "_pricing", "_currency", "_started_at",
        "_last_record_ts", "_total_cost", "_total_calls", "_sink",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: Dict[Tuple[str, str, str, str], Dict[str, float]] = {}
        self._pricing: Dict[str, Dict[str, float]] = {}
        self._currency: str = "CNY"
        self._started_at: float = time.time()
        self._last_record_ts: float = 0.0
        self._total_cost: float = 0.0
        self._total_calls: int = 0
        self._sink: Optional[Callable[[Dict[str, Any]], None]] = None

    # ── 配置 ────────────────────────────────────────────────────────────
    def set_pricing(self, pricing: Dict[str, Dict[str, float]],
                    currency: str = "CNY") -> None:
        """注入价格表；key 为 model 名（小写归一化）。"""
        with self._lock:
            norm: Dict[str, Dict[str, float]] = {}
            for k, v in (pricing or {}).items():
                if not isinstance(v, dict):
                    continue
                norm[str(k).lower()] = {
                    "prompt": float(v.get("prompt", 0) or 0),
                    "completion": float(v.get("completion", 0) or 0),
                }
            self._pricing = norm
            self._currency = str(currency or "CNY").upper()

    def set_sink(self, sink: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """挂持久层回调（每笔 record 同步调用；回调异常被吞，绝不影响出话）。"""
        with self._lock:
            self._sink = sink

    @property
    def currency(self) -> str:
        return self._currency

    def has_pricing(self) -> bool:
        return bool(self._pricing)

    def _price_for(self, model: str) -> Tuple[float, float]:
        key = (model or "").lower()
        p = self._pricing.get(key)
        if p is None:
            # 模糊匹配：去掉版本号后缀（gpt-4o-mini-2024-07-18 → gpt-4o-mini）；
            # 也兼容账单里的小写/无厂商前缀写法（deepseek-v3.2 ↔ deepseek-ai/DeepSeek-V3.2）
            for k, v in self._pricing.items():
                if key.startswith(k) or k.endswith("/" + key) or key.endswith("/" + k):
                    p = v
                    break
        if p is None:
            return (0.0, 0.0)
        return (p.get("prompt", 0.0), p.get("completion", 0.0))

    def estimate_cost(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        pp, cp = self._price_for(model)
        return (int(prompt_tokens or 0) / 1000.0) * pp + (int(completion_tokens or 0) / 1000.0) * cp

    # ── 记录 ────────────────────────────────────────────────────────────
    def record(
        self,
        *,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        tier: str = "default",
        account_id: str = "default",
        latency_ms: Optional[int] = None,
        purpose: str = PURPOSE_UNKNOWN,
        provider: str = "",
        status: str = "ok",
        suspected: bool = False,
    ) -> Dict[str, Any]:
        """原子记录一次 LLM 调用。返回该 bucket 的累计值（运维用）。

        ``status``：ok / timeout / error。``suspected=True`` 表示本地没拿到 usage
        （超时/断连），token 数是估算、服务端**可能**已计费——对账时单列。
        """
        model = str(model or "unknown")
        tier = str(tier or "default")
        account_id = str(account_id or "default")
        purpose = str(purpose or PURPOSE_UNKNOWN)
        if purpose not in KNOWN_PURPOSES:
            purpose = PURPOSE_UNKNOWN
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
        cost = self.estimate_cost(model, pt, ct)
        key = (model, tier, account_id, purpose)
        with self._lock:
            row = self._counters.get(key)
            if row is None:
                row = {
                    "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
                    "cost": 0.0, "latency_ms_sum": 0,
                    "suspected_calls": 0, "suspected_cost": 0.0,
                }
                self._counters[key] = row
            row["calls"] += 1
            row["prompt_tokens"] += pt
            row["completion_tokens"] += ct
            row["cost"] += cost
            if suspected:
                row["suspected_calls"] += 1
                row["suspected_cost"] += cost
            if latency_ms is not None:
                row["latency_ms_sum"] += int(latency_ms)
            self._total_cost += cost
            self._total_calls += 1
            self._last_record_ts = time.time()
            snapshot = dict(row)
            sink = self._sink
        if sink is not None:
            try:
                sink({
                    "ts": self._last_record_ts, "provider": str(provider or ""),
                    "model": model, "tier": tier, "account_id": account_id,
                    "purpose": purpose, "prompt_tokens": pt, "completion_tokens": ct,
                    "cost": cost, "currency": self._currency, "status": str(status or "ok"),
                    "suspected": bool(suspected), "latency_ms": int(latency_ms or 0),
                })
            except Exception:
                pass
        return snapshot

    # ── 导出 ────────────────────────────────────────────────────────────
    def dump(self) -> Dict[str, Any]:
        """返回完整状态（供 JSON API 使用）。``cost_usd`` 为兼容旧读者的别名（=cost）。"""
        with self._lock:
            rows = []
            for (m, t, a, p), v in sorted(self._counters.items()):
                rows.append({
                    "model": m, "tier": t, "account_id": a, "purpose": p,
                    **v,
                    "cost_usd": v["cost"],
                    "avg_latency_ms": (
                        v["latency_ms_sum"] / v["calls"] if v["calls"] else 0
                    ),
                })
            return {
                "started_at": self._started_at,
                "last_record_ts": self._last_record_ts,
                "total_calls": self._total_calls,
                "currency": self._currency,
                "total_cost": round(self._total_cost, 6),
                "total_cost_usd": round(self._total_cost, 6),
                "pricing_models": sorted(self._pricing.keys()),
                "rows": rows,
            }

    def dump_prom(self) -> str:
        """Prometheus 文本。"""
        lines = []
        with self._lock:
            lines.append(
                f"# HELP messenger_rpa_llm_total_cost_usd Accumulated LLM cost ({self._currency})"
            )
            lines.append("# TYPE messenger_rpa_llm_total_cost_usd counter")
            lines.append(
                f"messenger_rpa_llm_total_cost_usd {self._total_cost:.6f}"
            )
            lines.append(
                "# HELP messenger_rpa_llm_total_calls LLM calls (all accounts+tiers)"
            )
            lines.append("# TYPE messenger_rpa_llm_total_calls counter")
            lines.append(f"messenger_rpa_llm_total_calls {self._total_calls}")

            lines.append(
                "# HELP messenger_rpa_llm_tokens_total Total LLM tokens by bucket"
            )
            lines.append("# TYPE messenger_rpa_llm_tokens_total counter")
            lines.append(
                f"# HELP messenger_rpa_llm_cost_usd_total LLM cost ({self._currency}) by bucket"
            )
            lines.append("# TYPE messenger_rpa_llm_cost_usd_total counter")
            lines.append(
                "# HELP messenger_rpa_llm_calls_total LLM calls by bucket"
            )
            lines.append("# TYPE messenger_rpa_llm_calls_total counter")
            for (m, t, a, p), v in self._counters.items():
                labels = (
                    f'model="{_esc(m)}",tier="{_esc(t)}",account="{_esc(a)}",'
                    f'purpose="{_esc(p)}"'
                )
                lines.append(
                    f'messenger_rpa_llm_tokens_total{{{labels},kind="prompt"}}'
                    f' {v["prompt_tokens"]}'
                )
                lines.append(
                    f'messenger_rpa_llm_tokens_total{{{labels},kind="completion"}}'
                    f' {v["completion_tokens"]}'
                )
                lines.append(
                    f'messenger_rpa_llm_cost_usd_total{{{labels}}} '
                    f'{v["cost"]:.6f}'
                )
                lines.append(
                    f'messenger_rpa_llm_calls_total{{{labels}}} {v["calls"]}'
                )
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._total_cost = 0.0
            self._total_calls = 0
            self._last_record_ts = 0.0


def _esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


_SINGLETON: Optional[LlmCostTracker] = None
_LOCK = threading.Lock()


def get_llm_cost() -> LlmCostTracker:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = LlmCostTracker()
    return _SINGLETON


def record_usage_from_response(
    response: Any, *, model: str, purpose: str, provider: str = "",
    tier: str = "default", account_id: str = "default",
    latency_ms: Optional[int] = None,
) -> None:
    """从 OpenAI SDK 响应对象 / 兼容 dict 抽 usage 并记账。绝不抛。

    统一出口——直连 ``chat.completions.create`` 的调用点在拿到响应后调一行即可，
    不必各自解析 usage（0908 之前 12 处直连各写各的、多数根本没记）。
    """
    try:
        pt = ct = 0
        u = None
        if isinstance(response, dict):
            u = response.get("usage") or {}
            pt = int(u.get("prompt_tokens") or 0)
            ct = int(u.get("completion_tokens") or 0)
            if not pt and not ct:
                # Ollama 原生口
                pt = int(response.get("prompt_eval_count") or 0)
                ct = int(response.get("eval_count") or 0)
        else:
            u = getattr(response, "usage", None)
            if u is not None:
                pt = int(getattr(u, "prompt_tokens", 0) or 0)
                ct = int(getattr(u, "completion_tokens", 0) or 0)
        get_llm_cost().record(
            model=str(model or getattr(response, "model", "") or "unknown"),
            prompt_tokens=pt, completion_tokens=ct, tier=tier,
            account_id=account_id, latency_ms=latency_ms, purpose=purpose,
            provider=provider, status="ok", suspected=(u is None),
        )
    except Exception:
        pass


__all__ = [
    "LlmCostTracker", "get_llm_cost", "record_usage_from_response",
    "is_drill_id", "purpose_for_reply", "purpose_scope", "provider_from_base_url",
    "KNOWN_PURPOSES",
    "PURPOSE_CUSTOMER_REPLY", "PURPOSE_DRILL", "PURPOSE_MEMORY_EXTRACT",
    "PURPOSE_TRANSLATE", "PURPOSE_ASSISTANT", "PURPOSE_KB", "PURPOSE_VISION",
    "PURPOSE_PROBE", "PURPOSE_EVAL", "PURPOSE_TOOL", "PURPOSE_COLLOQUIAL",
    "PURPOSE_PERSONA", "PURPOSE_UNKNOWN",
]
