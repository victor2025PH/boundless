# -*- coding: utf-8 -*-
"""业务助手面板形态/飞层/撕出用量裁决读数 CLI（只读；P0-P3 收敛后的「数据裁决周」工具）。

背景
----
2026-08-17 业务助手四批收敛（P0 四形态状态机 / P1 收纳轨飞层 / P2 单卡撕出 /
P3 App 轨+悬浮窗打磨）后，遗留的全部是**读数决策**：撕出集合要不要扩到语音/目标卡、
撕出入口（卡头悬停显影）是否太隐蔽、飞层是「停靠踏板」还是「瞥一眼工作法」、
App(iframe) 灰度档要不要升运营默认。埋点全走 ``_uiBeacon`` 的 ``cppanel_*`` /
``cpapp_*`` 前缀 → ``ops.ui_event_trend`` 按日落库
（``<数据根>/config/ui_event_trend.db``）。本 CLI 把「样本到没到、证据指向哪」读出来
（对齐 ``tools/inbox_filter_usage_report.py`` 家法：只读、逐数据根、判词带样本闸门
与 ETA；DB 不存在绝不创建空库；读取走 ``mode=ro``）。

判词口径（纯函数，喂 rows 即可单测）
----
- **埋点纪元日 ``CPPANEL_EPOCH_DAY`` = 2026-08-18（不是功能上线日 08-17）**：
  上线当天 ``tools/verify_cp_panel_modes.py`` 真机门禁跑了 5+ 轮，每轮真点
  轨/飞层/钉住/撕出——当日 ``cppanel_*`` 行几乎全是门禁假用量。该工具自 P4 起已
  context 级 stub ``navigator.sendBeacon``（零遥测污染），故 **08-18 起才是干净
  人类数据**；纪元前的行如实读出但只作「已剔除」披露，绝不进判词分子分母。
- **零点击是合法证据**：满 ``min_days`` 后 total==0 照样出裁决（撕出满窗零使用
  ＝悬停显影入口太隐蔽，是「改常驻图标」的证据而不是「再等等」）。
- **粒度诚实**：trend 表只有 (日, 动作, 计数)——测不出「撕出后多快收回」这类
  时距；判词只用比值与总量，绝不假装有会话级时序。
- **埋点链自证**：满窗全 0 → 先查链路再谈用量（上线日有门禁数据证明链通，
  两周后全 0 更可能是链断了）。

用法
----
    python tools/cp_panel_usage_report.py [--days 30] [--min-days 7]
        [--min-total 12] [--data-root PATH] [--json]

数据根按 ``scripts/_data_root`` 契约解析（CLI → AITR_DATA_ROOT → 自动发现活跃
实例 → 引擎根）；多实例逐根输出。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

# 干净数据纪元（见模块 docstring：08-17 当日为门禁污染日，刻意不取功能上线日）
CPPANEL_EPOCH_DAY = "2026-08-18"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 7
DEFAULT_MIN_TOTAL = 12

# 动作词表（判词按组消费；未列出的 cppanel_* 新桶如实进 totals 但不参与判词）
MODE_ACTIONS = ("cppanel_expanded", "cppanel_rail", "cppanel_hidden")
FLYOUT_ACTIONS = ("cppanel_flyout", "cppanel_flyout_dismiss", "cppanel_pin")
TEAR_ACTIONS = ("cppanel_tear", "cppanel_tear_dock")
APP_ACTIONS = ("cpapp_on", "cpapp_off")


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 cppanel_* / cpapp_* 行；库不存在 → FileNotFoundError。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND (action LIKE 'cppanel\\_%' ESCAPE '\\' "
            "OR action LIKE 'cpapp\\_%' ESCAPE '\\') ORDER BY day",
            (cut,),
        ).fetchall()
        return [{"day": r["day"], "action": r["action"], "n": int(r["n"] or 0)}
                for r in rows]
    finally:
        conn.close()


def split_epoch(rows: List[Dict[str, Any]],
                epoch_day: str = CPPANEL_EPOCH_DAY) -> Dict[str, List[Dict[str, Any]]]:
    """纪元切分（纯函数）：纪元前=门禁污染期（只披露），纪元起=判词数据。"""
    clean: List[Dict[str, Any]] = []
    polluted: List[Dict[str, Any]] = []
    for r in rows or []:
        (clean if str(r.get("day") or "") >= epoch_day else polluted).append(r)
    return {"clean": clean, "polluted": polluted}


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows → {totals, by_day, grand_total}（纯函数）。"""
    totals: Dict[str, int] = {}
    by_day: Dict[str, Dict[str, int]] = {}
    for r in rows or []:
        a, d, n = str(r.get("action") or ""), str(r.get("day") or ""), int(r.get("n") or 0)
        if not a or not d:
            continue
        totals[a] = totals.get(a, 0) + n
        by_day.setdefault(d, {})
        by_day[d][a] = by_day[d].get(a, 0) + n
    return {"totals": totals, "by_day": by_day, "grand_total": sum(totals.values())}


