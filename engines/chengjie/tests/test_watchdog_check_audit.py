# -*- coding: utf-8 -*-
"""HealthWatchdog 巡检可观测性棘轮（2026-08-27）。

## 背景

``_tick`` 逐个调用 56 个 ``_check_*``，每个包在 ``try/except: logger.debug(...)`` 里。
运行时错误（缺导入的 NameError、改名后的 AttributeError、签名变更的 TypeError）会让
那个检查**从此不再运行**，而日志里只有一条 DEBUG。当天实锤：``_check_true_probes``
漏了个函数内 ``pathlib`` 导入 → 探针从重启起整段不跑。

「哪些检查坏了没人会发现」曾被当成产品判断，其实可机械推导——见
``tools/watchdog_check_audit.py``：**无 INFO 心跳 + 无计数器 + 无状态文件 + tick 吞成
debug ＝ 静默死掉后没有任何观测面会变化**（BLIND）。

首跑结论纠正了一个想当然的假设：56 个里 **35 个本来就有 INFO 心跳**，BLIND 只有 3 个。
所以这不是一场 56 项的大改造，而是一张三行的清单。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "watchdog_check_audit.py"

# ── 棘轮：BLIND 只许降不许升 ────────────────────────────────────────────────
# 新加的巡检若生来 BLIND（没有任何正面信号且异常吞成 debug），这里先红。
# 降下来了就把天花板一起降，**不要**为了让门禁过而调高它。
_BLIND_CEILING = 2

# 已知 BLIND 且**属于其他线**的，附原因登记；修好即从表里删。
# 判据说明：这两条都没有 INFO 心跳/计数器/状态文件，静默死掉零信号。
_KNOWN_BLIND = {
    "_check_coverage_trend": "覆盖率趋势巡检：无正面信号，需其所有者决定加心跳还是提级",
    "_check_scan_loop_stall": "扫描循环停滞巡检：同上",
}

# ── 第二维棘轮：没有「真调用」测试的巡检只许减不许增 ──────────────────────
# 源码级断言（assert "_check_X" in src）抓不到运行时的缺导入/改名/签名漂移——当天
# 那个 NameError 就是在这类断言全绿的情况下上的生产。只有真把方法调起来的测试才算数。
# 首测：56 个里 54 个已有真调用测试（分散在各功能自己的测试文件里），比想象中好得多；
# 剩下两个（_check_tg_history_autosync 完全无覆盖、_check_mutual_chat 仅被提及）已由
# tests/test_watchdog_tick_behavior.py 补齐 → **天花板降到 0**：从此每个新巡检都必须
# 带一条真把它跑起来的测试。天花板只降不升，不要为了让门禁过而调高它。
_NO_INVOKE_CEILING = 0
_KNOWN_NO_INVOKE: dict = {}


def _mod():
    spec = importlib.util.spec_from_file_location("_wd_audit", _TOOL)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_classify_is_mechanical_not_opinion():
    """分级必须只依赖可从源码推导的信号——它的价值就在于不掺主观。"""
    m = _mod()
    blind = {"info_log": False, "counter": False, "state_file": False,
             "tick_level": "debug"}
    assert m.classify(blind) == "BLIND"
    # 任一正面信号 → 至少 WEAK（有东西会变，只是要人去看板盯）
    for k in ("counter", "state_file"):
        assert m.classify({**blind, k: True}) == "WEAK"
    # INFO 心跳或 tick 已提 warning → OK（死了会在日志里显形）
    assert m.classify({**blind, "info_log": True}) == "OK"
    assert m.classify({**blind, "tick_level": "warning"}) == "OK"


def test_probe_watchdog_of_watchdog_is_not_blind():
    """`_check_probe_stalled` 是「看门狗的看门狗」——最后一道防线不许是 BLIND。

    审计工具首跑就把它标成 BLIND（当天刚加的检查，作者是我），已把 tick 提为
    warning。谁改回 debug，这条先红。
    """
    m = _mod()
    rows = {r["check"]: r for r in m.collect()}
    row = rows.get("_check_probe_stalled")
    assert row, "_check_probe_stalled 不在 _tick 调用清单里了？"
    assert row["risk"] != "BLIND", f"最后一道防线退回 BLIND：{row}"
    assert row["tick_level"] == "warning"


def test_true_probes_stays_observable():
    """探针本体同理：它是兜底机制，tick 异常必须是 warning 且有 INFO 心跳。"""
    m = _mod()
    rows = {r["check"]: r for r in m.collect()}
    row = rows["_check_true_probes"]
    assert row["tick_level"] == "warning" and row["info_log"] is True
    assert row["state_file"] is True          # 状态文件＝跨重启去抖+观测数据源


def test_blind_count_ratchet():
    """BLIND 数量非增棘轮：新巡检生来无观测面就先红。"""
    m = _mod()
    rows = m.collect()
    blind = sorted(r["check"] for r in rows if r["risk"] == "BLIND")
    assert len(blind) <= _BLIND_CEILING, (
        f"BLIND 巡检增加到 {len(blind)} 个（天花板 {_BLIND_CEILING}）：{blind}\n"
        "新巡检请至少给一个正面信号（INFO 心跳 / self.total_* 计数 / 状态文件），"
        "或把 _tick 里的 except 提为 logger.warning。"
    )
    # 登记表不得过期：已修好的必须从 _KNOWN_BLIND 里删掉
    stale = sorted(set(_KNOWN_BLIND) - set(blind))
    assert not stale, f"这些已不再 BLIND，请从 _KNOWN_BLIND 删除：{stale}"


def test_invoke_vs_mention_is_ast_based_not_textual():
    """「真调用」必须走 AST 判定——本工具治的就是「把字符串当行为」这种错，不能自己犯。

    源码断言里常写 ``assert "self._check_x()" in src``，那串字面**含** ``._check_x(``；
    纯正则会把它算成调用。用 AST 就只认真正的 Call 节点。
    """
    m = _mod()
    src = _TOOL.read_text(encoding="utf-8")
    i = src.index("def tests_index(")
    seg = src[i:src.index("def classify(")]
    assert "ast.walk(ast.parse(" in seg and "ast.Call" in seg
    # 同一个测试文件不可能同时落进两个桶（互斥判定，不是两次独立扫描）
    for r in m.collect():
        assert not (set(r["test_invokes"]) & set(r["test_mentions"])), r["check"]


def test_behavioral_test_coverage_ratchet():
    """没有「真调用」测试的巡检非增棘轮：新巡检必须带一条真把它跑起来的测试。"""
    m = _mod()
    rows = m.collect()
    no_invoke = sorted(r["check"] for r in rows if not r["test_invokes"])
    assert len(no_invoke) <= _NO_INVOKE_CEILING, (
        f"无真调用测试的巡检增加到 {len(no_invoke)} 个"
        f"（天花板 {_NO_INVOKE_CEILING}）：{no_invoke}\n"
        "源码级断言抓不到运行时缺导入/改名——请补一条真调用该方法的行为测试"
        "（参考 tests/test_no_fallback_discipline.py::test_watchdog_probe_tick_actually_runs）。"
    )
    stale = sorted(set(_KNOWN_NO_INVOKE) - set(no_invoke))
    assert not stale, f"这些已有真调用测试，请从 _KNOWN_NO_INVOKE 删除：{stale}"


def test_audit_covers_all_ticked_checks():
    """审计口径必须覆盖 _tick 真正调用的全部巡检——漏扫的那个恰好可能是坏的那个。"""
    m = _mod()
    import ast

    tree = ast.parse(m.TARGET.read_text(encoding="utf-8"), str(m.TARGET))
    ticked = set(m.tick_levels(tree))
    audited = {r["check"] for r in m.collect()}
    assert ticked - audited == set(), f"_tick 调用了但未被审计：{ticked - audited}"
    assert len(audited) >= 50, f"只审计到 {len(audited)} 个，疑似解析退化"


def test_check_phantom_unread_actually_runs(monkeypatch):
    """真调用 ``_check_phantom_unread``：差额低于阈值时不告警、不改告警位。

    源码字符串断言抓不到函数内导入改名。差额 0 < min_phantom 必须安静返回。
    """
    import types

    from src.inbox.health_watchdog import HealthWatchdog
    from src.inbox import unread_aggregate as ua

    monkeypatch.setattr(
        ua, "phantom_unread_report",
        lambda store: {"phantom": 0, "badge": 1, "store": 1, "by_account": {}})
    fake = types.SimpleNamespace(
        _config_manager=types.SimpleNamespace(config={
            "health_watchdog": {"phantom_unread_remind": {
                "enabled": True, "min_phantom": 5}}}),
        _inbox=lambda: object(),
        _pu_alerted=False,
        _pu_last_remind=0.0,
    )
    HealthWatchdog._check_phantom_unread(fake, now=1_700_000_000.0)
    assert fake._pu_alerted is False
    assert fake._pu_last_remind == 0.0
