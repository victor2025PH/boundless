"""发图配文复读治理门禁（2026-08-03）。

生产实锤：近 7 天出站发图配文高度模板化——「翻到一张之前拍的给你看」×10、
「找到一张我还挺喜欢的分享给你嘿嘿」×4 等，全部出自固定配文池（LLM 防复读
守卫 reply_variety 管不到的静态文案层）；旧选取＝进程游标重启归零恒取池首，
同一客户几天内反复收到逐字相同的配文。三层治理（本文件钉住）：
  ① 池扩容：caption / caption_album / caption_object 三池 zh ≥10、en ≥6；
     旧照池（caption_album）红线＝全部「翻出来的/之前拍的/存货」口径，
     禁「刚拍/刚出炉/热乎/刚照」等新鲜词（与 test_persona_media_consistency
     的 P0 配文诚实门禁双保险）；
  ② pick_caption：crc32(chat_key + 日期) 确定性种子——不同会话同日不同条、
     同会话同日恒定（可测可复现）；
  ③ 近用避重：note_caption_used **发出成功后**记账（512 会话 × 3 条 bounded），
     候选命中近用顺移，全池耗尽按种子取（不死循环），异常回落纯种子取。
"""
from __future__ import annotations

import datetime as dt
import uuid

import src.ai.companion_selfie as cs
import src.inbox.image_autosend as ia

# 固定时刻：种子含日期，钉住防跨午夜 flake。
_NOW = dt.datetime(2026, 8, 3, 15, 30)
_POOL_ZH = cs._STAGE_TEXTS["caption_album"]["zh"]
_POOL_EN = cs._STAGE_TEXTS["caption_album"]["en"]


def _key() -> str:
    """每测试独立会话键，避免进程级近用账本跨测试串味。"""
    return f"capvar:{uuid.uuid4().hex[:12]}"


# ── ① 池规模与红线 ─────────────────────────────────────────────────────────
def test_caption_pools_meet_size_floor():
    """三个配文池 zh ≥10 / en ≥6，条目非空且池内无重复（重复=变相缩池）。"""
    for key in ("caption", "caption_album", "caption_object"):
        zh = cs._STAGE_TEXTS[key]["zh"]
        en = cs._STAGE_TEXTS[key]["en"]
        assert isinstance(zh, (list, tuple)) and len(zh) >= 10, \
            f"{key} zh 池须 ≥10（现 {len(zh)}）"
        assert isinstance(en, (list, tuple)) and len(en) >= 6, \
            f"{key} en 池须 ≥6（现 {len(en)}）"
        for pool in (zh, en):
            assert all(str(t).strip() for t in pool), f"{key} 池含空条目"
            assert len(set(pool)) == len(pool), f"{key} 池含重复条目"


def test_old_pool_bans_fresh_words_zh():
    """旧照池（zh）逐条扫新鲜词红线：存货配文声称「刚拍」是实录穿帮点。"""
    banned = ("刚拍", "剛拍", "刚出炉", "剛出爐", "热乎", "熱乎",
              "刚照", "剛照", "新鲜出炉", "现在拍", "刚出锅")
    for text in _POOL_ZH:
        for b in banned:
            assert b not in text, f"旧照池不得含新鲜词 {b!r}：{text!r}"


def test_old_pool_bans_fresh_words_en():
    """旧照池（en）同一红线（英文口径）。"""
    banned = ("just took", "just shot", "just now", "just snapped",
              "fresh out", "hot off")
    for text in _POOL_EN:
        low = text.lower()
        for b in banned:
            assert b not in low, f"en 旧照池不得含新鲜词 {b!r}：{text!r}"


def test_fresh_pool_keeps_at_most_one_just_taken_claim():
    """新照池允许现在时口径，但「刚拍」弱时间声明至多 1 条（既有语义收紧钉住）。"""
    zh_pool = cs._STAGE_TEXTS["caption"]["zh"]
    assert sum(1 for t in zh_pool if "刚拍" in t) <= 1
    en_pool = cs._STAGE_TEXTS["caption"]["en"]
    assert sum(1 for t in en_pool if "just took" in t.lower()) <= 1


# ── ② 按会话×日期确定性种子 ────────────────────────────────────────────────
def test_different_chats_differ_same_day():
    """不同会话同一天取不同条（撒 60 样验证分布非常数）——旧游标根因的反面。"""
    picks = {cs.pick_caption(_POOL_ZH, f"chat:{i}", now=_NOW, recent=())
             for i in range(60)}
    assert len(picks) >= 5, f"60 个会话同日仅取到 {len(picks)} 条，分布退化"
    assert picks <= set(_POOL_ZH)


def test_same_chat_same_day_deterministic():
    """同会话同日（无近用记录）恒取同一条——可测可复现的确定性口径。"""
    k = _key()
    a = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=())
    b = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=())
    assert a == b and a in _POOL_ZH


def test_same_chat_across_days_varies():
    """同会话跨天轮换：14 天内至少出现 2 条不同配文（日期进种子）。"""
    k = _key()
    picks = {cs.pick_caption(_POOL_ZH, k, now=_NOW + dt.timedelta(days=i),
                             recent=())
             for i in range(14)}
    assert len(picks) >= 2


# ── ③ 近用避重 ─────────────────────────────────────────────────────────────
def test_recent_avoid_shifts_to_next():
    """候选命中近用（显式 recent 传原文）→ 顺移到下一条。"""
    k = _key()
    first = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=())
    second = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=[first])
    assert second != first and second in _POOL_ZH


