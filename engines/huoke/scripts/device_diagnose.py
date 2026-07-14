#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备网络诊断 + 自动修复.

通过 SSH 到 worker (W03/W175), 用 adb shell 探查每台设备的:
- 飞行模式 airplane_mode_on
- WiFi wifi_on
- 移动数据 mobile_data_on
- ping 8.8.8.8 (真实联网测试)
- ADB connection 状态

用法:
    python scripts/device_diagnose.py                  # 诊断全部
    python scripts/device_diagnose.py --fix            # 诊断 + 自动修复 (关飞行模式 / 开 WiFi)
    python scripts/device_diagnose.py --workers w03    # 只 W03
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
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


WORKERS = {
    "w03": {"ip": "192.168.0.103", "ssh_user": "administrator",
             "adb": r"C:\platform-tools\adb.exe"},
    "worker-175": {"ip": "192.168.0.175", "ssh_user": "administrator",
                    "adb": r"C:\platform-tools\adb.exe"},
}
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa")


def ssh_run(host_ip: str, user: str, cmd: str,
             timeout: float = 15.0) -> Tuple[int, str]:
    """SSH 执行一条命令, 返 (rc, stdout)."""
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no",
             "-o", f"ConnectTimeout={int(timeout)}",
             "-i", SSH_KEY, f"{user}@{host_ip}", cmd],
            capture_output=True, timeout=timeout,
        )
        # SSH 输出可能含 GBK
        try:
            stdout = proc.stdout.decode("utf-8", errors="replace")
        except Exception:
            stdout = proc.stdout.decode("gbk", errors="replace")
        return proc.returncode, stdout
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except Exception as e:
        return -1, str(e)


def list_devices(host_ip: str, user: str, adb_path: str) -> List[str]:
    rc, out = ssh_run(host_ip, user, f'"{adb_path}" devices', timeout=12)
    devices = []
    for line in out.splitlines():
        line = line.strip()
        # 格式: "DEVICEID\tdevice"
        if "\tdevice" in line and not line.startswith("List"):
            devices.append(line.split("\t")[0].strip())
    return devices


def diagnose_one_device(host_ip: str, user: str, adb_path: str,
                         device_id: str) -> Dict[str, Any]:
    """对一台设备跑 adb shell 一系列状态查询."""
    res: Dict[str, Any] = {
        "device_id": device_id,
        "host_ip": host_ip,
        "checks": {},
    }
    base = f'"{adb_path}" -s {device_id} shell'

    # 1. 飞行模式 (0=关 / 1=开)
    rc, out = ssh_run(host_ip, user, f"{base} settings get global airplane_mode_on",
                       timeout=8)
    val = (out or "").strip().split("\n")[0] if out else "?"
    res["checks"]["airplane_mode"] = val if val.isdigit() else "?"

    # 2. WiFi 状态 (这个 settings key 在 MIUI 不一定准, 但能看)
    rc, out = ssh_run(host_ip, user, f"{base} settings get global wifi_on",
                       timeout=8)
    val = (out or "").strip().split("\n")[0] if out else "?"
    res["checks"]["wifi_on"] = val if val.isdigit() else "?"

    # 3. 移动数据
    rc, out = ssh_run(host_ip, user, f"{base} settings get global mobile_data",
                       timeout=8)
    val = (out or "").strip().split("\n")[0] if out else "?"
    res["checks"]["mobile_data"] = val if val.isdigit() else "?"

    # 4. ping 8.8.8.8 (真实联网测试)
    rc, out = ssh_run(host_ip, user,
                       f"{base} ping -c 1 -W 3 8.8.8.8 2>&1", timeout=12)
    if "1 received" in out or "1 packets received" in out:
        res["checks"]["ping_8888"] = "ok"
    elif "100% packet loss" in out or "100%% packet loss" in out:
        res["checks"]["ping_8888"] = "no_internet"
    elif rc == -1 or "TIMEOUT" in out:
        res["checks"]["ping_8888"] = "timeout"
    elif "ping: " in out and "permitted" in out:
        res["checks"]["ping_8888"] = "blocked"
    else:
        res["checks"]["ping_8888"] = (out or "")[:80]

    # 5. ip route default (看默认网关存在否, 不存在 = 没网)
    rc, out = ssh_run(host_ip, user, f"{base} ip route 2>&1", timeout=8)
    has_default = "default" in (out or "")
    res["checks"]["default_route"] = "ok" if has_default else "missing"

    # 6. 上行 connectivity (curl / wget 验证 dns + http; Android shell 上一般有 wget)
    rc, out = ssh_run(host_ip, user,
                       f"{base} echo TEST_DNS && {base} getprop net.dns1",
                       timeout=8)
    dns1 = ""
    for line in out.splitlines():
        if "." in line and not line.startswith("TEST"):
            dns1 = line.strip()
            break
    res["checks"]["dns1"] = dns1 or "?"

    # 综合判定
    a = res["checks"]["airplane_mode"]
    p = res["checks"]["ping_8888"]
    if a == "1":
        res["verdict"] = "fixable_airplane"
    elif p == "ok":
        res["verdict"] = "healthy"
    elif p == "timeout":
        res["verdict"] = "adb_unstable"
    elif p == "no_internet":
        res["verdict"] = "no_internet_check_sim_wifi"
    elif p == "blocked":
        res["verdict"] = "icmp_blocked_check_http"
    else:
        res["verdict"] = f"unknown ({p[:30]})"

    return res