def observed_days(now_day: str, epoch_day: str = CPPANEL_EPOCH_DAY) -> int:
    """自纪元日起的观测天数（含首尾；纪元当天=1；纪元在未来=0）。解析失败→0。"""
    try:
        d = (date.fromisoformat(now_day) - date.fromisoformat(epoch_day)).days + 1
        return max(0, d)
    except Exception:
        return 0


def build_verdicts(totals: Dict[str, int], observed: int, *,
                   min_days: int = DEFAULT_MIN_DAYS,
                   min_total: int = DEFAULT_MIN_TOTAL) -> List[Dict[str, str]]:
    """判词（纯函数）：status ∈ ok(维持)/action(建议施工)/wait(样本未到)/check(先查链路)。"""
    v: List[Dict[str, str]] = []
    grand = sum(int(totals.get(a) or 0) for a in
                MODE_ACTIONS + FLYOUT_ACTIONS + TEAR_ACTIONS + APP_ACTIONS)

    if observed < min_days:
        v.append({"topic": "sample", "status": "wait",
                  "text": f"观测 {observed}/{min_days} 天未满（纪元 {CPPANEL_EPOCH_DAY} 起算，"
                          f"08-17 当日为门禁污染已剔除）——全部裁决待样本，只读趋势别动方案。"})
        return v
    if grand == 0:
        v.append({"topic": "chain", "status": "check",
                  "text": "观测期满但 cppanel_*/cpapp_* 全量为 0——上线日门禁数据证明链路通过，"
                          "满窗全 0 更可能是 ops.ui_event_trend 关了或埋点链断了，先查链路再谈用量。"})
        return v

    fly = int(totals.get("cppanel_flyout") or 0)
    pin = int(totals.get("cppanel_pin") or 0)
    dis = int(totals.get("cppanel_flyout_dismiss") or 0)
    if fly < min_total:
        v.append({"topic": "flyout", "status": "wait",
                  "text": f"飞层样本 {fly}/{min_total} 未到，预览黏性不裁。"})
    else:
        pin_rate = pin / fly if fly else 0.0
        dis_rate = dis / fly if fly else 0.0
        if pin_rate >= 0.5:
            v.append({"topic": "flyout", "status": "action",
                      "text": f"飞层→钉住率 {pin_rate:.0%}（{pin}/{fly}）——预览多为停靠踏板："
                              f"考虑默认形态回 expanded 或飞层加宽减少一次点击。"})
        elif dis_rate >= 0.6:
            v.append({"topic": "flyout", "status": "ok",
                      "text": f"飞层→收回率 {dis_rate:.0%}（{dis}/{fly}）——「瞥一眼」工作法成立："
                              f"保持点击预览，勿加悬停触发。"})
        else:
            v.append({"topic": "flyout", "status": "ok",
                      "text": f"飞层 {fly} 次（钉住 {pin} / 收回 {dis}）分布均衡——维持现方案。"})

    tear = int(totals.get("cppanel_tear") or 0)
    dock = int(totals.get("cppanel_tear_dock") or 0)
    if tear == 0:
        v.append({"topic": "tear", "status": "action",
                  "text": "撕出满窗零使用而面板其他动作有量——卡头悬停显影入口大概率太隐蔽："
                          "建议撕出按钮改常驻小图标再观察一窗。"})
    elif tear < min_total:
        v.append({"topic": "tear", "status": "wait",
                  "text": f"撕出样本 {tear}/{min_total} 未到，扩面不裁（voice/goal 先不动）。"})
    else:
        dock_rate = dock / tear if tear else 0.0
        if dock_rate >= 0.8:
            v.append({"topic": "tear", "status": "check",
                      "text": f"撕出 {tear} 次但收回率 {dock_rate:.0%}——用了就退，先查默认尺寸/"
                              f"位置是否碍事（趋势表无时距粒度，需人工复核），暂不扩面。"})
        else:
            v.append({"topic": "tear", "status": "action",
                      "text": f"撕出 {tear} 次、留存充分（收回率 {dock_rate:.0%}）——"
                              f"建议把语音/工作目标卡加入可撕出集合（机制已通用）。"})

    app_on = int(totals.get("cpapp_on") or 0)
    if app_on > 0:
        v.append({"topic": "app", "status": "ok",
                  "text": f"App(iframe) 灰度被主动开启 {app_on} 次——App 轨可用性纳入观察；"
                          f"注：cppanel_* 形态埋点不分 native/App 模式（粒度局限，如实披露）。"})

    exp = int(totals.get("cppanel_expanded") or 0)
    rail = int(totals.get("cppanel_rail") or 0)
    hid = int(totals.get("cppanel_hidden") or 0)
    v.append({"topic": "modes", "status": "ok",
              "text": f"形态切换分布：expanded {exp} / rail {rail} / hidden {hid}"
                      f"（切换次数≠停留时长，只作参考不单独裁决）。"})
    return v


