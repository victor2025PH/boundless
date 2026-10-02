"""P1-5：群控登录页单独品牌「智拓群控」，不影响其他产品。"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.utils.branding import DEFAULT_PRODUCT_NAME, get_branding

ENGINE = Path(__file__).resolve().parents[1]


def _preset():
    return yaml.safe_load((ENGINE / "config/presets/fleet_control.yaml").read_text(encoding="utf-8"))


def test_fleet_preset_brand_is_zhituo():
    b = get_branding(_preset())
    assert b["product_name"] == "智拓群控"
    assert b["site_name"] == "智拓群控 · 节点机群主控"
    assert b["login_line"] == "智拓群控 · 节点机群主控"
    assert b["sidebar_name"] == "无界 · 智拓群控"


def test_other_products_keep_default_brand():
    assert DEFAULT_PRODUCT_NAME == "智聊"
    assert get_branding({})["product_name"] == "智聊"
    for name in ("config.example.yaml",):
        cfg = yaml.safe_load((ENGINE / "config" / name).read_text(encoding="utf-8")) or {}
        assert (cfg.get("brand") or {}).get("product_name") in (None, "", "智聊")


def test_login_template_uses_product_name():
    html = (ENGINE / "src/web/templates/login.html").read_text(encoding="utf-8")
    assert "product_name" in html
    for rel in ("domains/fleet_control/manifest.yaml", "domains/fleet_control/config/defaults.yaml"):
        assert "智拓群控" in (ENGINE / rel).read_text(encoding="utf-8")
