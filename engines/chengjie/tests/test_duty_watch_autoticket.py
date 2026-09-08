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


MTRCH2_NOTE = (
    "【1.0.77.0 验收·启动日志对照】13:32:37 clean 安装启动 8.3s 无错。已落地：CompanionDomainHook+线上陪伴"
    "(3NVM7Q/TN736F/8FJDUK)；AutosendWorker CJK 语言硬闸+出站收口翻译器#106(8DVDVC)；deferred_outbox drain loop "
    "常驻(FHSZTA④)。新问题：①FateX 幻缘 产品库 fatex.db 随用户版打包；②Web 监听 0.0.0.0 全网卡开放；"
    "③clean 包缺 handoff_scripts.yaml；④‘首启体验档 100000 字符/0.0 小时窗口(剩 None 小时)’文案 bug。关联 ZFFBG6 / 9K8YJA"
)


def test_ticket_ref_only_in_attach_context(dat):
    """P-5 A：#N 紧贴中文 / 字母词尾（翻译器#106、器#106、abc#12）不算挂单意图；
    行首 / 空白 / 标点 / 前缀词后的 #N 才算。"""
    assert dat._TICKET_RE.findall(MTRCH2_NOTE) == []
    assert dat._TICKET_RE.findall("翻译器#106(8DVDVC)") == []
    assert dat._TICKET_RE.findall("器#106 abc#12 x#7") == []
    assert dat._TICKET_RE.findall("关联 #254") == ["254"]
    assert dat._TICKET_RE.findall("#188 复验通过") == ["188"]
    assert dat._TICKET_RE.findall("复验 #209：仍灰；见#210，工单#211、挂 # 212") == ["209", "210", "211", "212"]
    assert dat._TICKET_RE.findall("（#106）") == ["106"]


def test_decide_never_attaches_or_lifts_resolved_ticket(dat, tmp_path):
    """09-08 13:37 实锤：MTRCH2 原文提到「翻译器#106」→ 不挂已 fixed 的 #106（新立单）；
    显式「关联 #254」→ 挂开放单 #254；显式 #N 指向 fixed 单 → 不挂，走同报障人开放单，
    reason / mentions_resolved 记它。"""
    con = _mk_db(tmp_path / "b.db")
    _ticket(con, 106, "图片轮回复语言又漂中文", status="fixed", ts=time.time() - 8 * 86400)
    _ticket(con, 254, "em dash 出站文本", status="confirmed", ts=time.time() - 3600 * 10)
    diag = tmp_path / "diag"
    _write_report(diag, "MTRCH2", MTRCH2_NOTE, when="2026-09-08 13:36:58", app="1.0.77.0")
    dec = dat.decide(_head(dat, diag, "MTRCH2"), con, [], {"MTRCH2"}, diag)
    assert dec["action"] == "new" and dec["ticket"] == 0 and dec.get("mentions_resolved") in (None, [])
    _write_report(diag, "K1K1K1", "关联 #254：1.0.77 草稿预览仍有 — 破折号", when="2026-09-08 14:00:00", app="1.0.77.0")
    dec2 = dat.decide(_head(dat, diag, "K1K1K1"), con, [], {"MTRCH2", "K1K1K1"}, diag)
    assert dec2["action"] == "attach" and dec2["ticket"] == 254
    # 显式 #106（fixed）：不挂它；同报障人 90 分钟内有开放单 #254（同实体 12137839654）→ 挂开放单
    _ticket(con, 260, "WhatsApp evelyn(12137839654) 出站语言漂移", status="new",
            ts=time.mktime(time.strptime("2026-09-08 14:10:00", "%Y-%m-%d %H:%M:%S")))
    _write_report(diag, "K2K2K2", "复验 #106：evelyn(12137839654) 图片轮又回中文", when="2026-09-08 14:20:00", app="1.0.77.0")
    dec3 = dat.decide(_head(dat, diag, "K2K2K2"), con, [], {"MTRCH2", "K1K1K1", "K2K2K2"}, diag)
    assert dec3["action"] == "attach" and dec3["ticket"] == 260, dec3
    assert dec3["mentions_resolved"] == [106] and "#106（已修/已关）" in dec3["reason"]
    # 没有开放单可挂 → 新立，并带 mentions_resolved 让 apply 写「正文提及 #106（已修）」
    con.execute("DELETE FROM bug_tickets WHERE id=260")
    con.commit()
    _write_report(diag, "K3K3K3", "复验 #106：图片轮又回中文", when="2026-09-08 18:20:00", app="1.0.77.0")
    dec4 = dat.decide(_head(dat, diag, "K3K3K3"), con, [], {"K3K3K3"}, diag)
    assert dec4["action"] == "new" and dec4["mentions_resolved"] == [106]
    assert dat._mentions_resolved_note(dec4) == "[值守自动] 正文提及 #106（已修/已关，未挂不升）"
    assert dat._mentions_resolved_note({"action": "new"}) == ""
    # verify 报告对着已修单复验是正当的 → 仍记到 #106
    _write_report(diag, "V6V6V6", "#106 复验通过", kind="verify")
    assert dat.decide(_head(dat, diag, "V6V6V6"), con, [], {"V6V6V6"}, diag)["ticket"] == 106


