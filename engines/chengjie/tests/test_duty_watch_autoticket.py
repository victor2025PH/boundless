# -*- coding: utf-8 -*-
"""L-7 A（2026-09-06）：Cursor 报告到达自动挂单 / 立单的判定门禁（tools/duty_auto_ticket.py）。

三类 note → 挂单（#N / 关联码 / 30 分钟同主题）· 新立单 · verify 只记不立；
【汇总】总表不立单；回执文案把单号接进「收到 <码>」那一截。纯判定，不碰生产库、不发消息。
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
TOOL = ROOT.parent.parent / "tools" / "duty_auto_ticket.py"


@pytest.fixture(scope="module")
def dat():
    spec = importlib.util.spec_from_file_location("duty_auto_ticket", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["duty_auto_ticket"] = mod
    spec.loader.exec_module(mod)
    return mod


def _mk_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.execute(
        "CREATE TABLE bug_tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts REAL,"
        " updated_ts REAL, chat_id TEXT DEFAULT '', account_id TEXT DEFAULT '',"
        " reporter_id TEXT DEFAULT '', reporter_name TEXT DEFAULT '', title TEXT DEFAULT '',"
        " body TEXT DEFAULT '', status TEXT DEFAULT 'new', dup_of INTEGER DEFAULT 0,"
        " reporter_version TEXT DEFAULT '')")
    con.commit()
    return con


def _ticket(con, tid, title, *, reporter="8942577244", ts=None, status="new", dup_of=0, body=""):
    con.execute(
        "INSERT INTO bug_tickets (id, created_ts, updated_ts, chat_id, reporter_id, title, body,"
        " status, dup_of) VALUES (?,?,?,?,?,?,?,?,?)",
        (tid, ts or time.time(), ts or time.time(), "-1004345824259", reporter, title, body,
         status, dup_of))
    con.commit()


def _write_report(diag: Path, code: str, note: str, *, kind="report", when="2026-09-06 04:17:07",
                  app="1.0.74.0", fp="DC8F-0935-5EE4-F3D9"):
    d = diag / code
    d.mkdir(parents=True, exist_ok=True)
    name = f"20260906-041707_{'verify_' if kind == 'verify' else ''}report.md"
    if kind == "report":
        name = "20260906-041707_report_report.md"
    (d / name).write_text(
        f"# field-agent:skuio:{kind}  (zl_collect v1.3.1)\n"
        f"- time: {when}  fp: {fp}  app: {app}\n"
        + (f"- note: {note}\n" if note else "")
        + "\n## backend.log\n[x] y\n", encoding="utf-8")
    return d


def _head(dat, diag, code):
    h = dat.parse_report_head(code, diag)
    assert h is not None
    return h


# ── 解析 ─────────────────────────────────────────────────────────────────────

def test_parse_head_fields_and_kinds(dat, tmp_path):
    diag = tmp_path / "diag"
    _write_report(diag, "AAAAAA", "【复现·未修复】人设备份按钮仍缺失 关联 29BAG6")
    h = _head(dat, diag, "AAAAAA")
    assert h["kind"] == "report" and h["fp4"] == "DC8F" and h["app"] == "1.0.74.0"
    assert h["time"] == "2026-09-06 04:17:07" and h["ts"] > 0
    assert h["note"].startswith("【复现")
    _write_report(diag, "BBBBBB", "", kind="verify")
    assert _head(dat, diag, "BBBBBB")["kind"] == "verify"
    (diag / "CCCCCC").mkdir()
    assert _head(dat, diag, "CCCCCC")["kind"] == "diag"
    assert dat.parse_report_head("ZZZZZZ", diag) is None       # 还没解包完


def test_title_from_note_strips_tag_and_caps_80(dat):
    t = dat.title_from_note("【BUG】跟进SOP管理页‘批量挂链’按钮点击无反应：右侧均点了无弹窗。附：其他")
    assert t.startswith("跟进SOP管理页") and "【" not in t and len(t) <= 80
    long = "人设工作室底部仍为导出导入 " * 12
    assert len(dat.title_from_note(long)) <= 80
    # 首句太短并上第二句
    assert "：" in dat.title_from_note("复验。批量挂链弹层文字灰度过低几乎看不清")
    # 0906 12:47 实锤（#214）：英文引句里的 ? 不是句尾——不能把标题切成「…‘kim」
    t2 = dat.title_from_note("主动关怀未发送：Kxhm(telegram)‘kim? are you at cebu now?’到点 12:43:41 显示‘到点了，下轮巡检就处理’；skuio 13:39:52 待发")
    assert "are you at cebu now" in t2 and "下轮巡检就处理" in t2      # 切在全角「；」，不切在引句内的 ?
    assert t2.startswith("主动关怀未发送") and len(t2) <= 80 and "待发" not in t2
    # ASCII 句号/问号后接中文才切
    assert dat.title_from_note("Sceya said hi. 然后 AI 编了天气") == "Sceya said hi"


# ── 判定：三类 note ─────────────────────────────────────────────────────────

def test_decide_attach_by_ticket_ref_follows_dup_chain(dat, tmp_path):
    con = _mk_db(tmp_path / "b.db")
    _ticket(con, 173, "批量挂链按钮无反应")
    _ticket(con, 209, "#173 复测：弹层看不清", dup_of=173)
    diag = tmp_path / "diag"
    _write_report(diag, "R1R1R1", "复验 #209：1.0.74 下弹层文字仍灰")
    dec = dat.decide(_head(dat, diag, "R1R1R1"), con, [], {"R1R1R1"}, diag)
    assert dec["action"] == "attach" and dec["ticket"] == 173     # dup_of 跟到主单
    # 不存在的 #N 不算命中 → 新单
    _write_report(diag, "R2R2R2", "复验 #9999：仍未修")
    dec2 = dat.decide(_head(dat, diag, "R2R2R2"), con, [], {"R1R1R1", "R2R2R2"}, diag)
    assert dec2["action"] == "new"


def test_decide_attach_by_known_code_via_ledger_body_or_ticket_txt(dat, tmp_path):
    con = _mk_db(tmp_path / "b.db")
    _ticket(con, 202, "人设备份与迁移缺失")
    _ticket(con, 166, "目标不推进", body="证据 XJWZRG 报告", status="closed")
    _ticket(con, 300, "目标不推进 复报", body="又见 XJWZRG")
    diag = tmp_path / "diag"
    for c in ("29BAG6", "XJWZRG", "T3T3T3"):
        _write_report(diag, c, "旧报告")
    ledger = [{"ticket": 202, "event": "received", "diag": "29BAG6"}]
    _write_report(diag, "N1N1N1", "【复现·未修复】29BAG6 在 1.0.74 复验：底部仍无备份按钮")
    codes = {"29BAG6", "XJWZRG", "T3T3T3", "N1N1N1", "N2N2N2", "N3N3N3"}
    dec = dat.decide(_head(dat, diag, "N1N1N1"), con, ledger, codes, diag)
    assert dec == {"action": "attach", "ticket": 202, "reason": "关联报告 29BAG6 → #202",
                   "refs": [202], "via_code": "29BAG6"}
    # 工单正文里的码：两张单都含 XJWZRG → 未关闭的优先
    _write_report(diag, "N2N2N2", "复验 XJWZRG：目标仍未推进")
    dec2 = dat.decide(_head(dat, diag, "N2N2N2"), con, ledger, codes, diag)
    assert dec2["action"] == "attach" and dec2["ticket"] == 300
    # ticket.txt 里的归属也算反查面
    dat.write_ticket_txt("T3T3T3", {"code": "T3T3T3", "action": "new", "ticket": 202}, diag)
    _write_report(diag, "N3N3N3", "补证 T3T3T3 再传一份日志")
    dec3 = dat.decide(_head(dat, diag, "N3N3N3"), con, ledger, codes, diag)
    assert dec3["action"] == "attach" and dec3["ticket"] == 202


def test_decide_multiple_codes_prefers_open_then_newest(dat, tmp_path):
    """0906 12:49 实锤：9YYD44 写「关联 ZQ4ASK / 9K6G7W / M2SHYA」，首码 ZQ4ASK 指向已修的 #182，
    M2SHYA 是 2 分钟前刚立的开放单 #214——该挂 #214。"""
    con = _mk_db(tmp_path / "b.db")
    _ticket(con, 182, "主动关怀意图反转", status="fixed")
    _ticket(con, 214, "主动关怀未发送")
    diag = tmp_path / "diag"
    for c in ("ZQ4ASK", "M2SHYA"):
        _write_report(diag, c, "旧")
    ledger = [{"ticket": 182, "event": "received", "diag": "ZQ4ASK"},
              {"ticket": 214, "event": "received", "diag": "M2SHYA"}]
    _write_report(diag, "9YYD44", "【BUG·主动关怀不发送】根因：dry_run=True … 关联 ZQ4ASK / 9K6G7W / M2SHYA")
    dec = dat.decide(_head(dat, diag, "9YYD44"), con, ledger, {"ZQ4ASK", "M2SHYA", "9YYD44"}, diag)
    assert dec["action"] == "attach" and dec["ticket"] == 214 and dec["via_code"] == "M2SHYA"
    # 全是已修单 → 取 id 最大的那张
    con.execute("UPDATE bug_tickets SET status='fixed' WHERE id=214")
    con.commit()
    dec2 = dat.decide(_head(dat, diag, "9YYD44"), con, ledger, {"ZQ4ASK", "M2SHYA", "9YYD44"}, diag)
    assert dec2["ticket"] == 214


