# -*- coding: utf-8 -*-
"""人设考题夜间自动回归（J2 线）门禁——**零真 LLM**，全部走假 chat_fn / 假接缝。

覆盖五层：
1. 选人 —— ``select_personas``（资料太薄的人设被跳过 / 显式 ``--personas`` 覆盖默认 /
   不存在的 id 记 not_found）。
2. 判定纯函数 —— ``evaluate_run``（合格汇总 + delta 计算 + 压线语义 + 错题裁剪）与
   ``history_from_overview``（本轮之前的历史均分快照）。
3. 告警判定 —— 不合格触发 / 下滑触发 / 都正常不触发 / **无历史（首次跑）不误报下滑**，
   以及 ``build_alert_message`` 的可读性。
4. 退出码三态 —— 全合格 0 / 有不合格或下滑 1 / 执行异常 2（外加「未启用」「dry-run」0）。
5. 编排 —— ``--dry-run`` 一次都不调 chat_fn（计数 stub 断言 0 次）、历史快照必须早于
   落库、``--no-alert`` 不发告警、真 ``persona_quiz_store`` 端到端存档。

告警出口是 ``host_alert.notify_host``（复用现成 ``host_alert`` 事件与订阅别名，不新增
事件类型）；测试进程由 conftest 的 ``HOST_ALERT_SILENT=1`` 全局静默，本文件另把
``_send_alert`` 换成计数假函数，双保险绝不真弹窗/真外发。
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts import persona_quiz_nightly as nightly  # noqa: E402
from src.utils import persona_quiz as pq  # noqa: E402
from src.utils import persona_quiz_store as pqs  # noqa: E402

# ── 档案 fixture（形状抄 test_persona_quiz.py 的富档案，保证 build_quiz 出得来题）──

_RICH = {
    "id": "mizuki_test",
    "name": "美月",
    "role": "在西班牙巴塞罗那的W Barcelona酒店运营总监助理，32岁",
    "age": 32,
    "background": (
        "美月1992年9月13日出生于西班牙潘普洛纳。"
        "2010年考入都灵大学，主修酒店管理与旅游，2014年毕业。"
    ),
    "context": {
        "specific_memories": [
            "父亲José Leandro Navarro是建筑师，2019年5月因肺癌去世，享年68岁。",
            "母亲Misaki Fujimura在潘普洛纳开设寿司外卖店Sushi Ya。",
            "2016年10月在米兰意大利原野贸易公司任国际贸易专员。",
            "出生于1992年9月13日西班牙Navarra地区Pamplona私人医院。",
        ],
    },
    "tastes": {"likes": ["抹茶", "Barolo红酒"]},
}

_RICH2 = dict(_RICH, id="haruko_test", name="遥子")
_THIN = {"id": "thin_one", "name": "小薄"}

_LIMIT = 8


def _profiles():
    return {"mizuki_test": _RICH, "haruko_test": _RICH2, "thin_one": _THIN}


_WRONG_ANSWER = "这个我真不记得了"


def _answer_map(correct=True, n=_LIMIT):
    """题干 → 作答的查表：``correct`` 时逐题拼上 expect 关键词（判分必中）。"""
    out = {}
    for persona in (_RICH, _RICH2):
        for item in pq.build_quiz(persona, n=n):
            out[item["q"]] = ("、".join(item["expect"]) if correct
                              else _WRONG_ANSWER)
    return out


class _Env:
    """把所有编排接缝换成假实现，记录调用序列供断言。"""

    def __init__(self):
        self.chat_built = 0
        self.chat_calls = 0
        self.saved = []
        self.alerts = []
        self.order = []
        self.history = {}
        self.profiles = _profiles()
        self.correct = True
        self.nightly_cfg = {"enabled": True, "min_score": 0.7,
                            "drop_alert": 0.15, "limit": _LIMIT, "alert": True}

    def config(self):
        return {"personas": {"quiz": {"enabled": True,
                                      "nightly": dict(self.nightly_cfg)}}}


@pytest.fixture
def env(monkeypatch):
    e = _Env()

    def _chat_fn(system, user, timeout):
        e.chat_calls += 1
        return _answer_map(correct=e.correct).get(user, _WRONG_ANSWER)

    def _build_chat_fn():
        e.chat_built += 1
        return _chat_fn, lambda: None

    def _save(pid, report):
        e.order.append(("save", pid))
        e.saved.append((pid, report))
        return len(e.saved)

    def _hist():
        e.order.append(("history", None))
        return dict(e.history)

    def _alert(title, message):
        e.alerts.append((title, message))
        return True

    monkeypatch.setattr(nightly, "_load_config", e.config)
    monkeypatch.setattr(nightly, "load_personas", lambda cfg: dict(e.profiles))
    monkeypatch.setattr(nightly, "_build_chat_fn", _build_chat_fn)
    monkeypatch.setattr(nightly, "_history_snapshot", _hist)
    monkeypatch.setattr(nightly, "_save_report", _save)
    monkeypatch.setattr(nightly, "_send_alert", _alert)
    return e


# ── 1. 选人 ──────────────────────────────────────────────────────────────────

def test_select_skips_persona_without_material():
    """资料太薄（build_quiz 出不了题）的人设不进考场——空卷判分恒 0 分＝假告警。"""
    selected, skipped = nightly.select_personas(_profiles(), limit=_LIMIT)
    ids = [s["persona_id"] for s in selected]
    assert "thin_one" not in ids
    assert set(ids) == {"haruko_test", "mizuki_test"}
    assert ids == sorted(ids), "缺省顺序按 id 升序，保证每晚稳定"
    assert [(s["persona_id"], s["reason"]) for s in skipped] == \
        [("thin_one", "no_questions")]
    for item in selected:
        assert len(item["quiz"]) >= pq.MIN_QUIZ_ITEMS
        assert item["quiz"] == pq.build_quiz(item["persona"], n=_LIMIT), \
            "选人期出的题必须与 run_quiz(n=limit) 的考卷一致（确定性）"


def test_select_explicit_overrides_default_and_flags_unknown():
    selected, skipped = nightly.select_personas(
        _profiles(), explicit=["haruko_test", "ghost_id"], limit=_LIMIT)
    assert [s["persona_id"] for s in selected] == ["haruko_test"]
    assert [(s["persona_id"], s["reason"]) for s in skipped] == \
        [("ghost_id", "not_found")]


def test_select_empty_profiles_is_safe():
    assert nightly.select_personas({}, limit=_LIMIT) == ([], [])


# ── 1b. 覆盖率回退（K2d，2026-07-28）────────────────────────────────────────

def test_coverage_regressions_flags_only_previously_covered():
    """「以前考得了、现在出不来题」＝监控被静默丢掉，分数曲线上完全看不出来。"""
    skipped = [
        {"persona_id": "was_ok", "persona_name": "老人设", "reason": "no_questions"},
        {"persona_id": "brand_new", "persona_name": "新草稿", "reason": "no_questions"},
        {"persona_id": "typo_id", "persona_name": "", "reason": "not_found"},
    ]
    history = {"was_ok": 92.5, "typo_id": 80.0}
    lost = nightly.coverage_regressions(skipped, history)
    assert lost == [{"persona_id": "was_ok", "persona_name": "老人设",
                     "avg_before": 92.5}]
    # 没历史 = 从没考过的新档案，真薄，不轰人
    assert nightly.coverage_regressions(skipped, {}) == []
    # 入参异常一律安全返回（夜间任务不许因为脏数据炸掉）
    assert nightly.coverage_regressions(None, None) == []
    assert nightly.coverage_regressions(["not a dict"], {"x": 1}) == []


def test_coverage_loss_enters_alert_message():
    summary = {
        "personas": [], "failed": [], "dropped": [], "min_score": 0.7,
        "coverage_lost": [{"persona_id": "was_ok", "persona_name": "老人设",
                           "avg_before": 92.5}],
    }
    title, body = nightly.build_alert_message(summary)
    assert "失去监控" in body and "老人设" in body and "92.5" in body


# ── 2. 历史快照 + 打分汇总 / delta ───────────────────────────────────────────

def test_history_from_overview_maps_avg_scores():
    overview = {
        "personas": [
            {"persona_id": "a", "avg_score": 88.5},
            {"persona_id": "b", "avg_score": None},     # 无均分 → 不进历史
            {"persona_id": "", "avg_score": 50},        # 无 id → 丢弃
            "junk",
        ],
        "totals": {},
    }
    assert nightly.history_from_overview(overview) == {"a": 88.5}
    assert nightly.history_from_overview(None) == {}
    assert nightly.history_from_overview({}) == {}


def _result(pid, score, total=10, items=None):
    passed = round(score / 100 * total)
    return {"persona_id": pid, "persona_name": pid.upper(), "score": score,
            "passed": passed, "total": total, "items": items or []}


def test_evaluate_run_summary_and_delta():
    summary = nightly.evaluate_run(
        [_result("a", 100), _result("b", 80)],
        history={"a": 90.0, "b": 82.5},
        min_score=0.7, drop_alert=0.15)
    by_id = {r["persona_id"]: r for r in summary["personas"]}
    assert by_id["a"]["delta"] == 10.0 and by_id["a"]["avg_before"] == 90.0
    assert by_id["b"]["delta"] == -2.5
    assert summary["ok_count"] == 2
    assert summary["failed"] == [] and summary["dropped"] == []
    assert summary["alert"] is False and summary["exit_code"] == 0


def test_evaluate_run_boundary_scores_are_not_alarms():
    """压线不报警：score == 门槛算合格；下滑正好等于阈值不算下滑。"""
    summary = nightly.evaluate_run(
        [_result("a", 70), _result("b", 85)],
        history={"b": 100.0}, min_score=0.7, drop_alert=0.15)
    assert summary["failed"] == [] and summary["dropped"] == []
    assert summary["exit_code"] == 0


def test_evaluate_run_accepts_percent_style_thresholds():
    """``--min-score 70`` 这类百分数写法自动归一，不至于把所有人判成不合格。"""
    summary = nightly.evaluate_run([_result("a", 75)], history={},
                                   min_score=70, drop_alert=15)
    assert summary["min_score"] == 0.7 and summary["drop_alert"] == 0.15
    assert summary["failed"] == []


def test_evaluate_run_wrong_items_only_and_truncated():
    items = [
        {"q": "你今年多大？", "expect": ["32"], "answer": "32岁啦", "pass": True},
        {"q": "你父亲叫什么名字？", "expect": ["José", "建筑师"],
         "answer": "唔" * 300, "pass": False},
    ]
    summary = nightly.evaluate_run([_result("a", 50, items=items)], history={})
    wrong = summary["personas"][0]["wrong"]
    assert len(wrong) == 1
    assert wrong[0]["q"] == "你父亲叫什么名字？"
    assert wrong[0]["expect"] == ["José", "建筑师"]
    assert len(wrong[0]["answer"]) == nightly.WRONG_ANSWER_CHARS


# ── 3. 告警判定 ──────────────────────────────────────────────────────────────

def test_alert_on_low_score():
    summary = nightly.evaluate_run([_result("a", 40)], history={"a": 45.0},
                                   min_score=0.7, drop_alert=0.15)
    assert summary["failed"] == ["a"] and summary["dropped"] == []
    assert summary["ok_count"] == 0
    assert summary["alert"] is True and summary["exit_code"] == 1


def test_alert_on_score_drop_even_when_still_passing():
    """90 分仍高于门槛，但比历史均分掉了 20 分——回归信号，必须报。"""
    summary = nightly.evaluate_run([_result("a", 90)], history={"a": 110.0},
                                   min_score=0.7, drop_alert=0.15)
    assert summary["failed"] == [] and summary["dropped"] == ["a"]
    assert summary["ok_count"] == 1      # 合格数只看分数线
    assert summary["alert"] is True and summary["exit_code"] == 1


def test_no_alert_when_all_healthy():
    summary = nightly.evaluate_run([_result("a", 95), _result("b", 88)],
                                   history={"a": 93.0, "b": 90.0})
    assert summary["alert"] is False and summary["exit_code"] == 0


def test_first_run_without_history_never_reports_drop():
    """首次跑：没有历史均分 → delta 为 None，绝不误报「下滑」。"""
    summary = nightly.evaluate_run([_result("a", 40)], history={},
                                   min_score=0.3, drop_alert=0.15)
    assert summary["personas"][0]["avg_before"] is None
    assert summary["personas"][0]["delta"] is None
    assert summary["dropped"] == [] and summary["failed"] == []
    assert summary["alert"] is False and summary["exit_code"] == 0


def test_build_alert_message_lists_both_kinds():
    summary = nightly.evaluate_run([_result("a", 40), _result("b", 90)],
                                   history={"b": 110.0}, min_score=0.7,
                                   drop_alert=0.15)
    title, body = nightly.build_alert_message(summary)
    assert "人设" in title and "考题" in title
    assert "不合格" in body and "A" in body and "40" in body
    assert "下滑" in body and "B" in body and "110" in body


# ── 4/5. 编排 + 退出码 ───────────────────────────────────────────────────────

def test_dry_run_never_calls_llm(env, capsys):
    rc = nightly.main(["--dry-run", "--sleep", "0"])
    assert rc == 0
    assert env.chat_built == 0, "dry-run 连 chat_fn 都不该构建（会连 AIClient）"
    assert env.chat_calls == 0
    assert env.saved == [] and env.alerts == []
    out = capsys.readouterr().out
    assert "你今年多大？" in out           # 题目已生成可核对
    assert "thin_one" in out               # 跳过清单可见


def test_exit_zero_when_all_pass(env):
    env.correct = True
    rc = nightly.main(["--sleep", "0"])
    assert rc == 0
    assert env.chat_calls > 0
    assert [pid for pid, _ in env.saved] == ["haruko_test", "mizuki_test"]
    assert all(r["score"] == 100 for _, r in env.saved)
    assert env.alerts == []


def test_exit_one_and_alert_when_failing(env):
    env.correct = False
    rc = nightly.main(["--sleep", "0"])
    assert rc == 1
    assert all(r["score"] == 0 for _, r in env.saved)
    assert len(env.alerts) == 1
    title, body = env.alerts[0]
    assert "美月" in body and "遥子" in body


def test_coverage_loss_alerts_end_to_end(env):
    """真机复发路径：档案还在、但出题守卫/字段被改窄 → 该人设静默退出考场。
    分数看板上只是少了一行，必须由这条链兜住（告警 + exit 1）。"""
    env.correct = True
    env.history = {"thin_one": 88.0}     # 说明它以前考得了
    rc = nightly.main(["--sleep", "0"])
    assert rc == 1, "失去监控与考砸同级：都要让夜间任务变红"
    assert len(env.alerts) == 1
    assert "失去监控" in env.alerts[0][1] and "88.0" in env.alerts[0][1]
    # 其余人设照常跑完落库（一个人设丢监控不影响全场）
    assert [pid for pid, _ in env.saved] == ["haruko_test", "mizuki_test"]


def test_all_personas_uncoverable_still_alerts(env):
    """极端态：全员出不来题。旧实现在这里直接 return 0，等于全站失去监控却静默。"""
    env.profiles = {"thin_one": env.profiles["thin_one"]}
    env.history = {"thin_one": 91.0}
    rc = nightly.main(["--sleep", "0"])
    assert rc == 1 and len(env.alerts) == 1
    assert env.chat_built == 0, "没人可考就不该建 LLM 客户端"


def test_new_thin_persona_does_not_alert(env):
    """反例：新加的草稿人设从没考过 → 真薄，不该每晚轰人。"""
    env.correct = True
    env.history = {}
    assert nightly.main(["--sleep", "0"]) == 0
    assert env.alerts == []


def test_exit_two_on_execution_error(env, monkeypatch):
    def _boom(cfg):
        raise RuntimeError("profiles 装配炸了")

    monkeypatch.setattr(nightly, "load_personas", _boom)
    assert nightly.main(["--sleep", "0"]) == 2


def test_disabled_flag_exits_zero_without_touching_llm(env):
    env.nightly_cfg["enabled"] = False
    assert nightly.main(["--sleep", "0"]) == 0
    assert env.chat_built == 0 and env.saved == []
    # --force 才真跑
    assert nightly.main(["--sleep", "0", "--force"]) == 0
    assert env.chat_built == 1


def test_no_alert_flag_suppresses_send_but_keeps_exit_code(env):
    env.correct = False
    rc = nightly.main(["--sleep", "0", "--no-alert"])
    assert rc == 1
    assert env.alerts == []


def test_history_snapshot_taken_before_any_save(env):
    """历史均分必须在本轮落库之前取——否则本次成绩会稀释自己的 delta。"""
    env.correct = True
    nightly.main(["--sleep", "0"])
    kinds = [k for k, _ in env.order]
    assert kinds[0] == "history"
    assert "history" not in kinds[1:]


def test_explicit_personas_cli_limits_scope(env):
    env.correct = True
    rc = nightly.main(["--sleep", "0", "--personas", "mizuki_test"])
    assert rc == 0
    assert [pid for pid, _ in env.saved] == ["mizuki_test"]


def test_json_output_is_machine_readable(env, capsys):
    env.correct = False
    rc = nightly.main(["--sleep", "0", "--json", "--no-alert"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["min_score"] == 0.7 and payload["drop_alert"] == 0.15
    assert sorted(payload["failed"]) == ["haruko_test", "mizuki_test"]
    assert payload["exit_code"] == 1
    assert payload["skipped"][0]["persona_id"] == "thin_one"
    assert payload["personas"][0]["wrong"], "不合格人设应带错题清单"


def test_cli_thresholds_override_config(env):
    """命令行参数优先于配置：把门槛降到 0 → 0 分也算合格。"""
    env.correct = False
    assert nightly.main(["--sleep", "0", "--min-score", "0"]) == 0
    assert env.alerts == []


def test_resolve_settings_config_defaults_then_cli():
    class _Args:
        personas = "a, b"
        limit = None
        min_score = None
        drop_alert = None
        sleep = None
        no_alert = False

    cfg = {"personas": {"quiz": {"nightly": {"enabled": True, "min_score": 0.6,
                                             "drop_alert": 0.2, "limit": 5,
                                             "alert": False}}}}
    st = nightly.resolve_settings(cfg, _Args())
    assert st == {"enabled": True, "min_score": 0.6, "drop_alert": 0.2,
                  "limit": 5, "sleep_sec": nightly.DEFAULT_SLEEP_SEC,
                  "alert": False, "personas": ["a", "b"]}

    _Args.limit = 9
    _Args.min_score = 0.95
    st2 = nightly.resolve_settings(cfg, _Args())
    assert st2["limit"] == 9 and st2["min_score"] == 0.95

    # 配置整段缺失 → 全部走模块缺省，且默认不启用（新子系统约定）
    st3 = nightly.resolve_settings({}, _Args())
    assert st3["enabled"] is False
    assert st3["drop_alert"] == nightly.DEFAULT_DROP_ALERT


def test_end_to_end_reports_land_in_real_store(env, tmp_path, monkeypatch):
    """真 ``persona_quiz_store``（tmp 库）：报告落档 + overview 能读回历史均分。

    只把 ``_save_report`` / ``_history_snapshot`` 换回真实现（env 的其余假接缝保留），
    库路径经 ``--db`` 指到 tmp，绝不碰生产 ``config/persona_quiz.db``。
    """
    monkeypatch.setattr(nightly, "_save_report",
                        lambda pid, rep: pqs.save_report(pid, rep))
    monkeypatch.setattr(nightly, "_history_snapshot",
                        lambda: nightly.history_from_overview(pqs.overview()))
    db = tmp_path / "quiz_reports.db"
    pqs.reset()
    try:
        env.correct = True
        rc = nightly.main(["--sleep", "0", "--db", str(db)])
        assert rc == 0
        reports = pqs.list_reports("mizuki_test", limit=5)
        assert len(reports) == 1 and reports[0]["score"] == 100
        hist = nightly.history_from_overview(pqs.overview())
        assert hist["mizuki_test"] == 100.0
    finally:
        pqs.reset()


def test_alert_goes_through_host_alert_notify_host(monkeypatch):
    """告警走仓内既有出口 ``host_alert.notify_host``（复用 host_alert 事件与别名）。"""
    from src.utils import host_alert

    seen = {}

    def _fake(title, message, *, key="", cooldown_sec=0.0):
        seen.update(title=title, message=message, key=key, cooldown=cooldown_sec)
        return True

    monkeypatch.setattr(host_alert, "notify_host", _fake)
    assert nightly._send_alert("T", "M") is True
    assert seen["key"] == nightly.ALERT_KEY
    assert seen["cooldown"] == nightly.ALERT_COOLDOWN_SEC
    # 测试进程弹窗全局静默（conftest 设 HOST_ALERT_SILENT=1），双保险
    assert os.environ.get("HOST_ALERT_SILENT") == "1"
    assert host_alert.popups_suppressed() is True
