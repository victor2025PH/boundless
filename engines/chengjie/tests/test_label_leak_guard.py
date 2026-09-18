# -*- coding: utf-8 -*-
"""系统标签泄漏（2026-09-12「[我方语音消息]」事故）三层防线门禁。

事故链（zhiliao whatsapp:639270135480:639273815533，20:43–20:50，4 例实锤）：
normalize_history 给所有出站语音行加带内标签「[我方发出的语音] 念稿」→ 连续十轮
语音后 LLM 照抄「[我方发出的语音]」→ 改写「[我方语音消息]」「[语音消息来自我们这边]」
→ 翻译「[Voice message from our side]」；文本直发客户，英文那条被 TTS 念出。

三层：① 源头——normalize_history 有正文的出站媒体行不再带带内标签（形态带外）；
② 出稿口——apply_outbound_text_guard / presynth_text_guard 剥系统标签；
③ 巡检——label_leak_scan 按落库行兜底（watchdog + 清理工具同一判定）。
"""
from __future__ import annotations

import pytest

from src.ai.outbound_text_guard import (
    apply_outbound_text_guard,
    guard_stats,
    is_system_label,
    leading_tag,
    strip_system_labels,
)

# 生产实锤原文（一字不改）
_LEAKS = [
    "[我方发出的语音] 对呀，就是英语。\n不过这边因为华人多，偶尔还能听到粤语呢。",
    "[Voice message from our side] Um... I actually can't really speak Cantonese.",
    "[我方语音消息] 天哪，我这边说英语。我能听懂粤语，但一开口说就全错了。",
    "[语音消息来自我们这边] 嗯...怎么说呢，我一开口，就变成英语了。",
]
_CLEAN = [
    "对呀，就是英语。\n不过这边因为华人多，偶尔还能听到粤语呢。",
    "Um... I actually can't really speak Cantonese.",
    "天哪，我这边说英语。我能听懂粤语，但一开口说就全错了。",
    "嗯...怎么说呢，我一开口，就变成英语了。",
]


@pytest.mark.parametrize("leak,clean", list(zip(_LEAKS, _CLEAN)))
def test_strip_system_labels_incident_texts(leak, clean):
    out, hits = strip_system_labels(leak)
    assert out == clean
    assert len(hits) == 1 and hits[0].startswith(("[", "【"))
    assert not out.lstrip().startswith("[")


def test_strip_system_labels_history_markers_and_full_width():
    # trim_stale_history / 补收标记 / 全角括号 / 尾标签 / 连剥
    assert strip_system_labels("[3天前] 你好呀")[0] == "你好呀"
    assert strip_system_labels("[补收的历史消息，时间不详] 在吗")[0] == "在吗"
    assert strip_system_labels("【我方发出的图片】好看吧")[0] == "好看吧"
    # 中间的描述标记只剥标签本身（守卫不判后面哪些字是元数据——宁可留字不吞正文）
    assert strip_system_labels("好看吧 [图片内容] 海边微笑")[0] == "好看吧 海边微笑"
    assert strip_system_labels("好看吧 [图片内容]")[0] == "好看吧"
    assert strip_system_labels("[我方发出的语音][3天前] 早")[0] == "早"
    # 中间：只剥词表命中的
    out, hits = strip_system_labels("先看这张 [我方发出的图片] 再说 [iPhone] 的事")
    assert out == "先看这张 再说 [iPhone] 的事" and len(hits) == 1
    # 句首裸占位（LLM 不该输出的镜像占位族）
    assert strip_system_labels("[语音] 晚安呀")[0] == "晚安呀"
    assert strip_system_labels("[图片] 刚拍的")[0] == "刚拍的"


def test_strip_system_labels_332_english_placeholders():
    """#332 Cameron 实录（1.0.86，2026-09-17 00:56/00:59）：LLM 抄历史占位
    「[我方发出的图片] [图片]」，翻译后成「[Image sent by me] [Image]」。
    英文裸占位 [Image]/[Photo]/[Voice] 与中文 [图片] 同属系统标注，必须一起剥。"""
    out, hits = strip_system_labels(
        "[我方发出的图片] [图片] Just took this one, lazy night in 😉")
    assert out == "Just took this one, lazy night in 😉" and len(hits) == 2
    out, hits = strip_system_labels(
        "[Image sent by me] [Image] There's my face, now you owe me something 😜")
    assert out == "There's my face, now you owe me something 😜" and len(hits) == 2
    for tag in ("[Image]", "[Photo]", "[Voice]", "[Video]", "[Sticker]", "[image x2]", "[GIF]"):
        assert strip_system_labels(f"{tag} here you go")[0] == "here you go", tag
    # 人话里的方括号英文名词短语不动
    assert strip_system_labels("the [image quality] is great")[0] == "the [image quality] is great"
    assert strip_system_labels("see [Figure 2]")[0] == "see [Figure 2]"