def test_lift_severity_skips_resolved_ticket(dat, tmp_path):
    """auto_severity 只对开放单生效：fixed 单即使 note 判 P0 也不抬。"""
    db = tmp_path / "bug_intake.db"
    con = _mk_db(db)
    con.execute("ALTER TABLE bug_tickets ADD COLUMN severity TEXT DEFAULT 'P2'")
    _ticket(con, 106, "图片轮语言漂移", status="fixed")
    _ticket(con, 259, "全自动出站链 hana 破折号", status="new")
    con.execute("UPDATE bug_tickets SET severity='P1'")
    con.commit()

    class _BI:
        notes = []
        events = []

        @staticmethod
        def classify_severity(_note):
            return "P0"

        @staticmethod
        def _db_path():
            return db

        @classmethod
        def append_ticket_note(cls, tid, text):
            cls.notes.append((tid, text))

        @classmethod
        def _record_event(cls, *a):
            cls.events.append(a)

    assert dat._lift_severity(_BI, 106, "XXXXXX", "崩了 P0 直发中文", "8942577244") == ""
    assert con.execute("SELECT severity FROM bug_tickets WHERE id=106").fetchone()[0] == "P1"
    assert _BI.notes == [] and _BI.events == []
    assert dat._lift_severity(_BI, 259, "XXXXXX", "崩了 P0 直发中文", "8942577244") == "P0"
    assert con.execute("SELECT severity FROM bug_tickets WHERE id=259").fetchone()[0] == "P0"
    assert _BI.notes and _BI.events and "auto_severity" in _BI.events[0]


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


# ── N-5 D（2026-09-08）：同报障人 90 分钟跟进 → 挂最近单 ───────────────────────

def _attach(con, tid, code, note, when):
    """模拟 apply() 的挂单副作用：note[:380] 进 body（实体随补录累积）、updated_ts 前移。"""
    ts = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
    con.execute("UPDATE bug_tickets SET body=substr(body || char(10) || ?, 1, 4000), updated_ts=? WHERE id=?",
                (f"[报告 {code}] {note[:380]}", ts, tid))
    con.commit()


def _new(con, dat, tid, code, note, when):
    ts = time.mktime(time.strptime(when, "%Y-%m-%d %H:%M:%S"))
    _ticket(con, tid, dat.title_from_note(note), ts=ts, body=f"{note}\n[报告 {code}]")


def _replay(dat, con, diag, reports, next_id):
    """按时间顺序回放一组 (code, when, note)：new → 立单入库；attach → 补录进主单。返回每份的判定。"""
    out = []
    codes = set()
    for code, when, note in reports:
        _write_report(diag, code, note, when=when, app="1.0.76.0")
        codes.add(code)
        dec = dat.decide(_head(dat, diag, code), con, [], codes | {"ZQ4ASK", "9YYD44", "WN4JXJ"}, diag)
        if dec["action"] == "new":
            _new(con, dat, next_id, code, note, when)
            dat.write_ticket_txt(code, {"code": code, "action": "new", "ticket": next_id}, diag)
            dec = dict(dec, ticket=next_id)
            next_id += 1
        elif dec["action"] == "attach":
            _attach(con, dec["ticket"], code, note, when)
            dat.write_ticket_txt(code, {"code": code, "action": "attach", "ticket": dec["ticket"]}, diag)
        out.append((code, dec))
    return out


