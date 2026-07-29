# -*- coding: utf-8 -*-
"""出站「已发断言」守卫门禁（P0-1，2026-07-29 AI 对练实证驱动）。

金标语料全部来自 tools/duel_runner_tg.py 真机对练里 su_wan 逃逸出的原话——
promise 词表抓不到的「完成/进行断言」正是「说发了没发」的主因。detect_media_claim
一律 gate 在 media_context（客户在索要媒体），无语境零误伤。
"""
from __future__ import annotations

import pytest

from src.ai.outbound_promise_guard import (
    detect_media_claim,
    detect_media_promise,
    strip_media_claims,
    wants_media,
)


# ── wants_media：客户索要媒体的判据 ─────────────────────────────────────────
@pytest.mark.parametrize("text,expect", [
    ("发张自拍我看看呗", "image"),
    ("有没有你店里的照片", "image"),
    ("你是不是本人？真人吗", "image"),
    ("你长啥样啊", "image"),
    ("send me a selfie", "image"),
    ("发条语音呗想听听你声音", "voice"),
    ("你唱首歌给我听听", "voice"),
    ("说句话我听听", "voice"),
    ("今天天气不错啊", ""),
    ("你好呀在吗", ""),
])
def test_wants_media(text, expect):
    assert wants_media(text) == expect


# ── 对练实证：这些完成/进行断言在 media_context 下必须命中 ────────────────────
_CLAIM_HITS = [
    "好啦好啦，这不就来了嘛，发到群里了你看看合不合心意",   # T2 双谎
    "刚发的那张就是在窗边拍的呀，手冲壶都入镜了，你再仔细看看",  # T3
    "我这次真的拍好啦，刚发的，你看看嘛",                    # T10
    "照片发你了哦",
    "自拍来啦～好看吗",
    "there you go, check it out",
    "I just sent you a pic",
]


@pytest.mark.parametrize("text", _CLAIM_HITS)
def test_claim_detected_with_context(text):
    # 客户在要图 → media_context=True → 断言=谎，必须抓
    assert detect_media_claim(text, media_context=True) == "image"


@pytest.mark.parametrize("text", _CLAIM_HITS)
def test_claim_silent_without_context(text):
    # 无索要语境 → 一律不判（防误伤评论对方图 / 普通闲聊）
    assert detect_media_claim(text, media_context=False) == ""


# ── 语音断言 ────────────────────────────────────────────────────────────────
def test_voice_claim():
    assert detect_media_claim("语音发你了", media_context=True) == "voice"
    assert detect_media_claim("刚给你录了条语音", media_context=True) in ("voice", "image")


# ── 不误伤：正常表达 / 评论对方的图 / 疑问 / 远期 ─────────────────────────────
@pytest.mark.parametrize("text", [
    "这张拍得真好看，你手机像素不错",     # 评论客户发来的图（media_context 由调用方控）
    "你这张自拍我好喜欢",                 # 同上
    "要不要我拍张给你看？",               # 疑问 offer（问号）
    "改天拍张海边的发你",                 # 远期承诺（EXCLUDES）
    "现在不方便拍照哦",                   # 否认
    "我发不了照片啦",                     # 否认
])
def test_no_false_positive_even_with_context(text):
    # 即便 media_context=True，疑问/远期/否认/评论也不该被当"已发断言"
    # （评论对方图这两条靠调用方 media_context=False 兜底；此处验疑问/远期/否认）
    got = detect_media_claim(text, media_context=True)
    if "要不要" in text or "改天" in text or "不方便" in text or "发不了" in text:
        assert got == "", f"误伤: {text!r} → {got}"


# ── strip：剥掉断言句、保留正常句 ────────────────────────────────────────────
def test_strip_claim_keeps_rest():
    # 真机 transcript 里子句以 \n / 。 分隔（逗号不切句，是既有设计粒度）
    text = "哈哈你要求真高。这不就来了嘛发到群里了。多聊聊嘛"
    out = strip_media_claims(text, media_context=True)
    assert "群里" not in out and "这不就来了" not in out
    assert "多聊聊" in out  # 正常句保留


def test_strip_noop_without_context():
    text = "这不就来了嘛你看看"
    assert strip_media_claims(text, media_context=False) == text


# ── promise 与 claim 正交：promise 词表不受影响（回归护栏）──────────────────
def test_promise_still_works():
    assert detect_media_promise("等我拍一张给你发过去") == "image"
    assert detect_media_promise("改天拍给你") == ""   # 远期仍豁免


# ── 粤语补漏（2026-07-29 对练 T9/T10 实证：Mandarin 词表漏，判官也 false-green）──
@pytest.mark.parametrize("text,kind", [
    ("而家即刻拍俾你睇", "image"),        # 将发（现在马上拍给你看）
    ("今日黄昏海边嗰张而家拍俾你", "image"),
    ("影张相畀你睇下", "image"),
])
def test_cantonese_promise(text, kind):
    assert detect_media_promise(text) == kind


@pytest.mark.parametrize("text", [
    "黄昏海边真係而家啱啱拍嘅",           # 已发断言（刚刚拍的）
    "影咗俾你喇",
])
def test_cantonese_claim_with_context(text):
    assert detect_media_claim(text, media_context=True) == "image"


def test_cantonese_wants_media():
    assert wants_media("影张相畀我睇") == "image"
    assert wants_media("睇下你真身系咪同头像一样") == "image"


@pytest.mark.parametrize("text", [
    "我而家好累想瞓觉",      # 而家=现在，但非媒体
    "你要俾心机做嘢",        # 俾心机=用心，非发媒体
])
def test_cantonese_no_false_positive(text):
    assert detect_media_promise(text) == ""
    assert detect_media_claim(text, media_context=True) == ""
