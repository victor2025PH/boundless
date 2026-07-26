"""中央凭据池桥接门禁（platform/credpool 的引擎侧适配层）。

守住三条不变量：
1. **默认关**：不开 `platform_login.telegram.credpool.enabled` 时零网络、行为与接池前一致；
2. **绝不阻塞主流程**：池不可达 / 未配 token / 分配被拒 / 返回脏数据，一律回落自带凭据；
3. **粘定键语义**：pyrogram session 与登录所用 api_id 绑定，故 runner 必须凭 meta 里的
   key 向池索取同一组凭据——key 丢了就等于换凭据，会话会坏。
"""
from __future__ import annotations

import pytest

from src.integrations import credpool_bridge as cb


@pytest.fixture(autouse=True)
def _reset_client_cache():
    """瘦客户端是模块级缓存，逐用例复位防串味。"""
    cb._CLIENT_MOD = None
    cb._CLIENT_LOAD_FAILED = False
    yield
    cb._CLIENT_MOD = None
    cb._CLIENT_LOAD_FAILED = False


LOCAL_CFG = {"telegram": {"api_id": 111, "api_hash": "localhash"}}


def _cfg(enabled=True, **extra):
    c = dict(LOCAL_CFG)
    c["platform_login"] = {"telegram": {"credpool": {"enabled": enabled, **extra}}}
    return c


class _FakeClient:
    """替身瘦客户端；记录调用，不产生任何网络 I/O。"""

    def __init__(self, allocate_result=None, configured=True, **_kw):
        self._allocate_result = allocate_result or {}
        self._configured = configured
        self.calls = []

    def configured(self):
        return self._configured

    def allocate(self, phone, account_id=None, **_kw):
        self.calls.append(("allocate", phone, account_id))
        return self._allocate_result

    def report(self, api_id, success, error=None, phone=None):
        self.calls.append(("report", api_id, success, error, phone))
        return {"available": True, "success": True}

    def release(self, phone):
        self.calls.append(("release", phone))
        return {"available": True, "success": True}


def _install_fake(monkeypatch, client):
    """把桥接层的客户端工厂替换成替身。"""
    monkeypatch.setattr(cb, "_make_client", lambda config: client)
    return client


def _ok_payload(api_id="222", api_hash="poolhash"):
    return {"available": True, "success": True,
            "data": {"api_id": api_id, "api_hash": api_hash}}


# ── 开关语义 ─────────────────────────────────────────────────────────────

def test_disabled_by_default():
    assert cb.credpool_enabled({}) is False
    assert cb.credpool_enabled({"platform_login": {"telegram": {}}}) is False


def test_disabled_uses_config_creds_without_touching_pool(monkeypatch):
    """关池时不得构造客户端、不得产生任何 I/O。"""
    def _boom(_config):
        raise AssertionError("池未启用却尝试建客户端")

    monkeypatch.setattr(cb, "_make_client", _boom)
    creds, source = cb.resolve_credentials_for_account(_cfg(enabled=False))
    assert creds == (111, "localhash")
    assert source == "config"


# ── 正常分配 ─────────────────────────────────────────────────────────────

def test_allocate_returns_pool_creds(monkeypatch):
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    creds, source = cb.resolve_credentials_for_account(
        _cfg(), pool_key="chatx:abc")
    assert creds == (222, "poolhash")
    assert source == "credpool"
    assert fake.calls[0][:2] == ("allocate", "chatx:abc")


def test_pool_creds_take_priority_over_local(monkeypatch):
    """本地也配了凭据时仍优先用池——否则所有号共用一组，会被聚类连坐。"""
    _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    creds, source = cb.resolve_credentials_for_account(_cfg(), pool_key="k")
    assert source == "credpool" and creds[0] == 222


def test_sticky_key_read_from_account_meta(monkeypatch):
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    account = {"account_id": "acc1", "meta": {cb.META_KEY: "chatx:sticky"}}
    creds, source = cb.resolve_credentials_for_account(_cfg(), account=account)
    assert source == "credpool"
    assert fake.calls[0] == ("allocate", "chatx:sticky", "acc1")


# ── 降级回落（核心安全性）─────────────────────────────────────────────────

