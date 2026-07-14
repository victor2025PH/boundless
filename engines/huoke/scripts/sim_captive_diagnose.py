#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SIM 卡 captive portal 诊断 — 看所有设备是否被运营商强制门户拦截.

通过 curl http://www.google.com/generate_204 → 健康设备返 204, 被劫持的设备返 302+Location.
看 Location 域名能定位运营商问题.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORKERS = {
    "w03": {"ip": "192.168.0.103", "user": "administrator",
             "adb": r"C:\platform-tools\adb.exe"},
    "worker-175": {"ip": "192.168.0.175", "user": "administrator",
                    "adb": r"C:\platform-tools\adb.exe"},
}
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa")


def ssh(host_ip, user, cmd, timeout=10):
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={int(timeout)}",
             "-i", SSH_KEY, f"{user}@{host_ip}", cmd],
            capture_output=True, timeout=timeout)
        return proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


def captive_check(host_ip, user, adb_path, device_id):
    base = f'"{adb_path}" -s {device_id} shell'
    result = {"device_id": device_id}

    # curl Google 204 endpoint, capture http_code + Location header
    out = ssh(host_ip, user,
              f'{base} curl -m 6 -s -o /dev/null -w \\"%{{http_code}}|%{{redirect_url}}\\" http://www.google.com/generate_204',
              timeout=15)
    out = out.strip()
    parts = out.split("|", 1)
    code = parts[0] if parts else "?"
    redir = parts[1] if len(parts) > 1 else ""
    result["http_code"] = code
    result["redirect_to"] = redir[:120]

    # SIM 运营商
    out = ssh(host_ip, user,
              f'{base} getprop gsm.operator.alpha',
              timeout=8)
    result["operator"] = out.strip()[:60]

    # SIM 状态
    out = ssh(host_ip, user,
              f'{base} getprop gsm.sim.state',
              timeout=8)
    result["sim_state"] = out.strip()[:60]

    # 国家
    out = ssh(host_ip, user,
              f'{base} getprop gsm.operator.iso-country',
              timeout=8)
    result["country"] = out.strip()[:8]

    # 综合
    if code == "204":
        result["status"] = "healthy"
    elif code in ("302", "301", "307"):
        # 看 redirect 域名
        m = re.search(r"https?://([^/]+)", redir)
        domain = m.group(1) if m else "?"
        result["status"] = f"captive_portal:{domain}"
    elif code == "000" or code == "":
        result["status"] = "no_internet"
    else:
        result["status"] = f"http_{code}"

    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="reports/sim_captive.json")
    args = p.parse_args()

    all_results = []
    for w_key, w in WORKERS.items():
        # 列设备
        out = ssh(w["ip"], w["user"], f'"{w["adb"]}" devices', timeout=12)
        devs = []
        for line in out.splitlines():
            if "\tdevice" in line and not line.startswith("List"):
                devs.append(line.split("\t")[0].strip())

        print(f"━━━ {w_key} ({len(devs)} 设备) ━━━")
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = [ex.submit(captive_check, w["ip"], w["user"],
                                  w["adb"], d) for d in devs]
            for f in as_completed(futures):
                r = f.result()
                r["host"] = w_key
                all_results.append(r)
                op = r.get("operator", "?")
                cc = r.get("country", "?")
                st = r.get("status", "?")
                col = ("32" if st == "healthy"
                       else "33" if "captive" in st else "31")
                msg = f"  \033[{col}m●\033[0m {r['device_id'][:14]} " \
                      f"http={r['http_code']} country={cc:3s} op={op[:14]:14s} → {st}"
                print(msg)

    # 总结
    from collections import Counter
    print()
    print("━━━━━━ 总结 ━━━━━━")
    by_status = Counter(r["status"] for r in all_results)
    for s, n in by_status.most_common():
        print(f"  {s:50s} {n}")
    print()
    by_country = Counter(r.get("country", "?") for r in all_results)
    print("  国家分布:")
    for c, n in by_country.most_common():
        print(f"    {c}: {n}")

    by_op = Counter(r.get("operator", "?") for r in all_results)
    print("  运营商分布:")
    for o, n in by_op.most_common():
        print(f"    {o}: {n}")

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print()
    print(f"📁 详情: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
