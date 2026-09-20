"""P7 会员中心购买 CTA → 官网 /order 深链纯函数门禁。"""

from src.licensing.shop_link import (
    build_shop_url,
    resolve_shop_offer,
    resolve_upgrade_offer,
    shop_cta_from_license,
)


# ── E5：升级 CTA 按目标档挑 offer（与「续费当前档」语义相对） ────────────────

def test_upgrade_offer_chatx_targets_by_plan():
    assert resolve_upgrade_offer(
        sku_id="chatx-entry-AH-1", target_plan="pro") == "autochat-team"
    assert resolve_upgrade_offer(
        sku_id="chatx-team-ORD", target_plan="flagship") == "autochat-flagship"
    assert resolve_upgrade_offer(
        product_id="zhiliao", target_plan="basic") == "autochat-entry"


def test_upgrade_offer_conservative_blanks():
    """通译族升档语义不同、族不可知、目标档非法 → 一律空串（调用方回落
    按当前授权的 shop.url，家族永远不指错）。"""
    assert resolve_upgrade_offer(sku_id="lingox-pro-1", target_plan="pro") == ""
    assert resolve_upgrade_offer(product_id="tongyi", target_plan="pro") == ""
    assert resolve_upgrade_offer(target_plan="pro") == ""
    assert resolve_upgrade_offer(sku_id="chatx-entry", target_plan="galaxy") == ""


def test_resolve_offer_by_sku_prefix():
    assert resolve_shop_offer(sku_id="chatx-entry-AH-1") == "autochat-entry"
    assert resolve_shop_offer(sku_id="chatx-team-ORD") == "autochat-team"
    assert resolve_shop_offer(sku_id="lingox-team-X") == "translate-team"
    assert resolve_shop_offer(sku_id="lingox-pro-Y") == "translate-pro"
    # 长前缀优先：flagship 不被 entry/team 吞
    assert resolve_shop_offer(sku_id="chatx-flagship-9") == "autochat-flagship"


def test_resolve_offer_quota_exceeded_prefers_charpack():
    """通译额度耗尽 → 直达字符包（不是再买一份订阅）。"""
    assert resolve_shop_offer(
        sku_id="lingox-team-AH", quota_exceeded=True) == "translate-charpack"
    assert resolve_shop_offer(
        product_id="tongyi", quota_exceeded=True) == "translate-charpack"
    # chatx 不计量 → exceeded 也不误指字符包
    assert resolve_shop_offer(
        sku_id="chatx-entry-AH", quota_exceeded=True) == "autochat-entry"


def test_resolve_offer_fallbacks():
    assert resolve_shop_offer(product_id="tongyi") == "translate-team"
    assert resolve_shop_offer(product_id="zhiliao") == "autochat-entry"
    assert resolve_shop_offer(plan="pro") == "autochat-team"
    assert resolve_shop_offer(plan="community") == ""


def test_build_shop_url_only_on_order_path():
    assert build_shop_url("https://bd2026.cc/order", "autochat-entry") == (
        "https://bd2026.cc/order?plan=autochat-entry")
    assert build_shop_url("https://bd2026.cc/order/", "translate-team",
                          period="annual") == (
        "https://bd2026.cc/order/?plan=translate-team&period=annual")
    # 相对路径
    assert build_shop_url("/order", "autochat-team").endswith(
        "plan=autochat-team")
    # 非 order 基址（TG 客服）绝不污染
    tg = "https://t.me/boundless_support"
    assert build_shop_url(tg, "autochat-entry") == tg
    # 运营已写 plan → 不覆盖
    assert build_shop_url(
        "https://bd2026.cc/order?plan=translate-pro", "autochat-entry"
    ) == "https://bd2026.cc/order?plan=translate-pro"
    # 空 offer / 空 base
    assert build_shop_url("https://bd2026.cc/order", "") == "https://bd2026.cc/order"
    assert build_shop_url("", "autochat-entry") == ""


def test_shop_cta_from_license_e2e():
    cta = shop_cta_from_license(
        "https://bd2026.cc/order",
        {"sku_id": "lingox-team-AH-1", "product_id": "tongyi", "plan": "basic"},
        quota_exceeded=True,
    )
    assert cta["offer"] == "translate-charpack"
    assert "plan=translate-charpack" in cta["url"]
