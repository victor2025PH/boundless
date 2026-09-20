"""log_triage 纯核心门禁：归一化 / 解析 / 分组聚合。"""

from scripts.log_triage import normalize, parse_lines, triage


def test_normalize_masks_volatile_parts():
    assert normalize("端点 http://192.168.0.140:7852 合成失败") == \
        "端点 http://<ip>:<n> 合成失败"
    assert normalize("request_id=r-580a4db78707 failed") == \
        "request_id=r-<hex> failed"
    # 纯数字与 hex 都抹掉 → 同类消息折叠成同一模板
    a = normalize("cycle=125 scanned=30")
    b = normalize("cycle=999 scanned=1")
    assert a == b == "cycle=<n> scanned=<n>"


def test_normalize_truncates():
    long = "x" * 300
    out = normalize(long, width=50)
    assert len(out) == 51 and out.endswith("…")


def test_parse_skips_continuation_lines():
    lines = [
        "[2026-07-22 22:00:00] [WARNING] mod.a: hello 1",
        "    Traceback continuation without prefix",
        "[2026-07-22 22:01:00] [ERROR] mod.b: boom 2",
    ]
    recs = parse_lines(lines)
    assert len(recs) == 2
    assert recs[0].level == "WARNING" and recs[0].logger == "mod.a"
    assert recs[1].level == "ERROR" and recs[1].logger == "mod.b"


def _mk(ts, level, logger, msg):
    from scripts.log_triage import Record
    return Record(ts, level, logger, msg)


def test_triage_groups_and_counts():
    recs = [
        _mk("2026-07-22 10:00:00", "WARNING", "net", "timeout id=aaa111"),
        _mk("2026-07-22 11:00:00", "WARNING", "net", "timeout id=bbb222"),
        _mk("2026-07-23 09:00:00", "WARNING", "net", "timeout id=ccc333"),
        _mk("2026-07-23 09:05:00", "ERROR", "db", "no such column"),
    ]
    groups = triage(recs)  # today = 最新日期 07-23
    top = groups[0]
    assert top.logger == "net" and top.count == 3
    assert top.first_ts == "2026-07-22 10:00:00"
    assert top.last_ts == "2026-07-23 09:00:00"
    assert top.today_count == 1  # 仅 07-23 那条


def test_triage_level_and_grep_filters():
    recs = [
        _mk("2026-07-23 09:00:00", "WARNING", "a", "keep me"),
        _mk("2026-07-23 09:01:00", "ERROR", "a", "drop me"),
    ]
    only_warn = triage(recs, levels=["WARNING"])
    assert len(only_warn) == 1 and only_warn[0].level == "WARNING"

    grepped = triage(recs, grep="drop")
    assert len(grepped) == 1 and "drop" in grepped[0].sample


def test_triage_since_filter():
    recs = [
        _mk("2026-07-23 08:00:00", "WARNING", "a", "old"),
        _mk("2026-07-23 10:00:00", "WARNING", "a", "new"),
    ]
    out = triage(recs, since="2026-07-23 09:00")
    assert sum(g.count for g in out) == 1
