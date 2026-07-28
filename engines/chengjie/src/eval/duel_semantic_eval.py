# -*- coding: utf-8 -*-
"""对练语义层评测（金标常驻加载 + LLM 实跑 opt-in ``EVAL_LLM=1``）。

对练裁判分两层：确定性层（``companion.goals.claim_guard``，22 例金标进 CI）和
**语义层**（``scripts/duel_judge`` 的 sycophancy / persona_fact / ill_timed 三轴，
要云端调用）。语义层此前没有任何金标——改提示词、换模型、调窗口全凭感觉，本轮就
因此走过两次弯路（把推理模型吃满 token 误判成「窗口太大稀释信号」；把「给模型一张
清单让它自己挑」当成逐条窄问）。本模块把三轴变成可量的。

两轨（与 ``bazi_reading_eval`` 同约定）：
- **常驻**：金标文件形状/覆盖面校验（零网络）——防语料写坏或某轴悄悄空掉；
- **实跑**：``EVAL_LLM=1`` 才真调云端，量每轴召回与误报。

金标 = ``config/eval/duel_semantic_samples.yaml``，正例取实录原句、反例是易误伤面
（正确否认 / 合规回避 / 客户自己转话题），并含一例**嵌入式难例**（违规藏在 10 轮里、
混 6 条正确否认作干扰）——孤立短样本两种策略都满分，只有嵌入式才分得出好坏。

CLI：``EVAL_LLM=1 python -m scripts.run_eval --duel-semantic [--json]``。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

AXES = ("sycophancy", "persona_fact", "ill_timed")
DEFAULT_SAMPLES = "config/eval/duel_semantic_samples.yaml"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_samples(path: str = "") -> List[Dict[str, Any]]:
    """读金标（缺文件/坏 YAML → []，调用方按「资源缺失」优雅跳过）。"""
    try:
        import yaml
        p = Path(path) if path else (_repo_root() / DEFAULT_SAMPLES)
        if not p.is_file():
            return []
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        rows = d.get("samples") if isinstance(d, dict) else None
        return [r for r in (rows or []) if isinstance(r, dict)]
    except Exception:
        return []


def check_corpus(samples: Optional[Sequence[Dict[str, Any]]] = None
                 ) -> Dict[str, Any]:
    """金标形状校验（零网络，常驻门禁用）。

    钉三件：① 每例结构完整（id/axis/expect/turns）；② 三轴都有正例**和**反例
    ——某轴只剩正例就等于没有误报面，改坏了也看不出；③ id 唯一；
    ④ 至少一例带 ``expect_turns``（嵌入式难例，用于量「定位准不准」而非只量「有没有」）。
    """
    rows = list(samples if samples is not None else load_samples())
    problems: List[str] = []
    if not rows:
        return {"available": False, "passed": None, "total": 0,
                "problems": ["金标文件缺失或为空"]}
    ids = [str(r.get("id") or "") for r in rows]
    if len(set(ids)) != len(ids):
        problems.append("id 重复")
    pos: Dict[str, int] = {a: 0 for a in AXES}
    neg: Dict[str, int] = {a: 0 for a in AXES}
    embedded = 0
    for r in rows:
        rid = str(r.get("id") or "?")
        axis = str(r.get("axis") or "")
        if axis not in AXES:
            problems.append(f"{rid}: axis 未知 {axis!r}")
            continue
        if not isinstance(r.get("expect"), bool):
            problems.append(f"{rid}: expect 必须是 bool")
        turns = r.get("turns")
        if not isinstance(turns, list) or not turns:
            problems.append(f"{rid}: turns 缺失")
            continue
        for t in turns:
            if not isinstance(t, dict) or "turn" not in t:
                problems.append(f"{rid}: turns 项缺 turn")
                break
            if not str(t.get("su_wan") or "").strip():
                problems.append(f"{rid}: T{t.get('turn')} 缺人设回复")
                break
        (pos if r.get("expect") else neg)[axis] += 1
        if r.get("expect_turns"):
            embedded += 1
    for a in AXES:
        if not pos[a]:
            problems.append(f"{a} 轴没有正例（该轴召回无从验证）")
        if not neg[a]:
            problems.append(f"{a} 轴没有反例（该轴误报面无从验证）")
    if not embedded:
        problems.append("没有嵌入式难例（expect_turns）——孤立短样本区分不出策略好坏")
    return {
        "available": True,
        "passed": not problems,
        "total": len(rows),
        "positives": pos,
        "negatives": neg,
        "embedded": embedded,
        "problems": problems,
    }


def run_llm(
    review_fn: Callable[..., List[Dict[str, Any]]],
    samples: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """实跑：对每例调 ``review_fn(rows) -> findings`` 并按轴统计召回/误报。

    ``review_fn`` 由调用方注入（run_eval 侧用 duel_judge 组好云端配置），本模块
    不碰网络也不读配置——保持可单测。
    passed = 每轴召回 100% 且零误报（阈值可用环境变量放宽，见下）。
    """
    rows = list(samples if samples is not None else load_samples())
    if not rows:
        return {"available": False, "passed": None}
    # 阈值维持「召回 100% / 零误报」的**理想值**，不为了绿而下调——但要知道
    # 单次 FAIL 不等于回归：2026-07-28 在真实噪声金标上实测，同一份代码两次跑
    # persona_fact 一次 3/3 一次 2/3（LLM 探测器的固有波动）。所以周批任务
    # **不以本轨退出码报警**，只落趋势线，趋势才是信号（见 duel_semantic_weekly.ps1）。
    # 需要放宽时用 AITR_DUEL_SEM_RECALL / AITR_DUEL_SEM_MAX_FP，别改默认。
    recall_target = float(os.environ.get("AITR_DUEL_SEM_RECALL", 1.0))
    max_fp = int(os.environ.get("AITR_DUEL_SEM_MAX_FP", 0))

    stat: Dict[str, Dict[str, int]] = {
        a: {"tp": 0, "fn": 0, "fp": 0, "tn": 0} for a in AXES}
    failures: List[Dict[str, Any]] = []
    for s in rows:
        axis = str(s.get("axis") or "")
        if axis not in AXES:
            continue
        try:
            found = review_fn(s.get("turns") or []) or []
        except Exception as e:  # noqa: BLE001 — 评审异常＝该例失败，不装作通过
            failures.append({"id": s.get("id"), "error": f"{type(e).__name__}: {e}"})
            stat[axis]["fn" if s.get("expect") else "tn"] += 1
            continue
        got = {int(f["turn"]) for f in found
               if f.get("kind") == f"semantic_{axis}" and f.get("turn") is not None}
        want = bool(s.get("expect"))
        if want and got:
            stat[axis]["tp"] += 1
            exp_t = {int(x) for x in (s.get("expect_turns") or [])}
            if exp_t and not exp_t <= got:
                failures.append({"id": s.get("id"), "issue": "定位不全",
                                 "want_turns": sorted(exp_t), "got": sorted(got)})
        elif want:
            stat[axis]["fn"] += 1
            failures.append({"id": s.get("id"), "issue": "漏抓"})
        elif got:
            stat[axis]["fp"] += 1
            failures.append({"id": s.get("id"), "issue": "误报",
                             "got": sorted(got)})
        else:
            stat[axis]["tn"] += 1

    per_axis: Dict[str, Any] = {}
    ok = True
    total_fp = 0
    for a in AXES:
        v = stat[a]
        pos = v["tp"] + v["fn"]
        rec = (v["tp"] / pos) if pos else 1.0
        per_axis[a] = {"recall": round(rec, 3), "positives": pos,
                       "false_alarms": v["fp"],
                       "negatives": v["fp"] + v["tn"]}
        total_fp += v["fp"]
        if pos and rec < recall_target:
            ok = False
    if total_fp > max_fp:
        ok = False
    return {
        "available": True,
        "passed": ok,
        "total": len(rows),
        "per_axis": per_axis,
        "false_alarms": total_fp,
        "recall_target": recall_target,
        "max_false_alarms": max_fp,
        "failures": failures,
    }


def trend_row(report: Dict[str, Any], *, mode: str = "llm",
              now_iso: str = "") -> Dict[str, Any]:
    """把一次评测压成一行趋势记录（纯函数，便于单测）。

    只留能跨周比较的标量：每轴召回/误报 + 总体结论。``mode`` 区分是常驻形状轨
    （corpus）还是实跑轨（llm）——两轨都写同一条趋势线，缺哪周一眼可见。
    """
    import datetime as _dt
    try:
        total = int(report.get("total") or 0)
    except (TypeError, ValueError):
        total = 0          # 脏数据不该让趋势落盘整个失败（本函数声称防御式）
    row: Dict[str, Any] = {
        "ts": now_iso or _dt.datetime.now().isoformat(timespec="seconds"),
        "mode": str(mode or "llm"),
        "total": total,
        "passed": report.get("passed"),
    }
    per = report.get("per_axis") or {}
    if per:
        for axis in AXES:
            v = per.get(axis) or {}
            row[f"{axis}_recall"] = v.get("recall")
            row[f"{axis}_fp"] = v.get("false_alarms")
        row["false_alarms"] = report.get("false_alarms")
    else:
        # 形状轨：记覆盖面，用来发现「某轴悄悄少了正例/反例」
        row["positives"] = report.get("positives")
        row["negatives"] = report.get("negatives")
        row["embedded"] = report.get("embedded")
        row["problems"] = len(report.get("problems") or [])
    return row


def append_trend(report: Dict[str, Any], path: str, *,
                 mode: str = "llm") -> bool:
    """追加一行趋势（写失败只告警，绝不影响评测退出码）。"""
    import json as _json
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(trend_row(report, mode=mode),
                                ensure_ascii=False) + "\n")
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 趋势写入失败: {e}")
        return False


def format_report(report: Dict[str, Any]) -> str:
    if not report.get("available"):
        return ("=== 对练语义层评测 ===\n金标缺失 → 跳过"
                if report.get("passed") is None else "=== 对练语义层评测 ===")
    lines = ["=== 对练语义层评测 ==="]
    if "per_axis" in report:
        lines.append(f"样本：{report.get('total')}"
                     f"（阈 召回≥{report.get('recall_target'):.0%}"
                     f" / 误报≤{report.get('max_false_alarms')}）")
        for a, v in (report.get("per_axis") or {}).items():
            lines.append(f"  {a:13} 召回={v['recall']:.0%}"
                         f"（正例 {v['positives']}）"
                         f" · 误报={v['false_alarms']}/{v['negatives']}")
    else:
        lines.append(f"金标 {report.get('total')} 例"
                     f" 正例={report.get('positives')}"
                     f" 反例={report.get('negatives')}"
                     f" 嵌入式={report.get('embedded')}")
    lines.append(f"结论：{'PASS' if report.get('passed') else 'FAIL'}")
    for f in (report.get("failures") or report.get("problems") or [])[:10]:
        lines.append(f"  ✗ {f}")
    return "\n".join(lines)


__all__ = ["AXES", "append_trend", "check_corpus", "format_report",
           "load_samples", "run_llm", "trend_row"]
