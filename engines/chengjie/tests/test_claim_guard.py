# -*- coding: utf-8 -*-
"""P-1 B（#259 #254 · ZH3ZQ5③）：引用锚点守卫 claim_guard。

钉住：
  - ZH3ZQ5 回放：清库重装、零历史零记忆 → 「that hiking trail you mentioned a while back」整句改中性；
  - 给一条含 hiking 的历史 → 放行；记忆里有 → 放行（anchor=memory）；
  - 三语各 3 例（en / zh / ja）无锚点改写、有锚点放行；
  - 误伤：引用短语出现在客户原话引用（引号）里不改；出稿逐字复述历史不改；日常句零命中；
  - 中性句按语言取模板、同 seed 稳定；其余句子已在提问 → 只删不补；
  - 空 / None 安全；bot.db 只读反查记忆事实。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.inbox import claim_guard as cg

ZH3ZQ5 = "I was just thinking about that hiking trail you mentioned a while back \u2014 did you ever end up going?"


@pytest.fixture(autouse=True)
def _reset():
    cg._reset_for_tests()
    yield
    cg._reset_for_tests()


def test_replay_zh3zq5_no_history_no_memory_rewrites_to_neutral():
    out, rep = cg.check_claims(ZH3ZQ5, history_texts=[], memory_facts=[], lang="en", seed="whatsapp:a:b")
    assert rep["action"] == "rewrite" and rep["anchored"] is False
    assert "hik" in rep["keywords"] and "trail" in rep["keywords"]
    assert "mentioned" not in out and "hiking" not in out and "\u2014" not in out
    assert out in cg._NEUTRAL["en"] or out[:1].lower() + out[1:] in cg._NEUTRAL["en"]
    # 同 seed 稳定
    out2, _ = cg.check_claims(ZH3ZQ5, history_texts=[], memory_facts=[], lang="en", seed="whatsapp:a:b")
    assert out2 == out


def test_history_with_hiking_anchors_and_passes():
    hist = ["hey", "we did a hiking trip near the lake last month, it was great", "nice!"]
    out, rep = cg.check_claims(ZH3ZQ5, history_texts=hist, memory_facts=[], lang="en")
    assert out == ZH3ZQ5 and rep["action"] == "pass" and rep["anchor"] == "history"
    # 记忆事实里有 → 也放行
    out, rep = cg.check_claims(ZH3ZQ5, history_texts=["hi"], memory_facts=["likes hiking on weekends"], lang="en")
    assert out == ZH3ZQ5 and rep["anchor"] == "memory"


@pytest.mark.parametrize("text,lang", [
    ("You said work was a lot last week. Still crazy?", "en"),
    ("Remember when you told me about your sister's wedding?", "en"),
    ("Last time you were talking about moving to Lisbon.", "en"),
    ("你之前说的那个徒步路线，后来去了吗？", "zh"),
    ("记得你提过喜欢猫。", "zh"),
    ("上次你说要换工作，怎么样了？", "zh"),
    ("前に言ってたハイキングのコース、行った？", "ja"),
    ("覚えてる？あの時話してたカフェ。", "ja"),
    ("前回話してた妹さんの結婚式どうだった？", "ja"),
])
def test_trilingual_unanchored_rewritten(text, lang):
    out, rep = cg.check_claims(text, history_texts=[], memory_facts=[], lang=lang, seed="s")
    assert rep["action"] == "rewrite", (text, rep)
    assert out.strip() and out != text
    for ph in rep["phrases"]:
        assert ph not in out
    pool = cg._NEUTRAL[lang]
    # 其余句子已在提问（「Still crazy?」）→ 只删；否则补中性问句
    assert out.rstrip().endswith(("?", "？")) and (
        any(p in out or p[:1].upper() + p[1:] in out for p in pool)
        or text.rstrip().endswith(("?", "？"))), out


@pytest.mark.parametrize("text,lang,hist", [
    ("You said work was a lot last week. Still crazy?", "en", ["ugh work has been a lot"]),
    ("Remember when you told me about your sister's wedding?", "en", ["my sister is getting married in June"]),
    ("Last time you were talking about moving to Lisbon.", "en", ["thinking of moving to lisbon tbh"]),
    ("你之前说的那个徒步路线，后来去了吗？", "zh", ["周末去徒步了，累死"]),
    ("记得你提过喜欢猫。", "zh", ["我家猫又拆家了，喜欢归喜欢"]),
    ("上次你说要换工作，怎么样了？", "zh", ["想换工作了"]),
    ("前に言ってたハイキングのコース、行った？", "ja", ["先週ハイキングに行った"]),
    ("覚えてる？あの時話してたカフェ。", "ja", ["駅前のカフェ良かった"]),
    ("前回話してた妹さんの結婚式どうだった？", "ja", ["妹の結婚式が来月"]),
])
def test_trilingual_anchored_pass(text, lang, hist):
    out, rep = cg.check_claims(text, history_texts=hist, memory_facts=[], lang=lang)
    assert out == text and rep["action"] == "pass" and rep["anchor"] == "history", (text, rep)


def test_quoted_customer_words_not_touched():
    t = 'You literally wrote "you mentioned it twice" and then went quiet. How was the trip?'
    out, rep = cg.check_claims(t, history_texts=[], memory_facts=[], lang="en")
    assert out == t and rep["action"] == "pass"
    t2 = "你说「你之前说过的」这句我笑了。今天怎么样？"
    out, rep = cg.check_claims(t2, history_texts=[], memory_facts=[], lang="zh")
    assert out == t2 and rep["action"] == "pass"


def test_verbatim_restatement_of_history_not_touched():
    line = "last time you said you'd call."
    out, rep = cg.check_claims(line, history_texts=["Last time you said you'd call."], memory_facts=[], lang="en")
    assert out == line and rep["action"] == "pass"


def test_everyday_text_zero_hits():
    for t, lg in (("Morning! Coffee first, then work.", "en"),
                  ("I went hiking a while back, it was fun.", "en"),
                  ("Are you busy tonight?", "en"),
                  ("今天好累，你吃饭了吗？", "zh"),
                  ("你说得对。", "zh"),
                  ("今日は疲れた。ご飯食べた？", "ja")):
        assert cg.find_claims(t, lg) == [], t
        out, rep = cg.check_claims(t, history_texts=[], memory_facts=[], lang=lg)
        assert out == t and rep["action"] == "clean", t


def test_other_sentence_already_asks_then_only_drop():
    out, rep = cg.check_claims("Are you busy tonight? You said work was a lot.",
                               history_texts=[], memory_facts=[], lang="en")
    assert rep["action"] == "rewrite" and out == "Are you busy tonight?"
    # 没在提问 → 删掉引用句后补一句中性问
    out, rep = cg.check_claims("Coffee first. You said work was a lot.",
                               history_texts=[], memory_facts=[], lang="en", seed="x")
    assert out.startswith("Coffee first.") and out.rstrip().endswith("?") and "work" not in out


def test_bare_reference_like_you_said_passes_only_with_history():
    t = "Like you said, coffee first."
    out, rep = cg.check_claims(t, history_texts=["coffee is life"], memory_facts=[], lang="en")
    assert out == t and rep["action"] == "pass"
    out, rep = cg.check_claims(t, history_texts=[], memory_facts=[], lang="en")
    assert rep["action"] == "rewrite"


def test_empty_and_none_safe():
    assert cg.check_claims("")[1]["action"] == "clean"
    assert cg.check_claims(None)[1]["action"] == "clean"   # type: ignore[arg-type]
    assert cg.neutral_line("xx") == cg._NEUTRAL["en"][0]


def test_memory_facts_bot_db_readonly_fallback(tmp_path: Path):
    db = tmp_path / "bot.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE episodic_memory (id INTEGER PRIMARY KEY, user_id TEXT, content TEXT, status TEXT)")
    conn.executemany("INSERT INTO episodic_memory (user_id, content, status) VALUES (?,?,?)", [
        ("whatsapp:acct1:447349041791", "loves hiking in the Lake District", "active"),
        ("447349041791", "has a cat", None),
        ("447349041791", "ignored fact", "ignored"),
        ("other", "unrelated", "active"),
    ])
    conn.commit()
    conn.close()

    class _Store:
        _db_path = tmp_path / "inbox.db"

    facts = cg.memory_facts_for("447349041791", "acct1", store=_Store())
    assert "loves hiking in the Lake District" in facts and "has a cat" in facts
    assert "ignored fact" not in facts and "unrelated" not in facts
    # 没有 bot.db → 空；provider 优先
    class _Store2:
        _db_path = tmp_path / "elsewhere" / "inbox.db"
    assert cg.memory_facts_for("447349041791", "acct1", store=_Store2()) == []
    cg.set_memory_facts_provider(lambda ck, acct: ["from provider"])
    assert cg.memory_facts_for("447349041791", "acct1", store=_Store()) == ["from provider"]
