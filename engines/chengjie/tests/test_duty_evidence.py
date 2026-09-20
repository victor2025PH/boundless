# -*- coding: utf-8 -*-
"""值守取证台账门禁（值守循环 v3 §B/§C/§D/§G，2026-09-02）。

钉住的口径（每条都对应一次真实翻车/险些翻车）：
- 版本抽取：#139 用户自报「版本: 1.067」必须认成 1.0.67；语音时长 0:06、
  机器码 B990-0C62 绝不误吞；值守回写的「已随 v1.0.68 修复」不覆盖用户自报。
- 分类保守优先：#145「克隆语音…失败」哪怕同句有内容词也判必须日志——
  0902 实锤：无日志给克隆声定性=拿修复史猜。
- 台账重放：request→received 闭环；超时档 30m/2h/24h 与 v2③ 同刻度。
- 加列幂等：reporter_version 重复 ensure 不再 ALTER（引擎并存零破坏）。
"""
import json
import sqlite3
import time

import pytest

from tools.duty_evidence import (
    CLASS_IMAGE, CLASS_MUST_LOG, backfill_versions, classify_ticket,
    ensure_reporter_version_column, evidence_gate, extract_fingerprints,
    extract_version, overdue_tier, parse_telemetry, replay_ledger,
    resolve_ticket_fps, ticket_send_gate, version_at_least,
)


# ── 版本抽取 ──────────────────────────────────────────────────────────────────

def test_extract_version_user_reported_form():
    # #139 原文形态：客户端显示 1.067
    assert extract_version("产品: 智聊 ChatX\n版本: 1.067\n机器码: B990-0C62") == "1.0.67"
    assert extract_version("version 1.0.68 still broken") == "1.0.68"
    assert extract_version("升到 v1.0.69 了") == "1.0.69"


def test_extract_version_ignores_noise():
    # 语音时长 / 机器码 / 小数不是版本
    assert extract_version("工作台发 0:06 对端实收 0:35") == ""
    assert extract_version("机器码 B990-0C62-FC1C-F668") == ""
    assert extract_version("概率 1.5 倍") == ""
    assert extract_version("") == ""


def test_extract_version_prefers_near_mention_over_later_bare():
    # 值守回写「已随 1.0.68 修复」不得覆盖用户自报版本
    body = "版本: 1.067\n……\n[值守] 该件已随 1.0.68 修复请复验"
    assert extract_version(body) == "1.0.67"


def test_extract_version_ignores_duty_reply_writeback():
    # 0902 现网实锤：#145 无用户自报版本，值守回复回写含「v1.0.68」——
    # 截断值守段后必须抽不到（版本未知→回访走「先升级」分支）
    body = ("克隆语音后，工具箱还是没有克隆成功\n[截图已收到]\n"
            "[值守回复] 克隆声的修复在 v1.0.68、表情的修复在 v1.0.69，先升级再验")
    assert extract_version(body) == ""
    # 用户自报在前、值守回写在后 → 只认用户自报
    body2 = "版本: 1.067\n克隆失败\n[值守回复] 已随 v1.0.69 上线请复验"
    assert extract_version(body2) == "1.0.67"


def test_version_at_least():
    assert version_at_least("1.0.69", "1.0.68") is True
    assert version_at_least("1.0.67", "1.0.68") is False
    assert version_at_least("", "1.0.68") is None
    assert version_at_least("1.0.68", "旗舰版") is None


# ── 工单分类（保守优先）──────────────────────────────────────────────────────

def test_classify_must_log_wins_over_content_words():
    # #145件①：同一单里「克隆…失败”与内容词并存 → 必须日志
    cls, hits = classify_ticket("克隆语音后，工具箱还是没有克隆成功，AI 还会评论表情")
    assert cls == CLASS_MUST_LOG
    assert "克隆" in hits


def test_classify_image_evidence_for_content_only():
    # #147：贬低推广——截图即完整现场
    cls, _ = classify_ticket("AI 说起其他人设推荐的产品，并贬低笑话产品本身")
    assert cls == CLASS_IMAGE


def test_classify_unknown_falls_back_conservative():
    cls, hits = classify_ticket("这个地方感觉不太对劲")
    assert cls == CLASS_MUST_LOG
    assert hits == []


def test_classify_144_and_146_are_must_log():
    assert classify_ticket("两个客户为什么没有自动回复")[0] == CLASS_MUST_LOG
    assert classify_ticket("手机删除消息了，工作台要同步删除")[0] == CLASS_MUST_LOG


