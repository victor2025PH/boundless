# -*- coding: utf-8 -*-
"""Q-6 F 门禁（#263）：10 条要照片——没真发就不得声称发了（只许发图或拒绝）。"""
from __future__ import annotations

from src.inbox.media_gate import outbound_ok_for_photo_ask

# 客户要照片的 10 条（中英），配「没附图时的谎」与「诚实拒绝」。
_ASKS = [
    "send me a photo",
    "here's that view I promised you — wait where is it",
    "发张风景照",
    "给我看看你",
    "send a pic",
    "自拍一张",
    "got any photos of the beach",
    "把你的照片发我",
    "show me what you look like",
    "风景照来一张",
]

_LIES = [
    "just took this one for you—",
    "here it is, should be there",
    "maybe it's still loading",
    "let me try sending it again",
    "I thought it went through",
    "刚拍的发你了",
    "你看看这张",
    "在加载了等一下",
    "我再发一次忘了",
    "there it is babe",
]

_REFUSALS = [
    "I don't have a photo of that on me right now",
    "现在手头没有合适的风景照",
    "今天不太方便拍",
    "相册里没有这张",
    "maybe another time — I don't have one handy",
    "没有现成的",
    "这张我还没拍过",
    "sorry, no pic for that",
    "手头没有能发的",
    "I can't send that one",
]


def test_ten_photo_asks_lies_blocked_without_media():
    assert len(_ASKS) == 10 and len(_LIES) == 10
    for lie in _LIES:
        assert outbound_ok_for_photo_ask(lie, media_sent=False) is False, lie
        assert outbound_ok_for_photo_ask(lie, media_sent=True) is True, lie


def test_ten_honest_refusals_ok_without_media():
    assert len(_REFUSALS) == 10
    for line in _REFUSALS:
        assert outbound_ok_for_photo_ask(line, media_sent=False) is True, line
