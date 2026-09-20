"""记忆探针（2026-09-18 「还记得我们第一次聊什么吗」→「脑子有点空」事故沉淀）。"""
from __future__ import annotations

import time

from src.inbox.memory_probe import (
    build_memory_probe_block, detect_memory_probe, expand_probe_keywords,
    extract_probe_keywords, probe_from_store, select_evidence,
)

DAY = 86400.0


# ── 判定：保守词表 ─────────────────────────────────────────────────────────────

def test_detect_first_contact_variants():
    assert detect_memory_probe("还记得我们第一次聊什么吗") == "first_contact"
    assert detect_memory_probe("你还记得我们最开始是怎么聊起来的吗？") == "first_contact"
    assert detect_memory_probe("我们第一次聊的是什么？") == "first_contact"
    assert detect_memory_probe("do you remember what we first talked about?") == "first_contact"


def test_detect_name_and_recall():
    assert detect_memory_probe("我叫什么？") == "name"
    assert detect_memory_probe("你还记得我的名字吗") == "name"
    assert detect_memory_probe("what's my name?") == "name"
    assert detect_memory_probe("还记得我上次说的那个面试吗") == "recall"
    assert detect_memory_probe("我之前跟你说过我养了只猫，叫什么你记得吧") == "recall"
    assert detect_memory_probe("我上次跟你提过的那家店叫什么") == "recall"


def test_detect_stays_silent_on_normal_chat():
    for t in [
        "我第一次来北京，好冷", "我说过我不喜欢吃辣", "我的名字是小王", "我叫小王",
        "今天好累", "你在干嘛", "第一次做饭失败了哈哈", "记得早点睡", "我上次去了杭州",
        "Remember to drink water", "",
    ]:
        assert detect_memory_probe(t) is None, t


def test_keywords_strip_probe_phrases():
    kws = extract_probe_keywords("还记得我上次说的那个面试吗")
    assert "面试" in kws
    assert not any(k in ("还记得", "上次", "那个", "吗") for k in kws)
    kws2 = extract_probe_keywords("do you remember my cat Tom?")
    assert "cat" in kws2 and "tom" in kws2 and "remember" not in kws2
    assert extract_probe_keywords("还记得吗") == []
    # 单字停用词「到」不得把「吃到撑」切碎（否则关键词清空 → 明明说过却查无证据）
    kws3 = extract_probe_keywords("还记得我上次说吃什么吃到撑吗")
    assert any("吃到撑" in k or k == "吃到撑" for k in kws3)


def test_expand_probe_keywords_hotpot_synonyms():
    assert "火锅" in expand_probe_keywords(["涮肉"])
    assert "火锅" in expand_probe_keywords(["涮肉店"])
    assert expand_probe_keywords([]) == []


# ── 证据选择 ───────────────────────────────────────────────────────────────────

def _rows(now):
    return [
        {"direction": "in", "text": "hi 你好呀", "ts": now - 60 * DAY},
        {"direction": "out", "text": "你好，我是林佳欣", "ts": now - 60 * DAY + 30},
        {"direction": "in", "text": "我在深圳做设计", "ts": now - 60 * DAY + 90},
        {"direction": "out", "text": "设计师呀，厉害", "ts": now - 60 * DAY + 120},
        {"direction": "in", "text": "[图片]", "ts": now - 30 * DAY},
        {"direction": "in", "text": "下周有个面试，紧张", "ts": now - 20 * DAY},
        {"direction": "out", "text": "你可以的，放松点", "ts": now - 20 * DAY + 40},
        {"direction": "in", "text": "面试过了！", "ts": now - 18 * DAY},
        {"direction": "out", "text": "太棒了！", "ts": now - 18 * DAY + 10},
        {"direction": "in", "text": "还记得我上次说的那个面试吗", "ts": now},
    ]


