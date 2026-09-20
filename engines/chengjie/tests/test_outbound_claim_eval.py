# -*- coding: utf-8 -*-
"""出站事实声明校验门禁（P15）。

与 ``test_offer_guard_eval`` 分工：那边测**守卫行为**（该剥的剥），这边测
**回复文本 vs 登记事实**（报价/试用时长/产品线边界/回复语言/指令泄漏）。
金标正例全部来自 2026-07-28 本地×云端 AI 对练实录。

含探测器有效性自证：瞎检测器（永远空 / 永远报警）必须让评测 FAIL——
评测不是摆设。
"""

from __future__ import annotations

import pytest

from src.eval.outbound_claim_eval import (
    CHECKS,
    GOLD,
    catalog_prices,
    check_outbound_claims,
    dominant_language,
    evaluate_outbound_claims,
    find_directive_leak,
    find_gated_leak,
    find_language_drift,
    find_price_mismatch,
    find_trial_mismatch,
    format_outbound_claim_report,
)

PRICES = [58.0, 198.0, 598.0, 99.0, 59.0]
DAYS = [7.0]


def test_gold_corpus_passes():
    """金标全绿：必抓 100% 召回、必放零误报。"""
    report = evaluate_outbound_claims()
    assert report["passed"], format_outbound_claim_report(report)
    assert report["recall"] == 1.0
    assert report["false_alarms"] == 0
    # 五个轴都得有正例覆盖，别让某轴悄悄空掉
    for name in CHECKS:
        assert report["by_check"][name]["expected"] >= 1, name


def test_blind_detectors_fail_the_eval():
    """探测器自证：什么都不报 / 什么都报，评测都必须 FAIL。"""
    silent = evaluate_outbound_claims(check_fn=lambda text, **kw: [])
    assert not silent["passed"] and silent["recall"] < 1.0

    def _noisy(text, **kw):
        return [(c, "x") for c in CHECKS]

    noisy = evaluate_outbound_claims(check_fn=_noisy)
    assert not noisy["passed"] and noisy["false_alarms"] > 0


def test_checker_exception_is_not_silently_passed():
    """检查器抛异常必须记为失败，不许装作通过。"""
    def _boom(text, **kw):
        raise RuntimeError("boom")

    report = evaluate_outbound_claims(check_fn=_boom)
    assert not report["passed"]
    assert any("boom" in str(f.get("error", "")) for f in report["failures"])


# ── 报价轴 ───────────────────────────────────────────────────────────────
def test_price_mismatch_real_incident():
    """实录：同句先报 198 再报 168（≈8.5 折），offer_guard 的措辞轴管不着。"""
    hits = find_price_mismatch(
        "我那套智聊AI团队版198美金一个月，算下来一个月168",
        allowed_prices=PRICES)
    assert hits and any("168" in h for h in hits)


def test_price_correct_quote_clean():
    assert find_price_mismatch(
        "智聊团队版 $198/月，入门版 $58/月", allowed_prices=PRICES) == []


def test_price_ignores_persona_own_business():
    """人设自家生意报价（无产品词）永不进判定——最险的误伤面。"""
    assert find_price_mismatch(
        "我店里芒果干大包450比索，小包250", allowed_prices=PRICES) == []
    # 同条消息里混入产品词，自家报价小句仍不得被误读成套餐价
    assert find_price_mismatch(
        "我店里手冲一杯38块，智聊帮我省了人力", allowed_prices=PRICES) == []


def test_price_needs_currency_or_period_shape():
    """『三个客服』『多接200单』不是报价。"""
    assert find_price_mismatch(
        "你们三个客服扛不住，智聊能顶掉大半", allowed_prices=PRICES) == []
    assert find_price_mismatch(
        "智聊上线后我一个月能多接200单", allowed_prices=PRICES) == []


def test_price_empty_allowlist_is_noop():
    """目录读不到价格 → 不判（宁漏勿误，绝不因目录缺失全站报警）。"""
    assert find_price_mismatch("智聊团队版 $168/月", allowed_prices=[]) == []


