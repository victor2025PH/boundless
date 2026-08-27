# -*- coding: utf-8 -*-
"""真活探针自检 CLI（2026-08-27）——不等 watchdog 的 10 分钟 tick，当场跑一轮。

存在理由是当天那起事故：识图 03:10 切云 → 探针每 10min 红一次、弹窗攒到 strike=2，
而弹窗里只有 ``HTTPError: HTTP Error 400: Bad Request``（urllib 从不读响应体）。
根因（探针自带的 8x8 红图 IDAT CRC 是坏的、LAN ollama 宽容照收而云端严格校验）
是手工复现请求才看到的——**改完配置能立刻验一轮**这件事本身当时并不存在。

它回答三个问题：
1. 每个域现在处于哪一态——**在探**／**开着却探不了**（静默盲区，日志里连域名都不
   出现）／**运营关掉了**（正常）。三者混为一谈就会像 zhiliao_pilot 那样被误报成缺陷。
2. 每个域**真干活**过不过？失败时厂商原话是什么？
3. 措辞/时延稳不稳？（``-n`` 连打多轮——内容级断言最怕偶发误红）

**只读**：不改任何文件、不发业务消息。复用 ``scripts/_data_root`` 数据根契约，
看到的就是服务进程实际会读的那份配置。

用法::

    python tools/true_probe_selfcheck.py --from-state    # 零成本：读上一轮，不发探测
    python tools/true_probe_selfcheck.py                 # vision+translate+asr 各一轮
    python tools/true_probe_selfcheck.py -n 5            # 连打 5 轮验稳定性
    python tools/true_probe_selfcheck.py --domain vision # 只验一个域
    python tools/true_probe_selfcheck.py --all           # 含 tts（真烧 hub GPU，见下）
    python tools/true_probe_selfcheck.py --json

日常巡检先用 ``--from-state``（watchdog 每轮免费写好的读数，一发请求都不发）；
它报陈旧/停摆时再用实探模式定位。**改完配置要立刻验一轮**才用实探——状态文件是
上一轮的，改完配置马上读它只会读到改之前的结论。

⚠ tts 默认**不跑**：单次预算 90s 且真占 hub GPU 副本，随手跑会与线上语音抢资源
（2026-08-21 教训：探针放弃后在途请求会让 hub 把副本判「挂死」并规避）。要跑请
显式 ``--all`` / ``--domain tts``，并避开高峰。

退出码：全绿 0 ／ 有域失败**或**有静默盲区 2。域被运营关掉不计入（那是正常状态）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts._data_root import load_merged_config, resolve_data_roots  # noqa: E402
from src.ops.true_probe import (  # noqa: E402
    build_probe_specs,
    metrics_snapshot,
    probe_gap_reasons,
    run_probe,
    stalled_verdict,
)

# 默认「除 tts 之外全跑」而**不是**白名单：白名单会把后加的域（如识图备胎
# vision_backup1）默认漏掉——「新能力默认不被覆盖」是探针最容易长出的盲区。
TTS_PREFIX = "tts"


def selected(domain: str, wanted: List[str], include_tts: bool) -> bool:
    if wanted:
        return domain in wanted
    return include_tts or not domain.startswith(TTS_PREFIX)


def _assertion_label(spec: Dict[str, Any]) -> str:
    """把 spec 上的内容级断言渲染成人话——「这一轮到底验了什么」必须可见。"""
    if spec.get("expect_any"):
        return "含 " + "/".join(str(x) for x in spec["expect_any"])
    if spec.get("expect_latin"):
        return "须为拉丁文且无 CJK"
    if spec.get("domain") == "tts":
        return "magic bytes + 引擎归属"
    return "仅非空（弱）"


def _target(spec: Dict[str, Any]) -> str:
    return str(spec.get("model") or (spec.get("json") or {}).get("model") or "")


def run_root(root: Path, wanted: List[str], rounds: int,
             include_tts: bool = False) -> Dict[str, Any]:
    cfg = load_merged_config(root)
    specs = [s for s in build_probe_specs(cfg)
             if selected(s["domain"], wanted, include_tts)]
    have = {s["domain"] for s in specs}
    # 「没规格」分两种，绝不能混为一谈：域被运营关掉＝正常选择（zhiliao_pilot 三个
    # 媒体域全关），域开着却推不出规格＝静默盲区。混着报就是误报，而误报正是今天
    # 这起事故的代价来源。
    gaps = {d: why for d, why in probe_gap_reasons(cfg).items()
            if selected(d, wanted, include_tts)}
    disabled = [d for d in ("tts", "translate", "vision", "asr")
                if selected(d, wanted, include_tts)
                and d not in have and d not in gaps]
    results: List[Dict[str, Any]] = []
    for i in range(1, max(1, rounds) + 1):
        for spec in specs:
            t0 = time.time()
            ok, detail = run_probe(spec)
            results.append({
                "round": i, "domain": spec["domain"], "ok": ok,
                "detail": detail, "elapsed_ms": int((time.time() - t0) * 1000),
            })
    return {
        "root": str(root),
        "specs": [{"domain": s["domain"], "url": s.get("url", ""),
                   "model": _target(s), "assertion": _assertion_label(s),
                   "auth": "真 Bearer" if s.get("headers") else "probe"} for s in specs],
        # 三态分开报：gaps（开着却探不了＝静默盲区，最阴）／disabled（运营关的，
        # 正常）／failures（探了但没过）。前者与后者是两种病，混成一个数字就没法用。
        "gaps": gaps,
        "disabled_domains": disabled,
        "results": results,
        "failures": [r for r in results if not r["ok"]],
    }


def read_root_state(root: Path, wanted: List[str],
                    include_tts: bool = True) -> Dict[str, Any]:
    """``--from-state``：读 watchdog 上一轮写的状态，**一次探测都不发起**。

    「生产现在健康吗」这个问题此前只能靠真探一轮来答，而那要烧 GPU／云额度／hub 副本，
    于是没人愿意随手问——观测的成本本身把观测挡住了。状态文件是 watchdog 每轮免费写下的，
    读它零成本。

    **陈旧必须当失败报**：watchdog 死掉后状态文件会原地冻住，四域永远显示上一轮的绿。
    「绿了但没在更新」和「真的绿」长得一模一样——这正是同日在 ``_classify_alerts``
    上认出的同一个陷阱（告警源哑了＝表里没有新行＝看起来一切正常）。所以陈旧判据不自己
    造一套，直接复用探针自带的 ``stalled_verdict``（与 watchdog 告警同一口径，
    两边不会各说各话）。
    """
    cfg_dir = root / "config"
    snap = metrics_snapshot(cfg_dir)
    stalled = stalled_verdict(cfg_dir)
    domains = {d: row for d, row in (snap.get("domains") or {}).items()
               if selected(d, wanted, include_tts)}
    # 「没有状态文件」分两种，和实探模式的三态同一个道理：该实例**压根没有可探的域**
    # （运营把四域全关了，zhiliao_pilot 就是）＝正常，没有文件正是应有的样子；
    # 域配着却没文件＝watchdog 没在写，是真问题。不分开就会对着一台合规的实例长年报红，
    # 而误报的代价和探针误报一样高——工具喊狼来了几次之后就没人看它的退出码了。
    nothing_to_probe = False
    if not snap.get("present"):
        try:
            nothing_to_probe = not build_probe_specs(load_merged_config(root))
        except Exception:
            nothing_to_probe = False   # 读不出配置时保守按「该有却没有」报
    return {
        "root": str(root),
        "mode": "state",
        "present": bool(snap.get("present")),
        "nothing_to_probe": nothing_to_probe,
        "updated_at": snap.get("updated_at", ""),
        "stale_sec": int(snap.get("stale_sec") or 0),
        "stalled": stalled,
        "gaps": {d: why for d, why in (snap.get("gaps") or {}).items()
                 if selected(d, wanted, include_tts)},
        "domains": domains,
        "failing": sorted(d for d, row in domains.items() if not row.get("ok")),
    }


def render_state(report: Dict[str, Any]) -> None:
    print(f"\n=== 数据根 {report['root']}（读状态，未发起探测） ===")
    if not report["present"]:
        if report.get("nothing_to_probe"):
            print("  ·  四域均未启用（配置关闭）——没有状态文件正是应有的样子，不算问题。")
        else:
            print("  !! 有域配着却没有状态文件——watchdog 没在写。"
                  "这不等于健康，只等于「不知道」。")
        return
    age = report["stale_sec"]
    print(f"  上次写入 {report['updated_at']}（{age / 60:.1f} 分钟前）")
    if report["stalled"]:
        why = report["stalled"].get("reason") or report["stalled"]
        print(f"  !! 探针停摆：{why}")
        print("     下面这些「绿」是上一轮的遗留读数，不代表此刻健康。")
    for d, row in sorted(report["domains"].items()):
        print(f"  {'OK  ' if row.get('ok') else 'FAIL'} {d:<16} "
              f"fails={row.get('fails', 0)}  {row.get('detail', '')}")
    for d, why in report["gaps"].items():
        print(f"  !! {d:<16} 开着却探不了（静默盲区）：{why}")
    bad = report["failing"]
    print(f"\n  {len(report['domains'])} 域，失败 {len(bad)}"
          + (f"（{', '.join(bad)}）" if bad else "")
          + ("，探针停摆" if report["stalled"] else ""))


def render(report: Dict[str, Any]) -> None:
    print(f"\n=== 数据根 {report['root']} ===")
    for s in report["specs"]:
        print(f"  {s['domain']:<10} {s['url']}")
        print(f"  {'':<10} model={s['model']}  断言={s['assertion']}  auth={s['auth']}")
    for domain, why in report["gaps"].items():
        print(f"  !! {domain:<10} 开着却探不了（静默盲区）：{why}")
    if report["disabled_domains"]:
        print(f"  ·  未启用（配置关闭，正常）: {', '.join(report['disabled_domains'])}")
    if report["results"]:
        print()
    for r in report["results"]:
        print(f"  #{r['round']} {r['domain']:<10} "
              f"{'OK  ' if r['ok'] else 'FAIL'} {r['detail']}")
    n, bad = len(report["results"]), len(report["failures"])
    print(f"\n  {n} 次调用，失败 {bad} 次"
          + (f"，静默盲区 {len(report['gaps'])} 个" if report["gaps"] else ""))


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真活探针自检（只读）")
    ap.add_argument("--data-root", default="", help="显式指定实例数据根")
    ap.add_argument("--domain", default="", help="逗号分隔；默认 vision,translate,asr")
    ap.add_argument("--all", action="store_true", help="含 tts（真烧 hub GPU）")
    ap.add_argument("-n", "--rounds", type=int, default=1, help="每域连打几轮")
    ap.add_argument("--from-state", action="store_true",
                    help="读 watchdog 上一轮的状态，零成本零探测（含 tts）")
    ap.add_argument("--json", action="store_true", help="机读输出")
    args = ap.parse_args(argv)

    wanted = [d.strip() for d in args.domain.split(",") if d.strip()]
    roots = resolve_data_roots(args.data_root)
    if args.from_state:
        # 读状态时 tts 默认**含在内**：--all 存在的理由是「别随手烧 hub GPU」，
        # 而读文件一发请求都不发，那条理由在这条路径上不成立。
        reports = [read_root_state(root, wanted) for root in roots]
    else:
        reports = [run_root(root, wanted, args.rounds, include_tts=bool(args.all))
                   for root in roots]

    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            (render_state if rep.get("mode") == "state" else render)(rep)

    # 静默盲区与探针失败**同等**是问题（前者甚至更阴：它连红都不会红）；
    # 「域被运营关掉」不是问题，不占退出码——工具误报的代价和探针误报一样高。
    # 读状态模式下额外把「停摆」和「压根没有状态文件」也算失败：那两种情况下
    # 屏幕上的绿是上一轮的遗留读数，把它当健康正是本工具要防的那类误判。
    bad = any(
        (rep.get("failing") or rep.get("failures") or rep.get("gaps")
         or (rep.get("mode") == "state"
             and (rep.get("stalled")
                  or (not rep.get("present") and not rep.get("nothing_to_probe")))))
        for rep in reports
    )
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
