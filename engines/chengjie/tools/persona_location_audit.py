# -*- coding: utf-8 -*-
"""人设时区底账（只读；实施53 P2-1 运营决策工具）。

背景
----
2026-08-22 事故复盘（docs/实施53）：生产人设分布在多个时区（温哥华/旧金山/
巴塞罗那 vs 成都/台北…）——这是刻意的产品设定（persona_location 体系），但
运营/老板侧没有一张「谁在什么钟上、正在服务哪些会话」的底账：把设计当 bug
报障、或想把某些号本地化时无从下手。本 CLI 输出：

1. 每人设一行：location 状态（显式/文本推断/无）、当地此刻、与服务器时差、
   会话级绑定数（bindings_runtime.yaml::ref_bindings）、平台默认位；
2. 「海外钟」决策表（|时差| ≥ far-threshold，默认 3h）：老板拍板「哪些号保留
   海外设定、哪些本地化」直接读这张表；
3. 一致性红黄项：
   - RED  ``text_place_mismatch``：显式 location 与 role/background 文本里的
     城市**时区级**不一致（≥3h）——LLM 看得到档案文本，这是「优先人工核对」
     清单：若文本指的是现居地＝真矛盾（改齐一处）；若是出生地/旅行史等背景
     叙述＝合法，核对后忽略即可（工具判不了语义，宁可多列不静默）；
   - WARN ``text_place_differs``：文本城市与 location 不同但同钟（弱矛盾）；
   - INFO ``inferred_from_text``：无显式 location，生产按文本推断钟跑
     （resolve_place_with_fallback 的 auto_infer）——建议补显式 location 钉死；
   - INFO ``server_clock``：无任何地点信号，按服务器钟跑（本地人设的正确态）。

改 location 只需编辑 profiles_runtime.yaml 一行（mtime 热重载 ~3s 生效），但
**必须同步核对 role/background 里的城市叙述**（人设纪律：一个事实反规范化写进
多个字段，改一处不等于改干净）——本工具的红项就是那份核对清单。

用法
----
    python tools/persona_location_audit.py [--data-root PATH] [--json]
                                           [--far-threshold 3.0]
"""
from __future__ import annotations

import argparse
import importlib.util as _ilu
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402
from src.companion.persona_location import (  # noqa: E402
    PersonaPlace,
    daypart_label,
    infer_place_from_text,
    persona_now,
    resolve_persona_place,
    resolve_place_with_fallback,
    tz_offset_hours,
)


def _load_obs_module():
    """复用 time_consistency_obs 的绑定/平台默认加载器（单源，防口径漂移）。"""
    spec = _ilu.spec_from_file_location(
        "_tc_obs_for_audit", Path(__file__).with_name("time_consistency_obs.py"))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def place_gap_hours(a: Optional[PersonaPlace], b: Optional[PersonaPlace]) -> float:
    """两地此刻的时差绝对值（小时）；任一缺失/异常 → 0.0。

    与服务器时区无关（比较的是两地彼此），跨机器确定；DST 随当下日期取真值。
    """
    try:
        if a is None or b is None:
            return 0.0
        now = datetime.now(timezone.utc)
        oa = now.astimezone(ZoneInfo(a.tz_name)).utcoffset()
        ob = now.astimezone(ZoneInfo(b.tz_name)).utcoffset()
        if oa is None or ob is None:
            return 0.0
        return abs((oa - ob).total_seconds()) / 3600.0
    except Exception:
        return 0.0


def _text_place(profile: Dict[str, Any]) -> Optional[PersonaPlace]:
    """role/background 文本里可推断出的城市（与生产 auto_infer 同函数）。"""
    try:
        text = (str(profile.get("role", "") or "") + " "
                + str(profile.get("background", "") or ""))
        slug = infer_place_from_text(text)
        if not slug:
            return None
        return resolve_persona_place({"location": slug})
    except Exception:
        return None