# ── 遥测自动提取（v3 §C 自动化：能自己取的绝不问用户）────────────────────────

def test_extract_fingerprints():
    body = ("产品: 智聊 ChatX\n机器码: B990-0C62-FC1C-F668\n"
            "另一台 dc8f-0935-5ee4-f3d9 也有\n重复 B990-0C62-FC1C-F668")
    assert extract_fingerprints(body) == [
        "B990-0C62-FC1C-F668", "DC8F-0935-5EE4-F3D9"]
    # sha256 片段/普通连字符串不误吞
    assert extract_fingerprints("哈希 bb81fa29b1e6 和 2026-09-02 12:00") == []


def test_parse_telemetry_takes_latest_version_and_errors():
    lines = [
        '{"t":"2026-09-02T04:12:23Z","code":"YEBHMS","app":"1.067","fp":"DC8F-0935-5EE4-F3D9"}',
        '{"t":"2026-09-02T12:06:48Z","code":"64PY7D","app":"1.070","fp":"DC8F-0935-5EE4-F3D9"}',
        '{"t":"2026-09-02T11:46:17Z","fp":"B990-0C62-FC1C-F668","ver":"1.070","level":"INFO","msg":"boot"}',
        '{"t":"2026-09-02T11:46:18Z","fp":"B990-0C62-FC1C-F668","ver":"1.070","level":"ERROR","msg":"[SECURITY] secret_key"}',
        "not json at all",
    ]
    d = parse_telemetry(lines, "DC8F-0935-5EE4-F3D9")
    assert d["version"] == "1.0.70"          # 取时间最新，且规范化 1.070→1.0.70
    assert d["last_seen_utc"].startswith("2026-09-02T12:06")
    assert d["recent_errors"] == []          # 别机的 ERROR 不串味
    d2 = parse_telemetry(lines, "b990-0c62-fc1c-f668")   # 大小写不敏感
    assert d2["version"] == "1.0.70"
    assert len(d2["recent_errors"]) == 1 and "SECURITY" in d2["recent_errors"][0]


def test_resolve_ticket_fps_falls_back_to_reporter_history(tmp_path):
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE bug_tickets (id INTEGER PRIMARY KEY,"
              " reporter_id TEXT, body TEXT)")
    c.execute("INSERT INTO bug_tickets VALUES"
              " (1,'u1','旧单 机器码: B990-0C62-FC1C-F668'),"
              " (2,'u1','新单没写机器码'),"
              " (3,'u2','别人的单也没写')")
    assert resolve_ticket_fps(c, 2) == ["B990-0C62-FC1C-F668"]  # 回溯同人历史
    assert resolve_ticket_fps(c, 3) == []                        # 无处可回溯
    assert resolve_ticket_fps(c, 99) == []                       # 不存在的单
    # 三级回溯：正文全无 → 证据台账里值守登记的 fp（skuio 型报障人）
    ledger = [{"ticket": 3, "event": "request",
               "what": "远程diag（DC8F-0935-5EE4-F3D9 机）"}]
    assert resolve_ticket_fps(c, 3, ledger) == ["DC8F-0935-5EE4-F3D9"]


# ── 证据台账 ──────────────────────────────────────────────────────────────────

def test_replay_ledger_request_then_received():
    rows = [
        {"ts": 100.0, "ticket": 145, "event": "request", "what": "诊断包"},
        {"ts": 200.0, "ticket": 144, "event": "request", "what": "诊断包"},
        {"ts": 300.0, "ticket": 144, "event": "received", "diag": "dr-1a06"},
    ]
    st = replay_ledger(rows)
    assert st[145]["state"] == "pending"
    assert st[144]["state"] == "received"
    assert st[144]["diag"] == "dr-1a06"


def test_replay_ledger_second_request_reopens():
    rows = [
        {"ts": 100.0, "ticket": 9, "event": "request", "what": "一轮"},
        {"ts": 200.0, "ticket": 9, "event": "received", "diag": "dr-x"},
        {"ts": 300.0, "ticket": 9, "event": "request", "what": "二轮补证"},
    ]
    assert replay_ledger(rows)[9]["state"] == "pending"


def test_overdue_tiers_match_v2_cadence():
    now = time.time()
    assert overdue_tier(now - 60, now) == ""
    assert "30m" in overdue_tier(now - 31 * 60, now)
    assert "2h" in overdue_tier(now - 3 * 3600, now)
    assert "24h" in overdue_tier(now - 25 * 3600, now)


