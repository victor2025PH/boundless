"""M2：Telegram protocol（pyrogram）扫码登录 provider 单测。

仅覆盖纯函数 / 门控 / 状态机的 pending 分支（不联网、不需真账号）。
真实扫码成功 + DC 迁移路径需用测试号联调（protocol_enabled 默认 false）。
"""

from __future__ import annotations

import asyncio
import base64
import time

import pytest

from src.integrations import platform_login as pl
from src.integrations import telegram_protocol_login as tpl


def test_tg_login_url_format():
    url = tpl.tg_login_url(b"abc")
    assert url == "tg://login?token=" + base64.urlsafe_b64encode(b"abc").decode().rstrip("=")
    assert url.startswith("tg://login?token=")


def test_resolve_credentials_flat_and_accounts():
    assert tpl.resolve_credentials({}) is None
    assert tpl.resolve_credentials({"telegram": {"api_id": 0, "api_hash": ""}}) is None
    flat = tpl.resolve_credentials({"telegram": {"api_id": 123, "api_hash": "h"}})
    assert flat == (123, "h")
    nested = tpl.resolve_credentials(
        {"telegram": {"accounts": [{"api_id": 9, "api_hash": "z"}]}})
    assert nested == (9, "z")


def test_protocol_enabled_flag():
    assert tpl.protocol_enabled({}) is False
    assert tpl.protocol_enabled(
        {"platform_login": {"telegram": {"protocol_enabled": True}}}) is True


def test_maybe_register_gated_off_by_default():
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    # 有凭据但未开 protocol_enabled → 不注册
    cfg = {"telegram": {"api_id": 1, "api_hash": "h"}}
    assert tpl.maybe_register(cfg) is False
    assert pl.mode_available("telegram", "protocol") is False


def test_maybe_register_when_enabled():
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    cfg = {
        "telegram": {"api_id": 1, "api_hash": "h"},
        "platform_login": {"telegram": {"protocol_enabled": True}},
    }
    try:
        assert tpl.maybe_register(cfg) is True
        assert pl.mode_available("telegram", "protocol") is True
        # 幂等
        assert tpl.maybe_register(cfg) is True
    finally:
        tpl._registered = False
        pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)


def test_state_machine_pending_branch(tmp_path):
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    # pyrogram 顶层 import 会触发 sync 模块调用 get_event_loop()，
    # 在 xdist worker 线程里无 loop 会抛 RuntimeError —— 先确保有 loop。
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    from pyrogram.raw.types.auth import LoginToken
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    tok = LoginToken(expires=int(time.time()) + 30, token=b"abc")
    asyncio.run(login._advance(tok))
    assert login.status == "pending"
    assert login.qr_url.startswith("tg://login?token=")
    assert login.result()["status"] == "pending"


# ── 两步验证（2FA）：纯判定 + submit_password 状态机（实施29+，无需联网/真号）──

# 类名刻意与 pyrogram.errors 真类同名（检测逻辑按 type(ex).__name__ 判定，故此处不加下划线）
class SessionPasswordNeeded(Exception):
    """模拟 pyrogram.errors.SessionPasswordNeeded（按类名判定，不依赖真类路径）。"""


class PasswordHashInvalid(Exception):
    """模拟 pyrogram.errors.PasswordHashInvalid（密码错误可重试）。"""


def test_is_password_needed_by_name_and_text():
    assert tpl._is_password_needed(SessionPasswordNeeded()) is True
    assert tpl._is_password_needed(Exception("SESSION_PASSWORD_NEEDED")) is True
    assert tpl._is_password_needed(Exception("something else")) is False


def test_is_bad_password_detection():
    assert tpl._is_bad_password(PasswordHashInvalid()) is True
    assert tpl._is_bad_password(Exception("PASSWORD_HASH_INVALID")) is True
    assert tpl._is_bad_password(Exception("network")) is False


class _FakeStorage:
    async def user_id(self, *_a):
        return None

    async def is_bot(self, *_a):
        return None


class _FakeUser:
    id = 8127518232
    phone_number = "60111471"
    first_name = "Boss"
    last_name = ""
    username = "bossacct"


class _FakeClient:
    """最小可跑的 pyrogram Client 替身：只实现 submit_password 收尾用到的方法。"""

    def __init__(self, *, password_ok=True):
        self._password_ok = password_ok
        self.storage = _FakeStorage()
        self.disconnected = False

    async def check_password(self, password):
        if not self._password_ok:
            raise PasswordHashInvalid("PASSWORD_HASH_INVALID")
        return _FakeUser()

    async def export_session_string(self):
        return "FAKE_SESSION_STRING"

    async def disconnect(self):
        self.disconnected = True


def _mk_login(tmp_path, *, client):
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "password_needed"
    login.client = client
    return login


def test_submit_password_success(tmp_path):
    login = _mk_login(tmp_path, client=_FakeClient(password_ok=True))
    res = asyncio.run(login.submit_password("cloud-pw"))
    assert res["status"] == "authorized"
    assert login.account_id == "8127518232"
    assert login.session_string == "FAKE_SESSION_STRING"
    assert login.client.disconnected is True


def test_submit_password_wrong_keeps_retryable(tmp_path):
    login = _mk_login(tmp_path, client=_FakeClient(password_ok=False))
    res = asyncio.run(login.submit_password("wrong"))
    # 密码错误：停在 password_needed 可重试，不落 authorized/failed
    assert res["status"] == "password_needed"
    assert "错误" in login.detail


def test_submit_password_ignored_when_not_waiting(tmp_path):
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "pending"
    res = asyncio.run(login.submit_password("x"))
    assert res["status"] == "pending"  # 非 password_needed 态直接返回，不动作


def test_poll_short_circuits_on_password_needed(tmp_path):
    login = tpl.TelegramQrLogin(1, "h", str(tmp_path))
    login.status = "password_needed"
    login.client = object()  # 若真去 invoke 会 AttributeError → 证明没走网络分支
    res = asyncio.run(login.poll())
    assert res["status"] == "password_needed"
