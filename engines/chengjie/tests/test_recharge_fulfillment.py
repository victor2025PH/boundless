"""充值档履约纯逻辑回归网（2026-08-21 充值唯一化，实施50）。

覆盖：首充「每人一次」判定（contact_core/指纹双锚 + 确定性并列裁决）、新人包
72h 窗与每账号一次、凭证 chars 换算（1 Token=100 字符并账口径）、履约规划器
幂等跳过、与官网 chatx-pricing.ts 的跨仓价格交叉钉（同机才跑，CI 自动 skip）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.licensing.chatx_fulfillment import (
    NEWBIE_SKU,
    NEWBIE_TOKENS,
    NEWBIE_WINDOW_HOURS,
    RECHARGE_CHARS_PER_TOKEN,
    RECHARGE_SKU_SPECS,
    RECHARGE_TOKENS_PER_USD,
    VIP_REPEAT_BONUS_TIERS,
    cumulative_recharge_usd_before,
    is_chatx_order,
    is_first_recharge,
    is_recharge_sku,
    manual_followup_orders,
    newbie_eligibility,
    plan_recharge_fulfillment,
    recharge_voucher_decision,
    repeat_bonus_pct,
)

T0 = "2026-08-21T00:00:00.000Z"
T1 = "2026-08-21T01:00:00.000Z"
T2 = "2026-08-21T02:00:00.000Z"
T_LATE = "2026-08-25T00:00:00.000Z"  # T0 + 96h（超 72h 窗）


def order(oid="AH-20260821-AAAA", sku="recharge-200", contact="@alice",
          fp="AAAA-BBBB-CCCC-DDDD", status="paid", t=T1, **kw):
    o = {"id": oid, "sku_id": sku, "product_id": "zhiliao", "contact": contact,
         "fingerprint": fp, "status": status, "t": t}
    o.update(kw)
    return o


def claim(fp="AAAA-BBBB-CCCC-DDDD", contact="@alice", created=T0):
    return {"fingerprint": fp, "contact": contact, "created_at": created}


# ── SKU 判定 ─────────────────────────────────────────────────────────────────

def test_is_recharge_sku():
    assert is_recharge_sku("recharge-50")
    assert is_recharge_sku("recharge-10000")
    assert is_recharge_sku(NEWBIE_SKU)
    assert not is_recharge_sku("token-pack-s")
    assert not is_recharge_sku("chatx-personal")
    assert not is_recharge_sku("")


def test_is_chatx_order_recognizes_recharge_prefix_without_product_id():
    assert is_chatx_order({"sku_id": "recharge-5000"})


# ── 首充判定 ─────────────────────────────────────────────────────────────────

def test_first_recharge_no_history():
    assert is_first_recharge(order(), None) is True
    assert is_first_recharge(order(), []) is True


def test_first_recharge_prior_paid_order_same_contact_denies():
    prior = order(oid="AH-20260820-OLD1", sku="recharge-50", t=T0, status="activated")
    assert is_first_recharge(order(oid="AH-20260821-NEW1"), [prior]) is False


def test_first_recharge_contact_core_normalization():
    # 「Tg: @Alice (老板)」与「@alice」是同一个人（contact_core 语义）
    prior = order(oid="AH-20260820-OLD1", contact="Tg: @Alice (老板)",
                  fp="", t=T0, status="paid")
    cur = order(oid="AH-20260821-NEW1", contact="@alice", fp="EEEE-FFFF-GGGG-HHHH")
    assert is_first_recharge(cur, [prior]) is False


def test_first_recharge_fingerprint_match_denies():
    prior = order(oid="AH-20260820-OLD1", contact="someone-else@x.com",
                  fp="aaaa-bbbb-cccc-dddd", t=T0, status="paid")
    cur = order(oid="AH-20260821-NEW1", contact="@bob", fp="AAAA-BBBB-CCCC-DDDD")
    assert is_first_recharge(cur, [prior]) is False


def test_first_recharge_other_person_does_not_count():
    prior = order(oid="AH-20260820-OLD1", contact="@carol", fp="1111-2222-3333-4444",
                  t=T0, status="paid")
    assert is_first_recharge(order(), [prior]) is True


def test_first_recharge_newbie_pack_does_not_consume():
    prior = order(oid="AH-20260820-NB01", sku=NEWBIE_SKU, t=T0, status="activated")
    assert is_first_recharge(order(oid="AH-20260821-NEW1"), [prior]) is True


def test_first_recharge_pending_or_cancelled_history_ignored():
    p1 = order(oid="AH-20260820-PEND", t=T0, status="pending")
    p2 = order(oid="AH-20260820-CANC", t=T0, status="cancelled")
    assert is_first_recharge(order(oid="AH-20260821-NEW1"), [p1, p2]) is True


def test_first_recharge_simultaneous_tiebreak_exactly_one_wins():
    """两笔同时到账：按 (时间, 订单号) 确定性裁决，只有一笔算首充——
    且与处理顺序无关（无状态重放安全）。"""
    a = order(oid="AH-20260821-AAAA", t=T1)
    b = order(oid="AH-20260821-BBBB", t=T1)
    hist = [a, b]
    assert is_first_recharge(a, hist) is True
    assert is_first_recharge(b, hist) is False
    # 更早时间戳压过订单号序
    c = order(oid="AH-20260821-ZZZZ", t=T0)
    hist2 = [a, b, c]
    assert is_first_recharge(c, hist2) is True
    assert is_first_recharge(a, hist2) is False


def test_first_recharge_self_already_in_history_dedupes():
    cur = order(oid="AH-20260821-AAAA")
    assert is_first_recharge(cur, [cur]) is True


# ── 新人包资格 ───────────────────────────────────────────────────────────────

def test_newbie_ok_within_window():
    ok, reason = newbie_eligibility(order(sku=NEWBIE_SKU, t=T1), [], [claim(created=T0)])
    assert ok and reason == ""


def test_newbie_window_passed():
    ok, reason = newbie_eligibility(
        order(sku=NEWBIE_SKU, t=T_LATE), [], [claim(created=T0)])
    assert not ok and reason == "newbie_window_passed"


def test_newbie_no_claim_is_eligible_first_touch():
    ok, _ = newbie_eligibility(order(sku=NEWBIE_SKU), [], [])
    assert ok
    ok, _ = newbie_eligibility(order(sku=NEWBIE_SKU), [], None)  # 台账拉取失败
    assert ok


def test_newbie_claim_matched_by_contact_when_fp_missing():
    o = order(sku=NEWBIE_SKU, fp="", t=T_LATE)
    ok, reason = newbie_eligibility(o, [], [claim(fp="XXXX", contact="@alice", created=T0)])
    assert not ok and reason == "newbie_window_passed"


def test_newbie_once_per_account():
    prior = order(oid="AH-20260820-NB01", sku=NEWBIE_SKU, t=T0, status="activated")
    cur = order(oid="AH-20260821-NB02", sku=NEWBIE_SKU, t=T1)
    ok, reason = newbie_eligibility(cur, [prior], [])
    assert not ok and reason == "newbie_already_claimed"
    # 幂等重放：最早那笔自己重跑仍放行
    ok, _ = newbie_eligibility(prior, [prior, cur], [])
    assert ok


def test_newbie_earliest_claim_anchors_window():
    """多条 claim（换机重装）取最早的当注册锚——防「重装刷新 72h 窗」。"""
    claims = [claim(created=T_LATE), claim(created=T0)]
    ok, reason = newbie_eligibility(order(sku=NEWBIE_SKU, t=T_LATE), [], claims)
    assert not ok and reason == "newbie_window_passed"


# ── 凭证参数与金额 ───────────────────────────────────────────────────────────

def test_decision_first_charge_bonus_math_all_tiers():
    for sku, spec in RECHARGE_SKU_SPECS.items():
        d = recharge_voucher_decision(order(sku=sku), history=[], claims=[])
        assert d["action"] == "issue", sku
        base = spec["usd"] * RECHARGE_TOKENS_PER_USD
        bonus = base * spec["first_bonus_pct"] // 100
        assert d["meta"]["tokens"] == base + bonus, sku
        assert d["meta"]["first_charge"] is True
        assert d["args"]["chars"] == (base + bonus) * RECHARGE_CHARS_PER_TOKEN, sku
        assert d["args"]["ref"] == order()["id"]
        assert sku in d["args"]["note"]


def test_decision_repeat_no_bonus():
    prior = order(oid="AH-20260820-OLD1", sku="recharge-50", t=T0, status="activated")
    d = recharge_voucher_decision(order(sku="recharge-1000"), history=[prior], claims=[])
    assert d["action"] == "issue"
    assert d["meta"]["first_charge"] is False
    assert d["meta"]["tokens"] == 1000 * RECHARGE_TOKENS_PER_USD
    assert d["meta"]["vip_pct"] == 0  # 累计 50U < 500U 门槛
    assert "repeat" in d["args"]["note"]


# ── VIP 累充等级（实施50 P2：复充按累计已付充值加赠）────────────────────────

def test_repeat_bonus_pct_ladder_boundaries():
    assert repeat_bonus_pct(0) == 0
    assert repeat_bonus_pct(499) == 0
    assert repeat_bonus_pct(500) == 3
    assert repeat_bonus_pct(1999) == 3
    assert repeat_bonus_pct(2000) == 5
    assert repeat_bonus_pct(9999) == 5
    assert repeat_bonus_pct(10000) == 8
    assert repeat_bonus_pct(99999) == 8


def test_cumulative_usd_strictly_before_and_sku_priced():
    cur = order(oid="AH-20260821-CURR", sku="recharge-100", t=T2)
    hist = [
        order(oid="AH-20260820-A", sku="recharge-500", t=T0, status="activated"),
        order(oid="AH-20260820-B", sku=NEWBIE_SKU, t=T0, status="activated"),  # 新人包 6U 计入
        order(oid="AH-20260821-SAME", sku="recharge-200", t=T2, status="paid"),  # 同时刻不计
        order(oid="AH-20260822-LATER", sku="recharge-1000", t=T_LATE, status="paid"),  # 晚于本单不计
        order(oid="AH-20260820-PEND", sku="recharge-1000", t=T0, status="pending"),  # 未付不计
        order(oid="AH-20260820-OTHR", sku="recharge-1000", t=T0, status="paid",
              contact="@carol", fp="1111-2222-3333-4444"),  # 别人的不计
    ]
    assert cumulative_recharge_usd_before(cur, hist) == 506


def test_decision_repeat_vip_bonus_applied():
    prior = order(oid="AH-20260820-A", sku="recharge-500", t=T0, status="activated")
    d = recharge_voucher_decision(order(oid="AH-20260821-B", sku="recharge-100", t=T1),
                                  history=[prior], claims=[])
    assert d["action"] == "issue"
    assert d["meta"]["first_charge"] is False
    assert d["meta"]["vip_pct"] == 3 and d["meta"]["cum_usd"] == 500
    base = 100 * RECHARGE_TOKENS_PER_USD
    assert d["meta"]["tokens"] == base + base * 3 // 100  # 154,500
    assert d["args"]["chars"] == d["meta"]["tokens"] * RECHARGE_CHARS_PER_TOKEN
    assert "repeat_vip+3%" in d["args"]["note"]


def test_decision_first_charge_ignores_vip():
    """首单只走首充档（+40% 级别的一次性加赠），VIP 不叠加也不参与。"""
    d = recharge_voucher_decision(order(sku="recharge-200"), history=[], claims=[])
    assert d["meta"]["first_charge"] is True
    assert d["meta"]["bonus_pct"] == 10 and d["meta"]["vip_pct"] == 0


def test_refunded_history_rows_do_not_count():
    """退款单（status=refunded）在任何判定里都当不存在：不占首充、不进累计。"""
    refunded = order(oid="AH-20260820-RFND", sku="recharge-500", t=T0, status="refunded")
    cur = order(oid="AH-20260821-NEW1", sku="recharge-100", t=T1)
    assert is_first_recharge(cur, [refunded]) is True
    assert cumulative_recharge_usd_before(cur, [refunded]) == 0


def test_decision_newbie_chars():
    d = recharge_voucher_decision(order(sku=NEWBIE_SKU, t=T1), history=[], claims=[claim()])
    assert d["action"] == "issue"
    assert d["meta"]["tokens"] == NEWBIE_TOKENS
    assert d["args"]["chars"] == NEWBIE_TOKENS * RECHARGE_CHARS_PER_TOKEN


def test_decision_manual_paths():
    assert recharge_voucher_decision(order(contact=""), history=[], claims=[])["reason"] == "missing_contact"
    assert recharge_voucher_decision(order(delivery="hosted"), history=[], claims=[])["reason"] == "hosted_order"
    late = order(sku=NEWBIE_SKU, t=T_LATE)
    d = recharge_voucher_decision(late, history=[], claims=[claim(created=T0)])
    assert d["action"] == "manual" and d["reason"] == "newbie_window_passed"


def test_decision_skips_non_recharge():
    assert recharge_voucher_decision(order(sku="token-pack-s"), history=[], claims=[])["action"] == "skip"


# ── 履约规划器 ───────────────────────────────────────────────────────────────

def test_plan_buckets_and_idempotent_skips():
    good = order(oid="AH-20260821-GOOD", sku="recharge-200")
    done = order(oid="AH-20260821-DONE", sku="recharge-200")
    coded = order(oid="AH-20260821-CODE", sku="recharge-200", code="already")
    bad = order(oid="AH-20260821-BADD", sku=NEWBIE_SKU, t=T_LATE, contact="@dave",
                fp="9999-8888-7777-6666")
    tokenpack = order(oid="AH-20260821-TPCK", sku="token-pack-s")
    plan = plan_recharge_fulfillment(
        [good, done, coded, bad, tokenpack], {"AH-20260821-DONE"},
        history=[], claims=[claim(fp="9999-8888-7777-6666", contact="@dave", created=T0)])
    issue_ids = [o["id"] for o, _a, _m in plan["issue"]]
    manual_ids = [(o["id"], r) for o, r in plan["manual"]]
    assert issue_ids == ["AH-20260821-GOOD"]
    assert manual_ids == [("AH-20260821-BADD", "newbie_window_passed")]


def test_plan_simultaneous_first_charge_only_one_bonus():
    a = order(oid="AH-20260821-AAAA", sku="recharge-200", t=T1)
    b = order(oid="AH-20260821-BBBB", sku="recharge-200", t=T1)
    plan = plan_recharge_fulfillment([a, b], set(), history=[a, b], claims=[])
    metas = {o["id"]: m for o, _a, m in plan["issue"]}
    assert metas["AH-20260821-AAAA"]["first_charge"] is True
    assert metas["AH-20260821-BBBB"]["first_charge"] is False


def test_manual_followup_excludes_recharge_orders():
    # 充值单资格不符时由 plan_recharge_fulfillment 的 manual 队列点名（带原因），
    # 老的笼统点名函数必须让位，否则同一单被点两次名。
    o = order(sku="recharge-200", contact="")
    assert manual_followup_orders([o]) == []


# ── 跨仓价格交叉钉（同机有 website/ 才跑）────────────────────────────────────

def _site_src() -> str:
    site = Path(__file__).resolve().parents[3] / "website" / "lib" / "chatx-pricing.ts"
    if not site.exists():
        pytest.skip("website/ 不在本机（独立 CI 上下文），跳过跨仓充值档比对")
    return site.read_text(encoding="utf-8")


def _n(s: str) -> int:
    return int(s.replace("_", ""))


def test_recharge_specs_match_website_source():
    src = _site_src()
    site_tiers = {
        m.group(1): {"usd": _n(m.group(2)), "first_bonus_pct": _n(m.group(3))}
        for m in re.finditer(
            r'key:\s*"(recharge-[\w-]+)",\s*skuId:\s*"[\w-]+",\s*price:\s*([\d_]+),'
            r'\s*firstBonusPct:\s*([\d_]+)', src)
    }
    assert site_tiers, "官网 RECHARGE_TIERS 抽取为空——源码形状变了，更新本门禁正则"
    assert set(site_tiers) == set(RECHARGE_SKU_SPECS), (
        f"充值档集合分叉：官网 {sorted(site_tiers)} vs 引擎 {sorted(RECHARGE_SKU_SPECS)}")
    for sku, spec in RECHARGE_SKU_SPECS.items():
        assert site_tiers[sku] == spec, (
            f"{sku} 分叉：官网 {site_tiers[sku]} vs 引擎 {spec}（两处同批改）")


def test_newbie_and_rate_match_website_source():
    src = _site_src()
    m = re.search(r'RECHARGE_TOKENS_PER_USD\s*=\s*([\d_]+)', src)
    assert m and _n(m.group(1)) == RECHARGE_TOKENS_PER_USD
    nb = re.search(
        r'NEWBIE_PACK\s*=\s*\{[\s\S]{0,400}?tokens:\s*([\d_]+),[\s\S]{0,200}?'
        r'windowHours:\s*([\d_]+)', src)
    assert nb, "官网 NEWBIE_PACK 抽取失败——源码形状变了，更新本门禁正则"
    assert _n(nb.group(1)) == NEWBIE_TOKENS
    assert _n(nb.group(2)) == NEWBIE_WINDOW_HOURS


def test_vip_tiers_match_website_source():
    src = _site_src()
    tiers = [(_n(m.group(1)), _n(m.group(2))) for m in re.finditer(
        r'fromUsd:\s*([\d_]+),\s*pct:\s*([\d_]+)', src)]
    assert tiers, "官网 VIP_REPEAT_BONUS_TIERS 抽取为空——源码形状变了，更新本门禁正则"
    assert tiers == [(int(a), int(b)) for a, b in VIP_REPEAT_BONUS_TIERS], (
        f"VIP 累充等级分叉：官网 {tiers} vs 引擎 {VIP_REPEAT_BONUS_TIERS}（两处同批改）")
