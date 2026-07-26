"""channel_setup 渠道向导 × platform_login 桥接门禁（enable_on_ready）。

背景（2026-07 实机事故）：桌面版在后台向导填齐 Telegram api_id/api_hash 后，
接入弹窗的「协议多开」仍然灰着——向导只写 telegram.* 凭据段，而扫码登录
provider 还要 platform_login.telegram.protocol_enabled（扫上的号被拉上线还要
platform_login.orchestrator_enabled），这两个键此前没有任何产品入口会写。
修复＝Channel.enable_on_ready 声明式桥接：必填凭据齐全时顺带补 true。
"""
from __future__ import annotations

from src.utils.channel_setup import apply_channel_values, get_channel


def _pl(overlay):
    return (overlay.get("platform_login") or {})


# ── 既有行为钉子（凭据写入 + 渠道 enabled） ──────────────────────────


def test_apply_writes_fields_and_enables_channel():
    overlay = {}
    ok, _ = apply_channel_values(
        overlay, "telegram", {"api_id": "12345", "api_hash": "abcdef"})
    assert ok is True
    assert overlay["telegram"]["api_id"] == 12345
    assert overlay["telegram"]["api_hash"] == "abcdef"
    assert overlay["telegram"]["enabled"] is True


def test_apply_rejects_unknown_channel():
    ok, _ = apply_channel_values({}, "nope", {"x": 1})
    assert ok is False


# ── enable_on_ready 桥接 ─────────────────────────────────────────────


def test_telegram_declares_platform_login_bridge():
    ch = get_channel("telegram")
    assert "platform_login.telegram.protocol_enabled" in ch.enable_on_ready
    assert "platform_login.orchestrator_enabled" in ch.enable_on_ready


def test_bridge_opens_switches_when_required_ready():
    overlay = {}
    ok, _ = apply_channel_values(
        overlay, "telegram", {"api_id": "12345", "api_hash": "abcdef"})
    assert ok is True
    assert _pl(overlay)["telegram"]["protocol_enabled"] is True
    assert _pl(overlay)["orchestrator_enabled"] is True


def test_bridge_skipped_when_required_missing():
    """只填 api_id 没填 api_hash → 凭据不齐，不开闸（开了也注册不了 provider）。"""
    overlay = {}
    ok, _ = apply_channel_values(overlay, "telegram", {"api_id": "12345"})
    assert ok is True
    assert "platform_login" not in overlay


def test_bridge_skipped_on_placeholder_values():
    """占位值（your_*）不算就绪——与字段写入同一占位判定口径。"""
    overlay = {}
    ok, _ = apply_channel_values(
        overlay, "telegram", {"api_id": "123", "api_hash": "YOUR_API_HASH"})
    assert ok is True
    assert "platform_login" not in overlay


def test_bridge_merges_base_config_for_readiness():
    """主 config 已有 api_id、本次向导只补 api_hash → 合并视图判定就绪。"""
    overlay = {}
    ok, _ = apply_channel_values(
        overlay, "telegram", {"api_hash": "abcdef"},
        base_config={"telegram": {"api_id": 999}})
    assert ok is True
    assert _pl(overlay)["telegram"]["protocol_enabled"] is True
    assert _pl(overlay)["orchestrator_enabled"] is True


def test_bridge_respects_explicit_false_in_base_config():
    """管理员在运行 config 里显式关过的开关不被向导扳回；未设置的键照常补。"""
    overlay = {}
    ok, _ = apply_channel_values(
        overlay, "telegram", {"api_id": "1", "api_hash": "abcdef"},
        base_config={"platform_login": {"telegram": {"protocol_enabled": False}}})
    assert ok is True
    assert "protocol_enabled" not in (_pl(overlay).get("telegram") or {})
    assert _pl(overlay)["orchestrator_enabled"] is True


def test_bridge_respects_explicit_false_in_overlay():
    overlay = {"platform_login": {"orchestrator_enabled": False}}
    ok, _ = apply_channel_values(
        overlay, "telegram", {"api_id": "1", "api_hash": "abcdef"})
    assert ok is True
    assert _pl(overlay)["orchestrator_enabled"] is False
    assert _pl(overlay)["telegram"]["protocol_enabled"] is True


def test_bridge_ignores_optional_fields():
    """phone_number 为可选字段，缺席不阻塞就绪判定。"""
    overlay = {}
    apply_channel_values(
        overlay, "telegram", {"api_id": "1", "api_hash": "abcdef"})
    assert _pl(overlay)["telegram"]["protocol_enabled"] is True


def test_bridge_only_on_declaring_channels():
    """line 等未声明 enable_on_ready 的渠道不产生 platform_login 写入。"""
    overlay = {}
    ok, _ = apply_channel_values(overlay, "line", {
        "channel_access_token": "tok123", "channel_secret": "sec456"})
    assert ok is True
    assert "platform_login" not in overlay
