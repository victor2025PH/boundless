"""中央凭据池桥接门禁（platform/credpool 的引擎侧适配层）。

守住四条不变量：
1. **默认关**：不开 `platform_login.telegram.credpool.enabled` 时零网络、行为与接池前一致；
2. **绝不阻塞主流程**：池不可达 / 未配 token / 分配被拒 / 返回脏数据，一律回落自带凭据；
3. **粘定键语义**：pyrogram session 与登录所用 api_id 绑定，故 runner 必须凭 meta 里的
   key 向池索取同一组凭据——key 丢了就等于换凭据，会话会坏；
4. **半个代理比没有更糟**：出口数据不完整时宁可直连，也不能拿残缺配置去建连接。
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

    def allocate(self, phone, account_id=None, license_key=None, machine_id=None, **_kw):
        self.calls.append(("allocate", phone, account_id, license_key, machine_id))
        return self._allocate_result

    def report(self, api_id, success, error=None, phone=None):
        self.calls.append(("report", api_id, success, error, phone))
        return {"available": True, "success": True}

    def release(self, phone):
        self.calls.append(("release", phone))
        return {"available": True, "success": True}


def _install_fake(monkeypatch, client):
    monkeypatch.setattr(cb, "_make_client", lambda config: client)
    return client


def _ok_payload(api_id="222", api_hash="poolhash", **extra):
    data = {"api_id": api_id, "api_hash": api_hash}
    data.update(extra)
    return {"available": True, "success": True, "data": data}


# ── 开关语义 ─────────────────────────────────────────────────────────────

def test_disabled_by_default():
    assert cb.credpool_enabled({}) is False
    assert cb.credpool_enabled({"platform_login": {"telegram": {}}}) is False


def test_disabled_uses_config_creds_without_touching_pool(monkeypatch):
    """关池时不得构造客户端、不得产生任何 I/O。"""
    def _boom(_config):
        raise AssertionError("池未启用却尝试建客户端")

    monkeypatch.setattr(cb, "_make_client", _boom)
    alloc = cb.resolve_for_account(_cfg(enabled=False))
    assert (alloc.api_id, alloc.api_hash) == (111, "localhash")
    assert alloc.source == "config"
    assert alloc.proxy is None and alloc.tier == "free"


# ── 正常分配 ─────────────────────────────────────────────────────────────

def test_allocate_returns_pool_creds(monkeypatch):
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    alloc = cb.resolve_for_account(_cfg(), pool_key="chatx:abc")
    assert (alloc.api_id, alloc.api_hash) == (222, "poolhash")
    assert alloc.source == "credpool"
    assert fake.calls[0][:2] == ("allocate", "chatx:abc")


def test_pool_creds_take_priority_over_local(monkeypatch):
    """本地也配了凭据时仍优先用池——否则所有号共用一组，会被聚类连坐。"""
    _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert alloc.source == "credpool" and alloc.api_id == 222


def test_sticky_key_read_from_account_meta(monkeypatch):
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    account = {"account_id": "acc1", "meta": {cb.META_KEY: "chatx:sticky"}}
    alloc = cb.resolve_for_account(_cfg(), account=account)
    assert alloc.source == "credpool"
    assert fake.calls[0][:3] == ("allocate", "chatx:sticky", "acc1")


def test_tier_is_carried_through(monkeypatch):
    _install_fake(monkeypatch, _FakeClient(_ok_payload(member_level="gold")))
    assert cb.resolve_for_account(_cfg(), pool_key="k").tier == "gold"


def test_license_key_is_sent_for_tier_resolution(monkeypatch):
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    cb.resolve_for_account(_cfg(license_key="KEY-GOLD"), pool_key="k")
    assert fake.calls[0][3] == "KEY-GOLD"


def test_license_key_env_wins_over_config(monkeypatch):
    monkeypatch.setenv("CREDPOOL_LICENSE_KEY", "ENV-KEY")
    assert cb.license_key(_cfg(license_key="CFG-KEY")) == "ENV-KEY"


# ── 卡密绑机：机器标识必须稳定，且不含可反查身份的信息 ────────────────────

def test_machine_id_is_sent_with_allocation(monkeypatch):
    monkeypatch.setenv("CREDPOOL_MACHINE_ID", "mid-1")
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload()))
    cb.resolve_for_account(_cfg(), pool_key="k")
    assert fake.calls[0][4] == "mid-1", "不送 machine_id 服务端会按 free 处理"


def test_machine_id_env_wins_and_is_stable(monkeypatch):
    monkeypatch.setenv("CREDPOOL_MACHINE_ID", "mid-x")
    assert cb.machine_id() == "mid-x" == cb.machine_id()


def test_machine_id_persists_across_calls(monkeypatch, tmp_path):
    monkeypatch.delenv("CREDPOOL_MACHINE_ID", raising=False)
    monkeypatch.setattr(cb, "_MACHINE_ID", "")

    class _FakeCM:
        config_path = tmp_path / "config.yaml"

    import src.utils.config_manager as cm

    monkeypatch.setattr(cm, "ConfigManager", lambda *a, **kw: _FakeCM())
    first = cb.machine_id()
    monkeypatch.setattr(cb, "_MACHINE_ID", "")  # 模拟重启（进程缓存清空）
    assert cb.machine_id() == first, "重启后必须读回同一个 id，否则卡密要反复绑机"
    assert (tmp_path / ".credpool_machine_id").is_file()


def test_machine_id_carries_no_identifying_info(monkeypatch, tmp_path):
    monkeypatch.delenv("CREDPOOL_MACHINE_ID", raising=False)
    monkeypatch.setattr(cb, "_MACHINE_ID", "")

    class _FakeCM:
        config_path = tmp_path / "config.yaml"

    import platform as _plat
    import src.utils.config_manager as cm

    monkeypatch.setattr(cm, "ConfigManager", lambda *a, **kw: _FakeCM())
    mid = cb.machine_id()
    assert mid.startswith("chatx-") and len(mid) > 20
    assert _plat.node().lower() not in mid.lower(), "不得含主机名"


# ── 三隔离第三件套：独立出口 ──────────────────────────────────────────────

def test_proxy_is_carried_through(monkeypatch):
    proxy = {"scheme": "socks5", "host": "1.2.3.4", "port": 1080,
             "username": "u", "password": "p"}
    _install_fake(monkeypatch, _FakeClient(_ok_payload(proxy=proxy)))
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert alloc.proxy == proxy


def test_proxy_defaults_scheme_and_drops_empty_auth(monkeypatch):
    _install_fake(monkeypatch, _FakeClient(
        _ok_payload(proxy={"host": "1.2.3.4", "port": "1080",
                           "username": "", "password": ""})))
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert alloc.proxy == {"scheme": "socks5", "host": "1.2.3.4", "port": 1080}


@pytest.mark.parametrize("bad", [
    None, "notadict", {}, {"host": "1.2.3.4"}, {"port": 1080},
    {"host": "1.2.3.4", "port": 0}, {"host": "", "port": 1080},
    {"host": "1.2.3.4", "port": "abc"},
])
def test_incomplete_proxy_is_dropped_not_half_used(monkeypatch, bad):
    """半个代理会让整条连接起不来；没有代理只是直连，不阻塞登录。"""
    _install_fake(monkeypatch, _FakeClient(_ok_payload(proxy=bad)))
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert alloc.api_id == 222, "出口有问题不该连凭据一起丢"
    assert alloc.proxy is None


def test_config_fallback_carries_no_proxy(monkeypatch):
    """自带凭据本就不带隔离能力，如实反映。"""
    _install_fake(monkeypatch, _FakeClient({"available": False}))
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert alloc.source == "config" and alloc.proxy is None


# ── P2-⑨ 托管派发出口/换组凭据缓存（config 回落链的三件套补齐）───────────────

def test_config_fallback_carries_hosted_proxy(monkeypatch):
    """托管派发随凭据带了出口（telegram._hosted_proxy）→ config 回落也三件套齐发。"""
    cfg = {"telegram": {"api_id": 111, "api_hash": "localhash",
                        "_hosted_proxy": {"scheme": "socks5", "host": "5.6.7.8",
                                          "port": 1080, "username": "u", "password": "p"}}}
    # 无池键 = 走新登录 config 回落分支（不开池，零 I/O）
    alloc = cb.resolve_for_account(cfg)
    assert alloc.source == "config" and alloc.api_id == 111
    assert alloc.proxy and alloc.proxy["host"] == "5.6.7.8"


def test_config_fallback_half_hosted_proxy_dropped(monkeypatch):
    """半个 _hosted_proxy（缺端口）→ 丢掉不半用（与池出口同口径）。"""
    cfg = {"telegram": {"api_id": 111, "api_hash": "localhash",
                        "_hosted_proxy": {"host": "5.6.7.8", "port": 0}}}
    alloc = cb.resolve_for_account(cfg)
    assert alloc.source == "config" and alloc.proxy is None


def test_hosted_cred_cache_beats_config_after_group_swap(monkeypatch):
    """换组事故的收口：账号 meta 里有登录时缓存的凭据 → 优先于 config 注入的**新组**，
    避免 runner 拿新组 api_id 跑旧 session（错配风控）。自建账号无此缓存=旧行为。"""
    # config 里是「换组后的新组」凭据，但该账号 session 是旧组建的（缓存在 meta）
    cfg = {"telegram": {"api_id": 999, "api_hash": "newgroup"}}
    account = {"account_id": "acc1", "platform": "telegram", "meta": {
        cb.META_CRED_KEY: {"api_id": 111, "api_hash": "oldgroup", "tier": "free",
                           "proxy": {"scheme": "socks5", "host": "5.6.7.8", "port": 1080}}}}
    alloc = cb.resolve_for_account(cfg, account=account)  # 无池键=新登录/自建链
    assert alloc.api_id == 111, "必须用 session 绑定的旧组，不能用 config 的新组"
    assert alloc.source == "credpool_cache"
    assert alloc.proxy and alloc.proxy["host"] == "5.6.7.8"


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
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert (alloc.api_id, alloc.api_hash) == (111, "localhash"), why
    assert alloc.source == "config", why


def test_unconfigured_token_falls_back(monkeypatch):
    """没配服务 token 时不该硬打接口，直接回落。"""
    fake = _install_fake(monkeypatch, _FakeClient(_ok_payload(), configured=False))
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert (alloc.api_id, alloc.source) == (111, "config")
    assert fake.calls == []


def test_client_load_failure_falls_back(monkeypatch):
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert (alloc.api_id, alloc.source) == (111, "config")


def test_no_creds_anywhere_returns_none(monkeypatch):
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    cfg = {"platform_login": {"telegram": {"credpool": {"enabled": True}}}}
    assert cb.resolve_for_account(cfg, pool_key="k") is None


def test_allocate_exception_does_not_propagate(monkeypatch):
    class _Boom(_FakeClient):
        def allocate(self, phone, account_id=None, license_key=None, **_kw):
            raise RuntimeError("网络炸了")

    _install_fake(monkeypatch, _Boom())
    alloc = cb.resolve_for_account(_cfg(), pool_key="k")
    assert (alloc.api_id, alloc.source) == (111, "config")


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
    alloc = await cb.aresolve_for_account(_cfg(), pool_key="k")
    assert (alloc.api_id, alloc.source) == (222, "credpool")


async def test_async_wrapper_no_thread_when_disabled(monkeypatch):
    """关池时不该走线程池（省开销，也保证零 I/O）。"""
    import asyncio

    def _boom(*_a, **_kw):
        raise AssertionError("关池却切线程")

    monkeypatch.setattr(asyncio, "to_thread", _boom)
    alloc = await cb.aresolve_for_account(_cfg(enabled=False))
    assert (alloc.api_id, alloc.source) == (111, "config")


async def test_areport_best_effort(monkeypatch):
    class _Boom(_FakeClient):
        def report(self, **_kw):
            raise RuntimeError("boom")

    _install_fake(monkeypatch, _Boom())
    await cb.areport(_cfg(), 222, False, error="x", pool_key="k")  # 不得抛
