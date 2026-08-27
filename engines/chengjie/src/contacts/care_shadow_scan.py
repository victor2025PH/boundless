"""P2 2026-08-01：LLM 抽取影子扫描器（正则 vs LLM 双跑对照，只记不动）。

架构（深想后放弃了「定时扫库」方案）：**与真实捕获共用同一入站事件源**——
``InboxStore.register_new_inbound_cb`` 再挂一个回调（与 ``care_capture`` 并列），
真实捕获与影子对照吃同一条数据流，对照天然同口径；回调内只做「配置闸 + 廉价门
（``time_like``）+ 入队」（亚毫秒，零阻塞 ingest），LLM 调用全部发生在本模块的
异步 drain 循环里（每 tick 限批 + 每日预算，成本恒有界）。

影子语义（安全不变量）：
- **绝不写 care_schedule、绝不发送**——产物只有对照 JSONL（``logs/care_shadow/*.jsonl``）
  与进程内计数（经 /api/care/health 的 ``shadow`` 段可观测）。
- 引擎总闸 ``proactive_care.enabled=false``（页面「暂停」）时影子一并静默——
  「暂停=全停」语义不打折。
- 进程重启丢队列（内存 deque）——影子是采样对照不是审计台账，可接受。

切主判据（下一阶段）：JSONL 里 ``llm_only``（LLM 抓到、正则漏掉）与 ``regex_only``
（正则抓到、LLM 漏掉）的人工复核正确率——不再拍脑袋，用生产语料说话。
周审 CLI：``python -m scripts.care_shadow_report``（读 JSONL 聚合，计数器随重启
清零、JSONL 才是持久口径）。

P4 2026-08-01 **真实捕获模式**（``llm_extract.enabled``，默认关）：影子对照之外，
``llm_only``（LLM 抓到且正则漏掉）的约定经 ``commitment_from_llm`` 确定性换算后
**写入 care_schedule 真实排程**。防双写三层：① 只收 llm_only——正则命中的消息在
ingest 时已由 care_capture 入过库，正则永远优先；② 同联系人同事件日已有 pending
→ 跳过（「一天一件事一条关怀」，面试 vs 终面 这类跨措辞重复从日历维度收口）；
③ store.add_commitment 自带同主题邻近去重兜底。dry 期切主流程＝影子数据复核达标
→ overlay 翻 ``enabled: true``（热生效，无需重启——前提是实例已装载本模式代码，
判据＝health.shadow 快照里有 ``captured`` 字段）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.contacts.care_commitment import extract_commitments
from src.contacts.care_extract_llm import (
    build_llm_extract_prompt, commitment_from_llm, parse_llm_extract, time_like,
)

logger = logging.getLogger(__name__)

CfgProvider = Callable[[], dict]

_QUEUE_MAX = 500
_PER_TICK_MAX = 30


class CareShadowStats:
    """进程级计数（风格对齐 avatar_voice_stats / bazi_stats：纯内存、dump 只读）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enqueued = 0          # 过门入队
        self.gate_rejected = 0     # 廉价门拒绝（未过 time_like）
        self.dropped = 0           # 队列满被丢（drop-oldest）
        self.drained = 0           # drain 消费总数
        self.llm_calls = 0
        self.llm_ok = 0            # 解析出合法 JSON（含 found=false）
        self.llm_err = 0           # 调用异常/坏输出
        self.llm_found = 0         # LLM 判有约定
        self.regex_found = 0       # 正则判有约定
        self.both_found = 0
        self.llm_only = 0          # LLM 有、正则无（召回增益候选）
        self.regex_only = 0        # 正则有、LLM 无（LLM 漏抓候选）
        self.budget_skipped = 0    # 预算耗尽被跳过
        # P4 真实捕获模式（llm_extract.enabled）
        self.captured = 0                # llm_only → 真实写入 care_schedule
        self.capture_skipped_pending = 0  # 同联系人同事件日已有 pending / store 去重
        self.capture_invalid = 0         # LLM 结果换算失败（过去事/坏日期）
        self._budget_day = ""      # 当日预算窗（本地日）
        self._budget_used = 0

    def budget_used_today(self, now: Optional[float] = None) -> int:
        day = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")
        with self._lock:
            if day != self._budget_day:
                self._budget_day = day
                self._budget_used = 0
            return self._budget_used

    def budget_consume(self, now: Optional[float] = None) -> None:
        day = datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d")
        with self._lock:
            if day != self._budget_day:
                self._budget_day = day
                self._budget_used = 0
            self._budget_used += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enqueued": self.enqueued,
                "gate_rejected": self.gate_rejected,
                "dropped": self.dropped,
                "drained": self.drained,
                "llm_calls": self.llm_calls,
                "llm_ok": self.llm_ok,
                "llm_err": self.llm_err,
                "llm_found": self.llm_found,
                "regex_found": self.regex_found,
                "both_found": self.both_found,
                "llm_only": self.llm_only,
                "regex_only": self.regex_only,
                "budget_skipped": self.budget_skipped,
                "captured": self.captured,
                "capture_skipped_pending": self.capture_skipped_pending,
                "capture_invalid": self.capture_invalid,
                "budget_used_today": self._budget_used,
            }


