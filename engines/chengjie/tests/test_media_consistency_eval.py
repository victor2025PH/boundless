# -*- coding: utf-8 -*-
"""图文一致性评测（``src/eval/media_consistency_eval.py``）门禁。

四类硬违规的确定性校验器 + 金标语料回归网 + **探测器有效性自证**
（篡改金标必 FAIL——评测不是摆设，bazi_chart_eval 同款纪律）。
"""
import pytest

from src.eval.media_consistency_eval import (
    check_media_consistency,
    evaluate_media_consistency,
    format_media_consistency_report,
)


# ── check_media_consistency：四类违规逐一验证 ────────────────────────────────
def test_deny_with_photo_caught():
    v = check_media_consistency(
        "等我现在去拍一张？等我一下～", photo_sent=True, scene="in a cafe")
    assert not v["ok"] and "deny_with_photo" in v["violations"]


def test_claim_without_photo_caught():
    v = check_media_consistency("自拍来啦！好看吗", photo_sent=False)
    assert not v["ok"] and "claim_without_photo" in v["violations"]
    v2 = check_media_consistency("here's a selfie for you", photo_sent=False)
    assert not v2["ok"]


def test_scene_mismatch_only_on_strong_claim():
    # 强断言"我在健身房" vs 照片在海边 → 违规
    v = check_media_consistency(
        "我在健身房呢刚练完", photo_sent=True,
        scene="at the beach, sea in the background")
    assert not v["ok"] and "scene_mismatch" in v["violations"]
    # 愿望句（"改天陪你去海边"）不算断言 → 合法
    v2 = check_media_consistency(
        "改天陪你去海边呀", photo_sent=True,
        scene="at home on the couch")
    assert v2["ok"]
    # 场景一致 → 合法
    v3 = check_media_consistency(
        "我在咖啡厅呢～", photo_sent=True,
        scene="in a cozy cafe, holding a coffee cup")
    assert v3["ok"]
    # home/bedroom 互容（卧室也是家）
    v4 = check_media_consistency(
        "在家躺着呢", photo_sent=True, scene="in the bedroom, soft warm light")
    assert v4["ok"]


def test_time_mismatch_uses_phase19_words():
    v = check_media_consistency(
        "刚拍的", photo_sent=True,
        scene="campus walkway, afternoon light", hour=3)
    assert not v["ok"] and "time_mismatch" in v["violations"]
    # hour 未提供 → 跳过时间检查
    v2 = check_media_consistency(
        "刚拍的", photo_sent=True, scene="campus walkway, afternoon light")
    assert v2["ok"]


def test_photo_sent_flips_semantics():
    # 同一句「这是刚拍的」：有图=真话，无图=谎话
    t = "这是刚拍的，给你看～"
    assert check_media_consistency(t, photo_sent=True)["ok"]
    assert not check_media_consistency(t, photo_sent=False)["ok"]


def test_normal_chat_untouched():
    for t in ("宝贝想我了没？", "哈哈你太逗了", "let's chat more~"):
        assert check_media_consistency(t, photo_sent=False)["ok"]
        assert check_media_consistency(t, photo_sent=True)["ok"]


def test_past_reference_not_claim():
    # 谈论以前发过的照片 ≠ 断言本条附图（无图也合法）
    assert check_media_consistency(
        "上次刚拍的那张你还留着吗", photo_sent=False)["ok"]


# ── #171：英文过去时假声明（2026-09-05 WhatsApp Mizuki→John 实录必红）──────────
@pytest.mark.parametrize("text", [
    "Oh, sorry — I just sent it, you should have it now.",       # 实录①
    "Hmm, that's weird — let me try sending it again for you.",  # 实录②
    "I already sent you the photo, check again",
    "did you get the photo?",
    "我刚发给你了呀，你收到了吗",
    "我再发一次哈",
])
def test_sent_claim_without_photo_caught(text):
    v = check_media_consistency(text, photo_sent=False)
    assert not v["ok"] and "claim_without_photo" in v["violations"], text
    # 同一句真附了图 → 真话
    assert check_media_consistency(text, photo_sent=True)["ok"], text


@pytest.mark.parametrize("text", [
    "I didn't send anything yet, hold on",   # 诚实否认
    "I sent it yesterday, remember?",        # 远过去
    "我没发呀，等我一下",
])
def test_sent_claim_denial_and_past_not_flagged(text):
    assert check_media_consistency(text, photo_sent=False)["ok"], text


def test_sent_claim_tamper_selfproof():
    bad = [{"id": "tamper_sc",
            "text": "Oh, sorry — I just sent it, you should have it now.",
            "photo_sent": False, "expect_ok": True}]
    assert evaluate_media_consistency(bad)["passed"] is False


