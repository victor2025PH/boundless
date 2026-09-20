# -*- coding: utf-8 -*-
"""把 album_curator 产出的 manifest.json 导入 persona_media.db 注册相册。

用法：
    python tools/album_import_db.py --manifest <album>/manifest.json \
        --db <data>/config/persona_media.db

幂等：按 sha256 去重（重跑不产生重复条目）。
tags 约定：scene:<场景> / series:<服装系列> / curated（人工策展来源标记）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.companion.persona_media_store import PersonaMediaStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--db", required=True)
    args = ap.parse_args()

    mf_path = Path(args.manifest)
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    album_dir = mf_path.parent
    persona = str(mf.get("persona") or "")
    store = PersonaMediaStore(args.db)

    added = skipped = 0
    for p in mf.get("photos", []):
        sha = str(p.get("sha256") or "")
        if sha and store.find_by_sha(persona, sha):
            skipped += 1
            continue
        fp = album_dir / p["file"]
        tags = ["curated", f"scene:{p.get('scene') or 'misc'}",
                f"series:{p.get('outfit') or 'misc'}"]
        store.add(
            persona, "photo", str(fp), "",
            triggers=[t for t in (p.get("triggers") or []) if t],
            caption=str(p.get("caption") or ""),
            tags=tags, sha256=sha,
            created_by="album_curator",
        )
        added += 1

    v_added = 0
    for i, v in enumerate(mf.get("videos", []), 1):
        sha = str(v.get("sha256") or "")
        if sha and store.find_by_sha(persona, sha):
            skipped += 1
            continue
        fp = album_dir / v["file"]
        store.add(
            persona, "video", str(fp), "",
            triggers=["视频"],
            caption="", tags=["curated", f"series:video-{i:02d}"],
            sha256=sha, created_by="album_curator",
        )
        v_added += 1

    rows = store.list(persona)
    print(f"[import] persona={persona} 新增 photo={added} video={v_added} "
          f"跳过(已存在)={skipped} 库内总条目={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