def test_decide_attach_same_reporter_recent_topic_else_new(dat, tmp_path):
    con = _mk_db(tmp_path / "b.db")
    diag = tmp_path / "diag"
    _write_report(diag, "S1S1S1", "学习队列寒暄条目也入队，166 待审全部通过无确认")
    h = _head(dat, diag, "S1S1S1")
    # 同报障人 10 分钟前群里立的单，主题重叠 → 挂
    _ticket(con, 201, "学习队列：寒暄/系统占位文本也入队（166 待审）、全部通过一键无二次确认",
            ts=h["ts"] - 600)
    # 别人的同主题单 / 同人但 2 小时前的单 都不算
    _ticket(con, 202, "学习队列寒暄条目入队 待审 166", reporter="8852939166", ts=h["ts"] - 60)
    _ticket(con, 203, "学习队列寒暄条目入队 待审 166 全部通过", ts=h["ts"] - 7200)
    dec = dat.decide(h, con, [], {"S1S1S1"}, diag)
    assert dec["action"] == "attach" and dec["ticket"] == 201
    # 无重叠 → 新单
    _write_report(diag, "S2S2S2", "WhatsApp 发视频 60MB 上传超时失败")
    dec2 = dat.decide(_head(dat, diag, "S2S2S2"), con, [], {"S1S1S1", "S2S2S2"}, diag)
    assert dec2["action"] == "new" and dec2["ticket"] == 0


