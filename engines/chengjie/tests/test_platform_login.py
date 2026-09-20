"""M1：多平台登录方式（mode）+ 账号注册表 单测。"""

from __future__ import annotations

import os
import tempfile

from src.integrations import platform_login as pl
from src.integrations.account_registry import AccountRegistry


def test_list_modes_defaults():
    tg = {m["mode"]: m for m in pl.list_modes("telegram")}
    # 2026-08-11：web 占位从默认清单摘除——protocol 本就是官方关联设备扫码
    # （ExportLoginToken，二维码在本窗口显示），再挂一张永不可用的「网页扫码」
    # 重复卡只会引人点进死胡同（桌面种子早已同款摘除）。
    assert set(tg) == {"protocol", "device"}
    assert tg["device"]["available"] is True          # device 内置可用
    assert tg["protocol"]["recommended"] is True       # TG 默认 protocol
    # 未注册 provider 的 protocol 默认不可用
    assert tg["protocol"]["available"] is False

    # 仍留在清单里的未实现占位（whatsapp/web）必须报 not_implemented 而非
    # not_enabled——「没做」与「开关没开」处置完全不同，静态回落层也要分清。
    wa = {m["mode"]: m for m in pl.list_modes("whatsapp")}
    assert wa["web"]["available"] is False
    assert wa["web"]["reason_code"] == pl.REASON_NOT_IMPLEMENTED

    line = {m["mode"]: m for m in pl.list_modes("line")}
    # M7：LINE 新增 protocol(okline) 方式，默认推荐 protocol；device 仍内置可用；
    # P1（2026-08-05）：official = Messaging API 合规第二路（凭证经接入向导，
    # login_kind=credentials），不改默认方式
    assert set(line) == {"protocol", "device", "official"}
    assert line["protocol"]["recommended"] is True
    assert line["device"]["available"] is True
    # 未注册 provider 时 protocol 默认不可用（灰显「未启用」）
    assert line["protocol"]["available"] is False
    assert line["official"]["login_kind"] == "credentials"
    assert line["official"]["recommended"] is False


def test_list_modes_config_override():
    cfg = {"modes": ["device"], "default": "device"}
    modes = pl.list_modes("telegram", cfg)
    assert [m["mode"] for m in modes] == ["device"]
    assert modes[0]["recommended"] is True


def test_login_kind_mapping():
    """登录形态单一事实源：Messenger web=hosted（服务器托管），其余按 mode 默认。

    形态决定前端向导整套词汇（扫码/托管/设备端）——映射漂移会让 Messenger 用户
    重新对着「等待扫码」干等一个不存在的二维码（2026-08 修复的事故）。
    """
    assert pl.login_kind("messenger", "web") == "hosted"
    assert pl.login_kind("messenger", "device") == "device"
    assert pl.login_kind("telegram", "protocol") == "qr"
    assert pl.login_kind("telegram", "web") == "qr"
    assert pl.login_kind("whatsapp", "protocol") == "qr"
    assert pl.login_kind("line", "protocol") == "qr"
    assert pl.login_kind("line", "device") == "device"
    # 未知输入保守回落 qr（词汇最保守的一套）
    assert pl.login_kind("nope", "weird") == "qr"
    # list_modes 必须带 login_kind 字段（前端按它切换词汇）
    for m in pl.list_modes("messenger"):
        assert m["login_kind"] == pl.login_kind("messenger", m["mode"])


def test_messenger_mode_descriptions_have_no_qr_wording():
    """Messenger 两种方式的描述都不得出现「扫码」叙事（FB 网页端与 App 都是账密登录）。"""
    modes = {m["mode"]: m for m in pl.list_modes("messenger")}
    assert "扫码" not in modes["web"]["desc"]
    assert "扫码" not in modes["device"]["desc"]
    # 专属 desc_key 已登记（i18n 双语由 pack 门禁守护）
    assert modes["device"]["desc_key"] == "inbox.connect.mode_d_msg_device"


def test_register_provider_makes_mode_available():
    assert pl.mode_available("whatsapp", "protocol") is False
    pl.register_login_provider("whatsapp", "protocol", lambda *a, **k: {"qr_url": "x"})
    try:
        assert pl.mode_available("whatsapp", "protocol") is True
        modes = {m["mode"]: m for m in pl.list_modes("whatsapp")}
        assert modes["protocol"]["available"] is True
    finally:
        pl._PROVIDERS.pop(pl._pkey("whatsapp", "protocol"), None)