def test_catalog_prices_parses_shipped_catalog():
    cat = {"products": [
        {"id": "chatx", "price_from": "$58/月",
         "plans": [{"price": "$198/月"}, {"price": "$598/月"}]},
        {"id": "lingox", "price_from": "$99/月",
         "plans": [{"price": "$59 一次性"}]},
    ]}
    vals = set(catalog_prices(cat))
    assert {58.0, 198.0, 598.0, 99.0, 59.0} <= vals
    assert catalog_prices(None) == [] and catalog_prices({"products": "坏"}) == []


# ── 试用时长轴 ───────────────────────────────────────────────────────────
def test_trial_duration_catches_escaped_phrasing():
    """实录逃逸形：『官网有14天客户端试用』——offer_guard 因『免费』不贴数字漏掉。"""
    assert find_trial_mismatch(
        "官网有14天客户端试用，装上就能跑", allowed_free_days=DAYS)


def test_trial_duration_accepts_registered_fact():
    assert find_trial_mismatch(
        "注册就能免费试用7天，2.5万字符够跑一轮", allowed_free_days=DAYS) == []
    # 一周 ≡ 7 天：授权的是事实不是措辞
    assert find_trial_mismatch(
        "有一周的免费体验额度", allowed_free_days=DAYS) == []


def test_trial_duration_unregistered_is_noop():
    """没登记授权时长 → 本轴不判，避免与 offer_guard 赠送轴双重口径。"""
    assert find_trial_mismatch("官网有14天试用", allowed_free_days=[]) == []


def test_trial_duration_needs_trial_context():
    """没有试用语境的时长（『7天到货』）不抓。"""
    assert find_trial_mismatch(
        "寄到清迈大概7天到货", allowed_free_days=DAYS) == []


# ── 产品线边界轴 ─────────────────────────────────────────────────────────
def test_gated_line_leak():
    assert find_gated_leak("还有免费图片换脸和实时演示")
    assert find_gated_leak("we also do face swap demos")


def test_gated_line_does_not_flag_own_voice_stack():
    """本栈自用语音克隆 ≠ 卖 gated 换脸线。"""
    assert find_gated_leak("我们的语音克隆只给自己人设配音用") == []


# ── 回复语言轴 ───────────────────────────────────────────────────────────
def test_language_drift_real_incident():
    """实录：客户主体中文，回复整段泰语（连续 7 轮）。"""
    drift = find_language_drift(
        "ฮ่าๆ โดนจับได้ซะแล้ว สลับภาษาไปมาเองยังไม่ทันรู้ตัวเลย มะม่วงอบแห้ง",
        ["你突然切换泰语啦，吓我一跳～",
         "不过说真的，你们那芒果干多少钱一包？",
         "所以寄到清迈要多久啊"])
    assert drift == ["zh->th"]


def test_language_no_drift_when_customer_switched():
    """客户真整段换英文 → 跟着英文回是对的，不得报警。"""
    assert find_language_drift(
        "Sure, we ship to Thailand — usually 7 to 10 days.",
        ["do you ship to Thailand?", "how long to Chiang Mai?"]) == []


def test_language_fragment_does_not_flip_dominant():
    """单条泰语碎片不足以把会话主体语翻成泰语（本次事故的根因形态）。"""
    msgs = ["你好呀，想问下代购", "อร่อยมาก 你们那芒果干真好吃", "多少钱一包呀"]
    assert dominant_language(msgs) == "zh"


def test_language_short_reply_not_judged():
    assert find_language_drift("好呀～", ["在吗"]) == []


def test_language_ambiguous_customer_not_judged():
    """客户语种本身没有明确主体 → 不判（宁漏勿误）。"""
    assert find_language_drift("ok no problem", ["hello 你好 สวัสดี"]) == []


# ── 指令泄漏轴 ───────────────────────────────────────────────────────────
def test_directive_leak_real_incident():
    assert find_directive_leak(
        "รวมค่าสินค้า 2,250\n[PHOTO object shopping bag with dried mango]")


def test_directive_leak_ignores_plain_brackets():
    assert find_directive_leak("我记下了（真的）[认真脸]") == []


# ── 汇总入口 ─────────────────────────────────────────────────────────────
def test_check_outbound_claims_never_raises():
    for bad in (None, "", 12345, "x" * 5000):
        assert isinstance(
            check_outbound_claims(bad, allowed_prices=PRICES,
                                  allowed_free_days=DAYS), list)


