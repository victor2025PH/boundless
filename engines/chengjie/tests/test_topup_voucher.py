"""P4c 字符加量凭证：签发/验签/兑换 + charpack 自动履约 + 误贴护栏。

覆盖面：
- 签发→验签 roundtrip；篡改/换钥匙 → bad_signature；授权码贴进兑换口 → not_voucher；
  凭证贴进授权口 → license_manager 判 invalid 并指路（双向误贴都有护栏）；
- redeem 全链：lic 精确绑定 / sub 客户级绑定 / 绑定不符 / 未激活 / 不限量 /
  同 ref 幂等（复用 quota_store.add_license_topup 的守卫）；
- 履约映射：charpack 订单 → select_topup_fulfillable；缺 contact 转人工点名；
  license 单与 topup 单互不越界；端到端「厂商签发 → 客户兑换 → 额度生效」。
"""

import time
from types import SimpleNamespace

import pytest

from src.licensing import LicenseManager, generate_keypair, issue_license
from src.licensing.quota_store import (
    check_license_quota,
    configure_license_quota_store,
    reset_license_quota_store,
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
def _clean_quota_singleton(tmp_path):
    reset_license_quota_store()
    configure_license_quota_store(db_path=tmp_path / "quota.db")
    yield
    reset_license_quota_store()


def _st(*, licensed=True, included=100_000, lic_id="lingox-pro-88",
        customer="boss@acme.com", state="active"):
    return SimpleNamespace(
        licensed=licensed, included_chars=included, lic_id=lic_id,
        customer=customer, enforce=False, state=state,
    )


# ── 签发 / 验签纯函数 ────────────────────────────────────────────────────────

def test_issue_verify_roundtrip(keypair):
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=1_500_000, ref="ORD-1",
        lic_id="L-1", customer="a@b.c", note="lingox-charpack")
    v = verify_topup_voucher(tok, keypair["public_hex"])
    assert v["ok"] is True
    p = v["payload"]
    assert p["typ"] == "topup" and p["chars"] == 1_500_000
    assert p["ref"] == "ORD-1" and p["lic"] == "L-1" and p["sub"] == "a@b.c"


def test_issue_guards(keypair):
    from src.licensing.license_manager import LicenseError
    with pytest.raises(LicenseError):
        issue_topup_voucher(keypair["private_hex"], chars=0, ref="R", lic_id="L")
    with pytest.raises(LicenseError):
        issue_topup_voucher(keypair["private_hex"], chars=10, ref="", lic_id="L")
    with pytest.raises(LicenseError):  # 无绑定=谁捡到谁兑，拒发
        issue_topup_voucher(keypair["private_hex"], chars=10, ref="R")


def test_verify_rejects_tamper_and_wrong_key(keypair):
    tok = issue_topup_voucher(keypair["private_hex"], chars=10, ref="R", lic_id="L")
    body, sig = tok.split(".", 1)
    assert verify_topup_voucher(body + "x." + sig, keypair["public_hex"])["error"] == "bad_signature"
    other = generate_keypair()
    assert verify_topup_voucher(tok, other["public_hex"])["error"] == "bad_signature"
    assert verify_topup_voucher("", keypair["public_hex"])["error"] == "bad_signature"
    assert verify_topup_voucher("no-dot", keypair["public_hex"])["error"] == "bad_signature"


def test_license_token_is_not_voucher(keypair):
    """把授权码贴进兑换框：验签会过（同一把钥匙）但 typ 不对 → not_voucher。"""
    lic = issue_license({"sub": "ACME", "plan": "pro"}, keypair["private_hex"])
    assert verify_topup_voucher(lic, keypair["public_hex"])["error"] == "not_voucher"


def test_voucher_pasted_as_license_is_invalid(keypair):
    """反向误贴：凭证当授权码激活 → 必须判 invalid 并指路，绝不能变成幽灵授权。"""
    tok = issue_topup_voucher(keypair["private_hex"], chars=10, ref="R", lic_id="L")
    st = LicenseManager(
        license_token=tok, public_key_hex=keypair["public_hex"]).status()
    assert st.state == "invalid"
    assert any("加量凭证" in m for m in st.messages)


# ── 兑换全链 ─────────────────────────────────────────────────────────────────

def test_redeem_lic_binding_and_idempotent(keypair):
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=50_000, ref="ORD-9", lic_id="lingox-pro-88")
    res = redeem_topup_voucher(
        tok, lic_status=_st(), public_key_hex=keypair["public_hex"])
    assert res["ok"] is True and res["chars"] == 50_000
    assert res["included"] == 150_000  # 100k 基础 + 50k 凭证
    q = check_license_quota(lic_status=_st())
    assert q["included"] == 150_000 and q["topup_chars"] == 50_000
    dup = redeem_topup_voucher(
        tok, lic_status=_st(), public_key_hex=keypair["public_hex"])
    assert dup["ok"] is False and dup["error"] == "duplicate_ref"


