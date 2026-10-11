"""知识库后台向量化队列（2026-10-08 智语 · 导入即向量化）。

173 实例 22 条启用只向量化了 1 条：21 条是批量导入的，导入接口不走「保存即向量化」，
事后也没人点「向量化」。本模块补上这条流程：

- ``request(reason)``：批量导入 / 单条或批量启用 / 新增后调用，后台把「启用且无向量」的条目
  全部向量化（复用 ``kb_embed_job.embed_pending_entries``，批失败逐条重试）。并发请求合并：
  任务在跑时再来请求只记一个「跑完再来一轮」。
- 失败自动重试：本轮仍有失败 → 按 ``RETRY_DELAYS``（30s / 2min / 10min）退避重试，
  用完仍失败就停下，状态记为 ``failed``，等人在健康页点「重试」（``retry_now``）或下次导入触发。
- 状态持久化到 ``kb_meta``（键 ``embed_job_state``，JSON；**不含条目正文 / 密钥**），
  重启后健康页仍能看到上次结果；``health(kb)`` 给健康页：覆盖率 + 黄 / 绿灯 + 最近一次任务。

无事件循环（同步测试 / 脚本）时 ``request`` 不调度，返回 False；端点未配置时状态为
``unconfigured``（健康页同样亮黄灯并说明原因）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

from src.utils.kb_embed_job import EmbedFn, embed_pending_entries

logger = logging.getLogger(__name__)

STATE_KEY = "embed_job_state"
RETRY_DELAYS = (30.0, 120.0, 600.0)
_MAX_FAILED_IDS = 20


def _load_state(kb: Any) -> Dict[str, Any]:
    try:
        raw = kb.get_meta(STATE_KEY)
        v = json.loads(raw) if raw else {}
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _save_state(kb: Any, st: Dict[str, Any]) -> None:
    try:
        kb.set_meta(STATE_KEY, json.dumps(st, ensure_ascii=False))
    except Exception:
        logger.debug("[kb_embed_queue] 状态落库失败（忽略）", exc_info=True)


class KbEmbedQueue:
    def __init__(
        self,
        kb: Any,
        embed_fn: EmbedFn,
        *,
        enabled_fn: Callable[[], bool] = lambda: True,
        retry_delays=RETRY_DELAYS,
        sleep: Callable[[float], Any] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.kb = kb
        self.embed_fn = embed_fn
        self.enabled_fn = enabled_fn
        self.retry_delays = tuple(float(x) for x in retry_delays)
        self._sleep = sleep
        self._clock = clock
        self._task: Optional[asyncio.Task] = None
        self._again: Optional[str] = None

    # ── 状态 ──────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def state(self) -> Dict[str, Any]:
        st = _load_state(self.kb)
        st["running"] = self.running
        if not self._safe_enabled():
            st["status"] = "unconfigured"
        return st

    def _safe_enabled(self) -> bool:
        try:
            return bool(self.enabled_fn())
        except Exception:
            return False

    def _update(self, **kw: Any) -> Dict[str, Any]:
        st = _load_state(self.kb)
        st.update(kw)
        _save_state(self.kb, st)
        return st

    # ── 调度 ──────────────────────────────────────────────────────────────
    def request(self, reason: str) -> bool:
        """后台跑一轮（有待向量化条目才真跑）。返回是否已调度 / 合并。绝不抛。"""
        try:
            if not self._safe_enabled():
                self._update(status="unconfigured", last_reason=str(reason)[:40],
                             last_requested_at=self._clock())
                return False
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        except Exception:
            logger.debug("[kb_embed_queue] 调度失败（忽略）", exc_info=True)
            return False
        if self.running:
            self._again = str(reason)[:40]
            return True
        self._task = loop.create_task(self._runner(str(reason)[:40]))
        return True

    def retry_now(self, reason: str = "manual_retry") -> bool:
        return self.request(reason)

    async def run_once(self, reason: str) -> Dict[str, Any]:
        started = self._clock()
        self._update(status="running", last_reason=reason, last_started_at=started)
        try:
            st = await embed_pending_entries(self.kb, self.embed_fn)
            err = ""
        except Exception as e:  # embed_pending_entries 本身已吞端点异常，这里兜库错误
            logger.warning("[kb_embed_queue] 向量化任务异常: %s", type(e).__name__)
            st = {"pending": 0, "done": 0, "failed": 1, "failed_ids": []}
            err = type(e).__name__
        cov = {}
        try:
            cov = self.kb.embedding_coverage()
        except Exception:
            pass
        self._update(
            last_finished_at=self._clock(), last_done=int(st.get("done", 0)),
            last_failed=int(st.get("failed", 0)),
            failed_ids=list(st.get("failed_ids") or [])[:_MAX_FAILED_IDS],
            last_error=err or ("embedding_failed" if st.get("failed") else ""),
            coverage_pct=cov.get("pct"),
        )
        return st

    async def _runner(self, reason: str) -> None:
        try:
            while True:
                attempt = 0
                while True:
                    st = await self.run_once(reason if attempt == 0 else f"{reason}:retry{attempt}")
                    if not st.get("failed"):
                        self._update(status="ok", attempts=attempt + 1, next_retry_at=None)
                        break
                    if attempt >= len(self.retry_delays):
                        self._update(status="failed", attempts=attempt + 1, next_retry_at=None)
                        logger.warning("[kb_embed_queue] 向量化重试 %d 次仍有 %d 条失败，等待人工重试",
                                       attempt, st.get("failed", 0))
                        break
                    delay = self.retry_delays[attempt]
                    self._update(status="retrying", attempts=attempt + 1,
                                 next_retry_at=self._clock() + delay)
                    await self._sleep(delay)
                    attempt += 1
                if not self._again:
                    break
                reason, self._again = self._again, None
        except asyncio.CancelledError:
            self._update(status="cancelled")
            raise
        except Exception:
            logger.warning("[kb_embed_queue] 后台任务异常", exc_info=True)
            self._update(status="failed", last_error="runner_error")


def health(kb: Any, queue: Optional[KbEmbedQueue] = None, *, enabled: Optional[bool] = None) -> Dict[str, Any]:
    """健康页用：启用条目向量覆盖率 + 灯色（<100% 或端点未配 → yellow）+ 最近一次任务状态。"""
    try:
        cov = kb.embedding_coverage()
    except Exception:
        cov = {"total": 0, "done": 0, "pending": 0, "pct": 0}
    total, done = int(cov.get("total") or 0), int(cov.get("done") or 0)
    pct = round(done / total * 100, 1) if total else 100.0
    job = queue.state() if queue is not None else _load_state(kb)
    if enabled is None:
        enabled = job.get("status") != "unconfigured"
    if not enabled:
        job["status"] = "unconfigured"
    light = "green" if (total == 0 or done >= total) else "yellow"
    if not enabled and total and done < total:
        reason = "embedding_unconfigured"
    elif light == "yellow":
        reason = {"failed": "embedding_failed", "retrying": "embedding_retrying",
                  "running": "embedding_running"}.get(str(job.get("status")), "embedding_pending")
    else:
        reason = ""
    return {"enabled_total": total, "enabled_with_vector": done, "pending": max(0, total - done),
            "pct": pct, "light": light, "reason": reason, "job": job}