# ── 库操作（临时库，不碰现网）────────────────────────────────────────────────

@pytest.fixture()
def con(tmp_path):
    c = sqlite3.connect(str(tmp_path / "bug_intake.db"))
    c.execute("CREATE TABLE bug_tickets (id INTEGER PRIMARY KEY,"
              " title TEXT, body TEXT)")
    yield c
    c.close()


def test_ensure_column_idempotent(con):
    assert ensure_reporter_version_column(con) is True
    assert ensure_reporter_version_column(con) is False  # 二次不再 ALTER


def test_replay_ledger_set_class_override():
    rows = [
        {"ts": 1.0, "ticket": 5, "event": "set_class", "value": "image_evidence"},
        {"ts": 2.0, "ticket": 6, "event": "set_class", "value": "瞎写的"},
    ]
    st = replay_ledger(rows)
    assert st[5]["class_override"] == CLASS_IMAGE
    assert "class_override" not in st[6]   # 非法值不进状态


# ── 发送门禁（v3 §B 机器强制）────────────────────────────────────────────────

def test_evidence_gate_matrix():
    # 图即证据 / 证据已到 → 无声放行
    assert evidence_gate(CLASS_IMAGE, "") == (True, "")
    assert evidence_gate(CLASS_MUST_LOG, "received") == (True, "")
    # must_log 且未到 → 拦，说明里带工单号与出路
    ok, why = evidence_gate(CLASS_MUST_LOG, "pending", ticket=145)
    assert ok is False and "#145" in why and "--receipt" in why
    # ack 显式放行但必须带说明（调用方据此落审计）
    ok, why = evidence_gate(CLASS_MUST_LOG, "", ack=True, ticket=9)
    assert ok is True and "显式放行" in why


@pytest.fixture()
def data_root(tmp_path):
    (tmp_path / "config").mkdir()
    c = sqlite3.connect(str(tmp_path / "config" / "bug_intake.db"))
    c.execute("CREATE TABLE bug_tickets (id INTEGER PRIMARY KEY,"
              " title TEXT, body TEXT)")
    c.execute("INSERT INTO bug_tickets VALUES"
              " (145, '克隆语音失败', '工具箱还是没有克隆成功'),"
              " (147, 'AI 贬低自家推广', '说起其他人设推荐的产品并贬低')")
    c.commit()
    c.close()
    return tmp_path


def _ledger_append(root, row):
    fp = root / "logs" / "duty_evidence.jsonl"
    fp.parent.mkdir(exist_ok=True)
    with fp.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def test_ticket_send_gate_blocks_must_log_without_evidence(data_root):
    ok, why = ticket_send_gate(data_root, 145)
    assert ok is False and "#145" in why


def test_ticket_send_gate_allows_image_and_received_and_override(data_root):
    # 图即证据类直接放行
    assert ticket_send_gate(data_root, 147) == (True, "")
    # 证据到手后放行
    _ledger_append(data_root, {"ts": 1.0, "ticket": 145, "event": "request"})
    _ledger_append(data_root, {"ts": 2.0, "ticket": 145, "event": "received",
                               "diag": "dr-x"})
    assert ticket_send_gate(data_root, 145) == (True, "")


def test_ticket_send_gate_respects_class_override(data_root):
    # 启发式误判 must_log 时，人工覆写 image_evidence 放行
    _ledger_append(data_root, {"ts": 1.0, "ticket": 145,
                               "event": "set_class", "value": "image_evidence"})
    assert ticket_send_gate(data_root, 145) == (True, "")


def test_ticket_send_gate_soft_fails_open(data_root, tmp_path):
    # 工单不存在 / 库整个缺失 → 放行（台账绝不瘫发送通道）
    assert ticket_send_gate(data_root, 999) == (True, "")
    assert ticket_send_gate(tmp_path / "nonexist", 145) == (True, "")


def test_backfill_only_fills_extractable_and_empty(con):
    ensure_reporter_version_column(con)
    con.execute("INSERT INTO bug_tickets (id, title, body) VALUES"
                " (1, 't', '版本: 1.067 克隆失败'),"
                " (2, 't', '没有版本信息的单')")
    con.execute("UPDATE bug_tickets SET reporter_version='1.0.68' WHERE id=2")
    done = backfill_versions(con)
    assert done == [(1, "1.0.67")]
    got = dict(con.execute(
        "SELECT id, reporter_version FROM bug_tickets").fetchall())
    assert got == {1: "1.0.67", 2: "1.0.68"}
