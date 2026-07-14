# -*- coding: utf-8 -*-
"""LLM Router —— 主控集中式 ollama + 云端 fallback 的统一调用层。

背景
----
200 设备生产架构里, 主控 (192.168.0.118) 集中部署 ollama, 各 worker
(W03 / W175 ...) 通过 HTTP 调主控的 /api/chat。集中部署的好处是只需一张
GPU 卡和一份模型权重, 但单点故障风险大 —— 一旦主控 ollama 挂了 / 模型
崩了 / 网络抖动, 全部 worker 的 LLM 调用同时失败。

本模块提供一个统一的 ``llm_complete()`` API, 内部用 **circuit breaker**
模式管理 primary (ollama) → fallback (cloud) 的自动切换:

* 连续 ``failure_threshold`` 次失败 → 打开熔断器, 后续调用直接走云端
* 熔断打开后, 每 ``recovery_check_interval`` 秒做一次轻量 ping 探测 ollama
* 探测成功则关闭熔断器, 流量切回 primary
* 全过程 thread-safe (threading.Lock 保护状态)

API
---
::

    from src.host.llm_router import llm_complete
    res = llm_complete(
        prompt="你好",
        task="ai_greeting",     # 用于查 per_task_override
        system="...",           # optional
        max_tokens=512,
        temperature=0.7,
        images=[...],           # optional, vision-language model
    )
    # res = {"text": "...", "backend": "ollama_central"|"cloud",
    #        "model": "...", "latency_ms": int, "ok": bool, "error": str}

不引入新依赖
------------
``anthropic`` SDK 没在 requirements.txt 里, 因此 cloud backend 直接用
``urllib.request`` 调 ``https://api.anthropic.com/v1/messages``,
header 含 ``x-api-key`` + ``anthropic-version: 2023-06-01``。
后续若需 openai / qwen-cloud, 在 ``CloudBackend`` 子类里加 ``provider``
分支即可。

不做的事
--------
* 不改现有调用点 (ChatBrain / fb_profile_classifier / ollama_vlm) —— 那些保留
  各自的本地 ollama 路径, 后续 sibling A 接入时再迁。
* 不实现流式 (stream=True) —— 项目里 LLM 调用都是一次性 prompt → text,
  目前没有流式需求。
* 不做请求重试 —— 重试由 circuit breaker 触发的 fallback 实现, 不在单
  backend 内部 retry (避免和 timeout 互相放大)。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# 配置加载
# ════════════════════════════════════════════════════════════════════
_DEFAULT_CONFIG: Dict[str, Any] = {
    "primary": {
        "backend": "ollama_central",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "timeout": 30,
        "health_check_interval": 60,
    },
    "fallback": {
        "enabled": True,
        "backend": "cloud",
        "provider": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-haiku-4-5-20251001",
        "base_url": "https://api.anthropic.com",
        "timeout": 30,
    },
    "routing_policy": {
        "health_strategy": "circuit_breaker",
        "failure_threshold": 3,
        "recovery_check_interval": 120,
    },
    "per_task_override": {},
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_config_path() -> Path:
    return _project_root() / "config" / "llm_routing.yaml"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """从 YAML 加载配置, 缺失/解析失败用内置默认。

    顶层 key 兼容: 若文件根有 ``llm:`` 包裹则取 ``data["llm"]``, 否则视作扁平。
    """
    cfg_path = Path(path) if path else _default_config_path()
    data: Dict[str, Any] = {}
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            data = raw.get("llm", raw) if isinstance(raw, dict) else {}
        except Exception as e:
            logger.warning("[llm_router] 读取 %s 失败, 用默认配置: %s", cfg_path, e)
            data = {}
    else:
        logger.info("[llm_router] %s 不存在, 用默认配置 (本地 ollama)", cfg_path)

    # 深合并默认
    merged: Dict[str, Any] = {}
    for k, v in _DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            merged[k] = {**v, **(data.get(k) or {})}
        else:
            merged[k] = data.get(k, v)
    # per_task_override 是嵌套 dict, 直接取用户定义 (不与默认合并 — 默认就是 {})
    merged["per_task_override"] = dict(data.get("per_task_override") or {})
    return merged


# ════════════════════════════════════════════════════════════════════
# Backend 抽象
# ════════════════════════════════════════════════════════════════════
class BackendError(Exception):
    """所有 backend 失败统一抛此异常, 便于 router 捕获走 fallback。"""


class _BaseBackend:
    name: str = "base"

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        images: Optional[List[str]] = None,
        timeout: float = 30.0,
    ) -> str:
        raise NotImplementedError

    def ping(self, timeout: float = 3.0) -> bool:
        """轻量探活, 用于 circuit breaker recovery 探测。"""
        raise NotImplementedError


class OllamaBackend(_BaseBackend):
    """主控 ollama HTTP backend, 调 /api/chat (现代 ollama API)。"""

    name = "ollama_central"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        images: Optional[List[str]] = None,
        timeout: float = 30.0,
    ) -> str:
        messages: List[Dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_msg: Dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            # ollama /api/chat 接受 base64 字符串列表 (无 data: 前缀)
            user_msg["images"] = list(images)
        messages.append(user_msg)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
        }
        url = f"{self.base_url}/api/chat"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            raise BackendError(f"ollama_url_error:{e}") from e
        except Exception as e:
            raise BackendError(f"ollama_exc:{type(e).__name__}:{e}") from e
        try:
            data = json.loads(raw)
        except Exception as e:
            raise BackendError(f"ollama_parse:{e}") from e
        msg = data.get("message") or {}
        text = msg.get("content") or data.get("response") or ""
        if not isinstance(text, str):
            text = str(text)
        return text

    def ping(self, timeout: float = 3.0) -> bool:
        url = f"{self.base_url}/api/tags"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False


class AnthropicBackend(_BaseBackend):
    """Anthropic Messages API raw HTTP backend (无 SDK 依赖)。"""

    name = "cloud"

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com"):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    def complete(
        self,
        prompt: str,
        *,
        model: str,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        images: Optional[List[str]] = None,
        timeout: float = 30.0,
    ) -> str:
        if not self.api_key:
            raise BackendError("anthropic_no_api_key")
        # content blocks: 多 image 用 source.type=base64
        content: List[Dict[str, Any]] = []
        if images:
            for img_b64 in images:
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_b64,
                    },
                })
        content.append({"type": "text", "text": prompt})
        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            payload["system"] = system
        url = f"{self.base_url}/v1/messages"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise BackendError(f"anthropic_http_{e.code}:{err_body}") from e
        except urllib.error.URLError as e:
            raise BackendError(f"anthropic_url_error:{e}") from e
        except Exception as e:
            raise BackendError(f"anthropic_exc:{type(e).__name__}:{e}") from e
        try:
            data = json.loads(raw)
        except Exception as e:
            raise BackendError(f"anthropic_parse:{e}") from e
        # content: [{"type":"text","text":"..."}]
        parts = data.get("content") or []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
        return "".join(texts)

    def ping(self, timeout: float = 3.0) -> bool:
        # 云端不在 ping 里实际请求 (不是探测目标), 默认返回 True 表示 "永远可用"
        return True


def make_cloud_backend(fb_cfg: Dict[str, Any]) -> _BaseBackend:
    """根据 fallback 配置构造 cloud backend (预留 openai / qwen-cloud 扩展)。"""
    provider = (fb_cfg.get("provider") or "anthropic").lower()
    api_key_env = fb_cfg.get("api_key_env") or "ANTHROPIC_API_KEY"
    api_key = os.environ.get(api_key_env, "") or ""
    base_url = fb_cfg.get("base_url") or "https://api.anthropic.com"
    if provider == "anthropic":
        return AnthropicBackend(api_key=api_key, base_url=base_url)
    # 预留: openai / qwen-cloud
    raise BackendError(f"unsupported_cloud_provider:{provider}")


# ════════════════════════════════════════════════════════════════════
# Circuit Breaker 状态机
# ════════════════════════════════════════════════════════════════════
# 状态:
#   CLOSED   - 正常, 走 primary
#   OPEN     - 熔断, 走 fallback; 每 recovery_check_interval 秒 ping 探测
#   (无 HALF_OPEN — 因为我们的 ping 是被动空闲探测, 而不是用真实流量探活,
#    简化 closed↔open 二态机, 状态转换更清晰, 避免 half-open 抖动)
#
# 取舍说明:
#   - 不做 HALF_OPEN 真流量探测: 真流量探测意味着 OPEN 状态下要让若干请求
#     "试探性" 走 primary, 失败了再切回; 这会让上游看到不稳定的延迟/错误.
#     用空闲 ping 探活更稳, 唯一缺点是探测延迟 (recovery_check_interval 秒);
#     但本场景对秒级延迟不敏感 (LLM 一次推理本来就要 1~5s).
#   - failure 计数只在 CLOSED 状态累加, OPEN 状态下不再累加 (避免污染恢复
#     探测窗口).
class _CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"

    def __init__(self, failure_threshold: int = 3,
                 recovery_check_interval: float = 120.0):
        self.failure_threshold = max(1, int(failure_threshold))
        self.recovery_check_interval = float(recovery_check_interval)
        self._lock = threading.Lock()
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._last_recovery_check: float = 0.0
        # 统计
        self._open_count = 0
        self._recovery_count = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "state": self._state,
                "consecutive_failures": self._consecutive_failures,
                "opened_at": self._opened_at,
                "last_recovery_check": self._last_recovery_check,
                "open_count": self._open_count,
                "recovery_count": self._recovery_count,
            }

    def record_success(self) -> None:
        with self._lock:
            if self._state == self.CLOSED:
                self._consecutive_failures = 0

    def record_failure(self) -> bool:
        """记录一次 primary 失败, 返回是否新切到 OPEN。"""
        with self._lock:
            if self._state != self.CLOSED:
                return False
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                now = time.time()
                self._state = self.OPEN
                self._opened_at = now
                # 关键: 把 last_recovery_check 设成 opened_at, 这样首次探测
                # 也要等满 recovery_check_interval 秒, 避免熔断瞬间立刻被 ping
                # 关回 CLOSED 形成 "永远 CLOSED" 假象
                self._last_recovery_check = now
                self._open_count += 1
                logger.warning(
                    "[llm_router] circuit_breaker OPEN (consecutive_failures=%d)",
                    self._consecutive_failures,
                )
                return True
        return False

    def should_attempt_recovery(self) -> bool:
        """OPEN 状态下且距上次探测 ≥ recovery_check_interval 秒时返回 True。"""
        now = time.time()
        with self._lock:
            if self._state != self.OPEN:
                return False
            if now - self._last_recovery_check < self.recovery_check_interval:
                return False
            self._last_recovery_check = now
            return True

    def force_close(self) -> None:
        with self._lock:
            if self._state == self.OPEN:
                self._recovery_count += 1
                logger.info(
                    "[llm_router] circuit_breaker CLOSED (recovery #%d)",
                    self._recovery_count,
                )
            self._state = self.CLOSED
            self._consecutive_failures = 0
            self._opened_at = 0.0

    def force_open(self) -> None:
        """测试用: 强制熔断。"""
        with self._lock:
            if self._state != self.OPEN:
                self._state = self.OPEN
                self._opened_at = time.time()
                self._open_count += 1


# ════════════════════════════════════════════════════════════════════
# Router 单例
# ════════════════════════════════════════════════════════════════════
class LLMRouter:
    """无状态调用层 + circuit breaker 状态。

    生产里通常一个进程一份 (通过 ``get_router()`` 拿单例), 测试里可
    自己 new 一份, 注入 mock backend。
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        primary_backend: Optional[_BaseBackend] = None,
        fallback_backend: Optional[_BaseBackend] = None,
    ):
        self.config = config if config is not None else load_config()
        primary_cfg = self.config.get("primary") or {}
        fb_cfg = self.config.get("fallback") or {}
        policy = self.config.get("routing_policy") or {}

        self.primary = primary_backend or OllamaBackend(
            base_url=primary_cfg.get("base_url") or "http://127.0.0.1:11434",
        )
        self.fallback_enabled = bool(fb_cfg.get("enabled", True))
        if fallback_backend is not None:
            self.fallback: Optional[_BaseBackend] = fallback_backend
        elif self.fallback_enabled:
            try:
                self.fallback = make_cloud_backend(fb_cfg)
            except BackendError as e:
                logger.warning("[llm_router] fallback 初始化失败: %s", e)
                self.fallback = None
                self.fallback_enabled = False
        else:
            self.fallback = None

        self.breaker = _CircuitBreaker(
            failure_threshold=int(policy.get("failure_threshold", 3)),
            recovery_check_interval=float(policy.get("recovery_check_interval", 120)),
        )

    # -- 模型解析 --------------------------------------------------------
    def _resolve_models(self, task: Optional[str]) -> Dict[str, str]:
        primary_cfg = self.config.get("primary") or {}
        fb_cfg = self.config.get("fallback") or {}
        primary_model = primary_cfg.get("model") or "qwen2.5:7b"
        fallback_model = fb_cfg.get("model") or "claude-haiku-4-5-20251001"
        if task:
            override = (self.config.get("per_task_override") or {}).get(task) or {}
            primary_model = override.get("primary_model") or primary_model
            fallback_model = override.get("fallback_model") or fallback_model
        return {"primary": primary_model, "fallback": fallback_model}

    # -- 主入口 ----------------------------------------------------------
    def complete(
        self,
        prompt: str,
        *,
        task: Optional[str] = None,
        system: Optional[str] = None,
        max_tokens: int = 512,
        temperature: float = 0.7,
        images: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        models = self._resolve_models(task)
        primary_cfg = self.config.get("primary") or {}
        fb_cfg = self.config.get("fallback") or {}
        primary_timeout = float(primary_cfg.get("timeout", 30))
        fb_timeout = float(fb_cfg.get("timeout", 30))

        # 1) 若已熔断, 看是否到了恢复探测窗口
        if self.breaker.state == _CircuitBreaker.OPEN:
            if self.breaker.should_attempt_recovery():
                if self.primary.ping(timeout=3.0):
                    self.breaker.force_close()
                    logger.info("[llm_router] ollama recovered, 切回 primary")

        # 2) 走 primary or fallback
        last_err = ""
        if self.breaker.state == _CircuitBreaker.CLOSED:
            t0 = time.time()
            try:
                text = self.primary.complete(
                    prompt,
                    model=models["primary"],
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    images=images,
                    timeout=primary_timeout,
                )
                self.breaker.record_success()
                return {
                    "ok": True,
                    "text": text,
                    "backend": self.primary.name,
                    "model": models["primary"],
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": "",
                }
            except BackendError as e:
                last_err = str(e)
                logger.warning("[llm_router] primary 失败: %s", e)
                self.breaker.record_failure()
            except Exception as e:
                last_err = f"primary_exc:{type(e).__name__}:{e}"
                logger.warning("[llm_router] primary 未捕获异常: %s", e)
                self.breaker.record_failure()

        # 3) primary 失败或熔断 → fallback
        if self.fallback_enabled and self.fallback is not None:
            t0 = time.time()
            try:
                text = self.fallback.complete(
                    prompt,
                    model=models["fallback"],
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    images=images,
                    timeout=fb_timeout,
                )
                return {
                    "ok": True,
                    "text": text,
                    "backend": self.fallback.name,
                    "model": models["fallback"],
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": "",
                }
            except BackendError as e:
                logger.warning("[llm_router] fallback 也失败: %s", e)
                last_err = f"primary={last_err};fallback={e}"
            except Exception as e:
                logger.warning("[llm_router] fallback 未捕获异常: %s", e)
                last_err = f"primary={last_err};fallback_exc={type(e).__name__}:{e}"

        # 4) 全失败
        return {
            "ok": False,
            "text": "",
            "backend": "",
            "model": models["primary"],
            "latency_ms": 0,
            "error": last_err or "all_backends_failed",
        }


# ════════════════════════════════════════════════════════════════════
# 模块级单例 + 公开 API
# ════════════════════════════════════════════════════════════════════
_router_lock = threading.Lock()
_router_singleton: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    """进程级单例 (线程安全)。测试可调 ``reset_router()`` 清空。"""
    global _router_singleton
    if _router_singleton is None:
        with _router_lock:
            if _router_singleton is None:
                _router_singleton = LLMRouter()
    return _router_singleton


def reset_router() -> None:
    """测试钩子: 清空单例, 让下次 get_router() 重新构造。"""
    global _router_singleton
    with _router_lock:
        _router_singleton = None


def llm_complete(
    prompt: str,
    *,
    task: Optional[str] = None,
    system: Optional[str] = None,
    max_tokens: int = 512,
    temperature: float = 0.7,
    images: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """统一调用入口 —— 详见模块 docstring。

    返回 dict 字段::
        ok           : bool        # 是否拿到非空结果
        text         : str         # LLM 输出文本 (失败时为 "")
        backend      : str         # "ollama_central" / "cloud" / ""(全失败)
        model        : str         # 实际使用的模型名
        latency_ms   : int         # 单次后端调用延迟
        error        : str         # 失败时的错误描述, 成功为 ""
    """
    return get_router().complete(
        prompt,
        task=task,
        system=system,
        max_tokens=max_tokens,
        temperature=temperature,
        images=images,
    )
