"""P2：care LLM 抽取层纯函数门禁（廉价门 / 容错解析 / 确定性调度换算）。"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.contacts.care_extract_llm import (
    build_llm_extract_prompt, commitment_from_llm, parse_llm_extract, time_like,
)

NOW = datetime(2026, 8, 1, 10, 0, 0).timestamp()


# ── 廉价门：多语种时间信号 ────────────────────────────────────────────────
def test_gate_multilingual_positives():
    hits = [
        "明天下午终面，有点慌",            # zh 相对日
        "下周三去体检",                    # zh 周X
        "3月5日出差去曼谷",                # zh 绝对日期
        "8/15 提车！",                     # 数字日期
        "my interview is tomorrow",        # en
        "see you next week!",              # en
        "面接は明日です",                  # ja
        "내일 병원 가요",                   # ko
        "พรุ่งนี้ไปสัมภาษณ์งาน",           # th
        "ngày mai mình đi khám",           # vi
        "besok aku interview",             # id
        "mañana tengo examen",             # es
        "生日是15号哦",                    # zh 号
        "meeting at 15:30",                # 时刻
    ]
    for t in hits:
        assert time_like(t), f"应过门: {t}"


def test_gate_negatives():
    misses = [
        "好的谢谢",
        "哈哈哈哈",
        "ok",
        "多少钱？",          # 裸数字语境不算（「点/号」都没有）
        "在吗",
        "I love this so much",
    ]
    for t in misses:
        assert not time_like(t), f"不该过门: {t}"


def test_gate_empty_and_short():
    assert not time_like("")
    assert not time_like(" ")
    assert not time_like("a")


# ── prompt 构造：带今日/星期锚点 ─────────────────────────────────────────
def test_prompt_has_anchor_and_text():
    p = build_llm_extract_prompt("明天面试", now=NOW)
    assert "2026-08-01" in p
    assert "周六" in p          # 2026-08-01 是周六
    assert "明天面试" in p
    assert '"found"' in p       # JSON 契约在场


def test_prompt_truncates_long_text():
    p = build_llm_extract_prompt("啊" * 900, now=NOW)
    assert "啊" * 500 in p and "啊" * 501 not in p


# ── 容错解析 ─────────────────────────────────────────────────────────────
def test_parse_clean_json():
    d = parse_llm_extract('{"found":true,"topic":"面试","date":"2026-08-05","greeting":false,"confidence":0.9}')
    assert d == {"found": True, "topic": "面试", "date": "2026-08-05",
                 "greeting": False, "confidence": 0.9}


def test_parse_fenced_and_prose_wrapped():
    raw = "好的，这是结果：\n```json\n{\"found\": true, \"topic\": \"interview\", \"date\": \"2026-09-01\", \"confidence\": 0.8}\n```\n以上。"
    d = parse_llm_extract(raw)
    assert d and d["found"] and d["topic"] == "interview"


def test_parse_not_found_passthrough():
    assert parse_llm_extract('{"found": false}') == {"found": False}
    # found=false 时其它字段一律忽略
    assert parse_llm_extract('{"found": false, "topic": "x"}') == {"found": False}


def test_parse_garbage_returns_none():
    assert parse_llm_extract(None) is None
    assert parse_llm_extract("") is None
    assert parse_llm_extract("我觉得没有约定") is None
    assert parse_llm_extract('{"found": true') is None            # 截断
    assert parse_llm_extract('{"found":true,"topic":"","date":"2026-08-05"}') is None  # 空 topic
    assert parse_llm_extract('{"found":true,"topic":"面试","date":"08-05"}') is None   # 坏日期
    assert parse_llm_extract('{"found":true,"topic":"面试","date":"2026-13-40"}') is None  # 假日期
    assert parse_llm_extract('{"found":true,"topic":"' + "长" * 50 + '","date":"2026-08-05"}') is None  # 超长 topic


def test_parse_confidence_clamped_and_defaulted():
    d = parse_llm_extract('{"found":true,"topic":"t","date":"2026-08-05","confidence":7}')
    assert d["confidence"] == 1.0
    d2 = parse_llm_extract('{"found":true,"topic":"t","date":"2026-08-05"}')
    assert d2["confidence"] == 0.7


def test_parse_json_with_brace_in_string():
    d = parse_llm_extract('{"found":true,"topic":"考{试}","date":"2026-08-05"}')
    assert d and d["topic"] == "考{试}"


# ── LLM 结果 → CareCommitment（调度换算必须是确定性代码） ────────────────
def test_commitment_followup_2000():
    d = {"found": True, "topic": "面试", "date": "2026-08-05",
         "greeting": False, "confidence": 0.9}
    c = commitment_from_llm(d, source_text="明天面试", now=NOW)
    assert c is not None
    due = datetime.fromtimestamp(c.due_at)
    assert (due.year, due.month, due.day, due.hour) == (2026, 8, 5, 20)
    assert c.anchor_text == "llm"
    assert c.confidence == 0.9


def test_commitment_greeting_0900():
    d = {"found": True, "topic": "生日", "date": "2026-08-03",
         "greeting": True, "confidence": 0.85}
    c = commitment_from_llm(d, now=NOW)
    assert c is not None
    assert datetime.fromtimestamp(c.due_at).hour == 9


def test_commitment_past_date_dropped():
    d = {"found": True, "topic": "面试", "date": "2026-07-30",
         "greeting": False, "confidence": 0.9}
    assert commitment_from_llm(d, now=NOW) is None


def test_commitment_same_day_after_followup_hour_dropped():
    late = datetime(2026, 8, 5, 21, 0, 0).timestamp()   # 已过当日 20:00
    d = {"found": True, "topic": "面试", "date": "2026-08-05", "confidence": 0.9}
    assert commitment_from_llm(d, now=late) is None


def test_commitment_none_inputs():
    assert commitment_from_llm(None, now=NOW) is None
    assert commitment_from_llm({"found": False}, now=NOW) is None


def test_commitment_snippet_truncated():
    d = {"found": True, "topic": "面试", "date": "2026-08-05", "confidence": 0.9}
    c = commitment_from_llm(d, source_text="长" * 500, now=NOW)
    assert c is not None and len(c.source_text) <= 160
