"""相册语义召回·影子周审 CLI（实施90 阶段二）——只读汇总 shadow jsonl。

影子模式只记不发（logs/album_recall_shadow/*.jsonl）；本工具把窗口内命中
汇总成「样本量 / 按人设分布 / 余弦分布 / Top 样本 / 判词」，回答一个问题：
**语义召回值不值得升真发档**。零写入，退出码恒 0。

  python tools/album_recall_report.py                       # 当前目录数据根
  python tools/album_recall_report.py --dir D:/chengjie-instances/zhiliao/data/logs/album_recall_shadow
  python tools/album_recall_report.py --days 7 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.companion.album_semantic_recall import (  # noqa: E402
    resolve_semantic_recall_cfg,
    summarize_shadow_lines,
)


def _load_lines(d: Path):
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.jsonl")):
        try:
            for ln in f.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
        except Exception:
            continue
    return out


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="相册语义召回影子周审（只读）")
    ap.add_argument("--dir", default="logs/album_recall_shadow",
                    help="shadow jsonl 目录（生产=实例数据根下 logs/album_recall_shadow）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    lines = _load_lines(Path(args.dir))
    min_cos = resolve_semantic_recall_cfg(None)["min_cosine"]
    rep = summarize_shadow_lines(lines, days=args.days, min_cosine=min_cos)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"=== 语义召回影子周审（近 {rep['window_days']} 天，dir={args.dir}）===")
    print(f"命中 {rep['n']} 次 · 覆盖 {rep['days_covered']} 天 · "
          f"涉及素材 {rep['distinct_media']} 张")
    if rep["n"]:
        c = rep["cosine"]
        print(f"余弦: min {c['min']} / p50 {c['p50']} / p90 {c['p90']} / max {c['max']}")
        print("按人设: " + ", ".join(f"{k} {v}" for k, v in rep["by_persona"].items()))
        print("Top 样本:")
        for t in rep["top"]:
            print(f"  {t['cosine']:.3f}  「{t['text']}」 → {t['media_id'][:8]} "
                  f"({t['desc']})")
    for v in rep["verdict"]:
        print("→ " + v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
