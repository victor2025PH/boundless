# -*- coding: utf-8 -*-
"""最终装配 prompt 的环形留痕 + DeepSeek 前缀缓存命中统计（2026-09-11）。

为什么要有它：
  · 「聊天没人设、没记忆」查了一天靠的是 `[ai] prompt_tokens=… after=20` 这一行日志倒推——
    模型实际收到什么从来没人能直接看。本模块把主链**真正发出去**的 messages（预算裁剪之后）
    连同响应 usage 留在进程内环形缓冲里，`GET /api/ai/prompt-inspect` 直接看。
  · DeepSeek V4.1-Flash 缓存命中 ¥0.04/M vs 未命中 ¥2/M（50 倍差）。不量命中率就没法谈
    「稳定前缀布局」值不值——usage.prompt_cache_hit_tokens 每次响应都带，只是没人记。

纯进程内、绝不抛、绝不落盘：留痕里有客户原话与人设全文，只给 admin 口读，重启即清。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

MAX_ENTRIES = 200
MAX_TEXT_CHARS = 64_000      # 单条留痕存文本上限（超大档 900k token 的系统提示也只留头）

_lock = threading.Lock()
_entries: Deque[Dict[str, Any]] = deque(maxlen=MAX_ENTRIES)
_seq = 0
# 滚动缓存统计（进程生命周期）
_stats: Dict[str, Any] = {
    "calls": 0, "prompt_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0,
    "completion_tokens": 0, "reasoning_tokens": 0, "since_ts": time.time(),
}


def _clip(s: Any, n: int = MAX_TEXT_CHARS) -> str:
    t = str(s or "")
    if len(t) <= n:
        return t
    return t[:n] + f"\n…[已截断，原长 {len(t)} 字符]"


def usage_fields(usage: Any) -> Dict[str, int]:
    """从 OpenAI SDK usage 对象 / dict 抽 tokens（含 DeepSeek 缓存字段与推理 token）。绝不抛。"""
    out = {"prompt_tokens": 0, "completion_tokens": 0,
           "cache_hit_tokens": 0, "cache_miss_tokens": 0, "reasoning_tokens": 0}
    if usage is None:
        return out

    def _g(obj: Any, name: str) -> Any:
        if isinstance(obj, dict):
            return obj.get(name)
        return getattr(obj, name, None)

    try:
        out["prompt_tokens"] = int(_g(usage, "prompt_tokens") or 0)
        out["completion_tokens"] = int(_g(usage, "completion_tokens") or 0)
        hit = _g(usage, "prompt_cache_hit_tokens")
        if hit is None:  # OpenAI 口径：prompt_tokens_details.cached_tokens
            det = _g(usage, "prompt_tokens_details")
            hit = _g(det, "cached_tokens") if det is not None else None
        if hit is None:  # SDK 把未知字段放 model_extra
            extra = _g(usage, "model_extra") or {}
            hit = extra.get("prompt_cache_hit_tokens") if isinstance(extra, dict) else None
        out["cache_hit_tokens"] = int(hit or 0)
        miss = _g(usage, "prompt_cache_miss_tokens")
        if miss is None:
            extra = _g(usage, "model_extra") or {}
            miss = extra.get("prompt_cache_miss_tokens") if isinstance(extra, dict) else None
        out["cache_miss_tokens"] = int(miss if miss is not None
                                       else max(0, out["prompt_tokens"] - out["cache_hit_tokens"]))
        det_c = _g(usage, "completion_tokens_details")
        out["reasoning_tokens"] = int((_g(det_c, "reasoning_tokens") if det_c is not None else 0) or 0)
    except Exception:
        pass
    return out


def record(*, messages: List[Dict[str, Any]], model: str, host: str = "",
           conv: str = "", request_id: str = "", usage: Any = None,
           budget_stats: Optional[Dict[str, Any]] = None, latency_ms: int = 0,
           purpose: str = "", ok: bool = True) -> int:
    """留一条；返回序号。绝不抛。"""
    global _seq
    try:
        uf = usage_fields(usage)
        sys_txt = ""
        hist: List[Dict[str, str]] = []
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            content = m.get("content")
            if isinstance(content, list):  # 多模态：只留文本片
                content = "\n".join(str(p.get("text") or "") for p in content
                                    if isinstance(p, dict))
            if role == "system" and not sys_txt:
                sys_txt = str(content or "")
            else:
                hist.append({"role": role, "content": _clip(content, 4000)})
        sections = [ln.strip().split("】")[0] + "】" for ln in sys_txt.split("\n")
                    if ln.strip().startswith("【") and "】" in ln]
        entry = {
            "ts": time.time(), "conv": conv or "", "request_id": request_id or "",
            "model": model, "host": host, "purpose": purpose, "ok": bool(ok),
            "latency_ms": int(latency_ms or 0),
            "system_chars": len(sys_txt), "history_msgs": max(0, len(hist) - 1),
            "system_sections": sections[:60],
            "usage": uf, "budget": dict(budget_stats or {}),
            "system": _clip(sys_txt), "messages": hist,
        }
        with _lock:
            _seq += 1
            entry["seq"] = _seq
            _entries.append(entry)
            if ok and uf["prompt_tokens"]:
                _stats["calls"] += 1
                for k in ("prompt_tokens", "cache_hit_tokens", "cache_miss_tokens",
                          "completion_tokens", "reasoning_tokens"):
                    _stats[k] += uf[k]
            return _seq
    except Exception:
        return 0


def cache_stats() -> Dict[str, Any]:
    with _lock:
        s = dict(_stats)
    pt = s.get("prompt_tokens") or 0
    s["hit_ratio"] = round(s["cache_hit_tokens"] / pt, 4) if pt else 0.0
    return s


def list_entries(conv: str = "", limit: int = 20) -> List[Dict[str, Any]]:
    """摘要列表（不含正文），新的在前。"""
    with _lock:
        items = list(_entries)
    if conv:
        items = [e for e in items if conv in str(e.get("conv") or "")]
    items = items[-max(1, int(limit or 20)):]
    out = []
    for e in reversed(items):
        d = {k: v for k, v in e.items() if k not in ("system", "messages")}
        out.append(d)
    return out


def get_entry(seq: int) -> Optional[Dict[str, Any]]:
    with _lock:
        for e in _entries:
            if e.get("seq") == seq:
                return dict(e)
    return None


def reset() -> None:
    """测试用。"""
    global _seq
    with _lock:
        _entries.clear()
        _seq = 0
        for k in ("calls", "prompt_tokens", "cache_hit_tokens", "cache_miss_tokens",
                  "completion_tokens", "reasoning_tokens"):
            _stats[k] = 0
        _stats["since_ts"] = time.time()
