"""KB 命中准确率评测（2026-10-08 智语 · KB 可用化）。

数据：``tests/fixtures/kb_eval/kb_eval_zh_en_tl_v1.json``，zh/en/tl 三语 50 条，自带种子条目，
不碰线上库。口径与线上一致：检索走 ``KnowledgeBaseStore.search``，命中判定走
``kb_gate.judge_kb_hit``（skill_manager 查询日志同一函数）。

    python -m src.eval.kb_hit_eval                 # BM25-only 基线
    python -m src.eval.kb_hit_eval --json
    python -m src.eval.kb_hit_eval --config config/config.yaml --embed   # 接嵌入端点的混合检索

报告里的 ``legacy_hit_rate`` 是旧口径 ``bool(kb_ctx)`` 的命中率，用来对照「hits 恒等于 queries」。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_EVAL_SET = (Path(__file__).resolve().parents[2]
                    / "tests" / "fixtures" / "kb_eval" / "kb_eval_zh_en_tl_v1.json")


def load_eval_set(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else DEFAULT_EVAL_SET
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def build_seed_store(db_path: Path, data: Dict[str, Any]):
    """建一个只含评测种子条目的临时 KB（全部启用、source=user）。"""
    from src.utils.kb_store import KnowledgeBaseStore
    store = KnowledgeBaseStore(Path(db_path))
    for e in data.get("seed_entries") or []:
        store.add_entry(dict(e))
    for r in data.get("global_rules") or []:
        try:
            store.add_rule({"constraint_text": r, "scope": "global", "enabled": 1})
        except Exception:
            pass
    return store


def run_eval(
    store: Any,
    cases: List[Dict[str, Any]],
    *,
    embed_fn: Optional[Callable[[str], Optional[List[float]]]] = None,
    vec_min_sim: Optional[float] = None,
) -> Dict[str, Any]:
    from src.utils.kb_gate import DEFAULT_VEC_HIT_MIN_SIM, judge_kb_hit
    vmin = DEFAULT_VEC_HIT_MIN_SIM if vec_min_sim is None else float(vec_min_sim)
    rows = []
    for c in cases:
        q = c["q"]
        qv = embed_fn(q) if embed_fn else None
        res = store.search(q, top_k=3, lang="zh", query_vec=qv or None)
        hit, why = judge_kb_hit(q, res, vec_min_sim=vmin)
        ents = res.get("entries") or []
        top = str(ents[0].get("id")) if ents else None
        legacy = bool(store.build_ai_context_from_result(res, lang="zh"))
        exp = c.get("expect")
        ok = (hit and top == exp) if exp else (not hit)
        rows.append({"q": q, "lang": c.get("lang"), "expect": exp, "top": top,
                     "hit": hit, "why": why, "legacy_hit": legacy, "ok": ok})

    def _rate(xs, key):
        return round(sum(1 for x in xs if x[key]) / len(xs), 4) if xs else 0.0

    ins = [r for r in rows if r["expect"]]
    oos = [r for r in rows if not r["expect"]]
    by_lang: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b = by_lang.setdefault(r["lang"], {"n": 0, "ok": 0})
        b["n"] += 1
        b["ok"] += int(r["ok"])
    for b in by_lang.values():
        b["accuracy"] = round(b["ok"] / b["n"], 4)
    return {
        "n": len(rows),
        "accuracy": _rate(rows, "ok"),
        "in_scope": {"n": len(ins), "hit_recall": _rate(ins, "hit"), "top1_accuracy": _rate(ins, "ok")},
        "out_of_scope": {"n": len(oos), "false_hit_rate": _rate(oos, "hit")},
        "hit_rate": _rate(rows, "hit"),
        "legacy_hit_rate": _rate(rows, "legacy_hit"),
        "by_lang": by_lang,
        "errors": [r for r in rows if not r["ok"]],
        "mode": "hybrid" if embed_fn else "bm25",
    }


def evaluate_default(*, embed_fn=None, vec_min_sim=None, eval_set: Optional[Path] = None,
                     work_dir: Optional[Path] = None) -> Dict[str, Any]:
    """建临时种子库跑一遍。``work_dir`` 给定（如 pytest tmp_path）就建在那里，否则用临时目录
    （Windows 上 sqlite 句柄可能晚释放，清理失败不报错）。"""
    data = load_eval_set(eval_set)

    def _run(d: Path) -> Dict[str, Any]:
        store = build_seed_store(d / "kb_eval.db", data)
        if embed_fn is not None:
            _embed_seed(store, embed_fn)
        rep = run_eval(store, data["cases"], embed_fn=embed_fn, vec_min_sim=vec_min_sim)
        try:
            store._search_cache.clear()
        except Exception:
            pass
        return rep

    if work_dir is not None:
        return _run(Path(work_dir))
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        return _run(Path(td))


def _embed_seed(store: Any, embed_fn: Callable[[str], Optional[List[float]]]) -> None:
    from src.utils.kb_embed_job import build_embed_text
    for e in store.get_entries_without_embedding():
        full = store.get_entry(e["id"]) or e
        v = embed_fn(build_embed_text(full))
        if v:
            store.set_single_embedding(e["id"], v)


def _endpoint_embed_fn(config_path: Path):
    import yaml
    from src.utils.kb_embed_job import call_embedding_api
    with open(config_path, encoding="utf-8") as f:
        ai_cfg = (yaml.safe_load(f) or {}).get("ai") or {}

    def _fn(text: str):
        out = asyncio.run(call_embedding_api(ai_cfg, [text]))
        return out[0] if out else None
    return _fn


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="KB 命中准确率评测（zh/en/tl 50 条）")
    ap.add_argument("--set", default="", help="评测集 JSON（缺省自带 v1）")
    ap.add_argument("--embed", action="store_true", help="接 config 里的嵌入端点跑混合检索")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--vec-min-sim", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    fn = _endpoint_embed_fn(Path(a.config)) if a.embed else None
    rep = evaluate_default(embed_fn=fn, vec_min_sim=a.vec_min_sim,
                           eval_set=Path(a.set) if a.set else None)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"mode={rep['mode']} n={rep['n']} accuracy={rep['accuracy']:.0%} "
              f"(in-scope top1={rep['in_scope']['top1_accuracy']:.0%} recall={rep['in_scope']['hit_recall']:.0%}, "
              f"out-of-scope false_hit={rep['out_of_scope']['false_hit_rate']:.0%}) "
              f"hit_rate={rep['hit_rate']:.0%} legacy_hit_rate={rep['legacy_hit_rate']:.0%}")
        for lang, b in sorted(rep["by_lang"].items()):
            print(f"  {lang}: {b['ok']}/{b['n']} = {b['accuracy']:.0%}")
        for r in rep["errors"]:
            print(f"  ✗ [{r['lang']}] {r['q']!r} expect={r['expect']} top={r['top']} hit={r['hit']} ({r['why']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
