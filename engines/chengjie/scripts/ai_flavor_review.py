# -*- coding: utf-8 -*-
"""「AI 味」验收周报 CLI（活人感 P1-9/P1-10，2026-08-03，只读）。

「语音/文本还像不像 AI」从翻日志/靠耳朵变成一条命令：

    python -m scripts.ai_flavor_review [--data-root PATH] [--days 14]
                                       [--json] [--out-jsonl FILE]

读数（全部只读——inbox.db 走 mode=ro URI，对活体生产库零写风险）：
- 全部出站 + 语音切片的「AI 味」指标（开场同质化/反问率/emoji/长度/作文率，
  核心算法在 ``src/eval/ai_flavor_eval``），本窗 vs 上一窗自动环比；
- 验收目标达标表（与 2026-08-03 诊断报告验收表同源）；
- **北极星**（P1-10）：语音簇 vs 文字簇回复率（episode 口径，防连发压低）、
  入站 AI 质疑计数 + 样本（direct=直接质疑身份 / flavor=机器味投诉）；
- ``--out-jsonl`` 追加趋势行（与 proactive_trend 同哲学，周审看走势）。

多实例机自动逐根出报告（``scripts/_data_root.py`` 契约）。
"""
from __future__ import annotations

import argparse
import io
import json
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.eval.ai_flavor_eval import (
    build_verdicts,
    detect_ai_suspicion,
    episode_reply_stats,
    flavor_report,
    merge_reply_stats,
    target_checks,
    voice_mirror_text,
)

_VOICE_MEDIA = ("voice", "audio")


