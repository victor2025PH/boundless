"""Sprint3：chatx SKU→payload 映射 + 端到端签发→验签（厂商机履约层）。"""
import pytest

from src.licensing.chatx_fulfillment import (
    ALL_CHANNELS,
    CHATX_SKU_SPECS,
    build_issue_payload,
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
