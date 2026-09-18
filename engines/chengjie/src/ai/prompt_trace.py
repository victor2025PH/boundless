# -*- coding: utf-8 -*-
"""最终装配 prompt 的环形留痕 + DeepSeek 前缀缓存命中统计（2026-09-11）。

为什么要有它：
  · 「聊天没人设、没记忆」查了一天靠的是 `[ai] prompt_tokens=… after=20` 这一行日志倒推——
    模型实际收到什么从来没人能直接看。本模块把主链**真正发出去**的 messages（预算裁剪之后）
    连同响应 usage 留在进程内环形缓冲里，`GET /api/ai/prompt-inspect` 直接看。
  · DeepSeek V4.1-Flash 缓存命中 ¥0.04/M vs 未命中 ¥2/M（50 倍差）。不量命中率就没法谈
    「稳定前缀布局」值不值——usage.prompt_cache_hit_tokens 每次响应都带，只是没人记。

正文绝不落盘：留痕里有客户原话与人设全文，只给 admin 口读，重启即清。
**摘要**（模型/host/耗时/usage/路由快照，不含任何文本）另存一份环形 JSON 到
``$AITR_DATA_DIR/config/prompt_trace_summary.json``（2026-09-12），让 composer「模型」面板的
「上一条回复用了谁」跨重启仍能回答；没设 AITR_DATA_DIR（测试 / 裸跑）就纯内存。
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

MAX_ENTRIES = 200
MAX_TEXT_CHARS = 64_000      # 单条留痕存文本上限（超长系统提示也只留头）
SUMMARY_FILE = os.path.join("config", "prompt_trace_summary.json")
_SUMMARY_FLUSH_DELAY_SEC = 3.0
# 摘要不带的键（正文 / 可还原正文的东西）
_TEXT_KEYS = ("system", "messages", "system_sections")

_lock = threading.Lock()
_entries: Deque[Dict[str, Any]] = deque(maxlen=MAX_ENTRIES)
_seq = 0
_flush_timer: Optional[threading.Timer] = None
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


def _public_host(host: str, model: str) -> str:
    try:
        from src.ai.conv_route import public_host_for
        return public_host_for(host, model)
    except Exception:
        return ""


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
           purpose: str = "", ok: bool = True,
           route: Optional[Dict[str, Any]] = None) -> int:
    """留一条；返回序号。绝不抛。``route``＝会话级模型路由快照（conv_route.Route.as_dict），
    无限制会话在 inspect 里一眼看出「这条走的是 173、思考开没开、深度几档」。"""
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
            "model": model, "host": host,
            "public_host": _public_host(host, model),
            "purpose": purpose, "ok": bool(ok),
            "latency_ms": int(latency_ms or 0),
            "system_chars": len(sys_txt), "history_msgs": max(0, len(hist) - 1),
            "system_sections": sections[:60],
            "usage": uf, "budget": dict(budget_stats or {}),
            "system": _clip(sys_txt), "messages": hist,
        }
        if isinstance(route, dict) and route:
            entry["route"] = dict(route)
        with _lock:
            _seq += 1
            entry["seq"] = _seq
            _entries.append(entry)
            if ok and uf["prompt_tokens"]:
                _stats["calls"] += 1
                for k in ("prompt_tokens", "cache_hit_tokens", "cache_miss_tokens",
                          "completion_tokens", "reasoning_tokens"):
                    _stats[k] += uf[k]
            seq = _seq
        try:  # 按模型档分桶（conv_route stats「谁在花哪家的钱」）
            from src.ai import conv_route as _cr
            _cr.record_reply(entry.get("route"), ok=bool(ok), latency_ms=int(latency_ms or 0))
        except Exception:
            pass
        _schedule_flush()
        return seq
    except Exception:
        return 0


# ── 摘要环落盘（无正文）───────────────────────────────────────────────────────

def summary_path() -> Optional[Path]:
    """``$AITR_DATA_DIR/config/prompt_trace_summary.json``；未设数据根 → None（纯内存）。"""
    root = os.environ.get("AITR_DATA_DIR", "").strip()
    if not root:
        return None
    return Path(root) / SUMMARY_FILE


def _summary_of(e: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in e.items() if k not in _TEXT_KEYS}


def _schedule_flush() -> None:
    global _flush_timer
    if summary_path() is None:
        return
    with _lock:
        if _flush_timer is not None:
            return
        t = threading.Timer(_SUMMARY_FLUSH_DELAY_SEC, flush_summaries)
        t.daemon = True
        _flush_timer = t
    t.start()


def flush_summaries() -> bool:
    """把当前环的摘要写盘（原子替换）。绝不抛；返回是否写成功。"""
    global _flush_timer
    p = summary_path()
    with _lock:
        if _flush_timer is not None and _flush_timer is not threading.current_thread():
            try:
                _flush_timer.cancel()
            except Exception:
                pass
        _flush_timer = None
        items = [_summary_of(e) for e in _entries]
        seq = _seq
    if p is None:
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"v": 1, "seq": seq, "items": items},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def load_summaries() -> int:
    """启动时把上次的摘要环装回来（标 ``restored``，正文为空）。返回装回条数。绝不抛。"""
    global _seq
    p = summary_path()
    if p is None or not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return 0
        n = 0
        with _lock:
            if _entries:
                return 0  # 进程内已有新留痕，不覆盖
            for it in items[-MAX_ENTRIES:]:
                if not isinstance(it, dict) or not it.get("model"):
                    continue
                e = dict(it)
                e["restored"] = True
                e.setdefault("system", "")
                e.setdefault("messages", [])
                e.setdefault("system_sections", [])
                _entries.append(e)
                n += 1
            _seq = max(_seq, int(data.get("seq") or 0),
                       max((int(e.get("seq") or 0) for e in _entries), default=0))
        return n
    except Exception:
        return 0


load_summaries()


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
    global _seq, _flush_timer
    with _lock:
        if _flush_timer is not None:
            try:
                _flush_timer.cancel()
            except Exception:
                pass
            _flush_timer = None
        _entries.clear()
        _seq = 0
        for k in ("calls", "prompt_tokens", "cache_hit_tokens", "cache_miss_tokens",
                  "completion_tokens", "reasoning_tokens"):
            _stats[k] = 0
        _stats["since_ts"] = time.time()
