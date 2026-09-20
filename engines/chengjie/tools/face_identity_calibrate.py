# -*- coding: utf-8 -*-
"""人脸身份阈值校准（#333，只读）：从 ``visual_memory.db`` 的观察向量算分布，给 MATCH/AMBIGUOUS 定值。

    python tools/face_identity_calibrate.py [--db PATH] [--json] [--min-pairs 20]

口径：
- **同人对**：同一会话里 ``user_confirmed`` 的 customer_self 观察两两余弦（客户亲口确认过＝金标）。
- **异人对**：不同会话的带脸观察两两余弦（不同客户几乎不可能是同一人；同一坐席机多号共用
  客户的极少数例外按噪声计）。
- 建议阈：``MATCH = max(异人 P99 + 0.03, 同人 P10 − 0.05)`` 夹在 [0.40, 0.65]；``AMBIGUOUS = 异人 P95``。
样本不足（同人对 < ``--min-pairs`` 或异人对 < 200）只报分布不给建议——与 ``face_fidelity.
calibrate_fidelity_floor``「攒够再收紧」同纪律。数据库只读（``mode=ro``），多实例数据根自动发现。
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _cos(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _pct(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _discover_dbs(explicit: Optional[str]) -> List[Path]:
    if explicit:
        return [Path(explicit)]
    out: List[Path] = []
    try:
        from scripts._data_root import resolve_data_roots  # type: ignore
        for r in resolve_data_roots():
            p = Path(r) / "config" / "visual_memory.db"
            if p.is_file():
                out.append(p)
    except Exception:
        pass
    if not out:
        p = ROOT / "config" / "visual_memory.db"
        if p.is_file():
            out.append(p)
    return out


def load_observations(db: Path) -> List[Dict]:
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT conv_key, label, source, confirmed, score, embedding FROM visual_observations "
        "WHERE embedding <> ''").fetchall()
    out = []
    for r in rows:
        try:
            emb = json.loads(r["embedding"])
        except Exception:
            continue
        if isinstance(emb, list) and emb:
            out.append({"conv": r["conv_key"], "label": r["label"], "source": r["source"],
                        "confirmed": bool(r["confirmed"]), "score": float(r["score"] or 0), "emb": emb})
    con.close()
    return out


def analyze(obs: List[Dict], *, min_pairs: int = 20, max_impostor_pairs: int = 20000) -> Dict:
    by_conv: Dict[str, List[Dict]] = defaultdict(list)
    for o in obs:
        by_conv[o["conv"]].append(o)
    genuine: List[float] = []
    for conv, items in by_conv.items():
        selfs = [o["emb"] for o in items if o["label"] == "customer_self" and o["confirmed"]]
        for a, b in itertools.combinations(selfs, 2):
            genuine.append(_cos(a, b))
    impostor: List[float] = []
    convs = list(by_conv)
    for ca, cb in itertools.combinations(convs, 2):
        for oa in by_conv[ca][:4]:
            for ob in by_conv[cb][:4]:
                impostor.append(_cos(oa["emb"], ob["emb"]))
                if len(impostor) >= max_impostor_pairs:
                    break
            if len(impostor) >= max_impostor_pairs:
                break
        if len(impostor) >= max_impostor_pairs:
            break
    genuine.sort()
    impostor.sort()
    rep = {
        "observations": len(obs), "conversations": len(by_conv),
        "genuine_pairs": len(genuine), "impostor_pairs": len(impostor),
        "genuine": {k: round(_pct(genuine, q), 3) for k, q in (("p10", .1), ("p50", .5), ("min", 0.0))} if genuine else {},
        "impostor": {k: round(_pct(impostor, q), 3) for k, q in (("p95", .95), ("p99", .99), ("max", 1.0))} if impostor else {},
        "labels": dict(sorted(((o["label"], 0) for o in obs))),
    }
    for o in obs:
        rep["labels"][o["label"]] = rep["labels"].get(o["label"], 0) + 1
    enough = len(genuine) >= min_pairs and len(impostor) >= 200
    if enough:
        match = max(_pct(impostor, .99) + 0.03, _pct(genuine, .10) - 0.05)
        match = max(0.40, min(0.65, match))
        rep["suggest"] = {"MATCH_THRESHOLD": round(match, 2),
                          "AMBIGUOUS_THRESHOLD": round(min(match - 0.05, _pct(impostor, .95)), 2)}
    else:
        rep["suggest"] = None
        rep["note"] = f"样本不足（同人对 {len(genuine)}/{min_pairs}，异人对 {len(impostor)}/200）——只报分布不改阈"
    return rep


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", help="visual_memory.db 路径（缺省自动发现实例数据根）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-pairs", type=int, default=20)
    args = ap.parse_args(argv)
    dbs = _discover_dbs(args.db)
    if not dbs:
        print("no visual_memory.db found", file=sys.stderr)
        return 2
    obs: List[Dict] = []
    for db in dbs:
        obs.extend(load_observations(db))
    rep = analyze(obs, min_pairs=args.min_pairs)
    rep["dbs"] = [str(d) for d in dbs]
    try:
        from src.companion import visual_identity as vi
        rep["current"] = {"MATCH_THRESHOLD": vi.MATCH_THRESHOLD, "AMBIGUOUS_THRESHOLD": vi.AMBIGUOUS_THRESHOLD}
    except Exception:
        pass
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(f"db: {', '.join(rep['dbs'])}")
        print(f"observations={rep['observations']} conversations={rep['conversations']} labels={rep['labels']}")
        print(f"genuine pairs={rep['genuine_pairs']} {rep['genuine']}")
        print(f"impostor pairs={rep['impostor_pairs']} {rep['impostor']}")
        print(f"current={rep.get('current')}  suggest={rep['suggest']}  {rep.get('note', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
