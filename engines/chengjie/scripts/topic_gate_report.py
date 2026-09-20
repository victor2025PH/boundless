# -*- coding: utf-8 -*-
"""今日话题包分类审阅 CLI —— 词表校准的读数入口（P2 2026-08-04）。

把各实例数据根的 ``config/daily_topics_cache.json`` 里每条话题过一遍
``smalltalk_verdict``，输出「放行/否决 + 命中词」表：

- 看 DROP 行的 ``veto_hits``：否决对不对？错杀的轻话题 → 从 HEAVY_VETO_WORDS
  里收词；
- 看 PASS 行的 ``light_hits``：放行凭什么？可疑命中（媒体品牌名点亮轻词那类）
  → 轻词表剔词 / 否决表补词；
- 漏判/误判的真实标题请顺手补进 ``tests/test_daily_topics.py`` 的事故金标，
  词表校准才能只紧不松（与翻译弱语对周审同哲学：读数决策，不拍脑袋）。

用法::

    python -m scripts.topic_gate_report [--data-root PATH] [--json]

数据根解析走 ``scripts/_data_root`` 契约（CLI 值 → AITR_DATA_ROOT →
实例自动发现 → 引擎根）；缓存缺失的根静默跳过（不是每台机都开话题包）。
只读，零写入。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.companion.daily_topics import smalltalk_verdict  # noqa: E402


def report_for_cache(cache_path: Path) -> Dict[str, Any]:
    """单个缓存文件 → 分类报告 dict（缺失/坏文件返回 ``{"ok": False}``）。"""
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "cache": str(cache_path)}
    topics = [t for t in (data.get("topics") or []) if isinstance(t, dict)]
    fetched_ts = float(data.get("fetched_ts") or 0.0)
    rows: List[Dict[str, Any]] = []
    for t in topics:
        v = smalltalk_verdict(t)
        rows.append({
            "title": str(t.get("title") or "")[:120],
            "ok": bool(v["ok"]),
            "veto_hits": list(v["veto_hits"]),
            "light_hits": list(v["light_hits"]),
            "published_ts": float(t.get("published_ts") or 0.0),
        })
    return {
        "ok": True,
        "cache": str(cache_path),
        "fetched_ts": fetched_ts,
        "total": len(rows),
        "passed": sum(1 for r in rows if r["ok"]),
        "rows": rows,
    }


def collect_reports(data_root_cli: str = "") -> List[Dict[str, Any]]:
    """逐数据根收集报告（缓存缺失的根跳过）。"""
    out: List[Dict[str, Any]] = []
    for root in resolve_data_roots(data_root_cli):
        cache = Path(root) / "config" / "daily_topics_cache.json"
        if not cache.exists():
            continue
        rep = report_for_cache(cache)
        rep["data_root"] = str(root)
        out.append(rep)
    return out


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return "?"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def render_text(reports: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    if not reports:
        return "（没有任何数据根有话题缓存——daily_topics 未启用或还没抓过）"
    for rep in reports:
        if not rep.get("ok"):
            lines.append(f"[坏缓存] {rep.get('cache')}")
            continue
        lines.append(
            f"== {rep['data_root']}  抓取于 {_fmt_ts(rep['fetched_ts'])}  "
            f"放行 {rep['passed']}/{rep['total']} ==")
        for r in rep["rows"]:
            tag = "PASS" if r["ok"] else "DROP"
            why = ("veto:" + ",".join(r["veto_hits"][:4])) if r["veto_hits"] \
                else ("light:" + ",".join(r["light_hits"][:4])
                      if r["light_hits"] else "no-light-hit")
            lines.append(f"  {tag}  {r['title'][:52]}  [{why}]")
        lines.append("")
    return "\n".join(lines).rstrip()


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="今日话题包分类审阅（只读）")
    ap.add_argument("--data-root", default="", help="显式数据根（默认按契约自动发现）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args(argv)
    reports = collect_reports(args.data_root)
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=1))
    else:
        print(render_text(reports))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
