# -*- coding: utf-8 -*-
"""#143（0902 skuio 工单）门禁：贴纸轮 + 主动发图配文轮的语言锚。

三合一事故（WhatsApp 英文会话 viva Mexico，截图 6834964252_1013~1016）：
① 贴纸/图片一来 AI 就切中文——识图/贴纸描述恒为中文，混进上下文带偏回复语言；
② 我方主动发图的配文直接是中文（「手机里存的这张…」＝caption_album zh 池原句，
   lang 恒空 + LLM 配文被指示「跟随对方消息语言」而对方消息是系统中文标注）；
③ 表情包被当真实照片评论（贴纸识别产物冒充 [图片内容]，贴纸性在正文/历史丢失）。

#74 修的是「客户发照片」轮的语言锚（识图块标注勿跟随 + 语言证据剥离），
本单盖它的两个漏网口：贴纸轮、我方主动发图的配文轮——那轮的语义**不动**。
"""
from __future__ import annotations

import inspect

from types import SimpleNamespace


# ── ③ 贴纸识别产物前缀（贴纸性载体） ─────────────────────────────────────────

def test_media_desc_markers_include_sticker():
    from src.inbox.media_enrich import MEDIA_DESC_MARKERS, strip_media_desc
    assert "[贴纸内容]" in MEDIA_DESC_MARKERS
    assert strip_media_desc("看这个\n[贴纸内容] 一只卡通猫") == "看这个"


def test_fold_desc_sticker_uses_sticker_marker():
    from src.inbox.autodraft_helpers import _fold_image_desc, _media_desc_prefix
    assert _media_desc_prefix("sticker") == "[贴纸内容]"
    assert _media_desc_prefix("image") == "[图片内容]"
    assert _media_desc_prefix("") == "[图片内容]"

    hist = [{"role": "user", "content": "[贴纸]"}]
    last = _fold_image_desc("[贴纸]", hist, "一只卡通猫举着爱心",
                            kind="sticker")
    assert last == "[贴纸内容] 一只卡通猫举着爱心"
    assert hist[0]["content"] == "[贴纸内容] 一只卡通猫举着爱心"
    # 回解析必须还原贴纸性 → ai_client 媒体块走情绪分流而非照片评论
    from src.inbox.inbound_enrich import peer_media_context
    assert peer_media_context(last).get("_media_kind") == "sticker"


def test_fold_desc_image_behavior_unchanged():
    hist = [{"role": "user", "content": "[图片]"}]
    from src.inbox.autodraft_helpers import _fold_image_desc
    last = _fold_image_desc("[图片]", hist, "一只橘猫")
    assert last == "[图片内容] 一只橘猫"
    assert hist[0]["content"] == "[图片内容] 一只橘猫"


def test_kb_gate_recognizes_sticker_desc():
    from src.utils.kb_gate import is_media_desc_text
    assert is_media_desc_text("[贴纸内容] 一只卡通猫举着爱心")
    assert not is_media_desc_text("你们支持哪些通道")


def test_autodraft_rewrite_uses_kind_aware_prefix():
    """回写消息行/正文并入按媒体类型选前缀——静态接线钉（防回退硬编码 [图片内容]）。"""
    from src.inbox import autodraft_helpers as adh
    src = inspect.getsource(adh.enrich_auto_draft)
    assert "_media_desc_prefix(_peer_media_type)" in src
    assert 'kind=_peer_media_type' in src


# ── ① 贴纸轮语言（A 线 Stage 文案语言判定剥系统注入行） ──────────────────────

def _mk_sm(detect):
    from src.skills.skill_manager import SkillManager
    sm = SkillManager.__new__(SkillManager)
    sm.ai_client = SimpleNamespace(_detect_message_language=detect)
    return sm


def test_stage_lang_ignores_system_injected_lines():
    """贴纸/图片轮 text=系统中文标注 → 不得判成 zh，回落会话 reply_lang。"""
    calls = []

    def _detect(t):
        calls.append(t)
        return "zh" if any("\u4e00" <= c <= "\u9fff" for c in t) else "en"

    sm = _mk_sm(_detect)
    lang = sm._stage_lang({"reply_lang": "en"}, "[贴纸内容] 一只卡通猫举着爱心")
    assert lang == "en"
    assert calls == []          # 剥空＝纯媒体轮，检测器不应被喂系统标注
    # [表情]（A 线贴纸产物）同口径
    assert sm._stage_lang({"reply_lang": "en"}, "[表情] 笑哭了 · 一只猫") == "en"
    # 客户真实文字照常检测（行为不变）
    assert sm._stage_lang({}, "hello there my friend how are you") == "en"


