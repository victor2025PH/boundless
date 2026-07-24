# -*- coding: utf-8 -*-
"""相册系列精修：合并 VLM 打标漂移产生的"同套衣服不同系列名"。

问题（album_curator 实测）：同套衣服 VLM 两次措辞不同 →
``black-dress-purple-cardi`` 与 ``purple-cardigan-black-dr`` 被拆成两个系列，
系列排除失效（发过其一再发其二=客户看到同套衣服两张，复读感）。

规则（确定性、可重跑）：
1) token 化系列名（跳过截断残词），两系列 token Jaccard ≥ 阈值 → 合并（并入张数多/名字长的）；
2) 非服装杂词（hair/selfie/night/mask/outfit 等 VLM 跑偏词）→ 该图场景就近或 misc-N 独立系列。

用法：
    python tools/album_refine.py --manifest <album>/manifest.json --db <config>/persona_media.db [--dry]
更新 manifest.json（seri­es 字段可追溯：refined_from）+ DB tags（series:xxx 替换）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 非服装信号词：VLM 偶发用发型/场景/泛词当 outfit
_JUNK_TOKENS = {"hair", "selfie", "night", "mask", "outfit", "photo", "misc"}
# 颜色与款式同义归一（提升 Jaccard 命中）
_SYNONYM = {
    "cardi": "cardigan", "dre": "dress", "d": "", "trouser": "trousers",
    "legging": "leggings", "short": "shorts", "tshirt": "t-shirt",
}


def _tokens(series: str) -> set:
    toks = set()
    for t in str(series or "").split("-"):
        t = t.strip().lower()
        if not t or len(t) <= 1:      # 截断残词（如 "-d"）不参与
            continue
        toks.add(_SYNONYM.get(t, t))
    return {t for t in toks if t}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def refine_series(photos: list, *, threshold: float = 0.5) -> dict:
    """返回 {旧系列: 新系列}（不含恒等映射）。纯函数可测。"""
    from collections import Counter
    counts = Counter(str(p.get("outfit") or "") for p in photos)
    names = [n for n in counts if n]
    mapping: dict = {}

    # 1) 杂词系列 → 按该系列图的场景改名（junk 不参与相似合并）
    junk = [n for n in names
            if _tokens(n) and _tokens(n) <= _JUNK_TOKENS or not _tokens(n)]
    for n in junk:
        scenes = [str(p.get("scene") or "misc") for p in photos
                  if str(p.get("outfit") or "") == n]
        mapping[n] = f"misc-{scenes[0] if scenes else 'unknown'}"

    # 2) 相似合并（贪心：张数降序，被并入者指向代表者）
    rest = sorted((n for n in names if n not in mapping),
                  key=lambda n: (-counts[n], -len(n)))
    reps: list = []
    for n in rest:
        tk = _tokens(n)
        merged = False
        for r in reps:
            if _jaccard(tk, _tokens(r)) >= threshold:
                mapping[n] = r
                merged = True
                break
        if not merged:
            reps.append(n)
    return {k: v for k, v in mapping.items() if k != v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    mf_path = Path(args.manifest)
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    photos = mf.get("photos", [])
    mapping = refine_series(photos, threshold=args.threshold)
    if not mapping:
        print("[refine] 无需合并")
        return 0
    for old, new in sorted(mapping.items()):
        print(f"[refine] {old}  →  {new}")
    if args.dry:
        return 0

    # manifest 更新（保留 refined_from 追溯）
    for p in photos:
        o = str(p.get("outfit") or "")
        if o in mapping:
            p["refined_from"] = o
            p["outfit"] = mapping[o]
    mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    # DB tags 更新（series:old → series:new，按 sha256 定位行）
    from src.companion.persona_media_store import PersonaMediaStore
    store = PersonaMediaStore(args.db)
    persona = str(mf.get("persona") or "")
    updated = 0
    for p in photos:
        old = p.get("refined_from")
        if not old:
            continue
        row = store.find_by_sha(persona, str(p.get("sha256") or ""))
        if not row:
            continue
        tags = [t for t in (row.get("tags") or []) if not str(t).startswith("series:")]
        tags.append(f"series:{p['outfit']}")
        store.update(str(row["id"]), tags=tags)
        updated += 1
    print(f"[refine] 合并 {len(mapping)} 个系列名，DB 更新 {updated} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
