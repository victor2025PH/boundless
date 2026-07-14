#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""集群真实负载测试 — 验证 21 个设备真协同跑.

用法:
    python scripts/cluster_load_test.py                 # 默认 read-only tiktok_status
    python scripts/cluster_load_test.py --task tiktok_status --workers 21

不会发真消息或加陌生人; 只跑 read-only 状态查询.

测试维度:
1. 派发延迟 (POST /tasks 的 P50/P95/P99)
2. 执行延迟 (created_at → completed_at)
3. 分流准确率 (W03/W175 设备的任务真去对应 worker, 不被 fallback)
4. 并发协同 (同时 N 个任务跑, 不互相阻塞)
5. 错误分布 (per-worker 成功率)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty():
        return msg
    return f"\033[{code}m{msg}\033[0m"


def _fetch(base: str, path: str, timeout: float = 8.0) -> Optional[Any]:
    try:
        req = urllib.request.Request(base.rstrip("/") + path)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return {"_error": str(e)}


def _post(base: str, path: str, body: Dict[str, Any],
           timeout: float = 10.0) -> Tuple[int, Any, float]:
    """Returns (http_code, body, elapsed_ms)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200, json.loads(r.read()), (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace"), (time.time() - t0) * 1000
    except Exception as e:
        return 0, str(e), (time.time() - t0) * 1000


# ── stages ──────────────────────────────────────────────────────────
def collect_devices(base: str) -> List[Dict[str, Any]]:
    data = _fetch(base, "/cluster/devices")
    devices = (data or {}).get("devices", []) if isinstance(data, dict) else []
    by_host: Dict[str, int] = Counter(d.get("host_id", "?") for d in devices)
    print(f"  cluster devices: {len(devices)} 总")
    for h, n in by_host.items():
        print(f"    {h}: {n}")
    return [d for d in devices if d.get("status") == "connected"]


def dispatch_one(base: str, device: Dict[str, Any], task_type: str
                  ) -> Dict[str, Any]:
    body = {
        "type": task_type,
        "device_id": device["device_id"],
        "params": {
            "_smoke": "cluster_load_test",
            "_target_host": device.get("host_id", "?"),
        },
        "priority": 50,
    }
    code, resp, elapsed_ms = _post(base, "/tasks", body, timeout=15.0)
    return {
        "device_id": device["device_id"],
        "expected_host": device.get("host_id", "?"),
        "expected_host_ip": device.get("host_ip"),
        "post_code": code,
        "post_ms": elapsed_ms,
        "task_id": (resp.get("task_id") if isinstance(resp, dict)
                    else None),
        "post_resp": resp if not isinstance(resp, dict) else None,
    }


def poll_completion(base: str, dispatches: List[Dict[str, Any]],
                     deadline_sec: float = 90.0) -> List[Dict[str, Any]]:
    """Poll each task until completed/failed/timeout."""
    end = time.time() + deadline_sec
    pending = [d for d in dispatches if d.get("task_id")]
    results = {d["task_id"]: d for d in pending}
    completed = set()
    while pending and time.time() < end:
        for d in list(pending):
            tid = d["task_id"]
            if tid in completed:
                continue
            data = _fetch(base, f"/tasks/{tid}", timeout=4.0)
            if not isinstance(data, dict) or "_error" in data:
                continue
            status = data.get("status")
            if status in ("completed", "failed", "cancelled", "timeout"):
                completed.add(tid)
                d["final_status"] = status
                d["created_at"] = data.get("created_at")
                d["updated_at"] = data.get("updated_at")
                result = data.get("result") or {}
                d["success"] = result.get("success")
                d["error"] = (result.get("error") or "")[:200]
                d["screenshot_path"] = result.get("screenshot_path", "")
                # 推断真正的 host (worker_host / dispatched_to / 路径)
                d["actual_host"] = data.get("worker_host") or "?"
                if not d["actual_host"] or d["actual_host"] == "?":
                    sp = result.get("screenshot_path", "")
                    if "C:" in sp and "openclaw" in sp:
                        d["actual_host"] = "remote_worker"
                    elif "D:" in sp and "workspace" in sp:
                        d["actual_host"] = "coord_local"
        pending = [p for p in pending if p["task_id"] not in completed]
        if pending:
            time.sleep(2)
    # 没完成的标 timeout
    for d in dispatches:
        if d.get("task_id") and d["task_id"] not in completed:
            d["final_status"] = "stuck_timeout"
    return dispatches


def summarize(results: List[Dict[str, Any]]) -> None:
    print()
    print(_color("1;36", "═" * 70))
    print(_color("1;36", "  集群负载测试结果"))
    print(_color("1;36", "═" * 70))

    n = len(results)
    post_ok = sum(1 for r in results if r.get("post_code") == 200)
    post_fail = n - post_ok
    print(f"\n  📤 派发阶段 (POST /tasks):")
    print(f"    成功: {post_ok}/{n} ({post_ok/n*100:.0f}%)")
    if post_fail:
        print(_color("31", f"    失败: {post_fail}"))
        for r in results:
            if r.get("post_code") != 200:
                print(_color("31",
                              f"      device={r['device_id'][:12]}… "
                              f"code={r['post_code']} "
                              f"error={str(r.get('post_resp',''))[:80]}"))

    post_times = [r["post_ms"] for r in results if r.get("post_code") == 200]
    if post_times:
        print(f"    POST 延迟: P50={statistics.median(post_times):.0f}ms  "
              f"P95={sorted(post_times)[int(len(post_times)*0.95)]:.0f}ms  "
              f"max={max(post_times):.0f}ms")

    # 完成统计
    final = Counter(r.get("final_status", "no_dispatch") for r in results)
    print(f"\n  📊 完成阶段:")
    for s, c in final.most_common():
        col = ("32" if s == "completed"
               else "33" if s == "stuck_timeout"
               else "31")
        print(f"    {_color(col, s)}: {c}")

    # 执行时长
    exec_times = []
    for r in results:
        c, u = r.get("created_at"), r.get("updated_at")
        if c and u and r.get("final_status") == "completed":
            try:
                from datetime import datetime as _dt
                cs = _dt.fromisoformat(c.replace("Z", "+00:00"))
                us = _dt.fromisoformat(u.replace("Z", "+00:00"))
                exec_times.append((us - cs).total_seconds())
            except Exception:
                pass
    if exec_times:
        print(f"    执行时长: P50={statistics.median(exec_times):.1f}s  "
              f"P95={sorted(exec_times)[int(len(exec_times)*0.95)]:.1f}s  "
              f"max={max(exec_times):.1f}s")

    # 分流准确率
    print(f"\n  🎯 分流准确率 (设备应去其归属 worker):")
    by_host: Dict[str, Dict[str, int]] = defaultdict(
        lambda: {"total": 0, "correct": 0, "stuck": 0, "fail": 0})
    for r in results:
        h = r.get("expected_host", "?")
        by_host[h]["total"] += 1
        if r.get("final_status") == "completed":
            actual = r.get("actual_host", "")
            # local screenshot path → 本机执行 (coord_local)
            # remote_worker → 跑在 worker (correct)
            if h == "coord" or h == "?":
                # coord 本机设备就该跑本机
                if actual in ("coord_local", "?", ""):
                    by_host[h]["correct"] += 1
            else:
                if actual == "remote_worker":
                    by_host[h]["correct"] += 1
                else:
                    by_host[h]["fail"] += 1  # cluster fallback
        elif r.get("final_status") == "stuck_timeout":
            by_host[h]["stuck"] += 1
    for h, st in by_host.items():
        rate = st["correct"] / st["total"] * 100 if st["total"] else 0
        col = "32" if rate >= 90 else "33" if rate >= 50 else "31"
        print(f"    {h}: {_color(col, f'{rate:.0f}%')} 正确  "
              f"({st['correct']}/{st['total']}) "
              f"stuck={st['stuck']} fallback_to_local={st['fail']}")

    # 错误样本 (最多 3 条)
    errors = [r for r in results
               if r.get("final_status") in ("failed", "completed")
               and not r.get("success", True)]
    if errors:
        print(f"\n  ❌ 错误样本 (前 3):")
        for r in errors[:3]:
            print(f"    device={r['device_id'][:12]}… "
                  f"host={r.get('actual_host','?')} "
                  f"err={r.get('error','')[:120]}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default=os.environ.get(
        "OPENCLAW_E2E_BASE", "http://127.0.0.1:8000"))
    p.add_argument("--task", default="tiktok_status",
                   help="任务类型 (默认 read-only tiktok_status)")
    p.add_argument("--limit", type=int, default=0,
                   help="只跑前 N 个 device (0=全部)")
    p.add_argument("--deadline", type=int, default=90,
                   help="等任务完成最长秒数")
    args = p.parse_args()

    print(_color("1;36", f"集群负载测试 · {args.base} · task={args.task}"))
    print()
    print("[Stage 1] 收集 cluster 设备")
    devices = collect_devices(args.base)
    print(f"  status=connected: {len(devices)}")
    if args.limit:
        devices = devices[:args.limit]

    if not devices:
        print(_color("31", "❌ 无可用设备"))
        return 1

    print()
    print(f"[Stage 2] 并发派发 {len(devices)} 个 {args.task} 任务...")
    t0 = time.time()
    dispatches: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(dispatch_one, args.base, d, args.task)
                    for d in devices]
        for f in as_completed(futures):
            dispatches.append(f.result())
    print(f"  全部派发完毕: {(time.time()-t0):.1f}s")

    print()
    print(f"[Stage 3] 等待完成 (最多 {args.deadline}s)...")
    results = poll_completion(args.base, dispatches, deadline_sec=args.deadline)

    summarize(results)

    # 输出 JSON 详情供后续分析
    out_dir = "reports"
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(
        out_dir,
        f"cluster_load_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print()
    print(f"  📁 详情: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
