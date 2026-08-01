# -*- coding: utf-8 -*-
"""care LLM 影子对照周审 CLI（P4 2026-08-01）——切主判据的读数入口。

影子扫描器（``care_shadow_scan``）把每条对照写进
``<数据根>/logs/care_shadow/shadow-YYYYMMDD.jsonl``；进程内计数随重启清零，
**JSONL 才是持久口径**。本 CLI 聚合近 N 天记录，回答切主决策的三个问题：

1. LLM 比正则**多抓了多少**（llm_only）——收益面；附样本供人工复核正确率。
2. LLM **漏了正则能抓的**多少（regex_only）——风险面（切主≠关正则，正则仍在
   ingest 即时捕获，此数字只影响「LLM 单独顶班」的假设，供认知）。
3. 分语种（书写系统近似）分布——多语种召回改善是否真的发生在泰/越/西语上。

用法::

    python -m scripts.care_shadow_report                 # 自动发现实例数据根
    python -m scripts.care_shadow_report --days 7 --samples 12
    python -m scripts.care_shadow_report --json          # 机器可读

数据根解析走 ``scripts/_data_root`` 契约（CLI > env > 实例发现 > 引擎根），
多实例逐根出报告。只读，零写入。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from scripts._data_root import resolve_data_roots
except ImportError:  # 直跑（非 -m）时兜底
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts._data_root import resolve_data_roots

_DAY = 86400.0


def classify_script(text: str) -> str:
    """书写系统近似分类（够周审用，不是语种检测）：cjk/kana/hangul/thai/latin/other。

    优先级：假名 > 谚文 > 泰文 > CJK > 拉丁——「日文夹汉字」按日文归类
    （假名是日文独有信号，汉字不是）。
    """
    t = str(text or "")
    has = {"kana": False, "hangul": False, "thai": False, "cjk": False, "latin": False}
    for ch in t:
        o = ord(ch)
        if 0x3040 <= o <= 0x30FF:
            has["kana"] = True
        elif 0xAC00 <= o <= 0xD7AF:
            has["hangul"] = True
        elif 0x0E00 <= o <= 0x0E7F:
            has["thai"] = True
        elif 0x4E00 <= o <= 0x9FFF:
            has["cjk"] = True
        elif ("a" <= ch.lower() <= "z"):
            has["latin"] = True
    for k in ("kana", "hangul", "thai", "cjk", "latin"):
        if has[k]:
            return k
    return "other"


def load_records(log_dir: Path, *, days: float = 7.0,
                 now: Optional[float] = None) -> List[Dict[str, Any]]:
    """读近 N 天的 shadow-*.jsonl（坏行跳过，缺目录返回空）。"""
    n = float(now if now is not None else time.time())
    cutoff = n - float(days) * _DAY
    out: List[Dict[str, Any]] = []
    d = Path(log_dir)
    if not d.is_dir():
        return out
    for fp in sorted(d.glob("shadow-*.jsonl")):
        try:
            for line in fp.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict) and float(rec.get("ts") or 0) >= cutoff:
                    out.append(rec)
        except Exception:
            continue
    return out


def _bucket(rec: Dict[str, Any]) -> str:
    llm = rec.get("llm") or {}
    llm_found = bool(isinstance(llm, dict) and llm.get("found"))
    regex_found = bool(rec.get("regex"))
    if llm_found and regex_found:
        return "both"
    if llm_found:
        return "llm_only"
    if regex_found:
        return "regex_only"
    return "none"


def summarize(records: List[Dict[str, Any]], *, samples: int = 12) -> Dict[str, Any]:
    """聚合对照记录（纯函数）。"""
    total = len(records)
    llm_err = sum(1 for r in records if (r.get("llm") or {}).get("error"))
    buckets = {"both": 0, "llm_only": 0, "regex_only": 0, "none": 0}
    by_script: Dict[str, Dict[str, int]] = {}
    llm_only_samples: List[Dict[str, Any]] = []
    regex_only_samples: List[Dict[str, Any]] = []
    captured = 0
    for r in records:
        b = _bucket(r)
        buckets[b] += 1
        if r.get("captured"):
            captured += 1
        sc = classify_script(str(r.get("text") or ""))
        row = by_script.setdefault(sc, {"n": 0, "llm_only": 0, "regex_only": 0})
        row["n"] += 1
        if b == "llm_only":
            row["llm_only"] += 1
            if len(llm_only_samples) < samples:
                llm = r.get("llm") or {}
                llm_only_samples.append({
                    "text": str(r.get("text") or "")[:80],
                    "topic": llm.get("topic"),
                    "date": llm.get("date"),
                    "confidence": llm.get("confidence"),
                    "script": sc,
                })
        elif b == "regex_only":
            row["regex_only"] += 1
            if len(regex_only_samples) < samples:
                rx = (r.get("regex") or [{}])[0]
                regex_only_samples.append({
                    "text": str(r.get("text") or "")[:80],
                    "topic": rx.get("topic"),
                    "script": sc,
                })
    agree = buckets["both"] + buckets["none"]
    return {
        "total": total,
        "llm_err": llm_err,
        "agree": agree,
        "agree_rate": (round(agree / total, 3) if total else None),
        "both": buckets["both"],
        "llm_only": buckets["llm_only"],
        "regex_only": buckets["regex_only"],
        "none": buckets["none"],
        "captured": captured,
        "by_script": by_script,
        "llm_only_samples": llm_only_samples,
        "regex_only_samples": regex_only_samples,
    }


def render(summary: Dict[str, Any], *, root: str = "", days: float = 7.0) -> str:
    """人读报告（含切主判词指引）。"""
    s = summary
    lines: List[str] = []
    lines.append(f"== care LLM 影子对照周审  root={root}  近 {days:g} 天 ==")
    if not s["total"]:
        lines.append("  （无对照记录：影子刚开 / 无含时间信号的入站——等流量攒数据）")
        return "\n".join(lines)
    lines.append(
        f"  对照 {s['total']} 条 · 一致率 {s['agree_rate']:.0%}"
        f" (both {s['both']} / none {s['none']})"
        f" · LLM 坏输出 {s['llm_err']}")
    lines.append(
        f"  LLM 多抓 llm_only={s['llm_only']} · 正则独有 regex_only={s['regex_only']}"
        + (f" · 已入库 captured={s['captured']}" if s.get("captured") else ""))
    if s["by_script"]:
        seg = " / ".join(
            f"{k}:{v['n']}(llm_only {v['llm_only']})"
            for k, v in sorted(s["by_script"].items(), key=lambda x: -x[1]["n"]))
        lines.append(f"  分书写系统: {seg}")
    if s["llm_only_samples"]:
        lines.append("  -- llm_only 样本（人工复核这批的正确率 → 切主判据）--")
        for x in s["llm_only_samples"]:
            lines.append(
                f"    [{x['script']}] {x['text']!r} -> {x['topic']} @ {x['date']}"
                f" (conf {x['confidence']})")
    if s["regex_only_samples"]:
        lines.append("  -- regex_only 样本（LLM 漏抓，认知用）--")
        for x in s["regex_only_samples"]:
            lines.append(f"    [{x['script']}] {x['text']!r} -> {x['topic']}")
    lines.append(
        "  判词：llm_only 复核正确率 ≥80% 且样本 ≥20 → 可翻 llm_extract.enabled"
        "（overlay 热生效；先确认 health.shadow 含 captured 字段=P4 代码已装载）。")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="care LLM 影子对照周审")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    code = 0
    payload = []
    for root in resolve_data_roots(args.data_root):
        log_dir = Path(root) / "logs" / "care_shadow"
        recs = load_records(log_dir, days=args.days)
        summ = summarize(recs, samples=max(1, args.samples))
        if args.json:
            payload.append({"root": str(root), "days": args.days, **summ})
        else:
            print(render(summ, root=str(root), days=args.days))
            print()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
