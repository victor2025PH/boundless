"""KB 未命中路由（智语 2026-10-08）：回复兜底用新命中口径，未命中进学习池并生成待审草稿。

背景：``kb_gate.judge_kb_hit`` 把「真命中」从「kb_ctx 非空」里分出来——全局规则 / 示例
撑起来的非空上下文、只有一字重叠的首条都不算命中。此前该口径只进统计；本模块让回复链的
兜底（must 档固定话术 / 连续查无转人工 / 直出）和学习漏斗也按它走：

- ``kb_hit_for_reply(text, result, config)``：读 ``knowledge_base.hit_min_vec_sim``，返回 (hit, why)。
- ``record_kb_miss(...)``：未命中时
  1. 记入 ``kb_miss_log``（问题样式守门；must 档已由 ``_kb_after_search`` 记过的不重复记）；
  2. 后台生成一条 ``kb_drafts`` 待审草稿（复用 DailyLearner 的生成 / 同题合并 / 私事分流 / 查重）。
     - 只对事实类提问（``is_fact_question``），寒暄 / 占位 / 表情不生成；
     - 已有 pending / approved 同题草稿 → 不再调 LLM；
     - 进程内限速 ``knowledge_base.live_miss_drafts_per_hour``（默认 30，0 = 关）；
     - 学习器没有 AI 客户端 → 只留在池里，等定时学习收割（不丢素材）；
     - **不删** miss 记录：未命中统计 / 收件箱「未命中」chip 照常可见。
  绝不抛异常、绝不阻塞回复。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional, Tuple

_logger = logging.getLogger("ai_chat_assistant.kb_miss_route")

DEFAULT_DRAFTS_PER_HOUR = 30
LIVE_DRAFT_SOURCE = "kb_miss_live"

_lock = threading.Lock()
_recent: Deque[float] = deque()
_inflight: set = set()
_learners: Dict[str, Any] = {}
_tasks: set = set()


def _kb_cfg(config: Any) -> Dict[str, Any]:
    raw = getattr(config, "config", config)
    if not isinstance(raw, dict):
        return {}
    kb = raw.get("knowledge_base") or {}
    return kb if isinstance(kb, dict) else {}


def kb_hit_for_reply(text: str, result: Optional[Dict[str, Any]], config: Any = None) -> Tuple[bool, str]:
    """回复链用的命中判定（与统计同一口径）。"""
    from src.utils.kb_gate import DEFAULT_VEC_HIT_MIN_SIM, judge_kb_hit
    try:
        vmin = float(_kb_cfg(config).get("hit_min_vec_sim", DEFAULT_VEC_HIT_MIN_SIM))
    except (TypeError, ValueError):
        vmin = DEFAULT_VEC_HIT_MIN_SIM
    try:
        return judge_kb_hit(text, result or {}, vec_min_sim=vmin)
    except Exception:
        _logger.debug("judge_kb_hit 异常，按未命中处理", exc_info=True)
        return False, "error"


def _drafts_per_hour(config: Any) -> int:
    try:
        return max(0, int(_kb_cfg(config).get("live_miss_drafts_per_hour", DEFAULT_DRAFTS_PER_HOUR)))
    except (TypeError, ValueError):
        return DEFAULT_DRAFTS_PER_HOUR


def _take_slot(limit: int, now: Optional[float] = None) -> bool:
    if limit <= 0:
        return False
    now = time.time() if now is None else now
    with _lock:
        while _recent and now - _recent[0] > 3600:
            _recent.popleft()
        if len(_recent) >= limit:
            return False
        _recent.append(now)
        return True


def _learner_for(kb_store: Any, ai_client: Any = None):
    """复用进程内 DailyLearner 单例；没有则按 kb_store 的库路径惰性建一个（缓存）。"""
    from src.utils.daily_learner import DailyLearner, peek_daily_learner
    db = getattr(kb_store, "db_path", None) or getattr(kb_store, "_db_path", None)
    if db is None:
        return None
    ln = peek_daily_learner()
    if ln is not None and getattr(ln, "_kb", None) is kb_store:
        if ai_client is not None and not ln.ai_ready:
            ln.attach_ai(ai_client)
        return ln
    key = str(db)
    with _lock:
        ln = _learners.get(key)
    if ln is None:
        ln = DailyLearner(kb_store, ai_client=ai_client)
        with _lock:
            _learners[key] = ln
    elif ai_client is not None and not ln.ai_ready:
        ln.attach_ai(ai_client)
    return ln


def _has_open_draft(learner: Any, query: str) -> bool:
    from src.utils.daily_learner import normalize_query_key
    key = normalize_query_key(query)
    if not key:
        return True
    try:
        with learner._conn() as c:
            rows = c.execute(
                "SELECT query FROM kb_drafts WHERE status IN ('pending','approved')").fetchall()
    except Exception:
        return False
    return any(normalize_query_key(r[0]) == key for r in rows)


async def generate_live_draft(learner: Any, query: str, *, source_ref: str = "",
                              domain_context: str = "") -> Dict[str, Any]:
    """为一条未命中提问生成待审草稿（同题已有则跳过）。返回结果摘要。"""
    q = str(query or "").strip()[:200]
    if _has_open_draft(learner, q):
        return {"generated": 0, "reason": "duplicate"}
    if not getattr(learner, "ai_ready", False):
        return {"generated": 0, "reason": "ai_unavailable"}
    drafts = await learner.generate_drafts(
        [{"source": LIVE_DRAFT_SOURCE, "query": q, "count": 1, "source_ref": source_ref}],
        domain_context)
    saved = learner.save_drafts(drafts) if drafts else 0
    return {"generated": int(saved), "reason": "ok" if saved else "empty"}


def _spawn(coro) -> bool:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return False
    t = loop.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return True


def record_kb_miss(kb_store: Any, text: str, *, conversation_id: str = "", lang: str = "",
                   reason: str = "", already_logged: bool = False, config: Any = None,
                   ai_client: Any = None, domain_context: str = "") -> Dict[str, Any]:
    """KB 未命中的统一落点：进 miss_log + 后台生成待审草稿。永不抛。"""
    out: Dict[str, Any] = {"logged": False, "draft": "skipped", "reason": reason or ""}
    try:
        from src.utils.daily_learner import is_fact_question
        from src.utils.kb_gate import should_log_kb_miss
        q = str(text or "").strip()[:200]
        if not q or kb_store is None:
            out["draft"] = "no_text"
            return out
        if already_logged:
            out["logged"] = "by_policy"
        elif should_log_kb_miss(q) and hasattr(kb_store, "log_miss"):
            kb_store.log_miss(q)
            out["logged"] = True
        if not (already_logged or out["logged"]) or not is_fact_question(q):
            out["draft"] = "not_a_question"
            return out
        learner = _learner_for(kb_store, ai_client)
        if learner is None:
            out["draft"] = "no_learner"
            return out
        if not learner.ai_ready:
            out["draft"] = "ai_unavailable"
            return out
        from src.utils.daily_learner import normalize_query_key
        key = normalize_query_key(q)
        with _lock:
            if key in _inflight:
                out["draft"] = "inflight"
                return out
        if not _take_slot(_drafts_per_hour(config)):
            out["draft"] = "rate_limited"
            return out
        ref = f"conv:{conversation_id}"[:120] if conversation_id else ""

        async def _run():
            with _lock:
                _inflight.add(key)
            try:
                res = await generate_live_draft(learner, q, source_ref=ref,
                                                domain_context=domain_context)
                _logger.info("[kb_miss] 待审草稿 %s（lang=%s）", res.get("reason"), lang or "-")
                return res
            except Exception:
                _logger.debug("[kb_miss] 草稿生成失败（忽略）", exc_info=True)
                return {"generated": 0, "reason": "error"}
            finally:
                with _lock:
                    _inflight.discard(key)

        out["draft"] = "scheduled" if _spawn(_run()) else "no_loop"
    except Exception:
        _logger.debug("[kb_miss] record_kb_miss 异常（忽略）", exc_info=True)
        out["draft"] = "error"
    return out


def _reset_for_tests() -> None:
    with _lock:
        _recent.clear()
        _inflight.clear()
        _learners.clear()


async def _drain_for_tests() -> None:
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)
