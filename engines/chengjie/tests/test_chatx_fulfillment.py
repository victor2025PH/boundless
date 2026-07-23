"""Sprint3：chatx SKU→payload 映射 + 端到端签发→验签（厂商机履约层）。"""
import pytest

from src.licensing.chatx_fulfillment import (
    ALL_CHANNELS,
    CHATX_SKU_SPECS,
    build_issue_payload,
    fulfillment_payload_for_order,
    is_chatx_order,
    select_fulfillable,
    sku_spec,
)
from src.licensing.license_manager import (
    LicenseManager,
    generate_keypair,
    issue_license,
)


def test_sku_specs_seats_and_plan():
    assert sku_spec("chatx-entry")["seats"] == 3
    assert sku_spec("chatx-entry")["plan"] == "basic"
    assert sku_spec("chatx-team")["seats"] == 10
    assert sku_spec("chatx-team")["plan"] == "pro"
    assert sku_spec("chatx-flagship")["seats"] == 50
    assert sku_spec("chatx-flagship")["plan"] == "flagship"


def test_unknown_sku_raises():
    with pytest.raises(ValueError):
        sku_spec("matrixx-gold")  # 非 chatx
    with pytest.raises(ValueError):
        build_issue_payload("nope", customer="X")


def test_entry_single_platform_team_all_channels():
    ep = build_issue_payload("chatx-entry", customer="c")
    assert ep["channels"] == ["telegram"]  # 「1 平台」默认
    tp = build_issue_payload("chatx-team", customer="c")
    assert set(tp["channels"]) == set(ALL_CHANNELS)


def test_payload_carries_sku_and_order_lic_id():
    p = build_issue_payload("chatx-team", customer="Acme", order_id="ORD9")
    assert p["sku_id"] == "chatx-team"
    assert p["product_id"] == "zhiliao"
    assert p["lic_id"] == "chatx-team-ORD9"
    assert p["sub"] == "Acme"


def test_days_maps_to_exp():
    p = build_issue_payload("chatx-entry", customer="c", days=7, now=1_000_000)
    assert p["exp"] == 1_000_000 + 7 * 86400
    # days<=0 → 永久（不写 exp）
    perp = build_issue_payload("chatx-flagship", customer="c", days=0)
    assert "exp" not in perp


def test_end_to_end_issue_and_verify():
    """厂商机签发 → 引擎验签：状态 active，plan/seats/channels 与档位一致。"""
    kp = generate_keypair()
    payload = build_issue_payload("chatx-team", customer="Acme Ltd", order_id="ORD42")
    token = issue_license(payload, kp["private_hex"])
    st = LicenseManager(license_token=token, public_key_hex=kp["public_hex"]).status()
    assert st.state == "active"
    assert st.licensed is True
    assert st.plan == "pro"
    assert st.seats == 10
    assert "telegram" in st.channels and "web" in st.channels
    assert st.days_left is not None and 30 <= st.days_left <= 32


def test_end_to_end_expired_when_days_negative_window():
    """签发一张过去到期的 chatx license → 引擎判为过期（超宽限）。"""
    kp = generate_keypair()
    # now 设很久以前 + days 短 → exp 已过且超 grace
    payload = build_issue_payload("chatx-entry", customer="c", days=1, now=1_000_000)
    token = issue_license(payload, kp["private_hex"])
    st = LicenseManager(license_token=token, public_key_hex=kp["public_hex"]).status()
    assert st.state == "expired"
    assert st.licensed is False


# ── Sprint4 履约守护纯逻辑 ───────────────────────────────────────────────

def test_is_chatx_order():
    assert is_chatx_order({"sku_id": "chatx-team"}) is True
    assert is_chatx_order({"product_id": "zhiliao"}) is True  # 老单兜底
    assert is_chatx_order({"sku_id": "lingox-pro", "product_id": "tongyi"}) is False
    assert is_chatx_order({}) is False


def test_fulfillment_payload_maps_period_to_days():
    monthly = fulfillment_payload_for_order(
        {"id": "O1", "sku_id": "chatx-team", "contact": "a@b.c", "period": "monthly"})
    assert monthly["plan"] == "pro" and monthly["seats"] == 10
    assert monthly["lic_id"] == "chatx-team-O1"
    # annual → 366 天窗口（exp 明显更远）
    annual = fulfillment_payload_for_order(
        {"id": "O2", "sku_id": "chatx-team", "period": "annual"})
    assert annual["exp"] > monthly["exp"]


def test_fulfillment_payload_none_for_unmappable():
    # 非 chatx
    assert fulfillment_payload_for_order({"id": "X", "sku_id": "lingox-pro"}) is None
    # chatx 但 sku_id 缺失/未知（老单）→ 转人工
    assert fulfillment_payload_for_order({"id": "Y", "product_id": "zhiliao"}) is None


def test_select_fulfillable_skips_done_coded_and_nonchatx():
    orders = [
        {"id": "O1", "sku_id": "chatx-entry", "contact": "a", "period": "monthly"},
        {"id": "O2", "sku_id": "chatx-team", "code": "已回填"},          # 已回填 → 跳
        {"id": "O3", "sku_id": "chatx-flagship"},                        # 待履约
        {"id": "O4", "sku_id": "lingox-pro", "product_id": "tongyi"},    # 非 chatx → 跳
        {"id": "O5", "product_id": "zhiliao"},                           # 无 sku_id → 跳(转人工)
        {"id": "", "sku_id": "chatx-team"},                              # 无 id → 跳
    ]
    picked = select_fulfillable(orders, done_ids={"O1"})  # O1 已 done → 跳
    ids = [o["id"] for o, _ in picked]
    assert ids == ["O3"]
    assert picked[0][1]["sku_id"] == "chatx-flagship"
