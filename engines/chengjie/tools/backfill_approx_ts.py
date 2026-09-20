# -*- coding: utf-8 -*-
"""存量「合成时间戳」行补标 approx_ts=1（实施72 P5-a，一次性修复，非周期任务）。

## 为什么需要

Messenger 网页 DOM 拿不到绝对时间：P4 之前 worker 一律送 `ts:0`，引擎按数组序
合成时间（`导入时刻 − n + i`）。8/27 换号事故里找回的历史就是这样落库的——26 条
里 18 条声称「今天 02:11」，真实时间其实横跨 8/12~8/19。诚实标注（`approx_ts`）
的三个消费者（前端「≈」标 / 拟稿上下文「补收的历史消息」/ 回复时延 SLO 剔除）
都是**列建好之后**才上线的，于是**列之前落库的行全是 approx_ts=0**——错时间还
冒充精确时间。本工具把它们补标回来。

补标本身也是 **P5-b 自愈校正的前置**：`store.correct_approx_ts` 只动 approx 行，
补标后 worker 下一次重推（带 aria 解析出的真实 epoch）就会把 ts 就地修对。

## 判据（两条必须同时成立，实测零假阳性）

1. `ingested_at − ts > --min-lag`（默认 1h）：声称的时间远早于入库时刻；
2. 该行处在**步长恰好 1.000 秒的等差串**里（串长 ≥`--run`）——这是合成公式
   `base − n + i` 的指纹：步长永远精确 1 秒。串内步长不齐（1s 混 2s）＝形状不符，
   整串放过。

**为什么第 2 条不能省**：P4 之后 worker 会送真实 epoch，真历史行同样满足条件 1
（8/18 的消息今天入库）。真实对话不会连续精确 1.000 秒一条——轮询本身 4 秒一拍，
单拍内批量入库也不会造出整齐步进。本机实测：旧账号 202 条真实历史行零命中，
现役账号 18 条合成行全命中。

**锚点行护栏**：合成是从「库内已有的最早那条真实消息」往前倒推的，所以那条真消息
恰好落在串的末端、与合成段严丝合缝（实测 02:12:04 那条实时消息就被卷进 15 连串）。
串末行**带 platform_msg_id 而串内其他行没有** → 判定它是被倒推公式借用的真实锚点，
从名单剔除。（全员带 id 的串＝整串都是回填来的，不剔除。）

**刻意不做周期任务**：判据是启发式的，越跑越久越可能撞上「真实的密集突发」。
用法固定为「人看 dry-run 清单 → 确认 → --apply」，且 --apply 会落审计 JSONL。

用法::

    python tools/backfill_approx_ts.py                     # dry-run（默认）
    python tools/backfill_approx_ts.py --apply             # 真写 + 落审计
    python tools/backfill_approx_ts.py --prefix telegram:  # 换平台前缀（默认 messenger:）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ENGINE_ROOT))
sys.path.insert(0, str(_ENGINE_ROOT / "scripts"))


def _out(s: str = "") -> None:
    sys.stdout.buffer.write((s + "\n").encode("utf-8"))


def _hhmm(ts: Any) -> str:
    try:
        return time.strftime("%m-%d %H:%M:%S", time.localtime(float(ts or 0)))
    except (TypeError, ValueError, OSError):
        return "??"


def select_synthetic_rows(
    con: sqlite3.Connection,
    *,
    prefix: str,
    min_lag: float,
    step: float,
    run_len: int,
) -> List[Dict[str, Any]]:
    """挑出「合成形状的等差串成员、且尚未标注」的行（纯读）。"""
    hits: List[Dict[str, Any]] = []
    cids = [r[0] for r in con.execute(
        "SELECT DISTINCT conversation_id FROM messages "
        "WHERE conversation_id LIKE ? ORDER BY conversation_id",
        (prefix + "%",))]
    for cid in cids:
        rows = [dict(r) for r in con.execute(
            "SELECT message_id, ts, ingested_at, direction, platform_msg_id pmid, "
            "COALESCE(approx_ts,0) apx, "
            "substr(replace(text, char(10), ' '), 1, 40) t "
            "FROM messages WHERE conversation_id = ? ORDER BY ts", (cid,))]
        in_run = [False] * len(rows)
        i = 0
        while i < len(rows):
            j = i
            while (j + 1 < len(rows)
                   and 0 < (float(rows[j + 1]["ts"] or 0)
                            - float(rows[j]["ts"] or 0)) <= step):
                j += 1
            seg = rows[i:j + 1]
            if len(seg) >= run_len and all(
                    abs((float(b["ts"] or 0) - float(a["ts"] or 0)) - 1.0) < 1e-3
                    for a, b in zip(seg, seg[1:])):
                for k in range(i, j + 1):
                    in_run[k] = True
                # 锚点护栏：串末那条若**独有** platform_msg_id，它就是倒推公式
                # 借用的真实消息（实测 02:12:04 实时行被卷进 15 连串）→ 放过
                if str(seg[-1]["pmid"] or "").strip() and any(
                        not str(r["pmid"] or "").strip() for r in seg[:-1]):
                    in_run[j] = False
            i = j + 1 if j > i else i + 1
        for k, r in enumerate(rows):
            lag = float(r["ingested_at"] or 0) - float(r["ts"] or 0)
            if int(r["apx"] or 0):
                continue                      # 已标注：写入路径已负责
            if lag <= min_lag or not in_run[k]:
                continue
            r["conversation_id"] = cid
            r["lag"] = lag
            hits.append(r)
    return hits


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="存量合成时间戳行补标 approx_ts")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--prefix", default="messenger:",
                    help="会话 id 前缀（默认 messenger:——唯一会合成 ts 的平台）")
    ap.add_argument("--min-lag", type=float, default=3600.0)
    ap.add_argument("--step", type=float, default=2.0)
    ap.add_argument("--run", type=int, default=3)
    ap.add_argument("--apply", action="store_true", help="真写（默认只看清单）")
    args = ap.parse_args()

    from _data_root import resolve_data_roots
    root = Path(resolve_data_roots(args.data_root)[0])
    db = root / "config" / "inbox.db"
    if not db.exists():
        _out(f"inbox 库不存在: {db}")
        return 1

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {c[1] for c in con.execute("pragma table_info(messages)")}
    if "approx_ts" not in cols:
        _out("messages.approx_ts 列不存在——先重启实例跑迁移，再补标")
        con.close()
        return 1
    hits = select_synthetic_rows(con, prefix=args.prefix, min_lag=args.min_lag,
                                step=args.step, run_len=args.run)
    total_unmarked = con.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id LIKE ? "
        "AND COALESCE(approx_ts,0)=0", (args.prefix + "%",)).fetchone()[0]
    con.close()

    _out(f"=== 存量合成 ts 补标 {'（APPLY）' if args.apply else '（dry-run）'} "
         f"root={root} prefix={args.prefix} ===")
    _out(f"未标注行合计 {total_unmarked}，命中判据 {len(hits)} 行"
         f"（lag>{args.min_lag:.0f}s 且处在 ≥{args.run} 条、步长≤{args.step:.0f}s 的等差串）")
    by_cid: Dict[str, int] = {}
    for r in hits:
        by_cid[r["conversation_id"]] = by_cid.get(r["conversation_id"], 0) + 1
    for cid, n in sorted(by_cid.items(), key=lambda kv: -kv[1]):
        _out(f"  {cid}  {n} 行")
    _out()
    for r in hits:
        _out(f"  ts={_hhmm(r['ts'])} 入库={_hhmm(r['ingested_at'])} "
             f"lag={r['lag'] / 3600:.1f}h {r['direction']:>3} {r['t']}")
    if not hits:
        _out("无需补标。")
        return 0
    if not args.apply:
        _out()
        _out("以上为 dry-run。确认清单无误后加 --apply 真写。")
        return 0

    audit = root / "logs" / f"approx_backfill_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    audit.parent.mkdir(parents=True, exist_ok=True)
    with audit.open("w", encoding="utf-8") as fh:
        for r in hits:
            fh.write(json.dumps(
                {"message_id": r["message_id"], "conversation_id": r["conversation_id"],
                 "ts": r["ts"], "ingested_at": r["ingested_at"],
                 "approx_ts_before": 0, "approx_ts_after": 1},
                ensure_ascii=False) + "\n")
    rw = sqlite3.connect(str(db))
    try:
        cur = rw.executemany(
            "UPDATE messages SET approx_ts = 1 WHERE message_id = ? "
            "AND COALESCE(approx_ts,0) = 0",
            [(r["message_id"],) for r in hits])
        rw.commit()
        _out(f"已补标 {cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(hits)} 行；"
             f"审计 → {audit}")
    finally:
        rw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
