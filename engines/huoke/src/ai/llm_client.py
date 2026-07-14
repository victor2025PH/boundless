"""
Unified LLM Client — provider-agnostic interface for text + vision models.

Supports:
- DeepSeek (default, cheapest for Chinese + English)
- OpenAI-compatible APIs (GPT-4o, local vLLM, Ollama, etc.)
- Automatic retry with exponential backoff
- Token usage tracking for cost monitoring
- Response caching (SHA256 key → SQLite)

Design: All AI modules (MessageRewriter, AutoReply, VisionFallback) use this
single client. Switch providers by changing config, not code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import yaml
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.host.device_registry import config_file, data_file
from src.openclaw_env import local_api_base

log = logging.getLogger(__name__)

# 2026-05-13: 断路器告警冷却：防止断路器震荡时刷屏（30 分钟内至多一条）
_CB_ALERT_COOLDOWN_SEC = 1800


class LLMUnavailableError(RuntimeError):
    """2026-05-13: 主 provider 和 fallback provider 均不可用（断路器均开路或均超时）。

    调用方可捕获此异常并返回缓存/模板响应，而不是静默失败。
    只在 chat_messages(..., raise_if_unavailable=True) 时触发。
    """


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class LLMConfig:
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    vision_model: str = ""
    temperature: float = 0.7
    max_tokens: int = 512
    timeout_sec: float = 30.0
    max_retries: int = 3
    cache_enabled: bool = True
    cache_db_path: str = ""
    # 2026-05-13: 响应质量门控
    # 0 = 关闭; 设为正整数则小于此字符数的响应会触发 fallback
    min_response_len: int = 0
    # 2026-05-13: 缓存自动维护
    # cache_ttl_days: 超过此天数的缓存条目在启动时自动清理（0=关闭）
    # cache_max_rows: 缓存最大行数，超出时删除最旧条目（0=不限制）
    cache_ttl_days: int = 7
    cache_max_rows: int = 50000
    # 2026-05-13: 断路器（circuit breaker）
    # 防止主 provider 反复超时/5xx 堆积，open 后快速失败并由 fallback 接管
    # cb_failure_threshold: 连续瞬态失败（timeout/5xx）N 次后开路
    # cb_reset_timeout_sec:  开路 N 秒后切换 half_open 允许一次探针请求
    cb_enabled: bool = True
    cb_failure_threshold: int = 5
    cb_reset_timeout_sec: float = 300.0

    def __post_init__(self):
        if not self.api_key:
            env_map = {
                "deepseek": "DEEPSEEK_API_KEY",
                "openai": "OPENAI_API_KEY",
                "gemini": "GEMINI_API_KEY",
                "zhipu": "ZHIPU_API_KEY",
                "ollama": "",
            }
            env_var = env_map.get(self.provider, "")
            self.api_key = os.environ.get(env_var, "") if env_var else ""
            if not self.api_key:
                self.api_key = os.environ.get("ZHIPU_API_KEY", "") or \
                               os.environ.get("DEEPSEEK_API_KEY", "") or \
                               os.environ.get("OPENAI_API_KEY", "")

        if not self.base_url:
            providers = {
                "deepseek": "https://api.deepseek.com/v1",
                "openai": "https://api.openai.com/v1",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
                "zhipu": "https://open.bigmodel.cn/api/paas/v4",
                "ollama": "http://localhost:11434/v1",
                "local": f"{local_api_base('localhost')}/v1",
            }
            self.base_url = providers.get(self.provider, self.base_url)

        if not self.model:
            models = {
                "deepseek": "deepseek-chat",
                "openai": "gpt-4o-mini",
                "gemini": "gemini-2.5-flash",
                "zhipu": "glm-4-flash",
                "ollama": "llava:7b",
                "local": "default",
            }
            self.model = models.get(self.provider, "default")

        if not self.vision_model:
            vision = {
                "deepseek": "deepseek-chat",
                "openai": "gpt-4o",
                "gemini": "gemini-2.5-flash",
                "zhipu": "glm-4v-flash",
                "ollama": "llava:7b",
                "local": "default",
            }
            self.vision_model = vision.get(self.provider, "default")

        if not self.cache_db_path:
            self.cache_db_path = str(data_file("llm_cache.db"))


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

@dataclass
class UsageStats:
    total_calls: int = 0
    cached_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    errors: int = 0
    # 2026-05-13: 质量监控新字段
    low_quality_responses: int = 0   # 响应长度不达标的次数
    fallback_triggers: int = 0       # 主 provider 失败后触发 fallback 的次数
    total_latency_ms: float = 0.0    # 成功调用的总延迟（ms）
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # 2026-05-13: 最近 100 次成功调用延迟 ring-buffer，用于 p95 自适应超时
    _latency_ring: deque = field(
        default_factory=lambda: deque(maxlen=100), repr=False)

    def record(self, input_tokens: int, output_tokens: int,
               cached: bool = False, latency_ms: float = 0.0):
        with self._lock:
            self.total_calls += 1
            if cached:
                self.cached_calls += 1
            else:
                self.total_input_tokens += input_tokens
                self.total_output_tokens += output_tokens
                self.total_cost_usd += self._estimate_cost(input_tokens, output_tokens)
                if latency_ms > 0:
                    self.total_latency_ms += latency_ms
                    self._latency_ring.append(latency_ms)

    def record_error(self):
        with self._lock:
            self.errors += 1

    def record_low_quality(self):
        with self._lock:
            self.low_quality_responses += 1

    def record_fallback_trigger(self):
        with self._lock:
            self.fallback_triggers += 1

    @staticmethod
    def _estimate_cost(inp: int, out: int) -> float:
        # DeepSeek pricing: ~$0.14/M input, ~$0.28/M output (2025)
        return (inp * 0.14 + out * 0.28) / 1_000_000

    def snapshot(self) -> dict:
        with self._lock:
            live_calls = max(1, self.total_calls - self.cached_calls)
            return {
                "total_calls": self.total_calls,
                "cached_calls": self.cached_calls,
                "cache_hit_rate": f"{self.cached_calls/max(1,self.total_calls)*100:.1f}%",
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "estimated_cost_usd": round(self.total_cost_usd, 4),
                "errors": self.errors,
                "low_quality_responses": self.low_quality_responses,
                "fallback_triggers": self.fallback_triggers,
                "avg_latency_ms": round(self.total_latency_ms / live_calls, 1),
                "p95_latency_ms": round(self.latency_p95_sec() * 1000, 1),
                "latency_samples": len(self._latency_ring),
            }

    def latency_p95_sec(self) -> float:
        """2026-05-13: 最近 100 次成功调用的 p95 延迟（秒），样本不足 20 返回 0。"""
        with self._lock:
            ring = list(self._latency_ring)
        if len(ring) < 20:
            return 0.0
        return sorted(ring)[int(0.95 * len(ring))] / 1000.0

    def clear_latency_ring(self) -> None:
        """2026-05-13: 清空延迟样本，让 p95 从头重建。

        由 CB 恢复时调用：断路器开路期间累积的慢响应样本应予以丢弃，
        不应忙染恢复后的有效超时计算。
        """
        with self._lock:
            self._latency_ring.clear()


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------

class LLMClient:
    """
    Unified LLM client for all AI modules.

    Usage:
        client = LLMClient()
        response = client.chat("Rewrite this message in a friendly tone: ...")
        response = client.chat_with_system("You are a helpful assistant.", "Hello")
    """

    def __init__(self, config: Optional[LLMConfig] = None,
                 _fallback_config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self.stats = UsageStats()
        self._http = httpx.Client(
            timeout=self.config.timeout_sec,
            headers={"Authorization": f"Bearer {self.config.api_key}"},
        )
        self._cache_lock = threading.Lock()
        # P5c (2026-04-24): 失败时记最后一次 HTTP error code + body 给 caller
        # 做 provider swap 决策 (e.g. VLM Gemini 503 → Ollama fallback)。
        # 成功 call 会清为 None/""; 只保留最后一次 retry 的 error 信息。
        self.last_error_code: Optional[int] = None
        self.last_error_body: str = ""
        # 2026-05-13: fallback provider 支持 — 主 provider 失败后自动切换
        self._fallback_config: Optional[LLMConfig] = _fallback_config
        self._fallback_client_cache: Optional["LLMClient"] = None
        # 2026-05-13: 断路器状态机（closed → open → half_open → closed）
        self._cb_lock = threading.Lock()
        self._cb_state: str = "closed"  # closed / open / half_open
        self._cb_failures: int = 0       # 连续瞬态失败计数
        self._cb_opened_at: float = 0.0  # 开路时间戳
        self._cb_last_alert_ts: float = 0.0  # 最后一次开路告警时间（去重用）
        # 2026-05-13: CB 状态变迁历史——每条 (ts, from_state, to_state)
        self._cb_events: deque = deque(maxlen=100)
        if self.config.cache_enabled:
            self._init_cache()

    # -- Circuit Breaker ---------------------------------------------------

    def _cb_check(self) -> str:
        """2026-05-13: 返回 'allow' / 'probe' / 'block'。

        allow  — closed，正常发送
        probe  — half_open，允许一次探针请求
        block  — open 且未到重置超时，快速失败
        """
        if not self.config.cb_enabled:
            return "allow"
        with self._cb_lock:
            if self._cb_state == "closed":
                return "allow"
            if self._cb_state == "open":
                if time.time() - self._cb_opened_at >= self.config.cb_reset_timeout_sec:
                    self._cb_state = "half_open"
                    log.info("[llm] circuit breaker → HALF_OPEN (probe) provider=%s",
                             self.config.provider)
                    return "probe"
                return "block"
            return "probe"  # half_open

    def _cb_on_success(self):
        """2026-05-13: 成功响应，关闭断路器并重置计数。"""
        if not self.config.cb_enabled:
            return
        just_recovered = False
        prev_state = ""
        with self._cb_lock:
            prev_state = self._cb_state
            if self._cb_state != "closed":
                log.info("[llm] circuit breaker → CLOSED (recovered) provider=%s",
                         self.config.provider)
                just_recovered = True
                # 2026-05-13: 记录状态变迁事件
                self._cb_events.append((time.time(), prev_state, "closed"))
            self._cb_state = "closed"
            self._cb_failures = 0
        if just_recovered:
            # 2026-05-13: 断路器恢复时清空延迟样本，防止 CB 开路期圆慢响应污染恢复后的 p95 自适应超时
            if prev_state == "half_open":
                self.stats.clear_latency_ring()
                log.debug("[llm] CB 恢复：延迟样本已清空，p95 将从头重建")
            self._cb_notify_recovered()

    def _cb_on_transient_failure(self):
        """2026-05-13: 瞬态失败（timeout/5xx），累计后触发开路。

        401/429/4xx 不计入：前者需人工处理，后者是业务限流，断路无益。
        """
        if not self.config.cb_enabled:
            return
        just_opened = False
        with self._cb_lock:
            self._cb_failures += 1
            if (self._cb_state == "half_open"
                    or self._cb_failures >= self.config.cb_failure_threshold):
                prev = self._cb_state
                self._cb_state = "open"
                self._cb_opened_at = time.time()
                if prev != "open":
                    just_opened = True
                    log.warning(
                        "[llm] circuit breaker → OPEN provider=%s "
                        "(consecutive_failures=%d, reset_in=%ds)",
                        self.config.provider, self._cb_failures,
                        int(self.config.cb_reset_timeout_sec),
                    )
        if just_opened:
            with self._cb_lock:
                # 2026-05-13: 记录开路事件
                self._cb_events.append((time.time(), "closed", "open"))
            self._cb_notify_open()

    def _cb_notify_open(self):
        """2026-05-13: 断路器开路时通过 AlertNotifier + event_stream 推送告警。

        使用延迟导入避免 ai 层对 host 层的循环依赖。
        30 分钟内同一 provider 开路只推一次，防止断路器震荡时告警刷屏。
        """
        # 告警去重：30 分钟冷却
        now = time.time()
        with self._cb_lock:
            if now - self._cb_last_alert_ts < _CB_ALERT_COOLDOWN_SEC:
                return
            self._cb_last_alert_ts = now
        msg = (
            f"[LLM] 断路器开路: provider={self.config.provider}，"
            f"连续失败 {self._cb_failures} 次，"
            f"{int(self.config.cb_reset_timeout_sec)}s 后尝试恢复。"
            f"fallback 将接管请求。"
        )
        try:
            from src.host.alert_notifier import AlertNotifier
            AlertNotifier.get().notify("error", "", msg)
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event
            push_event("llm.circuit_open", {
                "provider": self.config.provider,
                "failures": self._cb_failures,
                "reset_in_sec": int(self.config.cb_reset_timeout_sec),
            })
        except Exception:
            pass

    def _cb_notify_recovered(self):
        """2026-05-13: 断路器恢复时推送 info 告警，并重置告警冷却使下次开路立即通知。"""
        # 重置冷却，确保下次开路时立即发告警（不被上次冷却压制）
        with self._cb_lock:
            self._cb_last_alert_ts = 0.0
        msg = f"[LLM] 断路器已恢复: provider={self.config.provider} ✅"
        try:
            from src.host.alert_notifier import AlertNotifier
            AlertNotifier.get().notify("info", "", msg)
        except Exception:
            pass
        try:
            from src.host.event_stream import push_event
            push_event("llm.circuit_closed", {"provider": self.config.provider})
        except Exception:
            pass

    def cb_status(self) -> dict:
        """2026-05-13: 返回断路器快照，供 /ai/stats 端点展示。"""
        with self._cb_lock:
            remaining = 0
            if self._cb_state == "open":
                elapsed = time.time() - self._cb_opened_at
                remaining = max(0, int(self.config.cb_reset_timeout_sec - elapsed))
            # 2026-05-13: 暴露最近 10 条状态变迁事件
            recent_events = [
                {"ts": round(ts, 3), "from": frm, "to": to}
                for ts, frm, to in list(self._cb_events)[-10:]
            ]
            return {
                "state": self._cb_state,
                "consecutive_failures": self._cb_failures,
                "reset_in_sec": remaining,
                "recent_events": recent_events,
            }

    def _get_fallback_client(self) -> Optional["LLMClient"]:
        """懒初始化 fallback LLM client（避免启动时建立两个 HTTP 连接池）。"""
        if not self._fallback_config:
            return None
        if self._fallback_client_cache is None:
            self._fallback_client_cache = LLMClient(self._fallback_config)
            log.info("[llm] fallback client 已初始化: provider=%s model=%s",
                     self._fallback_config.provider, self._fallback_config.model)
        return self._fallback_client_cache

    def close(self):
        self._http.close()
        if self._fallback_client_cache:
            self._fallback_client_cache.close()

    # -- Core API -----------------------------------------------------------

    def chat(self, user_message: str, temperature: Optional[float] = None,
             max_tokens: Optional[int] = None, use_cache: bool = True) -> str:
        """Simple single-turn chat. Returns assistant message text."""
        return self.chat_with_system("", user_message, temperature, max_tokens, use_cache)

    def chat_with_system(self, system: str, user: str,
                         temperature: Optional[float] = None,
                         max_tokens: Optional[int] = None,
                         use_cache: bool = True) -> str:
        """Chat with system prompt. Returns assistant message text."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return self.chat_messages(messages, temperature, max_tokens, use_cache)

    def chat_messages(self, messages: List[Dict[str, Any]],
                      temperature: Optional[float] = None,
                      max_tokens: Optional[int] = None,
                      use_cache: bool = True,
                      raise_if_unavailable: bool = False) -> str:
        """Full messages API (sync). Returns assistant message text.

        Args:
            raise_if_unavailable: if True and all providers fail (CB open/timeout),
                raises LLMUnavailableError instead of returning ''. Default False.
        Note: Core AI routing is intentionally NOT here (sync would block event loop).
        Use chat_messages_async() from async FastAPI handlers for Core routing.
        """
        temp = temperature if temperature is not None else self.config.temperature
        tokens = max_tokens or self.config.max_tokens

        cache_key = self._cache_key(messages, temp) if use_cache else None
        if cache_key and self.config.cache_enabled:
            cached = self._get_cache(cache_key)
            if cached is not None:
                self.stats.record(0, 0, cached=True)
                return cached

        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
        }

        response_text = self._call_api(payload)

        # 2026-05-13: 响应质量门控
        # 如配置了 min_response_len，过短的响应触发 fallback 重试
        if (response_text and self.config.min_response_len > 0
                and len(response_text.strip()) < self.config.min_response_len):
            self.stats.record_low_quality()
            log.warning("[llm] 响应过短 (%d chars < min %d)，触发 fallback 重试",
                        len(response_text.strip()), self.config.min_response_len)
            fallback = self._get_fallback_client()
            if fallback:
                _fb_model = fallback.config.model
                fb_response = fallback._call_api({**payload, "model": _fb_model})
                if fb_response and len(fb_response.strip()) >= self.config.min_response_len:
                    self.stats.record_fallback_trigger()
                    response_text = fb_response
                    log.info("[llm] fallback provider=%s 质量门控通过 (%d chars)",
                             fallback.config.provider, len(fb_response.strip()))

        if cache_key and self.config.cache_enabled and response_text:
            self._set_cache(cache_key, response_text)

        # 2026-05-13: LLM 完全不可用判断
        if not response_text and raise_if_unavailable:
            cause = self.last_error_body or "unknown"
            raise LLMUnavailableError(
                f"所有 LLM provider 均不可用 (cause={cause})，"
                f"主 provider={self.config.provider}"
            )

        return response_text

    async def chat_messages_async(self, messages: List[Dict[str, Any]],
                                  temperature: Optional[float] = None,
                                  max_tokens: Optional[int] = None,
                                  use_cache: bool = True) -> str:
        """
        P0-1 Fix: async版本，供FastAPI async路由使用。
        先异步尝试Core AI，失败后在线程池里运行本地同步LLM，不阻塞事件循环。
        """
        import asyncio

        # 1. 异步尝试 Core AI（不阻塞）
        core_reply = await self._try_core_chat_async(messages)
        if core_reply:
            return core_reply

        # 2. 本地LLM在线程池运行（不阻塞asyncio event loop）
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self.chat_messages,
            messages, temperature, max_tokens, use_cache,
        )

    async def _try_core_chat_async(self, messages: List[Dict[str, Any]]) -> str:
        """P0-1: 异步Core AI路由，使用httpx.AsyncClient，不阻塞事件循环。"""
        core_url = os.environ.get("OPENCLAW_CORE_URL", "").rstrip("/")
        core_token = os.environ.get("OPENCLAW_CORE_TOKEN", "")
        if not core_url:
            return ""
        try:
            headers = {"Content-Type": "application/json"}
            if core_token:
                headers["Authorization"] = f"Bearer {core_token}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{core_url}/api/chat",
                    json={"messages": messages, "stream": False},
                    headers=headers,
                )
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                text = (
                    (choices[0].get("message", {}).get("content", "") if choices else "")
                    or data.get("reply", "")
                    or data.get("text", "")
                ).strip()
                if text:
                    log.debug("Core AI 路由成功 (%d chars)", len(text))
                    return text
        except Exception as e:
            log.debug("Core AI 路由失败，降级本地: %s", e)
        return ""

    def chat_vision(self, text_prompt: str, image_base64: str,
                    max_tokens: Optional[int] = None) -> str:
        """Vision API: send text + image, get response."""
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": text_prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{image_base64}"
                }},
            ],
        }]

        tokens = max_tokens or 256
        # Gemini 2.5 thinking models consume extra tokens for reasoning
        if "gemini" in self.config.provider and "2.5" in self.config.vision_model:
            tokens = max(tokens, 2048)

        payload = {
            "model": self.config.vision_model,
            "messages": messages,
            "max_tokens": tokens,
        }

        return self._call_api(payload)

    # -- HTTP with retry ----------------------------------------------------

    def _call_api(self, payload: dict) -> str:
        """HTTP call with retry. 2026-04-24 P5c: 失败时保留 last_error_code
        / last_error_body 供 caller debug + provider swap 决策 (e.g. VLM
        Gemini 503 → Ollama fallback 判定)。

        成功时 reset to None/""; 失败时写入最后一次 error 信息。返 "" 表示
        所有 retry 用完, caller 应看 last_error_code 判断根因。
        """
        url = f"{self.config.base_url.rstrip('/')}/chat/completions"
        # 2026-05-13: 自适应超时 — p95 × 1.5 + 至少 5s，上限为 yaml 配置值
        _p95 = self.stats.latency_p95_sec()
        _eff_timeout = (
            max(5.0, min(_p95 * 1.5, self.config.timeout_sec))
            if _p95 > 0 else self.config.timeout_sec
        )

        # 2026-05-13: 断路器前置检查
        _cb_decision = self._cb_check()
        if _cb_decision == "block":
            log.warning("[llm] circuit breaker OPEN — 跳过 provider=%s，等待 fallback 接管",
                        self.config.provider)
            # 直接进入 fallback 判断（last_error_code 保留上次值）
            self.last_error_body = "circuit_breaker_open"
            return ""

        for attempt in range(self.config.max_retries):
            try:
                _t0 = time.time()
                resp = self._http.post(url, json=payload, timeout=_eff_timeout)
                if resp.status_code == 200:
                    _latency_ms = (time.time() - _t0) * 1000
                    data = resp.json()
                    text = data["choices"][0]["message"]["content"].strip()
                    usage = data.get("usage", {})
                    self.stats.record(
                        usage.get("prompt_tokens", 0),
                        usage.get("completion_tokens", 0),
                        latency_ms=_latency_ms,
                    )
                    # 成功 — 清 error state + 关闭断路器
                    self.last_error_code = None
                    self.last_error_body = ""
                    self._cb_on_success()
                    return text

                # 记 error (每次 retry 覆盖, 最终保留最后一次)
                self.last_error_code = resp.status_code
                self.last_error_body = resp.text[:500] if resp.text else ""

                if resp.status_code == 429:
                    wait = min(2 ** attempt * 5, 60)
                    log.warning("LLM rate limited, waiting %ds", wait)
                    time.sleep(wait)
                    continue

                log.error("LLM API error %d: %s", resp.status_code, resp.text[:200])
                self.stats.record_error()
                if resp.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                # 4xx (非 429): 不重试，break 出 loop 进入 fallback 判断
                # 401 key 失效 / 403 权限不足 → fallback 有机会接管
                break

            except httpx.TimeoutException:
                self.last_error_code = None
                self.last_error_body = "timeout"
                log.warning("LLM timeout (attempt %d/%d)", attempt + 1, self.config.max_retries)
                time.sleep(2 ** attempt)
            except Exception as e:
                self.last_error_code = None
                self.last_error_body = str(e)[:500]
                log.error("LLM call failed: %s", e)
                self.stats.record_error()
                time.sleep(2 ** attempt)

        log.error("LLM call exhausted all retries")
        self.stats.record_error()
        # 2026-05-13: 所有重试耗尽且属于瞬态失败 → 更新断路器计数
        # last_error_code is None (timeout/network) 或 >=500 (5xx) 才计入
        if self.last_error_code is None or self.last_error_code >= 500:
            self._cb_on_transient_failure()

        # 2026-05-13: 主 provider 重试耗尽 → 尝试 fallback provider
        # 触发条件: timeout(None) / 401 key失效 / 429 限流 / 5xx 服务端错误
        # 不触发: 402~428(非401/429) 等业务型客户端错误
        fallback = self._get_fallback_client()
        _should_fallback = (
            fallback is not None and (
                self.last_error_body == "circuit_breaker_open"  # CB 开路始终尝试 fallback
                or self.last_error_code is None          # 超时 / 网络错误
                or self.last_error_code == 401        # API key 失效
                or self.last_error_code == 429        # 限流
                or (self.last_error_code is not None and self.last_error_code >= 500)  # 5xx
            )
        )
        if _should_fallback:
            # 适配 fallback 的 model: vision payload 用 vision_model
            _pmodel = payload.get("model", "")
            if _pmodel and _pmodel == self.config.vision_model:
                _fb_model = fallback.config.vision_model
            else:
                _fb_model = fallback.config.model
            fallback_payload = {**payload, "model": _fb_model}
            log.warning("[llm] 主 provider=%s 失败 (code=%s)，切换到 fallback provider=%s",
                        self.config.provider, self.last_error_code, fallback.config.provider)
            self.stats.record_fallback_trigger()
            result = fallback._call_api(fallback_payload)
            if result:
                return result
            log.error("[llm] fallback provider=%s 也失败，最终返回空",
                      fallback.config.provider)
        return ""

    # -- Cache --------------------------------------------------------------

    def _init_cache(self):
        Path(self.config.cache_db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.config.cache_db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts    REAL NOT NULL
            )
        """)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.commit()
        conn.close()
        # 2026-05-13: 启动时自动清理过期条目
        if self.config.cache_ttl_days > 0:
            self.clear_cache(older_than_days=self.config.cache_ttl_days)
        self._cache_write_count = 0  # 计数器，用于周期性自动清理

    def _cache_key(self, messages: list, temperature: float) -> str:
        raw = json.dumps({"m": messages, "t": temperature, "model": self.config.model},
                         sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _get_cache(self, key: str) -> Optional[str]:
        if not self.config.cache_enabled:
            return None
        with self._cache_lock:
            try:
                conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
                row = conn.execute("SELECT value FROM llm_cache WHERE key=?", (key,)).fetchone()
                conn.close()
                return row[0] if row else None
            except Exception:
                return None

    def _set_cache(self, key: str, value: str):
        with self._cache_lock:
            try:
                conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
                conn.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, value, ts) VALUES (?, ?, ?)",
                    (key, value, time.time()),
                )
                conn.commit()
                conn.close()
            except Exception as e:
                log.warning("Cache write failed: %s", e)
                return
            # 2026-05-13: 每 500 次写入触发一次自动维护（TTL 过期 + 容量上限）
            self._cache_write_count = getattr(self, "_cache_write_count", 0) + 1
            if self._cache_write_count % 500 == 0:
                self._auto_clean_cache()

    def _auto_clean_cache(self):
        """2026-05-13: 维护 TTL 过期和最大容量上限。"""
        try:
            conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
            if self.config.cache_ttl_days > 0:
                cutoff = time.time() - self.config.cache_ttl_days * 86400
                deleted = conn.execute(
                    "DELETE FROM llm_cache WHERE ts < ?", (cutoff,)
                ).rowcount
                if deleted:
                    log.info("[llm_cache] TTL 清理: 删除 %d 条过期缓存 (ttl=%dd)",
                             deleted, self.config.cache_ttl_days)
            if self.config.cache_max_rows > 0:
                row = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()
                count = row[0] if row else 0
                if count > self.config.cache_max_rows:
                    excess = count - self.config.cache_max_rows
                    conn.execute(
                        "DELETE FROM llm_cache WHERE key IN "
                        "(SELECT key FROM llm_cache ORDER BY ts ASC LIMIT ?)",
                        (excess,)
                    )
                    log.info("[llm_cache] 容量限制: 删除 %d 最旧缓存 (max=%d)",
                             excess, self.config.cache_max_rows)
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("[llm_cache] 自动清理失败: %s", e)

    def clear_cache(self, older_than_days: int = 30):
        cutoff = time.time() - older_than_days * 86400
        try:
            conn = sqlite3.connect(self.config.cache_db_path, timeout=5)
            conn.execute("DELETE FROM llm_cache WHERE ts < ?", (cutoff,))
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("Cache cleanup failed: %s", e)

    # -- Test / health check ------------------------------------------------

    def test_connection(self) -> Tuple[bool, str]:
        """Quick health check: send a tiny prompt and see if we get a response."""
        if not self.config.api_key:
            return False, "No API key configured (set DEEPSEEK_API_KEY or OPENAI_API_KEY)"
        try:
            resp = self.chat("Reply with exactly: OK", max_tokens=5, use_cache=False)
            if resp:
                return True, f"Connected to {self.config.provider} ({self.config.model})"
            return False, "Empty response from LLM"
        except Exception as e:
            return False, str(e)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: Optional[LLMClient] = None
_client_lock = threading.Lock()
_client_yaml_mtime: float = 0.0  # 2026-05-13: ai.yaml 版本跟踪，用于热重载检测


def _llm_config_from_ai_yaml() -> Tuple[LLMConfig, Optional[LLMConfig]]:
    """从 config/ai.yaml 的 llm 段构建主 + 备用 LLMConfig.

    返回 (primary, fallback)，fallback 可为 None（未配置 fallback_provider 时）。
    """
    path = config_file("ai.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log.debug("ai.yaml 未读取 (%s)，使用环境变量", e)
        return LLMConfig(), None
    llm = data.get("llm") or {}
    if not llm:
        return LLMConfig(), None

    # ── 主 provider ──────────────────────────────────────────────────────
    kwargs: Dict[str, Any] = {}
    for k in (
        "provider", "api_key", "model", "vision_model", "temperature", "max_tokens",
        "timeout_sec", "max_retries", "cache_enabled", "min_response_len",
        "cache_ttl_days", "cache_max_rows",
        "cb_enabled", "cb_failure_threshold", "cb_reset_timeout_sec",
    ):
        if k in llm and llm[k] is not None:
            kwargs[k] = llm[k]
    primary = LLMConfig(**kwargs)

    # ── 备用 provider（fallback_*）─────────────────────────────────────
    fallback: Optional[LLMConfig] = None
    fb_provider = (llm.get("fallback_provider") or "").strip()
    if fb_provider:
        fb_kwargs: Dict[str, Any] = {"provider": fb_provider}
        # api_key: 优先 yaml > 环境变量（LLMConfig.__post_init__ 会自动补）
        if llm.get("fallback_api_key"):
            fb_kwargs["api_key"] = llm["fallback_api_key"]
        if llm.get("fallback_base_url"):
            fb_kwargs["base_url"] = llm["fallback_base_url"]
        if llm.get("fallback_model"):
            fb_kwargs["model"] = llm["fallback_model"]
        # fallback 继承主 provider 的通用参数，重试次数减半
        fb_kwargs["timeout_sec"] = primary.timeout_sec
        fb_kwargs["max_retries"] = max(1, (primary.max_retries or 3) // 2)
        fb_kwargs["cache_enabled"] = primary.cache_enabled
        fallback = LLMConfig(**fb_kwargs)
        log.info("[llm] 已配置 fallback provider: %s / %s",
                 fb_provider, fb_kwargs.get("model", "(auto)"))

    return primary, fallback


def _audit_llm_config(cfg: LLMConfig, section: str = "llm") -> None:
    """2026-05-13: 把脚敏化的 LLMConfig 写入配置审计日志。

    api_key / fallback_api_key 不写入：防止密钥泄漏到审计文件。
    """
    try:
        import dataclasses
        raw = dataclasses.asdict(cfg)
        sanitized = {k: v for k, v in raw.items()
                     if "api_key" not in k and not k.startswith("_")}
        from src.host.config_audit import record as _audit
        _audit("ai.yaml", section, sanitized)
    except Exception:
        pass


def get_llm_client(config: Optional[LLMConfig] = None) -> LLMClient:
    """2026-05-13: 单例工厂。支持 ai.yaml 热重载：mtime 变化时重建 LLMClient，CB 状态自动重置。"""
    global _client, _client_yaml_mtime
    if config is not None:
        # 外部传入配置：直接创建（不管理单例）
        with _client_lock:
            _client = LLMClient(config)
        return _client

    # 快速路径：已有实例且 mtime 未变（锁外检测，避免锁争用）
    try:
        _cur_mtime = config_file("ai.yaml").stat().st_mtime
    except Exception:
        _cur_mtime = 0.0

    if _client is not None and _cur_mtime == _client_yaml_mtime:
        return _client

    with _client_lock:
        # 双重检查（锁内再读一次 mtime，不信任锁外的旧快照）
        try:
            _cur_mtime = config_file("ai.yaml").stat().st_mtime
        except Exception:
            _cur_mtime = 0.0
        if _client is not None and _cur_mtime == _client_yaml_mtime:
            return _client

        is_reload = (_client is not None and _client_yaml_mtime > 0)
        primary, fallback = _llm_config_from_ai_yaml()
        _client = LLMClient(primary, _fallback_config=fallback)
        _client_yaml_mtime = _cur_mtime

        if is_reload:
            # 2026-05-13: 同步失效 Vision Client，保持与主 client 版本一致
            global _vision_client
            _vision_client = None
            log.info("[llm] ai.yaml 已热重载，主/Vision client 均已重建，CB 状态已重置")
            _audit_llm_config(primary)
        else:
            log.info("[llm] LLMClient 已初始化: provider=%s model=%s",
                     primary.provider, primary.model)
    return _client


# ---------------------------------------------------------------------------
# 免费 Vision 客户端 (用于 AI 精筛头像/资料页)
# ---------------------------------------------------------------------------

_vision_client: Optional[LLMClient] = None
_vision_lock = threading.Lock()


def get_free_vision_client() -> Optional[LLMClient]:
    """
    获取免费的 Vision LLM 客户端, 按优先级尝试:
      1. Google Gemini (GEMINI_API_KEY) — 免费 1500次/天
      2. Ollama 本地 (自动检测是否运行中) — 完全免费无限次
      3. 回退到默认 LLM client
    """
    global _vision_client
    if _vision_client is not None:
        return _vision_client

    with _vision_lock:
        if _vision_client is not None:
            return _vision_client

        # 1. 优先 Gemini (免费额度最大)
        gemini_key = os.environ.get("GEMINI_API_KEY", "")
        if gemini_key:
            log.info("AI精筛: 使用 Google Gemini (免费)")
            _vision_client = LLMClient(LLMConfig(
                provider="gemini",
                api_key=gemini_key,
                timeout_sec=15.0,
                max_retries=2,
                cache_enabled=True,
            ))
            return _vision_client

        # 2. 检测 Ollama 是否在运行
        try:
            probe = httpx.get("http://localhost:11434/api/tags", timeout=3)
            if probe.status_code == 200:
                models = [m["name"] for m in probe.json().get("models", [])]
                vision_models = [m for m in models
                                 if any(v in m for v in ("llava", "moondream",
                                                         "minicpm", "bakllava"))]
                if vision_models:
                    chosen = vision_models[0]
                    log.info("AI精筛: 使用 Ollama 本地模型 %s (免费)", chosen)
                    _vision_client = LLMClient(LLMConfig(
                        provider="ollama",
                        vision_model=chosen,
                        model=chosen,
                        timeout_sec=30.0,
                        max_retries=1,
                        cache_enabled=True,
                    ))
                    return _vision_client
                else:
                    log.info("Ollama 运行中但无 vision 模型, 可运行: ollama pull llava:7b")
        except Exception:
            pass

        # 3. 回退: 用默认 client (可能收费)
        log.info("AI精筛: 无免费 provider, 回退到默认 LLM")
        return None