def test_gold_ids_unique():
    ids = [c["id"] for c in GOLD]
    assert len(ids) == len(set(ids))


# ── 生产守卫动作 ─────────────────────────────────────────────────────────
def test_sanitize_strips_bad_clause_keeps_rest():
    """只剥命中小句，其余原样留（与 offer_guard 同口径）。"""
    from src.companion.goals.claim_guard import sanitize_outbound_claims
    out, n, hits = sanitize_outbound_claims(
        "这套我自己也在用，智聊团队版算下来一个月168，效果真的不错",
        allowed_prices=PRICES, allowed_free_days=DAYS)
    assert n >= 1 and "168" not in out
    assert "我自己也在用" in out and "效果真的不错" in out
    assert "，，" not in out


def test_sanitize_whole_reply_becomes_compliant_fallback():
    """整条就是错声明 → 换合规话术（回退原文＝把错价格照发，等于没守）。"""
    from src.companion.goals.claim_guard import (
        compliant_fallback, sanitize_outbound_claims,
    )
    out, n, _ = sanitize_outbound_claims(
        "智聊团队版一个月168", allowed_prices=PRICES)
    assert n == 1 and "168" not in out
    assert out == compliant_fallback("智聊团队版一个月168")


def test_sanitize_removes_directive_tag_only():
    """指令泄漏摘标记即可，不牵连整句语义。"""
    from src.companion.goals.claim_guard import sanitize_outbound_claims
    out, n, _ = sanitize_outbound_claims(
        "都给你装好啦\n[PHOTO object shopping bag with mango]",
        allowed_prices=PRICES, allowed_free_days=DAYS)
    assert n == 1
    assert "PHOTO" not in out and "都给你装好啦" in out


def test_sanitize_clean_text_untouched():
    """必放面零改写——守卫误伤等于对客户说谎。"""
    from src.companion.goals.claim_guard import sanitize_outbound_claims
    for txt in ("智聊团队版 $198/月，入门版 $58/月",
                "注册就能免费试用7天，2.5万字符够你跑一轮",
                "我店里手冲一杯38块，智聊帮我省了人力",
                "我记在小本本上了（真的）[认真脸]"):
        out, n, _ = sanitize_outbound_claims(
            txt, allowed_prices=PRICES, allowed_free_days=DAYS)
        assert (n, out) == (0, txt), txt


def test_sanitize_never_raises():
    from src.companion.goals.claim_guard import sanitize_outbound_claims
    for bad in (None, "", 123, "x" * 4000):
        out, n, hits = sanitize_outbound_claims(bad, allowed_prices=PRICES)
        assert isinstance(out, str) and isinstance(n, int) and isinstance(hits, list)


def test_skill_manager_wires_claim_guard():
    """接线：出站链读 _goal_cta 时过事实声明守卫 + 计数。"""
    import logging
    from types import SimpleNamespace

    from src.companion.goals.stats import get_goal_stats
    from src.skills.skill_manager import SkillManager

    get_goal_stats().reset()
    fake_self = SimpleNamespace(
        config=SimpleNamespace(config={"companion": {"goals": {}}}),
        logger=logging.getLogger("test-claim-guard"))
    uc = {"_goal_cta": {"cta": "order", "base_url": "https://bd2026.cc",
                        "order_path": "/order", "offer_texts": [],
                        "offer_free_days": [7.0],
                        "catalog_prices": PRICES}}
    out = SkillManager._apply_goal_link_guard(
        fake_self, "智聊团队版198美金一个月，算下来一个月168，另外还能免费图片换脸", uc)
    assert "168" not in out and "换脸" not in out
    assert get_goal_stats().dump()["claim_stripped"] >= 1
    assert "_goal_cta" not in uc          # 读后即焚不变


