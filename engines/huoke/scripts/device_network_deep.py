#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深度网络诊断 — 看 WiFi 是否真连了 + 移动数据真实状态.

shell 不够时用更详细命令:
- ip addr show wlan0 / rmnet0 (有无 IP)
- dumpsys wifi | grep "Wi-Fi is"
- dumpsys connectivity | grep "ActiveNetwork"
- getprop gsm.sim.state
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

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
        out = proc.stdout.decode("utf-8", errors="replace")
        return out
    except Exception as e:
        return ""


def deep_check(host_ip, user, adb_path, device_id):
    base = f'"{adb_path}" -s {device_id} shell'
    res = {"device_id": device_id, "host_ip": host_ip}

    # WiFi: ip + ssid
    out = ssh(host_ip, user,
              f'{base} "ip -4 addr show wlan0 2>/dev/null | grep inet"',
              timeout=8)
    res["wlan0_ip"] = out.strip()[:80] if out.strip() else "no_ip"

    out = ssh(host_ip, user,
              f'{base} "dumpsys wifi | grep -E \'mWifiInfo|SSID|state:\' | head -3"',
              timeout=10)
    res["wifi_dump"] = (out.strip()[:200] if out.strip() else "?").replace("\n", " | ")

    # 移动数据: rmnet IP
    out = ssh(host_ip, user,
              f'{base} "ip -4 addr show 2>/dev/null | grep -E \'rmnet|tethering|wwan\' -A 2 | grep inet"',
              timeout=8)
    res["mobile_ip"] = out.strip()[:80] if out.strip() else "no_mobile"

    # 默认路由 (走 wlan0 还是 rmnet0)
    out = ssh(host_ip, user,
              f'{base} "ip route | grep default"',
              timeout=8)
    res["default_route"] = (out.strip()[:120] if out.strip() else "no_default").replace("\n", " | ")

    # SIM 卡状态 (READY/PIN_REQUIRED/ABSENT)
    out = ssh(host_ip, user,
              f'{base} "getprop gsm.sim.state && getprop gsm.network.type && getprop gsm.operator.alpha"',
              timeout=8)
    res["sim_state"] = (out.strip()[:120] if out.strip() else "?").replace("\n", " | ")

    # ADB 端的 connectivity
    out = ssh(host_ip, user,
              f'{base} "dumpsys connectivity | grep -E \'NetworkAgentInfo|Active|hasInternet\' | head -3"',
              timeout=10)
    res["connectivity"] = (out.strip()[:200] if out.strip() else "?").replace("\n", " | ")

    return res


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--healthy-only", action="store_true",
                   help="只看 healthy 设备 (从 device_diagnose.json 读)")
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()

    # 读上一次诊断结果选 device
    diag_path = "reports/device_diagnose.json"
    if not os.path.exists(diag_path):
        print(f"❌ 先跑 scripts/device_diagnose.py 生成 {diag_path}")
        return 1
    diags = json.load(open(diag_path, encoding="utf-8"))
    if args.healthy_only:
        diags = [d for d in diags if d["verdict"] == "healthy"]
    if args.limit:
        diags = diags[:args.limit]

    print(f"深度诊断 {len(diags)} 设备...")
    print()

    results = []
    by_host: Dict[str, List] = {"w03": [], "worker-175": []}
    for d in diags:
        by_host.setdefault(d.get("host", "?"), []).append(d)

    for host_key, host_diags in by_host.items():
        if not host_diags:
            continue
        w = WORKERS.get(host_key)
        if not w:
            continue
        print(f"━━━ {host_key} ━━━")
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(deep_check, w["ip"], w["user"], w["adb"],
                                  d["device_id"]) for d in host_diags]
            for f in as_completed(futures):
                r = f.result()
                r["host"] = host_key
                # 找原诊断的 verdict
                orig = next((d for d in host_diags
                              if d["device_id"] == r["device_id"]), {})
                r["orig_verdict"] = orig.get("verdict", "?")
                results.append(r)

                v = "✓" if "ok" in r.get("default_route", "") and r["wlan0_ip"] != "no_ip" or r["mobile_ip"] != "no_mobile" else "✗"
                # 简洁展示
                wlan_ok = r["wlan0_ip"] != "no_ip"
                mobile_ok = r["mobile_ip"] != "no_mobile"
                interface = "wlan0" if "wlan0" in r["default_route"] else "rmnet" if "rmnet" in r["default_route"] else "?"
                print(f"  {r['device_id'][:14]} verdict={r['orig_verdict']:30s} "
                      f"wlan0_ip={'Y' if wlan_ok else 'N'} "
                      f"mobile_ip={'Y' if mobile_ok else 'N'} "
                      f"default_via={interface}")
                if r['orig_verdict'] != 'healthy':
                    if wlan_ok:
                        print(f"     wlan0: {r['wlan0_ip'][:60]}")
                    if mobile_ok:
                        print(f"     mobile: {r['mobile_ip'][:60]}")
                    print(f"     default: {r['default_route'][:100]}")
                    print(f"     SIM: {r['sim_state'][:80]}")

    out_path = "reports/device_network_deep.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print()
    print(f"📁 详情: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
