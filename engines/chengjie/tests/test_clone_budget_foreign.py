# -*- coding: utf-8 -*-
"""#102（实施91）「跟随翻译发声」外语合成预算门禁。

网关账实锤（0831 07:51，LINE Kevin 会话）：中文 22 字勾「跟随翻译发声→日语」
→ 日文译稿 33 字，服务端三连 200（28.0/19.9/17.6s），客户端按 #59 中文口径
（0.45s/字）的预算先到期 → TimeoutError ×3，成品全部白扔。

不变量：外语档（ja/ko 显式或假名/谚文实质占比）预算 ≥60s 地板 + 0.9s/字系数
——33 字最坏 28s 也留一次重试余量；中文/短文本旧行为一个数不变（预算只放宽
不收紧）；fast 档（edge 秒回）不受影响。
"""

from __future__ import annotations

from src.integrations.shared.tts_preview import (
    _is_foreign_slow_lang,
    clone_budget_sec,
)

# 事故原文（原图 852）与其日文译稿形态
INCIDENT_ZH = "我最近正在研究黄金投资，有没有兴趣一起加入？"
INCIDENT_JA = "最近、金投資について研究しているんですが、一緒に参加してみませんか？"


def test_incident_ja_budget_covers_observed_latency():
    # 33 字日文译稿 → 预算必须罩住实测最坏 28s + 重试余量
    budget = clone_budget_sec(INCIDENT_JA, lang="ja")
    assert budget >= 60.0, budget
    # 自判通道（不传 lang，按假名占比识别）同样成立——send-voice 手打日文同受益
    assert clone_budget_sec(INCIDENT_JA) >= 60.0


def test_explicit_lang_wins_even_for_latin_translation():
    # 「跟随翻译发声→韩语/日语」时目标语显式传入——译稿哪怕混拉丁也走外语档
    assert clone_budget_sec("a" * 30, lang="ko") >= 60.0
    assert clone_budget_sec("a" * 30, lang="ja-JP") >= 60.0


def test_zh_behavior_unchanged():
    # 中文口径一个数不变（#59 语义：0.45s/字+10s，地板 45）
    assert clone_budget_sec(INCIDENT_ZH) == 45.0
    assert clone_budget_sec("字" * 100) == 10.0 + 0.45 * 100
    assert clone_budget_sec("") == 45.0


def test_fast_lane_untouched():
    assert clone_budget_sec(INCIDENT_JA, fast=True, lang="ja") == 15.0


def test_cap_still_applies():
    assert clone_budget_sec("あ" * 400, lang="ja") == 190.0


def test_foreign_detection_narrow():
    assert _is_foreign_slow_lang("こんにちは、元気ですか", "") is True
    assert _is_foreign_slow_lang("좋은 아침이에요", "") is True
    # 中文夹一两个假名字符（顔文字/借词）不误判
    assert _is_foreign_slow_lang("今天天气不错哦～", "") is False
    assert _is_foreign_slow_lang("hello there my friend", "") is False
    assert _is_foreign_slow_lang("", "ja") is True
    assert _is_foreign_slow_lang("random", "en") is False


def test_budget_monotonic_vs_old_floor():
    # 外语档任何长度都不低于旧地板（预算只放宽不收紧的总原则）
    for n in (1, 10, 40, 80, 200):
        assert clone_budget_sec("あ" * n, lang="ja") >= 45.0
