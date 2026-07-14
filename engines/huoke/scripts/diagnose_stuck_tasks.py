# -*- coding: utf-8 -*-
"""
只读诊断脚本 - 排查某台设备任务长期"等待中"的根因

背景：
    前端任务中心里发现某设备（例如 4HUSIB4T）队列堆积 pending 任务，
    但设备卡片显示"空闲"，线程池从未执行这些任务。
    本脚本不修改任何数据，只 SELECT，用来快速判断以下两条嫌疑路径：

    路径 A：任务创建时 run_on_host=false，等待手机上的 agent 拉取，
            但该机没有 agent 在轮询 → pending 永不会动。
    路径 B：任务失败后写成 pending + next_retry_at，但 host 端没有
            后台循环调用 get_retry_ready_tasks → 重试无人领。

用法：
    python scripts/diagnose_stuck_tasks.py                  # 默认 4HUS 前缀
    python scripts/diagnose_stuck_tasks.py --device AIUKQ
    python scripts/diagnose_stuck_tasks.py --device 4HUS --limit 100
    python scripts/diagnose_stuck_tasks.py --db D:/path/to/openclaw.db

输出：
    1. 设备 pending 概览（总数 / 有 next_retry_at 数 / retry_count 分布）
    2. 最近 N 条 pending 明细（type / params 关键字段 / created_at / updated_at）
    3. 最终给出"最可能根因"判断

退出码：
    0 = 查询成功；非 0 = 数据库连接或查询失败
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from src.host.device_registry import data_file

# 默认数据库路径（与 src/host/database.py 保持一致）
DEFAULT_DB = data_file("openclaw.db")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _age_minutes(ts: str | None) -> float | None:
    dt = _parse_iso(ts)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return round(delta.total_seconds() / 60.0, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description="诊断 pending 任务堆积的根因")
    ap.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"openclaw.db 路径（默认 {DEFAULT_DB}）",
    )
    ap.add_argument(
        "--device",
        default="4HUS",
        help="设备序列号前缀（默认 4HUS 匹配 4HUSIB4T / 4HUS...9TJZ 等）",
    )
    ap.add_argument("--limit", type=int, default=30, help="明细输出行数上限")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[错误] 数据库不存在: {db_path}", file=sys.stderr)
        return 2

    print(f"[诊断] 库: {db_path}")
    print(f"[诊断] 设备前缀: {args.device}")
    print(f"[诊断] 当前时间: {_iso_now()}")
    print("-" * 72)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as err:
        print(f"[错误] 连接数据库失败: {err}", file=sys.stderr)
        return 3

    conn.row_factory = sqlite3.Row
    like = f"{args.device}%"

    # 概览：设备所有状态
    overview = conn.execute(
        """
        SELECT status, COUNT(*) AS cnt
        FROM tasks
        WHERE device_id LIKE ?
          AND (deleted_at IS NULL OR deleted_at = '')
        GROUP BY status
        ORDER BY cnt DESC
        """,
        (like,),
    ).fetchall()
    print("[1] 按状态统计（未删除）：")
    for row in overview:
        print(f"    {row['status']:<12} {row['cnt']}")
    if not overview:
        print("    （无任何任务）")
    print()

    # pending 细分：next_retry_at 是否非空 / retry_count 分布 / 是否有 _created_via
    pending_rows = conn.execute(
        """
        SELECT task_id, type, device_id, priority, retry_count, max_retries,
               next_retry_at, params, created_at, updated_at
        FROM tasks
        WHERE device_id LIKE ?
          AND status = 'pending'
          AND (deleted_at IS NULL OR deleted_at = '')
        ORDER BY created_at DESC
        """,
        (like,),
    ).fetchall()

    total = len(pending_rows)
    has_retry_at = sum(1 for r in pending_rows if r["next_retry_at"])
    retry_at_future = 0
    retry_at_past = 0
    via_counter: Counter[str] = Counter()
    type_counter: Counter[str] = Counter()
    run_on_host_false = 0
    run_on_host_true_or_missing = 0
    now = datetime.now(timezone.utc)

    for row in pending_rows:
        type_counter[row["type"]] += 1
        nra = _parse_iso(row["next_retry_at"])
        if nra:
            if nra.tzinfo is None:
                nra = nra.replace(tzinfo=timezone.utc)
            if nra > now:
                retry_at_future += 1
            else:
                retry_at_past += 1
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        via = str(params.get("_created_via") or params.get("_origin") or "unknown")
        via_counter[via] += 1
        # run_on_host 通常不落库（它是请求体字段），这里只作保守判断
        if params.get("run_on_host") is False:
            run_on_host_false += 1
        else:
            run_on_host_true_or_missing += 1

    print(f"[2] pending 细分（{total} 条）：")
    print(f"    next_retry_at 非空：{has_retry_at}  （未来={retry_at_future}  过去/现在={retry_at_past}）")
    print(f"    params.run_on_host=False：{run_on_host_false}")
    print(f"    其余（run_on_host=True 或未带）：{run_on_host_true_or_missing}")
    print()

    if type_counter:
        print("[3] pending 任务类型分布：")
        for t, c in type_counter.most_common():
            print(f"    {t:<40} {c}")
        print()

    if via_counter:
        print("[4] pending 来源 (_created_via / _origin) 分布：")
        for v, c in via_counter.most_common():
            print(f"    {v:<40} {c}")
        print()

    print(f"[5] 最近 {min(args.limit, total)} 条 pending 明细：")
    header = f"{'task_id':<10} {'type':<34} {'prio':>4} {'retry':>5} {'next_retry_at':<25} {'age(min)':>8}  via"
    print("    " + header)
    for row in pending_rows[: args.limit]:
        tid = row["task_id"][:8]
        typ = row["type"][:34]
        nra = row["next_retry_at"] or "-"
        age = _age_minutes(row["updated_at"] or row["created_at"])
        try:
            params = json.loads(row["params"] or "{}")
        except Exception:
            params = {}
        via = str(params.get("_created_via") or params.get("_origin") or "-")
        retry = f"{row['retry_count']}/{row['max_retries']}"
        age_s = f"{age:.1f}" if age is not None else "-"
        print(f"    {tid:<10} {typ:<34} {row['priority']:>4} {retry:>5} {nra:<25} {age_s:>8}  {via}")
    print()

    # 最终判断
    print("[6] 最可能根因判断：")
    if total == 0:
        print("    无 pending 任务，问题可能已自愈，或你看错了设备。")
    else:
        clues: list[str] = []
        if retry_at_future > total * 0.5:
            clues.append("超半数任务的 next_retry_at 指向未来 → 命中【路径 B 重试无人领】")
        if retry_at_past > 0:
            clues.append(
                f"有 {retry_at_past} 条任务 next_retry_at 已到期却仍 pending → 强烈命中【路径 B】"
            )
        if run_on_host_false > 0:
            clues.append(
                f"有 {run_on_host_false} 条 params.run_on_host=False → 命中【路径 A agent 未在轮询】"
            )
        if via_counter.get("ai_quick", 0) > 0:
            clues.append(
                f"有 {via_counter['ai_quick']} 条来自 ai_quick → AI 快捷指令路径历史上只 create 未 submit（路径 A 变体）"
            )
        if not clues:
            clues.append(
                "没有 next_retry_at，也没有 run_on_host=False 证据 → 可能是 WorkerPool 未调用或进程重启后丢失内存队列；"
                "建议检查 logs/openclaw.log 里 '[pool] 开始执行 ... device=" + args.device + "' 的出现情况。"
            )
        for c in clues:
            print(f"    - {c}")
        print()
        print("[7] 建议：")
        print("    1) 若命中路径 B：在 api.py lifespan 启动后台 pending_rescue_loop，"
              "每 15s 调 task_store.get_retry_ready_tasks 并补 pool.submit。")
        print("    2) 若命中路径 A：要么在手机上运行 openclaw_agent，要么把 run_on_host 强制置 True 并在"
              "routers/ai.py 的 create_task 路径后补 pool.submit。")
        print("    3) 若都不像：重启 host 进程观察是否新建任务也卡；并读 logs/openclaw.log "
              "搜 '4HUS' 看 WorkerPool 是否因 REJECT_HIGH_RISK 等原因拒绝了。")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
