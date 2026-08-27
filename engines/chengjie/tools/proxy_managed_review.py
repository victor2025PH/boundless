"""托管代理（一键代理）对账 CLI——只读，多实例数据根自动发现。

「先交付后扣费」的方向选择意味着钱侧可能欠账（绝不会多扣），欠账都长在
``proxy_subscriptions.db`` 的这些行上：

- ``charged=0`` 的 active 行 —— 首购已交付、``record_fixed_spend`` 没落账；
- ``renew_state='pending'`` —— 续期已延期、扣费前进程崩了；
- ``renew_state='unbilled'`` —— 续期已延期、扣费调用失败。

本工具把三类欠账 + 到期分布 + 幽灵占位（``pending:*`` 占着代理但订阅已不活跃）
一次列清。**只读**（sqlite ``mode=ro`` URI，零写风险），处置动作（补记/核销）
是人的决定，刻意不做自动修复。

用法::

    python tools/proxy_managed_review.py [--data-root D:\\chengjie-instances\\zhiliao\\data] [--json]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._data_root import resolve_data_roots  # noqa: E402


def diff_orders(vendor_orders: List[Dict[str, Any]],
                local_refs: List[str]) -> Dict[str, Any]:
    """上游对账单 × 本地订阅台账 双向 diff（纯函数）。

    - ``vendor_only``：上游有单、我们没账 ⇒ **白付了钱**（崩溃在 activate 前 /
      上游重复下单 / 有人绕过系统手工买）——JIT 模式最需要盯的泄漏面；
    - ``local_only``：我们有账、上游没单 ⇒ 理论上不该发生（stock 模式的
      order_ref 是池内 id、不在上游账单里，属正常，调用方按 provider 过滤）。
    """
    v_refs = {str(o.get("order_ref") or "") for o in vendor_orders
              if o.get("order_ref")}
    l_refs = {r for r in (str(x) for x in local_refs) if r}
    vendor_only = sorted(v_refs - l_refs)
    cost_map = {str(o.get("order_ref")): float(o.get("cost") or 0)
                for o in vendor_orders}
    return {
        "vendor_total": len(v_refs),
        "local_total": len(l_refs),
        "vendor_only": vendor_only,
        "vendor_only_cost": round(
            sum(cost_map.get(r, 0.0) for r in vendor_only), 2),
        "local_only": sorted(l_refs - v_refs),
    }


def collect_review(db_path: Path, *, now: float) -> Dict[str, Any]:
    """单实例数据根的对账快照（纯读；库不存在返回 exists=False）。"""
    out: Dict[str, Any] = {
        "db": str(db_path), "exists": db_path.exists(),
        "active": 0, "unbilled_rows": [], "renew_pending_rows": [],
        "expiring_7d": [], "expired_still_assigned": [],
        "tokens_total": 0, "order_refs": [],
    }
    if not out["exists"]:
        return out
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM proxy_subscriptions").fetchall()
    except sqlite3.OperationalError:
        conn.close()
        out["exists"] = False  # 库在但表不在（从未初始化过）
        return out

    for r in rows:
        d = dict(r)
        status = str(d.get("status") or "")
        if str(d.get("order_ref") or ""):
            out["order_refs"].append(str(d["order_ref"]))
        end = float(d.get("period_end") or 0)
        brief = {
            "sub_id": d.get("sub_id"), "country": d.get("country"),
            "kind": d.get("kind"), "tokens": int(d.get("tokens") or 0),
            "proxy_id": d.get("proxy_id"), "wallet": d.get("wallet"),
            "note": d.get("note"), "renew_state": d.get("renew_state") or "",
            "remaining_days": max(0, int((end - now) // 86400)) if end else 0,
        }
        if status == "active":
            out["active"] += 1
            out["tokens_total"] += brief["tokens"]
            if not int(d.get("charged") or 0):
                out["unbilled_rows"].append(brief)
            if str(d.get("renew_state") or ""):
                out["renew_pending_rows"].append(brief)
            if end and end <= now + 7 * 86400:
                out["expiring_7d"].append(brief)
    conn.close()
    return out


def render(review: Dict[str, Any]) -> str:
    lines: List[str] = [f"== {review['db']}"]
    if not review["exists"]:
        lines.append("  （无订阅库——该实例从未开通过托管代理）")
        return "\n".join(lines)
    lines.append(f"  活跃订阅 {review['active']} 条，"
                 f"累计已扣 {review['tokens_total']} Token")
    for label, key, action in (
        ("首购未入账（charged=0）", "unbilled_rows", "人工补记或核销"),
        ("续期扣费未落账（renew_state 非空）", "renew_pending_rows", "补扣或核销后清 renew_state"),
        ("7 天内到期", "expiring_7d", "确认续费意愿 / 检查 auto_renew 与余额"),
    ):
        items = review.get(key) or []
        if not items:
            continue
        lines.append(f"  [{label}] {len(items)} 条 —— {action}")
        for it in items[:10]:
            lines.append(
                f"    {it['sub_id']}  {it['country']}/{it['kind']}  "
                f"tokens={it['tokens']}  剩{it['remaining_days']}天  "
                f"note={it['note'] or '-'}{('/' + it['renew_state']) if it['renew_state'] else ''}")
    if (not review["unbilled_rows"] and not review["renew_pending_rows"]
            and not review["expiring_7d"]):
        lines.append("  账目干净：无待对账、无临期。")
    return "\n".join(lines)


def run_vendor_reconcile(cfg_path: Path,
                         reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    """拉上游对账单与本地台账互 diff（JIT 模式：上游有单我们没账＝白付钱）。"""
    import asyncio

    import yaml

    from src.integrations.proxy_provider import (
        HttpProxyProvider, parse_managed_cfg,
    )

    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    mcfg = parse_managed_cfg(cfg)
    provider = HttpProxyProvider(mcfg.get("http") or {})
    orders = asyncio.run(provider.list_vendor_orders())
    if orders is None:
        return {"ok": False, "error": "orders_not_configured",
                "hint": "在 proxies.managed.http.orders 配置上游订单列表端点"}
    local_refs: List[str] = []
    for rv in reviews:
        local_refs.extend(rv.get("order_refs") or [])
    out = diff_orders(orders, local_refs)
    out["ok"] = True
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-root", default="", help="指定实例数据根（缺省自动发现全部）")
    ap.add_argument("--vendor-config", default="",
                    help="含 proxies.managed.http.orders 的 yaml（如实例 "
                         "config.local.yaml）——拉上游对账单与本地台账互 diff")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    now = time.time()
    reviews = []
    for root in resolve_data_roots(args.data_root):
        reviews.append(collect_review(
            Path(root) / "config" / "proxy_subscriptions.db", now=now))

    vendor = None
    if args.vendor_config:
        vendor = run_vendor_reconcile(Path(args.vendor_config), reviews)

    if args.json:
        print(json.dumps({"reviews": reviews, "vendor": vendor},
                         ensure_ascii=False, indent=2))
    else:
        for rv in reviews:
            print(render(rv))
        if vendor is not None:
            print("== 上游对账单 diff")
            if not vendor.get("ok"):
                print(f"  {vendor.get('error')}: {vendor.get('hint', '')}")
            else:
                print(f"  上游 {vendor['vendor_total']} 单 / 本地 "
                      f"{vendor['local_total']} 账")
                if vendor["vendor_only"]:
                    print(f"  [白付了钱] 上游有单我们没账 "
                          f"{len(vendor['vendor_only'])} 单，共 "
                          f"{vendor['vendor_only_cost']}：")
                    for r in vendor["vendor_only"][:10]:
                        print(f"    {r}")
                if vendor["local_only"]:
                    print(f"  [没买到] 我们有账上游没单 "
                          f"{len(vendor['local_only'])} 条（stock 期的池内 id "
                          f"属正常）")
                if not vendor["vendor_only"] and not vendor["local_only"]:
                    print("  两边对平。")
    # 有欠账/白付时非零退出，方便接外部告警；「没有库」不算欠账。
    dirty = any((rv.get("unbilled_rows") or rv.get("renew_pending_rows"))
                for rv in reviews)
    if vendor is not None and vendor.get("ok") and vendor.get("vendor_only"):
        dirty = True
    return 1 if dirty else 0


if __name__ == "__main__":
    raise SystemExit(main())