def _ro_conn(db_path: Path) -> Optional[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _window_slice(rows: List[sqlite3.Row], since: float,
                  until: float, *, now: Optional[float] = None) -> Dict[str, Any]:
    """一个时间窗内的全部指标（rows＝跨两窗的全量行，按窗过滤）。纯内存计算。"""
    out_texts: List[str] = []
    voice_texts: List[str] = []
    suspicion: List[Tuple[str, str]] = []
    conv_rows: Dict[str, List[Tuple[float, str, str]]] = defaultdict(list)
    for r in rows:
        ts = float(r["ts"] or 0)
        if not (since <= ts < until):
            continue
        direction = str(r["direction"] or "")
        media = str(r["media_type"] or "")
        text = str(r["text"] or "")
        conv_rows[str(r["conversation_id"])].append((ts, direction, media))
        if direction == "out":
            if media.lower() in _VOICE_MEDIA:
                vt = voice_mirror_text(text)
                if vt:
                    voice_texts.append(vt)
                    out_texts.append(vt)
            elif text.strip() and not text.lstrip().startswith("["):
                # 排除 [图片]/[语音]×N 这类占位镜像（与 2026-08-03 基线口径一致）
                out_texts.append(text.strip())
        elif direction == "in" and text.strip():
            kind = detect_ai_suspicion(text)
            if kind:
                suspicion.append((kind, text.strip()[:40]))
    reply = merge_reply_stats(
        episode_reply_stats(sorted(v), now=now) for v in conv_rows.values())
    return {
        "overall": flavor_report(out_texts),
        "voice": flavor_report(voice_texts),
        "suspicion_n": len(suspicion),
        "suspicion_samples": suspicion[:5],
        "reply": reply,
    }


def collect_review(data_root: Path, *, days: float = 14.0,
                   now: Optional[float] = None) -> Dict[str, Any]:
    """单实例数据根 → 「AI 味」验收报告 dict（只读；坏根/缺库软失败）。"""
    t = float(now if now is not None else time.time())
    since = t - float(days) * 86400.0
    prev_since = since - float(days) * 86400.0
    root = Path(data_root)
    rep: Dict[str, Any] = {
        "data_root": str(root), "ts": t, "days": float(days), "db_ok": False,
    }
    conn = _ro_conn(root / "config" / "inbox.db")
    if conn is None:
        return rep
    try:
        rows = conn.execute(
            "SELECT conversation_id, direction, COALESCE(media_type,'') AS media_type, "
            "COALESCE(text,'') AS text, ts FROM messages "
            "WHERE ts >= ? AND ts < ? ORDER BY conversation_id, ts",
            (prev_since, t)).fetchall()
    except Exception:
        return rep
    finally:
        conn.close()
    rep["db_ok"] = True
    rep["cur"] = _window_slice(rows, since, t, now=t)
    rep["prev"] = _window_slice(rows, prev_since, since)
    rep["checks"] = target_checks(rep["cur"]["overall"], rep["cur"]["voice"])
    rep["verdicts"] = build_verdicts(rep["cur"], rep["prev"])
    return rep


def _fmt_flavor(tag: str, cur: Dict[str, Any], prev: Dict[str, Any]) -> List[str]:
    """一个切片（总体/语音）的 本窗 vs 上一窗 两行渲染。"""
    def _line(rep: Dict[str, Any]) -> str:
        if not rep.get("n"):
            return "（无样本）"
        top = rep.get("top_openers") or []
        top_s = "、".join(f"{k}×{v}" for k, v in top[:3]) or "-"
        return (f"n={rep['n']:<4} 感叹开场 {rep['interjection_opener_rate']:.0%}"
                f"（笑声 {rep['laugh_opener_rate']:.0%}，Top: {top_s}） "
                f"反问 {rep['question_rate']:.0%} emoji {rep['emoji_rate']:.0%} "
                f"均长 {rep['len_avg']:.0f}/p90 {rep['len_p90']:.0f} "
                f"作文式 {rep['essay_rate']:.0%}")
    return [f"-- {tag} --",
            f"  本窗   {_line(cur)}",
            f"  上一窗 {_line(prev)}"]


def render_review(rep: Dict[str, Any]) -> str:
    if not rep.get("db_ok"):
        return f"=== AI 味验收（{rep['data_root']}）=== inbox.db 不可读，跳过"
    cur, prev = rep["cur"], rep["prev"]
    lines = [f"=== AI 味验收（{rep['data_root']}，近 {rep['days']:.0f} 天 vs 前一窗）===", ""]
    lines += _fmt_flavor("全部出站文本", cur["overall"], prev["overall"])
    lines.append("")
    lines += _fmt_flavor("语音切片（念稿文本）", cur["voice"], prev["voice"])
    lines += ["", "-- 验收目标 --"]
    for c in rep.get("checks") or []:
        mark = "达标" if c["ok"] else "未达标 ⚠"
        val = f"{c['value']:.0%}" if c["value"] <= 1.0 else f"{c['value']:.0f}"
        lines.append(f"  {c['name']:<12} {val:>6}  目标 {c['target']:<10} {mark}")
    r = cur.get("reply") or {}
    v, tx = r.get("voice") or {}, r.get("text") or {}
    lines += [
        "", "-- 北极星（episode 口径，48h 回复窗）--",
        f"  语音簇 {v.get('episodes', 0):>4} 个  回复率 {v.get('reply_rate', 0):.0%}",
        f"  文字簇 {tx.get('episodes', 0):>4} 个  回复率 {tx.get('reply_rate', 0):.0%}",
        f"  AI 质疑入站：本窗 {cur['suspicion_n']} 条 / 上一窗 {prev['suspicion_n']} 条",
    ]
    for kind, snippet in cur.get("suspicion_samples") or []:
        lines.append(f"    [{kind}] {snippet}")
    verdicts = rep.get("verdicts") or []
    if verdicts:
        lines += ["", "-- 判词 --"] + [f"  · {v}" for v in verdicts]
    return "\n".join(lines)


def trend_row(rep: Dict[str, Any]) -> Dict[str, Any]:
    """趋势 JSONL 行（键保持扁平稳定，周审画走势用）。"""
    cur = rep.get("cur") or {}
    o, v = cur.get("overall") or {}, cur.get("voice") or {}
    r = cur.get("reply") or {}
    return {
        "ts": rep.get("ts"), "days": rep.get("days"),
        "data_root": rep.get("data_root"),
        "out_n": o.get("n", 0), "voice_n": v.get("n", 0),
        "interj_rate": o.get("interjection_opener_rate", 0),
        "laugh_rate": o.get("laugh_opener_rate", 0),
        "question_rate": o.get("question_rate", 0),
        "emoji_rate": o.get("emoji_rate", 0),
        "essay_rate": o.get("essay_rate", 0),
        "len_avg": o.get("len_avg", 0), "len_p90": o.get("len_p90", 0),
        "v_len_avg": v.get("len_avg", 0), "v_len_p90": v.get("len_p90", 0),
        "v_interj_rate": v.get("interjection_opener_rate", 0),
        "suspicion_n": cur.get("suspicion_n", 0),
        "voice_ep": (r.get("voice") or {}).get("episodes", 0),
        "voice_reply_rate": (r.get("voice") or {}).get("reply_rate", 0),
        "text_ep": (r.get("text") or {}).get("episodes", 0),
        "text_reply_rate": (r.get("text") or {}).get("reply_rate", 0),
        "targets_ok": all(c["ok"] for c in (rep.get("checks") or [])) if rep.get("checks") else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    if sys.stdout and hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="AI 味验收周报（只读）")
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--days", type=float, default=14.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out-jsonl", default="", help="趋势行追加到该文件")
    args = ap.parse_args(argv)

    from scripts._data_root import resolve_data_roots
    roots = resolve_data_roots(args.data_root)
    reports = [collect_review(r, days=args.days) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(render_review(rep))
            print()
    if args.out_jsonl:
        out = Path(args.out_jsonl)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as fh:
            for rep in reports:
                if rep.get("db_ok"):
                    fh.write(json.dumps(trend_row(rep), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
