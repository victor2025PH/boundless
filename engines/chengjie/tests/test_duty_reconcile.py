# -*- coding: utf-8 -*-
"""工单↔B案对账（tools/duty_reconcile.py）纯函数门禁（实施81 L1）。

红线：**台账没有已修标记的匹配绝不建议 mark_fixed**——把待开发说成已修复
＝对用户撒谎；「已修」标记出现在否定语境（未解决/待修）同样不算。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.duty_reconcile import (  # noqa: E402
    parse_bcase_rows,
    propose,
    render_proposals,
    similarity,
)

MD = """
| 案 | 一句话 | 状态 |
|---|---|---|
| B114 | 克隆语音绑定人设后生成音色仍是默认（根因 hub 超时，B61 族） | ✅ 已修待发版 |
| B116 | Messenger 不可达联系人弹窗循环=P1-9 残留 | 📋 待开发 |
| B114 | 0827 复报：客户端仍偶现，等下一版 | 观察中 |
| B120 | 语音关了仍发：B线/手动链不吃全局闸，未解决 ✅标记是引用别人的话 | 未解决 |
"""


def test_parse_rows_merge_and_fixed_markers():
    rows = {r["bid"]: r for r in parse_bcase_rows(MD, source="实施68")}
    assert set(rows) == {"B114", "B116", "B120"}
    assert rows["B114"]["fixed"] is True          # 任一行已修标记即真
    assert "0827 复报" in rows["B114"]["text"]     # 多行合并
    assert rows["B116"]["fixed"] is False
    # 否定语境里的 ✅ 不算已修（未解决）
    assert rows["B120"]["fixed"] is False
    assert rows["B114"]["source"] == "实施68"


def test_similarity_discriminates():
    ticket = "克隆语音上传了新的语音绑定人设，生成的音色仍然是默认的"
    good = "克隆音色回落=hub 超时（B61 族）语音绑定人设默认音色"
    bad = "Messenger 不可达联系人弹窗循环"
    assert similarity(ticket, good) > similarity(ticket, bad)
    assert similarity(ticket, good) >= 0.25
    assert similarity("", good) == 0.0


def test_propose_never_marks_fixed_without_marker():
    cases = parse_bcase_rows(MD)
    tickets = [
        {"id": 41, "title": "克隆语音绑定人设音色仍是默认",
         "body": "上传语音显示登记成功但生成音色默认"},
        {"id": 34, "title": "Messenger 不可达联系人弹窗循环重试",
         "body": "退出重新登录后仍弹窗"},
        {"id": 99, "title": "完全无关的新问题 xyzabc", "body": "qqq"},
    ]
    props = {p["ticket_id"]: p for p in propose(tickets, cases, min_score=0.2)}
    assert props[41]["action"] == "mark_fixed" and props[41]["bid"] == "B114"
    assert "B114" in props[41]["suggested_fix_note"]
    assert props[34]["action"] == "follow_up", "台账未标已修 → 只许跟进不许 fixed"
    assert props[99]["action"] == "none"
    txt = render_proposals(list(props.values()))
    assert "建议标 fixed" in txt and '--apply "41=B114"' in txt
    assert "勿标 fixed" in txt and "#99" in txt


def test_render_empty():
    assert "无开放工单" in render_proposals([])


# ── L-7 B（2026-09-06）：报告 ↔ 工单每日对账 ─────────────────────────────────

def _report(diag: Path, code: str, note: str, *, kind: str = "report",
            when: str = "2026-09-05 02:16:00", header: bool = True) -> None:
    d = diag / code
    d.mkdir(parents=True, exist_ok=True)
    name = ("20260905-021600_verify_report.md" if kind == "verify"
            else "selfcheck_report.md" if kind == "selfcheck"
            else "20260905-021600_report_report.md")
    body = (f"# field-agent:skuio:{kind}  (zl_collect v1.3.1)\n"
            f"- time: {when}  fp: DC8F-0935-5EE4-F3D9  app: 1.0.74.0\n"
            + (f"- note: {note}\n" if note else "")) if header else "# 首次自检报告\n- 机器码：B990\n"
    (d / name).write_text(body + "\n## backend.log\n", encoding="utf-8")


def test_reconcile_reports_three_link_faces_and_unlinked_list(tmp_path):
    import json
    import sqlite3
    from tools.duty_reconcile import reconcile_reports, render_reports

    diag = tmp_path / "diag"
    db = tmp_path / "bug_intake.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE bug_tickets (id INTEGER PRIMARY KEY, title TEXT, body TEXT)")
    con.executemany("INSERT INTO bug_tickets VALUES (?,?,?)", [
        (166, "目标不推进", "证据 XJWZRG 报告"),      # ② 正文含码
        (202, "人设备份", ""),
    ])
    con.commit()
    con.close()
    ledger = tmp_path / "duty_evidence.jsonl"
    ledger.write_text(json.dumps({"ticket": 202, "event": "received", "diag": "29BAG6"}) + "\n",
                      encoding="utf-8")                                        # ① 取证台账
    _report(diag, "XJWZRG", "目标仍未执行")
    _report(diag, "29BAG6", "人设备份缺失")
    _report(diag, "T3T3T3", "补证")                                            # ③ ticket.txt
    (diag / "T3T3T3" / "ticket.txt").write_text(
        json.dumps({"code": "T3T3T3", "action": "attach", "ticket": 166}) + "\n", encoding="utf-8")
    _report(diag, "C6EFRS", "【汇总·总表】42 项")                              # summary 不需挂单
    (diag / "C6EFRS" / "ticket.txt").write_text(
        json.dumps({"code": "C6EFRS", "action": "summary", "ticket": 0}) + "\n", encoding="utf-8")
    _report(diag, "TUC9EH", "", kind="verify")                                 # verify 不需挂单
    _report(diag, "ZRYNKN", "", kind="selfcheck", header=False)                # 首装自检：不算报告
    _report(diag, "5NAR3T", "工作目标仍未执行，复查目标引擎开关状态")            # 未挂单
    _report(diag, "OLD001", "09-02 的旧报告", when="2026-09-02 10:00:00")       # since 过滤

    res = reconcile_reports(diag, db, ledger)
    assert res["total"] == 7 and res["by_kind"] == {"report": 6, "verify": 1}
    assert res["linked"] == 5
    assert [u["code"] for u in res["unlinked"]] == ["5NAR3T", "OLD001"]
    assert res["unlinked"][0]["fp4"] == "DC8F" and "工作目标" in res["unlinked"][0]["note"]
    txt = render_reports(res)
    assert "未挂单 2 份" in txt and "5NAR3T" in txt and "duty_auto_ticket.py --code" in txt

    res2 = reconcile_reports(diag, db, ledger, since="20260905")
    assert [u["code"] for u in res2["unlinked"]] == ["5NAR3T"]
    assert res2["total"] == 6

    # 全挂上 → 0 份，文案不带处置行
    (diag / "5NAR3T" / "ticket.txt").write_text(
        json.dumps({"code": "5NAR3T", "action": "attach", "ticket": 166}) + "\n", encoding="utf-8")
    (diag / "OLD001" / "ticket.txt").write_text(
        json.dumps({"code": "OLD001", "action": "new", "ticket": 300}) + "\n", encoding="utf-8")
    res3 = reconcile_reports(diag, db, ledger)
    assert res3["unlinked"] == [] and "未挂单 0 份" in render_reports(res3)
    assert "duty_auto_ticket.py" not in render_reports(res3)


def test_reconcile_reports_missing_db_or_ledger_is_tolerated(tmp_path):
    from tools.duty_reconcile import reconcile_reports
    diag = tmp_path / "diag"
    _report(diag, "AAAAAA", "x")
    res = reconcile_reports(diag, tmp_path / "nope.db", tmp_path / "nope.jsonl")
    assert res["total"] == 1 and [u["code"] for u in res["unlinked"]] == ["AAAAAA"]


def test_daily_task_wrapper_calls_reports_alert():
    ps1 = Path(__file__).resolve().parents[3] / "deploy" / "duty" / "run_duty_reports_reconcile.ps1"
    src = ps1.read_text(encoding="utf-8")
    assert "--reports --alert" in src and "duty_reconcile.py" in src
    assert "DutyReportsReconcile" in src