def test_select_first_contact_takes_earliest_and_skips_placeholders():
    now = time.time()
    ev = select_evidence(_rows(now), "first_contact", [], current_text="还记得我上次说的那个面试吗", max_items=4)
    assert [r["text"] for r in ev] == ["hi 你好呀", "你好，我是林佳欣", "我在深圳做设计", "设计师呀，厉害"]


def test_select_recall_by_keyword_with_context_and_user_priority():
    now = time.time()
    ev = select_evidence(_rows(now), "recall", ["面试"], current_text="还记得我上次说的那个面试吗")
    texts = [r["text"] for r in ev]
    assert "下周有个面试，紧张" in texts and "面试过了！" in texts
    assert "还记得我上次说的那个面试吗" not in texts       # 本条问句不是证据
    assert texts == sorted(texts, key=lambda t: [r["text"] for r in _rows(now)].index(t))  # 时间序


def test_select_name_finds_self_introduction():
    now = time.time()
    rows = [{"direction": "in", "text": "我叫阿明，在广州", "ts": now - 10 * DAY},
            {"direction": "out", "text": "阿明你好", "ts": now - 10 * DAY + 5}]
    ev = select_evidence(rows, "name", [])
    assert [r["text"] for r in ev] == ["我叫阿明，在广州"]


def test_select_recall_no_keywords_or_no_hits_returns_empty():
    now = time.time()
    assert select_evidence(_rows(now), "recall", []) == []
    assert select_evidence(_rows(now), "recall", ["钓鱼"]) == []


# ── 注入块 ─────────────────────────────────────────────────────────────────────

def test_block_with_evidence_quotes_verbatim_and_fuzzy_time_rule():
    now = time.time()
    ev = select_evidence(_rows(now), "first_contact", [], max_items=3)
    block = build_memory_probe_block("first_contact", ev, now=now, total_msgs=204, first_ts=now - 60 * DAY)
    assert block.startswith("【记忆检索——重要】")
    assert "「hi 你好呀」" in block and "「我在深圳做设计」" in block
    assert "两个多月前" in block or "一个多月前" in block
    assert "不可添加记录里没有的细节" in block
    assert "204" in block


def test_block_without_evidence_forbids_fabrication():
    block = build_memory_probe_block("recall", [], total_msgs=10)
    assert "绝不能编" in block and "记不太清" in block


# ── store 接线 ─────────────────────────────────────────────────────────────────

class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_messages(self, cid, *, limit=50):
        return self.rows[:limit]

    def list_recent_messages(self, cid, *, limit=50, include_deleted=True):
        return self.rows[-limit:]

    def count_messages(self, cid=""):
        return len(self.rows)


def test_probe_from_store_first_contact_and_recall():
    now = time.time()
    st = _Store(_rows(now))
    b1 = probe_from_store(st, "c1", "还记得我们第一次聊什么吗", now=now)
    assert "「hi 你好呀」" in b1 and "【记忆检索——重要】" in b1
    b2 = probe_from_store(st, "c1", "还记得我上次说的那个面试吗", now=now)
    assert "面试过了" in b2
    # 空泛的「还记得吗」→ 坦白块（让 LLM 反问），不猜
    b3 = probe_from_store(st, "c1", "还记得吗", now=now)
    assert "绝不能编" in b3


def test_probe_from_store_silent_when_not_probe_or_no_store():
    now = time.time()
    st = _Store(_rows(now))
    assert probe_from_store(st, "c1", "今天好累", now=now) == ""
    assert probe_from_store(None, "c1", "还记得我们第一次聊什么吗", now=now) == ""
    assert probe_from_store(st, "", "还记得我们第一次聊什么吗", now=now) == ""


def test_probe_from_store_swallows_store_errors():
    class _Bad:
        def list_messages(self, *a, **k):
            raise RuntimeError("db locked")

        def list_recent_messages(self, *a, **k):
            raise RuntimeError("db locked")

    assert probe_from_store(_Bad(), "c1", "还记得我们第一次聊什么吗") == ""
