# -*- coding: utf-8 -*-
"""Phase 12.3 smoke: 启用自动化前跑一次看会发什么.

用法:
    python scripts/referral_dispatch_dry_run.py \
        [--api-base http://127.0.0.1:18080] \
        [--api-key <KEY>] \
        [--device 8DWOF6CYY5R8YHX8] \
        [--hours-window 24] \
        [--limit 5]

流程:
  1. 触发 facebook_line_dispatch_from_reply (dry_run=true) — 看 planned 候选
  2. 触发 facebook_send_referral_replies    (dry_run=true) — 看 would_send template
  3. 打印 stats 给运营决定是否把 scheduled_jobs 里两个 task enabled=true

不修改任何 DB, 不发任何 Messenger 消息.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _post_task(api_base: str, api_key: str, task_type: str,
                device_id: str, params: dict) -> dict:
    req = urllib.request.Request(
        f"{api_base.rstrip('/')}/tasks",
        method="POST",
        headers={"Content-Type": "application/json",
                  "X-API-Key": api_key} if api_key
                 else {"Content-Type": "application/json"},
        data=json.dumps({
            "type": task_type,
            "device_id": device_id,
            "params": params,
        }).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[ERROR] HTTP {e.code}: {body[:500]}", file=sys.stderr)
        sys.exit(1)


def _poll_task(api_base: str, api_key: str, task_id: str,
                timeout_sec: int = 120) -> dict:
    headers = {"X-API-Key": api_key} if api_key else {}
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        req = urllib.request.Request(
            f"{api_base.rstrip('/')}/tasks/{task_id}",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if data.get("status") in {"completed", "failed", "error", "timeout"}:
            return data
        time.sleep(1.5)
    print(f"[TIMEOUT] task {task_id} 超过 {timeout_sec}s 仍未完成",
           file=sys.stderr)
    return {"status": "timeout"}


def _pprint_dispatches(stats: dict) -> None:
    print(f"  scanned     = {stats.get('scanned', 0)}")
    print(f"  dispatched  = {stats.get('dispatched', 0)}")
    print(f"  filtered    = {stats.get('filtered_out', 0)}")
    print(f"  no_account  = {stats.get('no_account', 0)}")
    print(f"  dry_run     = {stats.get('dry_run')}")
    dps = stats.get("dispatches", [])
    if dps:
        print("  --- dispatches ---")
        for d in dps[:10]:
            age = (d.get("metadata") or {}).get("age_band", "?")
            gen = (d.get("metadata") or {}).get("gender", "?")
            print(f"    peer={d['peer_name']:<16} "
                   f"line_id={d['line_id']} "
                   f"age={age} gender={gen} "
                   f"template={str(d.get('message_template',''))[:60]}")


def _pprint_outcomes(stats: dict) -> None:
    print(f"  scanned        = {stats.get('scanned', 0)}")
    print(f"  sent/dry_run_ok = {stats.get('sent', 0)}")
    print(f"  failed         = {stats.get('failed', 0)}")
    print(f"  skipped_dedup  = {stats.get('skipped_dedup', 0)}")
    print(f"  skipped_device = {stats.get('skipped_device', 0)}")
    print(f"  skipped_mode   = {stats.get('skipped_mode', 0)}")
    print(f"  dry_run        = {stats.get('dry_run')}")
    out = stats.get("outcomes", [])
    if out:
        print("  --- outcomes (would send) ---")
        for o in out[:10]:
            print(f"    peer={o['peer_name']:<16} "
                   f"line_id={o.get('line_id','?')} "
                   f"err_code={o.get('err_code','')} "
                   f"template={str(o.get('would_send_template') or o.get('note',''))[:60]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-base",
                     default=os.environ.get("OPENCLAW_API_BASE",
                                              "http://127.0.0.1:18080"))
    ap.add_argument("--api-key",
                     default=os.environ.get("OPENCLAW_API_KEY", ""))
    ap.add_argument("--device", required=True,
                     help="真机 device_id (send 任务需要)")
    ap.add_argument("--hours-window", type=int, default=24)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--persona-key", default="jp_female_midlife")
    ap.add_argument("--region", default="jp")
    args = ap.parse_args()

    print(f"# Phase 12.3 referral smoke — api_base={args.api_base}")
    print(f"# device={args.device} persona={args.persona_key} region={args.region}")
    print(f"# hours_window={args.hours_window} limit={args.limit}")
    print()

    print("== 1) facebook_line_dispatch_from_reply (dry_run) ==")
    r1 = _post_task(args.api_base, args.api_key,
                      "facebook_line_dispatch_from_reply", args.device, {
                          "hours_window": args.hours_window,
                          "dedupe_hours": 24,
                          "require_l2_verified": True,
                          "persona_key": args.persona_key,
                          "region": args.region,
                          "limit": args.limit,
                          "dry_run": True,
                          "write_contact_event": False,
                      })
    tid1 = r1.get("task_id")
    if not tid1:
        print("[ERROR] 没拿到 task_id:", r1)
        sys.exit(1)
    done1 = _poll_task(args.api_base, args.api_key, tid1)
    _pprint_dispatches((done1.get("result") or {}))

    print()
    print("== 2) facebook_send_referral_replies (dry_run) ==")
    r2 = _post_task(args.api_base, args.api_key,
                      "facebook_send_referral_replies", args.device, {
                          "hours_window": args.hours_window,
                          "dedupe_hours": 24,
                          "strict_device_match": True,
                          "limit": args.limit,
                          "max_retry": 0,
                          "min_interval_sec": 0,
                          "max_interval_sec": 0,
                          "dry_run": True,
                      })
    tid2 = r2.get("task_id")
    if not tid2:
        print("[ERROR] 没拿到 task_id:", r2)
        sys.exit(1)
    done2 = _poll_task(args.api_base, args.api_key, tid2)
    _pprint_outcomes((done2.get("result") or {}))

    print()
    print("# DONE — 没改任何 DB / 没发任何消息.")
    print("# 若以上看起来符合预期, 可把 scheduled_jobs.json 中")
    print("#   line_dispatch_30min + send_referral_replies_30min")
    print("# 改 enabled=true, 重启服务即启用自动化.")


if __name__ == "__main__":
    main()
