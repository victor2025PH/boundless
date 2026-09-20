# -*- coding: utf-8 -*-
"""翻译记忆一次性清洗：找出被 LLM 引擎写进 ``translation_memory.db`` 的「引擎拒绝
话术」坏译文（#176，2026-09-05 skuio 实录：「Exactly」→「好的，请发送需要翻译的内容。」）。

背景：``looks_like_engine_refusal``（#115，0831）只挂在引擎新译分支；0831 之前入库
的坏译文每次命中记忆都原样吐出、永不重验。服务层现已在命中路径补了同一判据
（命中即删 + 重译），本 CLI 负责**存量一次性清洗**——不等它们一条条被撞到。

用法（默认 dry-run，只列不删）：
    python tools/xlate_memory_purge.py                 # 自动发现实例数据根，逐根扫描
    python tools/xlate_memory_purge.py --data-root D:\\chengjie-instances\\zhiliao\\data
    python tools/xlate_memory_purge.py --db path\\to\\translation_memory.db --json
    python tools/xlate_memory_purge.py --apply         # 真删（先 dry-run 看清单）

纪律：dry-run 用 ``mode=ro`` URI **只读**打开生产库（零写事务、不改 hit_count）；
``--apply`` 才走 ``TranslationMemoryStore.delete_many``（与服务层同一路径）。
数据根按 ``scripts/_data_root`` 契约（CLI → AITR_DATA_ROOT → 实例自动发现 → 引擎根），
库路径按 web_app 装配同口径：``translation.memory.db_path``（相对=相对 config 目录）
或 ``<root>/config/translation_memory.db``。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.ai.translation_confidence import looks_like_engine_refusal  # noqa: E402


def memory_db_path(root: Path) -> Path:
    """与 ``src/bootstrap/web_app.py`` 装配同口径解析记忆库路径。"""
    cfg_dir = Path(root) / "config"
    try:
        cfg = load_merged_config(Path(root)) or {}
    except Exception:
        cfg = {}
    tr = (cfg.get("translation") or {}) if isinstance(cfg, dict) else {}
    mem = (tr.get("memory") or {}) if isinstance(tr, dict) else {}
    raw = str(mem.get("db_path") or "").strip()
    p = Path(raw) if raw else (cfg_dir / "translation_memory.db")
    if not p.is_absolute():
        p = cfg_dir / p
    return p


def scan_db(db_path: Path, *, limit: int = 0) -> List[Dict[str, Any]]:
    """只读扫描：返回命中「引擎拒绝话术」的记忆行（不改库、不改 hit_count）。"""
    if not Path(db_path).is_file():
        return []
    uri = "file:%s?mode=ro" % Path(db_path).resolve().as_posix()
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT cache_key, source_text, translated_text, source_lang, "
               "target_lang, engine, hit_count, created_at, last_hit_at "
               "FROM translation_memory")
        if limit and int(limit) > 0:
            sql += " LIMIT %d" % int(limit)
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()
    bad: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if looks_like_engine_refusal(d.get("source_text") or "",
                                     d.get("translated_text") or ""):
            bad.append(d)
    return bad


def purge_db(db_path: Path, keys: List[str]) -> int:
    """真删：走服务层同一 store 路径。"""
    if not keys:
        return 0
    from src.ai.translation_memory import TranslationMemoryStore
    st = TranslationMemoryStore(db_path)
    try:
        return st.delete_many(keys)
    finally:
        st.close()


def _row_brief(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": str(d.get("cache_key") or "")[:12],
        "src": str(d.get("source_text") or "")[:40],
        "out": str(d.get("translated_text") or "")[:60],
        "lang": "%s->%s" % (d.get("source_lang"), d.get("target_lang")),
        "engine": d.get("engine"),
        "hits": int(d.get("hit_count") or 0),
    }


def run(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-root", default="", help="实例数据根（可省略，自动发现）")
    ap.add_argument("--db", default="", help="直接指定 translation_memory.db（跳过数据根解析）")
    ap.add_argument("--apply", action="store_true", help="真删（默认 dry-run 只列）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--limit", type=int, default=0, help="最多扫描多少行（0=全部）")
    args = ap.parse_args(argv)

    targets: List[Path] = []
    if args.db:
        targets = [Path(args.db)]
    else:
        for root in resolve_data_roots(args.data_root):
            targets.append(memory_db_path(root))

    report: Dict[str, Any] = {"apply": bool(args.apply), "dbs": []}
    total_bad = 0
    total_deleted = 0
    for db in targets:
        entry: Dict[str, Any] = {"db": str(db), "exists": db.is_file(), "bad": 0,
                                 "deleted": 0, "samples": []}
        if db.is_file():
            bad = scan_db(db, limit=args.limit)
            entry["bad"] = len(bad)
            entry["samples"] = [_row_brief(d) for d in bad[:20]]
            total_bad += len(bad)
            if args.apply and bad:
                n = purge_db(db, [str(d["cache_key"]) for d in bad])
                entry["deleted"] = n
                total_deleted += n
        report["dbs"].append(entry)
    report["total_bad"] = total_bad
    report["total_deleted"] = total_deleted

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        mode = "APPLY（已删）" if args.apply else "DRY-RUN（只列不删）"
        print("=== 翻译记忆坏译文清洗 %s ===" % mode)
        for e in report["dbs"]:
            if not e["exists"]:
                print("- %s : 库不存在，跳过" % e["db"])
                continue
            print("- %s : 命中 %d 条%s" % (
                e["db"], e["bad"],
                ("，已删 %d" % e["deleted"]) if args.apply else ""))
            for s in e["samples"]:
                print("    [%s] %s | %r -> %r (hits=%d, %s)" % (
                    s["key"], s["lang"], s["src"], s["out"], s["hits"], s["engine"]))
            if e["bad"] > len(e["samples"]):
                print("    ... 余 %d 条未展示" % (e["bad"] - len(e["samples"])))
        print("总计：命中 %d 条%s" % (
            total_bad, ("，已删 %d" % total_deleted) if args.apply else
            "（加 --apply 真删）"))
    return 0


if __name__ == "__main__":
    sys.exit(run())