# 09-07 15:30–16:55 skuio「主动关怀·立即发」十份报告原文（自动链立了 #243–#249 七张单）
CARE_243 = [
    ("B5TAA3", "2026-09-07 15:30:17", "主动关怀页面(运行中·真发)：给 Kxhm(Telegram 7092595256)排的原文直发‘what are you doing now kim ?’(计划 16:29)，点‘立即发’按钮后未发出消息"),
    ("GGUNG4", "2026-09-07 15:35:10", "主动关怀 立即发 复查：15:28:31/50/58 三次 bring_forward 之后，派发循环是否在下一拍(≤5 分钟)真发、发了几次"),
    ("G7KEUT", "2026-09-07 15:35:41", "核实 13:14 启动时 proactive_care 派发循环状态(enabled/dry_run/interval)，以及 15:27 运行时切开关后循环是否有任何 tick 日志"),
    ("TXP5G3", "2026-09-07 15:47:08", "主动关怀 立即发 第二次复查(15:46)：15:28 三次 bring_forward 后 18 分钟、跨过 15:34:52/15:44:52 两个 600s 派发拍点，是否有派发/发送记录"),
    ("5CM8QB", "2026-09-07 15:48:03", "【主动关怀‘立即发’点击后 18 分钟未发出、零反馈、零派发日志】页面 运行中·真发。13:14:52 启动：proactive_care 派发循环已常备(interval=600s, enabled=False, dry_run=False)。15:27:31 用户页面开启 enabled=True，15:27:39 dry_run=False。15:28:22 [care-gen] id=1 contact=telegram:7092595256:6088992099 mode=verbatim decision=enqueued_by_operator text='what are you doing now kim ?'。15:28:31/15:28:50/15:28:58 三次点‘立即发’均记 decision=bring_forward。关联 ZQ4ASK / 9YYD44 / WN4JXJ / B5TAA3 / GGUNG4 / G7KEUT / TXP5G3"),
    ("NRTDJH", "2026-09-07 16:24:48", "主动关怀页(新版 UI：运行中·真发/关怀方案卡/原文直发)点击‘立即发’后消息未发出：目标 Kxhm telegram(7092595256) 原文‘what are you doing now kim ?’ 原排 16:29:18；核实 16:1x-16:24 是否有 care 派发/立即发请求、投递结果、是否 dry_run、以及是否有新版本启动"),
    ("WZPMXF", "2026-09-07 16:35:20", "主动关怀 id=1 原定 16:29:18 到期(原文直发 Kxhm telegram 6088992099)，核实 16:25-16:35 派发循环是否到点投递"),
    ("8VGQDM", "2026-09-07 16:50:21", "主动关怀 id=1 于 16:34:52 enqueued deferred send_in_min=13，核实 16:47-16:50 是否实际投递到 telegram 6088992099"),
    ("RGPTQF", "2026-09-07 16:53:57", "主动关怀 id=1 deferred 队列(interval=120s) 预计 16:47:52 出手，核实至 16:53 是否有出队/投递记录"),
    ("FHSZTA", "2026-09-07 16:55:00", "【BUG·主动关怀‘立即发’85 分钟未发出：三层各扣一段】1.0.76 新关怀页 15:27:39 切真发 dry_run=False；15:28:22 [care-gen] id=1 contact=telegram:7092595256:6088992099 mode=verbatim；关联 ZQ4ASK / 9YYD44 / WN4JXJ / NRTDJH / WZPMXF / 8VGQDM / RGPTQF"),
]


def test_replay_243_seven_followups_make_one_ticket(dat, tmp_path):
    """#243 七连发回放：十份报告只立一单，其余九份全挂 #243（自动链当时立了七张）。"""
    con = _mk_db(tmp_path / "b.db")
    diag = tmp_path / "diag"
    out = _replay(dat, con, diag, CARE_243, next_id=243)
    by = {c: d for c, d in out}
    assert by["B5TAA3"]["action"] == "new" and by["B5TAA3"]["ticket"] == 243
    for code in ("GGUNG4", "G7KEUT", "TXP5G3", "5CM8QB", "NRTDJH", "WZPMXF", "8VGQDM", "RGPTQF", "FHSZTA"):
        assert by[code]["action"] == "attach" and by[code]["ticket"] == 243, (code, by[code])
    # 三档各有命中：实体（Kxhm / 7092595256 / id=1）· 跟进词（复查 / 核实 / 第二次）· 关联码
    assert by["NRTDJH"]["via"] == "entity" and "kxhm" in by["NRTDJH"]["reason"] or "7092595256" in by["NRTDJH"]["reason"]
    assert by["WZPMXF"]["via"] == "entity" and by["RGPTQF"]["via"] == "entity"
    assert by["GGUNG4"]["via"] == "marker" and "复查" in by["GGUNG4"]["reason"]
    assert by["G7KEUT"]["via"] == "marker" and "核实" in by["G7KEUT"]["reason"]
    assert by["5CM8QB"].get("via_code") and by["FHSZTA"].get("via_code")
    assert con.execute("SELECT count(*) FROM bug_tickets").fetchone()[0] == 1


