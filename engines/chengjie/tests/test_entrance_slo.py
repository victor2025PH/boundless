# -*- coding: utf-8 -*-
"""入口可用性 SLO（P1-7 2026-08-12）：边缘看门狗日志解析 + 快照合帐契约。

钉住的语义（改 entrance_slo.py 前先读）：
- OK/WARN/RECOVERED＝可用 tick（WARN=「ssh 探针抖但 public=200 用户无感」）；
  STRIKE/HOLD/FAILED＝不可用 tick；BOOT/RESTART/ALERT 等动作行不是探测结论不计。
- outage＝连续不可用 tick 段，时长=tick 数×5min（看门狗采样精度的诚实上限）；
  窗口末尾未恢复的段照记并标 ongoing。
- 一切软失败：垃圾文本/缺文件 → 空骨架不抛。
"""

import time

import src.ops.entrance_slo as slo


def _ts(base: float, offset_sec: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(base + offset_sec))


def _line(base: float, offset_sec: float, level: str, msg: str = "x") -> str:
    return "[%s] [%s] %s" % (_ts(base, offset_sec), level, msg)


NOW = time.mktime(time.strptime("2026-08-12 15:00:00", "%Y-%m-%d %H:%M:%S"))


def test_parse_all_ok():
    text = "\n".join(_line(NOW, -300 * i, "OK") for i in range(10))
    r = slo.parse_edge_log(text, now=NOW)
    assert r["ticks"] == 10 and r["ok_ticks"] == 10
    assert r["availability_pct"] == 100.0
    assert r["outage_count"] == 0 and r["outages"] == []


def test_parse_outage_grouping_and_minutes():
    # OK OK STRIKE STRIKE OK STRIKE OK -> 两段 outage（2 tick=10min, 1 tick=5min）
    seq = ["OK", "OK", "STRIKE", "STRIKE", "OK", "STRIKE", "OK"]
    text = "\n".join(_line(NOW, -300 * (len(seq) - i), lvl) for i, lvl in enumerate(seq))
    r = slo.parse_edge_log(text, now=NOW)
    assert r["ticks"] == 7 and r["ok_ticks"] == 4
    assert r["outage_count"] == 2
    assert [o["minutes"] for o in r["outages"]] == [10.0, 5.0]
    assert r["outage_minutes"] == 15.0
    assert not any(o.get("ongoing") for o in r["outages"])


def test_parse_warn_is_ok_and_actions_ignored():
    seq = [("OK", True), ("WARN", True), ("BOOT", None), ("ALERT", None),
           ("RESTART", None), ("HOLD", False), ("FAILED", False), ("RECOVERED", True)]
    text = "\n".join(_line(NOW, -300 * (len(seq) - i), lvl)
                     for i, (lvl, _) in enumerate(seq))
    r = slo.parse_edge_log(text, now=NOW)
    # 计 tick 的只有 OK/WARN/RECOVERED/HOLD/FAILED = 5 行；BOOT/ALERT/RESTART 不计
    assert r["ticks"] == 5 and r["ok_ticks"] == 3
    assert r["outage_count"] == 1 and r["outages"][0]["bad_ticks"] == 2


def test_parse_window_excludes_old_lines():
    old = _line(NOW, -8 * 86400, "STRIKE")   # 8 天前，窗外
    new = _line(NOW, -600, "OK")
    r = slo.parse_edge_log(old + "\n" + new, now=NOW, window_days=7)
    assert r["ticks"] == 1 and r["ok_ticks"] == 1 and r["outage_count"] == 0


def test_parse_ongoing_outage_flagged():
    text = "\n".join([_line(NOW, -900, "OK"), _line(NOW, -600, "STRIKE"),
                      _line(NOW, -300, "STRIKE")])
    r = slo.parse_edge_log(text, now=NOW)
    assert r["outage_count"] == 1
    assert r["outages"][0].get("ongoing") is True
    assert r["outages"][0]["minutes"] == 10.0


def test_parse_garbage_soft():
    r = slo.parse_edge_log("not a log line\n\x00\xff\n[broken", now=NOW)
    assert r["ticks"] == 0 and r["availability_pct"] is None
    assert r["outages"] == [] and r["outage_minutes"] == 0


def test_snapshot_with_explicit_path(tmp_path):
    p = tmp_path / "edge.log"
    p.write_text("\n".join([_line(NOW, -600, "OK"), _line(NOW, -300, "STRIKE")]),
                 encoding="utf-8")
    snap = slo.entrance_slo_snapshot(edge_log_path=str(p), now=NOW)
    assert snap["server"]["ticks"] == 2 and snap["server"]["ok_ticks"] == 1
    # 趋势库未配置（测试进程默认）→ client 空骨架不抛
    assert isinstance(snap["client"], dict)


def test_snapshot_missing_file_soft(tmp_path):
    snap = slo.entrance_slo_snapshot(edge_log_path=str(tmp_path / "nope.log"), now=NOW)
    assert snap["server"] == {}


def test_client_stats_from_db_readonly(tmp_path):
    """CLI 直读版与 store 写入端 schema 契约一致（store 写 → mode=ro 读）。"""
    from src.web.ui_event_trend import UiEventTrendStore
    db = tmp_path / "ui_event_trend.db"
    st = UiEventTrendStore(db)
    st.add("conn_ep_server", now=NOW - 3600)
    st.add("conn_ep_local", now=NOW - 3600)
    st.add("conn_ep_server", now=NOW - 2 * 86400)
    st.add("conn_dur_lt1m", now=NOW - 3600)
    st.add("iflt_all", now=NOW - 3600)  # 非 conn_ 前缀，必须被过滤
    r = slo.client_conn_stats_from_db(str(db), days=7, now=NOW)
    assert r["episodes"] == 3
    assert r["by_action"] == {"conn_dur_lt1m": 1, "conn_ep_local": 1,
                              "conn_ep_server": 2}
    # 窗口外（8 天前）不计
    st.add("conn_ep_server", now=NOW - 8 * 86400)
    r2 = slo.client_conn_stats_from_db(str(db), days=7, now=NOW)
    assert r2["episodes"] == 3


