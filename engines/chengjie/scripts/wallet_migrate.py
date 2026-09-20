# -*- coding: utf-8 -*-
"""字符池 → Token 钱包并账 CLI（实施50 P3；enforce 切换日的一次性动作）。

语义（token_ledger 模块 docstring 的落地）：
- **并的是客户已付的存量价值**：剩余字符 = ``included_chars + 充值包 - 已用``，
  按 ``CHARS_PER_TOKEN_LEGACY``（100 字符/Token）换算，**向上取整**（宁多给客户）；
- 入账走 ``TokenLedgerStore.grant_migration``：kind=migration、**永不过期**
  （过期语义只属 bonus/monthly——存量价值不设时限）、ref 幂等
  （``migrate-chars:<lic_id>``，重复跑天然拒绝，绝不双倍入账）；
- 钱包键 = ``wallet_id_for_status``（与消费点记账**同一把键**——键不同=并了个寂寞）；
- **默认 dry-run**：只算不写；``--apply`` 才落账本。

边界（如实声明）：
- 首启体验档（local_trial）的字符不在本工具范围——那是未留资的匿名体验，
  切换日由试用凭证升级链路自然换发 Token 凭证；
- 影子期（enforce 未开）**不必跑本工具**：字符池仍是强制口径，提前并账只会让
  快照陈旧。正确时序 = 切换日先并账、再开 enforce；
- 单根单进程：license_manager 是进程级单例，多实例机请逐实例
  ``--data-root`` 各跑一次（--apply 时强制显式单根）。

用法：
    python scripts/wallet_migrate.py                      # dry-run（自动发现单实例）
    python scripts/wallet_migrate.py --data-root D:\\chengjie-instances\\zhiliao\\data --apply
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.licensing.token_ledger import (  # noqa: E402
    CHARS_PER_TOKEN_LEGACY,
    TokenLedgerStore,
    wallet_id_for_status,
)

# ── 纯函数核心（测试面）───────────────────────────────────────────────────────


def plan_migration(included_chars: int, topup_chars: int, used_chars: int) -> Dict[str, int]:
    """剩余字符 → Token（向上取整；全部输入按非负钳制，负余额=0）。"""
    inc = max(0, int(included_chars or 0))
    top = max(0, int(topup_chars or 0))
    used = max(0, int(used_chars or 0))
    remaining = max(0, inc + top - used)
    tokens = math.ceil(remaining / float(CHARS_PER_TOKEN_LEGACY)) if remaining > 0 else 0
    return {
        "included_chars": inc, "topup_chars": top, "used_chars": used,
        "remaining_chars": remaining, "tokens": int(tokens),
    }


def migration_ref(lic_id: str) -> str:
    return f"migrate-chars:{str(lic_id or 'default')}"


def apply_migration(
    store: TokenLedgerStore, lic_status: Any, plan: Dict[str, int], *, apply: bool,
) -> Dict[str, Any]:
    """按 plan 入账（或 dry-run 预演）。返回 {wallet, ref, tokens, applied, skipped_reason}。"""
    wallet = wallet_id_for_status(lic_status)
    ref = migration_ref(getattr(lic_status, "lic_id", "") or "default")
    out: Dict[str, Any] = {
        "wallet": wallet, "ref": ref, "tokens": int(plan["tokens"]),
        "applied": False, "skipped_reason": "",
    }
    if plan["tokens"] <= 0:
        out["skipped_reason"] = "no_remaining_chars"
        return out
    if not apply:
        out["skipped_reason"] = "dry_run"
        return out
    ok = store.grant_migration(
        wallet, plan["tokens"], ref,
        note=f"chars={plan['remaining_chars']} (included={plan['included_chars']}"
             f" topup={plan['topup_chars']} used={plan['used_chars']})",
    )
    out["applied"] = bool(ok)
    if not ok:
        out["skipped_reason"] = "ref_exists_or_store_error"
    return out


# ── IO ───────────────────────────────────────────────────────────────────────


def _quota_sums(root: Path, lic_id: str) -> Dict[str, int]:
    """字符池只读汇总（库缺席 = 从未记账 = 0/0）。"""
    db = Path(root) / "config" / "license_quota.db"
    out = {"used": 0, "topup": 0}
    if not db.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(chars),0) FROM license_char_usage WHERE lic_id=?",
                (lic_id,)).fetchone()
            out["used"] = int(row[0] or 0)
            row = conn.execute(
                "SELECT COALESCE(SUM(chars),0) FROM license_char_topup WHERE lic_id=?",
                (lic_id,)).fetchone()
            out["topup"] = int(row[0] or 0)
        finally:
            conn.close()
    except Exception:
        pass
    return out


def migrate_root(root: Path, *, apply: bool) -> Dict[str, Any]:
    from src.licensing.license_manager import get_license_manager

    mgr = get_license_manager(license_path=str(Path(root) / "config" / "license.key"))
    st = mgr.status()
    lic_id = str(getattr(st, "lic_id", "") or "default")
    out: Dict[str, Any] = {"root": str(root), "lic_id": lic_id,
                           "licensed": bool(getattr(st, "licensed", False))}
    if not out["licensed"]:
        out["skipped_reason"] = "not_licensed"
        return out
    sums = _quota_sums(root, lic_id)
    plan = plan_migration(
        int(getattr(st, "included_chars", 0) or 0), sums["topup"], sums["used"])
    out["plan"] = plan
    db_path = Path(root) / "config" / "token_ledger.db"
    if not apply and not db_path.is_file():
        # dry-run 必须真只读：TokenLedgerStore 构造即建库，账本不存在时不碰盘
        wallet = wallet_id_for_status(st)
        out["result"] = {
            "wallet": wallet, "ref": migration_ref(lic_id),
            "tokens": int(plan["tokens"]), "applied": False,
            "skipped_reason": "dry_run" if plan["tokens"] > 0 else "no_remaining_chars",
        }
        out["balance_before"] = out["balance_after"] = 0
        return out
    store = TokenLedgerStore(db_path)
    before = store.balance(wallet_id_for_status(st))
    result = apply_migration(store, st, plan, apply=apply)
    out["result"] = result
    out["balance_before"] = before["balance"]
    out["balance_after"] = store.balance(result["wallet"])["balance"]
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="字符池 → Token 钱包并账（默认 dry-run）")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root)
    if args.apply and (len(roots) != 1 or not args.data_root):
        print("--apply 需显式 --data-root 指定唯一实例（license_manager 单例约束）")
        return 2
    rc = 0
    for root in roots:
        try:
            rep = migrate_root(root, apply=args.apply)
        except Exception as exc:  # 单根失败不拖累其它根的 dry-run
            print(f"[{root}] 失败: {exc}")
            rc = 1
            continue
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"== 并账 {mode} · {rep['root']} ==")
        if rep.get("skipped_reason") == "not_licensed":
            print("  未授权实例，跳过")
            continue
        p = rep["plan"]
        print(f"  授权 {rep['lic_id']}: included={p['included_chars']}"
              f" topup={p['topup_chars']} used={p['used_chars']}"
              f" → 剩余 {p['remaining_chars']} chars = {p['tokens']} Token")
        r = rep["result"]
        print(f"  钱包 {r['wallet']} · ref={r['ref']}"
              f" · {'已入账' if r['applied'] else '未写入(' + r['skipped_reason'] + ')'}")
        print(f"  余额 {rep['balance_before']} → {rep['balance_after']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
