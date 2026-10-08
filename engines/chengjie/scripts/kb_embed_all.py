#!/usr/bin/env python
"""启用条目 100% 向量化（2026-10-08 智语 · KB 可用化）。

默认 dry-run：只报覆盖率、待办条目和嵌入端点诊断，不调 API、不写库。
``--apply`` 才真正调用嵌入端点、把向量写进 knowledge_base.db（只补没有向量的启用条目，幂等）。

    python scripts/kb_embed_all.py --config config/config.yaml
    python scripts/kb_embed_all.py --config config/config.yaml --apply

退出码：0 = 覆盖率 100%（或 dry-run 正常完成）；2 = --apply 后仍有未向量化条目；1 = 参数 / 文件错误。
输出不含任何 key。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_ai_cfg(config_path: Path) -> dict:
    if not config_path.exists():
        return {}
    import yaml
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return dict(data.get("ai") or {})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config/config.yaml", help="智聊 config.yaml（读 ai.* 嵌入端点）")
    ap.add_argument("--db", default="", help="knowledge_base.db；缺省取 config 同目录")
    ap.add_argument("--apply", action="store_true", help="真正调用嵌入端点并写库（缺省 dry-run）")
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None, help="本次最多处理多少条（试跑用）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    a = ap.parse_args(argv)

    cfg_path = Path(a.config)
    db = Path(a.db) if a.db else cfg_path.parent / "knowledge_base.db"
    if not db.exists():
        print(f"knowledge_base.db 不存在: {db}", file=sys.stderr)
        return 1

    from src.utils.kb_embed_job import (
        call_embedding_api, describe_embedding_endpoint, embed_pending_entries,
    )
    from src.utils.kb_store import KnowledgeBaseStore

    ai_cfg = _load_ai_cfg(cfg_path)
    kb = KnowledgeBaseStore(db)
    pending = kb.get_entries_without_embedding()
    report = {
        "mode": "apply" if a.apply else "dry-run",
        "db": str(db),
        "endpoint": describe_embedding_endpoint(ai_cfg),
        "coverage": kb.embedding_coverage(),
        "pending": [{"id": e["id"], "title": (e.get("title") or "")[:40]} for e in pending],
    }
    if a.apply and pending:
        async def _embed(texts):
            return await call_embedding_api(ai_cfg, texts)
        st = asyncio.run(embed_pending_entries(kb, _embed, batch_size=a.batch_size, limit=a.limit))
        report.update({"done": st["done"], "failed": st["failed"],
                       "failed_ids": st["failed_ids"], "coverage_after": st["coverage_after"]})

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        cov = report["coverage"]
        ep = report["endpoint"]
        print(f"[{report['mode']}] {db}")
        print(f"  嵌入端点: {ep['kind']} host={ep['host']} model={ep['model']} "
              f"likely_supports_embeddings={ep['likely_supports_embeddings']}")
        if ep.get("hint"):
            print(f"  提示: {ep['hint']}")
        print(f"  覆盖率: {cov['done']}/{cov['total']} 启用条目 ({cov['pct']}%)，待向量化 {len(pending)} 条")
        for e in report["pending"][:30]:
            print(f"    - {e['id']}  {e['title']}")
        if "coverage_after" in report:
            ca = report["coverage_after"]
            print(f"  本次成功 {report['done']} 条，失败 {report['failed']} 条 → 覆盖率 {ca['done']}/{ca['total']} ({ca['pct']}%)")
            if report["failed_ids"]:
                print(f"  失败条目: {', '.join(report['failed_ids'][:20])}")

    if a.apply:
        final = report.get("coverage_after") or report["coverage"]
        return 0 if final.get("pending", 0) == 0 else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
