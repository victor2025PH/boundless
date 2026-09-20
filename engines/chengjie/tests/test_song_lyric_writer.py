# -*- coding: utf-8 -*-
"""专属歌填词器契约（实施58 P2）：等长句式硬闸 + 素材约束 + 重试环。"""
from __future__ import annotations

from src.companion.song_lyric_writer import (
    build_lyric_prompt,
    validate_lyrics,
    write_custom_lyrics,
)

GOOD = "月亮爬上小窗台\n想起你说的海边\n吉他声慢慢响起\n陪你到星光满天"


def test_validate_good():
    ok, why, lines = validate_lyrics(GOOD)
    assert ok and why == "ok" and len(lines) == 4


def test_validate_line_count():
    ok, why, _ = validate_lyrics("只有一句七个字")
    assert not ok and why.startswith("line_count")


def test_validate_line_length_bounds():
    # 第二句 5 字（2026-08-23 夜实测：短句被换词重唱系统性吞掉）
    bad = "月亮爬上小窗台\n星星眨着眼\n吉他声慢慢响起\n陪你到星光满天"
    ok, why, _ = validate_lyrics(bad)
    assert not ok and why.startswith("line_len:5")
    too_long = GOOD.replace("月亮爬上小窗台", "月亮悄悄爬上了小窗台边")
    ok, why, _ = validate_lyrics(too_long)
    assert not ok and why.startswith("line_len")


def test_validate_sensitive_and_digits_and_dup():
    ok, why, _ = validate_lyrics(GOOD.replace("吉他声慢慢响起", "记得给我转账呀"))
    assert not ok and why.startswith("sensitive")
    ok, why, _ = validate_lyrics(GOOD.replace("吉他声慢慢响起", "电话13800138000"))
    assert not ok and why == "digits"
    ok, why, _ = validate_lyrics(
        "月亮爬上小窗台\n月亮爬上小窗台\n吉他声慢慢响起\n陪你到星光满天")
    assert not ok and why == "dup_line"


def test_validate_copyright_hint():
    ok, why, _ = validate_lyrics(GOOD.replace("吉他声慢慢响起", "一起听周杰伦呀"))
    assert not ok and why.startswith("copyright")


def test_prompt_contains_constraints_and_facts():
    p = build_lyric_prompt(persona_name="陈美玲", peer_name="阿泽",
                           facts=["我上周去了海边", "在学吉他"])
    assert "4 行" in p or "恰好 4" in p
    assert "6~9" in p
    assert "海边" in p and "吉他" in p and "阿泽" in p
    assert "亲口" in p          # 素材约束在场
    p2 = build_lyric_prompt(persona_name="陈美玲", peer_name="",
                            facts=[])
    assert "不要编造称呼" in p2


def test_write_retry_loop_feeds_back_reason():
    calls = []

    def fake_llm(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return "太短\n第二句也短\n三\n四"      # 会被闸打回
        return GOOD

    lyrics, meta = write_custom_lyrics(
        fake_llm, persona_name="陈美玲", peer_name="阿泽",
        facts=["去了海边"], tries=3)
    assert lyrics == GOOD
    assert meta["ok_attempt"] == 2
    assert "上一稿被打回" in calls[1]      # 失败原因回喂


def test_write_gives_up_after_tries():
    lyrics, meta = write_custom_lyrics(
        lambda p: "废稿", persona_name="x", peer_name="", facts=[], tries=2)
    assert lyrics is None
    assert len(meta["attempts"]) == 2
