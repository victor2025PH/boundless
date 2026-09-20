"""Telegram 手机号+验证码登录（引擎纯函数 + 状态机 DI + 注册闸）。"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import pytest

from src.integrations import telegram_phone_login as tpl
from src.integrations.login_funnel_stats import _REASON_CODES


def _ensure_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class PhoneNumberInvalid(Exception):
    pass


class PhoneNumberBanned(Exception):
    pass


class PhoneNumberUnoccupied(Exception):
    pass


class PhoneCodeInvalid(Exception):
    pass


class PhoneCodeExpired(Exception):
    pass


class SessionPasswordNeeded(Exception):
    pass


@pytest.mark.parametrize("raw,expect", [
    ("", ""),
    ("13800138000", "+8613800138000"),
    ("+86 138-0013-8000", "+8613800138000"),
    ("008613800138000", "+8613800138000"),
    ("14712345678", "+8614712345678"),
    ("19912345678", "+8619912345678"),
    # NANP 带国家码也是 11 位且以 1 开头——绝不能误补 +86
    ("14155552671", "+14155552671"),
    ("+1 650 555 0100", "+16505550100"),
    ("6505550100", "+6505550100"),
])
def test_normalize_phone(raw, expect):
    assert tpl.normalize_phone(raw) == expect


@pytest.mark.parametrize("ex,expect", [
    (PhoneNumberInvalid("bad"), "phone_invalid"),
    (Exception("PHONE_NUMBER_INVALID"), "phone_invalid"),
    (PhoneNumberBanned("banned"), "phone_banned"),
    (PhoneNumberUnoccupied("none"), "phone_unoccupied"),
    (PhoneCodeInvalid("wrong"), "code_invalid"),
    (PhoneCodeExpired("old"), "code_expired"),
    (TimeoutError(), "tg_unreachable"),
])
def test_classify_phone_login_exception(ex, expect):
    assert tpl.classify_phone_login_exception(ex) == expect


def test_phone_reason_codes_inside_funnel_enum():
    for code in ("phone_invalid", "phone_banned", "phone_unoccupied",
                 "code_invalid", "code_expired"):
        assert code in _REASON_CODES


class _FakeSent:
    def __init__(self, h="hash1"):
        self.phone_code_hash = h


class _FakeUser:
    def __init__(self, uid=4242, phone="+8613800138000"):
        self.id = uid
        self.phone_number = phone
        self.first_name = "T"
        self.last_name = ""
        self.username = ""
        self.is_bot = False
        self.is_premium = False
        self.is_verified = False
        self.is_scam = False
        self.is_fake = False


class _FakeStorage:
    async def user_id(self, *_a, **_k):
        return None

    async def is_bot(self, *_a, **_k):
        return None


class _FakeClient:
    def __init__(self, *, send_ex=None, sign_ex=None, pwd_ex=None, user=None):
        self.send_ex = send_ex
        self.sign_ex = sign_ex
        self.pwd_ex = pwd_ex
        self.user = user or _FakeUser()
        self.storage = _FakeStorage()
        self.connected = False
        self.send_calls = 0

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def send_code(self, phone):
        self.send_calls += 1
        if self.send_ex:
            raise self.send_ex
        return _FakeSent(f"h{self.send_calls}")

    async def sign_in(self, phone, phone_code_hash, code):
        if self.sign_ex:
            raise self.sign_ex
        return self.user

    async def check_password(self, password):
        if self.pwd_ex:
            raise self.pwd_ex
        return self.user

    async def export_session_string(self):
        return "sess-string"


def _factory(client: _FakeClient):
    def _make(*_a, **_k):
        return client
    return _make


@pytest.mark.asyncio
async def test_phone_login_send_code_success():
    _ensure_loop()
    c = _FakeClient()
    login = tpl.TelegramPhoneLogin(1, "h", "sessions", client_factory=_factory(c))
    res = await login.start("+8613800138000")
    assert res["status"] == "code_needed"
    assert login.phone_code_hash == "h1"


@pytest.mark.asyncio
async def test_phone_login_send_code_invalid_number():
    _ensure_loop()
    c = _FakeClient(send_ex=PhoneNumberInvalid("x"))
    login = tpl.TelegramPhoneLogin(1, "h", "sessions", client_factory=_factory(c))
    res = await login.start("bad")
    assert res["status"] == "failed"
    assert res["reason_code"] == "phone_invalid"


@pytest.mark.asyncio
async def test_phone_login_sign_in_to_authorized():
    _ensure_loop()
    c = _FakeClient()
    login = tpl.TelegramPhoneLogin(1, "h", "sessions", client_factory=_factory(c))
    await login.start("+8613800138000")
    res = await login.submit_code("12345")
    assert res["status"] == "authorized"
    assert res["account_id"] == "4242"
    assert login.session_string == "sess-string"


@pytest.mark.asyncio
async def test_phone_login_bad_code_stays_code_needed():
    _ensure_loop()
    c = _FakeClient(sign_ex=PhoneCodeInvalid("x"))
    login = tpl.TelegramPhoneLogin(1, "h", "sessions", client_factory=_factory(c))
    await login.start("+8613800138000")
    res = await login.submit_code("00000")
    assert res["status"] == "code_needed"
    assert "验证码" in (login.detail or "")


@pytest.mark.asyncio
async def test_phone_login_password_needed_then_ok():
    _ensure_loop()
    c = _FakeClient(sign_ex=SessionPasswordNeeded("2fa"))
    login = tpl.TelegramPhoneLogin(1, "h", "sessions", client_factory=_factory(c))
    await login.start("+8613800138000")
    res = await login.submit_code("12345")
    assert res["status"] == "password_needed"
    c.sign_ex = None
    c.pwd_ex = None
    res2 = await login.submit_password("secret")
    assert res2["status"] == "authorized"


def test_maybe_register_gated_by_phone_enabled(monkeypatch):
    called = {"n": 0}

    def _reg(*_a, **_k):
        called["n"] += 1

    monkeypatch.setattr(tpl, "_registered", False)
    monkeypatch.setattr(
        "src.integrations.platform_login.register_login_provider", _reg)
    monkeypatch.setattr(
        "src.integrations.telegram_protocol_login.is_pyrogram_available",
        lambda: True)
    monkeypatch.setattr(
        "src.integrations.telegram_protocol_login.resolve_credentials",
        lambda _c: (1, "h"))
    assert tpl.maybe_register({"platform_login": {"telegram": {"phone_enabled": False}}}) is False
    assert called["n"] == 0
    assert tpl.maybe_register({"platform_login": {"telegram": {"phone_enabled": True}}}) is True
    assert called["n"] == 1
    # 幂等
    assert tpl.maybe_register({"platform_login": {"telegram": {"phone_enabled": True}}}) is True
    assert called["n"] == 1


def test_phone_enabled_desktop_default_on(monkeypatch, tmp_path):
    """桌面升级路径：缺省键时 resolve_login_switch 对 phone_enabled 默认开。"""
    from src.integrations.platform_login import resolve_login_switch
    # 空配置 + 桌面模式标记 → 读 _DESKTOP_LOGIN_DEFAULT_ON
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    assert resolve_login_switch({}, "platform_login.telegram.phone_enabled") is True
