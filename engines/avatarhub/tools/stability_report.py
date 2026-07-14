# -*- coding: utf-8 -*-
"""
Hub 稳定性账本（P11，2026-07-06）

背景：P10 把原生设备栈隔离出 Hub 进程后，终极验收指标是「Hub 非计划重启归零」——
但这个指标散在三处没人对得上号：mem_watchdog.log(守护拉活记录)、Windows 事件日志
(Application Error 1000,原生崩溃的唯一实锤)、hub_console.log([Boot] 启动溯源)。
排障/验收全靠人翻日志+记忆(今天下午就出现过「谁在 18:15 重启了 hub」说不清的场面)。

本工具把三源自动关联成一本账：
  - 守护自动重启事件(avatar_hub 进程不存在 → 自动重启)
  - python.exe 原生崩溃事件(事件日志 1000：故障模块/异常码→归类 audio/camera/runtime)
  - hub 启动记录([Boot] pid/父进程——人工重启 vs 守护拉活 vs 部署脚本)
  - 基线(data/stability_baseline.json = P10 部署时刻)前后对比 → 一句裁决

协议与 dev_probe 一致：stdout 最后一行 JSON；纯 stdlib，无第三方依赖。
用法：python tools/stability_report.py [--days 3] [--base C:\\模仿音色]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(os.environ.get("HUB_BASE", "") or Path(__file__).resolve().parent.parent)

# ── 解析（纯函数，供门禁行为级测试）───────────────────────────────────────

_TS_RE = r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]"
# 守护重启行示例：[2026-07-06 17:34:49] ⛔[死] avatar_hub 进程不存在(核心服务) → 自动拉起(第 1 次，注入完整环境)
_WD_RESTART_RE = re.compile(_TS_RE + r".*?(\w+) ?进程不存在.*?自动(?:重启|拉起)")
# Boot 溯源行示例：2026-07-06 18:12:53 [INFO] [-] [Boot] pid=48716 ppid=46892 由谁拉起 → cmd.exe :: ...
_BOOT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*?\[Boot\] pid=(\d+) ppid=(\d+)(?:.*?(?:→|->)\s*(.+))?$")


def _to_epoch(s: str) -> float:
    """'YYYY-MM-DD HH:MM:SS'(本地时区) → epoch；解析失败返回 0。"""
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.0


def parse_watchdog_lines(lines, service: str = "avatar_hub") -> list:
    """守护日志 → 自动重启事件 [{ts, service}]（只取指定服务；service=''=全部）。"""
    out = []
    for ln in lines:
        m = _WD_RESTART_RE.search(ln)
        if not m:
            continue
        svc = m.group(2)
        if service and svc != service:
            continue
        ts = _to_epoch(m.group(1))
        if ts:
            out.append({"ts": ts, "service": svc})
    return out


def parse_boot_lines(lines) -> list:
    """hub 控制台日志 → 启动记录 [{ts, pid, ppid, parent}]（只认带时间戳的 logger 行，
    print 副本无时间戳自然滤掉，不会双计）。"""
    out = []
    for ln in lines:
        if "[Boot] pid=" not in ln:
            continue
        m = _BOOT_RE.search(ln.strip())
        if not m:
            continue
        ts = _to_epoch(m.group(1))
        if not ts:
            continue
        out.append({"ts": ts, "pid": int(m.group(2)), "ppid": int(m.group(3)),
                    "parent": (m.group(4) or "").strip()[:160]})
    return out


def classify_crash_module(module: str) -> str:
    """故障模块 → 崩溃面归类（P10 隔离的两族 + 运行时 + 其它）。"""
    m = (module or "").lower()
    if any(k in m for k in ("portaudio", "sounddevice", "_sounddevice", "audioses", "wdmaud")):
        return "audio-native"
    if any(k in m for k in ("virtualcam", "splitcam", "droidcam", "ivcam", "dshow",
                            "qedit", "ksproxy", "vcam")):
        return "camera-native"
    if any(k in m for k in ("ucrtbase", "ntdll", "kernelbase", "msvcp", "vcruntime")):
        return "runtime"
    return "other"


def correlate(restarts: list, crashes: list, win_before: float = 120.0,
              win_after: float = 10.0) -> list:
    """给每次守护重启找归因：重启时刻前 win_before 秒内(或后 win_after 秒)最近的崩溃事件。
    找不到 = 无崩溃实锤（人工 taskkill/关窗/静默 OOM 等）。"""
    out = []
    for r in restarts:
        hit = None
        for c in crashes:
            dt = r["ts"] - c["ts"]
            if -win_after <= dt <= win_before:
                if hit is None or abs(dt) < abs(r["ts"] - hit["ts"]):
                    hit = c
        item = dict(r)
        item["crash"] = ({"module": hit["module"], "code": hit["code"],
                          "bucket": hit["bucket"], "ts": hit["ts"]} if hit else None)
        out.append(item)
    return out


# ── 取数 ────────────────────────────────────────────────────────────────


def _decode(b: bytes) -> str:
    """日志编码混杂(UTF-8 与 GBK 写手共存)：先严格 UTF-8，失败落 GBK(replace)。"""
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("gbk", errors="replace")


def _read_tail_lines(path: Path, max_bytes: int = 6_000_000) -> list:
    """大日志只读尾部 max_bytes（重启/崩溃是低频事件，尾部窗口足够覆盖统计期）。"""
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()   # 丢弃可能被截断的半行
            return [_decode(ln) for ln in f.read().splitlines()]
    except Exception:
        return []


def parse_boots_jsonl(lines) -> list:
    """P12 补强主源：logs/hub_boots.jsonl（hub 启动时自记，不依赖启动方式/输出重定向）。
    2026-07-06 19:37 实证：经 start_avatar_hub.bat 直启的 hub，stdout 进它的控制台窗口，
    hub_console.log 里 [Boot] 完全隐身——控制台解析降级为历史兜底。"""
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
            if d.get("ts") and d.get("pid"):
                out.append({"ts": float(d["ts"]), "pid": int(d["pid"]),
                            "ppid": int(d.get("ppid") or 0),
                            "parent": str(d.get("parent") or "")[:160]})
        except Exception:
            continue
    return out


def collect_logs(base: Path, days: float) -> tuple:
    """守护重启 + hub 启动记录（专用账页为主 + 控制台/轮转尾部兜底），按时间窗过滤并升序去重。"""
    cutoff = time.time() - days * 86400
    wd_lines, boot_lines = [], []
    for name in ("mem_watchdog.log.1", "mem_watchdog.log"):
        wd_lines += _read_tail_lines(base / "logs" / name)
    for name in ("hub_console.log.1", "hub_console.log"):
        boot_lines += _read_tail_lines(base / "logs" / name)
    restarts = [r for r in parse_watchdog_lines(wd_lines) if r["ts"] >= cutoff]
    boots = parse_boots_jsonl(_read_tail_lines(base / "logs" / "hub_boots.jsonl"))
    boots += parse_boot_lines(boot_lines)
    boots = [b for b in boots if b["ts"] >= cutoff]
    restarts.sort(key=lambda x: x["ts"])
    boots.sort(key=lambda x: x["ts"])
    # 去重：同 pid 且时间±5s 视为同一次启动（jsonl 与控制台双记、logger+print 双写、轮转重叠）
    uniq = []
    for b in boots:
        if any(u["pid"] == b["pid"] and abs(u["ts"] - b["ts"]) <= 5.0 for u in uniq):
            continue
        uniq.append(b)
    return restarts, uniq


def query_crash_events(days: float, timeout: float = 25.0) -> list:
    """Windows 事件日志 Application Error(1000) → python.exe 崩溃 [{ts,module,code,bucket}]。
    经 PowerShell 一次性查询(强制 UTF-8 输出)；查询失败返回 []（报告标注 evtlog_ok=false）。"""
    ps = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"$t0=(Get-Date).AddDays(-{days});"
        "$ev=Get-WinEvent -FilterHashtable @{LogName='Application';Id=1000;StartTime=$t0} "
        "-ErrorAction SilentlyContinue;"
        "$out=@(); foreach($e in $ev){ $x=[xml]$e.ToXml();"
        " $v=@($x.Event.EventData.Data | ForEach-Object {$_.'#text'});"
        # 注意：PS5.1 的 Get-Date -UFormat %s 把本地墙钟当 UTC(差 8h)，必须走 DateTimeOffset
        " $out += [pscustomobject]@{ts=[double]([DateTimeOffset]$e.TimeCreated).ToUnixTimeSeconds();"
        " app=[string]$v[0]; code=[string]$v[6]; module=[string]$v[3]; path=[string]$v[10]} };"
        "$out | ConvertTo-Json -Compress")
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = _decode(p.stdout or b"").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        out = []
        for e in data:
            app = str(e.get("app") or "")
            if "python" not in app.lower():
                continue
            out.append({"ts": float(e.get("ts") or 0), "app": app,
                        "module": str(e.get("module") or ""),
                        "code": str(e.get("code") or ""),
                        "bucket": classify_crash_module(str(e.get("module") or ""))})
        out.sort(key=lambda x: x["ts"])
        return out
    except Exception:
        return []


# ── 报告 ────────────────────────────────────────────────────────────────


def build_report(base: Path, days: float) -> dict:
    baseline_file = base / "data" / "stability_baseline.json"
    baseline = {}
    try:
        baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not baseline.get("p10_deploy_ts"):
        baseline = {"p10_deploy_ts": time.time(),
                    "note": "首跑自动落基线(视为 P10 部署时刻)"}
        try:
            baseline_file.parent.mkdir(parents=True, exist_ok=True)
            baseline_file.write_text(json.dumps(baseline, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except Exception:
            pass
    p10_ts = float(baseline["p10_deploy_ts"])

    restarts, boots = collect_logs(base, days)
    crashes = query_crash_events(days)
    evtlog_ok = True if crashes or _evtlog_reachable() else False
    attributed = correlate(restarts, crashes)
    # 击中 Hub 的崩溃 = 与某次守护重启在时间窗内关联上的；其余崩溃未击中 Hub
    # （P10 后多半是设备探测子进程/视频管线等其它 python 进程——被隔离与自愈兜住）。
    hit_ts = {r["crash"]["ts"] for r in attributed if r.get("crash")}

    now = time.time()
    before = [r for r in restarts if r["ts"] < p10_ts]
    after = [r for r in restarts if r["ts"] >= p10_ts]
    span_before_h = max(0.1, (min(now, p10_ts) - (now - days * 86400)) / 3600)
    span_after_h = max(0.1, (now - p10_ts) / 3600) if now > p10_ts else 0.1
    buckets = {}
    for c in crashes:
        buckets[c["bucket"]] = buckets.get(c["bucket"], 0) + 1
    crashes_after = [c for c in crashes if c["ts"] >= p10_ts]
    not_hub_after = [c for c in crashes_after if c["ts"] not in hit_ts]
    # 人工/部署重启：启动记录里既无守护拉活也无崩溃在前 120s 内的那部分
    # （关窗重开、boot_stack、部署脚本——中性事件，不算稳定性失败，但要看得见）。
    def _is_manual(b):
        return (not any(abs(b["ts"] - r["ts"]) <= 120 for r in restarts)
                and not any(0 <= b["ts"] - c["ts"] <= 120 for c in crashes))
    boots_after = [b for b in boots if b["ts"] >= p10_ts]
    manual_after = [b for b in boots_after if _is_manual(b)]

    # 口径(部署竞态实证后收窄)：崩溃实锤击中 Hub 才是 P10 崩溃面回归；
    # 无实锤拉活多为「部署 taskkill 被守护抢拉/人工关窗」——中性列出，不定罪。
    hits_after = [r for r in after if r.get("crash")]
    if hits_after:
        verdict = (f"P10 后有 {len(hits_after)} 次崩溃实锤击中 Hub"
                   f"(共 {len(after)} 次拉活)——崩溃面回归，按模块归因收窄(见 crashes)")
        tone = "bad"
    elif after:
        verdict = (f"P10 后 {len(after)} 次拉活但全无崩溃实锤(人工杀/部署竞态/静默死)"
                   f"——崩溃面无回归证据；此前 {span_before_h:.0f}h 重启 {len(before)} 次")
        tone = "warn"
    else:
        if not_hub_after:
            verdict = (f"P10 后 {span_after_h:.1f}h Hub 零重启；期间 {len(not_hub_after)} 次 "
                       f"python 崩溃全部未击中 Hub(探测子进程/其它进程,隔离在吸收伤害)"
                       f"——此前 {span_before_h:.0f}h 重启 {len(before)} 次")
        else:
            verdict = (f"P10 后 {span_after_h:.1f}h 零守护重启、零 python 崩溃"
                       f"（此前 {span_before_h:.0f}h 里重启 {len(before)} 次）——隔离生效，继续观察")
        tone = "good"

    return {"ok": True, "now": now, "window_days": days,
            "evtlog_ok": evtlog_ok,
            "baseline": {"p10_deploy_ts": p10_ts},
            "watchdog_restarts": attributed[-40:],
            "hub_boots": boots[-40:],
            "crashes": crashes[-40:],
            "summary": {
                "restarts_before_p10": len(before), "restarts_after_p10": len(after),
                "span_before_h": round(span_before_h, 1), "span_after_h": round(span_after_h, 1),
                "rate_before_per_h": round(len(before) / span_before_h, 3),
                "rate_after_per_h": round(len(after) / span_after_h, 3),
                "crashes_total": len(crashes), "crashes_after_p10": len(crashes_after),
                "crashes_not_hub_after_p10": len(not_hub_after),
                "boots_after_p10": len(boots_after),
                "manual_boots_after_p10": len(manual_after),
                "crash_buckets": buckets,
                "verdict": verdict, "tone": tone}}


def _evtlog_reachable() -> bool:
    """事件日志通道是否可用（空结果≠查询失败；用一条 1 天窗的轻量查询探活）。"""
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-WinEvent -ListLog Application -ErrorAction Stop).RecordCount -ge 0"],
            capture_output=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return b"True" in (p.stdout or b"")
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Hub 稳定性账本(守护重启×事件日志崩溃×启动溯源)")
    ap.add_argument("--days", type=float, default=3.0, help="统计窗口(天)，默认 3")
    ap.add_argument("--base", default=str(_ROOT), help="项目根目录")
    a = ap.parse_args()
    try:
        r = build_report(Path(a.base), max(0.25, min(30.0, a.days)))
    except Exception as e:
        r = {"ok": False, "detail": f"{type(e).__name__}: {e}"[:300]}
    print(json.dumps(r, ensure_ascii=False), flush=True)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
