# -*- coding: utf-8 -*-
"""快捷回复用量裁决读数 CLI（只读；cp-kb P2「机制先上线，样本门槛当等待期」）。

背景
----
2026-08-18 cp-kb 三批收敛（P0 中文化+美化 / P1 编辑闭环 / P2 用量记账）后，遗留
三个**读数决策**：哪些团队话术常用该排前、哪些零使用该清退、个人常用语层有没有
被真实采用。埋点全走 ``navigator.sendBeacon`` 的 ``cpkb_*`` 前缀 →
``ops.ui_event_trend`` 按日落库（``<数据根>/config/ui_event_trend.db``）。数据在攒，
本 CLI 把「样本到没到、证据指向哪」读出来（对齐 ``tools/inbox_filter_usage_report.py``
家法：只读、逐数据根、判词带样本闸门与 ETA）。

**只读、不改任何行为**：真正清退话术 / 调整排序，是看完本报告后的显式运营决定。
DB 不存在（``ops.ui_event_trend`` 未开 / 尚无数据）→ 明确提示，**不创建空库**；
读取走 ``mode=ro`` URI，对活体生产库零写事务。

判词设计（纯函数，喂 rows 即可单测）
----
- **埋点纪元日** ``QRPT_EPOCH_DAY``：cpkb_* 聚合埋点 P0 当日上线（2026-08-18），
  按键用量 ``cpkb_use_<key>`` P2 同日。观测天数按「今天 - 纪元日」算而不是数据
  首现日——零使用分不清「没人用」还是「埋点没装」的老坑。
- **零使用是合法证据**：满 ``min_days`` 后某键 0 次照样进清退候选（零就是答案）；
  但窗口内 cpkb_* **全量**为 0 → 一律「样本不足/链路存疑」，先查埋点链。
- **键宇宙来自实况 templates.yaml**（经与线上同一套准入谓词）——零使用键是
  「拿全集减去有声键」得出的，不是只看有声数据（否则永远发现不了死话术）。

用法
----
    python tools/quickreply_usage_report.py [--days 30] [--min-days 14]
        [--min-total 20] [--data-root PATH] [--json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from scripts._data_root import resolve_data_roots  # noqa: E402

QRPT_EPOCH_DAY = "2026-08-18"
DEFAULT_DAYS = 30
DEFAULT_MIN_DAYS = 14
DEFAULT_MIN_TOTAL = 20

# 聚合动作（P0/P1 埋点）；cpkb_use_<key> 为 P2 按键计数，前缀剥离后进 per_key。
_AGG_ACTIONS = (
    "cpkb_search", "cpkb_fill_tpl", "cpkb_fill_hit", "cpkb_var_fill",
    "cpkb_mine_add", "cpkb_mine_edit", "cpkb_mine_del", "cpkb_team_edit",
)
_USE_PREFIX = "cpkb_use_"


def _utc_day(now: Optional[float] = None) -> str:
    """UTC 日期键（与 ui_event_trend._day_str 同口径）。"""
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def read_rows(db_path: Any, *, days: int = DEFAULT_DAYS,
              now: Optional[float] = None) -> List[Dict[str, Any]]:
    """只读拉取近 N 天 cpkb_* 行；库不存在 → FileNotFoundError（绝不创建空库）。"""
    p = Path(db_path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    cut = _utc_day((now if now is not None else time.time()) - max(1, int(days)) * 86400)
    conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT day, action, n FROM ui_event_trend_daily "
            "WHERE day >= ? AND action LIKE 'cpkb\\_%' ESCAPE '\\' ORDER BY day",
            (cut,),
        ).fetchall()
        return [{"day": str(r["day"]), "action": str(r["action"]), "n": int(r["n"])}
                for r in rows]
    finally:
        conn.close()


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """rows → {totals(聚合动作), per_key(按键用量), total_all, days_with_data}。"""
    totals: Dict[str, int] = {a: 0 for a in _AGG_ACTIONS}
    per_key: Dict[str, int] = {}
    days_with_data: set = set()
    total_all = 0
    for r in rows:
        act = str(r.get("action") or "")
        n = int(r.get("n") or 0)
        if n <= 0 or not act.startswith("cpkb_"):
            continue
        total_all += n
        days_with_data.add(str(r.get("day") or ""))
        if act.startswith(_USE_PREFIX):
            key = act[len(_USE_PREFIX):]
            if key:
                per_key[key] = per_key.get(key, 0) + n
            continue
        if act in totals:
            totals[act] += n
    return {
        "totals": totals,
        "per_key": dict(sorted(per_key.items(), key=lambda kv: -kv[1])),
        "total_all": total_all,
        "days_with_data": len(days_with_data),
    }


def curated_team_keys(data_root: Any) -> List[str]:
    """键宇宙＝实况 templates.yaml 经**与线上同一套**准入谓词（单一事实源）。

    读序与 config_manager 同构：数据根 config/templates.yaml → 引擎根回落。
    dict 型值 / test / gxp_* 剔除（与 _collect_quick_templates 的坐席准入一致）。
    """
    import yaml

    from src.web.routes.unified_inbox_context import _qtpl_is_system_key

    for base in (Path(data_root) / "config", _ENGINE_ROOT / "config"):
        p = base / "templates.yaml"
        if not p.exists():
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return []
        out: List[str] = []
        for key, val in data.items():
            k = str(key or "").strip()
            if not k or _qtpl_is_system_key(k):
                continue
            if isinstance(val, (str, list)):
                out.append(k)
        return out
    return []


def _zh_name(key: str) -> str:
    """键 → 中文显示名（tp_nm_* 词条，与面板/后台同口径）；查不到回键名。"""
    try:
        from src.web.web_i18n import get_translations
        return str(get_translations("zh").get(f"tp_nm_{key}") or key)
    except Exception:
        return key


def observed_days(*, epoch_day: str = QRPT_EPOCH_DAY,
                  today: Optional[str] = None) -> int:
    """观测天数＝今天 - 纪元日 + 1（按埋点上线日算，不按数据首现日）。"""
    t = date.fromisoformat(today or _utc_day())
    e = date.fromisoformat(epoch_day)
    return max(0, (t - e).days + 1)


def build_verdicts(
    agg: Dict[str, Any], team_keys: List[str], *,
    min_days: int = DEFAULT_MIN_DAYS, min_total: int = DEFAULT_MIN_TOTAL,
    epoch_day: str = QRPT_EPOCH_DAY, today: Optional[str] = None,
) -> Dict[str, Any]:
    """判词纯函数：样本闸门 → 链路自证 → 排序/清退证据。"""
    days = observed_days(epoch_day=epoch_day, today=today)
    totals = agg["totals"]
    fills = totals["cpkb_fill_tpl"] + totals["cpkb_fill_hit"] + totals["cpkb_var_fill"]
    mine_ops = totals["cpkb_mine_add"] + totals["cpkb_mine_edit"] + totals["cpkb_mine_del"]
    out: Dict[str, Any] = {
        "observed_days": days,
        "fills": fills,
        "mine_ops": mine_ops,
        "verdict": "", "notes": [], "top_keys": [], "zero_keys": [],
    }
    if days < min_days:
        eta = (date.fromisoformat(epoch_day) + timedelta(days=min_days - 1)).isoformat()
        out["verdict"] = "sample_accumulating"
        out["notes"].append(
            f"样本积累中：观测 {days}/{min_days} 天（纪元 {epoch_day}），预计 {eta} 起可裁决。")
        return out
    if agg["total_all"] == 0:
        out["verdict"] = "chain_suspect"
        out["notes"].append(
            f"观测已满 {days} 天但 cpkb_* 全量为 0——上线当日即有埋点，先查链路"
            "（trend 开关 ops.ui_event_trend / beacon 是否被剥），别当「没人用」。")
        return out
    out["verdict"] = "ok" if fills >= min_total else "low_sample"
    if fills < min_total:
        out["notes"].append(
            f"填入总量 {fills} < {min_total}：比值/排名先别当真理，清退决策再等等。")
    per_key = agg["per_key"]
    out["top_keys"] = [
        {"key": k, "name": _zh_name(k), "n": n}
        for k, n in list(per_key.items())[:10]]
    zero = [k for k in team_keys if k not in per_key]
    out["zero_keys"] = [{"key": k, "name": _zh_name(k)} for k in sorted(zero)]
    if zero and out["verdict"] == "ok":
        out["notes"].append(
            f"零使用团队话术 {len(zero)} 条＝清退候选（确认其在观察窗内一直在架后再删；"
            "按键计数 P2 起才有，早于纪元的用量不在账上）。")
    return out


def render_report(root: str, agg: Dict[str, Any], v: Dict[str, Any]) -> str:
    lines = [f"=== 快捷回复用量报告 @ {root} ==="]
    lines.append(
        f"观测 {v['observed_days']} 天（纪元 {QRPT_EPOCH_DAY}）｜有数据天数 {agg['days_with_data']}")
    t = agg["totals"]
    lines.append(
        f"填入合计 {v['fills']}（团队 {t['cpkb_fill_tpl']} / 知识库 {t['cpkb_fill_hit']}"
        f" / 变量补全 {t['cpkb_var_fill']}）｜搜索 {t['cpkb_search']}")
    lines.append(
        f"个人常用语操作 {v['mine_ops']}（存 {t['cpkb_mine_add']} / 改 {t['cpkb_mine_edit']}"
        f" / 删 {t['cpkb_mine_del']}）｜团队话术面板内编辑 {t['cpkb_team_edit']}")
    if v["top_keys"]:
        lines.append("-- 团队话术用量 Top --")
        for it in v["top_keys"]:
            lines.append(f"  {it['n']:>5}  {it['name']}（{it['key']}）")
    if v["zero_keys"]:
        lines.append(f"-- 零使用（清退候选，共 {len(v['zero_keys'])}）--")
        for it in v["zero_keys"]:
            lines.append(f"      0  {it['name']}（{it['key']}）")
    for n in v["notes"]:
        lines.append(f"⚠ {n}")
    if not v["notes"]:
        lines.append("✓ 样本充分，可按上表做排序/清退决策。")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    # Windows GBK 控制台会被 ⚠/✓ 这类字符炸出 UnicodeEncodeError（首跑实锤）
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="快捷回复用量裁决读数（只读）")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--min-days", type=int, default=DEFAULT_MIN_DAYS)
    ap.add_argument("--min-total", type=int, default=DEFAULT_MIN_TOTAL)
    ap.add_argument("--data-root", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    roots = resolve_data_roots(args.data_root)
    payload = []
    for root in roots:
        db = Path(root) / "config" / "ui_event_trend.db"
        try:
            rows = read_rows(db, days=args.days)
        except FileNotFoundError:
            print(f"=== {root} ===\n（ui_event_trend.db 不存在：ops.ui_event_trend 未开"
                  "或尚无数据；本工具不创建空库）")
            continue
        agg = aggregate(rows)
        v = build_verdicts(agg, curated_team_keys(root),
                           min_days=args.min_days, min_total=args.min_total)
        if args.json:
            payload.append({"root": str(root), "aggregate": agg, "verdicts": v})
        else:
            print(render_report(str(root), agg, v))
            print()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
