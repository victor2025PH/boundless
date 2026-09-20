# -*- coding: utf-8 -*-
"""人设文档导入 / 一致性考题观测（G 线，进程级单例）。

背景：B/D/E 线落了「文档 → LLM 抽取 → 人设」「长传记入库」「人设一致性考题」整条
导入链，但运营在 ops-overview 上看不到任何读数——导入用没用、抽取多慢、完整度
多少、考题平均分多少，全靠翻日志。本模块把导入漏斗变成**可观测计数**：

    parse / parse_docx → extract_start → extract_done|extract_error
                       → bio_stash（长传记入库） → quiz_run → quiz_done(score)

埋点全部 best-effort（routes 侧 try/except 吞）——观测绝不允许影响导入本体。
读出：``dump()`` → ``/api/workspace/metrics.persona_import``、``dump_prom()`` →
Prometheus ``persona_import_*``、ops-overview「📄 人设导入」卡（零流量整卡隐藏）。

另转发长传记检索观测（``persona_bio_store.retrieval_stats_snapshot``）为
``dump()["bio_retrieval"]`` 与 Prometheus ``persona_bio_retrieval_*_total``——
导入完只是「存进去了」，检索命中率才说明长传记有没有真的在被用上。

风格对齐 src/web/frontend_error_stats.py：无新增依赖，线程安全，进程级单例。
**只存计数与均值**（耗时/完整度/考题分），绝不存文档内容/人设字段/考题答案。
未知事件名静默忽略（前向兼容：旧进程收到新埋点不炸）。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

# 认可的漏斗事件（未知事件静默忽略，前向兼容）
_EVENTS = (
    "parse",            # 文档解析成功（JSON 文本或上传文件）
    "parse_docx",       # 其中：.docx 上传解析（parse 的子集，另计一发）
    "extract_start",    # 抽取任务已提交并开跑
    "extract_done",     # 抽取成功（附 duration_ms / completeness）
    "extract_error",    # 抽取失败（LLM 输出坏 JSON 重试仍败等）
    "bio_stash",        # 长传记原文入库（persona_bio_store）
    "bio_reembed",      # 给缺向量的块/句补嵌（老库回填；任务提交即记一发）
    "quiz_run",         # 一致性考题任务提交
    "quiz_done",        # 考题完成（附 score 0-100）
    "quiz_saved",       # 考题报告已落库（persona_quiz_store）
)


def _num(v: Any) -> Optional[float]:
    """数值护栏：int/float（bool 不算）→ float；其余 → None。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


# 长传记检索观测取自 persona_bio_store 的模块级计数（另一条线维护）。这里只做
# **只读转发**：局部 import + 全异常吞掉 → 传记子系统缺席/改签名/DB 炸都不能把
# /api/workspace/metrics 带崩（观测挂了比业务挂了轻，但把业务看板打死不可接受）。
_BIO_RETRIEVAL_FIELDS = ("queries", "hits", "empty", "embed_fail")


def _bio_retrieval() -> Dict[str, Any]:
    """``{queries,hits,empty,embed_fail,hit_rate,avg_hits}``；任何异常 → ``{}``。"""
    try:
        from src.companion.persona_bio_store import retrieval_stats_snapshot
        snap = retrieval_stats_snapshot()
        return dict(snap) if isinstance(snap, dict) else {}
    except Exception:
        return {}


