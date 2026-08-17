# -*- coding: utf-8 -*-
"""翻译引擎 A/B 横比裁判台（纯函数核心，零 IO）。

**为什么需要它**：既有 `evaluate_translation_quality` 一次只评**一个**引擎，周批
`TranslationEvalWeekly` 攒的是**同引擎的时序趋势**。于是「要不要换 MT 模型」这个问题，
当前只能手跑两遍 CLI、肉眼比两份 JSON 的均分——而均分比较回答不了「这 0.02 的差是真的
还是噪声」，恰恰是换不换模型唯一需要的那个答案。

本模块把「两份独立报告」变成**配对实验**：

1. **裁判恒定且第三方**——两个候选必须共用同一个回译引擎。让候选各自回译自己量的是
   「复读自己措辞」的自洽度不是质量（2026-07-12 周审已实证：自回译给 DeepSeek 虚高
   0.750 vs 交叉回译后 HY-MT 反超）。判据在 `judge_is_contaminated`。
2. **配对差 + bootstrap 置信区间**——逐样本对齐后算 delta，重采样估计均值差的 CI。
   CI 跨零 ⇒ 判「无差异」。30~50 样本上 0.02 的均分差本来就在噪声带里，不做区间估计
   等于拿噪声做迁移决策。
3. **效应量闸门**——统计显著 ≠ 值得迁移。CI 不跨零但效应 < `min_effect` 仍判
   `marginal`（"赢了但不值得动"），因为换模型的代价是真实的而 0.005 的质量差不是。
4. **硬失败单独计**——某候选把样本翻成空串（超时/端点抖动）会被记 0 分拖垮均分，
   看起来像「质量差」实为「不可用」。两者的处置完全不同（一个是选型，一个是运维），
   故 `hard_failures` 独立成头条指标而不是混进分数里。

**per_lang_order 建议的适用边界**（一个天真实现会搞错的地方）：
`translation.engines.per_lang_order` 按**引擎名**路由（`{hi: [ai, ollama_mt]}`）。
若两个候选是同一 engine 名下的不同 model（如两个 ollama_mt 模型），per_lang_order
**根本无法区分它们**——此时唯一可执行的结论是全局改 `model` 字段，或先给新模型起一个
独立 engine 名。`suggest_per_lang_order` 会据此拒绝给出误导性建议。
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 逐样本分差小于此值视为平局（分数本身保留 3 位小数，再小的差是舍入噪声）
DEFAULT_TIE_EPS = 0.005
# 均值差的最小可行动效应：CI 不跨零但低于此值 → marginal（赢了但不值得迁移）
DEFAULT_MIN_EFFECT = 0.02
# 语对级结论的最小样本数：低于此值只报数字不下结论
DEFAULT_MIN_PAIR_N = 3

_BOOTSTRAP_ITERS = 2000
_BOOTSTRAP_SEED = 20260803  # 固定种子：门禁与周批必须可复现


def sample_key(row: Dict[str, Any]) -> Tuple[str, str, str]:
    """逐样本对齐键。

    刻意用 (原文, 源语, 目标语) 而不是列表下标：下标对齐依赖两次评测的 results 顺序
    与长度严格一致，一旦某侧提前返回就会**静默错位**——错位后的 delta 全是垃圾但看起来
    完全正常，是这类工具最容易出的无声 bug。
    """
    return (
        str(row.get("text", "")),
        str(row.get("source", "")),
        str(row.get("target", "")),
    )


def _metric_of(row: Dict[str, Any], metric: str) -> Optional[float]:
    """取某样本在指定轨上的分。语义轨缺失（嵌入失败）返回 None，由调用方决定跳过。"""
    if metric == "semantic":
        v = row.get("semantic")
        return float(v) if v is not None else None
    return float(row.get("score", 0.0) or 0.0)


def is_hard_failure(row: Dict[str, Any]) -> bool:
    """该样本是否属「压根没翻出来」而非「翻得不好」。

    forward_failed = 候选自身没出译文；back_failed = 裁判没回译出来。后者是**裁判**的
    问题，对两个候选是共同噪声，不该算到某一方头上，故只认前者。
    """
    return str(row.get("reason", "")) == "forward_failed"


def join_results(
    a_results: Sequence[Dict[str, Any]],
    b_results: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按样本键对齐两份逐样本结果，返回配对行。

    只保留两边都出现的样本（对不上的样本无法配对比较，计入 `unpaired` 由调用方观测）。
    同键重复样本按出现顺序一一配对，不做去重——语料里若真有重复条目，那是语料的事，
    这里静默丢弃反而会让样本数对不上账。
    """
    buckets: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in b_results:
        buckets.setdefault(sample_key(r), []).append(dict(r))
    out: List[Dict[str, Any]] = []
    for ra in a_results:
        k = sample_key(ra)
        queue = buckets.get(k)
        if not queue:
            continue
        rb = queue.pop(0)
        out.append({
            "key": k,
            "source": k[1],
            "target": k[2],
            "a": dict(ra),
            "b": rb,
        })
    return out