def test_replay_252_253_and_254_255_pairs(dat, tmp_path):
    """09-08 00:49 / 00:51：同人 2 分钟「补证：Vanessa(17345893728)×Sinue」应挂 #252（当时立了 #253）；
    02:04 同分钟 em dash 两份共享 12137839654 应只立一单（当时立了 #254 / #255）。"""
    con = _mk_db(tmp_path / "b.db")
    diag = tmp_path / "diag"
    out = _replay(dat, con, diag, [
        ("EC7QZM", "2026-09-08 00:49:40", "WhatsApp Vanessa(17345893728) 与 Sinue Alvarez(121****40) 会话：客户 21:02 明说‘Grammar, typing speed, and inconsistencies in situations are obviously handled by an AI assistant, so stop writing to me’，随后‘Never write me again’。取该会话今晚全自动回复链路"),
        ("K9F7PU", "2026-09-08 00:51:29", "补证：Vanessa(17345893728)×Sinue(12134989840) 21:00-21:04 三次起草的实际发送记录与入站-出站时间差；21:03:38 起草(客户已说 Never write me again)是否仍自动发出"),
        ("TDNJHQ", "2026-09-08 02:04:00", "统计近 4 小时全部自动回复文本中 em dash(—) 出现频率：WhatsApp evelyn(12137839654)×Martin Tohmasi 00:16/00:40/00:41 三条连续含 —"),
        ("KT992X", "2026-09-08 02:04:34", "查近 4 小时日志中任何含 em dash 的出站文本记录，及 12137839654 会话 00:16-00:45 起草/发送链路"),
    ], next_id=252)
    by = {c: d for c, d in out}
    assert by["EC7QZM"]["action"] == "new" and by["EC7QZM"]["ticket"] == 252
    assert by["K9F7PU"]["action"] == "attach" and by["K9F7PU"]["ticket"] == 252 and by["K9F7PU"]["via"] == "entity"
    # 02:04 距 #252 已 75 分钟且实体不同 → em dash 是新单；34 秒后的第二份同实体 → 挂它
    assert by["TDNJHQ"]["action"] == "new" and by["TDNJHQ"]["ticket"] == 253
    assert by["KT992X"]["action"] == "attach" and by["KT992X"]["ticket"] == 253 and "12137839654" in by["KT992X"]["reason"]


def test_related_code_pointing_at_fixed_ticket_yields_to_open_same_topic(dat, tmp_path):
    """MJGHKQ（09-07 13:22）：附带提及的 UE7VM3 / 29BAG6 都指向已修单，而同人 1 分钟前刚立的开放单
    #237（RN2SKR）才是它说的事——开放同题单优先；跟进词兜底档不参与这条抢占。"""
    con = _mk_db(tmp_path / "b.db")
    diag = tmp_path / "diag"
    t0 = time.mktime(time.strptime("2026-09-07 13:21:12", "%Y-%m-%d %H:%M:%S"))
    _ticket(con, 202, "人设备份与迁移缺失", status="fixed", ts=t0 - 86400, body="29BAG6")
    _ticket(con, 232, "Messenger Roselyn Maru 登录后会话显示全自动", status="fixed", ts=t0 - 80000, body="UE7VM3 JV83XM")
    rn = "人设工作室新版‘人设备份与迁移’：软件卸载重装后人设池为 0 个，用户点击‘从备份恢复…’选择卸载前导出的 chatx-personas-20260907-1301.json，页面报‘请求失败 _esc is not defined’"
    _ticket(con, 237, dat.title_from_note(rn), ts=t0, body=rn)
    for c in ("29BAG6", "UE7VM3", "RN2SKR"):
        _write_report(diag, c, "旧")
    mj = ("【BUG·1.0.76 人设备份恢复失败】卸载重装(13:14 启动 runtime=0 人设)后，人设工作室新‘人设备份与迁移’→‘从备份恢复…’"
          "选择卸载前导出的 chatx-personas-20260907-1301.json，界面报‘请求失败 _esc is not defined’。29BAG6 建议的备份/恢复已落地。"
          "附带发现：①AutoDraft 全局默认 mode=review——UE7VM3/JV83XM 落地。关联 29BAG6 / E42974")
    _write_report(diag, "MJGHKQ", mj, when="2026-09-07 13:22:28", app="1.0.76.0")
    codes = {"29BAG6", "UE7VM3", "RN2SKR", "MJGHKQ"}
    dec = dat.decide(_head(dat, diag, "MJGHKQ"), con, [], codes, diag)
    assert dec["action"] == "attach" and dec["ticket"] == 237, dec
    assert "已修/已关 #232" in dec["reason"] and dec["via_code"] == "UE7VM3"
    # 若同人没有开放同题单（#237 不存在）→ 仍按旧口径挂码指向的最新单 #232
    con.execute("DELETE FROM bug_tickets WHERE id=237")
    con.commit()
    dec2 = dat.decide(_head(dat, diag, "MJGHKQ"), con, [], codes, diag)
    assert dec2["ticket"] == 232 and dec2["reason"] == "关联报告 UE7VM3 → #232"


