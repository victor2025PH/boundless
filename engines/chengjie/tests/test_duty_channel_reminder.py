# -*- coding: utf-8 -*-
"""P-5 B（2026-09-08）：报告回执链的重试上限 / 收件箱幂等 / 空备注不回执（tools/duty_channel_reminder.py）。

09-08 13:47–14:09 实锤：32PTMK 回执 502 十二次（适配器回 delivered=False 无原因），每次在收件箱留一条
failed 行、群里只到 1 条；13:33 selfcheck 自检包 2WD7FY 被当 report 回执了「（无备注）」。
纯函数 + monkeypatch 的 send_group，不碰生产库、不发消息。
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT.parent.parent / "tools"


@pytest.fixture()
def rem(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("duty_channel_reminder", TOOLS / "duty_channel_reminder.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["duty_channel_reminder"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DIAG", tmp_path / "diag")
    monkeypatch.setattr(mod, "WATCH_STATE", tmp_path / "watch.json")
    monkeypatch.setattr(mod, "LOG", tmp_path / "rem.log")
    monkeypatch.setattr(mod, "ALERTS", tmp_path / "alerts.log")
    monkeypatch.setattr(mod, "_auto_ticket", lambda code, dry_run: {"action": "new", "ticket": 300, "is_new": True})
    monkeypatch.setattr(mod, "_receipt_item", lambda code, topic, res: f"{code}（{topic}）→ 已立 #300")
    monkeypatch.setattr(mod, "_order_pending", lambda codes: list(codes))
    monkeypatch.setattr(mod, "_register_skip", lambda code, reason: None)
    return mod


@pytest.fixture()
def no_inbox(rem, monkeypatch):
    """流程测试里收件箱查不到同文本行（单测 already_in_inbox 本体的用例不挂这个）。"""
    monkeypatch.setattr(rem, "already_in_inbox", lambda text, **kw: "")
    return rem


def _pack(diag: Path, code: str, note: str, *, kind="report", fname=None):
    d = diag / code
    d.mkdir(parents=True, exist_ok=True)
    name = fname or ("20260908-133658_report_report.md" if kind == "report" else f"20260908-133658_{kind}_report.md")
    (d / name).write_text(
        f"# field-agent:skuio:{kind}  (zl_collect v1.3.1)\n"
        f"- time: 2026-09-08 13:36:58  fp: DC8F-0935-5EE4-F3D9  app: 1.0.77.0\n"
        + (f"- note: {note}\n" if note else "") + "\n## backend.log\n", encoding="utf-8")


def _watch(mod, codes):
    mod.WATCH_STATE.write_text(json.dumps({"codes": codes}), encoding="utf-8")


def _state():
    return {"event_id": 1, "last_remind": {}, "receipted": [], "receipt_inited": True}


def test_is_empty_note(rem):
    assert rem.is_empty_note("") and rem.is_empty_note("  ") and rem.is_empty_note("（无备注）")
    assert rem.is_empty_note("(无备注)") and rem.is_empty_note("无备注")
    assert not rem.is_empty_note("【BUG】批量挂链弹层看不清")


def test_report_head_kind_from_header_not_filename(rem):
    """selfcheck 自检包（文件名 *_selfcheck_report.md、头 field-agent:skuio:selfcheck）不是 report。"""
    _pack(rem.DIAG, "2WD7FY", "", kind="selfcheck", fname="20260908-133242_selfcheck_report.md")
    assert rem._report_head("2WD7FY")[0] == "selfcheck"
    _pack(rem.DIAG, "ZFFBG6", "", kind="verify", fname="20260908-133500_verify_report.md")
    assert rem._report_head("ZFFBG6")[0] == "verify"
    _pack(rem.DIAG, "MTRCH2", "【1.0.77.0 验收】启动无错")
    kind, fp4, note = rem._report_head("MTRCH2")
    assert (kind, fp4) == ("report", "DC8F") and note.startswith("【1.0.77.0")
    (rem.DIAG / "NOPACK").mkdir()
    assert rem._report_head("NOPACK")[0] == "diag"
    assert rem._report_head("ZZZZZZ") is None


def test_already_in_inbox_only_counts_rows_with_mid(rem, tmp_path):
    db = tmp_path / "inbox.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE messages (message_id TEXT PRIMARY KEY, conversation_id TEXT, platform_msg_id TEXT,"
                " direction TEXT, text TEXT, ts REAL, status TEXT)")
    cid = "telegram:6834964252:-1004345824259"
    now = time.time()
    text = "skuio 花无缺 报告已收到：32PTMK（GXP 条目已清）→ 已挂 #241。不必再在群里贴同一件"
    rows = [
        (f"{cid}:fail:a", cid, "", "out", text, now - 300, "failed"),           # 留痕行：无 mid 不算
        (f"{cid}:fail:b", cid, "", "out", text, now - 120, "failed"),
        (f"{cid}:9", cid, "9", "in", text, now - 60, ""),                       # 入站不算
        (f"{cid}:1300", cid, "1300", "out", text, now - 3000, "read"),          # 超出 10 分钟窗不算
        (f"{cid}:1301", cid, "1301", "out", "别的文本", now - 30, "sent"),
    ]
    con.executemany("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", rows)
    con.commit()
    assert rem.already_in_inbox(text, now=now, db_path=db) == ""
    con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?)", (f"{cid}:1345", cid, "1345", "out", text + " ", now - 10, "read"))
    con.commit()
    assert rem.already_in_inbox(text, now=now, db_path=db) == "1345"
    assert rem.already_in_inbox("", now=now, db_path=db) == ""
    assert rem.already_in_inbox(text, now=now, db_path=tmp_path / "missing.db") == ""     # 查不到 → 按没有


def test_receipt_gives_up_after_three_failures(no_inbox, rem, monkeypatch):
    """模拟 502 三次 → 第四轮不再发，标 receipted，写告警一行。"""
    _pack(rem.DIAG, "32PTMK", "GXP 条目已清(698J28①落地)。剩 8 条均已")
    _watch(rem, ["32PTMK"])
    sent = []
    monkeypatch.setattr(rem, "send_group", lambda text, ticket, dry_run: (sent.append(text), False)[1])
    st = _state()
    for i in range(1, 4):
        rem.check_new_reports(st, dry_run=False)
        assert len(sent) == i and st["receipted"] == [] and st["receipt_attempts"] == {"32PTMK": i}
    rem.check_new_reports(st, dry_run=False)
    assert len(sent) == 3                                    # 第四轮没再发
    assert st["receipted"] == ["32PTMK"] and st["receipt_attempts"] == {}
    alerts = rem.ALERTS.read_text(encoding="utf-8")
    assert "receipt_gave_up" in alerts and "32PTMK" in alerts and "duty_reply.py" in alerts
    # 已 receipted 的不会再进 pending
    rem.check_new_reports(st, dry_run=False)
    assert len(sent) == 3


def test_receipt_success_clears_attempts(no_inbox, rem, monkeypatch):
    _pack(rem.DIAG, "AAAAAA", "第一次失败第二次成功")
    _watch(rem, ["AAAAAA"])
    results = iter([False, True])
    monkeypatch.setattr(rem, "send_group", lambda text, ticket, dry_run: next(results))
    st = _state()
    rem.check_new_reports(st, dry_run=False)
    assert st["receipt_attempts"] == {"AAAAAA": 1}
    rem.check_new_reports(st, dry_run=False)
    assert st["receipted"] == ["AAAAAA"] and st["receipt_attempts"] == {}
    assert not rem.ALERTS.exists()


def test_receipt_already_in_inbox_marks_receipted_without_send(rem, monkeypatch):
    """模拟「收件箱已有同文本带 mid 行」→ 直接标 receipted，不再发。"""
    _pack(rem.DIAG, "32PTMK", "GXP 条目已清")
    _watch(rem, ["32PTMK"])
    sent = []
    monkeypatch.setattr(rem, "send_group", lambda text, ticket, dry_run: (sent.append(text), True)[1])
    monkeypatch.setattr(rem, "already_in_inbox", lambda text, **kw: "1345")
    st = _state()
    st["receipt_attempts"] = {"32PTMK": 2}
    rem.check_new_reports(st, dry_run=False)
    assert sent == [] and st["receipted"] == ["32PTMK"] and st["receipt_attempts"] == {}
    assert "视为已发" in rem.LOG.read_text(encoding="utf-8")


def test_empty_note_and_selfcheck_packs_are_not_receipted(no_inbox, rem, monkeypatch):
    """空备注 report 包 / selfcheck 包 → 无回执、不调挂单链，但进 receipted 不再重看。"""
    _pack(rem.DIAG, "2WD7FY", "", kind="selfcheck", fname="20260908-133242_selfcheck_report.md")
    _pack(rem.DIAG, "E1E1E1", "")
    _pack(rem.DIAG, "E2E2E2", "（无备注）")
    _pack(rem.DIAG, "OKOKOK", "【BUG】真报告")
    _watch(rem, ["2WD7FY", "E1E1E1", "E2E2E2", "OKOKOK"])
    sent = []
    ticketed = []
    monkeypatch.setattr(rem, "send_group", lambda text, ticket, dry_run: (sent.append(text), True)[1])
    monkeypatch.setattr(rem, "_auto_ticket", lambda code, dry_run: (ticketed.append(code), {"action": "new", "ticket": 301})[1])
    st = _state()
    rem.check_new_reports(st, dry_run=False)
    assert len(sent) == 1 and "OKOKOK" in sent[0] and "（无备注）" not in sent[0]
    assert ticketed == ["OKOKOK"]
    assert sorted(st["receipted"]) == ["2WD7FY", "E1E1E1", "E2E2E2", "OKOKOK"]
    logtxt = rem.LOG.read_text(encoding="utf-8")
    assert "2WD7FY kind=selfcheck" in logtxt and "E1E1E1 kind=report 空备注" in logtxt


def test_source_keeps_l7_and_n5_wiring():
    src = (TOOLS / "duty_channel_reminder.py").read_text(encoding="utf-8")
    assert "RECEIPT_MAX_ATTEMPTS = 3" in src and "receipt_gave_up" in src
    assert "already_in_inbox(text)" in src and "is_empty_note(note)" in src
    assert 'st["receipt_attempts"]' in src
    body = src.split("def check_new_reports", 1)[1]
    assert "if send_group(text, 0, dry_run=dry_run):" in body and "下一轮重试" in body
    assert "不必再在群里贴同一件" not in src
    assert "进展会按单回访" not in src
    assert 'DEFAULT_RECEIPT = "{name} 报告已收到：\\n{items}"' in src
    assert '"\\n".join(items)' in src


def test_default_receipt_omits_process_lecture(rem):
    assert "不必再在群里贴同一件" not in rem.DEFAULT_RECEIPT
    assert "验收清单" not in rem.DEFAULT_RECEIPT
    assert "进展会按单回访" not in rem.DEFAULT_RECEIPT
    assert "{name}" in rem.DEFAULT_RECEIPT and "{items}" in rem.DEFAULT_RECEIPT
    assert "{detail}" in rem.DEFAULT_REMIND


def test_receipt_text_has_no_process_lecture(no_inbox, rem, monkeypatch):
    _pack(rem.DIAG, "32PTMK", "GXP 条目已清")
    _watch(rem, ["32PTMK"])
    sent = []
    monkeypatch.setattr(rem, "send_group", lambda text, ticket, dry_run: (sent.append(text), True)[1])
    rem.check_new_reports(_state(), dry_run=False)
    assert sent
    assert sent[0].startswith("skuio 花无缺 报告已收到：\n")
    assert "32PTMK（GXP 条目已清）" in sent[0]
    assert "不必再在群里贴同一件" not in sent[0]
    assert "验收清单" not in sent[0]
    assert "进展会按单回访" not in sent[0]


def test_receipt_includes_problem_core_and_fix_method(tmp_path, monkeypatch):
    """真 _receipt_item：括号里是问题核心，工单有修复说明则另起一行。"""
    spec = importlib.util.spec_from_file_location(
        "duty_channel_reminder_live", TOOLS / "duty_channel_reminder.py")
    rem = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rem)
    rem.DIAG = tmp_path / "diag"
    rem.WATCH_STATE = tmp_path / "watch.json"
    rem.LOG = tmp_path / "rem.log"
    rem.ALERTS = tmp_path / "alerts.log"
    monkeypatch.setattr(rem, "_auto_ticket",
                        lambda code, dry_run: {"action": "attach", "ticket": 241})
    monkeypatch.setattr(
        rem, "_ticket_public",
        lambda tid: {"title": "GXP 条目已清", "fix_note": "1.0.80 内测包已含，手装后重启"})
    monkeypatch.setattr(rem, "_order_pending", lambda codes: list(codes))
    monkeypatch.setattr(rem, "_register_skip", lambda code, reason: None)
    monkeypatch.setattr(rem, "already_in_inbox", lambda text, **kw: "")
    sent = []
    monkeypatch.setattr(rem, "send_group",
                        lambda text, ticket, dry_run: (sent.append(text), True)[1])
    _pack(rem.DIAG, "32PTMK", "【BUG】关联 #241")
    _watch(rem, ["32PTMK"])
    rem.check_new_reports(_state(), dry_run=False)
    assert sent
    assert "32PTMK（GXP 条目已清）→ 已挂 #241" in sent[0]
    assert "修复：1.0.80 内测包已含，手装后重启" in sent[0]
    assert "不必再在群里贴同一件" not in sent[0]