def test_strip_system_labels_no_false_positive():
    keep = [
        "我买了新手机[iPhone]，挺好用",
        "[笑] 你太逗了",                      # 表情/旁白不归本守卫（旁白守卫另管）
        "今天 [Monday] 有点忙",
        "这个价格（含税）是 99",
        "OK, deal [handshake]",
    ]
    for t in keep:
        out, hits = strip_system_labels(t)
        assert out == t and hits == [], t
    # 无括号的快路径
    assert strip_system_labels("纯文本") == ("纯文本", [])
    assert strip_system_labels("") == ("", [])


def test_strip_system_labels_label_only_reply_falls_back_to_source():
    src = "[我方语音消息]"
    out, hits = strip_system_labels(src)
    assert out == src and hits == [src]          # 清成空不吞回复，交调用方空稿语义


def test_peel_leading_system_label_only_leading():
    from src.ai.outbound_text_guard import peel_leading_system_label
    # 句首标签后的空白被 lead 正则一起吃掉
    assert peel_leading_system_label("[我方语音消息] 天哪") == "天哪"
    assert peel_leading_system_label("正常") is None
    # 出稿口会剥 [图片内容]；归一层 strip_out_label 才保留信息标记
    assert peel_leading_system_label("[图片内容] 海边") == "海边"


def test_is_system_label_and_leading_tag():
    assert is_system_label("我方语音消息")
    assert is_system_label("Voice message from our side")
    assert is_system_label("语音消息来自我们这边")
    assert is_system_label("语音")                       # 裸占位族
    assert is_system_label("语音×3")
    assert is_system_label("2 days ago")
    assert not is_system_label("iPhone")
    assert not is_system_label("笑")
    assert not is_system_label("")
    assert leading_tag("[我方语音消息] 天哪") == "我方语音消息"
    assert leading_tag("  【系统提示】x") == "系统提示"
    assert leading_tag("天哪 [我方语音消息]") == ""
    assert leading_tag("") == ""


def test_apply_outbound_text_guard_reports_hits_and_counts():
    before = guard_stats().get("system_label", 0)
    out, meta = apply_outbound_text_guard(_LEAKS[2])
    assert out == _CLEAN[2]
    assert meta["system_label_hits"] == ["[我方语音消息]"]
    assert guard_stats()["system_label"] == before + 1
    # 开关可关
    out2, meta2 = apply_outbound_text_guard(
        _LEAKS[2], {"enabled": True, "system_label": False, "monologue": False,
                    "lang_mix": False, "unfounded_recall": False,
                    "recall_grounding": False, "apology_dedup": False,
                    "shared_past": False, "goal_meta": False, "degenerate": False})
    assert out2 == _LEAKS[2] and "system_label_hits" not in meta2


def test_presynth_text_guard_strips_before_tts_but_not_for_interactive():
    from src.ai.sendpoint_guard import presynth_text_guard, sendpoint_guard_stats
    before = sendpoint_guard_stats().get("voice_system_label", 0)
    out = presynth_text_guard(_LEAKS[1], persona_id="lin_jiaxin", config={})
    assert not out.lstrip().startswith("[")
    assert "Cantonese" in out
    assert sendpoint_guard_stats().get("voice_system_label", 0) == before + 1
    # 坐席手打逐字链「所打即所念」整体豁免
    assert presynth_text_guard(_LEAKS[1], config={}, interactive=True) == _LEAKS[1]


def test_normalize_history_ten_voice_turns_produce_no_bracket_assistant_rows():
    """事故形态复刻：连续十轮出站语音 → assistant 内容零方括号开头（源头防线）。"""
    from src.inbox.persona_reply import media_form_note, normalize_history
    msgs = []
    for i in range(10):
        msgs.append({"direction": "in", "text": f"客户第{i}句", "media_type": "voice"})
        msgs.append({"direction": "out", "text": f"人设第{i}句", "media_type": "voice",
                     "media_ref": f"/static/protocol_media/whatsapp/out_{i}.ogg"})
    hist, last = normalize_history(msgs)
    asst = [r for r in hist if r["role"] == "assistant"]
    assert len(asst) == 10
    assert all(not r["content"].startswith(("[", "【")) for r in asst)
    assert all(r.get("media") == "voice" for r in asst)
    assert last == "客户第9句"
    note = media_form_note(hist)
    assert note.startswith("【消息形态说明】") and "10 条是以语音发出的" in note
    assert "方括号" in note