def test_redeem_sub_binding(keypair):
    """自动履约凭证按客户绑定（订单只有 contact）：sub 一致即可兑。"""
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=10_000, ref="ORD-S", customer="boss@acme.com")
    ok = redeem_topup_voucher(
        tok, lic_status=_st(), public_key_hex=keypair["public_hex"])
    assert ok["ok"] is True
    other = redeem_topup_voucher(
        issue_topup_voucher(keypair["private_hex"], chars=10_000, ref="ORD-S2",
                            customer="evil@else.com"),
        lic_status=_st(), public_key_hex=keypair["public_hex"])
    assert other["error"] == "customer_mismatch"


# ── contact_core 核心标识比对（P6 首单演练实锤修复，2026-07-24）─────────────


def test_contact_core_extraction():
    """TG handle / 邮箱 / 回退归一化三档；提取结果带类型前缀防跨类碰撞。"""
    from src.licensing.topup_voucher import contact_core

    # TG handle：前缀/大小写/@/备注全不敏感
    assert contact_core("tg:@Alice_88 (老板)") == "tg:alice_88"
    assert contact_core("Telegram: alice_88") == "tg:alice_88"
    assert contact_core("t.me/ALICE_88") == "tg:alice_88"
    assert contact_core("@alice_88") == "tg:alice_88"
    # 邮箱：大小写/围绕文本无关
    assert contact_core("联系 Boss@Acme.COM 下单") == "mail:boss@acme.com"
    # 回退：casefold + 去空白（无 handle/邮箱时仍是精确语义）
    assert contact_core("微信 wxid 12345") == "raw:微信wxid12345"
    assert contact_core("") == ""
    # 类型前缀防「raw 串恰好等于别人 handle」跨类误配
    assert contact_core("alice_88 无前缀纯文本") != contact_core("@alice_88")


def test_redeem_sub_binding_core_match(keypair):
    """P6 演练实锤：订阅单与加量包单 contact 只差备注 → 核心标识一致必须放行。"""
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=1_500_000, ref="AH-20260724-G0EARY",
        customer="tg:@boundless_selftest (P6 charpack 演练)")
    st = _st(customer="tg:@boundless_selftest (P6 首单演练)")
    res = redeem_topup_voucher(tok, lic_status=st, public_key_hex=keypair["public_hex"])
    assert res["ok"] is True and res["chars"] == 1_500_000
    # 不同 handle 仍拒（防串号语义不变）
    evil = redeem_topup_voucher(
        issue_topup_voucher(keypair["private_hex"], chars=10, ref="R-EVIL",
                            customer="tg:@someone_else9"),
        lic_status=st, public_key_hex=keypair["public_hex"])
    assert evil["error"] == "customer_mismatch"
    # 授权侧 customer 为空 → 核心标识空 → 拒（凭证不能兑进无主授权）
    empty = redeem_topup_voucher(
        issue_topup_voucher(keypair["private_hex"], chars=10, ref="R-EMPTY",
                            customer="tg:@boundless_selftest"),
        lic_status=_st(customer=""), public_key_hex=keypair["public_hex"])
    assert empty["error"] == "customer_mismatch"


def test_redeem_binding_and_state_guards(keypair):
    lic_tok = issue_topup_voucher(
        keypair["private_hex"], chars=10, ref="R1", lic_id="OTHER-LIC")
    assert redeem_topup_voucher(
        lic_tok, lic_status=_st(), public_key_hex=keypair["public_hex"],
    )["error"] == "lic_mismatch"
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=10, ref="R2", lic_id="lingox-pro-88")
    assert redeem_topup_voucher(
        tok, lic_status=_st(licensed=False, state="unlicensed"),
        public_key_hex=keypair["public_hex"])["error"] == "not_licensed"
    assert redeem_topup_voucher(
        tok, lic_status=_st(included=0),
        public_key_hex=keypair["public_hex"])["error"] == "unlimited"
    assert redeem_topup_voucher(
        "garbage", lic_status=_st(),
        public_key_hex=keypair["public_hex"])["error"] == "bad_signature"


def test_lic_binding_wins_over_sub(keypair):
    """lic 与 sub 同时在场：lic 不符即拒，哪怕 sub 相同（精确绑定优先）。"""
    tok = issue_topup_voucher(
        keypair["private_hex"], chars=10, ref="R3",
        lic_id="OTHER", customer="boss@acme.com")
    assert redeem_topup_voucher(
        tok, lic_status=_st(), public_key_hex=keypair["public_hex"],
    )["error"] == "lic_mismatch"