def test_order_by_report_time_puts_related_after_its_source(dat, tmp_path):
    diag = tmp_path / "diag"
    _write_report(diag, "MJGHKQ", "关联 RN2SKR", when="2026-09-07 13:22:28")
    _write_report(diag, "RN2SKR", "源报告", when="2026-09-07 13:21:12")
    (diag / "NOPACK").mkdir()                          # 解析不到时间 → 排最后
    assert dat.order_by_report_time(["MJGHKQ", "NOPACK", "RN2SKR"], diag) == ["RN2SKR", "MJGHKQ", "NOPACK"]
    src = (TOOL.parent / "duty_channel_reminder.py").read_text(encoding="utf-8")
    assert "pending = _order_pending(" in src and "order_by_report_time" in src


def test_watermarks_never_truncate_alphabetically():
    """0908 04:51 实锤：watch 水位 sorted(seen)[-200:] 按字母序截，码总数一过 200，以数字开头的新码
    （3HNCJ7 / 47KNBV）排最前被截掉 → 每 2 分钟重下重解、永远进不了回执 / 立单链。三处水位
    （watch codes / reminder receipted / auto_ticket handled）都只许按首见顺序截尾或跟随 watch 水位。"""
    watch = (TOOL.parent / "duty_watch_loop.py").read_text(encoding="utf-8")
    assert "sorted(seen)[-200:]" not in watch and 'st["codes"] = order[-600:]' in watch
    assert "order.append(code)" in watch
    rem = (TOOL.parent / "duty_channel_reminder.py").read_text(encoding="utf-8")
    assert "sorted(receipted)[-400:]" not in rem and "sorted(set(codes))[-400:]" not in rem
    assert 'st["receipted"] = [c for c in codes if c in receipted]' in rem
    auto = TOOL.read_text(encoding="utf-8")
    assert "sorted(handled)[-400:]" not in auto and "sorted(set(codes))[-400:]" not in auto
    assert 'st["handled"] = [c for c in codes if c in handled]' in auto


def test_followup_marker_and_entities(dat):
    assert dat.followup_marker("核实 13:14 启动时 proactive_care 派发循环状态") == "核实"
    assert dat.followup_marker("主动关怀 立即发 复查：15:28:31/50/58 三次") == "复查"
    assert dat.followup_marker("主动关怀 立即发 第二次复查(15:46)：…").startswith("第二次")
    assert dat.followup_marker("【补 MKYD7D·文件大小已确认】product_mv_16x9 = 98MB") == "补 MKYD7D"
    assert dat.followup_marker("补证：Vanessa(17345893728)×Sinue") == "补证"
    # 正文里的「核实：」不算引导段；【BUG】类新报告不算
    assert dat.followup_marker("人设工作室新版‘人设备份与迁移’：软件卸载重装后人设池为 0 个。核实：当前版本号") == ""
    assert dat.followup_marker("【BUG·主动关怀‘立即发’85 分钟未发出：三层各扣一段】") == ""
    e = dat.note_entities("主动关怀页面(运行中·真发)：给 Kxhm(Telegram 7092595256)排的原文直发")
    assert {"kxhm", "7092595256"} <= e and "telegram" not in e
    e2 = dat.note_entities("目标 Kxhm telegram(7092595256) 原文；id=1 contact=telegram:7092595256:6088992099")
    assert {"kxhm", "7092595256", "6088992099", "id=1", "telegram:7092595256:6088992099"} <= e2
    e3 = dat.note_entities("WhatsApp Vanessa(17345893728) 与 Sinue Alvarez(121****40) 会话")
    assert {"vanessa", "17345893728", "sinue alvarez"} <= e3
    # 日期 / 版本 / 时刻不是实体
    assert dat.note_entities("chatx-personas-20260907-1301.json 1.0.76.0 16:47:52") == set()
    # 不同会话不相交
    assert not (dat.note_entities("evelyn(12137839654)×Martin") & dat.note_entities("Vanessa(17345893728)×Sinue(12134989840)"))


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
    assert "receipted.update(batch)" in body
    assert "下一轮重试" in body