def test_online_account_keys():
    status_map = {
        "wa_a": {"platform": "whatsapp", "account_id": "a", "running": True},
        "wa_b": {"platform": "whatsapp", "account_id": "b", "running": False},
        "tg": {"platform": "telegram", "account_id": "t", "running": True},
    }
    assert pl.online_account_keys(status_map, "whatsapp") == {"a"}
    assert pl.online_account_keys(status_map, "telegram") == {"t"}
    assert pl.online_account_keys(status_map, "line") == set()


def test_login_manager_lifecycle():
    mgr = pl.LoginManager()
    s = mgr.create("telegram", "", baseline={"old"}, mode="protocol")
    assert s.status == "pending"
    assert s.mode == "protocol"
    assert mgr.get(s.login_id) is s
    mgr.cancel(s.login_id)
    assert mgr.get(s.login_id) is None


def test_login_session_expiry(monkeypatch):
    s = pl.LoginSession(login_id="x", platform="telegram")
    assert s.is_expired() is False
    s.created_at -= (pl.TTL_SEC + 5)
    assert s.is_expired() is True


def test_hosted_session_gets_longer_ttl():
    """hosted 形态（账密+2FA 人工登录）自动放宽 TTL。

    180s 会在运维输到一半时判过期，前端旧行为随即自动重开会话 → 把服务器上
    正在操作的登录窗口直接关掉。qr/device 会话保持原 TTL 不变。
    """
    mgr = pl.LoginManager()
    hosted = mgr.create("messenger", "", baseline=set(), mode="web")
    assert hosted.ttl_sec == pl.HOSTED_TTL_SEC
    hosted.created_at -= (pl.TTL_SEC + 5)      # 超过旧 TTL 但在 hosted TTL 内
    assert hosted.is_expired() is False
    hosted.created_at -= pl.HOSTED_TTL_SEC     # 超过 hosted TTL
    assert hosted.is_expired() is True

    qr = mgr.create("telegram", "", baseline=set(), mode="protocol")
    assert qr.ttl_sec == 0                     # 0 = 按 TTL_SEC 默认
    qr.created_at -= (pl.TTL_SEC + 5)
    assert qr.is_expired() is True


def test_default_instruction_carries_i18n_key():
    """指引取平台默认表时必须带 i18n 键（英文坐席不再看中文）；provider 自带指引不硬配键。"""
    mgr = pl.LoginManager()
    s = mgr.create("whatsapp", "", baseline=set(), mode="device")
    assert s.instruction == pl.PLATFORM_INSTRUCTIONS["whatsapp"]
    assert s.instruction_key == pl.PLATFORM_INSTRUCTION_KEYS["whatsapp"]
    # provider 自带 instruction（无键）→ 键保持为空，防止前端按键显示错话
    s2 = mgr.create("telegram", "", baseline=set(), mode="protocol",
                    instruction="provider 自己的指引")
    assert s2.instruction == "provider 自己的指引"
    assert s2.instruction_key == ""


def _fresh_registry() -> AccountRegistry:
    p = os.path.join(tempfile.mkdtemp(), "acct.db")
    return AccountRegistry(p)


def test_registry_upsert_merge_keeps_unspecified_fields():
    r = _fresh_registry()
    r.upsert("telegram", "123", mode="protocol", label="A", status="pending")
    r.upsert("telegram", "123", status="online")  # 只改状态
    g = r.get("telegram", "123")
    assert g["mode"] == "protocol"
    assert g["label"] == "A"
    assert g["status"] == "online"
    assert g["last_online_at"] > 0


def test_registry_list_and_remove():
    r = _fresh_registry()
    r.upsert("telegram", "1", mode="protocol")
    r.upsert("whatsapp", "2", mode="protocol")
    assert len(r.list()) == 2
    assert len(r.list("telegram")) == 1
    r.remove("telegram", "1")
    assert len(r.list("telegram")) == 0
    assert len(r.list("telegram", include_removed=True)) == 1


def test_registry_set_status_invalid_ignored():
    r = _fresh_registry()
    r.upsert("telegram", "1", status="pending")
    r.set_status("telegram", "1", "bogus")
    assert r.get("telegram", "1")["status"] == "pending"
