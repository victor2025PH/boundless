"""health_report 巡检的数据根契约与告警判据门禁（2026-08-27 事故沉淀）。

事故：脚本按**引擎仓库根**读 inbox.db / auth_token / logs，而生产实例的数据根在
``D:\\chengjie-instances\\<iid>\\data``。后果不是「少看几个数」，而是巡检
**结构性失明 + 每天误报**——会话列表冻结在两周前（告警里 5 个会话 ID 一字不差）、
token 是旧的（/thread 恒 401）、重启与热重载计数恒空，连续 14 天没人能读出信息。
叠加硬编码 ``实例数 != 1`` 的单实例时代判据，迁多实例后恒红。

本门禁守三条不变量：
1. 落点一律从 Target（数据根推导），源码里不得再出现引擎根 config/logs 字面量；
2. 实例数判据＝进程数 vs 活跃实例数，不得回退成硬编码 1；
3. 「采不到样」必须显式告警——静默返回空列表正是「监控假装在工作」的温床。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from scripts import health_report as hr  # noqa: E402


def _instances(n_proc, expected, ids=("zhiliao",)):
    return {"main_py_processes": n_proc, "expected_instances": expected,
            "instance_ids": list(ids)}


def _inst_row(**kw):
    row = {
        "instance": "zhiliao", "root": r"D:\x\data", "port": 18799,
        "logs": r"D:\x\data\logs", "web_ready": True, "probe_ms": 12,
        "web_error": "", "thread_latency": [{"conv": "telegram:1", "ms": 30, "ok": True}],
        "skip": {}, "inbound_xlate": {"untranslated_stock": 0, "by_conv": {}, "daily_7d": []},
        "restarts_by_day": {}, "hot_reloads_by_day": {"config": {}, "i18n": {}},
        "unclean_deaths_today": 0,
    }
    row.update(kw)
    return row


# ---------------------------------------------------------------- 实例数判据

def test_multi_instance_is_not_an_alert():
    """两个实例 + 两个进程＝正常态。硬编码 `!=1` 时代这里恒红两周。"""
    report = {"instances": _instances(2, 2, ("zhiliao", "zhiliao_pilot")),
              "by_instance": [_inst_row()]}
    assert hr._detect_alerts(report) == []


def test_single_instance_still_normal():
    report = {"instances": _instances(1, 1), "by_instance": [_inst_row()]}
    assert hr._detect_alerts(report) == []


def test_process_missing_is_alerted():
    """期望 2 个却只跑 1 个＝有实例没起来，必须报。"""
    report = {"instances": _instances(1, 2, ("zhiliao", "zhiliao_pilot")),
              "by_instance": [_inst_row()]}
    alerts = hr._detect_alerts(report)
    assert any("少了" in a for a in alerts), alerts


def test_process_stampede_is_alerted():
    report = {"instances": _instances(3, 2, ("zhiliao", "zhiliao_pilot")),
              "by_instance": [_inst_row()]}
    alerts = hr._detect_alerts(report)
    assert any("踩踏" in a for a in alerts), alerts


def test_zero_process_is_alerted():
    report = {"instances": _instances(0, 2), "by_instance": [_inst_row()]}
    assert any("服务全死" in a for a in hr._detect_alerts(report))


def test_probe_failure_does_not_false_alarm():
    """进程探测本身失败（-1）信息不足，宁可漏报不误报。"""
    report = {"instances": _instances(-1, 2), "by_instance": [_inst_row()]}
    assert not any("进程数" in a for a in hr._detect_alerts(report))


# ---------------------------------------------------------------- 失明必须发声

@pytest.mark.parametrize("code", sorted(hr.SKIP_BLIND))
def test_blind_instance_is_alerted(code):
    """巡检自己坏了（缺 token / 找不到库 / 读库失败）＝失明，比「没有告警」更危险。"""
    report = {"instances": _instances(1, 1), "by_instance": [_inst_row(
        thread_latency=[], skip={"code": code, "text": "x"})]}
    assert any("失明" in a for a in hr._detect_alerts(report))


def test_brand_new_instance_without_conversations_is_not_alerted():
    """新租户还没会话＝确实无可采样，不是失明。报它就是拿噪音换覆盖。"""
    report = {"instances": _instances(1, 1), "by_instance": [_inst_row(
        thread_latency=[], skip={"code": "no_conversations", "text": "新实例"})]}
    assert hr._detect_alerts(report) == []


def test_thread_failure_carries_status_code():
    """失败原因必须带 HTTP 码——原实现只记 `HTTPError`，把 401 藏了两周。"""
    report = {"instances": _instances(1, 1), "by_instance": [_inst_row(
        thread_latency=[{"conv": "telegram:1", "ms": 15, "ok": False, "error": "HTTP 401"}])]}
    alerts = hr._detect_alerts(report)
    assert any("HTTP 401" in a for a in alerts), alerts


def test_http_error_label_keeps_code():
    import urllib.error
    exc = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
    assert hr._http_error_label(exc) == "HTTP 401"
    assert hr._http_error_label(TimeoutError()) == "TimeoutError"


# ---------------------------------------------------------------- 其余判据

def test_restart_red_line_and_web_down():
    import time
    today = time.strftime("%Y%m%d")
    report = {"instances": _instances(1, 1), "by_instance": [_inst_row(
        web_ready=False, web_error="URLError",
        restarts_by_day={today: hr.RESTART_RED_LINE + 1})]}
    alerts = hr._detect_alerts(report)
    assert any("不可达" in a for a in alerts), alerts
    assert any("超纪律" in a for a in alerts), alerts


def test_alerts_are_instance_tagged():
    """多实例下每条告警必须点名是哪个实例，否则运维无法定位。"""
    report = {"instances": _instances(2, 2, ("zhiliao", "zhiliao_pilot")),
              "by_instance": [_inst_row(instance="zhiliao_pilot", web_ready=False)]}
    assert any(a.startswith("[zhiliao_pilot]") for a in hr._detect_alerts(report))


# ---------------------------------------------------------------- 落点契约

def test_targets_derive_paths_from_data_root(tmp_path):
    """token / db / logs / port 全部从数据根推导，绝不回落引擎根。"""
    root = tmp_path / "acme" / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(
        "web_admin:\n  port: 19099\n  auth_token: tok-from-instance\n", encoding="utf-8")

    targets = hr.build_targets(str(root))
    assert len(targets) == 1
    t = targets[0]
    assert t.iid == "acme"
    assert t.port == 19099
    assert t.token == "tok-from-instance"
    assert t.base == "http://127.0.0.1:19099"
    assert t.db == root / "config" / "inbox.db"
    assert t.logs == root / "logs"


def test_overlay_wins_over_base_config(tmp_path):
    """config.local.yaml overlay 优先——生产真 token 恰恰只在 overlay 里。"""
    root = tmp_path / "acme" / "data"
    (root / "config").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(
        "web_admin:\n  port: 18799\n  auth_token: stale\n", encoding="utf-8")
    (root / "config" / "config.local.yaml").write_text(
        "web_admin:\n  auth_token: live-token\n", encoding="utf-8")

    t = hr.build_targets(str(root))[0]
    assert t.token == "live-token"
    assert t.port == 18799


def test_suspended_tenant_is_not_expected_to_run(tmp_path, monkeypatch):
    """暂停中的租户不计入活跃实例，否则进程数判据会永远差一个。"""
    base = tmp_path / "instances"
    for iid in ("live", "paused"):
        cfg = base / iid / "data" / "config"
        cfg.mkdir(parents=True)
        (cfg / "config.yaml").write_text("web_admin:\n  port: 18799\n  auth_token: t\n",
                                         encoding="utf-8")
    flag = base / ".ops" / "suspended"
    flag.mkdir(parents=True)
    (flag / "paused.flag").write_text("", encoding="utf-8")
    monkeypatch.setenv("CHENGJIE_INSTANCES_BASE", str(base))
    monkeypatch.delenv("AITR_DATA_ROOT", raising=False)

    ids = [t.iid for t in hr.build_targets()]
    assert ids == ["live"]


def test_token_override_ignored_when_multi_target(tmp_path, monkeypatch):
    """多实例下一把 token 打所有端口必然 401——那正是本次事故的形态。"""
    base = tmp_path / "instances"
    for iid, port in (("a", 18799), ("b", 18999)):
        cfg = base / iid / "data" / "config"
        cfg.mkdir(parents=True)
        (cfg / "config.yaml").write_text(
            f"web_admin:\n  port: {port}\n  auth_token: own-{iid}\n", encoding="utf-8")
    monkeypatch.setenv("CHENGJIE_INSTANCES_BASE", str(base))
    monkeypatch.delenv("AITR_DATA_ROOT", raising=False)

    targets = hr.build_targets("", "one-token-to-rule-them-all")
    assert sorted(t.token for t in targets) == ["own-a", "own-b"]


def test_no_engine_root_data_paths_left_in_source():
    """静态 ratchet：源码里不得再出现「引擎根 / CWD 相对」的数据落点。

    告警留痕 logs/health_alerts.log 是巡检自己的账本（不属任何被观测实例），
    经 ENGINE_ROOT 显式拼接，不在此禁令内。
    """
    src = (ENGINE_ROOT / "scripts" / "health_report.py").read_text(encoding="utf-8")
    forbidden = [
        'ROOT / "config" / "inbox.db"',
        'ROOT / "logs"' if 'ENGINE_ROOT / "logs"' not in src else "",
        'BASE = "http://127.0.0.1:18799"',
    ]
    for bad in forbidden:
        if bad:
            assert bad not in src, f"数据落点回退到引擎根: {bad}"
    # 端口不得再硬编码成模块级常量（多实例各有各的端口）
    assert "def build_targets" in src
    assert "resolve_data_roots" in src


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
