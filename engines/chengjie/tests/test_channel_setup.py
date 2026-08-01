"""channel_setup 渠道向导 × platform_login 桥接门禁（enable_on_ready）。

背景（2026-07 实机事故）：桌面版在后台向导填齐 Telegram api_id/api_hash 后，
接入弹窗的「协议多开」仍然灰着——向导只写 telegram.* 凭据段，而扫码登录
provider 还要 platform_login.telegram.protocol_enabled（扫上的号被拉上线还要
platform_login.orchestrator_enabled），这两个键此前没有任何产品入口会写。
修复＝Channel.enable_on_ready 声明式桥接：必填凭据齐全时顺带补 true。
"""
from __future__ import annotations

from src.utils.channel_setup import apply_channel_values, channel_status, get_channel


def _tg(config):
    return next(c for c in channel_status(config) if c["id"] == "telegram")


# ── 托管派发：凭据由官网自动配置时隐藏 api_id/hash 黑话（2026-07-29）──────


def test_hosted_cred_hides_api_fields_and_shows_plain_intro():
    cfg = {"telegram": {"api_id": 12345, "api_hash": "a" * 32,
                        "_hosted_cred": True, "enabled": True}}
    tg = _tg(cfg)
    keys = [f["key"] for f in tg["fields"]]
    assert "telegram.api_id" not in keys and "telegram.api_hash" not in keys
    assert tg["auto_provisioned"] is True
    assert tg["configured"] is True
    # 凭据齐 ≠ 能收发消息：Telegram 还得真登录一个号。旧口径在这里报 ready=True，
    # 是「已就绪 N/4」失真的另一半（另一半是登录进来的号根本不写 yaml 键）。
    assert tg["ready"] is False
    # 人话 intro，不含 yaml/API 黑话
    assert "API ID" not in tg["intro"] or "无需" in tg["intro"]
    assert "登录" in tg["intro"]


def test_without_hosted_cred_manual_fields_stay():
    """没真拿到托管凭据 → 必须保留手填路径（否则砸掉唯一出路）。"""
    tg = _tg({"telegram": {"api_id": "", "api_hash": ""}})
    keys = [f["key"] for f in tg["fields"]]
    assert "telegram.api_id" in keys and "telegram.api_hash" in keys
    assert tg.get("auto_provisioned") is False
    assert tg["configured"] is False


def test_phone_field_survives_hosted_cred():
    """手机号是登录要用的，不该被一起藏掉。"""
    tg = _tg({"telegram": {"api_id": 1, "api_hash": "b" * 32, "_hosted_cred": True}})
    assert "telegram.phone_number" in [f["key"] for f in tg["fields"]]


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


# ── 「能不能收发消息」口径（2026-07-31）────────────────────────────────
#
# 旧口径 = configured and enabled，两头都不准：正在收发消息的 Telegram 被报成
# 「已就绪 0/4」（登录进来的账号不写 yaml 键），而只填了凭据没登录号的反被算就绪。


def _by_id(config, cid, **kw):
    return next(c for c in channel_status(config, **kw) if c["id"] == cid)


def test_linked_account_makes_channel_ready_without_any_yaml():
    """实机反馈的那一幕：号在跑、消息在收，向导却说 0/4。"""
    tg = _by_id({}, "telegram", accounts_by_platform={"telegram": 2})
    assert tg["linked_accounts"] == 2
    assert tg["ready"] is True and tg["ready_by"] == "login"


def test_official_api_credentials_alone_make_line_ready():
    """LINE 官方账号是「填完就能收 webhook」的形态 → 无需登录也算就绪。"""
    cfg = {"line": {"enabled": True, "channel_access_token": "t",
                    "channel_secret": "s"}}
    line = _by_id(cfg, "line")
    assert line["ready"] is True and line["ready_by"] == "api"


def test_telegram_credentials_alone_are_not_transport():
    """Telegram 的 api_id/hash 只是登录前置，本身不通消息。"""
    cfg = {"telegram": {"enabled": True, "api_id": 1, "api_hash": "h" * 32}}
    tg = _by_id(cfg, "telegram")
    assert tg["paths"]["api"]["is_transport"] is False
    assert tg["ready"] is False


def test_web_widget_ready_when_enabled():
    assert _by_id({"web_chat": {"enabled": True}}, "web")["ready"] is True
    assert _by_id({}, "web")["ready"] is False


# ── 两条接入路径 ──────────────────────────────────────────────────────


def test_every_account_channel_exposes_a_login_path():
    """四个平台都得有「用自己的账号登录」这条路——它才是多数客户真正走的那条。"""
    for cid in ("telegram", "line", "whatsapp", "messenger"):
        ch = _by_id({}, cid)
        p = ch["paths"]["login"]
        assert p is not None, cid
        assert p["platform"] == cid
        assert p["deeplink"] == f"/workspace?drawer=1&connect={cid}"


def test_whatsapp_is_present_and_scan_only():
    """WhatsApp 此前整个缺席，客户据此以为不支持；且它只有扫码一条真实路径。"""
    wa = _by_id({}, "whatsapp")
    assert wa["paths"]["login"] is not None
    assert wa["paths"]["api"] is None       # 不摆一个填了也不通的半成品表单
    assert wa["fields"] == []


def test_login_path_unavailable_is_reported():
    wa = _by_id({}, "whatsapp", login_ready={"whatsapp": False})
    assert wa["paths"]["login"]["available"] is False


def test_login_path_defaults_to_available_without_signal():
    """拿不到诊断信号时别凭空画灰——让用户点进去看真实原因。"""
    assert _by_id({}, "line")["paths"]["login"]["available"] is True