def test_client_stats_from_db_soft(tmp_path):
    assert slo.client_conn_stats_from_db(str(tmp_path / "nope.db")) == {}
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite", encoding="utf-8")
    assert slo.client_conn_stats_from_db(str(bad)) == {}


def test_rollup_daily_per_day_aggregation():
    text = "\n".join([
        "[2026-08-10 10:00:00] [OK] x",
        "[2026-08-10 10:05:00] [STRIKE] x",
        "[2026-08-10 10:10:00] [BOOT] x",      # 动作行不计
        "[2026-08-11 10:00:00] [OK] x",
    ])
    r = slo.rollup_daily(text)
    assert r["2026-08-10"] == {"ticks": 2, "ok_ticks": 1, "bad_ticks": 1,
                               "outage_minutes": 5.0}
    assert r["2026-08-11"]["ticks"] == 1 and r["2026-08-11"]["bad_ticks"] == 0


def test_ledger_upsert_bigger_wins_and_prune(tmp_path):
    log = tmp_path / "edge.log"
    led = tmp_path / "led.json"
    # 第一次：当天 1 tick（日志还在长）
    log.write_text("[2026-08-12 00:05:00] [OK] x", encoding="utf-8")
    slo.update_rollup_ledger(str(log), str(led), now=NOW)
    # 第二次：同日更全（3 tick）→ 覆盖
    log.write_text("\n".join([
        "[2026-08-12 00:05:00] [OK] x",
        "[2026-08-12 00:10:00] [OK] x",
        "[2026-08-12 00:15:00] [STRIKE] x",
    ]), encoding="utf-8")
    p2 = slo.update_rollup_ledger(str(log), str(led), now=NOW)
    assert p2["days"]["2026-08-12"]["ticks"] == 3
    # 第三次：日志轮转只剩半窗（1 tick）→ 账本旧全量不被半窗顶掉
    log.write_text("[2026-08-12 00:20:00] [OK] x", encoding="utf-8")
    p3 = slo.update_rollup_ledger(str(log), str(led), now=NOW)
    assert p3["days"]["2026-08-12"]["ticks"] == 3
    # 超保留期（61 天前）的旧日被裁剪
    import json
    data = json.loads(led.read_text(encoding="utf-8"))
    data["days"]["2026-06-01"] = {"ticks": 9, "ok_ticks": 9, "bad_ticks": 0,
                                  "outage_minutes": 0}
    led.write_text(json.dumps(data), encoding="utf-8")
    p4 = slo.update_rollup_ledger(str(log), str(led), now=NOW)
    assert "2026-06-01" not in p4["days"]


def test_ledger_summary_window(tmp_path):
    import json
    led = tmp_path / "led.json"
    led.write_text(json.dumps({"days": {
        "2026-08-11": {"ticks": 100, "ok_ticks": 99, "bad_ticks": 1,
                       "outage_minutes": 5.0},
        "2026-08-12": {"ticks": 100, "ok_ticks": 100, "bad_ticks": 0,
                       "outage_minutes": 0.0},
        "2026-06-20": {"ticks": 50, "ok_ticks": 0, "bad_ticks": 50,
                       "outage_minutes": 250.0},   # 30 天窗外，不计
    }}), encoding="utf-8")
    s = slo.ledger_summary(str(led), days=30, now=NOW)
    assert s["days_covered"] == 2 and s["ticks"] == 200
    assert s["availability_pct"] == 99.5 and s["outage_minutes"] == 5.0
    assert slo.ledger_summary(str(tmp_path / "nope.json"), now=NOW) == {}


def test_snapshot_includes_d30(tmp_path):
    import json
    log = tmp_path / "edge.log"
    log.write_text(_line(NOW, -300, "OK"), encoding="utf-8")
    led = tmp_path / "led.json"
    led.write_text(json.dumps({"days": {
        "2026-08-12": {"ticks": 10, "ok_ticks": 9, "bad_ticks": 1,
                       "outage_minutes": 5.0}}}), encoding="utf-8")
    snap = slo.entrance_slo_snapshot(edge_log_path=str(log),
                                     ledger_path=str(led), now=NOW)
    assert snap["d30"]["availability_pct"] == 90.0
    assert snap["d30"]["days_covered"] == 1


def test_snapshot_cache_ttl(monkeypatch, tmp_path):
    p = tmp_path / "edge.log"
    p.write_text(_line(NOW, -300, "OK"), encoding="utf-8")
    monkeypatch.setattr(slo, "DEFAULT_EDGE_LOG", str(p))
    monkeypatch.setattr(slo, "DEFAULT_LEDGER", str(tmp_path / "led.json"))
    slo._reset_cache()
    s1 = slo.entrance_slo_snapshot(now=NOW)
    assert s1["server"]["ticks"] == 1
    # 文件变了但 TTL 内 → 仍回缓存
    p.write_text("\n".join([_line(NOW, -600, "OK"), _line(NOW, -300, "OK")]),
                 encoding="utf-8")
    s2 = slo.entrance_slo_snapshot(now=NOW + 10)
    assert s2["server"]["ticks"] == 1
    # 过 TTL → 重读
    s3 = slo.entrance_slo_snapshot(now=NOW + 301)
    assert s3["server"]["ticks"] == 2
    slo._reset_cache()
