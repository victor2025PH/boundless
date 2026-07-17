#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_triage.py — 崩溃簇 → 结构化归因报告 + 修复草案入口（AI 升级闭环脚手架，2026-07-13 P2）。

闭环形态：用户报错 → ingest 聚簇 → 本工具每日拉"新增/激增簇" → 为每簇定位到项目源码
（栈签名里就带 文件:函数:行号）→ 产出结构化归因报告（Markdown）；报告即"喂给 AI agent
产修复 PR 草案"的输入。默认只出报告（人审后再让 agent 改代码 + 走 app 增量热修发布）。

设计：
  * 纯标准库；只读 ingest 的 /t/stats（或本地 agg.sqlite），不改生产。
  * 栈签名 `文件:函数:行号|...#ExcType` → 反查本机源码对应行 + 上下文（就近可读）。
  * 输出 logs/ai_triage/<date>.md：每簇一节（服务/异常/次数/版本/源码定位/样例/建议动作）。
  * --emit-tasks 追加机器可读 tasks.jsonl（供自动化 agent 逐条消费 → 产 patch → 人审）。

用法：
  python tools/ai_triage.py --stats-url https://usdt2026.cc/t/stats --token <T>
  python tools/ai_triage.py --sqlite ~/avatarhub-telemetry/agg.sqlite   # 服务端本地直读
  python tools/ai_triage.py ... --emit-tasks
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
OUT_DIR = HERE / "logs" / "ai_triage"
SIG_RE = re.compile(r"([\w.\-]+\.py):([^:|#]+):(\d+)")


def _fetch_clusters_url(url: str, token: str) -> list[dict]:
    full = url + ("&" if "?" in url else "?") + "token=" + token
    with urllib.request.urlopen(full, timeout=15) as r:
        return json.loads(r.read().decode("utf-8")).get("clusters", [])


def _fetch_clusters_sqlite(db_path: str) -> list[dict]:
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT service,exc,kind,count,versions,sample_msg,sig,first_ts,last_ts "
                      "FROM clusters ORDER BY count DESC LIMIT 100").fetchall()
    out = []
    for r in rows:
        out.append({"service": r[0], "exc": r[1], "kind": r[2], "count": r[3],
                    "versions": json.loads(r[4] or "[]"), "msg": r[5], "sig": r[6],
                    "first": r[7], "last": r[8]})
    return out


def _locate_source(sig: str) -> list[dict]:
    """从栈签名反查项目源码：返回最深若干帧 [{file,func,line,snippet}]（仅本机存在的源文件）。"""
    frames = []
    for m in SIG_RE.finditer(sig or ""):
        fname, func, line = m.group(1), m.group(2), int(m.group(3))
        # 项目根内同名文件（栈签名只留文件名，天然无 PII）
        cands = list(HERE.glob(fname)) + list(HERE.glob(f"**/{fname}"))
        snippet = ""
        loc = ""
        for c in cands[:1]:
            try:
                lines = c.read_text(encoding="utf-8", errors="replace").splitlines()
                lo, hi = max(0, line - 4), min(len(lines), line + 3)
                snippet = "\n".join(f"{'>' if i + 1 == line else ' '}{i+1:5d}| {lines[i]}"
                                    for i in range(lo, hi))
                loc = str(c.relative_to(HERE))
            except Exception:
                pass
        frames.append({"file": fname, "func": func, "line": line, "loc": loc, "snippet": snippet})
    return frames


def triage(clusters: list[dict], only_errors=True) -> list[dict]:
    items = []
    for c in clusters:
        if only_errors and c.get("kind") not in ("crash", "error"):
            continue
        frames = _locate_source(c.get("sig", ""))
        located = next((f for f in reversed(frames) if f.get("loc")), None)
        items.append({**c, "frames": frames, "primary": located})
    # 按次数降序
    items.sort(key=lambda x: x.get("count", 0), reverse=True)
    return items


def write_report(items: list[dict], emit_tasks: bool) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y%m%d")
    md = OUT_DIR / f"{day}.md"
    lines = [f"# 崩溃归因报告 {day}", "",
             f"> 自动生成（tools/ai_triage.py）；共 {len(items)} 个错误簇。人审后择需喂 AI agent 产修复草案。", ""]
    for i, it in enumerate(items, 1):
        p = it.get("primary") or {}
        lines += [f"## {i}. {it.get('exc','?')} @ {it.get('service','?')}（{it.get('count',0)} 次）",
                  f"- 版本分布：{', '.join(it.get('versions') or []) or '未知'}",
                  f"- 栈签名：`{it.get('sig','')}`",
                  f"- 样例(已脱敏)：{it.get('msg','') or '（无）'}",
                  f"- 源码定位：{('`'+p['loc']+'`:'+str(p['line'])) if p.get('loc') else '未在本机源码命中（可能是三方库/环境）'}"]
        if p.get("snippet"):
            lines += ["", "```", p["snippet"], "```"]
        lines += [f"- 建议动作：{'定位到项目源码，可交 agent 产修复补丁 → 人审 → app 增量热修' if p.get('loc') else '先判断是否环境/三方问题；必要时加防御性 try 或升级依赖'}", ""]
    md.write_text("\n".join(lines), encoding="utf-8")
    if emit_tasks:
        tasks = OUT_DIR / "tasks.jsonl"
        # 影响面权重排序：次数 × 版本跨度（跨多版本=系统性问题，优先级更高）；近 24h 仍在发的加权。
        def _weight(it):
            cnt = it.get("count", 0) or 0
            nver = len(it.get("versions") or []) or 1
            recent = 1.5 if (int(time.time()) - int(it.get("last", 0) or 0) < 86400) else 1.0
            return cnt * (1 + 0.3 * (nver - 1)) * recent
        ranked = sorted([it for it in items if (it.get("primary") or {}).get("loc")],
                        key=_weight, reverse=True)
        with open(tasks, "a", encoding="utf-8") as f:
            for rank, it in enumerate(ranked, 1):
                p = it.get("primary") or {}
                f.write(json.dumps({
                    "ts": int(time.time()), "exc": it.get("exc"), "service": it.get("service"),
                    "count": it.get("count"), "sig": it.get("sig"),
                    "versions": it.get("versions") or [], "first": it.get("first"), "last": it.get("last"),
                    "file": p.get("loc"), "line": p.get("line"),
                    "weight": round(_weight(it), 1), "priority_rank": rank,
                    "sample": it.get("msg", ""),
                    "task": f"修复 {it.get('exc')}：{p.get('loc')}:{p.get('line')}"
                            f"（{it.get('count')} 次/{len(it.get('versions') or [])} 版本，权重 {round(_weight(it),1)}）",
                }, ensure_ascii=False) + "\n")
    return md


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats-url", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--sqlite", default="")
    ap.add_argument("--emit-tasks", action="store_true")
    args = ap.parse_args()
    if args.sqlite:
        clusters = _fetch_clusters_sqlite(args.sqlite)
    elif args.stats_url:
        clusters = _fetch_clusters_url(args.stats_url, args.token)
    else:
        print("需 --stats-url 或 --sqlite"); return 2
    items = triage(clusters)
    md = write_report(items, args.emit_tasks)
    print(f"[ai_triage] {len(items)} 簇 → {md}")
    for it in items[:8]:
        p = it.get("primary") or {}
        print(f"  · {it.get('exc'):24s} x{it.get('count'):<4} {p.get('loc','?')}:{p.get('line','')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
