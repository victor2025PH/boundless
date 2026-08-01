# -*- coding: utf-8 -*-
"""流失预警全量榜冷启动：把 inbox 存量私聊回放成 contacts 旅程（RH-P2）。

用法（引擎根运行；默认 dry-run 只出统计，人审后再 --apply）::

    python -m scripts.relations_seed_journeys                # 全实例 dry-run
    python -m scripts.relations_seed_journeys --apply        # 真写
    python -m scripts.relations_seed_journeys --platforms telegram,whatsapp
    python -m scripts.relations_seed_journeys --data-root D:\\chengjie-instances\\zhiliao\\data --apply

数据根按 scripts/_data_root 契约解析（CLI 值 → AITR_DATA_ROOT → 实例自动发现）。
**无实例部署时不回落引擎根**（防把 contacts.db 建进仓库 config/），必须显式
--data-root 才对任意目录操作。

安全性：contacts.db 为 WAL + busy_timeout（与常驻服务并存安全）；inbox.db 扫描
只读、contact_id 回写为短事务。事件幂等（确定性 event_id + INSERT OR IGNORE），
重跑不重复。写完自动物化 stored intimacy（全量榜按该列扫描排序）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import ENGINE_ROOT, load_merged_config, resolve_data_roots  # noqa: E402
from src.contacts.journey_seed import DEFAULT_PER_CONV_CAP, run_seed  # noqa: E402


def _print_summary(root: Path, s: dict) -> None:
    mode = "APPLY" if s.get("apply") else "DRY-RUN"
    print(f"\n=== [{mode}] {root} ===")
    print(f"  候选私聊会话: {s.get('candidates', 0)}  "
          f"(平台外跳过 {s.get('skipped', {}).get('platform', 0)} / "
          f"群样残留跳过 {s.get('skipped', {}).get('group_like', 0)})")
    print(f"  待回放会话:   {s.get('conversations_seeded', 0)}")
    print(f"  事件 计划/实插: {s.get('events_planned', 0)} / "
          f"{s.get('events_inserted', 0)}")
    if s.get("apply"):
        print(f"  新建 contact:  {s.get('contacts_created', 0)}")
        print(f"  contact_id 回写: {s.get('contact_ids_written', 0)}")
        print(f"  intimacy 物化:  {s.get('intimacy_refreshed', 0)}")
        print(f"  失败:          {s.get('errors', 0)}")
    print(f"  耗时: {s.get('duration_s', 0)}s")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--platforms", default="",
                    help="逗号分隔平台过滤（默认全部合法渠道）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理 N 个会话（0=全部）")
    ap.add_argument("--cap", type=int, default=DEFAULT_PER_CONV_CAP,
                    help=f"每会话事件上限（默认 {DEFAULT_PER_CONV_CAP}，与亲密度重放窗对齐）")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root)
    plats = [p for p in (args.platforms or "").split(",") if p.strip()] or None

    rc = 0
    for root in roots:
        root = Path(root)
        if root == ENGINE_ROOT and not args.data_root:
            print(f"SKIP {root}: 引擎仓库根不是实例数据根（要对它操作请显式 --data-root）")
            continue
        inbox_db = root / "config" / "inbox.db"
        contacts_db = root / "config" / "contacts.db"
        if not inbox_db.is_file():
            print(f"SKIP {root}: 无 inbox.db")
            continue
        try:
            cfg = load_merged_config(root)
            enabled = bool(((cfg.get("contacts") or {}).get("enabled")))
        except Exception:
            enabled = False
        if not enabled:
            print(f"NOTE {root}: contacts.enabled 未开启（照常种子化，"
                  f"数据在子系统开启后即被消费）")

        from src.contacts.store import ContactStore
        from src.skills.intimacy_engine import IntimacyEngine

        store = ContactStore(db_path=contacts_db)
        try:
            engine = IntimacyEngine(store)
            summary = run_seed(
                inbox_db, store, engine,
                apply=bool(args.apply),
                per_conv_cap=max(10, int(args.cap)),
                platforms=plats,
                limit=max(0, int(args.limit)),
            )
            _print_summary(root, summary)
            if summary.get("errors"):
                rc = 1
            if args.apply:
                print("  提示: 全量榜接口有 60s 缓存，页面点「刷新」（force=1）即见新数据。")
        finally:
            try:
                store.close()
            except Exception:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
