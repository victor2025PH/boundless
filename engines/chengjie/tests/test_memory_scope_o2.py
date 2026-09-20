"""O-2 C（2026-09-08，#201 / W7ZTSB / SV62XJ③ 收口）：私事只进客户记忆，永不进共享 KB。

单一来源 ``src/utils/memory_scope.py``：``personal_anchors`` / ``memory_scope``。抽取链去向日志
``[episodic] scope=customer reason=anchors:…`` 与 DailyLearner ``private_kinds`` 分流同一口径。

指令验收三例：约会地点 → customer；对方生日 → customer；通用天气常识 → shared。
再钉：弱信号（星期 / 钟点 / 场所 / 相对日）单独不成锚——业务问题必须还能进共享 KB。
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from src.utils.daily_learner import PRIVATE_KINDS, DailyLearner, private_kinds
from src.utils.memory_scope import (
    ANCHOR_KINDS,
    REASON_NO_ANCHOR,
    SCOPE_CUSTOMER,
    SCOPE_SHARED,
    is_customer_private,
    memory_scope,
    personal_anchors,
)


# ── 指令验收三例 ─────────────────────────────────────────────────────────────

def test_case_1_date_place_is_customer_scope():
    scope, reason = memory_scope("周六下午三点星巴克见吧")
    assert scope == SCOPE_CUSTOMER
    assert "time" in reason and "place" in reason


def test_case_2_birthday_is_customer_scope():
    scope, reason = memory_scope("我生日是3月2号")
    assert scope == SCOPE_CUSTOMER
    assert "birthday" in reason and "time" in reason
    assert memory_scope("My birthday is March 2")[0] == SCOPE_CUSTOMER


def test_case_3_general_weather_is_shared():
    assert memory_scope("曼谷雨季一般是五月到十月，下午常有雷阵雨") == (SCOPE_SHARED, REASON_NO_ANCHOR)
    assert is_customer_private("曼谷雨季一般是五月到十月，下午常有雷阵雨") is False


# ── 强锚点单独成立 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,kind", [
    ("我住在曼谷", "personal"),
    ("I live in Bangkok", "personal"),
    ("你今年30岁了吧", "personal"),
    ("2026-09-15 出发", "time"),
    ("我叫小美，记住我", "name"),
    ("My name is Maria, remember me", "name"),
    ("可以先转账 5000 元给你吗", "money"),
    ("下周我飞过来见你，接机吗", "meet"),
    ("我女儿下个月结婚", "family"),
    ("你答应周末来找我的", "commitment"),
    ("我的地址是幸福小区 3 号楼 502", "address"),
])
def test_strong_anchors_alone(text, kind):
    kinds = personal_anchors(text)
    assert kind in kinds, (text, kinds)
    assert memory_scope(text)[0] == SCOPE_CUSTOMER


# ── 弱信号必须合取：业务问题仍是共享 ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    "怎么充值VIP？", "退款要几天到账",
    "你们周六营业吗？",          # 星期 单独
    "周一到周五营业",            # 星期 单独
    "Do you open on Sunday?",    # weekday 单独
    "附近有公园吗",              # 场所 单独
    "客服 24/7 在线吗",          # 不是日期
    "有三点建议",                # 「三点」不是钟点
    "明天几点开门",              # 相对日 + 疑问，无钟点无约见动词
    "你们住在哪个城市",          # 「你们」不是个人属性
    "喜欢吃辣",                  # 偏好无锚点（抽取链仍写客户 episodic，见 skill_manager 日志口径）
])
def test_weak_signals_alone_stay_shared(text):
    assert personal_anchors(text) == [], text
    assert memory_scope(text) == (SCOPE_SHARED, REASON_NO_ANCHOR)


@pytest.mark.parametrize("text,expect", [
    ("明天下午三点我到机场", {"time", "place", "meet"}),
    ("Saturday 3pm at Starbucks?", {"time", "place"}),
    ("tonight at 8 see you", {"time"}),
    ("在星巴克等你", {"place"}),
])
def test_weak_signals_conjoined_become_anchors(text, expect):
    assert expect <= set(personal_anchors(text)), (text, personal_anchors(text))


def test_anchor_order_and_empty_input():
    kinds = personal_anchors("我下周飞过来见你，先转 5000 块给你")
    assert kinds == [k for k in ANCHOR_KINDS if k in kinds], "reason 串按 ANCHOR_KINDS 顺序"
    assert personal_anchors("") == [] and personal_anchors(None) == []
    assert memory_scope("   ") == (SCOPE_SHARED, REASON_NO_ANCHOR)


# ── DailyLearner：同一口径 + 三例落库去向 ───────────────────────────────────

def test_private_kinds_follows_memory_scope_and_keeps_l4_cases():
    assert set(private_kinds("周六下午三点星巴克见吧")) >= {"time", "place"}
    assert set(private_kinds("我生日是3月2号")) >= {"birthday", "time"}
    assert private_kinds("曼谷雨季一般是五月到十月，下午常有雷阵雨") == []
    # L-4 F 既有断言不回归
    assert "money" in private_kinds("可以先转账 5000 元给你吗")
    assert "meet" in private_kinds("下周我飞过来见你，接机吗")
    assert "name" in private_kinds("My name is Maria, remember me")
    assert private_kinds("怎么充值VIP？") == []
    assert private_kinds("退款要几天到账") == []
    for k in ANCHOR_KINDS:
        assert k in PRIVATE_KINDS, f"锚点类别 {k} 必须能在学习页打标"


def test_every_private_kind_has_bilingual_label():
    from src.web.i18n_packs.learner_page import EN, ZH
    for k in PRIVATE_KINDS:
        assert ZH.get(f"lr4_private_kind_{k}") and EN.get(f"lr4_private_kind_{k}"), k


class _KB:
    def __init__(self, tmp_path):
        self.db_path = tmp_path / "kb.db"
        self.added = []
        self.meta = {}

    def get_meta(self, key, default=None):
        return self.meta.get(key, default)

    def set_meta(self, key, value):
        self.meta[key] = value

    def add_entry(self, data):
        self.added.append(dict(data))
        return f"kb-{len(self.added)}"

    def get_miss_stats(self, top_k=10):
        return []

    def list_feedback(self, limit=50):
        return []

    def get_auto_suggestions(self, **kw):
        return []

    def search(self, query, top_k=3, include_vendor=None):
        return {"entries": []}

    def _conn(self):
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE kb_entries (id TEXT, title TEXT, triggers TEXT, enabled INT)")
        return c

    def delete_miss_entry(self, q):
        pass


def _draft(query, **kw):
    d = {"source": "miss", "query": query, "hit_count": 2, "category": "其他",
         "title": query[:20], "triggers": ["测试"], "example_reply": "示例回复",
         "confidence": 60}
    d.update(kw)
    return d


def test_learner_routes_three_cases_and_logs_scope(tmp_path, caplog):
    kb = _KB(tmp_path)
    dl = DailyLearner(kb, None, db_path=tmp_path / "drafts.db")
    written = []
    dl.set_memory_writer(lambda conv, content, quote: (written.append((conv, content)) or
                                                       {"key": "k:" + conv, "row_id": 1}))
    dl.save_drafts([
        _draft("周六下午三点星巴克见吧", source_ref="conv:whatsapp:acct:c1"),
        _draft("我生日是3月2号", source_ref="conv:whatsapp:acct:c1"),
        _draft("曼谷雨季一般是五月到十月，下午常有雷阵雨", source_ref="conv:whatsapp:acct:c1"),
    ])
    rows = {d["query"]: d for d in dl.list_drafts(limit=10)}
    assert set(rows["周六下午三点星巴克见吧"]["private_kinds"].split(",")) >= {"time", "place"}
    assert set(rows["我生日是3月2号"]["private_kinds"].split(",")) >= {"birthday", "time"}
    assert rows["曼谷雨季一般是五月到十月，下午常有雷阵雨"]["private_kinds"] == ""

    with caplog.at_level(logging.INFO, logger="ai_chat_assistant.DailyLearner"):
        r1 = dl.approve_draft(rows["周六下午三点星巴克见吧"]["id"], operator="t")
        r2 = dl.approve_draft(rows["我生日是3月2号"]["id"], operator="t")
        r3 = dl.approve_draft(rows["曼谷雨季一般是五月到十月，下午常有雷阵雨"]["id"], operator="t")
    assert r1.startswith("mem:") and r2.startswith("mem:"), "私事只进该客户记忆"
    assert r3 == "kb-1", "通用常识进共享 KB"
    assert len(written) == 2 and all(c == "whatsapp:acct:c1" for c, _ in written)
    assert len(kb.added) == 1 and kb.added[0]["title"].startswith("曼谷雨季")
    # ai_chat_assistant.* 的上色 handler 会给 record 消息包 ANSI 码 → 用子串不用 startswith
    msgs = [r.getMessage() for r in caplog.records if "[episodic] scope=" in r.getMessage()]
    assert sum("[episodic] scope=customer reason=" in m and "dest=memory:" in m for m in msgs) == 2
    assert sum("[episodic] scope=shared reason=no_personal_anchor" in m and "dest=kb:kb-1" in m
               for m in msgs) == 1


# ── 抽取链去向日志口径（源码级钉住：只写客户库、scope 恒 customer、reason 带锚点）────

def test_skill_manager_extract_logs_scope_with_anchors():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "skills" / "skill_manager.py"
           ).read_text(encoding="utf-8", errors="ignore")
    i = src.index("async def _episodic_memory_extract_async(")
    body = src[i:i + 12000]
    assert "from src.utils.memory_scope import personal_anchors" in body
    assert '"[episodic] scope=customer reason=%s facts=%d user=%s"' in body
    assert "episodic_only" in body
    # 抽取链没有任何共享写口：add_fact 全部按客户 key
    assert "add_fact(\n                    key," in body or "add_fact(key," in body
