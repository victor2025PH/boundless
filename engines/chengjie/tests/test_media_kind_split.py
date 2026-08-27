# -*- coding: utf-8 -*-
"""实施74 阶段3 门禁（B117）：贴纸/表情包按消息类型分流，不走照片评论链。

事故（0826 _576 实录）：客户发贴纸，AI 当真实照片评论「这小朋友挺可爱，
亲戚家的？」。修复＝ai_client 媒体块按 kind 硬分流：sticker/animated_sticker/
gif → 情绪回应指令（识图描述只作语义参考），禁照片式评论；image/video 等
保持旧框架。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.inbox.inbound_enrich import peer_media_context


def _mk_ai(cfg=None):
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)  # 绕过 __init__ 重依赖，只测纯 prompt 构建
    c.config = SimpleNamespace(config=cfg or {})
    return c


def _prompt(ctx):
    return _mk_ai()._build_context_prompt(dict({"last_message": "看这个"}, **ctx))


# ── kind 识别（全平台占位 → sticker）────────────────────────────────────────

def test_sticker_kinds_recognized_across_platforms():
    for t in ("[贴纸]", "[表情] 笑哭了", "[LINE贴图] 开心",
              "[贴纸·happy] 举爱心", "[动态贴纸·love] 比心"):
        got = peer_media_context(t)
        assert got.get("_media_kind") in ("sticker", "animated_sticker"), t


# ── prompt 分流（_576 场景金标）─────────────────────────────────────────────

def test_sticker_with_vision_desc_gets_emotion_instruction():
    """贴纸带识图描述（卡通小孩）→ 情绪回应指令，绝不照片式框架。"""
    p = _prompt({
        "_peer_message_is_media": True, "_media_kind": "sticker",
        "_media_desc": "一个可爱的小朋友卡通形象在笑", "platform": "whatsapp",
    })
    assert "表情贴纸" in p and "不是真实照片" in p
    assert "回应对方此刻的情绪" in p
    assert "这是你拍的吗" in p  # 反例句显式进禁令
    # 不落照片评论框架
    assert "系统已识别对方发来的媒体内容如下" not in p
    # 识图描述仍作语义参考在场
    assert "参考语义" in p and "卡通形象" in p


def test_bare_sticker_gets_emotion_instruction():
    p = _prompt({"_peer_message_is_media": True, "_media_kind": "sticker",
                 "platform": "telegram"})
    assert "表情贴纸" in p and "回应对方此刻的情绪" in p


def test_gif_and_animated_sticker_stickerish():
    for kind in ("gif", "animated_sticker"):
        p = _prompt({"_peer_message_is_media": True, "_media_kind": kind})
        assert "回应对方此刻的情绪" in p, kind


def test_image_keeps_photo_comment_frame():
    """真实照片链保持旧框架（分流不误伤识图评论主链）。"""
    p = _prompt({
        "_peer_message_is_media": True, "_media_kind": "image",
        "_media_desc": "一个小朋友在公园玩滑梯",
    })
    assert "系统已识别对方发来的媒体内容如下" in p
    assert "表情贴纸" not in p


def test_video_keeps_thumbnail_frame():
    p = _prompt({
        "_peer_message_is_media": True, "_media_kind": "video",
        "_media_desc": "海边日落画面",
    })
    assert "缩略图" in p and "表情贴纸" not in p