def fix_device(host_ip: str, user: str, adb_path: str,
                diag: Dict[str, Any]) -> Dict[str, Any]:
    """根据诊断结果尝试自动修复."""
    device = diag["device_id"]
    base = f'"{adb_path}" -s {device} shell'
    actions = []

    a = diag["checks"].get("airplane_mode")
    if a == "1":
        # 关飞行模式: settings put + broadcast
        ssh_run(host_ip, user,
                f"{base} settings put global airplane_mode_on 0", timeout=5)
        ssh_run(host_ip, user,
                f'{base} cmd connectivity airplane-mode disable', timeout=5)
        actions.append("airplane_mode_off")
        time.sleep(2)

    w = diag["checks"].get("wifi_on")
    if w == "0":
        # 开 WiFi
        ssh_run(host_ip, user, f'{base} svc wifi enable', timeout=5)
        actions.append("wifi_enable")
        time.sleep(2)

    m = diag["checks"].get("mobile_data")
    if m == "0":
        ssh_run(host_ip, user, f'{base} svc data enable', timeout=5)
        actions.append("data_enable")

    return {"device_id": device, "actions": actions}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workers", default="w03,worker-175",
                   help="comma-separated worker keys")
    p.add_argument("--fix", action="store_true",
                   help="自动修复能修的 (飞行模式/WiFi)")
    p.add_argument("--output", default="reports/device_diagnose.json")
    args = p.parse_args()

    targets = {k: v for k, v in WORKERS.items()
                if k in args.workers.split(",")}

    all_diags = []

    print(_color("1;36", f"设备诊断 · {len(targets)} workers"))

    for w_key, w in targets.items():
        print()
        print(_color("36", f"━━━ {w_key} ({w['ip']}) ━━━"))
        devices = list_devices(w["ip"], w["ssh_user"], w["adb"])
        print(f"  adb devices: {len(devices)}")
        if not devices:
            continue

        # 并发诊断 (8 设备并行)
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {
                ex.submit(diagnose_one_device, w["ip"], w["ssh_user"],
                          w["adb"], d): d for d in devices
            }
            for f in as_completed(futures):
                diag = f.result()
                diag["host"] = w_key
                all_diags.append(diag)
                v = diag["verdict"]
                col = ("32" if v == "healthy"
                       else "33" if v.startswith("fixable")
                       else "31" if "internet" in v else "33")
                a = diag["checks"]["airplane_mode"]
                p_ = diag["checks"]["ping_8888"]
                print(f"  {_color(col, '●')} {diag['device_id'][:14]} "
                      f"airplane={a} wifi={diag['checks']['wifi_on']} "
                      f"data={diag['checks']['mobile_data']} "
                      f"ping={p_} → {_color(col, v)}")

    # 总结
    print()
    print(_color("1;36", "━━━━━━ 总结 ━━━━━━"))
    from collections import Counter
    by_verdict = Counter(d["verdict"] for d in all_diags)
    for v, n in by_verdict.most_common():
        col = "32" if v == "healthy" else "33" if v.startswith("fixable") else "31"
        print(f"  {_color(col, v):40s} {n}")

    # 写 JSON 详情
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(all_diags, f, ensure_ascii=False, indent=2, default=str)
    print()
    print(f"📁 详情: {args.output}")

    # 修复
    if args.fix:
        fixable = [d for d in all_diags
                    if d["verdict"].startswith("fixable") or
                    d["checks"].get("airplane_mode") == "1" or
                    d["checks"].get("wifi_on") == "0"]
        print()
        print(_color("1;36", f"━━━━━━ 自动修复 {len(fixable)} 设备 ━━━━━━"))
        for diag in fixable:
            host_ip = diag["host_ip"]
            host_key = diag["host"]
            w = WORKERS[host_key]
            r = fix_device(host_ip, w["ssh_user"], w["adb"], diag)
            if r["actions"]:
                print(f"  {diag['device_id'][:14]:16s} → "
                      f"{', '.join(r['actions'])}")
        print()
        print(_color("33", "等 5s 后重新诊断 ping..."))
        time.sleep(5)
        # 再 ping 一遍验证
        for diag in fixable:
            w = WORKERS[diag["host"]]
            base = f'"{w["adb"]}" -s {diag["device_id"]} shell'
            rc, out = ssh_run(w["ip"], w["ssh_user"],
                              f"{base} ping -c 1 -W 3 8.8.8.8", timeout=10)
            if "1 received" in out or "1 packets received" in out:
                print(f"  ✓ {diag['device_id'][:14]} 修复成功 ping ok")
            else:
                snippet = out.strip().split("\n")[-1][:80] if out else "?"
                print(f"  ✗ {diag['device_id'][:14]} 仍无网: {snippet}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
