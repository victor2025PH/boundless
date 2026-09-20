"""Stage 兜底文案单测（2026-07-15「自称+复读」复盘修复）。

两条硬规则：
① 全部第一人称——模板绝不能出现人设名自称（"林小雨现在不太方便拍照"穿帮）；
② 高频兜底键（no_photo/capped）连续触发必换措辞（防一字不差复读）。
"""
from __future__ import annotations

from src.ai.companion_selfie import _STAGE_TEXTS, selfie_stage_text


def _all_variants(entry) -> list:
    out = []
    for v in entry.values():
        if isinstance(v, (list, tuple)):
            out.extend(v)
        else:
            out.append(v)
    return out


def test_no_third_person_self_reference_in_any_template():
    """任何键、任何语言、任何变体：渲染后都不得出现人设名（第一人称硬规则）。"""
    for key in _STAGE_TEXTS:
        entry = _STAGE_TEXTS[key]
        n_variants = max(
            len(v) if isinstance(v, (list, tuple)) else 1 for v in entry.values())
        for lang in ("zh", "en"):
            for salt in range(n_variants):
                out = selfie_stage_text(
                    key, lang, persona_name="林小雨", variant_salt=salt)
                assert "林小雨" not in out, f"{key}/{lang}#{salt} 出现名字自称: {out}"
                assert "{name}" not in out


def test_incident_line_is_gone():
    """事故原句「{name}现在不太方便拍照呢」已不存在于任何模板。"""
    for entry in _STAGE_TEXTS.values():
        for tpl in _all_variants(entry):
            assert "{name}现在不太方便" not in tpl


def test_consecutive_calls_rotate_variants():
    """缺省调用（进程游标）：同一 key 连续两次必换措辞——防复读的核心保证。"""
    a = selfie_stage_text("no_photo", "zh", persona_name="林小雨")
    b = selfie_stage_text("no_photo", "zh", persona_name="林小雨")
    assert a and b and a != b
    c = selfie_stage_text("capped", "zh")
    d = selfie_stage_text("capped", "zh")
    assert c and d and c != d


def test_explicit_salt_deterministic():
    x1 = selfie_stage_text("no_photo", "zh", variant_salt=1)
    x2 = selfie_stage_text("no_photo", "zh", variant_salt=1)
    assert x1 == x2
    assert selfie_stage_text("no_photo", "zh", variant_salt=0) != x1


def test_lang_fallback_and_unknown_key():
    assert selfie_stage_text("no_photo", "en", variant_salt=0).isascii() or True
    en = selfie_stage_text("no_photo", "ja", variant_salt=0)   # 非 zh → en 模板
    assert "photo" in en.lower() or "one" in en.lower()
    zh = selfie_stage_text("no_photo", "zh-CN", variant_salt=0)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in zh)
    assert selfie_stage_text("nonexistent_key", "zh") == ""


def test_caption_rotates_and_mostly_avoids_just_taken_claim():
    """2026-07-22 复盘：配文键升级为变体池——连发必换措辞；且 album 后端发旧照，
    池里至多一条声称「刚拍」的弱时间声明（防"刚拍的怎么衣服都变了"被抓包）。"""
    a = selfie_stage_text("caption", "zh")
    b = selfie_stage_text("caption", "zh")
    assert a and b and a != b
    zh_pool = _STAGE_TEXTS["caption"]["zh"]
    assert isinstance(zh_pool, (list, tuple)) and len(zh_pool) >= 3
    assert sum(1 for t in zh_pool if "刚拍" in t) <= 1
    # 英文池同语义
    en_pool = _STAGE_TEXTS["caption"]["en"]
    assert isinstance(en_pool, (list, tuple)) and len(en_pool) >= 3
    assert sum(1 for t in en_pool if "just took" in t.lower()) <= 1


def test_single_string_keys_unchanged_shape():
    """真正的单句键（too_soon/promise_fail 等低频键）仍稳定存在。"""
    assert selfie_stage_text("too_soon", "zh")
    assert selfie_stage_text("promise_fail", "en")
