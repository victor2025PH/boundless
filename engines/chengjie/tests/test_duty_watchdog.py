# -*- coding: utf-8 -*-
"""值守缺位看门狗（tools/duty_watchdog.py）纯函数门禁（实施81 P0-4）。

重点覆盖**不该告警**的路径（安全网误报＝老板半夜被吵，比漏报先失去信任）：
支持号已出站应答 / 员工亲号入站应答 / 提问未超龄 / 空壳行 / 员工自己发的
消息不算提问；以及该告警的：超龄未应答、媒体消息（截图报障常无文字）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_watchdog import (  # noqa: E402
    DEFAULT_EVENT_KINDS,
    alert_decision,
    build_alert_text,
    events_as_messages,
    find_unanswered,
    prune_state,
)

NOW = time.time()


def _msg(direction, ts, *, sender="500", name="客户A", text="消息发不出去",
         media="", mid=""):
    return {"direction": direction, "ts": ts, "sender_id": sender,
            "sender_name": name, "text": text, "media_type": media,
            "message_id": mid or f"m{int(ts)}"}


def test_unanswered_old_question_hits():
    msgs = [_msg("in", NOW - 3600)]
    hit = find_unanswered(msgs, now=NOW, threshold_min=30, staff_ids={"777"})
    assert hit and hit["age_min"] >= 59


def test_young_question_not_yet():
    msgs = [_msg("in", NOW - 300)]
    assert find_unanswered(msgs, now=NOW, threshold_min=30,
                           staff_ids=set()) is None


def test_outbound_after_question_clears():
    msgs = [_msg("in", NOW - 3600), _msg("out", NOW - 3500, sender="")]
    assert find_unanswered(msgs, now=NOW, threshold_min=30,
                           staff_ids=set()) is None


def test_staff_inbound_counts_as_answer():
    # 老板/值守用自己账号在群里答复：从读取账号视角是 in，但必须算已应答
    msgs = [_msg("in", NOW - 3600),
            _msg("in", NOW - 3500, sender="777", name="值守")]
    assert find_unanswered(msgs, now=NOW, threshold_min=30,
                           staff_ids={"777"}) is None


def test_staff_own_message_is_not_a_question():
    msgs = [_msg("in", NOW - 3600, sender="777", name="值守")]
    assert find_unanswered(msgs, now=NOW, threshold_min=30,
                           staff_ids={"777"}) is None


def test_new_question_after_answer_hits_again():
    msgs = [_msg("in", NOW - 7200), _msg("out", NOW - 7000, sender=""),
            _msg("in", NOW - 3600, text="还是不行")]
    hit = find_unanswered(msgs, now=NOW, threshold_min=30, staff_ids=set())
    assert hit and hit["text"] == "还是不行"


def test_media_only_question_counts():
    # 截图报障常无文字——不算提问＝最常见形态被漏
    msgs = [_msg("in", NOW - 3600, text="", media="photo")]
    hit = find_unanswered(msgs, now=NOW, threshold_min=30, staff_ids=set())
    assert hit and hit["media_type"] == "photo"


def test_empty_shell_rows_ignored():
    msgs = [_msg("in", NOW - 3600, text="", media="")]
    assert find_unanswered(msgs, now=NOW, threshold_min=30,
                           staff_ids=set()) is None


def test_order_independent():
    # thread API 顺序不保证——乱序输入同一结论
    msgs = [_msg("out", NOW - 3500, sender=""), _msg("in", NOW - 3600)]
    assert find_unanswered(msgs, now=NOW, threshold_min=30,
                           staff_ids=set()) is None


def test_prune_state_ttl():
    st = {"a": NOW - 47 * 3600, "b": NOW - 49 * 3600}
    kept = prune_state(st, NOW)
    assert "a" in kept and "b" not in kept


def test_alert_text_shape():
    hit = dict(_msg("in", NOW - 3600), age_min=61.0,
               chat_key="-1004345824259")
    txt = build_alert_text([hit], now=NOW)
    assert "内测bug群" in txt and "61.0" in txt
    assert "duty_reply.py" in txt, "告警必须带处置指路（不能只报问题不给出路）"


def test_alert_decision_first_repeat_and_off():
    # 首见 → first
    assert alert_decision({}, "g:1", NOW, 240) == "first"
    # 报过、未到重提点 → 静默
    assert alert_decision({"g:1": NOW - 100 * 60}, "g:1", NOW, 240) == ""
    # 报过、超重提间隔 → repeat（升级重提）
    assert alert_decision({"g:1": NOW - 241 * 60}, "g:1", NOW, 240) == "repeat"
    # re_alert_min=0 ＝关闭重提（旧行为：报过即永久静默到 TTL）
    assert alert_decision({"g:1": NOW - 999 * 60}, "g:1", NOW, 0) == ""


def test_repeat_alert_text_marked():
    hit = dict(_msg("in", NOW - 5 * 3600), age_min=300.0,
               chat_key="-1004345824259", re_alert=True)
    txt = build_alert_text([hit], now=NOW)
    assert "🔁 持续未应答" in txt, "重提必须与首报视觉区分（老板要知道这是同一条在恶化）"


# ── C2（2026-09-02 两踩实锤）：限流静默的真反馈/提问纳入扫描面 ────────────────

def _ev(kind, ts, *, eid=1, chat="-1004345824259", rid="500", detail="语音发不出去"):
    return {"id": eid, "ts": ts, "chat_id": chat, "kind": kind,
            "reporter_id": rid, "detail": detail}


def test_default_event_kinds_cover_capped_and_usage():
    assert set(DEFAULT_EVENT_KINDS.split(",")) == {"rate_capped_report", "usage"}


def test_rate_capped_report_event_surfaces_as_unanswered_question():
    """skuio 四连报形态：镜像里没这几条（被限频压制没走 handler 主链），
    只有台账 rate_capped_report 事件——必须能告警。"""
    rows = events_as_messages([_ev("rate_capped_report", NOW - 3600)],
                              chat_key="-1004345824259", staff_ids={"777"})
    assert len(rows) == 1
    hit = find_unanswered(rows, now=NOW, threshold_min=30, staff_ids={"777"})
    assert hit and hit["kind"] == "rate_capped_report"
    assert hit["message_id"] == "ev:1"                 # 与镜像 mid 不撞键
    txt = build_alert_text([dict(hit, chat_key="-1004345824259")], now=NOW)
    assert "限流静默" in txt, "值守必须知道这条是被 bot 限流吞掉的（未回执/未立单）"
    assert "语音发不出去" in txt


def test_event_cleared_by_later_staff_reply_in_thread():
    rows = events_as_messages([_ev("usage", NOW - 3600, detail="怎么切全自动？")],
                              chat_key="-1004345824259", staff_ids=set())
    thread = [_msg("out", NOW - 3500, sender="")]
    assert find_unanswered(thread + rows, now=NOW, threshold_min=30,
                           staff_ids=set()) is None


def test_events_filtered_by_group_and_staff_and_named_from_thread():
    thread = [_msg("in", NOW - 7200, sender="500", name="skuio")]
    evs = [
        _ev("rate_capped_report", NOW - 3600, eid=1, rid="500"),
        _ev("usage", NOW - 3500, eid=2, chat="-1004290740529"),   # 别的群
        _ev("usage", NOW - 3400, eid=3, rid="777"),                # 支持号自己
    ]
    rows = events_as_messages(evs, chat_key="-1004345824259",
                              thread_msgs=thread, staff_ids={"777"})
    assert [r["message_id"] for r in rows] == ["ev:1"]
    assert rows[0]["sender_name"] == "skuio"          # 名字从镜像同 sender 借


def test_events_do_not_double_alert_same_key_across_runs():
    hit_key = "-1004345824259:ev:1"
    assert alert_decision({}, hit_key, NOW, 240) == "first"
    assert alert_decision({hit_key: NOW - 60}, hit_key, NOW, 240) == ""


def test_watchdog_reads_events_sqlite_fallback(tmp_path):
    """旧引擎无 /events 端点 → 本机只读打开台账。"""
    import sqlite3
    from tools.duty_watchdog import _read_events_sqlite
    (tmp_path / "config").mkdir()
    con = sqlite3.connect(str(tmp_path / "config" / "bug_intake.db"))
    con.execute("CREATE TABLE bug_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " ts REAL, chat_id TEXT, kind TEXT, reporter_id TEXT, detail TEXT)")
    con.execute("INSERT INTO bug_events (ts, chat_id, kind, reporter_id, detail)"
                " VALUES (?,?,?,?,?)", (NOW - 100, "-1", "rate_capped_report", "5", "x"))
    con.execute("INSERT INTO bug_events (ts, chat_id, kind, reporter_id, detail)"
                " VALUES (?,?,?,?,?)", (NOW - 100, "-1", "bug_new", "5", "y"))
    con.commit()
    con.close()
    rows = _read_events_sqlite(tmp_path, ["rate_capped_report", "usage"], NOW - 1000)
    assert [r["kind"] for r in rows] == ["rate_capped_report"]
    assert _read_events_sqlite(tmp_path / "nope", ["usage"], 0) == []


def test_main_dry_run_alerts_on_capped_event_only_in_ledger(tmp_path, monkeypatch, capsys):
    """端到端（HTTP 打桩）：镜像线程里没有那条被限流的提问，只有台账事件 →
    看门狗仍要在 dry-run 输出里报出来并带「限流静默」标。"""
    import tools.duty_watchdog as wd

    def fake_get_json(base, path, token, params, timeout=30):
        if path == "/api/unified-inbox/thread":
            return {"messages": [_msg("in", NOW - 9000, sender="500", name="skuio",
                                      text="老问题", mid="m1"),
                                 _msg("out", NOW - 8900, sender="", mid="m2")]}
        if path == "/api/admin/bug-intake/events":
            assert params["kinds"] == "rate_capped_report,usage"
            return {"ok": True, "events": [
                _ev("rate_capped_report", NOW - 3600, eid=9, rid="500",
                    detail="语音还是发不出去，第四次了")]}
        raise AssertionError(path)

    monkeypatch.setattr(wd, "_get_json", fake_get_json)
    monkeypatch.setattr(wd, "resolve_data_roots", lambda v: [tmp_path])
    monkeypatch.setattr(wd, "load_merged_config", lambda root: {
        "bug_intake": {"groups": ["-1004345824259"], "support_accounts": ["777"]}})
    monkeypatch.setattr(wd, "_read_token", lambda root: "tok")
    monkeypatch.setattr(wd, "_deliver", lambda text: (_ for _ in ()).throw(
        AssertionError("dry-run 不得投递")))
    monkeypatch.setattr(sys, "argv", [
        "duty_watchdog", "--dry-run", "--state", str(tmp_path / "st.json")])
    assert wd.main() == 0
    out = capsys.readouterr().out
    assert "并入台账事件 1 条" in out
    assert "限流静默" in out and "第四次了" in out
    assert "skuio" in out, "发言人名应从镜像同 sender 借到"
    assert "[dry-run]" in out


def test_alert_recipient_is_boss():
    """内部告警收件人＝@ai_zkw（老板 0829 拍板）——单点在 duty_alert.py，
    两个消费方必须走它（谁绕开单点直连 monitor 的 deliver，收件人就会
    静默漂回 katie 信箱）。"""
    from tools import duty_alert
    assert duty_alert.DUTY_ALERT_TG_ID == "5433982810"   # @ai_zkw
    assert duty_alert.FALLBACK_ACCOUNT == "6834964252"   # 支持号回落发件人
    root = Path(__file__).resolve().parents[1]
    for rel in ("tools/duty_watchdog.py", "tools/duty_sla_report.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "from tools.duty_alert import deliver" in src, (
            f"{rel} 必须走 duty_alert 统一出口")
        assert "mod.deliver(" not in src, f"{rel} 不得直连 monitor 的 deliver"
