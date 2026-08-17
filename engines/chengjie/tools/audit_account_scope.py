# -*- coding: utf-8 -*-
"""账号归属体检（只读）——「切账号后列表残留别号会话」的病灶盘点。

背景（2026-08-14）：统一收件箱的会话可见性在账号视角下靠前端
``c.account_id === accountFilter`` 过滤；任何一行 ``account_id`` 落库为空 /
'default' / 与 ``conversation_id`` 前缀不一致，都会穿透过滤表现为「切了账号
还看到上一个账号的会话」。本工具把三类脏行清单化：

  A. **键列分裂**：``conversation_id`` ≠ ``platform:account_id:chat_key``
     重组值（落库两套真相，过滤读列、身份读键 → 必串）；
  B. **幽灵账号**：多账号平台上 ``account_id`` 为空或 'default'，但该平台
     另有真实账号在跑（这行会出现在**每个**账号视角里）；
  C. **跨号同 peer**：同 (platform, chat_key) 挂在多个 account_id 下——本身
     合法（两个号都跟同一客户聊过），仅当其中一号是空/'default' 时标可疑。

只读连接（``mode=ro`` URI），对活体生产库零写事务；多实例机逐根跑。

用法：
    python tools/audit_account_scope.py            # 全部活跃实例
    python tools/audit_account_scope.py --json     # 机器可读
    python tools/audit_account_scope.py --data-root D:/chengjie-instances/zhiliao/data
    python tools/audit_account_scope.py --db D:/tmp/inbox.db   # 直接指库
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts._data_root import resolve_data_roots  # noqa: E402

PLACEHOLDER_ACCTS = ("", "default")


def _ro_connect(db: Path):
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def audit_db(db: Path) -> dict:
    """对单个 inbox.db 出体检结果（纯读；库缺表时按空处理不抛）。"""
    out: dict = {
        "db": str(db),
        "total_conversations": 0,
        "by_platform_account": [],      # [(platform, account_id, n, last_ts)]
        "key_split_rows": [],           # A 类：conversation_id 与列重组值不一致
        "ghost_account_rows": [],       # B 类：多账号平台上的空/'default' 行
        "cross_account_peers": [],      # C 类：同 peer 多账号（含可疑标记）
    }
    con = _ro_connect(db)
    try:
        try:
            rows = con.execute(
                "SELECT conversation_id, platform, account_id, chat_key, "
                "display_name, last_ts FROM conversations").fetchall()
        except sqlite3.OperationalError:
            return out
        out["total_conversations"] = len(rows)

        # 平台×账号分布
        dist: dict = {}
        for _cid, plat, acct, _ck, _dn, ts in rows:
            k = (str(plat), str(acct or ""))
            n, latest = dist.get(k, (0, 0.0))
            dist[k] = (n + 1, max(latest, float(ts or 0)))
        out["by_platform_account"] = sorted(
            [(p, a, n, ts) for (p, a), (n, ts) in dist.items()],
            key=lambda r: (r[0], -r[2]))

        # 每平台的「真实账号集合」（排除占位）——B 类判定基准
        real_accts: dict = {}
        for (p, a), _ in dist.items():
            if a not in PLACEHOLDER_ACCTS:
                real_accts.setdefault(p, set()).add(a)

        peers: dict = {}
        for cid, plat, acct, ck, dn, ts in rows:
            plat_s, acct_s, ck_s = str(plat), str(acct or ""), str(ck or "")
            # A 类：键列分裂
            rebuilt = f"{plat_s}:{acct_s}:{ck_s}"
            if str(cid) != rebuilt:
                out["key_split_rows"].append({
                    "conversation_id": str(cid), "rebuilt": rebuilt,
                    "display_name": str(dn or ""), "last_ts": float(ts or 0)})
            # B 类：幽灵账号（该平台有真实账号，这行却是占位号）
            if acct_s in PLACEHOLDER_ACCTS and real_accts.get(plat_s):
                out["ghost_account_rows"].append({
                    "conversation_id": str(cid), "platform": plat_s,
                    "account_id": acct_s, "chat_key": ck_s,
                    "display_name": str(dn or ""), "last_ts": float(ts or 0)})
            peers.setdefault((plat_s, ck_s), set()).add(acct_s)

        # C 类：同 peer 挂多账号
        for (plat_s, ck_s), accts in peers.items():
            if len(accts) > 1 and ck_s:
                out["cross_account_peers"].append({
                    "platform": plat_s, "chat_key": ck_s,
                    "accounts": sorted(accts),
                    "suspicious": any(a in PLACEHOLDER_ACCTS for a in accts)})
    finally:
        con.close()
    return out


def _fmt_ts(ts: float) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def render(report: dict) -> str:
    lines = [f"== {report['db']} =="]
    lines.append(f"conversations 总数: {report['total_conversations']}")
    lines.append("平台×账号分布:")
    for p, a, n, ts in report["by_platform_account"]:
        tag = "  <-- 占位号" if a in PLACEHOLDER_ACCTS else ""
        lines.append(f"  {p:<12} {a or '(空)':<24} {n:>5} 行  最近 {_fmt_ts(ts)}{tag}")
    ks = report["key_split_rows"]
    lines.append(f"[A] 键列分裂: {len(ks)} 行" + ("" if not ks else "  ← 必修"))
    for r in ks[:10]:
        lines.append(f"    {r['conversation_id']}  ≠  {r['rebuilt']}  ({r['display_name']})")
    gh = report["ghost_account_rows"]
    lines.append(f"[B] 幽灵账号行（多账号平台上的空/'default'）: {len(gh)} 行"
                 + ("" if not gh else "  ← 串号残留直接来源"))
    for r in gh[:10]:
        lines.append(f"    {r['platform']}:{r['account_id'] or '(空)'}:{r['chat_key']}"
                     f"  ({r['display_name']})  最近 {_fmt_ts(r['last_ts'])}")
    cs = [c for c in report["cross_account_peers"] if c["suspicious"]]
    lines.append(f"[C] 跨号同 peer（可疑=含占位号）: 可疑 {len(cs)} / "
                 f"共 {len(report['cross_account_peers'])}")
    for r in cs[:10]:
        lines.append(f"    {r['platform']} peer={r['chat_key']} 挂在 {r['accounts']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-root", default="", help="显式数据根（默认自动发现活跃实例）")
    ap.add_argument("--db", default="", help="直接指定 inbox.db 路径（跳过数据根解析）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args()

    dbs: list = []
    if args.db:
        dbs = [Path(args.db)]
    else:
        for root in resolve_data_roots(args.data_root):
            cand = Path(root) / "config" / "inbox.db"
            if cand.is_file():
                dbs.append(cand)
    if not dbs:
        print("(未找到 inbox.db)")
        return 1

    reports = [audit_db(db) for db in dbs]
    if args.as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for r in reports:
            print(render(r))
            print()
    # 有 A/B 类脏行 → 退出码 2（可接告警；C 类仅信息）
    dirty = any(r["key_split_rows"] or r["ghost_account_rows"] for r in reports)
    return 2 if dirty else 0


if __name__ == "__main__":
    sys.exit(main())
