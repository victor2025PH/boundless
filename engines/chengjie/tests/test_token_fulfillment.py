# -*- coding: utf-8 -*-
"""2026-08-19 Token 定价改版 P3 履约门禁：新 SKU specs / 按坐席签发 / Token 包凭证。

钉住：
1. 新档 payload 契约：personal/team-seat/flagship/workbench 的 seats、Token 月含量、
   **新档不带 included_chars（=标准翻译免费）**；停售档行为原样（历史续费不破）。
2. 按坐席签发：订单 seats clamp [min,max]、Token 月含量 ×seats、缺省按下限。
3. Token 包 → topup 凭证（双载荷轨道）：签发/验签/兑换入客户钱包、同 ref 幂等、
   绑定校验复用字符包全套护栏；字符包老轨道零回归。
4. 钱包主键 = contact_core（跨续费存活——P3 设计抓到的关键缺陷的回归钉）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.licensing import generate_keypair
from src.licensing.chatx_fulfillment import (
    TOPUP_SKU_TOKENS,
    build_issue_payload,
    fulfillment_payload_for_order,
    is_chatx_order,
    select_topup_fulfillable,
    topup_voucher_args_for_order,
)
from src.licensing.token_ledger import (
    TokenLedgerStore,
    configure_token_ledger,
    ensure_monthly_tokens,
    record_action_for_status,
    reset_token_ledger,
    wallet_id_for_status,
    wallet_snapshot,
)
from src.licensing.topup_voucher import (
    issue_topup_voucher,
    redeem_topup_voucher,
    verify_topup_voucher,
)


@pytest.fixture()
def keypair():
    return generate_keypair()


@pytest.fixture(autouse=True)
def _isolated_ledger():
    reset_token_ledger()
    store = TokenLedgerStore(":memory:")
    configure_token_ledger(enabled=True, store=store)
    yield store
    reset_token_ledger()


def _st(*, licensed=True, lic_id="chatx-personal-AH1", customer="tg:@boss",
        included_tokens_monthly=0, state="active"):
    return SimpleNamespace(
        licensed=licensed, lic_id=lic_id, customer=customer,
        included_chars=0, included_tokens_monthly=included_tokens_monthly,
        enforce=False, state=state,
    )


# ── 1) 新档 payload 契约 ─────────────────────────────────────────────────────

def test_personal_payload_tokens_no_chars():
    p = build_issue_payload("chatx-personal", customer="a@b.c", order_id="O1")
    assert p["plan"] == "basic" and p["seats"] == 1
    assert p["included_tokens_monthly"] == 30_000
    assert "included_chars" not in p          # 字符不限 = 标准翻译免费（引擎侧实现）
    assert p["sku_id"] == "chatx-personal" and p["product_id"] == "zhiliao"


def test_team_seat_scales_by_order_seats():
    p5 = build_issue_payload("chatx-team-seat", customer="c", seats=5)
    assert p5["seats"] == 5
    assert p5["included_tokens_monthly"] == 250_000   # 50k × 5
    # 缺省/越界 clamp
    assert build_issue_payload("chatx-team-seat", customer="c")["seats"] == 2
    assert build_issue_payload("chatx-team-seat", customer="c", seats=0)["seats"] == 2
    assert build_issue_payload("chatx-team-seat", customer="c", seats=99)["seats"] == 50


def test_flagship_and_workbench_contract():
    f = build_issue_payload("chatx-flagship", customer="c")
    assert f["seats"] == 50 and "included_tokens_monthly" not in f  # 0 = 不写
    assert "included_chars" not in f
    w = build_issue_payload("lingox-workbench", customer="c", seats=3)
    assert w["plan"] == "basic" and w["seats"] == 3
    assert w["product_id"] == "tongyi" and w["features"].get("analytics") is True
    assert "included_chars" not in w          # 翻译免费化：工作台不带字符池


def test_legacy_specs_unchanged():
    """停售档历史续费仍按旧契约签发（included_chars 语义不破）。"""
    e = build_issue_payload("chatx-entry", customer="c")
    assert e["seats"] == 3 and e["channels"] == ["telegram"]
    t = build_issue_payload("lingox-team", customer="c", days=32)
    assert t["included_chars"] == 3_000_000   # 月付 1 个月
    assert "included_tokens_monthly" not in t


def test_fulfillment_passes_order_seats():
    order = {"id": "AH-1", "sku_id": "chatx-team-seat", "product_id": "zhiliao",
             "contact": "tg:@boss", "period": "monthly", "seats": 4}
    p = fulfillment_payload_for_order(order)
    assert p is not None and p["seats"] == 4
    assert p["included_tokens_monthly"] == 200_000


# ── 2) Token 包 → 凭证轨道 ───────────────────────────────────────────────────

def test_token_pack_voucher_args_and_selection():
    ok = {"id": "AH-2", "sku_id": "token-pack-m", "product_id": "zhiliao",
          "contact": "a@b.c"}
    args = topup_voucher_args_for_order(ok)
    assert args == {"tokens": 60_000, "ref": "AH-2", "customer": "a@b.c",
                    "note": "token-pack-m"}
    assert TOPUP_SKU_TOKENS["token-pack-xl"] == 1_000_000
    # 字符包老轨道零回归
    legacy = {"id": "AH-3", "sku_id": "lingox-charpack", "contact": "a@b.c"}
    assert topup_voucher_args_for_order(legacy)["chars"] == 1_500_000
    # 缺 contact 拒绝裸发
    assert topup_voucher_args_for_order({"id": "x", "sku_id": "token-pack-s"}) is None
    # select_topup_fulfillable 双轨道通吃
    picked = select_topup_fulfillable([ok, legacy])
    assert len(picked) == 2
    assert is_chatx_order(ok) is True         # token-pack 前缀归 chatx 引擎


def test_token_voucher_issue_verify_redeem(keypair, _isolated_ledger):
    tok = issue_topup_voucher(
        keypair["private_hex"], tokens=60_000, ref="AH-9",
        customer="tg:@boss", note="token-pack-m")
    v = verify_topup_voucher(tok, keypair["public_hex"])
    assert v["ok"] is True and v["payload"]["tokens"] == 60_000
    assert "chars" not in v["payload"]
    st = _st(customer="Tg: @Boss (老板)")     # 写法不同但同一客户（contact_core）
    res = redeem_topup_voucher(tok, lic_status=st, public_key_hex=keypair["public_hex"])
    assert res["ok"] is True and res["tokens"] == 60_000
    assert res["balance"] == 60_000
    # 同 ref 幂等拒绝
    res2 = redeem_topup_voucher(tok, lic_status=st, public_key_hex=keypair["public_hex"])
    assert res2["ok"] is False and res2["error"] == "duplicate_ref"


def test_token_voucher_binding_guards(keypair):
    tok = issue_topup_voucher(
        keypair["private_hex"], tokens=10_000, ref="AH-10", customer="a@b.c")
    stranger = _st(customer="someone@else.com")
    res = redeem_topup_voucher(tok, lic_status=stranger,
                               public_key_hex=keypair["public_hex"])
    assert res["ok"] is False and res["error"] == "customer_mismatch"
    unlic = _st(licensed=False)
    res2 = redeem_topup_voucher(tok, lic_status=unlic,
                                public_key_hex=keypair["public_hex"])
    assert res2["ok"] is False and res2["error"] == "not_licensed"


def test_voucher_payload_validation(keypair):
    from src.licensing.license_manager import LicenseError
    with pytest.raises(LicenseError):        # chars/tokens 都缺
        issue_topup_voucher(keypair["private_hex"], ref="R", customer="a@b.c")
    # 双载荷（换发场景）合法
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=1_500_000, tokens=60_000, ref="R2",
        customer="a@b.c")
    v = verify_topup_voucher(tok, keypair["public_hex"])
    assert v["ok"] is True
    assert v["payload"]["chars"] == 1_500_000 and v["payload"]["tokens"] == 60_000


# ── 3) 钱包主键 = 客户身份（跨续费存活）─────────────────────────────────────

def test_wallet_id_survives_license_renewal(_isolated_ledger):
    s = _isolated_ledger
    st1 = _st(lic_id="chatx-personal-AH1", customer="tg:@boss")
    st2 = _st(lic_id="chatx-personal-AH2", customer="Tg: @BOSS")  # 续费=新 lic_id
    assert wallet_id_for_status(st1) == wallet_id_for_status(st2) == "tg:boss"
    s.grant_pack(wallet_id_for_status(st1), 10_000, ref="o1")
    # 换了授权号，余额仍在
    snap = wallet_snapshot(st2)
    assert snap["enabled"] is True and snap["balance"] == 10_000
    # 无 customer 的老授权回退 lic_id
    st3 = _st(lic_id="L-legacy", customer="")
    assert wallet_id_for_status(st3) == "L-legacy"


def test_monthly_grant_and_action_record(_isolated_ledger):
    st = _st(customer="a@b.c", included_tokens_monthly=30_000)
    assert ensure_monthly_tokens(st) is True
    assert ensure_monthly_tokens(st) is False       # 同月幂等
    n = record_action_for_status("ai_reply", 1, lic_status=st)
    assert n == 10
    snap = wallet_snapshot(st)
    assert snap["balance"] == 29_990 and snap["monthly"] == 30_000
    assert snap["by_action"] == {"ai_reply": 10}
    assert snap["grants"] and snap["grants"][0]["kind"] == "monthly"
    assert snap["grants"][0]["expires_day"]          # 展示就绪：有到期日


def test_record_noop_when_disabled():
    reset_token_ledger()
    configure_token_ledger(enabled=False, store=TokenLedgerStore(":memory:"))
    st = _st(included_tokens_monthly=30_000)
    assert record_action_for_status("ai_reply", 1, lic_status=st) == 0
    assert wallet_snapshot(st)["enabled"] is False