# ── 金标语料 + 探测器自证 ────────────────────────────────────────────────────
def test_golden_corpus_all_pass():
    report = evaluate_media_consistency()
    assert report["passed"] is True
    assert report["total"] >= 12
    txt = format_media_consistency_report(report)
    assert "PASS" in txt


def test_detector_would_catch_tampering():
    """探测器有效性自证：把违规样本标成 expect_ok=True（假装没问题）必 FAIL。"""
    bad = [{"id": "tamper", "text": "自拍来啦！", "photo_sent": False,
            "expect_ok": True}]  # 实际是 claim_without_photo 违规
    report = evaluate_media_consistency(bad)
    assert report["passed"] is False


def test_detector_symmetric_tampering():
    """反向自证：把合法样本标成 expect_ok=False（冤枉好人）也必 FAIL。"""
    good = [{"id": "tamper2", "text": "宝贝想我了没？", "photo_sent": False,
             "expect_ok": False}]
    report = evaluate_media_consistency(good)
    assert report["passed"] is False


# ── 实施90：季节/地点 ×「此刻实拍」口径 ─────────────────────────────────────
def test_season_mismatch_fresh_claim_only():
    # 盛夏把雪景说成刚拍 → 违规
    v = check_media_consistency(
        "刚拍的，外面雪好大～", photo_sent=True,
        media_season="winter", now_season="summer")
    assert not v["ok"] and "season_mismatch" in v["violations"]
    # 旧照口径（去年冬天去玩拍的）→ 合法（真人也发旅行旧照）
    v2 = check_media_consistency(
        "去年冬天去玩拍的，给你看雪～", photo_sent=True,
        media_season="winter", now_season="summer")
    assert v2["ok"]
    # 没有「刚拍」口径（只是发图闲聊）→ 不判
    v3 = check_media_consistency(
        "看看这张～", photo_sent=True,
        media_season="winter", now_season="summer")
    assert v3["ok"]
    # 过渡季（秋 vs 冬）刻意不判
    v4 = check_media_consistency(
        "刚拍的落叶～", photo_sent=True,
        media_season="autumn", now_season="winter")
    assert v4["ok"]
    # 同季实拍=真话
    v5 = check_media_consistency(
        "刚拍的，今天太阳好舒服", photo_sent=True,
        media_season="summer", now_season="summer")
    assert v5["ok"]
    # 无标注不判（保守）
    v6 = check_media_consistency(
        "刚拍的～", photo_sent=True, media_season="", now_season="summer")
    assert v6["ok"]


def test_place_mismatch_fresh_claim_only():
    # 人在温哥华把东京照说成现拍 → 违规（中英两式）
    v = check_media_consistency(
        "刚拍的哦，这边夜景超美", photo_sent=True,
        media_place="JP", home_place="CA")
    assert not v["ok"] and "place_mismatch" in v["violations"]
    v2 = check_media_consistency(
        "Just snapped this outside!", photo_sent=True,
        media_place="TH", home_place="CA")
    assert not v2["ok"] and "place_mismatch" in v2["violations"]
    # 诚实旅行旧照 → 合法
    v3 = check_media_consistency(
        "之前去日本玩拍的，超好看", photo_sent=True,
        media_place="JP", home_place="CA")
    assert v3["ok"]
    # 同国 → 合法；无标注 → 不判
    assert check_media_consistency(
        "刚拍的～", photo_sent=True, media_place="CA", home_place="CA")["ok"]
    assert check_media_consistency(
        "刚拍的～", photo_sent=True, media_place="", home_place="CA")["ok"]
    # 无图不参与本轨（claim_without_photo 另有其判）
    v4 = check_media_consistency(
        "上次拍的那张", photo_sent=False, media_place="JP", home_place="CA")
    assert v4["ok"]


def test_new_goldens_present_and_detector_selfproof():
    report = evaluate_media_consistency()
    assert report["passed"] is True
    ids = {r["id"] for r in report["results"]}
    for need in ("season1", "season2", "place1", "place2",
                 "ok_season_old", "ok_place_honest",
                 "sent_claim1", "sent_claim2", "ok_sent_claim_backed"):
        assert need in ids, need
    # 篡改新轨金标（把季节错配标成没问题）必 FAIL
    bad = [{"id": "tamper3", "text": "刚拍的，外面雪好大～", "photo_sent": True,
            "media_season": "winter", "now_season": "summer",
            "expect_ok": True}]
    assert evaluate_media_consistency(bad)["passed"] is False
