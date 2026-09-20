"""cred_invalid 处置元信息契约（2026-08-13，「香港 IP 批量登录失败」池事故 UX 闭环）。

事故形态：托管池给直连机器（tg_direct，如香港 IP 客群）粘定的组废掉后，旧 UI 只能
甩一段 90 字红文让**用户自己判断**「凭据是自动配置的还是自己填的」并「等约 2 分钟」
——而系统明知答案（alloc.source + telegram._hosted_cred）也明知冷却剩余。本文件钉住
新契约：失败响应必须携带 ``cred_source``（hosted|pool|self）+ ``retry_after_sec``
（换发冷却剩余秒），前端据此倒计时自愈 / 亮修正表单。

三层：纯函数（cred_fail_meta / swap_retry_after_sec）→ 状态机 result() 携带 →
provider 返回值契约（QR 与 phone 两条链同口径）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.ai import hosted_gateway as hg
from src.integrations import credpool_bridge as cpb
from src.integrations import telegram_phone_login as tpl_phone
from src.integrations import telegram_protocol_login as tpl
from src.integrations.platform_login import LoginManager


# ── 纯函数：swap_retry_after_sec ─────────────────────────────────────────────

def test_swap_retry_after_zero_when_never_swapped(monkeypatch):
    monkeypatch.setattr(hg, "_last_swap_ts", 0.0)
    assert hg.swap_retry_after_sec() == 0


def test_swap_retry_after_counts_down_from_recent_swap(monkeypatch):
    monkeypatch.setattr(hg, "_last_swap_ts", time.time())
    left = hg.swap_retry_after_sec()
    # 刚换发过 → 剩余应接近整个冷却窗（向上取整，不会是 0）
    assert 0 < left <= int(hg.SWAP_COOLDOWN_SEC) + 1
    assert left >= int(hg.SWAP_COOLDOWN_SEC) - 2


def test_swap_retry_after_expired_cooldown_is_zero(monkeypatch):
    monkeypatch.setattr(hg, "_last_swap_ts", time.time() - hg.SWAP_COOLDOWN_SEC - 5)
    assert hg.swap_retry_after_sec() == 0


# ── 纯函数：cred_fail_meta 三分支 ────────────────────────────────────────────

def _alloc(source: str) -> cpb.Allocation:
    return cpb.Allocation(12345, "a" * 32, source, "free", None)


def test_meta_pool_source(monkeypatch):
    meta = tpl.cred_fail_meta({}, _alloc("credpool"))
    assert meta == {"cred_source": "pool", "retry_after_sec": 0}


def test_meta_hosted_source_carries_cooldown(monkeypatch):
    monkeypatch.setattr(hg, "swap_retry_after_sec", lambda: 42)
    cfg = {"telegram": {"_hosted_cred": True}}
    meta = tpl.cred_fail_meta(cfg, _alloc("config"))
    assert meta == {"cred_source": "hosted", "retry_after_sec": 42}


def test_meta_self_filled(monkeypatch):
    cfg = {"telegram": {"api_id": "111", "api_hash": "x"}}
    meta = tpl.cred_fail_meta(cfg, _alloc("config"))
    assert meta == {"cred_source": "self", "retry_after_sec": -1}


def test_meta_hosted_helper_failure_degrades_to_zero(monkeypatch):
    def _boom():
        raise RuntimeError("no gateway")
    monkeypatch.setattr(hg, "swap_retry_after_sec", _boom)
    cfg = {"telegram": {"_hosted_cred": True}}
    meta = tpl.cred_fail_meta(cfg, _alloc("config"))
    assert meta == {"cred_source": "hosted", "retry_after_sec": 0}


# ── 状态机 result()：只在设置后携带（不污染正常/其它失败响应）────────────────

def test_qr_result_omits_meta_by_default():
    _ensure_loop()
    login = tpl.TelegramQrLogin(1, "h", "sessions")
    out = login.result()
    assert "cred_source" not in out and "retry_after_sec" not in out


def test_qr_result_carries_meta_when_set():
    _ensure_loop()
    login = tpl.TelegramQrLogin(1, "h", "sessions")
    login.status = "failed"
    login.reason_code = "cred_invalid"
    login.cred_source = "hosted"
    login.retry_after_sec = 77
    out = login.result()
    assert out["cred_source"] == "hosted" and out["retry_after_sec"] == 77


def test_phone_result_carries_meta_when_set():
    login = tpl_phone.TelegramPhoneLogin(1, "h", "sessions")
    out0 = login.result()
    assert "cred_source" not in out0
    login.cred_source = "self"
    login.retry_after_sec = -1
    out = login.result()
    assert out["cred_source"] == "self" and out["retry_after_sec"] == -1


# ── LoginSession 透传：create 落字段（start 路由据此回给前端）────────────────

def test_login_session_create_passthrough():
    mgr = LoginManager()
    sess = mgr.create(
        "telegram", "", set(), mode="protocol",
        initial_status="failed", reason_code="cred_invalid",
        detail="[400 API_ID_INVALID]", cred_source="hosted", retry_after_sec=99)
    assert sess.status == "failed"
    assert sess.cred_source == "hosted" and sess.retry_after_sec == 99


def test_login_session_defaults_empty():
    mgr = LoginManager()
    sess = mgr.create("telegram", "", set(), mode="protocol")
    assert sess.cred_source == "" and sess.retry_after_sec == -1


# ── provider 契约：开局 cred_invalid → 返回值带终态 + 元信息（QR/phone 同口径）──

def _ensure_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class _FakeFailedLogin:
    """替身状态机：start 即 cred_invalid（模拟池组废）。"""

    def __init__(self, *a, **k):
        self.status = "pending"
        self.reason_code = ""
        self.detail = ""
        self.qr_url = ""
        self.cred_source = ""
        self.retry_after_sec = -1
        self.session_name = "fake"
        self.phone = ""

    def result(self):
        out = {"status": self.status, "account_id": "", "qr_url": self.qr_url,
               "detail": self.detail, "reason_code": self.reason_code}
        if self.cred_source:
            out["cred_source"] = self.cred_source
            out["retry_after_sec"] = int(self.retry_after_sec)
        return out

    async def start(self, *a, **k):
        self.status = "failed"
        self.reason_code = "cred_invalid"
        self.detail = "[400 API_ID_INVALID]"
        return self.result()

    async def poll(self):
        return self.result()

    async def cancel(self):
        return None


@pytest.fixture()
def _stubbed_deps(monkeypatch):
    """让两条 provider 都跑在替身上：无 pyrogram 网络、无真实池、无换发 HTTP。"""
    async def _fake_resolve(cfg, account=None, pool_key=None):
        return cpb.Allocation(12345, "a" * 32, "config", "free", None)

    monkeypatch.setattr(cpb, "credpool_enabled", lambda cfg: False)
    monkeypatch.setattr(cpb, "aresolve_for_account", _fake_resolve)
    from src.integrations import device_fingerprint as dfp
    monkeypatch.setattr(dfp, "enabled", lambda cfg: False)
    # 换发失败（池不可换）→ 保持 cred_invalid 终态
    monkeypatch.setattr(hg, "report_invalid_and_refetch", lambda cfg, bad, **k: None)
    monkeypatch.setattr(hg, "swap_retry_after_sec", lambda: 42)
    monkeypatch.setattr(tpl, "TelegramQrLogin", _FakeFailedLogin)
    monkeypatch.setattr(tpl_phone, "TelegramPhoneLogin", _FakeFailedLogin)


def test_qr_provider_start_failure_contract(_stubbed_deps):
    _ensure_loop()
    cfg = {"telegram": {"_hosted_cred": True}}
    provider = tpl.make_provider(cfg, "sessions")
    out = asyncio.get_event_loop().run_until_complete(
        provider(None, "telegram", "protocol", ""))
    assert out["status"] == "failed"
    assert out["reason_code"] == "cred_invalid"
    assert out["cred_source"] == "hosted"
    assert out["retry_after_sec"] == 42
    # poll（旧前端仍会打一轮）读到同一份元信息
    res = asyncio.get_event_loop().run_until_complete(out["poll"](None))
    assert res["cred_source"] == "hosted" and res["retry_after_sec"] == 42


def test_qr_provider_self_filled_contract(_stubbed_deps):
    _ensure_loop()
    cfg = {"telegram": {"api_id": "111", "api_hash": "x"}}
    provider = tpl.make_provider(cfg, "sessions")
    out = asyncio.get_event_loop().run_until_complete(
        provider(None, "telegram", "protocol", ""))
    assert out["status"] == "failed"
    assert out["cred_source"] == "self"
    assert out["retry_after_sec"] == -1


def test_phone_provider_start_failure_contract(_stubbed_deps):
    _ensure_loop()
    cfg = {"telegram": {"_hosted_cred": True}}
    provider = tpl_phone.make_provider(cfg, "sessions")
    out = asyncio.get_event_loop().run_until_complete(
        provider(None, "telegram", "phone", "", ctx={"phone": "+8613800138000"}))
    assert out["status"] == "failed"
    assert out["reason_code"] == "cred_invalid"
    assert out["cred_source"] == "hosted"
    assert out["retry_after_sec"] == 42
