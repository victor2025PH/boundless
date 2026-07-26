"""群脉 CrowdX 出席矩阵 CLI —— 排一张「哪个号进哪个群、哪天进」的执行表。

导播台上那张卡只渲染前 30 行（页面装不下更多），而这个功能的目标规模恰恰是几十上百个
群。所以完整的表得从这里导出：``--json`` 喂给脚本，默认文本档给人照着做。

**纯计算，零副作用**：不落库、不发消息、不改注册表；只**读**账号注册表拿在线号，
以及导播台记下的出席台账（哪些号已经在哪些群里）——台账里不在本次清单的群算历史，
它们的共同出席照进度量，否则每批新群都显示「安全」而累积起来早已超标。

    # 我现在这些号，安全能铺几个群？
    python -m scripts.group_show_attendance --capacity

    # 给我一张覆盖 40 个群的排班表（每群 3 个号在场）
    python -m scripts.group_show_attendance --groups 40

    # 用真实群清单（一行一个），并导出 JSON
    python -m scripts.group_show_attendance --group-file groups.txt --json

两条轴要分清：本 CLI 算的是**跨群成员重合**（多号多群下的头号特征）；同一个群里能同时
上几个号是另一条轴，见 ``linkage.linkage_readiness``（``group_show_shadow.py`` 会打印）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.companion.group_show.attendance import (  # noqa: E402
    DEFAULT_JOINS_PER_DAY,
    format_plan,
    max_safe_groups,
    plan_attendance,
    recommend_pool_size,
    rotate_roles,
    schedule_joins,
    split_ledger,
)
from src.companion.group_show.linkage import (  # noqa: E402
    derive_fingerprint_groups,
)

_SLOTS = ("advocate", "skeptic", "bystander", "asker")

DEFAULT_REGISTRY = (
    Path(__file__).resolve().parents[1] / "config" / "account_registry.db")

DEFAULT_LEDGER = (
    Path(__file__).resolve().parents[1] / "config" / "group_show.db")


def _online_rows(registry_path: Path) -> List[Dict[str, Any]]:
    """账号注册表里的在线号原始行。

    要**原始行**而非洗过的候选：指纹派生要看 ``proxy_id`` / ``mode`` / ``meta``。
    """
    try:
        from src.integrations.account_registry import AccountRegistry
        reg = AccountRegistry(registry_path)
        return [r for r in (reg.list("telegram") or [])
                if str(r.get("status") or "") == "online"]
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读账号注册表失败（{exc}）", file=sys.stderr)
        return []


def _recorded_memberships(db_path: Path) -> Dict[str, List[str]]:
    """出席台账（控制台点「已加入」记下的）——与导播台读同一本账。

    不读它，CLI 导出的表会跟页面上的对不上，而且会漏掉历史群贡献的共同出席。
    库不存在就当空账（首次使用的正常状态，不该报警）。
    """
    if not Path(db_path).is_file():
        return {}
    try:
        from src.companion.group_show.store import GroupShowStore
        return {k: list(v) for k, v in
                (GroupShowStore(db_path).memberships() or {}).items()}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读出席台账失败（{exc}）", file=sys.stderr)
        return {}


def _read_group_file(path: Path) -> List[str]:
    """一行一个群 id；空行与 ``#`` 注释行跳过。"""
    out: List[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            gid = line.split("#", 1)[0].strip()
            if gid and gid not in out:
                out.append(gid)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 读群清单失败（{exc}）", file=sys.stderr)
    return out


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="群脉 CrowdX 出席矩阵：跨群排班 + 加群排期")
    ap.add_argument("--groups", type=int, default=0,
                    help="要覆盖多少个群（没有真实 id 时的测算模式，用 #n 占位）")
    ap.add_argument("--group-file", type=Path,
                    help="真实群清单文件（一行一个，优先于 --groups）")
    ap.add_argument("--seats", type=int, default=3,
                    help="每个群派几个号在场（默认 3）")
    ap.add_argument("--joins-per-day", type=int, default=DEFAULT_JOINS_PER_DAY,
                    help=f"单号每天加几个群（默认 {DEFAULT_JOINS_PER_DAY}）")
    ap.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY,
                    help="账号注册表路径")
    ap.add_argument("--capacity", action="store_true",
                    help="只测算「现有号池能安全铺几个群」，不出排班表")
    ap.add_argument("--rows", type=int, default=40,
                    help="文本档最多打印多少行排班（默认 40）")
    ap.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                    help="出席台账库（控制台记的「已加入」）")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略台账，当作从零开始排（只用于测算，别照着执行）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    rows = _online_rows(args.registry)
    pool = [str(r.get("account_id") or "") for r in rows]
    pool = [a for a in pool if a]
    seats = max(1, args.seats)

    if args.capacity or (not args.groups and not args.group_file):
        limit = max_safe_groups(pool=len(pool), seats=seats)
        payload = {"pool_size": len(pool), "seats": seats,
                   "max_safe_groups": limit}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"在线号 {len(pool)} 个，每群 {seats} 人在场")
            print(f"  安全能铺：{limit} 个群"
                  f"（再多就得补号——共同出席是按号池平方降的，加号比加群划算）")
            for target in (20, 50, 100):
                want = recommend_pool_size(groups=target, seats=seats)
                print(f"  想铺 {target} 个群 → 号池需要 {want} 个")
        return 0

    groups = (_read_group_file(args.group_file) if args.group_file
              else [f"#{i + 1}" for i in range(max(args.groups, 0))])

    # 台账里不在本次清单的群＝历史：不排，但共同出席照算（否则每批新群都显示
    # 「安全」，累积起来早就超标了）。拆分规则与导播台共用同一个函数，防两边漂。
    known, past = split_ledger(
        {} if args.fresh else _recorded_memberships(args.ledger), groups)
    plan = plan_attendance(
        pool, groups, seats=seats, existing=known, history=past,
        fingerprint_groups=derive_fingerprint_groups(rows))
    roles = rotate_roles(plan.assignments, _SLOTS[:seats])
    sched = schedule_joins(plan, per_account_per_day=max(1, args.joins_per_day))

    if args.json:
        print(json.dumps({
            "seats": plan.seats,
            "assignments": {k: list(v) for k, v in plan.assignments.items()},
            "joins": {k: list(v) for k, v in plan.joins.items()},
            "metrics": plan.metrics,
            "problems": plan.problems,
            "advice": plan.advice,
            "roles": roles,
            "schedule": sched,
        }, ensure_ascii=False, indent=2))
        return 0

    print(format_plan(plan, max_rows=args.rows))
    print()
    print(f"加群排期：单号每天 ≤{sched['per_account_per_day']} 个群，"
          f"共 {sched['day_count']} 天铺完")
    print("  （同一个群每天只进我们一个号——前后脚涌入三个陌生人比共同出席更刺眼）")
    for i, day in enumerate(sched.get("days") or [], 1):
        if i > 5:
            print(f"    …… 其余 {sched['day_count'] - 5} 天略（--json 看全表）")
            break
        cells = "  ".join(f"{t['account']}→{t['group']}" for t in day)
        print(f"    第 {i} 天: {cells}")
    for problem in (roles.get("problems") or ()):
        print(f"  ⚠ {str(problem).removeprefix('warn: ')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