_stats_singleton: Optional[CareShadowStats] = None
_stats_lock = threading.Lock()


def get_care_shadow_stats() -> CareShadowStats:
    global _stats_singleton
    if _stats_singleton is None:
        with _stats_lock:
            if _stats_singleton is None:
                _stats_singleton = CareShadowStats()
    return _stats_singleton


def _shadow_cfg(cfg: dict) -> dict:
    return dict((cfg or {}).get("llm_extract") or {})


def shadow_active(cfg: dict) -> bool:
    """影子是否该跑：引擎总闸开 && (shadow 或 enabled)。纯函数（回调与 drain 同判据）。"""
    if not (cfg or {}).get("enabled", False):
        return False
    llm = _shadow_cfg(cfg)
    return bool(llm.get("shadow", False) or llm.get("enabled", False))


class CareShadowScanner:
    """入站回调（同步、亚毫秒）+ 异步 drain 循环（LLM 对照、限批限预算）。"""

    def __init__(
        self,
        *,
        ai_client: Any,
        cfg_provider: CfgProvider,
        log_dir,
        interval_sec: float = 300.0,
        queue_max: int = _QUEUE_MAX,
        per_tick_max: int = _PER_TICK_MAX,
        stats: Optional[CareShadowStats] = None,
        care_store: Any = None,
    ) -> None:
        self._ai = ai_client
        self._cfg = cfg_provider
        self._care_store = care_store   # P4：真实捕获模式的写侧（None=纯影子）
        self._log_dir = Path(log_dir)
        self._interval = max(60.0, float(interval_sec))
        self._queue_max = max(10, int(queue_max))
        self._per_tick = max(1, int(per_tick_max))
        self._q: deque = deque()
        self._q_lock = threading.Lock()
        self.stats = stats or get_care_shadow_stats()
        self.last_tick_ts: float = 0.0
        self._stop_evt: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None

    # ── 入站回调（挂 register_new_inbound_cb；ingest 线程内同步调用，必须极轻）──
    def inbound_cb(self, conv: Dict[str, Any], text: str) -> None:
        try:
            cfg = self._cfg() or {}
            if not shadow_active(cfg):
                return
            t = (text or "").strip()
            if not t:
                return
            # B68（实施67 P2-i）：群聊不进扫描队列（与正则捕获链 care_capture
            # 同口径）——关怀约定是私聊语义，报障群消息被 LLM 捕成「关怀约定」
            # 的实锤已两条（值守现场 cancel #6/#7）。bug_intake 私聊形态的排除
            # 在 care_capture（那里有全配置树）；本回调 cfg_provider 只给 care 段。
            if str(conv.get("chat_type") or "private") != "private":
                return
            # 扫描门（2026-08-18 可配置）：time_like（默认，仅含时间信号的入站）
            # / all（全量入站，仍受每日预算封顶）。切主判据要 llm_only 样本 ≥20，
            # 窄门实测 7 天才攒 1 条（预算 150/天只用 ~4/天）——评估期把门放宽
            # 是在**预算内**买证据积累速度；LLM prompt 本身严格要求可定日期的
            # 事（模糊说法 found=false），门宽不等于抓得滥，且影子只记不写。
            _gate = str((cfg.get("llm_extract") or {}).get(
                "scan_gate") or "time_like").strip().lower()
            if _gate != "all" and not time_like(t):
                self.stats.gate_rejected += 1
                return
            item = {
                "ts": time.time(),
                "conversation_id": str(conv.get("conversation_id") or ""),
                "platform": str(conv.get("platform") or ""),
                "account_id": str(conv.get("account_id") or "default"),
                "chat_key": str(conv.get("chat_key") or ""),
                "text": t[:500],
            }
            with self._q_lock:
                if len(self._q) >= self._queue_max:
                    self._q.popleft()
                    self.stats.dropped += 1
                self._q.append(item)
                self.stats.enqueued += 1
        except Exception:
            logger.debug("care shadow inbound_cb 异常（忽略）", exc_info=True)

    # ── 生命周期（与 CareDispatcher 同范式）──────────────────────────────
    def is_running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_evt = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="care_shadow_scan")

    async def stop(self) -> None:
        if self._stop_evt:
            self._stop_evt.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            except Exception:
                pass

    async def _loop(self) -> None:
        try:
            while not (self._stop_evt and self._stop_evt.is_set()):
                try:
                    n = await self.run_once()
                    if n:
                        logger.info("[care_shadow] tick: 对照 %d 条", n)
                except Exception:
                    logger.exception("care_shadow run_once 异常")
                try:
                    if self._stop_evt:
                        await asyncio.wait_for(self._stop_evt.wait(), timeout=self._interval)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("care_shadow 退出")

    async def run_once(self, *, now: Optional[float] = None) -> int:
        """drain 一批：LLM 与正则双跑 → 计数 + JSONL。返回本轮对照条数。"""
        n = float(now if now is not None else time.time())
        self.last_tick_ts = n
        cfg = self._cfg() or {}
        if not shadow_active(cfg):
            return 0
        llm_cfg = _shadow_cfg(cfg)
        budget = max(1, int(llm_cfg.get("daily_budget", 150)))
        min_conf = float(cfg.get("min_confidence", 0.6))
        done = 0
        while done < self._per_tick:
            with self._q_lock:
                if not self._q:
                    break
                item = self._q.popleft()
            self.stats.drained += 1
            done += 1
            if self.stats.budget_used_today(n) >= budget:
                self.stats.budget_skipped += 1
                continue
            await self._compare_one(item, min_conf=min_conf, care_cfg=cfg)
        return done

    def _has_pending_same_day(self, contact_key: str, event_at: float) -> bool:
        """P4 防双写②：同联系人同**事件日**已有 pending → 不再入库。

        跨措辞重复（正则昨天抓了「面试」、LLM 今天抓「终面」）topic_norm 不同、
        store 去重兜不住——从日历维度收口：一天一件事一条关怀。查不到按无
        （store 自带同主题去重仍兜底）。"""
        if self._care_store is None:
            return False
        try:
            rows = self._care_store.list_by_contact(
                contact_key, status="pending", limit=50)
        except Exception:
            return False
        day = datetime.fromtimestamp(float(event_at)).date()
        for r in rows or []:
            try:
                if datetime.fromtimestamp(
                        float(r.get("event_at") or 0)).date() == day:
                    return True
            except Exception:
                continue
        return False

    def _maybe_capture(self, item: Dict[str, Any], parsed: Optional[Dict[str, Any]],
                       *, care_cfg: dict, min_conf: float) -> bool:
        """P4 真实捕获：llm_only 结果写入 care_schedule。返回是否真的入了库。

        只在 ``llm_extract.enabled=true`` 且注入了 care_store 时走到这里；
        任何一步失败只计数不抛（捕获是增益，绝不影响影子对照主流程）。
        """
        try:
            c = commitment_from_llm(
                parsed, source_text=str(item.get("text") or ""),
                now=float(item.get("ts") or time.time()))
            if c is None:
                self.stats.capture_invalid += 1
                return False
            cid = str(item.get("conversation_id") or "")
            if self._has_pending_same_day(cid, c.event_at):
                self.stats.capture_skipped_pending += 1
                return False
            rid = self._care_store.add_commitment(
                c, contact_key=cid,
                platform=str(item.get("platform") or ""),
                account_id=str(item.get("account_id") or "default"),
                chat_key=str(item.get("chat_key") or ""),
                min_confidence=min_conf,
                dedup_window_days=float(care_cfg.get("dedup_window_days", 3)))
            if rid:
                self.stats.captured += 1
                logger.info("[care_shadow] LLM 捕获入库 id=%s contact=%s topic=%s",
                            rid, cid, c.topic)
                return True
            self.stats.capture_skipped_pending += 1   # store 同主题去重兜底命中
            return False
        except Exception:
            logger.debug("care shadow 捕获失败（忽略）", exc_info=True)
            return False

    async def _compare_one(self, item: Dict[str, Any], *, min_conf: float,
                           care_cfg: Optional[dict] = None) -> None:
        text = str(item.get("text") or "")
        ts = float(item.get("ts") or time.time())
        # 正则侧（含置信度阈值——与真实捕获入库同口径）
        try:
            regex_hits = [
                c for c in extract_commitments(text, now=ts)
                if c.confidence >= min_conf
            ]
        except Exception:
            regex_hits = []
        # LLM 侧
        parsed = None
        self.stats.budget_consume(ts)
        self.stats.llm_calls += 1
        try:
            raw = await self._ai.chat(build_llm_extract_prompt(text, now=ts))
            parsed = parse_llm_extract(raw)
        except Exception:
            logger.debug("care shadow LLM 调用失败", exc_info=True)
        if parsed is None:
            self.stats.llm_err += 1
        else:
            self.stats.llm_ok += 1
        llm_found = bool(parsed and parsed.get("found")
                         and float(parsed.get("confidence", 0)) >= min_conf)
        regex_found = bool(regex_hits)
        if llm_found:
            self.stats.llm_found += 1
        if regex_found:
            self.stats.regex_found += 1
        if llm_found and regex_found:
            self.stats.both_found += 1
        elif llm_found:
            self.stats.llm_only += 1
        elif regex_found:
            self.stats.regex_only += 1
        # P4 真实捕获（默认关）：只收 llm_only——正则命中的消息 ingest 时已由
        # care_capture 入过库（防双写①：正则优先）。
        captured = False
        _cc_cfg = care_cfg or {}
        if (llm_found and not regex_found and self._care_store is not None
                and bool(_shadow_cfg(_cc_cfg).get("enabled", False))):
            captured = self._maybe_capture(
                item, parsed, care_cfg=_cc_cfg, min_conf=min_conf)
        self._log_jsonl({
            "ts": round(ts, 3),
            "cid": item.get("conversation_id") or "",
            "platform": item.get("platform") or "",
            "text": text[:120],
            "regex": [
                {"topic": c.topic, "due_at": round(c.due_at, 1),
                 "confidence": round(c.confidence, 3)}
                for c in regex_hits
            ],
            "llm": parsed if parsed is not None else {"error": True},
            "agree": llm_found == regex_found,
            "captured": captured,
        })

    def _log_jsonl(self, rec: Dict[str, Any]) -> None:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            day = datetime.fromtimestamp(float(rec.get("ts") or time.time()))
            p = self._log_dir / f"shadow-{day.strftime('%Y%m%d')}.jsonl"
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("care shadow jsonl 写入失败（忽略）", exc_info=True)

    def snapshot(self) -> Dict[str, Any]:
        out = self.stats.snapshot()
        with self._q_lock:
            out["queue"] = len(self._q)
        out["running"] = self.is_running()
        out["last_tick_ts"] = self.last_tick_ts
        return out


__all__ = [
    "CareShadowScanner", "CareShadowStats", "get_care_shadow_stats",
    "shadow_active",
]
