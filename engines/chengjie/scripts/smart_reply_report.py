# -*- coding: utf-8 -*-
"""smart-reply 分段耗时报表 CLI（读日志，零写入）。

背景（2026-08-01）：`[smart_reply]` 日志行补齐了分段计时（gen/xlate/gloss_ms + path），
「尖峰慢在生成还是翻译」有了数据面——但读数靠人肉 grep。本 CLI 把周读固化成一条命令：

    python -m scripts.smart_reply_report [--log PATH] [--json] [--top 5]

输出：调用量/成功率、分段 p50/p90/max、生成路径分布（unified/direct/fallback）、
最慢 Top-N（带时间戳+分段归因——例如 176 宕机期的 gen=68269 一眼可见）、
旧格式（无分段）行单独计数不混入分段统计。

默认日志：按 `scripts/_data_root.resolve_data_roots()` 契约逐实例找 `logs/app.log`
（防「从引擎根跑 CLI 读到旧副本」的迁移病，见 AGENTS.md CWD 相对路径一节）。
纯函数（parse/summarize/render）可单测：tests/test_smart_reply_report.py。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# [2026-08-01 11:47:54] [INFO] ...: [smart_reply] mode=reply ok=1 gloss=0 instr=0
#   ms=68269 gen=68269 xlate=0 gloss_ms=0 path=unified conv=telegram::gate_selfcheck
_LINE_RE = re.compile(
    r"^\[(?P<ts>[\d\- :]+)\].*\[smart_reply\]\s+(?P<kv>.*)$")
_KV_RE = re.compile(r"(\w+)=(\S+)")


def parse_smart_reply_line(line: str) -> Optional[Dict[str, Any]]:
    """单行 → {ts, mode, ok, ms, gen, xlate, gloss_ms, path, conv, has_segments}。

    非 smart_reply 行返回 None；旧格式（无 gen=）返回 has_segments=False。
    数值字段解析失败按缺失处理（脏行不炸报表）。
    """
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    kv = dict(_KV_RE.findall(m.group("kv")))
    if "ms" not in kv:
        return None

    def _int(key: str) -> Optional[int]:
        try:
            return int(kv[key])
        except (KeyError, ValueError):
            return None

    ts_str = m.group("ts").strip()
    try:
        epoch = time.mktime(time.strptime(ts_str, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        epoch = 0.0
    out: Dict[str, Any] = {
        "ts": ts_str,
        "epoch": epoch,
        "mode": kv.get("mode", ""),
        "ok": kv.get("ok") == "1",
        "ms": _int("ms") or 0,
        "gen": _int("gen"),
        "xlate": _int("xlate"),
        "gloss_ms": _int("gloss_ms"),
        "path": kv.get("path", ""),
        "conv": kv.get("conv", ""),
    }
    out["has_segments"] = out["gen"] is not None
    return out


def _pct(sorted_vals: List[int], p: float) -> int:
    """线性插值分位数（sorted 输入；空列表返回 0）。"""
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    return int(round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)))


def _seg_stats(vals: List[int]) -> Dict[str, int]:
    s = sorted(vals)
    return {"n": len(s), "p50": _pct(s, 0.5), "p90": _pct(s, 0.9),
            "max": s[-1] if s else 0}


def summarize(entries: List[Dict[str, Any]], top: int = 5) -> Dict[str, Any]:
    """解析行列表 → 报表结构（纯函数）。"""
    total = len(entries)
    ok_n = sum(1 for e in entries if e["ok"])
    seg = [e for e in entries if e["has_segments"]]
    by_path: Dict[str, int] = {}
    for e in seg:
        p = e["path"] or "-"
        by_path[p] = by_path.get(p, 0) + 1
    slowest = sorted(seg, key=lambda e: -e["ms"])[:top]
    return {
        "total": total,
        "ok": ok_n,
        "ok_rate": round(ok_n / total, 3) if total else 0.0,
        "legacy_no_segments": total - len(seg),
        "segmented": len(seg),
        "ms": _seg_stats([e["ms"] for e in seg]),
        "gen": _seg_stats([e["gen"] for e in seg if e["gen"] is not None]),
        "xlate": _seg_stats([e["xlate"] for e in seg if e["xlate"] is not None]),
        "gloss": _seg_stats([e["gloss_ms"] for e in seg if e["gloss_ms"] is not None]),
        "by_path": dict(sorted(by_path.items(), key=lambda kv2: -kv2[1])),
        "slowest": [
            {"ts": e["ts"], "ms": e["ms"], "gen": e["gen"], "xlate": e["xlate"],
             "gloss_ms": e["gloss_ms"], "path": e["path"], "conv": e["conv"]}
            for e in slowest],
    }


def render_text(s: Dict[str, Any]) -> str:
    lines = [
        f"smart-reply 调用 {s['total']} 次，成功 {s['ok']}（{s['ok_rate']:.1%}）；"
        f"含分段 {s['segmented']} 次，旧格式 {s['legacy_no_segments']} 次",
    ]
    if s["segmented"]:
        for label, key in (("总耗时", "ms"), ("生成", "gen"),
                           ("翻译", "xlate"), ("对照", "gloss")):
            st = s[key]
            lines.append(
                f"  {label:>4}: p50={st['p50']}ms  p90={st['p90']}ms  max={st['max']}ms")
        path_str = " ".join(f"{k}={v}" for k, v in s["by_path"].items()) or "-"
        lines.append(f"  生成路径: {path_str}")
        lines.append("  最慢 Top:")
        for e in s["slowest"]:
            lines.append(
                f"    {e['ts']}  ms={e['ms']} (gen={e['gen']} xlate={e['xlate']}"
                f" gloss={e['gloss_ms']}) path={e['path']} conv={e['conv']}")
    else:
        lines.append("  （尚无分段数据——等下一次重启装载分段计时后再读）")
    return "\n".join(lines)


def _default_logs() -> List[Path]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from _data_root import resolve_data_roots  # type: ignore
        roots = resolve_data_roots(None)
    except Exception:
        roots = [Path.cwd()]
    out = []
    for r in roots:
        fp = Path(r) / "logs" / "app.log"
        if fp.exists():
            out.append(fp)
    return out


def filter_window(entries: List[Dict[str, Any]], days: float,
                  *, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """时间窗过滤（纯函数）：days<=0 = 全量；epoch=0（脏 ts）的行保守保留。"""
    if days <= 0:
        return list(entries)
    cutoff = float(now if now is not None else time.time()) - days * 86400.0
    return [e for e in entries if not e.get("epoch") or e["epoch"] >= cutoff]


def trend_row(s: Dict[str, Any], days: float) -> Dict[str, Any]:
    """趋势行（周批 JSONL 用）：只留聚合数字，不带 slowest（conv 键不进趋势文件）。"""
    return {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "days": days,
        "total": s["total"], "ok_rate": s["ok_rate"],
        "segmented": s["segmented"],
        "ms": s["ms"], "gen": s["gen"], "xlate": s["xlate"], "gloss": s["gloss"],
        "by_path": s["by_path"],
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="smart-reply 分段耗时报表（只读日志）")
    ap.add_argument("--log", default="", help="日志文件路径（默认按实例数据根契约自动发现）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--days", type=float, default=0,
                    help="只统计最近 N 天（0=全量；周批用 7）")
    ap.add_argument("--out-jsonl", default="",
                    help="把聚合摘要按行追加到该 JSONL（周批趋势文件）")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    logs = [Path(args.log)] if args.log else _default_logs()
    if not logs:
        print("未找到 app.log（--log 显式指定，或确认实例数据根）")
        return 2
    entries: List[Dict[str, Any]] = []
    for fp in logs:
        try:
            with open(fp, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    e = parse_smart_reply_line(line)
                    if e:
                        entries.append(e)
        except OSError as ex:
            print(f"读取失败 {fp}: {ex}")
    entries = filter_window(entries, float(args.days))
    s = summarize(entries, top=max(1, args.top))
    if args.out_jsonl:
        out_fp = Path(args.out_jsonl)
        out_fp.parent.mkdir(parents=True, exist_ok=True)
        with open(out_fp, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(trend_row(s, float(args.days)),
                                ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(s, ensure_ascii=False, indent=2))
    else:
        for fp in logs:
            print(f"# {fp}")
        print(render_text(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
