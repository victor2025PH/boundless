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


_COV_SQL_TOTAL = "SELECT COUNT(*) FROM kb_entries WHERE enabled=1"
_HAS_VEC = ("embedding IS NOT NULL AND TRIM(IFNULL(embedding, '')) != '' "
            "AND TRIM(IFNULL(embedding, '')) != '[]'")


def ro_coverage(db: Path) -> dict:
    """dry-run 专用：sqlite ``mode=ro`` 只读打开，口径与 KnowledgeBaseStore.embedding_coverage 一致。

    不实例化 KnowledgeBaseStore（它会建表 / 迁移 / 开 WAL，属于写操作），线上库预演只走这里。
    只返回计数，不返回条目正文。
    """
    import sqlite3
    uri = "file:" + Path(db).resolve().as_posix() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        total = conn.execute(_COV_SQL_TOTAL).fetchone()[0]
        done = conn.execute(_COV_SQL_TOTAL + " AND " + _HAS_VEC).fetchone()[0]
        all_rows = conn.execute("SELECT COUNT(*) FROM kb_entries").fetchone()[0]
        by_cat = {}
        try:
            for cat, n_all, n_done in conn.execute(
                "SELECT IFNULL(category,''), COUNT(*), SUM(CASE WHEN " + _HAS_VEC + " THEN 1 ELSE 0 END) "
                "FROM kb_entries WHERE enabled=1 GROUP BY IFNULL(category,'')"
            ):
                by_cat[cat or "(none)"] = {"total": int(n_all), "done": int(n_done or 0)}
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    return {"total": int(total), "done": int(done), "pending": int(total - done),
            "pct": round(done / total * 100, 1) if total else 0,
            "all_entries": int(all_rows), "disabled": int(all_rows - total),
            "by_category": by_cat, "opened": "mode=ro"}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config/config.yaml", help="智聊 config.yaml（读 ai.* 嵌入端点）")
    ap.add_argument("--db", default="", help="knowledge_base.db；缺省取 config 同目录")
    ap.add_argument("--apply", action="store_true", help="真正调用嵌入端点并写库（缺省 dry-run）")
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--limit", type=int, default=None, help="本次最多处理多少条（试跑用）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    ap.add_argument("--summary", action="store_true", help="只出计数，不列待向量化条目标题")
    a = ap.parse_args(argv)

    cfg_path = Path(a.config)
    db = Path(a.db) if a.db else cfg_path.parent / "knowledge_base.db"
    if not db.exists():
        print(f"knowledge_base.db 不存在: {db}", file=sys.stderr)
        return 1

    from src.utils.kb_embed_job import describe_embedding_endpoint

    ai_cfg = _load_ai_cfg(cfg_path)
    if not a.apply:
        # dry-run：只读打开，不碰 KnowledgeBaseStore
        cov = ro_coverage(db)
        report = {"mode": "dry-run", "db": str(db),
                  "endpoint": describe_embedding_endpoint(ai_cfg), "coverage": cov, "pending": []}
        if not a.summary:
            import sqlite3
            conn = sqlite3.connect("file:" + db.resolve().as_posix() + "?mode=ro", uri=True)
            try:
                rows = conn.execute("SELECT id, title FROM kb_entries WHERE enabled=1 AND NOT ("
                                    + _HAS_VEC + ")").fetchall()
            finally:
                conn.close()
            report["pending"] = [{"id": r[0], "title": (r[1] or "")[:40]} for r in rows]
        pending = report["pending"] if not a.summary else [None] * cov["pending"]
    else:
        from src.utils.kb_embed_job import call_embedding_api, embed_pending_entries
        from src.utils.kb_store import KnowledgeBaseStore
        kb = KnowledgeBaseStore(db)
        pending = kb.get_entries_without_embedding()
        report = {
            "mode": "apply",
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
