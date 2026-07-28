# -*- coding: utf-8 -*-
"""扫码前后对照：账号注册表 + 中央池痕迹（只读，不打印任何密钥）。

用法：
    python deploy/instances/scan_verify.py            # 打印当前快照
    python deploy/instances/scan_verify.py --baseline # 存基线
    python deploy/instances/scan_verify.py --diff     # 与基线比对（扫码后跑这个）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DB = Path(r"D:\chengjie-instances\zhiliao\data\config\account_registry.db")
BASELINE = Path(r"D:\chengjie-instances\zhiliao\data\logs\_scan_baseline.json")


def snapshot() -> dict:
    if not DB.is_file():
        return {"error": f"找不到注册表 {DB}"}
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT account_id, mode, status, meta_json FROM platform_accounts "
            "WHERE platform='telegram'"
        ).fetchall()
    finally:
        conn.close()

    out = {"total": len(rows), "accounts": {}}
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except Exception:
            meta = {}
        out["accounts"][str(r["account_id"])] = {
            "mode": r["mode"],
            "status": r["status"],
            # 只记「有没有」，不记值
            "has_session": bool(meta.get("session_name") or meta.get("session_string")),
            "credpool_key": bool(meta.get("credpool_key")),
            "device_seed": bool(meta.get("device_seed")),
            "device_fp": (meta.get("device_fp") or None) and {
                k: meta["device_fp"].get(k) for k in
                ("device_model", "system_version", "app_version")
            },
            "persona_id": meta.get("persona_id") or "",
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--diff", action="store_true")
    args = ap.parse_args()

    snap = snapshot()
    if "error" in snap:
        print(snap["error"])
        return 2

    if args.baseline:
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已存：{BASELINE}（telegram 账号 {snap['total']} 个）")
        return 0

    if args.diff:
        if not BASELINE.is_file():
            print("没有基线，先跑 --baseline")
            return 2
        old = json.loads(BASELINE.read_text(encoding="utf-8"))
        new_ids = set(snap["accounts"]) - set(old["accounts"])
        print(f"扫码前 {old['total']} 个 → 现在 {snap['total']} 个；新增 {len(new_ids)} 个")
        if not new_ids:
            print("（没有新增账号——要么还没扫成功，要么扫的是已存在的号）")
        for aid in sorted(new_ids):
            a = snap["accounts"][aid]
            print(f"\n  新账号 {aid}")
            print(f"    mode={a['mode']}  status={a['status']}  session={'有' if a['has_session'] else '无'}")
            print(f"    人设={a['persona_id'] or '(未绑定)'}")
            print(f"    中央池粘定键 credpool_key : {'有' if a['credpool_key'] else '无（走的是配置自带凭据）'}")
            print(f"    设备指纹 device_fp        : {a['device_fp'] or '无（指纹开关未开）'}")
        return 0

    print(f"telegram 账号 {snap['total']} 个")
    for aid, a in sorted(snap["accounts"].items()):
        flags = []
        if a["credpool_key"]:
            flags.append("池")
        if a["device_fp"]:
            flags.append("指纹")
        print(f"  {aid:<16} {a['mode']:<10} {a['status']:<10} {' '.join(flags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