# ── charpack 履约映射（chatx_fulfillment）────────────────────────────────────

def _order(oid="W-1", sku="lingox-charpack", contact="boss@acme.com", **kw):
    o = {"id": oid, "sku_id": sku, "contact": contact, "product_id": "tongyi"}
    o.update(kw)
    return o


def test_topup_voucher_args_for_order():
    from src.licensing.chatx_fulfillment import topup_voucher_args_for_order
    args = topup_voucher_args_for_order(_order())
    assert args == {"chars": 1_500_000, "ref": "W-1",
                    "customer": "boss@acme.com", "note": "lingox-charpack"}
    assert topup_voucher_args_for_order(_order(contact="")) is None  # 无绑定面
    assert topup_voucher_args_for_order(_order(sku="lingox-pro")) is None
    assert topup_voucher_args_for_order({}) is None


def test_select_topup_fulfillable_and_manual_split():
    from src.licensing.chatx_fulfillment import (
        manual_followup_orders,
        select_fulfillable,
        select_topup_fulfillable,
    )
    orders = [
        _order("W-1"),                                   # charpack → 凭证
        _order("W-2", sku="lingox-pro"),                 # license 单
        _order("W-3", contact=""),                       # charpack 缺 contact → 人工
        _order("W-4", code="already"),                   # 已回填
        {"id": "W-5", "sku_id": "voicex-std", "contact": "x"},  # 外产品
    ]
    topups = select_topup_fulfillable(orders)
    assert [o["id"] for o, _ in topups] == ["W-1"]
    lic = select_fulfillable(orders)
    assert [o["id"] for o, _ in lic] == ["W-2"]
    manual = manual_followup_orders(orders)
    assert [o["id"] for o in manual] == ["W-3"]
    # done 幂等
    assert select_topup_fulfillable(orders, {"W-1"}) == []


def test_charpack_not_a_license_sku():
    """charpack 绝不能被当 plan license 签发（会覆掉客户订阅档）。"""
    from src.licensing.chatx_fulfillment import (
        MANUAL_SKUS,
        fulfillment_payload_for_order,
        sku_spec,
    )
    assert fulfillment_payload_for_order(_order()) is None
    with pytest.raises(ValueError):
        sku_spec("lingox-charpack")
    assert "lingox-charpack" not in MANUAL_SKUS  # 已迁自动凭证通道


def test_end_to_end_vendor_to_customer(keypair):
    """端到端：官网订单 → 守护签凭证（args 直传 issue）→ 客户兑换 → 额度生效。"""
    from src.licensing.chatx_fulfillment import topup_voucher_args_for_order
    args = topup_voucher_args_for_order(_order("ORD-E2E"))
    token = issue_topup_voucher(keypair["private_hex"], **args)
    res = redeem_topup_voucher(
        token, lic_status=_st(), public_key_hex=keypair["public_hex"])
    assert res["ok"] is True
    q = check_license_quota(lic_status=_st())
    assert q["included"] == 100_000 + 1_500_000
    assert q["topup_chars"] == 1_500_000


# ── 批量签发（license_tool --count，大客户一次买 N 包）──────────────────────

def test_batch_refs_derivation():
    """count==1 原样；N>1 零填充序号宽度自适应；ref 全体唯一。"""
    from src.licensing.topup_voucher import batch_refs

    assert batch_refs("ORD-9", 1) == ["ORD-9"]
    refs = batch_refs("ORD-9", 3)
    assert refs == ["ORD-9-01", "ORD-9-02", "ORD-9-03"]
    wide = batch_refs("O", 120)
    assert wide[0] == "O-001" and wide[-1] == "O-120"
    assert len(set(wide)) == 120
    assert batch_refs("R", 0) == ["R"]   # 非法 count 回落单张


def test_batch_vouchers_redeem_independently(keypair):
    """批量 3 张：逐张兑换独立入账、任一张重复兑换幂等拒绝、合计正确。"""
    from src.licensing.topup_voucher import batch_refs

    tokens = [
        issue_topup_voucher(keypair["private_hex"], chars=500_000, ref=r,
                            customer="boss@acme.com", note="batch")
        for r in batch_refs("ORD-BAT", 3)
    ]
    for t in tokens:
        assert redeem_topup_voucher(
            t, lic_status=_st(), public_key_hex=keypair["public_hex"],
        )["ok"] is True
    dup = redeem_topup_voucher(
        tokens[1], lic_status=_st(), public_key_hex=keypair["public_hex"])
    assert dup["ok"] is False and dup["error"] == "duplicate_ref"
    q = check_license_quota(lic_status=_st())
    assert q["topup_chars"] == 1_500_000   # 3 × 50 万，一张不多一张不少