class PersonaImportStats:
    """导入漏斗计数 + 抽取耗时/完整度/考题分均值（线程安全，进程级）。"""

    __slots__ = (
        "_lock", "_counts",
        "_extract_ms_sum", "_extract_ms_n",
        "_extract_comp_sum", "_extract_comp_n",
        "_quiz_score_sum", "_quiz_score_n", "_quiz_last_score",
    )

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counts: Dict[str, int] = {e: 0 for e in _EVENTS}
        self._extract_ms_sum = 0.0
        self._extract_ms_n = 0
        self._extract_comp_sum = 0.0
        self._extract_comp_n = 0
        self._quiz_score_sum = 0.0
        self._quiz_score_n = 0
        self._quiz_last_score: Optional[float] = None

    def record(self, event: str, *, duration_ms: Optional[float] = None,
               score: Optional[float] = None,
               completeness: Optional[float] = None) -> None:
        """记一发漏斗事件；未知事件名静默忽略（前向兼容）。

        ``extract_done`` 额外累计 ``duration_ms`` / ``completeness`` 均值；
        ``quiz_done`` 额外累计 ``score`` 均值与最近一次分数。
        非数值附加参数按缺席处理（绝不抛）。
        """
        ev = str(event or "")
        if ev not in self._counts:
            return
        with self._lock:
            self._counts[ev] += 1
            if ev == "extract_done":
                ms = _num(duration_ms)
                if ms is not None:
                    self._extract_ms_sum += ms
                    self._extract_ms_n += 1
                comp = _num(completeness)
                if comp is not None:
                    self._extract_comp_sum += comp
                    self._extract_comp_n += 1
            elif ev == "quiz_done":
                sc = _num(score)
                if sc is not None:
                    self._quiz_score_sum += sc
                    self._quiz_score_n += 1
                    self._quiz_last_score = sc

    # ── 读出 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _avg(total: float, n: int) -> Optional[float]:
        return (total / n) if n > 0 else None

    def snapshot(self) -> Dict[str, Any]:
        """结构化快照：counts 分组 + 均值字段（None=还没有样本）。"""
        with self._lock:
            return {
                "active": any(v > 0 for v in self._counts.values()),
                "counts": dict(self._counts),
                "extract_avg_ms": self._avg(self._extract_ms_sum,
                                            self._extract_ms_n),
                "extract_avg_completeness": self._avg(self._extract_comp_sum,
                                                      self._extract_comp_n),
                "quiz_avg_score": self._avg(self._quiz_score_sum,
                                            self._quiz_score_n),
                "quiz_last_score": self._quiz_last_score,
            }

    def dump(self) -> Dict[str, Any]:
        """进 ``/api/workspace/metrics.persona_import``：键名平铺易读。"""
        snap = self.snapshot()
        out: Dict[str, Any] = {"active": snap["active"]}
        out.update(snap["counts"])
        for k in ("extract_avg_ms", "extract_avg_completeness",
                  "quiz_avg_score", "quiz_last_score"):
            out[k] = snap[k]
        out["bio_retrieval"] = _bio_retrieval()
        return out

    def dump_prom(self) -> str:
        """Prometheus 文本（前缀 ``persona_import_``，风格对齐 frontend_error_stats）。"""
        snap = self.snapshot()
        lines = [
            "# HELP persona_import_events_total Persona doc-import / quiz funnel events",
            "# TYPE persona_import_events_total counter",
        ]
        for ev in _EVENTS:
            lines.append(
                f'persona_import_events_total{{event="{ev}"}} '
                f"{int(snap['counts'][ev])}")
        gauges = (
            ("persona_import_extract_avg_ms",
             "Average successful extraction duration in milliseconds",
             snap["extract_avg_ms"]),
            ("persona_import_extract_avg_completeness",
             "Average persona completeness score of successful extractions (0-100)",
             snap["extract_avg_completeness"]),
            ("persona_import_quiz_avg_score",
             "Average persona consistency quiz score (0-100)",
             snap["quiz_avg_score"]),
            ("persona_import_quiz_last_score",
             "Most recent persona consistency quiz score (0-100)",
             snap["quiz_last_score"]),
        )
        for name, help_text, value in gauges:
            if value is None:            # 无样本不出该 series（gauge 无 null 语义）
                continue
            lines += [
                f"# HELP {name} {help_text}",
                f"# TYPE {name} gauge",
                f"{name} {float(value)}",
            ]
        # 长传记检索（命中率/空结果/嵌入失败由 Prometheus 侧自行做比值与告警）
        bio = _bio_retrieval()
        if bio:
            counters = (
                ("persona_bio_retrieval_queries_total",
                 "Persona long-bio retrieval queries"),
                ("persona_bio_retrieval_hits_total",
                 "Persona long-bio retrieval queries returning at least one chunk"),
                ("persona_bio_retrieval_empty_total",
                 "Persona long-bio retrieval queries returning nothing"),
                ("persona_bio_retrieval_embed_fail_total",
                 "Persona long-bio retrieval query-embedding failures"),
            )
            for (name, help_text), field in zip(counters, _BIO_RETRIEVAL_FIELDS):
                v = _num(bio.get(field)) or 0.0
                lines += [
                    f"# HELP {name} {help_text}",
                    f"# TYPE {name} counter",
                    f"{name} {int(v)}",
                ]
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            for k in self._counts:
                self._counts[k] = 0
            self._extract_ms_sum = 0.0
            self._extract_ms_n = 0
            self._extract_comp_sum = 0.0
            self._extract_comp_n = 0
            self._quiz_score_sum = 0.0
            self._quiz_score_n = 0
            self._quiz_last_score = None


_SINGLETON: Optional[PersonaImportStats] = None
_LOCK = threading.Lock()


def get_persona_import_stats() -> PersonaImportStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = PersonaImportStats()
    return _SINGLETON


def record_import_event(event: str, *, duration_ms: Optional[float] = None,
                        score: Optional[float] = None,
                        completeness: Optional[float] = None) -> None:
    """模块级便捷入口（routes 直接 import 本函数；任务线程调用安全）。"""
    get_persona_import_stats().record(
        event, duration_ms=duration_ms, score=score, completeness=completeness)


def snapshot() -> Dict[str, Any]:
    return get_persona_import_stats().snapshot()


def dump() -> Dict[str, Any]:
    return get_persona_import_stats().dump()


def dump_prom() -> str:
    return get_persona_import_stats().dump_prom()


def reset_for_test() -> None:
    """清零单例（测试隔离用）。"""
    get_persona_import_stats().reset()


__all__ = [
    "PersonaImportStats", "get_persona_import_stats", "record_import_event",
    "snapshot", "dump", "dump_prom", "reset_for_test",
]
