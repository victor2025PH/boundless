# -*- coding: utf-8 -*-
"""D1b P0-4（2026-09-05）：目标「自动推进」运行时闸进真相出口。

不变量：
- ``goal_runtime_gates`` 是 ticker ``run_once`` 与 preflight 的**同一函数**——
  preflight 说「能」ticker 就一定会排；说「不能」ticker 一定跳过（同 reason）。
- ``stop_on_fail=True`` 短路（首个未过闸即返回，零多余 I/O）；``False`` 全列。
- fail-closed 口径：无 inbox / 读不到档位 → 不过；情绪闸/opt-out 自身异常 → 放行。
- ``preflight_goal`` 三层合一：引擎配置 blockers + 进程活性 + 运行时闸；
  ``ok`` 只在全过时 True；``next_due`` 只在 ok 时给（不给「不能发」的目标时间承诺）。
- 卡片 ``sprint_live``：运行时闸未过 → ``next_phase_ts=0`` + ``nudgeable=False``
  + blockers 点名（此前只看引擎配置，review 档会话照样挂着时间承诺）。
- 新 blocker 词表 zh/en 齐备（前端 ``inbox.goal.engine.blk.*`` 直译）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from src.companion.goals.sprint_ticker import (
    RUNTIME_GATE_ORDER,
    goal_runtime_gates,
    parse_sprint_cfg,
    preflight_goal,
)

_ROOT = Path(__file__).resolve().parents[1]


class _Inbox:
    def __init__(self, mode="auto_ai", meta=None, msgs=None, raise_mode=False):
        self.mode = mode
        self.meta = meta or {}
        self.msgs = msgs or []
        self.raise_mode = raise_mode
        self.calls = []

    def get_automation_mode(self, conv):
        self.calls.append("mode")
        if self.raise_mode:
            raise RuntimeError("db down")
        return self.mode

    def get_conv_meta(self, conv):
        self.calls.append("meta")
        return dict(self.meta)

    def list_recent_messages(self, conv, limit=10):
        self.calls.append("msgs")
        return list(self.msgs)


def _goal(**kw):
    now = time.time()
    g = {
        "goal_id": "g1", "conversation_id": "telegram:acc:123",
        "platform": "telegram", "autonomy": "auto", "status": "active",
        "template": "custom", "params": {"pace": "today"},
        "start_ts": now - 3600, "deadline_ts": now + 3600 * 5,
        "created_at": now - 3600,
    }
    pace = kw.pop("pace", None)
    if pace is not None:
        g["params"] = {"pace": pace}
    g.update(kw)
    return g


def _cfg(**kw):
    base = {"enabled": True, "platforms": ["telegram", "whatsapp", "line"]}
    base.update(kw)
    return parse_sprint_cfg({"sprint": base})


# ---------------------------------------------------------------- gates ----

def test_gates_all_pass_returns_no_skip_and_ts():
    n = time.time()
    ib = _Inbox(msgs=[{"ts": n - 100, "direction": "in"},
                      {"ts": n - 50, "direction": "out"}])
    rg = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=ib, now=n)
    assert rg["skip"] == ""
    assert all(rg["gates"][k] for k in RUNTIME_GATE_ORDER)
    assert rg["last_in"] == pytest.approx(n - 100)
    assert rg["last_out"] == pytest.approx(n - 50)


@pytest.mark.parametrize("patch,expect", [
    ({"autonomy": "suggest"}, "autonomy"),
    ({"platform": "messenger"}, "platform"),
    ({"conversation_id": ""}, "no_conversation"),
])
def test_gates_goal_shape_failures(patch, expect):
    rg = goal_runtime_gates(_goal(**patch), cfg=_cfg(), inbox=_Inbox(),
                            stop_on_fail=True)
    assert rg["skip"] == expect
    assert rg["gates"][expect] is False


def test_gates_no_inbox_is_fail_closed():
    rg = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=None)
    assert rg["skip"] == "no_inbox"
    # preflight 全列口径：依赖 inbox 的后续闸一律标未过
    assert rg["gates"]["automation_mode"] is False
    assert rg["gates"]["optout"] is False


def test_gates_automation_mode_review_and_exception_both_fail():
    assert goal_runtime_gates(
        _goal(), cfg=_cfg(), inbox=_Inbox(mode="review"))["skip"] == "automation_mode"
    assert goal_runtime_gates(
        _goal(), cfg=_cfg(), inbox=_Inbox(raise_mode=True))["skip"] == "automation_mode"


def test_gates_crisis_block_and_gate_exception_lenient():
    rg = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=_Inbox(),
                            emotion_gate=lambda g, m: "block")
    assert rg["skip"] == "crisis"
    rg2 = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=_Inbox(),
                             emotion_gate=lambda g, m: "soft")
    assert rg2["gates"]["crisis"] is True

    def _boom(g, m):
        raise RuntimeError("probe down")
    rg3 = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=_Inbox(),
                             emotion_gate=_boom)
    assert rg3["gates"]["crisis"] is True, "情绪探针自身故障不判死目标"


def test_gates_optout_mute_blocks():
    n = time.time()
    conv = _goal()["conversation_id"]
    mutes = {conv: {"ts": n - 60, "until": n + 86400}}
    rg = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=_Inbox(), mutes=mutes,
                            now=n, emotion_gate=lambda g, m: "ok")
    # optout_active 的具体键由 proactive_optout 定义；这里只断言「命中静默表
    # 且判定为 active → optout 闸不过」，判定不 active 则放行——两种都不许抛。
    from src.utils.proactive_optout import optout_active
    expected = not optout_active(mutes[conv], now=n, last_in_ts=0.0)
    assert rg["gates"]["optout"] is expected
    assert (rg["skip"] == "optout") is (not expected)


def test_gates_stop_on_fail_short_circuits_io():
    ib = _Inbox(mode="review")
    goal_runtime_gates(_goal(), cfg=_cfg(), inbox=ib, stop_on_fail=True)
    assert ib.calls == ["mode"], "档位不过就该停，不该再读 meta/消息流"
    ib2 = _Inbox(mode="review")
    rg = goal_runtime_gates(_goal(), cfg=_cfg(), inbox=ib2, stop_on_fail=False,
                            emotion_gate=lambda g, m: "ok")
    assert "msgs" in ib2.calls, "preflight 全列口径要把后面的闸也判完"
    assert rg["skip"] == "automation_mode"
    assert rg["gates"]["crisis"] is True and rg["gates"]["optout"] is True


def test_run_once_uses_goal_runtime_gates_as_single_source():
    """ticker 不许再手写一套闸——静态钉住 run_once 调用 goal_runtime_gates。"""
    src = (_ROOT / "src/companion/goals/sprint_ticker.py").read_text("utf-8")
    body = src.split("def run_once(", 1)[1]
    assert "goal_runtime_gates(" in body
    assert "stop_on_fail=True" in body
    # 旧的手写闸不得残留在 run_once 内
    assert 'inbox.get_automation_mode(conv)' not in body
    assert '_skip("crisis")' not in body


# ------------------------------------------------------------- preflight ----

def _root(**sprint):
    s = {"enabled": True, "platforms": ["telegram"]}
    s.update(sprint)
    return {"companion": {"goals": {"enabled": True, "sprint": s}},
            "proactive_care": {"enabled": False, "dry_run": True}}


def test_preflight_all_green_gives_next_due_and_ok():
    n = time.time()
    ib = _Inbox(msgs=[{"ts": n - 7200, "direction": "in"}])
    out = preflight_goal(_goal(), cfg_root=_root(), inbox=ib, now=n,
                         ticker_running=True, dispatcher_running=True)
    assert out["ok"] is True, out["blockers"]
    assert out["blockers"] == []
    assert out["pace"] == "today"
    assert out["next_due"] > n, "ok 时要给下一相位点"
    assert out["process"] == {"ticker_running": True, "dispatcher_running": True}
    assert isinstance(out["due_now"], bool)


def test_preflight_engine_blockers_first_then_process_then_runtime():
    out = preflight_goal(
        _goal(platform="whatsapp", autonomy="suggest"),
        cfg_root=_root(dry_run=True, platforms=["telegram"]),
        inbox=_Inbox(mode="review"),
        ticker_running=False, dispatcher_running=False,
    )
    assert out["ok"] is False
    b = out["blockers"]
    assert b.index("sprint_dry_run") < b.index("platform") < b.index(
        "ticker_not_running") < b.index("autonomy") < b.index("automation_mode")
    assert "dispatcher_not_running" in b
    assert b.count("platform") == 1, "平台闸引擎层已点名，运行时层不重复"
    assert out["next_due"] == 0.0, "不能发的目标不给时间承诺"


def test_preflight_process_unknown_is_not_a_blocker():
    out = preflight_goal(_goal(), cfg_root=_root(), inbox=_Inbox(),
                         ticker_running=None, dispatcher_running=None)
    assert out["ok"] is True
    assert out["process"] == {"ticker_running": None, "dispatcher_running": None}


def test_preflight_inactive_goal_is_blocked():
    out = preflight_goal(_goal(status="done"), cfg_root=_root(), inbox=_Inbox())
    assert "goal_not_active" in out["blockers"] and out["ok"] is False


def test_preflight_natural_goal_uses_daily_gate_and_flag():
    n = time.time()
    g = _goal(pace="natural", deadline_ts=0, start_ts=n - 86400 * 2,
              created_at=n - 86400 * 2)
    out = preflight_goal(g, cfg_root=_root(natural_daily=False), inbox=_Inbox(),
                         now=n)
    assert "natural_daily_off" in out["blockers"]
    out2 = preflight_goal(g, cfg_root=_root(natural_daily=True), inbox=_Inbox(),
                          now=n)
    assert out2["ok"] is True
    assert out2["next_due"] > n


def test_preflight_counts_pending_rows_for_goal_only():
    class _Care:
        def list_recent(self, limit=200):
            return [
                {"status": "pending", "topic_norm": "goal:g1:p0"},
                {"status": "sent", "topic_norm": "goal:g1:p1"},
                {"status": "pending", "topic_norm": "goal:other:p0"},
            ]
    out = preflight_goal(_goal(), cfg_root=_root(), inbox=_Inbox(),
                         care_store=_Care())
    assert out["pending_rows"] == 1


def test_preflight_never_raises_on_garbage():
    out = preflight_goal({"goal_id": "x"}, cfg_root=None, inbox=object())
    assert out["ok"] is False
    assert isinstance(out["blockers"], list) and out["blockers"]


# ------------------------------------------------------------ route/i18n ----

def test_preflight_route_registered_and_reads_process_state():
    src = (_ROOT / "src/web/routes/goal_routes.py").read_text("utf-8")
    assert '@app.get("/api/goals/{goal_id}/preflight")' in src
    body = src.split('@app.get("/api/goals/{goal_id}/preflight")', 1)[1]
    assert 'getattr(app.state, "goal_sprint_ticker", None)' in body
    assert 'getattr(app.state, "care_engine", None)' in body
    assert "preflight_goal(" in body
    assert "next_due_hhmm" in body


def test_sprint_live_only_promises_when_runtime_gates_pass():
    """卡片 sprint_live：运行时闸进 blockers，未过则不给 next_phase_ts / nudgeable。"""
    src = (_ROOT / "src/web/routes/goal_routes.py").read_text("utf-8")
    m = re.search(r"def _attach_sprint_live\(view, store\):(.*?)\n    def _attach_beats",
                  src, re.S)
    assert m, "找不到 _attach_sprint_live"
    body = m.group(1)
    assert "goal_runtime_gates(" in body
    assert "live_ok" in body
    assert 'if live_ok else 0' in body
    assert '"nudgeable": live_ok' in body
    assert '"runtime": runtime' in body


def test_blocker_i18n_keys_bilingual():
    from src.web.i18n_packs.goals import EN, ZH
    for k in ("natural_daily_off", "ticker_not_running", "dispatcher_not_running",
              "goal_not_active", "autonomy", "no_conversation", "no_inbox",
              "automation_mode", "crisis", "optout"):
        key = f"inbox.goal.engine.blk.{k}"
        assert key in ZH and key in EN, key
        assert ZH[key].strip() and EN[key].strip()
