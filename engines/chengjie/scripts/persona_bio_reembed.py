# -*- coding: utf-8 -*-
"""长传记「向量补齐」运维 CLI（I5 线收尾，2026-07-28）。

I5 把**句向量**移到入库期预计算（表 ``persona_bio_sents``）：句级语义重排从此在查询期
零网络往返，`sentence_semantic` 才敢默认开。代价是——**开关生效只对新入库的文档**，
早于该改动入库的传记（本机 Mizuki 即是）库里没有句向量行，检索会静默退化回纯词法重排：
不报错、不告警，只是精度回到改动前。``reembed_bio_doc`` 能补，但此前既无路由也无 CLI，
等于「代码里有能力、运维摸不到」。本 CLI 就是那个手柄。

判据：只按「库里有传记」选人（``list_bio_personas``），不猜人设注册表——传记库是这件事
的单一事实源。逐人设 ``reembed_bio_doc``：块向量缺失则补，句向量整表重建（幂等，重复跑
只是多花一次 embed 预算）。嵌入端点不可用时 ``reembed_bio_doc`` 自身软失败返 None，
这里如实记 failed 而不假装成功。

用法（引擎根目录）::

    python -m scripts.persona_bio_reembed --dry-run     # 只列谁缺，不调嵌入
    python -m scripts.persona_bio_reembed               # 全量补齐
    python -m scripts.persona_bio_reembed --personas mizuki
    python -m scripts.persona_bio_reembed --json

退出码：全成 0 / 有失败 1 / 执行异常 2。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:  # Windows GBK 控制台兜底
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass


def select_personas(available: Sequence[str], wanted: Optional[Sequence[str]]) -> List[str]:
    """按 ``--personas`` 过滤；未指定＝全量。保序去重，未知 id 直接丢（不炸）。"""
    have = list(dict.fromkeys(str(p).strip() for p in available if str(p).strip()))
    if not wanted:
        return have
    want = {str(p).strip() for p in wanted if str(p).strip()}
    return [p for p in have if p in want]


def summarize(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐人设结果压成一行读数（纯函数，门禁直接吃）。"""
    ok = [r for r in results if r.get("status") == "ok"]
    failed = [r for r in results if r.get("status") == "failed"]
    return {
        "total": len(results),
        "ok": len(ok),
        "failed": len(failed),
        "failed_personas": [str(r.get("persona_id")) for r in failed],
        "chunks_embedded": sum(int(r.get("updated") or 0) for r in ok),
        "sentences_embedded": sum(int(r.get("sents_added") or 0) for r in ok),
    }


def _reembed_one(pid: str, *, bio_store: Any) -> Dict[str, Any]:
    stat = bio_store.reembed_bio_doc(pid) or {}
    if not stat:
        return {"persona_id": pid, "status": "failed"}
    return {
        "persona_id": pid,
        "status": "ok",
        "updated": int(stat.get("updated") or 0),
        "sents_added": int(stat.get("sents_added") or 0),
        "chunks": int(stat.get("chunks") or 0),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="长传记块/句向量补齐")
    ap.add_argument("--personas", default="", help="逗号分隔，默认全量")
    ap.add_argument("--dry-run", action="store_true", help="只列人设与库存，不调嵌入")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        from src.companion import persona_bio_store as bio_store
    except Exception as exc:  # pragma: no cover - 导入炸了没别的可做
        print(f"[persona_bio_reembed] 导入失败: {exc}")
        return 2

    wanted = [p for p in args.personas.split(",") if p.strip()] if args.personas else None
    targets = select_personas(bio_store.list_bio_personas(), wanted)
    if not targets:
        out = {"total": 0, "ok": 0, "failed": 0, "note": "no_bio_docs"}
        print(json.dumps(out, ensure_ascii=False) if args.as_json
              else "[persona_bio_reembed] 库里没有传记，无需补齐")
        return 0

    if args.dry_run:
        rows = [{"persona_id": p, "meta": bio_store.get_bio_meta(p) or {}} for p in targets]
        if args.as_json:
            print(json.dumps({"dry_run": True, "personas": rows}, ensure_ascii=False, indent=2))
        else:
            print(f"[persona_bio_reembed] 待补 {len(rows)} 个人设（dry-run）")
            for r in rows:
                meta = r["meta"]
                print(f"  - {r['persona_id']}: chars={meta.get('chars')} chunks={meta.get('chunks')}")
        return 0

    results: List[Dict[str, Any]] = []
    for pid in targets:
        try:
            results.append(_reembed_one(pid, bio_store=bio_store))
        except Exception as exc:
            results.append({"persona_id": pid, "status": "failed", "error": str(exc)})

    summary = summarize(results)
    if args.as_json:
        print(json.dumps({"summary": summary, "results": results},
                         ensure_ascii=False, indent=2))
    else:
        print(f"[persona_bio_reembed] {summary['ok']}/{summary['total']} 成功，"
              f"块向量 {summary['chunks_embedded']}，句向量 {summary['sentences_embedded']}")
        for r in results:
            mark = "OK " if r.get("status") == "ok" else "ERR"
            print(f"  {mark} {r.get('persona_id')}: "
                  f"chunks+{r.get('updated', 0)} sents+{r.get('sents_added', 0)}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