# ── ② 配文轮语言（LLM 配文指令 + 固定池 + 链路接线） ─────────────────────────

def test_caption_instruction_pins_reply_lang():
    from src.ai.companion_selfie import build_photo_caption_instruction
    p = build_photo_caption_instruction(
        "send me a pic", kind="selfie", reply_lang="en")
    assert "英语" in p and "禁止使用其它语言" in p
    assert "相同的语言" not in p


def test_caption_instruction_strips_system_injected_peer_text():
    """贴纸轮的「对方消息」是系统中文标注：剥掉后不引用、语言按会话既有语言。"""
    from src.ai.companion_selfie import build_photo_caption_instruction
    p = build_photo_caption_instruction(
        "[贴纸内容] 一只卡通猫举着爱心", kind="selfie")
    assert "卡通猫" not in p            # 系统标注绝不冒充对方原话进指令
    assert "对方刚才的消息" not in p
    assert "会话一直在用的语言" in p


def test_caption_instruction_legacy_behavior_kept():
    from src.ai.companion_selfie import build_photo_caption_instruction
    p = build_photo_caption_instruction("send me a pic", kind="selfie")
    assert "使用与对方消息相同的语言" in p
    assert "对方刚才的消息：「send me a pic」" in p


def test_fixed_caption_pool_follows_lang():
    """lang=en → caption_album 英文池；lang 空 → 中文池（默认行为不变）。"""
    import re
    from src.inbox.image_autosend import KIND_SELFIE, _fixed_caption
    _CJK = re.compile(r"[\u4e00-\u9fff]")
    cap_en = _fixed_caption({}, KIND_SELFIE, "old", "en", chat_key="conv-en")
    assert cap_en and not _CJK.search(cap_en)
    cap_zh = _fixed_caption({}, KIND_SELFIE, "old", "", chat_key="conv-zh")
    assert cap_zh and _CJK.search(cap_zh)


def test_autosend_image_wires_conversation_lang():
    """B 线全自动发图必须解析会话语言并传入：lang（固定池/registry 配文）
    + reply_lang（LLM 配文指令）——静态接线钉。"""
    from src.inbox import autosend_helpers as ash
    src = inspect.getsource(ash.autosend_image)
    assert "resolve_reply_language" in src, "会话语言解析被摘（#143）"
    assert "lang=_conv_lang" in src, "固定池配文语言接线被摘（#143）"
    assert "reply_lang=_conv_lang" in src, "LLM 配文语言钉被摘（#143）"


# ── 出口兜底（#64 收口点罩上媒体配文） ───────────────────────────────────────

def test_send_media_caption_passes_sendpoint_guard():
    from src.integrations.account_orchestrator import AccountOrchestrator
    src = inspect.getsource(AccountOrchestrator.send_media)
    assert "sendpoint_lang_pin_fix" in src, "媒体配文语种兜底被摘（#143/#64）"
    assert "sendpoint_lang_mix_pass" in src, "媒体配文混语兜底被摘（#143/#97）"
    # HOLD 语义＝弃配文照发图（图语言无关；发错语言配文比无配文更糟）
    assert 'caption = ""' in src, "配文 HOLD 弃配文语义被摘（#143）"
    # 人工路径豁免不变量
    assert '"manual"' in src


# ── 贴纸块语言锚（ai_client 识图块） ─────────────────────────────────────────

def test_sticker_block_semantics_and_anchor():
    from src.ai.ai_client import AIClient
    c = AIClient.__new__(AIClient)
    c.config = SimpleNamespace(config={})
    p = c._build_context_prompt({
        "last_message": "[贴纸内容] 一只卡通猫举着爱心",
        "_peer_message_is_media": True, "_media_kind": "sticker",
        "_media_desc": "一只卡通猫举着爱心", "platform": "whatsapp",
    })
    # 工单验收语义：表达情绪、非生活照片、不追问出处、不当作本人照片
    assert "表达情绪" in p and "不是生活照片" in p
    assert "出处" in p and ("本人的照片" in p or "对方本人" in p)
    # 语言锚：中文参考语义不代表对方语言
    assert "语言不代表对方的语言" in p