def test_recent_accepts_fingerprints_too():
    """recent 传 crc32 指纹与传原文等效（账本存的就是指纹）。"""
    k = _key()
    first = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=())
    by_text = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=[first])
    by_fp = cs.pick_caption(_POOL_ZH, k, now=_NOW,
                            recent=[cs._caption_fp(first)])
    assert by_text == by_fp != first


def test_note_caption_used_ledger_rotation():
    """进程账本闭环：发出记账 → 同日再取必换；窗口只保最近 3 条。"""
    k = _key()
    a = cs.pick_caption(_POOL_ZH, k, now=_NOW)
    cs.note_caption_used(k, a)
    b = cs.pick_caption(_POOL_ZH, k, now=_NOW)
    assert b != a
    cs.note_caption_used(k, b)
    c = cs.pick_caption(_POOL_ZH, k, now=_NOW)
    assert c not in (a, b)
    for cap in _POOL_ZH[:5]:
        cs.note_caption_used(k, cap)
    assert len(cs.recent_captions(k)) == 3  # 窗口上限=3


def test_pool_exhausted_falls_back_to_seed_no_deadlock():
    """全池都近用过 → 按种子取（与无记录时同一条），绝不死循环/拒答。"""
    k = _key()
    seed_pick = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=())
    got = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=list(_POOL_ZH))
    assert got == seed_pick


def test_exception_falls_back_to_seed():
    """recent 传不可迭代垃圾 → 回落纯种子取；chat_key/now 异常也不崩。"""
    k = _key()
    seed_pick = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=())
    got = cs.pick_caption(_POOL_ZH, k, now=_NOW, recent=object())
    assert got == seed_pick
    assert cs.pick_caption(_POOL_ZH, None, now="garbage") in _POOL_ZH
    assert cs.pick_caption(_POOL_ZH, 123, now=_NOW, recent=()) in _POOL_ZH


def test_empty_or_bad_pool_returns_empty():
    assert cs.pick_caption([], _key()) == ""
    assert cs.pick_caption(None, _key()) == ""
    assert cs.pick_caption(123, _key()) == ""
    assert cs.pick_caption(["", "  "], _key()) == ""
    # 单字符串误传（非列表）按单条池处理，不逐字符迭代
    assert cs.pick_caption("整句配文", _key()) == "整句配文"


def test_en_pool_same_semantics():
    """en 池同款：分布非常数 + 近用避重。"""
    picks = {cs.pick_caption(_POOL_EN, f"en:{i}", now=_NOW, recent=())
             for i in range(40)}
    assert len(picks) >= 3 and picks <= set(_POOL_EN)
    k = _key()
    first = cs.pick_caption(_POOL_EN, k, now=_NOW, recent=())
    assert cs.pick_caption(_POOL_EN, k, now=_NOW, recent=[first]) != first


def test_ledger_bounded_512_keys():
    """账本 512 会话上限（LRU 裁剪）——长期运行不撑爆进程内存。"""
    for i in range(600):
        cs.note_caption_used(f"bulk:{i}", "x")
    assert len(cs._RECENT_CAPTIONS) <= cs._RECENT_CAPTIONS_MAX_KEYS


# ── 消费点接线（A 线经 selfie_stage_text；B 线经 _fixed_caption）────────────
def test_selfie_stage_text_chat_key_routes_via_pick_caption():
    """selfie_stage_text 带 chat_key＝走 pick_caption 同口径；记账后必换。"""
    k = _key()
    out1 = cs.selfie_stage_text("caption_album", "zh", chat_key=k)
    assert out1 == cs.pick_caption(_POOL_ZH, k)
    cs.note_caption_used(k, out1)
    out2 = cs.selfie_stage_text("caption_album", "zh", chat_key=k)
    assert out2 != out1 and out2 in _POOL_ZH


def test_stage_counter_rotation_still_works_without_chat_key():
    """无 chat_key 的旧路径（A 线现状）：连续两次触发必换措辞（既有保证不变）。"""
    a = cs.selfie_stage_text("caption_album", "zh")
    b = cs.selfie_stage_text("caption_album", "zh")
    assert a and b and a != b
    # 显式 variant_salt 仍完全确定性（测试口径不变）
    assert (cs.selfie_stage_text("caption_album", "zh", variant_salt=2)
            == cs.selfie_stage_text("caption_album", "zh", variant_salt=2))


def test_fixed_caption_passes_chat_key_and_keeps_compat():
    """B 线 _fixed_caption：chat_key 透传生效、运营配置仍最高优先、旧签名兼容。"""
    scfg = {"caption": "刚拍的x", "caption_album": ""}
    k = _key()
    cap = ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "zh", chat_key=k)
    assert cap in _POOL_ZH and "刚拍" not in cap
    assert cap == cs.pick_caption(_POOL_ZH, k)  # 与纯函数同口径
    # 不同会话同日应取到多条（撒 40 样）
    picks = {ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "zh",
                               chat_key=f"fc:{i}") for i in range(40)}
    assert len(picks) >= 3
    # 运营显式配 caption_album 优先（不进池）
    assert ia._fixed_caption({"caption_album": "运营配文"}, ia.KIND_SELFIE,
                             "old", "zh", chat_key=k) == "运营配文"
    # 旧 4 参位置调用兼容（游标路径）
    assert ia._fixed_caption(scfg, ia.KIND_SELFIE, "old", "zh") in _POOL_ZH
    # fresh/video/object 语义不变
    assert ia._fixed_caption(scfg, ia.KIND_SELFIE, "fresh", "zh",
                             chat_key=k) == "刚拍的x"
    assert ia._fixed_caption(scfg, "video", "old", "zh", chat_key=k) == ""