def test_decide_verify_and_summary_never_create(dat, tmp_path):
    con = _mk_db(tmp_path / "b.db")
    _ticket(con, 188, "手动发送双显")
    diag = tmp_path / "diag"
    _write_report(diag, "V1V1V1", "", kind="verify")
    d1 = dat.decide(_head(dat, diag, "V1V1V1"), con, [], {"V1V1V1"}, diag)
    assert d1["action"] == "verify" and d1["ticket"] == 0
    _write_report(diag, "V2V2V2", "#188 复验通过", kind="verify")
    d2 = dat.decide(_head(dat, diag, "V2V2V2"), con, [], {"V2V2V2"}, diag)
    assert d2["action"] == "verify" and d2["ticket"] == 188
    _write_report(diag, "M1M1M1", "【汇总·问题跟踪总表 v1.0.74 复验】42 项 …")
    d3 = dat.decide(_head(dat, diag, "M1M1M1"), con, [], {"M1M1M1"}, diag)
    assert d3["action"] == "summary" and d3["ticket"] == 0
    (diag / "D1D1D1").mkdir()
    assert dat.decide(_head(dat, diag, "D1D1D1"), con, [], set(), diag)["action"] == "skip"


# ── 回执文案 / ticket.txt ───────────────────────────────────────────────────

def test_receipt_item_carries_ticket_number(dat):
    assert dat.receipt_item("E42974", "人设备份", {"action": "attach", "ticket": 202}) == \
        "E42974（人设备份）→ 已挂 #202"
    assert dat.receipt_item("KJ6BSF", "SOP 链", {"action": "new", "ticket": 214, "is_new": True}) == \
        "KJ6BSF（SOP 链）→ 已立 #214"
    assert dat.receipt_item("C6EFRS", "总表", {"action": "summary", "ticket": 0}) == \
        "C6EFRS（总表）→ 总表，值守人工逐项对"
    assert dat.receipt_item("TUC9EH", "x", {"action": "verify", "ticket": 188}) == \
        "TUC9EH（x）→ 复验记录已挂 #188"
    # 工具失败 → 回落成无单号回执（绝不丢回执）
    assert dat.receipt_item("XXXXXX", "主题", None) == "XXXXXX（主题）"


def test_ticket_txt_roundtrip_last_line_wins(dat, tmp_path):
    diag = tmp_path / "diag"
    assert dat.read_ticket_txt("Q1Q1Q1", diag) is None
    dat.write_ticket_txt("Q1Q1Q1", {"code": "Q1Q1Q1", "action": "new", "ticket": 5}, diag)
    dat.write_ticket_txt("Q1Q1Q1", {"code": "Q1Q1Q1", "action": "attach", "ticket": 7}, diag)
    got = dat.read_ticket_txt("Q1Q1Q1", diag)
    assert got["ticket"] == 7 and got["action"] == "attach"
    raw = (diag / "Q1Q1Q1" / "ticket.txt").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 2 and json.loads(raw[0])["ticket"] == 5


def test_reminder_wires_auto_ticket_into_receipt():
    """duty_channel_reminder 的回执链必须调 auto_ticket 并用 receipt_item 拼单号；
    verify 无 #N 仍不回执。"""
    src = (TOOL.parent / "duty_channel_reminder.py").read_text(encoding="utf-8")
    assert "_auto_ticket(code, dry_run=dry_run)" in src
    assert "_receipt_item(code, topic, res)" in src
    assert 'kind == "verify" and not (res and res.get("ticket"))' in src
    assert "不自动立单" not in src.split('"""', 2)[1]   # 模块 docstring 不再声称不立单
    # 0906 12:49：回执发失败（409 近重复）不得算已回执——发成功才 receipted.update，否则下一轮重试
    body = src.split("def check_new_reports", 1)[1]
    assert "if send_group(text, 0, dry_run=dry_run):" in body
    assert "receipted.update(owner_codes.get(owner, []))" in body
    assert "下一轮重试" in body