def bootstrap_ci(
    deltas: Sequence[float],
    *,
    iters: int = _BOOTSTRAP_ITERS,
    alpha: float = 0.05,
    seed: int = _BOOTSTRAP_SEED,
) -> Tuple[Optional[float], Optional[float]]:
    """配对差均值的 bootstrap 百分位置信区间。

    刻意不引 scipy/numpy：评测链要能在任何环境跑起来，为一个百分位数拖进科学计算栈
    不划算。固定 seed ⇒ 同输入同结论（门禁可断言，周批趋势可比）。
    """
    n = len(deltas)
    if n < 2:
        return (None, None)
    rng = random.Random(seed)
    means: List[float] = []
    for _ in range(max(1, int(iters))):
        s = 0.0
        for _ in range(n):
            s += deltas[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_i = int((alpha / 2.0) * len(means))
    hi_i = min(len(means) - 1, int((1.0 - alpha / 2.0) * len(means)))
    return (round(means[lo_i], 4), round(means[hi_i], 4))


def sign_counts(
    deltas: Sequence[float], *, eps: float = DEFAULT_TIE_EPS,
) -> Dict[str, int]:
    """逐样本胜负平计数（delta = b - a，正数为 B 胜）。"""
    a_win = sum(1 for d in deltas if d < -eps)
    b_win = sum(1 for d in deltas if d > eps)
    return {"a_wins": a_win, "b_wins": b_win, "ties": len(deltas) - a_win - b_win}


def judge_is_contaminated(
    judge_label: str, a_label: str, b_label: str,
) -> Optional[str]:
    """裁判是否与某候选同源（同源 ⇒ 该候选拿到「复读自己措辞」的不当加分）。

    返回被污染的一侧标识，干净则 None。标签比较用规范化后的全等——宁可漏判也不误判，
    误判会让一次合法的 A/B 被拒跑。
    """
    j = (judge_label or "").strip().lower()
    if not j or j == "same":
        return "both"
    if j == (a_label or "").strip().lower():
        return "a"
    if j == (b_label or "").strip().lower():
        return "b"
    return None


def _pair_breakdown(
    paired: Sequence[Dict[str, Any]], metric: str, *, eps: float, min_pair_n: int,
) -> Dict[str, Dict[str, Any]]:
    """按语对拆分胜负——真正可执行的结论多半是语对级的（某语对换引擎），不是全局的。"""
    agg: Dict[str, Dict[str, Any]] = {}
    for row in paired:
        sa = _metric_of(row["a"], metric)
        sb = _metric_of(row["b"], metric)
        if sa is None or sb is None:
            continue
        key = f"{row['source']}->{row['target']}"
        g = agg.setdefault(key, {"n": 0, "a_sum": 0.0, "b_sum": 0.0, "deltas": []})
        g["n"] += 1
        g["a_sum"] += sa
        g["b_sum"] += sb
        g["deltas"].append(sb - sa)
    out: Dict[str, Dict[str, Any]] = {}
    for key, g in sorted(agg.items()):
        n = int(g["n"])
        deltas = g["deltas"]
        mean_delta = sum(deltas) / n
        counts = sign_counts(deltas, eps=eps)
        if n < min_pair_n:
            verdict = "insufficient"
        elif mean_delta > eps and counts["b_wins"] > counts["a_wins"]:
            verdict = "b_better"
        elif mean_delta < -eps and counts["a_wins"] > counts["b_wins"]:
            verdict = "a_better"
        else:
            verdict = "tie"
        out[key] = {
            "n": n,
            "a_mean": round(g["a_sum"] / n, 3),
            "b_mean": round(g["b_sum"] / n, 3),
            "delta": round(mean_delta, 3),
            "verdict": verdict,
            **counts,
        }
    return out


def compare(
    a_report: Dict[str, Any],
    b_report: Dict[str, Any],
    *,
    a_label: str = "A",
    b_label: str = "B",
    judge_label: str = "",
    metric: str = "auto",
    tie_eps: float = DEFAULT_TIE_EPS,
    min_effect: float = DEFAULT_MIN_EFFECT,
    min_pair_n: int = DEFAULT_MIN_PAIR_N,
) -> Dict[str, Any]:
    """把两份 `evaluate_translation_quality` 报告合成一份配对横比结论。

    metric="auto"：两侧都有语义轨就用语义轨，否则字符轨。语义轨是更可信的主判据
    ——字符轨会把正确的意译压成低分（既有 rescue 机制正是为此而生），而选型决策不该
    被措辞偏好带偏。字符轨仍全程计算并随报告一起给出。
    """
    a_rows = list(a_report.get("results") or [])
    b_rows = list(b_report.get("results") or [])
    paired = join_results(a_rows, b_rows)

    has_sem = (
        any(r["a"].get("semantic") is not None for r in paired)
        and any(r["b"].get("semantic") is not None for r in paired)
    )
    chosen = metric if metric in ("semantic", "char") else ("semantic" if has_sem else "char")
    if chosen == "semantic" and not has_sem:
        chosen = "char"

    deltas: List[float] = []
    a_vals: List[float] = []
    b_vals: List[float] = []
    for row in paired:
        sa = _metric_of(row["a"], chosen)
        sb = _metric_of(row["b"], chosen)
        if sa is None or sb is None:
            continue
        a_vals.append(sa)
        b_vals.append(sb)
        deltas.append(sb - sa)

    n = len(deltas)
    # 配对成功但在本轨上没分（语义轨嵌入失败）的样本会被静默排除在判据之外。
    # 3/50 无伤大雅，30/50 就是「结论其实只用了 20 个样本」——而两种情况下报告长得
    # 一模一样。故把「参与判据的样本数」与「配对总数」分开报，缺口自己说话。
    unscored = len(paired) - n
    mean_delta = round(sum(deltas) / n, 4) if n else 0.0
    lo, hi = bootstrap_ci(deltas, alpha=0.05)
    counts = sign_counts(deltas, eps=tie_eps)

    a_fail = sum(1 for r in a_rows if is_hard_failure(r))
    b_fail = sum(1 for r in b_rows if is_hard_failure(r))

    if n < 2 or lo is None or hi is None:
        verdict = "insufficient_data"
    elif lo > 0:
        verdict = "b_better" if mean_delta >= min_effect else "b_better_marginal"
    elif hi < 0:
        verdict = "a_better" if -mean_delta >= min_effect else "a_better_marginal"
    else:
        verdict = "no_difference"

    contaminated = judge_is_contaminated(judge_label, a_label, b_label)

    return {
        "a_label": a_label,
        "b_label": b_label,
        "judge_label": judge_label,
        "judge_contaminated": contaminated,
        "metric": chosen,
        "paired_n": n,
        "paired_total": len(paired),
        "unscored_on_metric": unscored,
        "unpaired_a": len(a_rows) - len(paired),
        "unpaired_b": len(b_rows) - len(paired),
        "a_mean": round(sum(a_vals) / n, 3) if n else 0.0,
        "b_mean": round(sum(b_vals) / n, 3) if n else 0.0,
        "mean_delta": mean_delta,
        "ci95": [lo, hi],
        "min_effect": min_effect,
        **counts,
        "a_hard_failures": a_fail,
        "b_hard_failures": b_fail,
        "verdict": verdict,
        "by_pair": _pair_breakdown(paired, chosen, eps=tie_eps, min_pair_n=min_pair_n),
    }


def suggest_per_lang_order(
    comparison: Dict[str, Any],
    *,
    a_engine: str,
    b_engine: str,
) -> Dict[str, Any]:
    """把语对级胜负翻译成 `translation.engines.per_lang_order` 覆写建议。

    **只有两候选分属不同 engine 名时才成立**：per_lang_order 是按引擎名路由的，同一
    engine 名下的两个 model 它区分不了。此时给建议等于给错误指令，故显式拒绝并说明
    唯一可行的两条路（全局换 model / 先给新模型起独立 engine 名）。
    """
    a_engine = (a_engine or "").strip()
    b_engine = (b_engine or "").strip()
    if not a_engine or not b_engine or a_engine == b_engine:
        return {
            "applicable": False,
            "reason": "same_engine_name",
            "note": (
                f"两个候选同属 engine「{a_engine or '?'}」，per_lang_order 按引擎名路由、"
                "区分不了同名引擎下的不同 model。可行路径只有两条：① 全局改 "
                "translation.engines.ollama_mt.model；② 先给新模型注册一个独立 engine 名，"
                "再按语对覆写。"
            ),
            "order": {},
        }
    order: Dict[str, List[str]] = {}
    for pair, g in (comparison.get("by_pair") or {}).items():
        verdict = g.get("verdict")
        if verdict not in ("a_better", "b_better"):
            continue
        tgt = pair.split("->", 1)[-1].strip()
        if not tgt:
            continue
        winner, loser = (b_engine, a_engine) if verdict == "b_better" else (a_engine, b_engine)
        order[tgt] = [winner, loser]
    return {
        "applicable": True,
        "reason": "",
        "note": "仅含语对级有明确胜负者；覆写外的引擎按默认序补尾兜底。",
        "order": order,
    }


_VERDICT_TEXT = {
    "b_better": "B 显著更好，值得切换",
    "b_better_marginal": "B 统计上更好但效应太小，不值得为此迁移",
    "a_better": "A 显著更好，维持现状",
    "a_better_marginal": "A 统计上更好但效应太小，两者实质等价",
    "no_difference": "无显著差异（置信区间跨零）——别换",
    "insufficient_data": "配对样本不足，无法下结论",
}


def format_ab_report(comparison: Dict[str, Any], suggestion: Optional[Dict[str, Any]] = None) -> str:
    """人读版横比报告。结论先行——读的人要的是「换不换」，不是一堆数字。"""
    c = comparison
    lines: List[str] = []
    verdict = c.get("verdict", "")
    lines.append(f"结论：{_VERDICT_TEXT.get(verdict, verdict)}")
    lines.append("")

    if c.get("judge_contaminated"):
        who = c["judge_contaminated"]
        which = {"a": c.get("a_label"), "b": c.get("b_label")}.get(who, "两个候选")
        lines.append(
            f"⚠ 裁判污染：回译引擎与「{which}」同源，该候选会拿到复读自己措辞的不当加分，"
            "本次数值不可用于选型。换一个第三方回译引擎重跑。"
        )
        lines.append("")

    af, bf = c.get("a_hard_failures", 0), c.get("b_hard_failures", 0)
    if af or bf:
        lines.append(
            f"⚠ 硬失败（压根没出译文）：A={af} / B={bf}。"
            "这是可用性问题不是质量问题，两边不等时先查端点再谈分数。"
        )
        lines.append("")

    lo, hi = (c.get("ci95") or [None, None])
    ci = f"[{lo}, {hi}]" if lo is not None else "n/a"
    lines.append(
        f"轨道={c.get('metric')}  配对样本={c.get('paired_n')}  "
        f"A({c.get('a_label')})={c.get('a_mean')}  B({c.get('b_label')})={c.get('b_mean')}"
    )
    lines.append(
        f"均值差(B-A)={c.get('mean_delta')}  95%CI={ci}  可行动效应阈={c.get('min_effect')}"
    )
    lines.append(
        f"逐样本胜负：B 胜 {c.get('b_wins')} / A 胜 {c.get('a_wins')} / 平 {c.get('ties')}"
    )
    up_a, up_b = c.get("unpaired_a", 0), c.get("unpaired_b", 0)
    if up_a or up_b:
        lines.append(f"未配对样本：A={up_a} / B={up_b}（未参与比较）")
    unscored = c.get("unscored_on_metric", 0)
    if unscored:
        total = c.get("paired_total", 0)
        lines.append(
            f"注：{unscored}/{total} 个配对样本在 {c.get('metric')} 轨上无分（嵌入缺失），"
            "未参与判据——占比过高时结论的有效样本数比看上去少。"
        )

    by_pair = c.get("by_pair") or {}
    actionable = {k: v for k, v in by_pair.items()
                  if v.get("verdict") in ("a_better", "b_better")}
    if actionable:
        lines.append("")
        lines.append("语对级差异（可用于按语对路由）：")
        for k, g in sorted(actionable.items(), key=lambda kv: kv[1]["delta"]):
            side = "B" if g["verdict"] == "b_better" else "A"
            lines.append(
                f"  {k:<12} n={g['n']:<3} A={g['a_mean']} B={g['b_mean']} "
                f"Δ={g['delta']:+.3f}  → {side} 胜"
            )

    if suggestion is not None:
        lines.append("")
        if not suggestion.get("applicable"):
            lines.append(f"per_lang_order 建议：不适用——{suggestion.get('note')}")
        elif suggestion.get("order"):
            lines.append("per_lang_order 建议（写入 translation.engines.per_lang_order）：")
            for lang, chain in sorted(suggestion["order"].items()):
                lines.append(f"  {lang}: {chain}")
        else:
            lines.append("per_lang_order 建议：无语对达到可行动差异，维持现状。")
    return "\n".join(lines)


__all__ = [
    "DEFAULT_MIN_EFFECT",
    "DEFAULT_MIN_PAIR_N",
    "DEFAULT_TIE_EPS",
    "bootstrap_ci",
    "compare",
    "format_ab_report",
    "is_hard_failure",
    "join_results",
    "judge_is_contaminated",
    "sample_key",
    "sign_counts",
    "suggest_per_lang_order",
]
