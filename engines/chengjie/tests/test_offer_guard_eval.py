"""出站优惠守卫评测轨门禁（P15）。

三层：① 金标语料对真守卫全绿（召回 1.0 / 误伤 0）；② 探测器有效性自证——
换成「什么都不剥 / 什么都剥」的假守卫必 FAIL（评测不是摆设）；③ 语料形状
不变量（id 唯一、双面覆盖、事故形态类别齐）。
"""

from __future__ import annotations

from src.eval.offer_guard_eval import GOLD, evaluate_offer_guard


def test_gold_corpus_passes_with_real_guard():
    report = evaluate_offer_guard()
    assert report["passed"], report["failures"]
    assert report["recall"] == 1.0
    assert report["false_alarms"] == 0


def test_noop_guard_must_fail():
    """假守卫 A：什么都不剥 → 所有必剥样本失败（召回 0）。"""
    def _noop(text, *, allowed_texts=(), allowed_free_days=()):
        return text, 0, []
    report = evaluate_offer_guard(sanitize_fn=_noop)
    assert not report["passed"]
    assert report["recall"] == 0.0
    # 必留样本不受影响（noop 恰好零误伤）——FAIL 完全来自漏拦
    assert report["false_alarms"] == 0


def test_nuke_guard_must_fail():
    """假守卫 B：什么都剥 → 必留样本全数误伤，同样 FAIL。"""
    def _nuke(text, *, allowed_texts=(), allowed_free_days=()):
        return "", 1, ["x"]
    report = evaluate_offer_guard(sanitize_fn=_nuke)
    assert not report["passed"]
    assert report["false_alarms"] == report["keep_cases"] > 0


def test_guard_exception_counts_as_failure():
    """守卫抛异常不得装作通过（必剥样本按失败计）。"""
    def _boom(text, *, allowed_texts=(), allowed_free_days=()):
        raise RuntimeError("guard exploded")
    report = evaluate_offer_guard(sanitize_fn=_boom)
    assert not report["passed"]
    assert any("error" in f for f in report["failures"])


def test_corpus_shape_invariants():
    ids = [str(c.get("id")) for c in GOLD]
    assert len(ids) == len(set(ids)), "语料 id 必须唯一"
    strips = [c for c in GOLD if c.get("expect") == "strip"]
    keeps = [c for c in GOLD if c.get("expect") == "keep"]
    assert len(strips) >= 10 and len(keeps) >= 8, "双面覆盖不得缩水"
    # 事故形态五类 + P15 traction 至少各有一例（防有人删语料把门禁掏空）
    joined = " ".join(str(c.get("id")) for c in strips)
    for kind in ("discount", "percent", "cashback", "coupon",
                 "freebie", "traction"):
        assert kind in joined, f"缺 {kind} 类必剥样本"
    # 必留面须包含婉拒与授权两族反例（守卫最容易误伤的两处）
    keep_ids = " ".join(str(c.get("id")) for c in keeps)
    assert "refusal" in keep_ids and "allowed" in keep_ids
