# -*- coding: utf-8 -*-
"""投诉词表扩容 + 道歉螺旋检测门禁（P0-4，2026-07-29 对练实证驱动）。

审计实锤：detect_media_complaint 四句里只「骗人」命中，「没收到/你不是说/
打字算什么」全漏——是「连环解释」触发前提没被识别的根因。这里钉住新增的
lie_caught/distrust 类与 detect_apology_spiral。
"""
from __future__ import annotations

import pytest

from src.ai.companion_selfie import (
    detect_apology_spiral,
    detect_media_complaint,
)


@pytest.mark.parametrize("text,expect", [
    # lie_caught：说发没发 / 说话不算话 / 冒充
    ("我没收到啊，你发了吗？啥都没有", "lie_caught"),
    # 实施69 语义细分：地点/内容对不上 → content_mismatch（正确回应=如实带过，
    # 与 lie_caught 的「别编传输借口」纠偏不同）；催兑现 → unfulfilled（只计数，
    # 措辞交悬置常驻 hint）。两条金标随语义升级改钉。
    ("你不是说在海边吗？怎么在家里", "content_mismatch"),
    ("打字算什么唱歌", "lie_caught"),
    ("说话不算话，光说不做", "lie_caught"),
    ("照片呢？图呢", "unfulfilled"),
    ("I didn't get anything, nothing here", "lie_caught"),
    # distrust：整体失望
    ("算了吧，感觉你从头到尾都在敷衍我", "distrust"),
    ("太失望了，说的话没一句兑现", "distrust"),
    # 原有类保持
    ("又是这张，发过了", "repeat"),
    ("这是你吗？不像你啊", "not_you"),
    ("网图吧，假的", "fake"),
    ("你真会骗人", "fake"),
    # 正常消息不误伤
    ("今天天气真好呀", ""),
    ("你在忙什么呢", ""),
])
def test_detect_complaint_kinds(text, expect):
    assert detect_media_complaint(text) == expect


@pytest.mark.parametrize("text,expect", [
    # 螺旋：多个道歉/解释标记，或超长带解释
    ("哈哈被你抓到了，其实我今天先去了海边，然后回来车里歇了会儿，时间线有点乱不好意思", True),
    ("我错了我错了，真的拍过但那天店里有客人打断后来就忙忘了", True),
    ("被你发现了，我承认刚才有点端着", True),
    # 单个礼貌道歉不算螺旋
    ("不好意思久等啦", False),
    ("嗯好的", False),
    ("", False),
])
def test_apology_spiral(text, expect):
    assert detect_apology_spiral(text) is expect
