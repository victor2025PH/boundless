# -*- coding: utf-8 -*-
"""出站「时间一致性」错位率读数 CLI（只读；实施53 验收工具）。

背景
----
2026-08-22 03:31 老板实录：AI 回复「一会白天一会晚上」+ 凌晨 3:31「在二手书店
看书」。根因三件套（B 线双时钟锚点 / 深夜场景场所盲区 / 出站守卫无场所断言防线）
已修（见 docs/实施53）。本 CLI 把验收从「老板肉测」变成数字：扫近 N 天出站消息，
按**该会话人设的当地钟**复判「时段问候 / 场所断言」是否与发送时刻冲突，输出
错位率——修复装载前跑一次拿基线，装载 48h 后复跑对比。

口径
----
· **只读**（sqlite ``mode=ro`` URI，DB 不存在不创建）；数据根按 ``scripts/_data_root``
  契约（CLI → AITR_DATA_ROOT → 自动发现活跃实例 → 引擎根），多实例逐根输出。
· 人设钟解析（与生产链同源函数，逐层回落并**如实标注 clock 来源**）：
    1. ``bindings_runtime.yaml::ref_bindings``（会话级绑定，生产真实主力）；
    2. 平台默认（``config.local.yaml``/``config.yaml`` 的 ``<platform>.persona_ids[0]``
       / ``platform_login.default_persona_id`` / ``accounts.default_persona_id``）；
    3. 解析不出居住地 → 服务器钟（clock=server；无居住地人设这就是正确语义）。
  刻意**不碰** account_registry 单例（env 依赖，从引擎根跑会读错根——实施53 排查
  中的 CWD 陷阱教训）。
· 冲突判定复用出站守卫同一纯函数（``world_clock_guard.detect_daypart_conflict`` /
  ``detect_venue_conflict``）——读数口径与拦截口径必须同源，否则「报表说好了、
  守卫还在拦」两张皮。
· 出站媒体镜像占位（「[图片]…」）剔除；``--cutoff`` 给定时按装载时刻分 前/后 两窗。

用法
----
    python tools/time_consistency_obs.py [--days 14] [--cutoff "2026-08-22 12:30"]
                                         [--samples 8] [--data-root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.companion.persona_location import (  # noqa: E402
    PersonaPlace,
    persona_now,
    resolve_place_with_fallback,
)
from src.companion.world_clock_guard import (  # noqa: E402
    detect_daypart_conflict,
    detect_venue_conflict,
)

_PLACEHOLDER_RE = re.compile(r"^\s*\[")   # [图片] / [语音]×N / [文件]…


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_persona_places(cfg_dir: Path) -> Dict[str, Optional[PersonaPlace]]:
    """profiles_runtime.yaml → {persona_id: PersonaPlace|None}（解析失败回空表）。"""
    out: Dict[str, Optional[PersonaPlace]] = {}
    data = _load_yaml(cfg_dir / "profiles_runtime.yaml")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        return out
    for pid, p in profiles.items():
        if isinstance(p, dict):
            try:
                out[str(pid)] = resolve_place_with_fallback(p)
            except Exception:
                out[str(pid)] = None
    return out


def load_conv_bindings(cfg_dir: Path) -> Dict[str, str]:
    """bindings_runtime.yaml::ref_bindings → {conversation_id: persona_id}。"""
    data = _load_yaml(cfg_dir / "bindings_runtime.yaml")
    refs = data.get("ref_bindings")
    if not isinstance(refs, dict):
        return {}
    return {str(k): str(v) for k, v in refs.items() if v}


def load_platform_defaults(root: Path) -> Dict[str, str]:
    """平台默认人设（best effort）：<platform>.persona_ids[0] →
    platform_login.default_persona_id → accounts.default_persona_id。
    配置读取复用 ``_data_root.load_merged_config``（base+overlay 深合并单源）。"""
    try:
        merged: Dict[str, Any] = load_merged_config(root) or {}
    except Exception:
        merged = {}
    out: Dict[str, str] = {}
    global_default = str(
        ((merged.get("platform_login") or {}).get("default_persona_id"))
        or ((merged.get("accounts") or {}).get("default_persona_id")) or "")
    for plat in ("telegram", "whatsapp", "line", "messenger"):
        pids = (merged.get(plat) or {}).get("persona_ids")
        pid = ""
        if isinstance(pids, (list, tuple)) and pids:
            pid = str(pids[0] or "")
        out[plat] = pid or global_default
    return out


def local_hour_for(
    ts: float, place: Optional[PersonaPlace],
) -> Tuple[int, str]:
    """消息发送时刻 → (判定用小时, 时钟来源标签)。无居住地＝服务器钟。"""
    if place is not None:
        try:
            dt_local = persona_now(
                place, datetime.fromtimestamp(float(ts), tz=timezone.utc))
            return int(dt_local.hour), f"persona:{place.slug}"
        except Exception:
            pass
    return int(time.localtime(float(ts)).tm_hour), "server"


def judge_rows(
    rows: List[Dict[str, Any]],
    *,
    conv_bindings: Dict[str, str],
    platform_defaults: Dict[str, str],
    persona_places: Dict[str, Optional[PersonaPlace]],
    cutoff_ts: float = 0.0,
    samples_cap: int = 8,
) -> Dict[str, Any]:
    """纯函数聚合（喂 rows 即可单测）：每行 {conversation_id, text, ts}。

    返回 {judged, violations{daypart,venue}, by_window, by_clock, samples[]}。
    """
    res: Dict[str, Any] = {
        "judged": 0, "skipped_placeholder": 0,
        "violations": {"daypart": 0, "venue": 0},
        "by_window": {"before": {"n": 0, "bad": 0}, "after": {"n": 0, "bad": 0}},
        "by_clock": {},
        "samples": [],
    }
    for row in rows:
        text = str(row.get("text") or "").strip()
        ts = float(row.get("ts") or 0)
        if not text or ts <= 0:
            continue
        if _PLACEHOLDER_RE.match(text):
            res["skipped_placeholder"] += 1
            continue
        cid = str(row.get("conversation_id") or "")
        plat = cid.split(":", 1)[0] if ":" in cid else ""
        pid = conv_bindings.get(cid) or platform_defaults.get(plat, "")
        place = persona_places.get(pid) if pid else None
        hour, clock = local_hour_for(ts, place)
        res["judged"] += 1
        ck = res["by_clock"].setdefault(clock, {"n": 0, "bad": 0})
        ck["n"] += 1
        win = "after" if (cutoff_ts and ts >= cutoff_ts) else "before"
        res["by_window"][win]["n"] += 1
        tags = []
        dp = detect_daypart_conflict(text, hour)
        if dp:
            tags.append(f"daypart:{dp}")
            res["violations"]["daypart"] += 1
        vn = detect_venue_conflict(text, hour)
        if vn:
            tags.append(f"venue:{vn}")
            res["violations"]["venue"] += 1
        if tags:
            ck["bad"] += 1
            res["by_window"][win]["bad"] += 1
            if len(res["samples"]) < samples_cap:
                res["samples"].append({
                    "conv": cid, "persona": pid or "-", "clock": clock,
                    "hour": hour, "tags": tags,
                    "at": time.strftime("%m-%d %H:%M", time.localtime(ts)),
                    "text": text[:60],
                })
    return res


def scan_root(root: Path, *, days: int, cutoff_ts: float,
              samples_cap: int) -> Optional[Dict[str, Any]]:
    cfg_dir = root / "config"
    db = cfg_dir / "inbox.db"   # 数据类文件落实例数据根 config/（与 spoken_style_obs 同）
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        since = time.time() - days * 86400
        rows = [dict(r) for r in conn.execute(
            "SELECT conversation_id, text, ts FROM messages "
            "WHERE direction='out' AND ts >= ? ORDER BY ts", (since,))]
        conn.close()
    except Exception as exc:
        return {"root": str(root), "error": str(exc)}
    report = judge_rows(
        rows,
        conv_bindings=load_conv_bindings(cfg_dir),
        platform_defaults=load_platform_defaults(root),
        persona_places=load_persona_places(cfg_dir),
        cutoff_ts=cutoff_ts,
        samples_cap=samples_cap,
    )
    report["root"] = str(root)
    report["n_out"] = len(rows)
    return report


def _fmt_rate(bad: int, n: int) -> str:
    return f"{bad}/{n} ({bad / n * 100:.1f}%)" if n else "0/0"


def render(report: Dict[str, Any], *, cutoff: str) -> str:
    lines = [f"== {report['root']}"]
    if report.get("error"):
        lines.append(f"  读取失败：{report['error']}")
        return "\n".join(lines)
    v = report["violations"]
    lines.append(
        f"  出站 {report['n_out']} 条 / 判定 {report['judged']} 条"
        f"（剔媒体占位 {report['skipped_placeholder']}）；"
        f"时段错位 {v['daypart']}、场所错位 {v['venue']}")
    for clock, st in sorted(report["by_clock"].items()):
        lines.append(f"    按钟 {clock:<22} 错位 {_fmt_rate(st['bad'], st['n'])}")
    if cutoff:
        b, a = report["by_window"]["before"], report["by_window"]["after"]
        lines.append(
            f"    装载前 {_fmt_rate(b['bad'], b['n'])} → 装载后 "
            f"{_fmt_rate(a['bad'], a['n'])}（cutoff {cutoff}）")
    for s in report["samples"]:
        lines.append(
            f"    ✗ [{s['at']}] {s['persona']}@{s['clock']} h={s['hour']} "
            f"{'+'.join(s['tags'])} | {s['text']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="出站时间一致性错位率（只读）")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--cutoff", default="",
                    help="修复装载时刻（YYYY-MM-DD HH:MM，本地钟）；给定则分前后两窗")
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    cutoff_ts = 0.0
    if args.cutoff:
        try:
            cutoff_ts = time.mktime(
                time.strptime(args.cutoff, "%Y-%m-%d %H:%M"))
        except ValueError:
            print(f"cutoff 格式应为 YYYY-MM-DD HH:MM：{args.cutoff}")
            return 2

    roots = resolve_data_roots(args.data_root)
    reports = []
    for root in roots:
        r = scan_root(Path(root), days=args.days, cutoff_ts=cutoff_ts,
                      samples_cap=args.samples)
        if r is not None:
            reports.append(r)
    if not reports:
        print("未发现任何含 inbox.db 的数据根")
        return 1
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"# 出站时间一致性读数（近 {args.days} 天；判钟=会话人设当地钟）")
        for r in reports:
            print(render(r, cutoff=args.cutoff))
    return 0


if __name__ == "__main__":
    sys.exit(main())
