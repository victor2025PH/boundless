"""日志巡检看门狗：在 log_triage 聚合之上做「基线抑制 + 偏差检测」，只在
出现**新错误类**或**已知错误激增**时才告警——把「每 6 小时扫一遍日志」从
人工 Select-String 变成可托管的计划任务，且默认不对历史噪声重复轰炸。

与 log_triage 的分工：
  - ``log_triage``：把日志折叠成 (级别, logger, 归一化模板) 分组计数（一次性巡检视图）。
  - ``triage_watch``：加一层**会话记忆（基线）**——首跑只记录当前模板集为「已知」，
    之后每次只上报 ①基线里没有的新模板（NEW）②基线里有但本窗计数暴涨的模板（SURGE）。
    已知噪声（如历史掉线刷屏）被基线吸收，不再反复告警。

告警出口（全部 best-effort，绝不因告警失败中断巡检）：
  - 结构化报告追加 ``logs/triage/triage-YYYYMMDD.jsonl``（每次一行 summary + findings）；
  - ``--alert`` 时调 ``host_alert.notify_host``（算力机弹窗 + app.log 记录 + EventBus 镜像）；
  - 退出码：有 ERROR 级 finding → 2；仅 WARNING 级 → 1；无 → 0（供计划任务判定）。

用法：
    python -m scripts.triage_watch [--file <路径>] [--window-hours 6]
        [--baseline <路径>] [--new-min-count N] [--surge-factor F] [--surge-min N]
        [--levels ERROR,WARNING] [--alert] [--json] [--no-update-baseline]

注册每 6 小时计划任务见 ``scripts/register_triage_task.ps1``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 复用 log_triage 的解析/归一化/分组核心（单一事实源，避免两套归一化漂移）
try:
    from scripts.log_triage import Group, _default_log_path, parse_lines, triage
except ImportError:  # 允许 `python scripts/triage_watch.py` 直接跑
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.log_triage import Group, _default_log_path, parse_lines, triage


# ── 偏差检测（纯函数，便于单测） ──────────────────────────────────────

@dataclass
class Finding:
    kind: str          # "new" | "surge"
    level: str
    logger: str
    template: str
    count: int             # 本窗计数
    baseline_count: int    # 基线计数（new 时为 0）
    first_ts: str
    last_ts: str
    sample: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "level": self.level, "logger": self.logger,
            "count": self.count, "baseline": self.baseline_count,
            "first_ts": self.first_ts, "last_ts": self.last_ts,
            "template": self.template, "sample": self.sample,
        }


def _key(level: str, logger: str, template: str) -> str:
    return f"{level}\x1f{logger}\x1f{template}"


def detect_deviations(
    groups: List[Group],
    baseline: Dict[str, int],
    *,
    new_min_count: int = 1,
    surge_factor: float = 3.0,
    surge_min: int = 20,
    warn_new_min_count: int = 10,
) -> List[Finding]:
    """对比本窗分组与基线，产出「新错误类」与「激增」两类 finding。

    - NEW：基线里没有该 (level,logger,template)。ERROR 级 ≥ ``new_min_count`` 即报；
      WARNING 级门槛更高（``warn_new_min_count``，默认 10）——新 WARNING 常是低价值噪声。
    - SURGE：基线里有，但本窗计数 ≥ 基线 × ``surge_factor`` 且 ≥ ``surge_min``（绝对量
      闸门，防「基线 1 → 现 4」这种无意义放大触发）。
    """
    out: List[Finding] = []
    for g in groups:
        k = _key(g.level, g.logger, g.template)
        base = int(baseline.get(k, 0))
        cnt = g.count
        if base == 0:
            floor = new_min_count if g.level == "ERROR" else warn_new_min_count
            if cnt >= floor:
                out.append(Finding("new", g.level, g.logger, g.template, cnt, 0,
                                   g.first_ts, g.last_ts, g.sample))
        else:
            if cnt >= surge_min and cnt >= base * surge_factor:
                out.append(Finding("surge", g.level, g.logger, g.template, cnt, base,
                                   g.first_ts, g.last_ts, g.sample))
    # ERROR 优先、计数降序
    out.sort(key=lambda f: (f.level != "ERROR", -f.count))
    return out


def merge_baseline(baseline: Dict[str, int], groups: List[Group]) -> Dict[str, int]:
    """把本窗计数并入基线：取 max（已知模板的「可接受水位」只升不降），
    新模板登记为其当前计数。让「上报过一次的偏差」下次成为已知基线、不复报。
    """
    merged = dict(baseline)
    for g in groups:
        k = _key(g.level, g.logger, g.template)
        merged[k] = max(int(merged.get(k, 0)), int(g.count))
    return merged


# ── I/O（基线加载/保存、窗口起点、报告写入） ─────────────────────────

def load_baseline(path: Path) -> Dict[str, int]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): int(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def save_baseline(path: Path, baseline: Dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def window_since(hours: float, *, now: Optional[datetime] = None) -> str:
    now = now or datetime.now()
    return (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def _default_baseline_path(log_path: Path) -> Path:
    return log_path.parent / "triage" / "baseline.json"


def _report_path(log_path: Path, *, now: Optional[datetime] = None) -> Path:
    now = now or datetime.now()
    return log_path.parent / "triage" / f"triage-{now:%Y%m%d}.jsonl"


@dataclass
class ScanResult:
    findings: List[Finding]
    groups: List[Group]
    first_run: bool
    baseline: Dict[str, int]
    since: str


def scan_once(
    log_path: Path,
    baseline_path: Path,
    *,
    window_hours: float = 6.0,
    levels: Optional[List[str]] = None,
    new_min_count: int = 1,
    warn_new_min_count: int = 10,
    surge_factor: float = 3.0,
    surge_min: int = 20,
    now: Optional[datetime] = None,
) -> ScanResult:
    """无副作用扫描：读日志+基线，返回偏差 finding（不写基线/报告/告警）。

    CLI（main）与 in-process 看门狗（HealthWatchdog._check_log_triage）共用同一核心，
    各自决定「怎么告警、是否更新基线」——单一事实源，避免两处偏差检测漂移。
    首跑（基线文件不存在）返回 first_run=True 且 findings 清空（只用于建立基线）。
    """
    levels = levels or ["ERROR", "WARNING"]
    first_run = not baseline_path.exists()
    baseline = load_baseline(baseline_path)
    since = window_since(window_hours, now=now)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        records = parse_lines(f)
    groups = triage(records, levels=levels, since=since)
    findings = detect_deviations(
        groups, baseline, new_min_count=new_min_count,
        warn_new_min_count=warn_new_min_count,
        surge_factor=surge_factor, surge_min=surge_min,
    )
    if first_run:
        findings = []
    return ScanResult(findings=findings, groups=groups, first_run=first_run,
                      baseline=baseline, since=since)


def summarize(findings: List[Finding], *, window_hours: float) -> str:
    if not findings:
        return f"[triage-watch] 近 {window_hours:.0f}h 无新增/激增告警"
    errs = sum(1 for f in findings if f.level == "ERROR")
    lines = [f"[triage-watch] 近 {window_hours:.0f}h 发现 {len(findings)} 类异常"
             f"（ERROR {errs}）："]
    for f in findings[:8]:
        tag = "新增" if f.kind == "new" else f"激增(基线{f.baseline_count})"
        lines.append(f"  · [{f.level}] {tag} ×{f.count} {f.logger}: {f.template[:80]}")
    if len(findings) > 8:
        lines.append(f"  … 还有 {len(findings) - 8} 类")
    return "\n".join(lines)


def _emit_alert(findings: List[Finding], summary: str) -> None:
    """best-effort 告警：host_alert（算力机弹窗 + app.log + EventBus）。"""
    try:
        try:
            from src.utils.host_alert import notify_host
        except ImportError:
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from src.utils.host_alert import notify_host
        errs = [f for f in findings if f.level == "ERROR"]
        title = "日志巡检告警" + (f"（{len(errs)} 类新错误）" if errs else "")
        # 按 finding 集合去抖：同一批异常 6h 内只提醒一次
        key = "triage:" + ",".join(sorted(_key(f.level, f.logger, f.template)
                                          for f in findings))[:200]
        notify_host(title, summary, key=key, cooldown_sec=21600.0)
    except Exception:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="日志巡检看门狗（基线抑制 + 偏差检测）")
    ap.add_argument("--file", default=None, help="日志路径（默认自动探测）")
    ap.add_argument("--window-hours", type=float, default=6.0)
    ap.add_argument("--baseline", default=None, help="基线文件路径（默认 <log>/triage/baseline.json）")
    ap.add_argument("--levels", default="ERROR,WARNING")
    ap.add_argument("--new-min-count", type=int, default=1)
    ap.add_argument("--warn-new-min-count", type=int, default=10)
    ap.add_argument("--surge-factor", type=float, default=3.0)
    ap.add_argument("--surge-min", type=int, default=20)
    ap.add_argument("--alert", action="store_true", help="发现异常时调 host_alert")
    ap.add_argument("--no-update-baseline", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    log_path = Path(args.file) if args.file else _default_log_path()
    if not log_path or not log_path.exists():
        print(f"[triage-watch] 日志文件不存在: {log_path}", file=sys.stderr)
        return 3

    baseline_path = Path(args.baseline) if args.baseline else _default_baseline_path(log_path)
    levels = [x for x in args.levels.split(",") if x.strip()]

    res = scan_once(
        log_path, baseline_path,
        window_hours=args.window_hours, levels=levels,
        new_min_count=args.new_min_count, warn_new_min_count=args.warn_new_min_count,
        surge_factor=args.surge_factor, surge_min=args.surge_min,
    )
    findings, groups, first_run, baseline, since = (
        res.findings, res.groups, res.first_run, res.baseline, res.since
    )

    summary = summarize(findings, window_hours=args.window_hours)
    report = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "window_hours": args.window_hours,
        "since": since,
        "log": str(log_path),
        "first_run": first_run,
        "groups": len(groups),
        "findings": [f.as_dict() for f in findings],
    }
    # 追加结构化报告
    try:
        rp = _report_path(log_path)
        rp.parent.mkdir(parents=True, exist_ok=True)
        with open(rp, "a", encoding="utf-8") as rf:
            rf.write(json.dumps(report, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        if first_run:
            print(f"[triage-watch] 首跑：已登记 {len(groups)} 个基线模板（本次不告警）")
        else:
            print(summary)

    if findings and args.alert:
        _emit_alert(findings, summary)

    if not args.no_update_baseline:
        save_baseline(baseline_path, merge_baseline(baseline, groups))

    if any(f.level == "ERROR" for f in findings):
        return 2
    if findings:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
