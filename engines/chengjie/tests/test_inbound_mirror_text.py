# -*- coding: utf-8 -*-
"""B29（实施49 2026-08-21）：坐席台入站镜像正文口径（`_inbound_mirror_text`）。

内测实录：客户发纯 emoji（😍 / 😝），坐席气泡显示「[表情] 花痴 / 眯眯吐舌」
文字占位——那是 ``annotate_inbound_emoji`` 给 AI 媒体块解析的注解（其 docstring
自己写明「加注不许进存储正文」），镜像层应还原客户原始 emoji。

钉住的不变量：
- 纯 emoji 注解 + 有原始文本 → 镜像用原始 emoji（AI 层 text 不动）；
- 贴纸（media_type=sticker，message.text 为空）→ 注解原样（图渲染由 media_ref 走）；
- 语音转写剥前缀（P1-2 既有语义回归钉）；
- 普通文本 / 混合文本一律原样。
"""

from src.client.telegram_client import _inbound_mirror_text


def test_pure_emoji_mirror_restores_raw():
    assert _inbound_mirror_text("", "[表情] 花痴", "😍😍") == "😍😍"
    assert _inbound_mirror_text("", "[表情] 眯眯吐舌", "😝") == "😝"


def test_sticker_keeps_annotation_for_media_render():
    # 贴纸：message.text 为空 → 注解保留（media_type=sticker 的图渲染走 media_ref）
    assert _inbound_mirror_text("sticker", "[表情] 花痴", "") == "[表情] 花痴"


def test_emoji_annotation_without_raw_keeps_annotation():
    # 异常形态（拿不到原文）→ 保注解，不产出空正文
    assert _inbound_mirror_text("", "[表情] 花痴", "") == "[表情] 花痴"


def test_voice_transcript_prefix_stripped():
    assert _inbound_mirror_text("voice", "[语音转录] 你好", "") == "你好"


def test_plain_and_mixed_text_untouched():
    assert _inbound_mirror_text("", "Haha 🤣", "Haha 🤣") == "Haha 🤣"
    assert _inbound_mirror_text("", "你好", "你好") == "你好"
    assert _inbound_mirror_text("image", "[图片内容] 一只猫", "") == "[图片内容] 一只猫"
