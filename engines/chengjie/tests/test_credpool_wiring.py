"""中央凭据池接线门禁：登录 provider / runner worker / 注册门控三处。

这三处是「新用户不用申请 api_id」能否成立的全部落点，任何一处退回读死配置，
要么用户又被逼去 my.telegram.org，要么 runner 用错凭据把 session 跑坏。
"""
from __future__ import annotations

import inspect

import pytest

from src.integrations import account_orchestrator as ao
from src.integrations import credpool_bridge as cb
from src.integrations import platform_login as pl
from src.integrations import telegram_protocol_login as tpl


POOL_CFG = {
    "platform_login": {
        "telegram": {"protocol_enabled": True, "credpool": {"enabled": True}},
    },
}


@pytest.fixture(autouse=True)
def _clean_registry():
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)
    yield
    tpl._registered = False
    pl._PROVIDERS.pop(pl._pkey("telegram", "protocol"), None)


# ── 注册门控 ─────────────────────────────────────────────────────────────

def test_registers_with_pool_and_no_local_creds():
    """开池即可注册——这正是「用户没有自己的 api_id 也能扫码」的前提。"""
    if not tpl.is_pyrogram_available():
        pytest.skip("pyrogram 未安装")
    assert tpl.maybe_register(POOL_CFG) is True
    assert pl.mode_available("telegram", "protocol") is True


def test_still_gated_by_protocol_enabled():
    """开了池但没开 protocol_enabled 仍不注册（两道闸互不替代）。"""
    cfg = {"platform_login": {"telegram": {"credpool": {"enabled": True}}}}
    assert tpl.maybe_register(cfg) is False


def test_no_pool_no_creds_still_refuses():
    assert tpl.maybe_register(
        {"platform_login": {"telegram": {"protocol_enabled": True}}}) is False


# ── 登录 provider：无凭据时的提示不得回归 ─────────────────────────────────

async def test_provider_reports_missing_creds_when_pool_unreachable(monkeypatch):
    monkeypatch.setattr(cb, "_make_client", lambda config: None)
    provider = tpl.make_provider(POOL_CFG)
    res = await provider(None, "telegram", "protocol", "acc1")
    assert "api_id" in res.get("instruction", "")


# ── 异步安全：两个 async 落点都必须走异步包装，不能阻塞事件循环 ───────────

def test_login_provider_uses_async_resolver():
    src = inspect.getsource(tpl.make_provider)
    assert "aresolve_credentials_for_account" in src
    assert "await" in src


def test_worker_uses_async_resolver_and_account_meta():
    src = inspect.getsource(ao.TelegramProtocolWorker.start)
    assert "aresolve_credentials_for_account" in src
    # 必须把 account 传进去，否则读不到 meta 里的粘定键 → 换凭据 → session 坏
    assert "account=self.account" in src


# ── 粘定键落库：丢了它，下次拉起就会换凭据 ────────────────────────────────

def test_provider_persists_sticky_key_and_reports():
    src = inspect.getsource(tpl.make_provider)
    assert "_cpb.META_KEY" in src, "扫码成功必须把粘定键写进账号 meta"
    assert "report(config, api_id, True" in src, "登录成功必须回报池的健康分"
    assert "report(config, api_id, False" in src, "登录失败必须回报，否则坏凭据不会被换掉"


def test_meta_key_is_stable_contract():
    """meta 键名是跨版本契约（改名会让存量账号丢失粘定键）。"""
    assert cb.META_KEY == "credpool_key"


# ── 删号还容量：不还会造成池容量缓慢泄漏 ──────────────────────────────────

# ── 桌面打包：瘦客户端在引擎目录之外，不显式带上就会静默失效 ──────────────

def test_client_found_in_source_tree():
    """源码态必须能真找到 platform/credpool/credpool_client.py。"""
    assert any(p.is_file() for p in cb._client_path_candidates())


def test_frozen_bundle_path_is_probed(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "_MEIPASS", r"X:\bundle", raising=False)
    cands = [str(p) for p in cb._client_path_candidates()]
    assert any(c.startswith(r"X:\bundle") and c.endswith("credpool_client.py")
               for c in cands), "冻结态必须先找 sys._MEIPASS 下的副本"


def test_desktop_build_bundles_credpool_client():
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parents[1]
           / "desktop" / "build" / "build_backend.py").read_text(encoding="utf-8")
    assert "platform/credpool" in src, "桌面打包清单必须带上中央池瘦客户端"


def test_registry_remove_releases_pool_capacity():
    from src.integrations import account_registry as ar

    src = inspect.getsource(ar.AccountRegistry.remove)
    assert "release_for_account_bg" in src


def test_release_bg_is_non_blocking_and_platform_scoped(monkeypatch):
    """非 telegram 平台不该触发任何池调用；telegram 走后台线程不阻塞调用方。"""
    started = []
    monkeypatch.setattr(cb, "release", lambda *a, **kw: started.append(a))

    cb.release_for_account_bg("line", "acc1")
    assert started == []

    import threading

    spawned = []

    class _FakeThread:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target
            spawned.append(name)

        def start(self):
            pass  # 不真跑，只验证「走的是线程、没在调用线程里同步打 HTTP」

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    cb.release_for_account_bg("telegram", "acc1")
    assert spawned == ["credpool-release"]
