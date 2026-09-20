"""口头禅短语账本 + 句级复读（2026-09-18 「说真的」事故沉淀）。

事故：一个会话里几乎条条「说真的」。三处根因：人设 style/quirks/openers 同时在教；
vivid 口语改写档点名推荐「说真的」「我跟你讲」；而 reply_variety 的 bigram 兜底在停用字
（说/的）处切段 → 「说真的」被切成「真」一个字，账本对它**完全失明**。同理「57 天没聊了
真不好意思」这种整句借口模板 bigram 也抓不到。
"""
from __future__ import annotations

from src.ai.reply_variety import (
    build_variety_hint, collect_overused, message_clauses, message_fillers, parse_variety_cfg,
)
from src.ai.voice_colloquial_llm import build_colloquial_prompt


def test_message_fillers_detects_stopword_phrases():
    assert message_fillers("说真的，今天遇到一个超治愈的瞬间") == ["说真的"]
    assert message_fillers("我跟你讲，说真的这事挺逗") == ["我跟你讲", "说真的"]
    assert message_fillers("今天好累") == []


def test_bigram_ledger_was_blind_but_filler_ledger_sees_it():
    msgs = ["说真的，今天好累", "说真的我有点想你", "说真的这个不错", "嗯好的"]
    over = collect_overused(msgs, keyword_limit=3, filler_limit=2)
    # bigram 兜底抓不到（停用字切段）——这是事故的技术根因，钉住
    assert not any("真" in d["word"] and len(d["word"]) >= 2 for d in over.get("keywords", []))
    assert over["fillers"][0] == {"word": "说真的", "count": 3}
    hint = build_variety_hint(over, "zh")
    assert "「说真的」" in hint and "一个字都不要出现" in hint
    assert "讲真" in hint          # 连同义口头禅一起禁，防换皮


def test_filler_below_limit_not_reported():
    over = collect_overused(["说真的，好累", "嗯好的", "睡了"], filler_limit=2)
    assert "fillers" not in over


def test_sentence_level_template_repetition():
    msgs = [
        "哎，57天没聊了真不好意思，最近忙晕了",
        "你来啦！57天没聊了真不好意思～",
        "嗯嗯好的",
    ]
    over = collect_overused(msgs, sentence_limit=2)
    assert over["sentences"][0]["text"] == "57天没聊了真不好意思"
    assert over["sentences"][0]["count"] == 2
    hint = build_variety_hint(over, "zh")
    assert "同一句「57天没聊了真不好意思」" in hint
    assert "它的改写都不要再说" in hint


def test_message_clauses_normalises_and_filters_short():
    cs = message_clauses("好呀～ 明天见！  我们老地方等你哦（笑）")
    assert "我们老地方等你哦笑" in cs
    assert not any(len(c) < 6 for c in cs)


def test_fillers_rank_before_laugh_in_hint():
    msgs = ["说真的哈哈", "说真的哈哈哈", "哈哈"]
    over = collect_overused(msgs, laugh_limit=2, filler_limit=2)
    hint = build_variety_hint(over, "zh", max_items=1)
    assert "口头禅" in hint and "笑法" not in hint


def test_english_hint_for_fillers_and_sentences():
    over = {"fillers": [{"word": "说真的", "count": 3}],
            "sentences": [{"text": "sorry for the long silence", "count": 2}]}
    hint = build_variety_hint(over, "en")
    assert "filler phrases" in hint and "do not repeat" in hint


def test_parse_cfg_exposes_new_limits():
    cfg = parse_variety_cfg({"ai": {"reply_variety": {"enabled": True, "filler_limit": 1}}})
    assert cfg["filler_limit"] == 1 and cfg["sentence_limit"] == 2


def test_vivid_rewrite_prompt_no_longer_recommends_filler():
    p = build_colloquial_prompt(intensity="vivid")
    assert "允许加一点点主观口吻框架" not in p
    assert "不要加「说真的」" in p
