"""登录 ?next= 安全回跳 + 托管交付深链门禁。"""
from __future__ import annotations

import pytest

from src.web.login_redirect import (
    DEFAULT_ONBOARD_NEXT,
    login_redirect_location,
    login_url_with_next,
    resolve_post_login_dest,
    safe_next_path,
)


@pytest.mark.parametrize(
    "raw,ok",
    [
        ("/workspace/dash", True),
        ("/workspace/golive?tab=ai", True),
        ("/%2Fworkspace%2Fdash", False),  # 解码后变 //workspace… 拒
        ("https://evil.com", False),
        ("//evil.com", False),
        ("/\\evil", False),
        ("workspace/dash", False),
        ("", False),
        (None, False),
        ("/" + "a" * 600, False),
        ("/login", True),
    ],
)
def test_safe_next_path_matrix(raw, ok):
    got = safe_next_path(raw)
    assert bool(got) is ok
    if ok and raw and not str(raw).startswith("%"):
        assert got.startswith("/")


def test_resolve_post_login_prefers_next():
    assert resolve_post_login_dest(
        next_raw="/workspace/golive", role_default="/") == "/workspace/golive"
    assert resolve_post_login_dest(
        next_raw="https://evil", role_default="/workspace/dash") == "/workspace/dash"
    assert resolve_post_login_dest(
        next_raw="/login", role_default="/workspace/dash") == "/workspace/dash"


def test_login_url_with_next_and_middleware_location():
    assert login_url_with_next("https://bd2026.cc:18987") == (
        "https://bd2026.cc:18987/login?next=%2Fworkspace%2Fdash")
    assert login_url_with_next("https://x/login", "/workspace/golive").endswith(
        "next=%2Fworkspace%2Fgolive")
    assert login_redirect_location("/workspace/dash") == (
        "/login?next=%2Fworkspace%2Fdash")
    assert login_redirect_location("/login") == "/login"
    assert DEFAULT_ONBOARD_NEXT == "/workspace/dash"


def test_tenant_card_login_deep_link():
    from src.ops import tenant_lifecycle as tl

    plan = {"instance_id": "zhiliao_acme", "web_port": 18999}
    card = tl.build_tenant_card(plan, "tok", owner_user="owner", owner_password="pw")
    assert card["login_url"] == "http://127.0.0.1:18999/login?next=/workspace/dash"
    pub = tl.apply_public_base("https://bd2026.cc:18987")
    assert pub["login_url"] == "https://bd2026.cc:18987/login?next=/workspace/dash"
    assert pub["workspace_url"].endswith("/workspace/dash")


def test_delivery_code_uses_login_deep_link():
    from src.ops import tenant_fulfillment as tf

    code = tf.build_delivery_code("https://acme.bd2026.cc", "cust-pw",
                                  "zhiliao_acme", username="owner")
    assert "login?next=/workspace/dash" in code
    assert "owner" in code and "cust-pw" in code
    assert "上线自检" in code
    assert "OPS-TOKEN" not in code
