"""陪伴记忆**抽取**质量评测（源头质量：召回 + 误抽 / 精确率守护）。

口径：记忆系统的质量上限在「抽取」这一步——抽漏 = 该记的没记，抽错 = 把句子片段/
噪声当事实写进长期记忆污染人设。本模块给定一条消息 + 期望/禁止子串，跑**真实抽取器**：
  - ``expect``：该消息应被抽出的事实子串（任一抽取结果含之 → 召回命中）；
  - ``forbid``：不应被抽出的子串（出现 → 记一次误抽）。
输出召回率 + 误抽数 + passed（召回达标 **且** 误抽不超阈）。

设计（与 faq/translation/memory-recall 评测一致）：
  - 核心 ``evaluate_fact_extraction(extract_fn, samples)`` 与抽取器实现解耦，``extract_fn``
    签名 ``(text, reply) -> List[str]``：
    * 启发式：``heuristic_extract_fn``（纯函数、零依赖、可复现）→ CI 常驻门禁；
    * LLM：``build_llm_extract_fn`` 包 ``ai_client.extract_memory_bullets`` → 度量真实抽取，
      缺 key/不可用时优雅返回 None（门禁跳过）。
  - **不改任何生产默认**：本模块是抽取器的质量标尺/回归网，不触发任何写回。

J-10 A1（#183 跨语言接地）追加**接地护栏评测** ``evaluate_evidence_grounding``：抽取器
之后还有一道 ``memory_grounding`` 护栏，它决定「抽出来的进不进库」。88MP86 实锤：英文
客户的中文事实在旧词汇判据下 100% 被丢。本评测给定「用户原话 + 抽取器候选（事实 +
引文）+ 期望 keep/drop」，直接打真实护栏——纯函数、离线、常驻门禁；跨语言 keep 用例
与 Phase8 事故 drop 用例（AI 臆测一条都不能过）同表。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from .dataset import ExtractSample, load_extract_samples

GROUNDING_SAMPLES_PATH = "config/eval/memory_grounding_samples.yaml"
XLANG_EXTRACT_SAMPLES_PATH = "config/eval/memory_extract_samples_xlang.yaml"

# (text, reply) -> 抽出的事实串列表
ExtractFn = Callable[[str, str], List[str]]


def heuristic_extract_fn(text: str, reply: str = "") -> List[str]:
    """启发式抽取器适配为统一签名（忽略 reply）。"""
    from src.utils.memory_heuristic import extract_heuristic_facts

    return extract_heuristic_facts(text)


def _facts_contain(facts: List[str], sub: str) -> bool:
    return any(sub in (f or "") for f in (facts or []))


def evaluate_fact_extraction(
    extract_fn: ExtractFn,
    samples: Optional[List[ExtractSample]] = None,
    *,
    recall_target: float = 0.8,
    max_false_positive: int = 0,
) -> Dict[str, Any]:
    """跑抽取评测；返回逐样本明细 + 召回率 + 误抽数 + passed。

    召回率 = 命中的 expect 子串数 / 全部 expect 子串数（按子串粒度，跨样本汇总）。
    误抽数 = 全部 forbid 子串里被抽出的个数（精确率/防污染守护）。
    passed = 召回率 ≥ recall_target **且** 误抽数 ≤ max_false_positive。
    """
    rows = samples if samples is not None else load_extract_samples()
    results: List[Dict[str, Any]] = []
    total_expect = 0
    found_expect = 0
    fp_total = 0
    for s in rows:
        facts = extract_fn(s.text, s.reply) or []
        missing = [e for e in s.expect if not _facts_contain(facts, e)]
        fp_hits = [f for f in s.forbid if _facts_contain(facts, f)]
        total_expect += len(s.expect)
        found_expect += len(s.expect) - len(missing)
        fp_total += len(fp_hits)
        results.append({
            "text": s.text,
            "facts": facts,
            "missing": missing,
            "false_positives": fp_hits,
            "note": s.note,
        })
    recall = round(found_expect / total_expect, 3) if total_expect else 1.0
    return {
        "results": results,
        "summary": {
            "samples": len(rows),
            "expect_total": total_expect,
            "expect_found": found_expect,
            "recall": recall,
            "false_positives": fp_total,
        },
        "recall_target": recall_target,
        "max_false_positive": max_false_positive,
        "passed": recall >= recall_target and fp_total <= max_false_positive,
    }


def format_extract_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 记忆抽取质量报告 ===",
        f"样本: {m['samples']}  召回: {m['recall']:.2%} "
        f"({m['expect_found']}/{m['expect_total']})  "
        f"误抽: {m['false_positives']}  "
        f"目标: 召回≥{report['recall_target']:.0%}/误抽≤{report['max_false_positive']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    miss = [r for r in report["results"] if r["missing"]]
    if miss:
        lines.append(f"漏抽 {len(miss)} 例:")
        for r in miss[:20]:
            lines.append(f"  - {r['text']}  缺={r['missing']}")
    fps = [r for r in report["results"] if r["false_positives"]]
    if fps:
        lines.append(f"误抽 {len(fps)} 例:")
        for r in fps[:20]:
            lines.append(f"  - {r['text']}  误={r['false_positives']}  全部={r['facts']}")
    return "\n".join(lines)


def build_llm_extract_fn(
    config: Optional[Dict[str, Any]] = None,
) -> Optional[ExtractFn]:
    """包 ``ai_client.extract_memory_bullets`` 成同步 extract_fn；探针失败 → None（门禁跳过）。

    注意 extract_memory_bullets 要求 user/assistant 双方均 ≥2 字符，故空 reply 时补一句
    中性占位回复，避免被早退过滤掉。
    """
    try:
        import asyncio

        # 2026-07-29：按数据根契约解析（自动发现活跃实例），别用 CWD 相对读取——
        # 从引擎根跑会读到迁移遗留旧副本，评的是没在跑的配置。见 eval_config。
        from src.eval.eval_config import load_runtime_config
        cfg = load_runtime_config(config)
        from src.ai.ai_client import AIClient

        class _Cfg:
            config = cfg
            config_path = "config/config.yaml"

            def get_ai_config(self):
                return (cfg or {}).get("ai", {})

        client = AIClient(_Cfg())

        async def _probe():
            if hasattr(client, "initialize"):
                try:
                    await client.initialize()
                except Exception:
                    pass
            return await client.extract_memory_bullets(
                "我叫小明，住在大阪", "好的小明，我记住啦")

        # 单一常驻 loop 贯穿 probe + 全部样本：AsyncOpenAI/httpx 连接绑定首个事件
        # 循环，逐样本各开新 asyncio.run 会撞已关闭的旧 loop → 异常被吞成 [] →
        # 召回假性掉到 50%（2026-07-26 实锤，逐条单跑全过而 harness 批跑随机漏）。
        loop = asyncio.new_event_loop()
        try:
            probed = loop.run_until_complete(_probe())
        except Exception:
            loop.close()
            return None
        # 探针句「我叫小明」抽不出任何事实＝没有可用模型（空 key / 占位端点
        # 不抛、只回空列表）。按门禁约定跳过，不要把 0% 召回报成失败。
        if not probed:
            loop.close()
            return None

        def _extract(text: str, reply: str = "") -> List[str]:
            r = reply or "嗯嗯，我记下了"
            try:
                return list(
                    loop.run_until_complete(
                        client.extract_memory_bullets(text, r)) or [])
            except Exception:
                return []

        return _extract
    except Exception:
        return None


def load_grounding_samples(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """加载接地护栏评测样本（YAML）。

    每条：``{text: 用户原话, reply: 助手回复(可省), candidates: [{fact, evidence, expect:
    keep|drop, reason?: 期望丢弃原因(可省)}], note}``。
    """
    p = path or GROUNDING_SAMPLES_PATH
    if not os.path.exists(p):
        raise FileNotFoundError(p)
    import yaml
    with open(p, "r", encoding="utf-8") as f:
        rows = yaml.safe_load(f) or []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("text"):
            continue
        cands = []
        for c in (r.get("candidates") or []):
            if not isinstance(c, dict) or not c.get("fact"):
                continue
            cands.append({
                "fact": str(c.get("fact") or ""),
                "evidence": str(c.get("evidence") or ""),
                "expect": str(c.get("expect") or "keep").strip().lower(),
                "reason": str(c.get("reason") or "").strip(),
            })
        out.append({
            "text": str(r.get("text") or ""),
            "reply": str(r.get("reply") or ""),
            "candidates": cands,
            "note": str(r.get("note") or ""),
        })
    return out


def evaluate_evidence_grounding(
    samples: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """跑真实接地护栏（``memory_grounding.ground_fact_with_evidence``）。

    口径：``leaked``＝期望 drop 却被放行（假记忆入库——硬红线，一条都不许）；
    ``lost``＝期望 keep 却被丢（跨语言漏记——本条修的就是它）；``reason_mismatch``＝
    丢了但原因与期望不符（只计数不判 FAIL，原因字面是观测口径不是安全口径）。
    ``passed`` = leaked == 0 且 lost == 0。
    """
    from src.ai.memory_grounding import ground_fact_with_evidence

    rows = samples if samples is not None else load_grounding_samples()
    results: List[Dict[str, Any]] = []
    n_keep = n_drop = kept_ok = dropped_ok = leaked = lost = reason_mismatch = 0
    for s in rows:
        for c in s["candidates"]:
            ok, reason = ground_fact_with_evidence(c["fact"], c["evidence"], s["text"])
            want_keep = c["expect"] != "drop"
            verdict = "ok"
            if want_keep:
                n_keep += 1
                if ok:
                    kept_ok += 1
                else:
                    lost += 1
                    verdict = "lost"
            else:
                n_drop += 1
                if ok:
                    leaked += 1
                    verdict = "leaked"
                else:
                    dropped_ok += 1
                    if c["reason"] and c["reason"] != reason:
                        reason_mismatch += 1
                        verdict = "reason_mismatch"
            results.append({
                "text": s["text"], "fact": c["fact"], "evidence": c["evidence"],
                "expect": "keep" if want_keep else "drop",
                "got": "keep" if ok else "drop", "reason": reason,
                "verdict": verdict, "note": s["note"],
            })
    return {
        "results": results,
        "summary": {
            "samples": len(rows),
            "candidates": n_keep + n_drop,
            "expect_keep": n_keep, "kept_ok": kept_ok, "lost": lost,
            "expect_drop": n_drop, "dropped_ok": dropped_ok, "leaked": leaked,
            "reason_mismatch": reason_mismatch,
        },
        "passed": leaked == 0 and lost == 0,
    }


def format_grounding_report(report: Dict[str, Any]) -> str:
    m = report["summary"]
    lines = [
        "=== 记忆接地护栏报告（引文级 / 跨语言） ===",
        f"样本: {m['samples']}  候选: {m['candidates']}  "
        f"应留: {m['kept_ok']}/{m['expect_keep']} (漏记 {m['lost']})  "
        f"应丢: {m['dropped_ok']}/{m['expect_drop']} (漏网 {m['leaked']})  "
        f"原因不符: {m['reason_mismatch']}  "
        f"{'[PASS]' if report['passed'] else '[FAIL]'}",
    ]
    bad = [r for r in report["results"] if r["verdict"] != "ok"]
    for r in bad[:30]:
        lines.append(
            f"  - [{r['verdict']}] {r['fact']} ⇐ {r['evidence']!r} | 原话: {r['text'][:60]}"
            f" | got={r['got']}/{r['reason'] or '-'} ({r['note']})")
    return "\n".join(lines)


__all__ = [
    "ExtractFn",
    "GROUNDING_SAMPLES_PATH",
    "XLANG_EXTRACT_SAMPLES_PATH",
    "heuristic_extract_fn",
    "evaluate_fact_extraction",
    "format_extract_report",
    "build_llm_extract_fn",
    "load_grounding_samples",
    "evaluate_evidence_grounding",
    "format_grounding_report",
]