@pytest.mark.parametrize("payload,why", [
    ({"available": False, "error": "URLError"}, "池不可达"),
    ({"available": True, "success": False, "message": "池已空"}, "业务拒绝"),
    ({"available": True, "success": True, "data": {}}, "空数据"),
    ({"available": True, "success": True,
      "data": {"api_id": "abc", "api_hash": "h"}}, "api_id 非数字"),
])
def test_falls_back_to_config_on_any_pool_problem(monkeypatch, payload, why):
    _install_fake(monkeypatch, _FakeClient(payload))
    creds, source = cb.resolve_credentials_for_account(_cfg(), pool_key="k")
    assert creds == (111, "localhash"), why
    assert source == "config", why


def test_unconfigured_token_falls_back(monkeypatch):
    """没配服务 token 时不该硬打接口，直接回落。"""
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload(), configured=False))
    creds, source = cb.resolve_credentials_for_account(_cfg(), pool_key="k")
    assert (creds, source) == ((111, "localhash"), "config")
    assert fake.calls == []


def test_client_load_failure_falls_back(monkeypatch):
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    creds, source = cb.resolve_credentials_for_account(_cfg(), pool_key="k")
    assert (creds, source) == ((111, "localhash"), "config")


def test_no_creds_anywhere_returns_none(monkeypatch):
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    cfg = {"platform_login": {"telegram": {"credpool": {"enabled": True}}}}
    creds, source = cb.resolve_credentials_for_account(cfg, pool_key="k")
    assert creds is None and source == "none"


def test_allocate_exception_does_not_propagate(monkeypatch):
    class _Boom(_FakeClient):
        def allocate(self, phone, account_id=None, **_kw):
            raise RuntimeError("网络炸了")

    _install_fake(monkeypatch, _Boom())
    creds, source = cb.resolve_credentials_for_account(_cfg(), pool_key="k")
    assert (creds, source) == ((111, "localhash"), "config")


# ── 回报 / 释放：best-effort，绝不影响调用方 ──────────────────────────────

def test_report_is_best_effort(monkeypatch):
    class _Boom(_FakeClient):
        def report(self, **_kw):
            raise RuntimeError("boom")

    _install_fake(monkeypatch, _Boom())
    cb.report(_cfg(), 222, True, pool_key="k")  # 不得抛


def test_report_skipped_when_disabled(monkeypatch):
    def _boom(_config):
        raise AssertionError("关池却建客户端")

    monkeypatch.setattr(cb, "_make_client", _boom)
    cb.report(_cfg(enabled=False), 222, True)
    cb.release(_cfg(enabled=False), "k")


def test_release_passes_pool_key(monkeypatch):
    fake = _install_fake(monkeypatch, _FakeClient())
    cb.release(_cfg(), "chatx:zzz")
    assert fake.calls == [("release", "chatx:zzz")]


# ── 粘定键 ───────────────────────────────────────────────────────────────

def test_new_pool_key_shape_and_uniqueness():
    a, b = cb.new_pool_key(), cb.new_pool_key()
    assert a.startswith("chatx:") and a != b


def test_pool_key_of_handles_missing():
    assert cb.pool_key_of(None) == ""
    assert cb.pool_key_of({}) == ""
    assert cb.pool_key_of({"meta": {cb.META_KEY: "k"}}) == "k"


# ── 异步包装（防阻塞事件循环）────────────────────────────────────────────

async def test_async_wrapper_returns_same_result(monkeypatch):
    _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    creds, source = await cb.aresolve_credentials_for_account(_cfg(), pool_key="k")
    assert (creds, source) == ((222, "poolhash"), "credpool")


async def test_async_wrapper_no_thread_when_disabled(monkeypatch):
    """关池时不该走线程池（省开销，也保证零 I/O）。"""
    import asyncio

    def _boom(*_a, **_kw):
        raise AssertionError("关池却切线程")

    monkeypatch.setattr(asyncio, "to_thread", _boom)
    creds, source = await cb.aresolve_credentials_for_account(_cfg(enabled=False))
    assert (creds, source) == ((111, "localhash"), "config")


async def test_areport_best_effort(monkeypatch):
    class _Boom(_FakeClient):
        def report(self, **_kw):
            raise RuntimeError("boom")

    _install_fake(monkeypatch, _Boom())
    await cb.areport(_cfg(), 222, False, error="x", pool_key="k")  # 不得抛