def test_normalize_history_strips_persisted_incident_prefix():
    from src.inbox.persona_reply import normalize_history, strip_out_label
    assert strip_out_label("[我方发出的语音] 对呀") == "对呀"
    assert strip_out_label("[我方发出的图片][我方发出的图片] x") == "x"
    assert strip_out_label("正常") == "正常"
    # 镜像占位（image_autosend 写「[图片] 配文」/ 旧语音行「[语音]×2 念稿」）同样剥，
    # 「[图片内容] 描述」信息标记保留
    assert strip_out_label("[图片] 刚拍的") == "刚拍的"
    assert strip_out_label("[语音]×2 晚安呀") == "晚安呀"
    assert strip_out_label("[图片内容] 海边微笑") == "[图片内容] 海边微笑"
    # 事故改写/翻译形态：纯文本行也必须剥（旧实现只扫媒体行 + 窄正则）
    assert strip_out_label(_LEAKS[2]) == _CLEAN[2]
    assert strip_out_label(_LEAKS[1]) == _CLEAN[1]
    assert strip_out_label(_LEAKS[3]) == _CLEAN[3]
    hist2, _ = normalize_history([
        {"direction": "out", "text": "[图片] 刚拍的", "media_type": "image", "media_ref": "/a.jpg"},
    ])
    assert hist2[0]["content"] == "刚拍的" and hist2[0]["media"] == "image"
    hist, _ = normalize_history([
        {"direction": "out", "text": _LEAKS[0], "media_type": "voice", "media_ref": "/x.ogg"},
    ])
    assert hist[0]["content"] == _CLEAN[0]
    hist_txt, _ = normalize_history([
        {"direction": "out", "text": _LEAKS[2]},
        {"direction": "out", "text": _LEAKS[1]},
    ])
    assert hist_txt[0]["content"] == _CLEAN[2]
    assert hist_txt[1]["content"] == _CLEAN[1]


def test_apply_draft_humanize_strips_system_label():
    from src.inbox.outbound_humanize import apply_draft_humanize
    out, meta = apply_draft_humanize(_LEAKS[2], conversation_id="c", lang="zh", cfg_root={})
    assert out == _CLEAN[2]
    assert meta.get("system_label")


def test_translated_lang_mix_also_strips_system_label():
    """关 lang_mix 时仍剥译文里的系统标签（事故英文形态）。"""
    from src.inbox.autosend_helpers import _guard_translated_lang_mix

    class _A:
        class config:
            config = {"companion": {"outbound_text_guard": {"lang_mix": False}}}

        class logger:
            @staticmethod
            def warning(*_a, **_k):
                pass

            @staticmethod
            def debug(*_a, **_k):
                pass

    out = _guard_translated_lang_mix(_A(), "天哪", _LEAKS[1])
    assert out == _CLEAN[1]


def test_translated_mix_strips_sent_claim_after_label():
    """#332：译文剥完 [Image sent by me] 后，无图配文「Just took this」一并剥掉。"""
    from src.inbox.autosend_helpers import _guard_translated_lang_mix

    class _A:
        class config:
            config = {"companion": {"outbound_text_guard": {"lang_mix": False}}}

        class logger:
            @staticmethod
            def warning(*_a, **_k):
                pass

            @staticmethod
            def debug(*_a, **_k):
                pass

    src = "[我方发出的图片] [图片] Just took this one. How was your day?"
    tx = "[Image sent by me] [Image] Just took this one. How was your day?"
    out = _guard_translated_lang_mix(_A(), src, tx)
    assert "Just took" not in out
    assert "Image sent" not in out
    assert "How was your day" in out


def test_safe_voice_inbox_text_strips_label_keeps_plain():
    from src.inbox.autosend_helpers import _safe_voice_inbox_text
    assert _safe_voice_inbox_text(_LEAKS[1]) == _CLEAN[1]
    assert _safe_voice_inbox_text("晚安呀") == "晚安呀"


def test_label_leak_scan_classification():
    from src.inbox.label_leak_scan import classify_outbound_text
    # 事故行：语音镜像行带标签 / 纯文本行带标签
    assert classify_outbound_text(_LEAKS[0], media_type="voice", sent_by="ai") == "system_label"
    assert classify_outbound_text(_LEAKS[2], media_type="", sent_by="ai") == "system_label"
    assert classify_outbound_text(_LEAKS[1], media_type="voice", sent_by="ai") == "system_label"
    # 合法镜像占位（媒体行正文口径）不算泄漏
    assert classify_outbound_text("[图片] 刚拍的", media_type="image", sent_by="ai") == ""
    assert classify_outbound_text("[语音]×2 晚安", media_type="voice", sent_by="") == ""
    assert classify_outbound_text("[图片内容] 海边微笑", media_type="image", sent_by="ai") == ""
    assert classify_outbound_text("刚拍的\n[图片内容] 海边微笑", media_type="image", sent_by="ai") == ""
    # 人工广播（【智聊 ChatX 1.0.84 已发布】）不算
    assert classify_outbound_text("【智聊 ChatX 1.0.84 已发布】…", media_type="", sent_by="") == ""
    assert classify_outbound_text("【钧机 v1.2】", media_type="document", sent_by="") == ""
    # AI 出站以任何非占位方括号开头 → 低级别可疑（巡检口径宁可多报）
    assert classify_outbound_text("[自动秒发] 你好", media_type="", sent_by="ai") == "bracket_prefix"
    assert classify_outbound_text("[自动秒发] 你好", media_type="", sent_by="") == ""
    assert classify_outbound_text("你好", media_type="", sent_by="ai") == ""
