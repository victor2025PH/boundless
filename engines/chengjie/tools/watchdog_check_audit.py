# -*- coding: utf-8 -*-
"""审计 HealthWatchdog 的 55 个巡检：**哪些静默死掉之后没人会发现**（2026-08-27）。

## 为什么需要它

``HealthWatchdog._tick`` 逐个调用 ``_check_*``，每个都包在
``try/except: logger.debug(...)`` 里。任何运行时错误（缺导入的 NameError、改名后的
AttributeError、签名变更的 TypeError）都会让那个检查**从此不再运行**，而日志里只有
一条 DEBUG。当天实锤：``_check_true_probes`` 漏了个函数内 ``pathlib`` 导入 → 探针
从重启起整段不跑，唯一信号是「轮次完成那行不再出现」——一个需要人主动去数的负面信号。

「该给哪些检查提日志级别 / 补行为测试」曾被当成产品判断。其实**它可以机械推导**：

    如果一个检查既没有正面成功信号（INFO 日志 / 计数器 / 状态文件），
    tick 异常又被吞成 DEBUG，那么它静默死掉之后**没有任何观测面会变化**。

这类就是「兜底型」——坏了没人知道等于没有。本工具把这句话变成一张可复核的表。

## 判据（全部从源码 AST 推导，不猜）

对每个 ``_check_X``：
  · ``tick_level``   ——它在 ``_tick`` 里的 except 处理级别（debug / warning / …）
  · ``info_log``     ——方法体里有没有**非条件性**的 logger.info（正面心跳）
  · ``counter``      ——有没有写 ``self.total_*`` 之类的累计计数（可被 metrics 读）
  · ``state_file``   ——有没有写状态文件（write_state / json.dump / state_path）
  · ``alerts``       ——有没有外发（notify_host / EventBus emit）
  · ``has_test``     ——tests/ 里有没有**提到该方法名**（弱信号，但零成本）

风险分级：
  **BLIND**  无 info_log 且无 counter 且无 state_file 且 tick_level=debug
             → 静默死掉零信号。有 alerts 的更危险（它本该是告警源，却哑了）。
  **WEAK**   只有计数器/状态文件（要有人去看板上盯才发现）
  **OK**     有 INFO 心跳或 tick 已是 warning

只读，不改任何文件。

## 要不要推广到别的周期循环？——已经量过了，基本不用（2026-08-27）

这工具好用，第一反应是「全仓推广」。别急，先看这次的实测：

全仓 ``except Exception: logger.debug`` 有 **1626 处 / 159 个文件**——照单列出来是
一张没人会看的表，因为绝大多数是**正当的** best-effort 兜底（可选的富化、埋点、
镜像），吞掉正是想要的行为。本工具能出结论，靠的不是「找吞异常」，而是
``_tick`` 那个**特定形状**：一个周期循环派发若干**独立具名单元**，某个单元死掉后
整块能力永久消失而循环照常转。

按这个形状扫全仓，同构的只有 6 处，逐个看完只剩两处真同构
（``line_rpa/service.py::_loop`` 与 ``messenger_rpa/service.py::_standby_loop``，
共 6 个单元）；``messenger_rpa/runner.py`` 那三处是**单次流程内的步骤**
（screenshot / exit_thread / finish），吞掉＝这一轮这步失败，不是能力消失，语义不同。

那 6 个单元里 **5 个本来就有 INFO 心跳**。也就是说 **HealthWatchdog 是异类**
（58 个单元共用一个 tick、又长期无人跑行为测试），不是全仓通病——所以推广没有价值，
别再花时间重做这段调查。

唯一的真发现：``line_rpa/service.py::_classify_alerts`` 是真 BLIND。它把 adb_lost /
send_fail_streak 写进告警表，死了之后表就不再进新行——而**「没有行」和「一切正常」
长得一模一样**，绝症正在于此（告警源哑了比没有告警更糟）。属已知小额债务：
影响面是 LINE RPA 一条告警路径，修法是加一条周期性 INFO 心跳或让派发处提到 warning。

用法::

    python tools/watchdog_check_audit.py            # 表格
    python tools/watchdog_check_audit.py --blind    # 只列 BLIND（改造清单）
    python tools/watchdog_check_audit.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ENGINE_ROOT = Path(__file__).resolve().parent.parent
TARGET = ENGINE_ROOT / "src" / "inbox" / "health_watchdog.py"
TESTS_DIR = ENGINE_ROOT / "tests"

ALERT_FUNCS = {"notify_host", "notify_key_failure", "notify_cloud_outage", "emit"}
STATE_FUNCS = {"write_state", "save_state", "dump", "state_path"}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
    return ""


def _logger_level(node: ast.AST) -> str:
    """``logger.debug(...)`` → 'debug'；不是 logger 调用返回 ''。"""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "logger":
            return node.func.attr
    return ""


def tick_levels(tree: ast.AST) -> Dict[str, str]:
    """``_tick`` 里每个 ``self._check_X()`` 对应的 except 日志级别。"""
    out: Dict[str, str] = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("_tick"):
            continue
        for tryblk in ast.walk(fn):
            if not isinstance(tryblk, ast.Try):
                continue
            called = [_call_name(n) for n in ast.walk(tryblk)
                      if isinstance(n, ast.Call)]
            checks = [c for c in called if c.startswith("_check_")]
            if not checks:
                continue
            level = ""
            for h in tryblk.handlers:
                for n in ast.walk(h):
                    lv = _logger_level(n)
                    if lv:
                        level = lv
                        break
                if level:
                    break
            for c in checks:
                out[c] = level or "(无日志)"
    return out


def analyse_method(fn: ast.AST) -> Dict[str, Any]:
    """方法体里的正面信号与外发路径。"""
    info_log = False
    alerts: List[str] = []
    counter = False
    state_file = False
    for n in ast.walk(fn):
        lv = _logger_level(n)
        if lv == "info":
            info_log = True
        name = _call_name(n)
        if name in ALERT_FUNCS:
            alerts.append(name)
        if name in STATE_FUNCS:
            state_file = True
        # self.total_xxx += 1 / self.total_xxx = ...
        if isinstance(n, (ast.AugAssign, ast.Assign)):
            targets = ([n.target] if isinstance(n, ast.AugAssign) else n.targets)
            for t in targets:
                if (isinstance(t, ast.Attribute) and t.attr.startswith("total_")
                        and isinstance(t.value, ast.Name) and t.value.id == "self"):
                    counter = True
    return {"info_log": info_log, "alerts": sorted(set(alerts)),
            "counter": counter, "state_file": state_file}


def tests_index() -> Dict[str, Dict[str, List[str]]]:
    """tests/ 对每个 ``_check_X`` 的覆盖，**区分「提及」与「真调用」**。

    这个区分是本工具的关键精度：当天那个 NameError 之所以能上生产，正是因为已有的
    源码级断言（``assert "_check_X" in src``）全绿——**提到 ≠ 跑过**。只有真调用
    （``wd._check_X(...)`` / ``HealthWatchdog._check_X(fake, ...)``）才能抓到运行时
    的缺导入 / 改名 / 签名漂移。

    判据必须走 **AST**，不能用正则：源码断言里常写
    ``assert "self._check_x()" in src``，那个字符串**字面含** ``._check_x(``，正则会
    把它算成调用——本工具要治的正是「把字符串当行为」这种错，自己不能先犯。
    （首版用正则实测报出「真调用 54/56」，AST 复核后大幅回落，差额全是字符串误判。）
    """
    idx: Dict[str, Dict[str, List[str]]] = {}
    name_pat = re.compile(r"_check_\w+")
    for p in sorted(TESTS_DIR.glob("test_*.py")):
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        invoked: set = set()
        try:
            for n in ast.walk(ast.parse(txt, str(p))):
                # 真调用＝ AST 里确实是 Call，且被调对象是 `<something>._check_X`
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    if n.func.attr.startswith("_check_"):
                        invoked.add(n.func.attr)
        except SyntaxError:
            pass                       # 语法坏的测试文件按「无调用」处理，不猜
        for name in set(name_pat.findall(txt)):
            row = idx.setdefault(name, {"invokes": [], "mentions": []})
            row["invokes" if name in invoked else "mentions"].append(p.name)
    return idx


def classify(row: Dict[str, Any]) -> str:
    positive = row["info_log"] or row["counter"] or row["state_file"]
    if row["tick_level"] == "warning" or row["info_log"]:
        return "OK"
    if not positive:
        return "BLIND"
    return "WEAK"


def collect() -> List[Dict[str, Any]]:
    tree = ast.parse(TARGET.read_text(encoding="utf-8"), str(TARGET))
    levels = tick_levels(tree)
    tidx = tests_index()
    rows: List[Dict[str, Any]] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("_check_"):
            continue
        if fn.name not in levels:
            continue                       # 未被 _tick 调用的辅助方法不计
        row: Dict[str, Any] = {"check": fn.name, "tick_level": levels[fn.name]}
        row.update(analyse_method(fn))
        t = tidx.get(fn.name) or {"invokes": [], "mentions": []}
        row["test_invokes"] = t["invokes"]     # 真调用＝能抓运行时错误
        row["test_mentions"] = t["mentions"]   # 仅提及＝源码断言，抓不到
        row["risk"] = classify(row)
        rows.append(row)
    order = {"BLIND": 0, "WEAK": 1, "OK": 2}
    rows.sort(key=lambda r: (order[r["risk"]], not r["alerts"], r["check"]))
    return rows


def render(rows: List[Dict[str, Any]], blind_only: bool) -> None:
    shown = [r for r in rows if r["risk"] == "BLIND"] if blind_only else rows
    print(f"{'检查':<34}{'风险':<7}{'tick':<9}{'正面信号':<22}{'外发':<6}测试")
    print("-" * 106)
    for r in shown:
        pos = ",".join(x for x, on in (("INFO", r["info_log"]),
                                       ("计数", r["counter"]),
                                       ("状态文件", r["state_file"])) if on) or "—"
        test = ("真调用" if r["test_invokes"]
                else "仅提及" if r["test_mentions"] else "无")
        print(f"{r['check']:<34}{r['risk']:<7}{r['tick_level']:<9}{pos:<22}"
              f"{('有' if r['alerts'] else '—'):<6}{test}")
    n = len(rows)
    b = sum(1 for r in rows if r["risk"] == "BLIND")
    w = sum(1 for r in rows if r["risk"] == "WEAK")
    ba = sum(1 for r in rows if r["risk"] == "BLIND" and r["alerts"])
    inv = sum(1 for r in rows if r["test_invokes"])
    only_men = sum(1 for r in rows if not r["test_invokes"] and r["test_mentions"])
    none = sum(1 for r in rows if not r["test_invokes"] and not r["test_mentions"])
    print(f"\n合计 {n} 个巡检：BLIND {b} ／ WEAK {w} ／ OK {n - b - w}")
    print(f"  其中 BLIND 且**本身是告警源**（哑了＝告警链断）：{ba}")
    print(f"\n测试覆盖：真调用 {inv} ／ 仅提及 {only_men} ／ 无 {none}")
    print("  「仅提及」＝只有源码级断言（assert '_check_X' in src）——当天那个 NameError"
          "\n  就是在这类断言全绿的情况下上的生产：提到 ≠ 跑过，抓不到运行时缺导入/改名。")
    print("\nBLIND ＝ 无 INFO 心跳、无计数器、无状态文件，且 tick 异常吞成 debug；"
          "\n        静默死掉之后没有任何观测面会变化。改造＝tick 提 warning + 补真调用的行为测试。")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="HealthWatchdog 巡检可观测性审计（只读）")
    ap.add_argument("--blind", action="store_true", help="只列 BLIND")
    ap.add_argument("--json", action="store_true", help="机读输出")
    args = ap.parse_args(argv)
    rows = collect()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        render(rows, args.blind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
