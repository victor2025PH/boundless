# -*- coding: utf-8 -*-
"""#333 视觉身份层门禁：识图 prompt v3 单源同步 + caption 字段解析 + 身份判定刻度。"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.companion import visual_identity as vi

_ROOT = Path(__file__).resolve().parents[1]


# ── prompt 单一事实源 ─────────────────────────────────────────────────────────

def test_prompt_contains_required_clauses_and_keeps_v2_type_contract():
    for clause in vi.PROMPT_REQUIRED_CLAUSES:
        assert clause in vi.INBOUND_IMAGE_PROMPT, clause
    # 旧 v1 文案（截图/OCR 导向）不得回潮
    assert "与聊天/文字相关" not in vi.INBOUND_IMAGE_PROMPT
    assert "最后一条对方消息大意" not in vi.INBOUND_IMAGE_PROMPT
    # 敏感描述维度刻意不要（身份归属交人脸嵌入层，不靠外貌词）
    assert "肤色" not in vi.INBOUND_IMAGE_PROMPT
    assert "种族" not in vi.INBOUND_IMAGE_PROMPT


def test_vision_client_default_prompt_is_v3():
    from src import vision_client
    assert vision_client.default_inbound_image_prompt() == vi.INBOUND_IMAGE_PROMPT
    # 首行契约仍被 media_enrich.parse_desc_type 认（前端徽标 / stats 消费）
    from src.inbox.media_enrich import parse_desc_type
    code, body = parse_desc_type("类型=C\n主体：自拍 人物：1 人，男性…")
    assert code == "C" and body.startswith("主体：")


def test_desktop_internal_seed_prompt_in_sync():
    cfg = yaml.safe_load((_ROOT / "config" / "config.desktop.internal.yaml").read_text(encoding="utf-8"))
    p = str(((cfg.get("vision") or {}).get("prompt")) or "")
    assert " ".join(p.split()) == " ".join(vi.INBOUND_IMAGE_PROMPT.split())


# ── caption 字段解析 ─────────────────────────────────────────────────────────

def test_parse_v3_selfie_caption():
    cap = ("类型=C 主体：自拍 人物：1 人，男性，30 岁左右，红发、有胡须，微笑，正对镜头自拍。"
           "场景：室内，木质墙面，贴着几张纸，暖光。")
    f = vi.parse_caption_fields(cap)
    assert f["kind"] == "C" and f["subject"] == "自拍"
    assert f["has_person"] and f["is_selfie"] and f["person_count"] == 1
    assert "红发" in f["people"] and "木质墙面" in f["scene"]
    assert vi.observation_summary(f).startswith("自拍；")


def test_parse_v3_no_person_caption():
    f = vi.parse_caption_fields("类型=C 主体：物品 人物：无 场景：一堆工业生产的金属零件放在桌上，白光。")
    assert f["subject"] == "物品" and not f["has_person"] and not f["is_selfie"]
    assert f["person_count"] == 0


def test_parse_v3_group_caption():
    f = vi.parse_caption_fields("类型=C 主体：多人 人物：三人，两女一男，年轻，户外合影，非自拍。场景：海边。")
    assert f["subject"] == "多人" and f["has_person"] and f["person_count"] == 3
    assert not f["is_selfie"]


def test_parse_legacy_v1_cameron_caption_still_yields_person():
    # 1.0.86 实录（旧 v1 prompt 产出）：无字段，只能靠关键词兜底
    cap = ("这张图片是一张自拍照，并非聊天或文字截图，因此没有“对方消息”可描述。 "
           "图中是一位红发、有胡须的男性，他正对着镜头微笑。背景是室内环境，可以看到窗户和窗帘。")
    f = vi.parse_caption_fields(cap)
    assert f["kind"] == "" and f["subject"] == ""
    assert f["has_person"] and f["is_selfie"] and f["person_count"] == 1


def test_parse_empty_and_garbage_never_raise():
    assert vi.parse_caption_fields("")["has_person"] is False
    assert vi.parse_caption_fields(None)["subject"] == ""
    assert vi.parse_caption_fields(12345)["kind"] == ""


# ── R7775K 降级话术口径 ───────────────────────────────────────────────────────

def test_honest_ask_pool_acknowledges_and_never_asks_to_redescribe():
    """识图超时/失败的降级话术：认收 + 自己这边没加载完 + 等下再看；不反问客户「你发的
    是什么」，不被盲断言闸误拦，也不被 sent-claim（「我发的图还在加载」）误剥。"""
    import re
    from src.inbox import media_enrich as me
    from src.ai.outbound_promise_guard import detect_sent_claim
    from src.ai.outbound_text_guard import strip_system_labels
    redescribe = re.compile(
        r"描述一下|说说拍的|你发的是什么|发的是啥|what did you send|what'?s in it|tell me what|"
        r"what is it\?", re.I)
    for lang, pool in me._HONEST_ASK_POOL.items():
        assert len(pool) >= 3, lang
        for s in pool:
            assert not redescribe.search(s), s
            assert me.blind_image_assertion(s) is False, s          # 诚实标记在
            assert detect_sent_claim(s) == "", s                       # 不当假声明
            assert strip_system_labels(s) == (s, []), s                # 无方括号标签
            assert "显示不出来" not in s and "not showing" not in s.lower(), s


# ── 身份判定 ─────────────────────────────────────────────────────────────────

def test_classify_identity_bands():
    assert vi.classify_identity({}, has_face=False) == ("no_face", "", 0.0)
    assert vi.classify_identity({"persona": 0.62, "customer_self": 0.12})[0] == "persona"
    lab, key, sc = vi.classify_identity({"persona": 0.1, "customer_self": 0.55})
    assert (lab, key) == ("customer_self", "customer_self") and sc == pytest.approx(0.55)
    lab, key, _ = vi.classify_identity({"persona": 0.1, "customer_self": 0.2, "妹妹": 0.57})
    assert (lab, key) == ("known", "妹妹")
    # 疑似区：不下结论但带回最接近的键（调用方可据此追问「是不是你？」）
    lab, key, _ = vi.classify_identity({"customer_self": 0.42})
    assert lab == "unknown" and key == "customer_self"
    # 176 实测：不同人设生成脸 impostor 最高 0.455 → 必须落在「疑似/不同人」，绝不判同一人
    lab, _, _ = vi.classify_identity({"persona": 0.455})
    assert lab == "unknown"
    # 明显不同人：连键都不给
    lab, key, _ = vi.classify_identity({"customer_self": 0.1, "persona": 0.05})
    assert lab == "unknown" and key == ""
    # 坏值不抛
    assert vi.classify_identity({"persona": "x", "customer_self": None})[0] == "unknown"


def test_identity_note_wording_has_no_bracket_labels():
    for lab in vi.LABELS:
        note = vi.identity_note(lab, matched="妹妹", relation="妹妹", who="TA")
        assert "[" not in note and "【" not in note, lab
    assert "不要猜" in vi.identity_note("unknown")
    assert "本人" in vi.identity_note("customer_self", confirmed=True)
    assert "人设" in vi.identity_note("persona")
    assert "妹妹" in vi.identity_note("known", relation="妹妹")
    assert vi.identity_note("no_face") == "" and vi.identity_note("") == ""