def audit_personas(
    profiles: Dict[str, Any],
    ref_bindings: Dict[str, str],
    platform_defaults: Dict[str, str],
    *,
    far_threshold: float = 3.0,
    offset_fn: Callable[[Optional[PersonaPlace]], float] = tz_offset_hours,
) -> Dict[str, Any]:
    """纯函数聚合（喂 dict 即可单测）：人设×时钟×绑定 底账 + 红黄项。

    ``offset_fn`` 可注入（默认生产口径 ``tz_offset_hours``＝相对服务器时区，
    机器相关；测试注入假偏移拿确定性断言）。
    """
    bound_count: Dict[str, int] = {}
    for _cid, pid in (ref_bindings or {}).items():
        if pid:
            bound_count[str(pid)] = bound_count.get(str(pid), 0) + 1
    rows: List[Dict[str, Any]] = []
    reds: List[Dict[str, Any]] = []
    warns: List[Dict[str, Any]] = []
    for pid, prof in (profiles or {}).items():
        if not isinstance(prof, dict):
            continue
        pid = str(pid)
        has_loc_key = "location" in prof
        place = resolve_place_with_fallback(prof)
        loc_state = (
            "explicit" if has_loc_key and place is not None
            else "disabled" if has_loc_key
            else "inferred" if place is not None
            else "none")
        offset = float(offset_fn(place) or 0.0) if place is not None else 0.0
        local = persona_now(place) if place is not None else None
        row: Dict[str, Any] = {
            "pid": pid,
            "name": str(prof.get("name") or ""),
            "loc_state": loc_state,
            "place": place.display("zh") if place is not None else "",
            "tz": place.tz_name if place is not None else "",
            "offset_server_h": round(offset, 1),
            "local_now": (
                f"{local:%H:%M}（{daypart_label(local.hour, 'zh')}）"
                if local is not None else ""),
            "bound_convs": bound_count.get(pid, 0),
            "default_platforms": sorted(
                plat for plat, dpid in (platform_defaults or {}).items()
                if dpid == pid),
            "flags": [],
        }
        tp = _text_place(prof)
        if loc_state == "explicit" and tp is not None and place is not None \
                and tp.slug != place.slug:
            gap = place_gap_hours(place, tp)
            item = {
                "pid": pid, "location": place.display("zh"),
                "text_place": tp.display("zh"), "gap_h": round(gap, 1),
            }
            if gap >= 3.0:
                row["flags"].append("RED:text_place_mismatch")
                reds.append(item)
            else:
                row["flags"].append("WARN:text_place_differs")
                warns.append(item)
        elif loc_state == "inferred":
            row["flags"].append("INFO:inferred_from_text")
        elif loc_state in ("none", "disabled"):
            row["flags"].append("INFO:server_clock")
        rows.append(row)
    far = sorted(
        (r for r in rows if abs(r["offset_server_h"]) >= float(far_threshold)),
        key=lambda r: -abs(r["offset_server_h"]))
    return {
        "personas": rows,
        "reds": reds,
        "warns": warns,
        "far": far,
        "summary": {
            "total": len(rows),
            "far_tz": len(far),
            "bound_on_far_tz": sum(r["bound_convs"] for r in far),
            "reds": len(reds),
            "warns": len(warns),
        },
    }


def scan_root(root: Path, *, far_threshold: float) -> Optional[Dict[str, Any]]:
    cfg_dir = root / "config"
    prof_path = cfg_dir / "profiles_runtime.yaml"
    if not prof_path.exists():
        return None
    obs = _load_obs_module()
    data = obs._load_yaml(prof_path)
    profiles = data.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    report = audit_personas(
        profiles,
        obs.load_conv_bindings(cfg_dir),
        obs.load_platform_defaults(root),
        far_threshold=far_threshold,
    )
    report["root"] = str(root)
    return report


def render(report: Dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        f"== {report.get('root', '(memory)')}",
        f"  人设 {s['total']} 个；海外钟（≥3h）{s['far_tz']} 个，"
        f"其上挂着 {s['bound_on_far_tz']} 个绑定会话；"
        f"红项 {s['reds']} / 黄项 {s['warns']}",
    ]
    for r in report["personas"]:
        loc = r["place"] or "（无地点→服务器钟）"
        flags = f"  ⚑{','.join(r['flags'])}" if r["flags"] else ""
        defaults = (f" 默认位:{'/'.join(r['default_platforms'])}"
                    if r["default_platforms"] else "")
        lines.append(
            f"    {r['pid']:<22} {loc:<14} 时差{r['offset_server_h']:+.1f}h"
            f" 当地{r['local_now'] or '-'} 绑定{r['bound_convs']}"
            f"{defaults}{flags}")
    if report["far"]:
        lines.append("  —— 海外钟决策表（保留海外设定 or 本地化？）——")
        for r in report["far"]:
            lines.append(
                f"    {r['pid']:<22} {r['place']:<14}"
                f" 时差{r['offset_server_h']:+.1f}h 绑定会话 {r['bound_convs']}"
                + (f"（平台默认:{'/'.join(r['default_platforms'])}）"
                   if r["default_platforms"] else ""))
    for item in report["reds"]:
        lines.append(
            f"  ✗ RED {item['pid']}：location={item['location']} 但档案文本提到"
            f" {item['text_place']}（差 {item['gap_h']}h）——LLM 看得到文本，"
            "请人工核对：文本若指现居地＝真矛盾要改齐一处；"
            "若是出生地/旅行史等背景叙述＝合法可忽略")
    for item in report["warns"]:
        lines.append(
            f"  ⚠ WARN {item['pid']}：location={item['location']} vs 文本"
            f" {item['text_place']}（同钟，弱矛盾）")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="人设时区底账（只读）")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--far-threshold", type=float, default=3.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    reports = []
    for root in resolve_data_roots(args.data_root):
        r = scan_root(Path(root), far_threshold=args.far_threshold)
        if r is not None:
            reports.append(r)
    if not reports:
        print("未发现任何含 profiles_runtime.yaml 的数据根")
        return 1
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2, default=str))
    else:
        print("# 人设时区底账（location 改一行热生效；红项=档案自相矛盾清单）")
        for r in reports:
            print(render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
