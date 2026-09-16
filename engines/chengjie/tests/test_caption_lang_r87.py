# -*- coding: utf-8 -*-
"""R87 #329b（3TCW5P）：日语会话配图文案发成中文——固定配文池 / Stage 语言判定按会话铆定语走。

真闸回放：十翼会话 ``[lang_pref] send=ja by=ui_select``（B67 显式铆定），客户用中文问「我可以看看你的照片吗」
→ A 线 ``_stage_lang`` 只看客户本句语种 → zh → ``caption_album`` 中文池「相册里躺了好久的一张，想起来给你看看」
→ 客户 12:14「为什么你突然说中文呢？」「你是中国人吗？」。

钉三层：
A. ``selfie_stage_text``：配文键（caption / caption_album / caption_object）在非 zh/en 会话语言下返回空串
   （图不配字 ≫ 配错语言）；搪塞 / 兜底句（no_photo / too_soon …）仍按旧口径回落英文。
B. ``image_autosend._fixed_caption`` 旧照口径透传语言 → ja 空配文；zh / en 照常。
C. ``SkillManager._stage_lang``：``_b67_pin`` 在场即返回铆定语（变体归 zh），不再被客户本句语种覆盖；
   process_message 每轮写 / 清 ``_b67_pin``；B 线 ``autosend_helpers`` 配文语言先问 ``outbound_lang_pin``。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.ai import companion_selfie as cs
from src.inbox import image_autosend as ia

_ROOT = Path(__file__).resolve().parents[1]
_CJK = re.compile(r"[\u4e00-\u9fff]")


# ── A ────────────────────────────────────────────────────────────────────────

def test_caption_pools_empty_for_unsupported_langs():
    for key in ("caption", "caption_album", "caption_object"):
        for lg in ("ja", "ko", "th", "vi", "id", "ja-JP"):
            assert cs.selfie_stage_text(key, lg, chat_key="c1") == "", (key, lg)
            assert cs.selfie_stage_text(key, lg, variant_salt=0) == "", (key, lg)


def test_caption_pools_still_serve_zh_and_en():
    zh = cs.selfie_stage_text("caption_album", "zh", chat_key="c1")
    en = cs.selfie_stage_text("caption_album", "en", chat_key="c1")
    assert zh and _CJK.search(zh)
    assert en and not _CJK.search(en)
    assert cs.selfie_stage_text("caption_album", "", chat_key="c1")            # 空＝中文默认（旧口径）
    assert cs.selfie_stage_text("caption_album", "zh-TW", variant_salt=1)
    assert cs.selfie_stage_text("caption", "en-US", variant_salt=1)


def test_non_caption_stage_texts_keep_english_fallback():
    en = cs.selfie_stage_text("no_photo", "ja", variant_salt=0)
    assert en and not _CJK.search(en)
    assert cs.selfie_stage_text("too_soon", "ko")
    assert cs.selfie_stage_text("promise_fail", "th")


# ── B ────────────────────────────────────────────────────────────────────────

def test_fixed_caption_old_photo_follows_lang():
    scfg = {}   # 运营未配 caption_album → 走池
    assert ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "ja", chat_key="k") == ""
    assert ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "ko", chat_key="k") == ""
    zh = ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "zh", chat_key="k")
    en = ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "en", chat_key="k")
    assert zh and _CJK.search(zh) and en and not _CJK.search(en)
    # 粤语归 zh（既有口径不动）
    assert ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "yue", chat_key="k")
    # 运营显式配了 caption_album → 原样（运营自己负责语种）
    assert ia._fixed_caption({"caption_album": "X"}, ia.KIND_SELFIE, "old", "ja", chat_key="k") == "X"


# ── C ────────────────────────────────────────────────────────────────────────

class _AI:
    def _detect_message_language(self, text):
        return "zh" if _CJK.search(text or "") else "en"


class _SM:
    """只借 SkillManager._stage_lang 的自由函数体，不构造整个 SkillManager。"""
    ai_client = _AI()


def test_stage_lang_prefers_b67_pin_over_message_lang():
    from src.skills.skill_manager import SkillManager
    f = SkillManager._stage_lang
    # 无铆定：客户中文 → zh（旧行为）
    assert f(_SM(), {"reply_lang": "ja"}, "我可以看看你的照片吗？") == "zh"
    # 铆「发→日」：客户中文也按 ja
    assert f(_SM(), {"reply_lang": "ja", "_b67_pin": "ja"}, "我可以看看你的照片吗？") == "ja"
    # 变体铆定归 zh
    assert f(_SM(), {"_b67_pin": "zh-tw"}, "hello") == "zh"
    assert f(_SM(), {"_b67_pin": "yue"}, "hello") == "zh"
    # 铆定撤了（键被清）→ 回到本句检测
    assert f(_SM(), {"reply_lang": "ja"}, "hello there") == "en"


def test_process_message_writes_and_clears_b67_pin():
    src = (_ROOT / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8", errors="ignore")
    assert 'user_context["_b67_pin"] = _b67_pin' in src
    assert 'user_context.pop("_b67_pin", None)' in src
    assert 'user_context.get("_b67_pin")' in src


def test_autosend_helpers_caption_lang_asks_pin_first():
    src = (_ROOT / "src" / "inbox" / "autosend_helpers.py").read_text(encoding="utf-8", errors="ignore")
    i_pin = src.index("outbound_lang_pin as _olp")
    i_res = src.index("_conv_lang = resolve_reply_language(_peer_text, _history")
    assert i_pin < i_res
    assert "reply_lang=_conv_lang" in src   # LLM 配文仍钉会话语言（#143）
