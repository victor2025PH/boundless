# -*- coding: utf-8 -*-
"""坐席日志监控：Traceback 时间戳继承 + 离线判定（2026-09-16 幽灵告警）。"""
from __future__ import annotations

import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MOD_PATH = _REPO / "deploy" / "desktop" / "monitor_seat_logs.py"


@pytest.fixture(scope="module")
def msl():
    spec = importlib.util.spec_from_file_location("_monitor_seat_logs", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_assign_line_timestamps_inherits_for_traceback(msl):
    lines = [
        "[2026-09-13 14:52:01] [ERROR] boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: not iterable",
        "[2026-09-13 15:00:00] [WARNING] later",
    ]
    stamped = msl.assign_line_timestamps(lines)
    assert stamped[0][0] == "2026-09-13 14:52:01"
    assert stamped[1][0] == "2026-09-13 14:52:01"  # Traceback 继承
    assert stamped[2][0] == "2026-09-13 14:52:01"
    assert stamped[3][0] == "2026-09-13 14:52:01"
    assert stamped[4][0] == "2026-09-13 15:00:00"


def test_traceback_below_floor_is_skipped(msl):
    """无戳 Traceback 继承后 ≤ floor → 不得当新问题（旧 bug：ts=None 绕过）。"""
    floor = "2026-09-13 14:52:01"
    lines = [
        "[2026-09-13 14:52:01] [ERROR] boom",
        "Traceback (most recent call last):",
    ]
    kept = []
    for eff_ts, line in msl.assign_line_timestamps(lines):
        if eff_ts and floor and eff_ts <= floor:
            continue
        c = msl.classify(line)
        if c and c[0] == "real":
            kept.append(line)
    assert kept == []


def test_new_traceback_after_floor_still_reports(msl):
    floor = "2026-09-13 14:00:00"
    lines = [
        "[2026-09-13 14:52:01] [ERROR] boom",
        "Traceback (most recent call last):",
    ]
    kept = []
    for eff_ts, line in msl.assign_line_timestamps(lines):
        if eff_ts and floor and eff_ts <= floor:
            continue
        c = msl.classify(line)
        if c and c[0] == "real":
            kept.append((eff_ts, c[2]))
    assert any("后端异常" in why for _, why in kept)
    assert kept[0][0] == "2026-09-13 14:52:01"


def test_seat_offline_when_ping_empty(msl):
    now = datetime(2026, 9, 16, 10, 0, 0)
    seat = {"ok": True, "version": "", "mtime": "2026-09-13T14:52:00"}
    why = msl.seat_offline_reason(seat, now=now, offline_after_h=6.0)
    assert why and "离线" in why and "ping" in why

    online = {"ok": True, "version": "ChatX 1.2.3", "mtime": "2026-09-16T09:50:00"}
    assert msl.seat_offline_reason(online, now=now) is None


def test_seat_offline_stale_mtime_even_with_ping(msl):
    now = datetime(2026, 9, 16, 10, 0, 0)
    seat = {"ok": True, "version": "ChatX 1.0", "mtime": "2026-09-13T14:52:00"}
    why = msl.seat_offline_reason(seat, now=now, offline_after_h=6.0)
    assert why and "停摆" in why


def test_parse_mtime_formats(msl):
    assert msl.parse_mtime("2026-09-13T14:52:00") == datetime(2026, 9, 13, 14, 52, 0)
    assert msl.parse_mtime("2026-09-13 14:52:00") == datetime(2026, 9, 13, 14, 52, 0)
    assert msl.parse_mtime("") is None


def test_offline_signature_stable_across_hours(msl):
    """2026-09-18：离线原因里的「日志已 Nh 未更新」每小时 +1 曾让指纹每小时变一次，
    同一台离线坐席整点重报（140 连报 68 条）。指纹必须对小时数不敏感。"""
    seat = {"ok": True, "version": "", "mtime": "2026-08-24T16:20:01"}
    sigs = set()
    for now in (datetime(2026, 9, 18, 10), datetime(2026, 9, 18, 11),
                datetime(2026, 9, 18, 12), datetime(2026, 9, 19, 12)):
        why = msl.seat_offline_reason(seat, now=now)
        assert why and "无响应" in why
        sigs.add(msl.signature("tingxie", why, f"offline|{seat['mtime']}|"))
    assert len(sigs) == 1, sigs


def test_load_seats_from_real_ledger_excludes_140(msl):
    """真台账：坐席 = chatx_seat=true 的三台；140（记忆机）不在；名字用台账现名 zh。"""
    seats = msl.load_seats()
    ids = [s["id"] for s in seats]
    assert "tingxie" not in ids
    assert set(ids) == {"yunsheng", "lianbei", "kouxing"}
    names = " ".join(s["name"] for s in seats)
    for legacy in ("听写", "智拓", "幻颜", "云升", "口型", "脸备", "韵声"):
        assert legacy not in names, names
    for cur in ("173 语言机(", "104 声音机(", "198 视觉机("):
        assert cur in names, names
    # 显示名括号里是主别名 ssh[0]；状态键 id 与旧别名一致（水位不丢）
    by_id = {s["id"]: s for s in seats}
    assert by_id["kouxing"]["alias"] == "shijue"
    assert by_id["lianbei"]["alias"] == "shengyin"
    assert by_id["yunsheng"]["alias"] == "yuyan"


def test_load_seats_synthetic_and_failures(msl, tmp_path):
    p = tmp_path / "machines.json"
    p.write_text(json.dumps({"machines": [
        {"id": "a", "zh": "甲机", "ip": "10.0.0.11", "ssh": ["a1", "a2"], "chatx_seat": True},
        {"id": "b", "zh": "乙机", "ip": "10.0.0.12", "ssh": ["b1"]},            # 非坐席
        {"id": "c", "zh": "丙机", "ip": "10.0.0.13", "chatx_seat": True},       # 无 ssh → 退回 id
    ]}, ensure_ascii=False), encoding="utf-8")
    seats = msl.load_seats(p)
    assert [s["id"] for s in seats] == ["a", "c"]
    assert seats[0] == {"id": "a", "name": "11 甲机(a1)", "alias": "a1", "ip": "10.0.0.11"}
    assert seats[1]["alias"] == "c" and seats[1]["name"] == "13 丙机(c)"

    with pytest.raises(RuntimeError):
        msl.load_seats(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RuntimeError):
        msl.load_seats(bad)
    none = tmp_path / "none.json"
    none.write_text(json.dumps({"machines": [{"id": "b", "zh": "乙", "ip": "10.0.0.12"}]}), encoding="utf-8")
    with pytest.raises(RuntimeError):
        msl.load_seats(none)


def test_scan_prunes_state_of_machines_no_longer_seats(msl, tmp_path, monkeypatch):
    """140 改编后其 last_seen / 指纹要被清掉；仍是坐席的水位保留。"""
    state_path = tmp_path / "state.json"
    old = time.time() - 60
    state_path.write_text(json.dumps({
        "last_seen": {"kouxing": "2026-09-18 10:00:00", "tingxie": "2026-08-23 05:50:18"},
        "alerted": {"tingxie|坐席离线|offline|#|": old, "kouxing|入站漏球|x": old},
    }), encoding="utf-8")
    monkeypatch.setattr(msl, "STATE_PATH", state_path)
    monkeypatch.setattr(msl, "load_seats", lambda: [
        {"id": "kouxing", "name": "198 视觉机(shijue)", "alias": "shijue", "ip": "192.168.0.198"}])
    monkeypatch.setattr(msl, "pull_seat", lambda alias: {
        "alias": alias, "ok": True, "version": "chengjie 1.087",
        "mtime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "lines": []})
    report = msl.scan(dry_run=False, since_min=0, include_benign=False)
    assert [s["name"] for s in report["seats"]] == ["198 视觉机(shijue)"]
    assert report["new_real"] == []
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert "tingxie" not in saved["last_seen"]
    assert saved["last_seen"]["kouxing"] == "2026-09-18 10:00:00"   # 水位按 id 保留
    assert all(not k.startswith("tingxie|") for k in saved["alerted"])
    assert any(k.startswith("kouxing|") for k in saved["alerted"])


def test_scan_reports_ledger_failure_once_and_keeps_state(msl, tmp_path, monkeypatch):
    """台账读不到 → 一条 critical（同指纹 24h 一次），不清水位、不巡检任何机器。"""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "last_seen": {"kouxing": "2026-09-18 10:00:00"}, "alerted": {}}), encoding="utf-8")
    monkeypatch.setattr(msl, "STATE_PATH", state_path)

    def boom():
        raise RuntimeError("读台账失败 x")
    monkeypatch.setattr(msl, "load_seats", boom)
    monkeypatch.setattr(msl, "pull_seat", lambda alias: pytest.fail("不该巡检任何机器"))

    r1 = msl.scan(dry_run=False, since_min=0, include_benign=False)
    assert r1["seats"] == [] and len(r1["new_real"]) == 1
    assert r1["new_real"][0]["level"] == "critical" and r1["new_real"][0]["seat"] == "台账"
    text = msl.render_report(r1, baseline=False)
    assert "0 台坐席受监控" in text and "台账" in text
    r2 = msl.scan(dry_run=False, since_min=0, include_benign=False)
    assert r2["new_real"] == []                                  # 24h 内不重报
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["last_seen"]["kouxing"] == "2026-09-18 10:00:00"  # 水位没被清


def test_signature_still_separates_kinds_and_seats(msl):
    """去数字不能把不同机器 / 不同问题类别揉成一个指纹。"""
    a = msl.signature("kouxing", "后端异常 / 页面 500 崩溃", "[2026-09-18 10:00:00] Traceback x")
    b = msl.signature("lianbei", "后端异常 / 页面 500 崩溃", "[2026-09-18 10:00:00] Traceback x")
    c = msl.signature("kouxing", "入站漏球", "[2026-09-18 10:00:00] Traceback x")
    assert len({a, b, c}) == 3
    # 同机同类、只差时间戳与数字 → 同一指纹
    d = msl.signature("kouxing", "后端异常 / 页面 500 崩溃", "[2026-09-18 11:30:00] Traceback x")
    assert a == d
