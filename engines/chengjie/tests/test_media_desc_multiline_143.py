# -*- coding: utf-8 -*-
"""#143 C-补（0902）金标：多行识图描述漏过语言证据剥离——「切中文」真机制。

证据（tmp_diag/HXP9YD backend.log）：
  13347-13349 ``[AutoDraft] 图片识别补全: 1. 可见物体：一位白发老年男性…\\n\\n2. 无票据/订单/
  证件/表单/聊天记录`` ——VLM 按「简洁分条」多行落库；
  13328/13385 ``burst=en voted=zh``；13334/13339-13340/13351 英文原稿被 #106 铆定守卫与
  AIClient 语言守卫改成中文（pin/expected=zh）。
根因：``_MEDIA_DESC_LINE_RE`` 只剥带标记的首行，续行「2. 无票据…」以裸中文进证据链。

修向：① 写入侧三点（media_enrich / telegram_client / inbound_video）描述压单行；
② 读取侧兼容存量多行：标记行后的编号行/项目符号行/纯中文模板句一并剥；
③ 禁区：客户自己写的 caption（标记前段）仍是证据。
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.ai.lang_policy import (  # noqa: E402
    EvidenceStrength,
    classify_evidence,
    evidence_lang,
    strip_neutral_tokens,
    strip_system_injected,
)
from src.inbox.outbound_translate import (  # noqa: E402
    current_inbound_burst_lang,
    peer_language_hint,
    vote_language,
)

# 日志 13347-13349 原文（[AutoDraft] 图片识别补全 落库形态）
LOG_13347 = (
    "[图片内容] 1. 可见物体：一位白发老年男性、黑色西装外套、白色衬衫、红底带白点领带、"
    "背景为室内窗框结构（浅色墙面与深色边框）。\n\n2. 无票据/订单/证件/表单/聊天记录"
)
STICKER_MULTI = "[贴纸内容] 类型=C\n1. 一只卡通猫闭眼微笑\n\n2. 无票据/订单/证件/表单/聊天记录"


def _detect(text: str) -> str:
    from src.ai.translation_service import detect_language
    return detect_language(text)


# ── ② 读取侧金标 ─────────────────────────────────────────────────────────────

def test_log_13347_is_no_language_evidence():
    assert classify_evidence(LOG_13347) == ("", EvidenceStrength.NONE)
    assert strip_system_injected(LOG_13347) == ""
    assert strip_neutral_tokens(LOG_13347) == ""
    assert evidence_lang(LOG_13347) == ""


def test_sticker_multiline_is_no_language_evidence():
    assert classify_evidence(STICKER_MULTI) == ("", EvidenceStrength.NONE)
    assert strip_system_injected(STICKER_MULTI) == ""


def test_vote_language_does_not_count_multiline_desc():
    msgs = [
        {"direction": "in", "text": "hey what's up, how are you doing today?", "ts": 1},
        {"direction": "out", "text": "I'm good, thanks!", "ts": 2},
        {"direction": "in", "text": LOG_13347, "ts": 3},
    ]
    assert vote_language(msgs, detect=_detect) == "en"
    # 只剩多行描述一条 → 不投票（无证据），而非 zh
    assert vote_language([{"direction": "in", "text": LOG_13347, "ts": 3}],
                         detect=_detect) == ""


def test_current_inbound_burst_lang_skips_multiline_desc():
    msgs = [
        {"direction": "in", "text": "hello there", "ts": 1},
        {"direction": "out", "text": "hi!", "ts": 2},
        {"direction": "in", "text": LOG_13347, "ts": 3},
    ]
    assert current_inbound_burst_lang(msgs) == ""


def test_caption_before_marker_is_still_evidence():
    """禁区：客户自己写的 caption（标记前段）仍是证据，别一起剥。"""
    en = "Look at this old man in a suit!\n" + LOG_13347
    assert strip_system_injected(en) == "Look at this old man in a suit!"
    assert classify_evidence(en)[0] == "en"
    zh = "这是我爸\n" + LOG_13347
    assert strip_system_injected(zh) == "这是我爸"
    assert classify_evidence(zh) == ("zh", EvidenceStrength.STRONG)


def test_customer_text_merged_after_desc_is_kept():
    """burst 合并把客户正文接在描述后面（带字母文字）→ 不当续行剥。"""
    en = LOG_13347 + "\nhey did you see this picture?"
    assert strip_system_injected(en) == "hey did you see this picture?"
    assert classify_evidence(en)[0] == "en"
    ru = "[图片内容] 一只猫\nпривет как дела"
    assert strip_system_injected(ru) == "привет как дела"
    assert classify_evidence(ru)[0] == "ru"


def test_single_line_desc_behavior_unchanged():
    assert classify_evidence("[图片内容] 一只橘猫趴在沙发上，很可爱") == ("", EvidenceStrength.NONE)
    assert classify_evidence("[语音] I miss you so much today")[0] == "en"
    assert classify_evidence("hello there my friend how are you")[0] == "en"
    assert classify_evidence("你好呀朋友最近怎么样") == ("zh", EvidenceStrength.STRONG)


# ── ① 写入侧压单行 ────────────────────────────────────────────────────────────

def test_flatten_desc_line_semantics():
    from src.inbox.media_enrich import flatten_desc_line, parse_desc_type
    raw = "类型=C\n1. 可见物体：一位白发老年男性。\n\n2. 无票据/订单/证件/表单/聊天记录"
    out = flatten_desc_line(raw)
    assert "\n" not in out
    assert out == "类型=C 1. 可见物体：一位白发老年男性。 2. 无票据/订单/证件/表单/聊天记录"
    assert parse_desc_type(out) == ("C", "1. 可见物体：一位白发老年男性。 2. 无票据/订单/证件/表单/聊天记录")
    assert flatten_desc_line("a\nb\nc") == "a；b；c"          # 换行→「；」
    assert flatten_desc_line("单行描述") == "单行描述"
    assert flatten_desc_line("") == ""
    assert flatten_desc_line("x\r\ny") == "x；y"


@pytest.mark.asyncio
async def test_media_enrich_writes_single_line_desc(tmp_path):
    from src.inbox.media_enrich import enrich_inbound_media_text
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    multi = ("1. 可见物体：一位白发老年男性、黑色西装外套。\n\n"
             "2. 无票据/订单/证件/表单/聊天记录")
    with patch("src.inbox.media_enrich._resolve_local_path", return_value=str(img)), \
         patch("src.vision_client.has_any_vision_backend", return_value=True), \
         patch("src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
               new=AsyncMock(return_value=(multi, "ollama_ok"))):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": True}})
    assert "\n" not in desc and "\n" not in text
    assert text.startswith("[图片内容] 1. 可见物体")
    assert "2. 无票据" in text
    # 落库形态过语言证据链必须是「无证据」（修后的写入侧 + 读取侧双保险）
    assert classify_evidence(text) == ("", EvidenceStrength.NONE)
    # 贴纸同口径
    with patch("src.inbox.media_enrich._resolve_local_path", return_value=str(img)), \
         patch("src.vision_client.has_any_vision_backend", return_value=True), \
         patch("src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
               new=AsyncMock(return_value=("类型=C\n一只卡通猫\n举着爱心", "ollama_ok"))):
        text2, desc2 = await enrich_inbound_media_text(
            media_type="sticker", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": True}})
    assert text2 == "[贴纸内容] 类型=C 一只卡通猫；举着爱心"


def test_inbound_video_compose_flattens():
    from src.ai.inbound_video import compose_video_inbound_text
    out = compose_video_inbound_text(caption="", video_desc="画面：海边日落\n语音：无")
    assert out == "[视频内容] 画面：海边日落；语音：无"
    out2 = compose_video_inbound_text(caption="see this", video_desc="画面：海边\n语音：hi")
    assert out2 == "see this\n[视频内容] 画面：海边；语音：hi"
    assert strip_system_injected(out2) == "see this"


def test_telegram_get_image_content_flattens_wiring():
    """A 线写入点（telegram_client._get_image_content）接线钉。"""
    tg = (REPO / "src/client/telegram_client.py").read_text(encoding="utf-8")
    seg = tg.split("async def _get_image_content", 1)[1][:2200]
    assert "flatten_desc_line" in seg
    iv = (REPO / "src/ai/inbound_video.py").read_text(encoding="utf-8")
    assert "_single_line(visual_desc)" in iv and "_single_line(audio_text)" in iv


# ── 端到端：英文会话「贴纸 → 客户英文追问」序列 pin/hint=en ─────────────────────

class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, cid, limit=50):
        return list(self.rows)[-limit:]

    def get_conversation(self, cid):
        return {"language": ""}

    def get_outbound_lang_if_set(self, cid):
        return ""


def _en_sticker_sequence():
    return [
        {"direction": "in", "text": "hey, how's your day going?", "ts": 1},
        {"direction": "out", "text": "Pretty good! Just relaxing at home.", "ts": 2},
        {"direction": "in", "text": STICKER_MULTI, "ts": 3},              # 贴纸（多行描述）
        {"direction": "out", "text": "Haha that cat looks so sleepy.", "ts": 4},
        {"direction": "in", "text": LOG_13347, "ts": 5},                  # 图片（多行描述）
        {"direction": "in", "text": "again? you mean the cat sticker again?", "ts": 6},
    ]


def test_e2e_peer_language_hint_stays_en():
    store = _Store(_en_sticker_sequence())
    assert peer_language_hint(store, "whatsapp:acc:peer", detect=_detect) == "en"
    # 客户追问之前（末尾是两条媒体行）：burst 无证据 → 多数决仍 en，不得 voted=zh
    store2 = _Store(_en_sticker_sequence()[:-1])
    assert peer_language_hint(store2, "whatsapp:acc:peer", detect=_detect) == "en"


def test_e2e_lang_policy_resolution_stays_en():
    from src.ai.lang_policy import resolve_conversation_language
    hist = []
    for m in _en_sticker_sequence()[:-1]:
        hist.append({"role": "user" if m["direction"] == "in" else "assistant",
                     "content": m["text"]})
    d = resolve_conversation_language(
        "again? you mean the cat sticker again?", hist, prev_lang="en")
    assert d.lang == "en"
    # 媒体轮本身（当前消息是多行描述）也不能翻成 zh
    d2 = resolve_conversation_language(LOG_13347, hist[:-1], prev_lang="en")
    assert d2.lang == "en"


@pytest.mark.asyncio
async def test_e2e_sendpoint_hint_en_no_forced_translation(monkeypatch):
    """收口点（#106/#133）：hint=en → 英文原稿不与铆定冲突，翻译器绝不被调用。"""
    import src.integrations.protocol_bridge as pb
    from src.ai import sendpoint_guard as sg
    store = _Store(_en_sticker_sequence())
    monkeypatch.setattr(pb, "get_inbox_store", lambda: store)
    calls = []

    async def _translator(platform, account_id, chat_key, text):
        calls.append(text)
        return "你在说那个猫咪表情包吗？"

    old = sg._TRANSLATOR
    sg.set_sendpoint_translator(_translator)
    try:
        assert sg.outbound_peer_lang_hint("whatsapp", "acc", "peer") == "en"
        out, action = await sg.sendpoint_lang_pin_fix(
            "whatsapp", "acc", "peer", "Are you talking about that cat meme?")
    finally:
        sg.set_sendpoint_translator(old)
    assert out == "Are you talking about that cat meme?" and action == ""
    assert calls == []
