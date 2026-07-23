"""融合实例 P4：lingox SKU 入履约表。

不变量：
- lingox 档位语义＝翻译线：plan 恒 basic，差异走 seats/channels/显式 features；
  绝不发 pro（防与同价 chatx-team 无差异 + 白送 ai_autosend）。
- lingox-charpack（字符加购包）不发 plan license（会覆掉订阅档）→ 走 topup 凭证
  自动履约（P4c，见 test_topup_voucher.py）；缺 contact 才转人工且守护可见。
- chatx 既有行为零变化（product_id 仍 zhiliao、features 仍空）。
- 端到端：签发的 lingox license 在 feature_gate 下 translation_suite+analytics 开、
  ai_autosend/companion 锁。
"""
import pytest

from src.licensing.chatx_fulfillment import (
    ALL_CHANNELS,
    LINGOX_SKU_SPECS,
    MANUAL_SKUS,
    build_issue_payload,
    fulfillment_payload_for_order,
    is_chengjie_order,
    is_lingox_order,
    manual_followup_orders,
    select_fulfillable,
    sku_spec,
)
from src.licensing.feature_gate import feature_enabled
from src.licensing.license_manager import (
    LicenseManager,
    generate_keypair,
    issue_license,
)


def test_lingox_specs_are_translation_line():
    """plan 恒 basic；seats 阶梯 5→15；全渠道；analytics 显式授予。"""
    for sid, spec in LINGOX_SKU_SPECS.items():
        assert spec["plan"] == "basic", sid
        assert set(spec["channels"]) == set(ALL_CHANNELS), sid
        assert spec["features"].get("analytics") is True, sid
        assert spec["product_id"] == "tongyi", sid
    assert sku_spec("lingox-team")["seats"] == 5
    assert sku_spec("lingox-pro")["seats"] == 15


def test_charpack_never_issues_plan_license():
    """charpack 绝不能走 license 签发通道（P4c 起改 topup 凭证，此不变量仍须守住）。"""
    assert "lingox-charpack" not in MANUAL_SKUS  # 已迁自动凭证通道
    with pytest.raises(ValueError):
        sku_spec("lingox-charpack")
    order = {"id": "O1", "sku_id": "lingox-charpack", "contact": "c@x"}
    assert is_lingox_order(order) is True
    assert fulfillment_payload_for_order(order) is None
    # 有 contact（可绑定）→ 凭证通道接走，不再进人工清单
    assert manual_followup_orders([order]) == []
    # 缺 contact（无绑定面）→ 仍要人工点名，绝不静默漏单
    naked = {"id": "O2", "sku_id": "lingox-charpack", "contact": ""}
    assert [o["id"] for o in manual_followup_orders([naked])] == ["O2"]


def test_lingox_payload_carries_product_and_features():
    p = build_issue_payload("lingox-team", customer="Acme", order_id="ORD1")
    assert p["plan"] == "basic"
    assert p["seats"] == 5
    assert p["product_id"] == "tongyi"
    assert p["sku_id"] == "lingox-team"
    assert p["lic_id"] == "lingox-team-ORD1"
    assert p["features"] == {"analytics": True}
    # 调用方显式 features 与 spec 默认合并，同 key 调用方赢
    p2 = build_issue_payload(
        "lingox-pro", customer="c", features={"analytics": False, "kb": True})
    assert p2["features"] == {"analytics": False, "kb": True}


def test_chatx_behavior_unchanged():
    p = build_issue_payload("chatx-team", customer="c")
    assert p["product_id"] == "zhiliao"
    assert p["features"] == {}


def test_order_family_detection():
    assert is_lingox_order({"sku_id": "lingox-pro"}) is True
    assert is_lingox_order({"sku_id": "", "product_id": "tongyi"}) is True
    assert is_lingox_order({"sku_id": "chatx-team"}) is False
    assert is_chengjie_order({"sku_id": "chatx-entry"}) is True
    assert is_chengjie_order({"sku_id": "lingox-team"}) is True
    assert is_chengjie_order({"sku_id": "voicex-std"}) is False


def test_select_fulfillable_mixed_batch():
    """chatx+lingox 混批：两家都出单；charpack/外族/已回填 不入自动清单。"""
    orders = [
        {"id": "A", "sku_id": "chatx-team", "contact": "a", "period": "monthly"},
        {"id": "B", "sku_id": "lingox-pro", "contact": "b", "period": "annual"},
        {"id": "C", "sku_id": "lingox-charpack", "contact": "c"},
        {"id": "D", "sku_id": "voicex-std", "contact": "d"},        # 外族引擎
        {"id": "E", "sku_id": "lingox-team", "contact": "e", "code": "tok"},  # 已回填
    ]
    todo = select_fulfillable(orders)
    got = {o["id"]: p for o, p in todo}
    assert set(got) == {"A", "B"}
    assert got["A"]["plan"] == "pro" and got["A"]["product_id"] == "zhiliao"
    assert got["B"]["plan"] == "basic" and got["B"]["product_id"] == "tongyi"
    # annual 周期 → ~366 天
    assert got["B"]["exp"] - got["A"]["exp"] > 300 * 86400
    # charpack(C) 走凭证通道（P4c）；人工清单为空（D 非本引擎、E 已回填）
    from src.licensing.chatx_fulfillment import select_topup_fulfillable
    assert [o["id"] for o, _ in select_topup_fulfillable(orders)] == ["C"]
    assert manual_followup_orders(orders) == []


def test_end_to_end_lingox_license_under_feature_gate():
    """签发→验签→feature_gate：翻译+analytics 开，autosend/companion/rpa 锁。"""
    kp = generate_keypair()
    payload = build_issue_payload("lingox-pro", customer="Trans Co", order_id="ORD7")
    token = issue_license(payload, kp["private_hex"])
    st = LicenseManager(license_token=token, public_key_hex=kp["public_hex"]).status()
    assert st.state == "active"
    assert st.plan == "basic"
    assert st.seats == 15
    cfg = {"licensing": {"feature_gate": {"enabled": True}}}
    assert feature_enabled("translation_suite", cfg, st) is True   # basic 档默认
    assert feature_enabled("analytics", cfg, st) is True           # 显式 features 越档授予
    assert feature_enabled("ai_autosend", cfg, st) is False        # pro 卖点不白送
    assert feature_enabled("companion", cfg, st) is False
    assert feature_enabled("rpa", cfg, st) is False


def test_lingox_team_vs_pro_only_capacity_differs():
    """team 与 pro 的差异是容量（seats），功能位一致——翻译线的档位哲学。"""
    t = build_issue_payload("lingox-team", customer="c")
    p = build_issue_payload("lingox-pro", customer="c")
    assert t["plan"] == p["plan"] == "basic"
    assert t["features"] == p["features"]
    assert set(t["channels"]) == set(p["channels"])
    assert t["seats"] < p["seats"]
