"""相册健康报告 CLI（实施90 阶段二）——只读体检：该补什么、该清什么。

对生产注册相册库 ``mode=ro`` 只读（含 scene_demand_daily 需求账本），逐人设出
备货/打标覆盖/零命中/近重复族/敏感/分歧/缺货场景 + 判词。零写入，退出码恒 0。

  python tools/album_health_report.py
  python tools/album_health_report.py --db D:/chengjie-instances/zhiliao/data/config/persona_media.db
  python tools/album_health_report.py --persona lin_xiaoyu --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.persona_media_backfill_meta import _read_rows_ro  # noqa: E402
from src.companion.album_health import build_album_health  # noqa: E402


def _read_demand_ro(db_path: str, days: int):
    """scene_demand_daily 只读聚合（表不存在/旧库 → {}）。"""
    try:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            since = time.strftime(
                "%Y-%m-%d", time.localtime(time.time() - days * 86400))
            rows = conn.execute(
                "SELECT scene, SUM(demand), SUM(unmet) FROM scene_demand_daily"
                " WHERE day >= ? GROUP BY scene", (since,)).fetchall()
        finally:
            conn.close()
        return {str(r[0]): {"demand": int(r[1] or 0), "unmet": int(r[2] or 0)}
                for r in rows}
    except Exception:
        return {}


def _parse_auto_meta(rows):
    """_read_rows_ro 只解析 tags/triggers；auto_meta 列在这补（旧库无列=空）。"""
    for r in rows:
        raw = r.get("auto_meta")
        if isinstance(raw, str) and raw:
            try:
                r["auto_meta"] = json.loads(raw)
            except Exception:
                r["auto_meta"] = {}
        elif not isinstance(raw, dict):
            r["auto_meta"] = {}
    return rows


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="相册健康体检（只读）")
    ap.add_argument("--db", default="config/persona_media.db")
    ap.add_argument("--persona", default="")
    ap.add_argument("--days", type=int, default=14, help="需求账本窗口（天）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    rows = _read_rows_ro(str(args.db), args.persona)
    if rows is None:
        print(f"库不可读：{args.db}", file=sys.stderr)
        return 0
    rep = build_album_health(_parse_auto_meta(rows),
                             _read_demand_ro(str(args.db), args.days))
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0
    print(f"=== 相册健康体检（db={args.db}，需求窗 {args.days} 天）===")
    if not rep["personas"]:
        print("库为空。")
        return 0
    for pid, p in rep["personas"].items():
        st = p["tag_status"]
        print(f"● {pid}: {p['total']} 条（图 {p['photo']} / 视频 {p['video']} / "
              f"启用 {p['enabled']}）")
        print(f"  打标: tagged {st['tagged']} · 未标 {st['untagged']} · "
              f"失败 {st['failed']} · 指纹 {p['phash_cover']} · "
              f"缺缩略图 {p['thumbs_missing']}")
        if p["scene_supply"]:
            print("  场景备货: " + ", ".join(
                f"{k} {v}" for k, v in p["scene_supply"].items()))
        if p["neardup_groups"]:
            print(f"  近重复族: {len(p['neardup_groups'])} 组 "
                  + str([len(g) for g in p["neardup_groups"]]))
        for v in p["verdict"]:
            print("  → " + v)
        if not p["verdict"]:
            print("  → 健康：无待办")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