def test_service_stashes_catalog_prices():
    """注入侧把目录价格一并暂存（守卫读的是 _goal_cta）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "src" / "companion" / "goals"
           / "service.py").read_text(encoding="utf-8")
    assert '"catalog_prices": _catalog_price_list(' in src
    assert "def _catalog_price_list(" in src


@pytest.mark.parametrize("case", [c for c in GOLD if c.get("expect") != "clean"])
def test_each_violation_case_is_caught(case):
    """逐例点名，失败时直接看得出是哪条金标退化。"""
    found = {k for k, _ in check_outbound_claims(
        case["text"],
        allowed_prices=case.get("allowed_prices", PRICES),
        allowed_free_days=case.get("allowed_free_days", DAYS),
        customer_msgs=case.get("customer_msgs") or ())}
    assert case["expect"] in found, f"{case['id']} 未被抓到：{found}"


# ── 跨轮报价语境（2026-07-28 二次实录逃逸）───────────────────────────────
# 修复验证跑里 LLM 把产品词与越权数字**拆到两轮**说：T2「团队版每月198美金」、
# T3「一个月摊下来也就168」。单条口径要求产品词与价格同条消息 → T3 整条跳过，
# 168 照发给客户。``product_context=True`` 放宽到跨轮，人设自家报价仍由周期闸兜。
_STICKY_T3 = ("咱算笔简单账：你三个客服月薪加起来怎么也得小一千美金吧？198还不到零头。"
              "一个月摊下来也就168，进群还能领折扣码")


def test_cross_turn_price_escapes_single_message_scope():
    """先钉住「为什么单条口径抓不到」——这是二次逃逸的根因，别悄悄退化。"""
    assert find_price_mismatch(_STICKY_T3, allowed_prices=PRICES) == []


def test_cross_turn_price_caught_with_product_context():
    """会话里已谈过我方产品 → 跨轮的越权报价必须抓到。"""
    hits = find_price_mismatch(_STICKY_T3, allowed_prices=PRICES,
                               product_context=True)
    assert any("168" in h for h in hits), hits


def test_product_context_keeps_catalog_price_clean():
    """跨轮模式下目录内价格（198）不得被误报。"""
    hits = find_price_mismatch("一个月就198，不到一个客服月薪",
                               allowed_prices=PRICES, product_context=True)
    assert hits == []


def test_product_context_ignores_non_money_counters():
    """裸数字后跟非货币量词（单/人/天）不是报价——防跨轮模式误伤业务数字。"""
    for txt in ("智聊上线后我一个月能多接200单",
                "一个月能省下3个客服的人力",
                "一个月大概处理1200条咨询"):
        assert find_price_mismatch(txt, allowed_prices=PRICES,
                                   product_context=True) == [], txt


def test_product_context_still_needs_period_context():
    """跨轮模式也只在有周期语境的小句里读裸数字（人设自家「一杯38块」不判）。"""
    assert find_price_mismatch("我店里手冲一杯38块，你要不要试试",
                               allowed_prices=PRICES, product_context=True) == []


def test_product_context_default_off_everywhere():
    """纯函数层默认关：调用方显式 opt-in（生产在 skill_manager 侧开）。"""
    import inspect
    sig = inspect.signature(find_price_mismatch)
    assert sig.parameters["product_context"].default is False
    sig2 = inspect.signature(check_outbound_claims)
    assert sig2.parameters["product_context"].default is False


# ROI 话术是销售最常说的话，而「块」是货币单位 → 跨轮模式若不排除，
# 「一个月省下2000块」这类会被当越权报价剥掉。开生产开关前实测误报 6/9，
# 加 _OTHER_MONEY_CTX_RE 小句级排除后归零。这些反例必须常驻。
_OTHER_MONEY_CASES = [
    "一个月能帮你省下2000块人力成本",
    "算下来一个月省3000块，比养个客服划算",
    "你三个客服月薪加起来怎么也得小一千美金吧",
    "一个月房租都要8000块，这点工具钱不算啥",
    "上线后我一个月多赚了5000块",
    "一个月流水做到10万块了",
    "客服一个月工资4500块，还得管社保",
    "我这边一个月预算就500美金",
]


@pytest.mark.parametrize("txt", _OTHER_MONEY_CASES)
def test_product_context_ignores_other_peoples_money(txt):
    """客户成本/收益、人设自家开销里的钱不是我方报价——误伤即毁掉 ROI 话术。"""
    assert find_price_mismatch(txt, allowed_prices=PRICES,
                               product_context=True) == [], txt


@pytest.mark.parametrize("txt", [
    "一个月摊下来也就168",
    "咱这个一个月就88，别家都99",
    "团队版给你算一个月150美金",
])
def test_product_context_still_catches_real_overreach(txt):
    """排除层不得把真越权报价一起放过。"""
    assert find_price_mismatch(txt, allowed_prices=PRICES,
                               product_context=True), txt


def test_production_guard_enables_cross_turn_by_default():
    """生产侧默认开跨轮（本次决策：数据支持 + kill-switch 兜底）。

    kill-switch = ``companion.goals.claim_guard.price_cross_turn: false``。
    """
    import logging

    from src.skills.skill_manager import _guard_outbound_claims
    info = {"catalog_prices": PRICES, "offer_free_days": DAYS}
    log = logging.getLogger("test-claim-wire")
    cross = "咱算笔简单账，一个月摊下来也就168，进群还能领折扣码"
    on = _guard_outbound_claims(cross, info, cfg_root={"companion": {"goals": {}}},
                                logger=log)
    assert "168" not in on, "生产默认应开跨轮报价校验"
    off = _guard_outbound_claims(
        cross, info,
        cfg_root={"companion": {"goals": {"claim_guard": {"price_cross_turn": False}}}},
        logger=log)
    assert off == cross, "kill-switch 必须能回到旧行为"
    # kill-switch 只关跨轮，别把其它轴一起关了
    trial = "官网有14天客户端试用"
    off2 = _guard_outbound_claims(
        trial, info,
        cfg_root={"companion": {"goals": {"claim_guard": {"price_cross_turn": False}}}},
        logger=log)
    assert off2 != trial


def test_production_roi_talk_survives_guard():
    """端到端：ROI 话术过生产守卫必须原样出去。"""
    import logging

    from src.skills.skill_manager import _guard_outbound_claims
    info = {"catalog_prices": PRICES, "offer_free_days": DAYS}
    txt = "上线后一个月能帮你省下2000块人力成本，比养个客服划算"
    out = _guard_outbound_claims(txt, info, cfg_root={"companion": {"goals": {}}},
                                 logger=logging.getLogger("test-roi"))
    assert out == txt


# ── 与生产守卫的交叉校验 ─────────────────────────────────────────────────
# 本模块的检查器与生产件 ``companion.goals.claim_guard`` 是**两份独立维护的实现**
# （house 约定：评测器不 import 生产词表，防「词表改坏评测跟着瞎」，见
# media_consistency_eval 模块头）。独立维护的代价是可能**各自漂移**：生产件被改松
# 而评测仍绿 = 门禁形同虚设。故把同一份金标同时喂给生产件，两边行为必须一致。
def _production_check():
    try:
        from src.companion.goals.claim_guard import check_outbound_claims as prod
    except ImportError:
        return None
    return prod


def test_production_guard_module_exists():
    """生产守卫在位（它被删/改名时这里点名，而不是静默失去保护）。"""
    assert _production_check() is not None, (
        "src/companion/goals/claim_guard.py 不见了——出站事实校验已无生产侧执行者")


@pytest.mark.parametrize("case", list(GOLD))
def test_production_guard_agrees_with_eval(case):
    """生产件与评测器对每条金标的判定必须一致（防单侧漂移）。"""
    prod = _production_check()
    if prod is None:
        pytest.skip("生产守卫不存在，已由 test_production_guard_module_exists 点名")
    kw = dict(
        allowed_prices=case.get("allowed_prices", PRICES),
        allowed_free_days=case.get("allowed_free_days", DAYS),
        customer_msgs=case.get("customer_msgs") or ())
    mine = {k for k, _ in check_outbound_claims(case["text"], **kw)}
    theirs = {k for k, _ in prod(case["text"], **kw)}
    expect = str(case.get("expect") or "clean")
    if expect == "clean":
        assert not theirs, f"{case['id']} 生产件误报：{sorted(theirs)}"
    else:
        assert expect in theirs, f"{case['id']} 生产件漏抓（评测抓到 {sorted(mine)}）"
    assert mine == theirs, (
        f"{case['id']} 两侧判定漂移：评测={sorted(mine)} 生产={sorted(theirs)}")
