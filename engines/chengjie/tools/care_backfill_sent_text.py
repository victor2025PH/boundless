# -*- coding: utf-8 -*-
"""存量回填：把 deferred 队列里尚未过保留期的 care 话术补进 care_schedule.sent_text。

背景（P3 2026-08-03）：P2 起新派发会随行写话术快照，但历史 sent 行的快照为空；
deferred 队列有终态清理保留期（默认 7 天），趁老队列行还在，把话术一次性搬回
care 表——之后队列清理不再影响审计。

安全性：
- 默认 **dry-run** 只报告，``--apply`` 才写；
- 只补**空**快照（``backfill_sent_text`` 店内守卫：已有快照/非 sent 行拒绝，幂等）；
- 只认 care 指纹对得上的队列行（extra.care=True 且 care_id 与行一致），
  与线上读侧同一隐私边界；
- 数据根走 ``scripts/_data_root`` 契约（--data-root → AITR_DATA_ROOT →
  自动发现活跃实例 → 引擎根），多实例逐根跑、各回各家。

用法：
    python tools/care_backfill_sent_text.py            # 逐实例 dry-run 报告
    python tools/care_backfill_sent_text.py --apply    # 真写
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.contacts.care_schedule import CareScheduleStore  # noqa: E402
from src.integrations.shared.deferred_outbox import DeferredOutboxStore  # noqa: E402
from src.web.routes.care_routes import _deferred_id_from_note  # noqa: E402


def backfill_root(root: Path, *, apply: bool = False) -> dict:
    """单个数据根的回填。返回统计（dry-run 时 filled=将会补的条数）。"""
    cfg = Path(root) / "config"
    out = {"root": str(root), "candidates": 0, "filled": 0, "skipped": 0}
    care_db = cfg / "care_schedule.db"
    dof_db = cfg / "deferred_outbox.db"
    if not care_db.is_file() or not dof_db.is_file():
        out["skip_reason"] = "db_missing"
        return out
    care = CareScheduleStore(care_db)
    dof = DeferredOutboxStore(dof_db)
    try:
        rows = care.list_history(status="sent", limit=500)
        targets = {}
        for r in rows:
            if str(r.get("sent_text") or ""):
                continue  # 已有快照，不覆盖
            did = _deferred_id_from_note(str(r.get("note") or ""))
            if did > 0:
                targets[did] = r
        out["candidates"] = len(targets)
        if not targets:
            return out
        drows = dof.get_by_ids(list(targets))
        for did, r in targets.items():
            d = drows.get(did)
            if not d:
                out["skipped"] += 1  # 队列行已被保留期清理，无从回填
                continue
            try:
                extra = json.loads(str(d.get("extra") or "{}")) or {}
            except Exception:
                extra = {}
            if not (bool(extra.get("care"))
                    and int(extra.get("care_id") or 0) == int(r.get("id") or 0)):
                out["skipped"] += 1  # 指纹对不上：宁可不回填
                continue
            text = str(d.get("reply_text") or "").strip()
            if not text:
                out["skipped"] += 1
                continue
            if apply:
                if care.backfill_sent_text(int(r["id"]), text):
                    out["filled"] += 1
                else:
                    out["skipped"] += 1
            else:
                out["filled"] += 1  # dry-run：将会补
    finally:
        care.close()
        dof.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="", help="显式数据根（缺省按契约自动发现）")
    ap.add_argument("--apply", action="store_true", help="真写（缺省 dry-run 只报告）")
    args = ap.parse_args()
    total = 0
    for root in resolve_data_roots(args.data_root):
        rep = backfill_root(Path(root), apply=args.apply)
        tag = "[APPLY]" if args.apply else "[DRY]"
        print(tag + " " + json.dumps(rep, ensure_ascii=True))
        total += int(rep.get("filled") or 0)
    print(("backfilled=" if args.apply else "would_backfill=") + str(total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
