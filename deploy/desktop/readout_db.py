"""ChatX 桌面节点 inbox.db 读数分析器（观察期量化用，stdlib-only）。

用法：python readout_db.py <inbox.db 副本路径> [--days 7]

对**拷贝出来的**库做只读统计（chatx_readout.ps1 先 scp 再调本脚本；
不要直接指向正在运行的节点库）。输出 ASCII 表——PS5.1 会把无 BOM UTF-8
当 GBK 解码，任何非 ASCII 输出在远程链路上都可能变成乱码。
旧构建缺表/缺列一律容错跳过，绝不抛。
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from typing import Any, List, Tuple


def _q(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> List[Tuple[Any, ...]]:
    try:
        return conn.execute(sql, params).fetchall()
    except Exception:
        return []


def _q1(conn: sqlite3.Connection, sql: str, params: Tuple = ()) -> Any:
    rows = _q(conn, sql, params)
    return rows[0][0] if rows and rows[0] else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    now = time.time()
    day_ago = now - 86400
    win_ago = now - args.days * 86400

    print("== conversations ==")
    for plat, n in _q(conn, "SELECT platform, COUNT(*) FROM conversations GROUP BY platform ORDER BY 2 DESC"):
        print(f"  {plat:<12} {n}")
    n_colon = _q1(conn, "SELECT COUNT(*) FROM conversations WHERE platform='whatsapp' AND chat_key GLOB '*:*'")
    n_zero = _q1(conn, "SELECT COUNT(*) FROM conversations WHERE platform='whatsapp' AND chat_key='0'")
    print(f"  [health] wa colon-key convs: {n_colon} (expect 0)")
    print(f"  [health] wa zero-key convs:  {n_zero} (expect 0)")

    print(f"== messages (24h / {args.days}d) ==")
    for label, t0 in (("24h", day_ago), (f"{args.days}d", win_ago)):
        n_in = _q1(conn, "SELECT COUNT(*) FROM messages WHERE direction='in' AND ts>=?", (t0,))
        n_out = _q1(conn, "SELECT COUNT(*) FROM messages WHERE direction='out' AND ts>=?", (t0,))
        # 出站翻译判据＝original_text（坐席原文）与 text（实际发出）不同——
        # 镜像行的 translated_text 语义随路径漂移，不可靠
        n_tx = _q1(
            conn,
            "SELECT COUNT(*) FROM messages WHERE direction='out' AND ts>=? "
            "AND original_text != '' AND original_text != text", (t0,))
        pct = (100.0 * n_tx / n_out) if n_out else 0.0
        print(f"  {label:<4} in={n_in:<5} out={n_out:<5} out_translated={n_tx} ({pct:.0f}%)")

    print("== drafts ==")
    for status, n in _q(conn, "SELECT status, COUNT(*) FROM reply_drafts GROUP BY status ORDER BY 2 DESC"):
        print(f"  {status:<12} {n}")
    n_d24 = _q1(conn, "SELECT COUNT(*) FROM reply_drafts WHERE created_ts>=?", (day_ago,))
    print(f"  created last 24h: {n_d24}")

    print(f"== top active convs ({args.days}d, by outbound) ==")
    for cid, n in _q(
        conn,
        "SELECT conversation_id, COUNT(*) AS c FROM messages "
        "WHERE direction='out' AND ts>=? GROUP BY conversation_id "
        "ORDER BY c DESC LIMIT 3", (win_ago,)):
        print(f"  {n:>4}  {cid}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
