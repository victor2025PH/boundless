# -*- coding: utf-8 -*-
"""冷启动隔离闸「照着真库复盘」审计（只读；2026-08-04 随 .198 事故根因闸落地）。

为什么需要它（门禁之外还要这个）
================================
``tests/test_outbound_gate.py`` 证明判定逻辑对，``tests/test_proactive_cold_start_wiring.py``
证明闸真的接上了——但两者用的都是**我自己捏的 fixture**。捏 fixture 的人和写闸的是同一个，
「场景想漏了」这类错误两边会一起漏。真正能证伪的证据只有一种：**拿事故当天的真库跑一遍，
看它到底会压下哪些会话、放行哪些**。

本工具就是那把尺子。它**不重新实现任何判定**，而是直接调生产同一批纯函数
（``outbound_gate.account_earliest_created`` / ``may_contact`` / ``resolve_cold_start_cfg``
+ ``account_connection.AccountConnectionLog``）——所以审计结论与线上行为**不可能漂移**：
哪天有人改了闸的语义，这份报告的数字立刻跟着变。

它同时也是**运维日常用具**：新号接进来之后，运营想知道「这号现在还在隔离期吗、还有多久、
放开后有几条会真的被触达」，跑一条命令即可，不必翻日志、不必等 tick。

用法
----
::

    # 本机实例（自动发现数据根）
    python tools/proactive_cold_start_audit.py

    # 复盘别的机器：把它的 inbox.db 拷过来直接指
    python tools/proactive_cold_start_audit.py --db D:/tmp/zhituo198/inbox.db

    # 复盘「事故那一刻」的判定（而不是现在）
    python tools/proactive_cold_start_audit.py --db ... --at "2026-08-04 15:30"

    # 机器可读
    python tools/proactive_cold_start_audit.py --json

只读保证
--------
- sqlite 一律 ``file:...?mode=ro`` URI 打开（对活体生产库零写事务）；
- ``AccountConnectionLog`` 传 ``path=None`` **纯内存**——审计绝不写、更不污染线上那份
  「首见即冻结」的接入时刻登记（写脏了会让真闸误判老账号为新号）。
- 因此**审计是幂等的**，可以随便重复跑。

口径与生产对齐的几处细节（不对齐就会给出好看的假数字）
------------------------------------------------------
- 取数与 ``_conversations()`` 同样是 ``ORDER BY last_ts DESC LIMIT scan_limit``——
  生产只扫最近 N 条，审计扫全库就会报出生产根本看不到的会话；
- ``last_in_ts`` 与 ``store.last_inbound_ts_map`` 同一口径（``MAX(ts) WHERE direction='in'``），
  从未入站的会话按 0；
- 沉默阈值 ``min_silent_hours`` 一并施加，报告区分「本来就没到点」与「到点了但被闸压住」——
  只有后者才是本闸的功劳，混在一起统计等于给自己贴金。

**刻意不覆盖**的既有护栏（群/频道、系统 peer、死 peer、占位会话、opt-out、bot、舰队）：
那些判定散落在 ``_conversations()`` 闭包里且依赖运行期状态，离线复刻必然失真。所以本报告的
``eligible`` 是**上界**（真实候选只会更少），结论方向仍然成立：上界都被压到 0，实际必然是 0。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.inbox.account_connection import AccountConnectionLog  # noqa: E402
from src.inbox.store import is_system_peer  # noqa: E402
from src.inbox.outbound_gate import (  # noqa: E402
    account_earliest_created,
    may_contact,
    resolve_cold_start_cfg,
)

HOUR = 3600.0


# ── 生产既有护栏中「可离线忠实复刻」的那部分 ────────────────────────────────
def passes_static_guards(row: Dict[str, Any]) -> bool:
    """复刻 ``_conversations()`` 里**不依赖运行期状态**的三道过滤，逐条对齐源码。

    为什么要费这个事：不复刻的话 ``eligible`` 会把群聊/收藏夹也算进去，报告出来的
    「被压了 N 条」就虚高——安全工具给自己贴金是最坏的一种失真（下次真出问题时没人
    信这个数）。这三条判据是纯函数、可离线精确复刻，就没有理由估着来：

    - ``chat_type`` 不在 private/user/bot → 群、频道（源码 610-612 行）；
    - Telegram 负数 chat_key → 群/频道兜底（源码 623-625 行）；
    - ``is_system_peer`` → Saved Messages / 官方服务号（源码 620-622 行，直接调
      同一个函数，口径不可能漂移）。

    **刻意不复刻**（依赖运行期状态，离线复刻必然失真，宁可如实留成残差）：死 peer
    集合 ``_is_bad_peer``、账号可发性 ``_account_can_send``、opt-out 静默、占位会话
    （需查 ``last_message_dirs``）、以及另一条线在建的 bot / 舰队护栏。故 ``eligible``
    仍是**上界**——但已经是紧得多的上界，且方向不变：上界被压到 0，实际必然是 0。
    """
    ct = str(row.get("chat_type") or "").strip().lower()
    if ct and ct not in ("private", "user", "bot"):
        return False
    ck = str(row.get("chat_key") or "")
    pf = str(row.get("platform") or "telegram")
    if pf == "telegram":
        if is_system_peer(pf, str(row.get("account_id") or ""), ck):
            return False
        try:
            if int(ck) < 0:
                return False
        except (TypeError, ValueError):
            pass
    return True


# ── 取数（只读）──────────────────────────────────────────────────────────────
def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = "file:%s?mode=ro" % str(db_path).replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def load_rows(db_path: Path, scan_limit: int) -> List[Dict[str, Any]]:
    """与 ``_conversations()`` 同口径取会话 + 补 ``last_in_ts``。"""
    con = _connect_ro(db_path)
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM conversations ORDER BY last_ts DESC LIMIT ?",
            (int(scan_limit),)).fetchall()]
        inbound: Dict[str, float] = {}
        for cid, mts in con.execute(
                "SELECT conversation_id, MAX(ts) FROM messages "
                "WHERE direction='in' GROUP BY conversation_id").fetchall():
            inbound[str(cid)] = float(mts or 0.0)
    finally:
        con.close()
    for r in rows:
        r["last_in_ts"] = inbound.get(str(r.get("conversation_id")), 0.0)
    return rows


# ── 审计（纯函数：喂 rows 出报告，便于单测）──────────────────────────────────
def audit_rows(
    rows: List[Dict[str, Any]],
    *,
    cfg: Dict[str, Any],
    now: float,
    min_silent_hours: float,
    quota_exhausted: bool = False,
) -> Dict[str, Any]:
    """按账号汇总：接入时刻 / 是否隔离期 / 到点候选中被压了几条、按什么理由。

    ``eligible`` = 过了静态护栏（``passes_static_guards``）**且**过了沉默阈值的会话数；
    受运行期护栏影响的部分未复刻，故仍是上界（见该函数 docstring）。
    ``suppressed`` = 其中被本闸压下的，按 reason 分桶。两者之差即真正会被触达的条数。
    """
    # 接入时刻自举用**全量** rows（与生产一致：群/系统会话的 created_at 同样是接入
    # 证据，拿来估账号年龄更准），静态护栏只用于筛「谁算候选」。
    earliest = account_earliest_created(rows)
    log = AccountConnectionLog(None)  # 纯内存：绝不污染线上登记

    accounts: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        platform = str(r.get("platform") or "telegram")
        account_id = str(r.get("account_id") or "default")
        key = f"{platform}:{account_id}"
        conn_at = log.observe(platform, account_id,
                              earliest.get((platform, account_id), 0.0), now=now)
        acc = accounts.setdefault(key, {
            "platform": platform,
            "account_id": account_id,
            "connected_at": conn_at,
            "connected_at_iso": _iso(conn_at),
            "account_age_hours": round((now - conn_at) / HOUR, 1),
            "conversations": 0,
            "private": 0,
            "eligible": 0,
            "suppressed": {},
            "would_send": 0,
            "samples": [],
        })
        acc["conversations"] += 1
        if not passes_static_guards(r):
            continue                      # 群/频道/收藏夹：生产本就不发，别算进战功
        acc["private"] += 1

        last_ts = float(r.get("last_ts") or 0.0)
        silent_h = (now - last_ts) / HOUR if last_ts > 0 else 1e9
        if silent_h < min_silent_hours:
            continue                      # 本来就没到点，不算本闸的功劳
        acc["eligible"] += 1

        v = may_contact({"last_in_ts": float(r.get("last_in_ts") or 0.0)},
                        connected_at=conn_at, now=now, cfg=cfg,
                        quota_exhausted=quota_exhausted)
        if v.ok:
            acc["would_send"] += 1
        else:
            acc["suppressed"][v.reason] = acc["suppressed"].get(v.reason, 0) + 1
            if len(acc["samples"]) < 8:
                acc["samples"].append({
                    "chat_key": str(r.get("chat_key") or ""),
                    "display_name": str(r.get("display_name") or "")[:24],
                    "silent_hours": round(silent_h, 1),
                    "reason": v.reason,
                })

    totals = {"eligible": 0, "would_send": 0, "suppressed": {}}
    for acc in accounts.values():
        totals["eligible"] += acc["eligible"]
        totals["would_send"] += acc["would_send"]
        for k, n in acc["suppressed"].items():
            totals["suppressed"][k] = totals["suppressed"].get(k, 0) + n

    return {
        "now": now,
        "now_iso": _iso(now),
        "min_silent_hours": min_silent_hours,
        "quota_exhausted": quota_exhausted,
        "config": dict(cfg),
        "totals": totals,
        "accounts": sorted(accounts.values(),
                           key=lambda a: -sum(a["suppressed"].values())),
    }


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "?"


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def render(rep: Dict[str, Any]) -> str:
    t = rep["totals"]
    out: List[str] = []
    out.append("冷启动隔离闸审计 @ %s" % rep["now_iso"])
    out.append("  闸配置: enabled=%s warmup=%sh require_inbound=%s quota_gate=%s"
               % (rep["config"].get("enabled"), rep["config"].get("warmup_hours"),
                  rep["config"].get("require_inbound_since_connect"),
                  rep["config"].get("quota_gate")))
    out.append("  沉默阈值 %.1fh；额度耗尽=%s" % (rep["min_silent_hours"],
                                                 rep["quota_exhausted"]))
    out.append("  合计：到点候选 %d → 被压 %d → 实际会发 %d"
               % (t["eligible"], sum(t["suppressed"].values()), t["would_send"]))
    if t["suppressed"]:
        out.append("  压制理由：%s" % json.dumps(t["suppressed"], ensure_ascii=False))
    out.append("")
    for a in rep["accounts"]:
        n_sup = sum(a["suppressed"].values())
        # 文案刻意不用 emoji：PS5.1 控制台是 GBK，emoji 会让整个报告以
        # UnicodeEncodeError 收场（本仓在夜间任务上踩过同一个坑）。
        flag = "[隔离期内]" if a["account_age_hours"] < float(
            rep["config"].get("warmup_hours") or 0) else "[已过隔离期]"
        out.append("[%s:%s] %s  接入=%s（%.1fh 前）"
                   % (a["platform"], a["account_id"], flag,
                      a["connected_at_iso"], a["account_age_hours"]))
        out.append("    会话 %d（私聊 %d）｜到点 %d｜压制 %d｜会发 %d  %s"
                   % (a["conversations"], a["private"], a["eligible"], n_sup,
                      a["would_send"],
                      json.dumps(a["suppressed"], ensure_ascii=False)
                      if a["suppressed"] else ""))
        for s in a["samples"]:
            out.append("      · chat=%s %s 沉默%.0fh → %s"
                       % (s["chat_key"], s["display_name"], s["silent_hours"],
                          s["reason"]))
    return "\n".join(out)


# ── CLI ──────────────────────────────────────────────────────────────────────
def _parse_at(v: str) -> float:
    """``--at``：epoch 秒 或 ``YYYY-MM-DD HH:MM[:SS]``。"""
    s = str(v or "").strip()
    if not s:
        return time.time()
    try:
        return float(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    raise SystemExit("--at 无法解析：%s（用 epoch 秒或 'YYYY-MM-DD HH:MM'）" % s)


def _resolve_dbs(args: argparse.Namespace) -> List[Tuple[str, Path, Dict[str, Any]]]:
    """返回 [(标签, inbox.db 路径, 合并后的 config)]。"""
    if args.db:
        p = Path(args.db)
        if not p.is_file():
            raise SystemExit("找不到库：%s" % p)
        return [(p.name, p, {})]
    out: List[Tuple[str, Path, Dict[str, Any]]] = []
    for root in resolve_data_roots(args.data_root):
        db = Path(root) / "data" / "inbox.db"
        if not db.is_file():
            db = Path(root) / "inbox.db"
        if db.is_file():
            out.append((Path(root).parent.name or str(root), db,
                        load_merged_config(Path(root)) or {}))
    if not out:
        raise SystemExit("未发现任何 inbox.db（用 --db 显式指定）")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="冷启动隔离闸只读审计")
    ap.add_argument("--data-root", default="", help="实例数据根（默认自动发现）")
    ap.add_argument("--db", default="", help="直接指定 inbox.db（复盘异机时用）")
    ap.add_argument("--at", default="", help="按某一时刻判定（默认现在）")
    ap.add_argument("--scan-limit", type=int, default=0,
                    help="覆盖生产 scan_limit（默认读配置，缺省 200）")
    ap.add_argument("--min-silent-hours", type=float, default=-1.0,
                    help="覆盖沉默阈值（默认读配置，缺省 24）")
    ap.add_argument("--quota-exhausted", action="store_true",
                    help="按「额度已耗尽」判定（复盘 .198 那种现场）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = _parse_at(args.at)
    reports = []
    for label, db, cfg_all in _resolve_dbs(args):
        pt = (((cfg_all.get("companion") or {}).get("proactive_topic")) or {})
        scan_limit = args.scan_limit or int(pt.get("scan_limit", 200) or 200)
        min_silent = (args.min_silent_hours if args.min_silent_hours >= 0
                      else float(pt.get("min_silent_hours", 24) or 24))
        rows = load_rows(db, scan_limit)
        rep = audit_rows(rows, cfg=resolve_cold_start_cfg(cfg_all), now=now,
                         min_silent_hours=min_silent,
                         quota_exhausted=bool(args.quota_exhausted))
        rep["label"] = label
        rep["db"] = str(db)
        rep["scan_limit"] = scan_limit
        reports.append(rep)

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print("=" * 68)
            print("库：%s（scan_limit=%d）" % (rep["db"], rep["scan_limit"]))
            print(render(rep))
    # 隔离期内还有账号 → 退出码 10，便于脚本判「现在别放量」
    warming = any(
        a["account_age_hours"] < float(r["config"].get("warmup_hours") or 0)
        for r in reports for a in r["accounts"])
    return 10 if warming else 0


if __name__ == "__main__":
    raise SystemExit(main())
