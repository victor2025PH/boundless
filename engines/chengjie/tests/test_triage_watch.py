"""triage_watch 门禁：偏差检测（NEW/SURGE）+ 基线合并 + 首跑抑制 + 退出码。"""

import json

from scripts.log_triage import Group
from scripts.triage_watch import (
    Finding,
    _key,
    detect_deviations,
    load_baseline,
    main,
    merge_baseline,
    save_baseline,
    summarize,
    window_since,
)


def _g(level, logger, template, count, today=0, first="2026-07-23 00:00:00",
       last="2026-07-23 06:00:00"):
    return Group(level=level, logger=logger, template=template, count=count,
                 first_ts=first, last_ts=last, today_count=today, sample=template)


def test_new_error_template_reported_even_at_count_1():
    groups = [_g("ERROR", "db", "no such column: <n>", 1)]
    f = detect_deviations(groups, {})
    assert len(f) == 1 and f[0].kind == "new" and f[0].level == "ERROR"


def test_new_warning_below_floor_not_reported():
    # 新 WARNING 门槛更高（默认 10），少量新 WARNING 视为噪声不报
    groups = [_g("WARNING", "net", "timeout <n>", 5)]
    assert detect_deviations(groups, {}) == []
    # 达到门槛则报
    groups = [_g("WARNING", "net", "timeout <n>", 12)]
    f = detect_deviations(groups, {})
    assert len(f) == 1 and f[0].kind == "new" and f[0].level == "WARNING"


def test_known_template_not_reported():
    tmpl = "timeout <n>"
    groups = [_g("WARNING", "net", tmpl, 15)]
    baseline = {_key("WARNING", "net", tmpl): 100}  # 已知且水位高
    assert detect_deviations(groups, baseline) == []


def test_surge_requires_factor_and_absolute_min():
    tmpl = "conn reset <n>"
    k = _key("WARNING", "net", tmpl)
    # 基线 5，现 30 = 6x 且 ≥ surge_min(20) → 报
    f = detect_deviations([_g("WARNING", "net", tmpl, 30)], {k: 5})
    assert len(f) == 1 and f[0].kind == "surge" and f[0].baseline_count == 5
    # 基线 1 现 4 = 4x 但 < surge_min → 不报（防小基数放大误报）
    assert detect_deviations([_g("WARNING", "net", tmpl, 4)], {k: 1}) == []
    # 基线 100 现 120 = 1.2x < factor → 不报
    assert detect_deviations([_g("WARNING", "net", tmpl, 120)], {k: 100}) == []


def test_error_sorted_before_warning():
    groups = [
        _g("WARNING", "a", "w", 50),
        _g("ERROR", "b", "e", 2),
    ]
    f = detect_deviations(groups, {})
    assert f[0].level == "ERROR"  # ERROR 优先于高计数 WARNING


def test_merge_baseline_takes_max():
    tmpl = "x <n>"
    k = _key("ERROR", "db", tmpl)
    base = {k: 10}
    merged = merge_baseline(base, [_g("ERROR", "db", tmpl, 3)])
    assert merged[k] == 10  # 只升不降
    merged2 = merge_baseline(base, [_g("ERROR", "db", tmpl, 42)])
    assert merged2[k] == 42


def test_baseline_roundtrip(tmp_path):
    p = tmp_path / "triage" / "baseline.json"
    save_baseline(p, {"a\x1fb\x1fc": 7})
    assert load_baseline(p) == {"a\x1fb\x1fc": 7}
    # 不存在 → 空
    assert load_baseline(tmp_path / "nope.json") == {}


def test_summarize_empty_and_nonempty():
    assert "无新增" in summarize([], window_hours=6)
    f = [Finding("new", "ERROR", "db", "boom <n>", 3, 0, "t1", "t2", "boom")]
    s = summarize(f, window_hours=6)
    assert "ERROR" in s and "新增" in s


def test_window_since_format():
    from datetime import datetime
    s = window_since(6, now=datetime(2026, 7, 23, 12, 0, 0))
    assert s == "2026-07-23 06:00:00"


# ── 端到端：首跑只建基线不告警；二跑对新错误告警 + 退出码 ──────────────

_LOG = """[2026-07-23 05:00:00] [WARNING] net: timeout id=aaa
[2026-07-23 05:01:00] [WARNING] net: timeout id=bbb
[2026-07-23 05:02:00] [INFO] app: started ok
"""

_LOG2 = _LOG + "[2026-07-23 05:30:00] [ERROR] db: no such column: xyz\n"


def _write_log(tmp_path, content):
    d = tmp_path / "logs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "app.log"
    p.write_text(content, encoding="utf-8")
    return p


def test_first_run_seeds_baseline_no_alert(tmp_path, monkeypatch):
    import scripts.triage_watch as tw
    log = _write_log(tmp_path, _LOG)
    # 固定 since 窗口覆盖测试日志
    monkeypatch.setattr(tw, "window_since", lambda h, now=None: "2026-07-23 00:00:00")
    rc = main(["--file", str(log), "--no-update-baseline"])
    # first run: even with --no-update, first_run detection is by baseline file absence
    assert rc == 0
    # 建立基线（去掉 --no-update-baseline）
    rc = main(["--file", str(log)])
    assert rc == 0
    bp = log.parent / "triage" / "baseline.json"
    assert bp.exists()


def test_second_run_flags_new_error(tmp_path, monkeypatch):
    import scripts.triage_watch as tw
    monkeypatch.setattr(tw, "window_since", lambda h, now=None: "2026-07-23 00:00:00")
    log = _write_log(tmp_path, _LOG)
    assert main(["--file", str(log)]) == 0          # 首跑建基线
    log.write_text(_LOG2, encoding="utf-8")           # 新增一条 ERROR
    rc = main(["--file", str(log)])
    assert rc == 2                                    # ERROR finding → 退出码 2


def test_missing_log_returns_3(tmp_path):
    assert main(["--file", str(tmp_path / "nope.log")]) == 3