def report_for_root(root: Path, *, days: int, min_days: int, min_total: int,
                    now: Optional[float] = None) -> Dict[str, Any]:
    """单数据根报告（IO 壳；判词全在纯函数）。"""
    db = root / "config" / "ui_event_trend.db"
    out: Dict[str, Any] = {"data_root": str(root), "db": str(db)}
    try:
        rows = read_rows(db, days=days, now=now)
    except FileNotFoundError:
        out["error"] = "ui_event_trend.db 不存在（ops.ui_event_trend 未开或尚无数据）——不创建空库"
        return out
    parts = split_epoch(rows)
    agg = aggregate(parts["clean"])
    polluted_n = sum(int(r.get("n") or 0) for r in parts["polluted"])
    obs = observed_days(_utc_day(now))
    out.update({
        "epoch_day": CPPANEL_EPOCH_DAY,
        "observed_days": obs,
        "polluted_rows_excluded": polluted_n,
        "totals": agg["totals"],
        "grand_total": agg["grand_total"],
        "verdicts": build_verdicts(agg["totals"], obs,
                                   min_days=min_days, min_total=min_total),
    })
    return out


_STATUS_ICON = {"ok": "OK ", "action": ">> ", "wait": ".. ", "check": "?! "}


def render_text(rep: Dict[str, Any]) -> str:
    lines = [f"== 数据根: {rep.get('data_root')}"]
    if rep.get("error"):
        lines.append(f"  [SKIP] {rep['error']}")
        return "\n".join(lines)
    lines.append(f"  纪元 {rep['epoch_day']} 起观测 {rep['observed_days']} 天；"
                 f"纪元前(门禁污染)剔除 {rep['polluted_rows_excluded']} 次；"
                 f"干净总量 {rep['grand_total']}")
    totals = rep.get("totals") or {}
    if totals:
        lines.append("  分桶: " + "  ".join(f"{k}={v}" for k, v in sorted(totals.items())))
    for it in rep.get("verdicts") or []:
        lines.append(f"  {_STATUS_ICON.get(it['status'], '   ')}[{it['topic']}] {it['text']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    roots = resolve_data_roots(args.data_root or None)
    reports = [report_for_root(Path(r), days=args.days, min_days=args.min_days,
                               min_total=args.min_total) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print(render_text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
