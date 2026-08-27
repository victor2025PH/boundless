# -*- coding: utf-8 -*-
"""assistant 帮助语料灌库 CLI（幂等，重跑=增量更新）。

用法（引擎根或实例数据根均可）：
    python -m scripts.seed_product_help            # dry-run：只报告将写多少条
    python -m scripts.seed_product_help --apply    # 真写库
    python -m scripts.seed_product_help --apply --probe "怎么发语音"  # 写完顺手试检索

库落点 = licensing.data_paths.config_dir()/assistant_help.db（可写数据区
SSOT；从引擎根跑而想灌生产实例库时，设 AITR_DATA_DIR 指向实例数据根）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="seed assistant product-help corpus")
    ap.add_argument("--apply", action="store_true", help="真写库（缺省 dry-run）")
    ap.add_argument("--db", default="", help="显式 db 路径（缺省走 config_dir()）")
    ap.add_argument("--probe", default="", help="灌完试检索一个问题（人工验收用）")
    args = ap.parse_args()

    from src.assistant.seed_corpus import build_all_entries

    entries = build_all_entries()
    by_src: dict[str, int] = {}
    for e in entries:
        by_src[e["source"]] = by_src.get(e["source"], 0) + 1
    print(f"生成条目 {len(entries)} 条：{by_src}")
    if not args.apply:
        print("dry-run（--apply 真写）")
        return 0

    from src.assistant.help_kb import HelpKB, get_help_kb

    kb = HelpKB(args.db) if args.db else get_help_kb()
    n = kb.upsert_entries(entries)
    print(f"已写入 {n} 条 → {kb._path}")  # noqa: SLF001 - CLI 报告落点
    print(f"库内启用条目：{kb.count()}")

    if args.probe:
        hits = kb.search(args.probe, top_k=3)
        print(f"试检索「{args.probe}」→ {len(hits)} 条：")
        for h in hits:
            print(f"  [{h['score']}] {h['title']} ({h['id']}) {h['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
