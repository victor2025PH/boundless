# -*- coding: utf-8 -*-
"""编排器工厂注册豁免 + 拉号自愈门禁（P1-198，2026-08-05）。

198 事故链修复面之一：托管/中央池桌面部署的 config 里**没有本地 api_id**
（凭据由网关运行期注入 / 池按账号粘定键派发），而旧的
``ensure_builtin_workers`` 把「config 有 api_id」当 telegram worker 工厂的
注册前置——网关/池在启动窗口不可达时工厂永远缺位，重启后在线号全部不
恢复，且失败只有 debug 级（backend.log 零痕迹）。两层修复：

1. 注册门槛加中央池豁免（与登录侧 ``maybe_register`` 同款）；
2. ``_start_account`` 无工厂时先自愈重试一次 ``ensure_builtin_workers``
   （凭据晚到 → 第一次拉号即自我修复，不必等重启）。

重点覆盖**不该注册/不该自愈**的边界。
"""
import os
import tempfile

import pytest

from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator
from src.integrations.account_registry import AccountRegistry


class FakeWorker:
    def __init__(self, account, config):
        self.account = account
        self.started = 0

    async def start(self):
        self.started += 1

    async def stop(self):
        pass

    async def healthy(self):
        return True

    def status(self):
        return {"type": "fake"}


@pytest.fixture()
def registry():
    return AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))


@pytest.fixture(autouse=True)
def _clean_factory():
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    yield
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


def _patch_tg_predicates(monkeypatch, *, creds, pool, pyro=True, enabled=True,
                         companion=False):
    import src.integrations.telegram_protocol_login as tpl
    monkeypatch.setattr(tpl, "protocol_enabled", lambda cfg: enabled)
    monkeypatch.setattr(tpl, "is_pyrogram_available", lambda: pyro)
    monkeypatch.setattr(
        tpl, "resolve_credentials",
        lambda cfg: (11111, "hash") if creds else None)
    import src.integrations.credpool_bridge as cpb
    monkeypatch.setattr(cpb, "credpool_enabled", lambda cfg: pool)
    import src.integrations.telegram_companion_worker as tcw
    monkeypatch.setattr(tcw, "companion_runtime_enabled",
                        lambda cfg: companion)


def test_factory_registers_with_local_creds(monkeypatch):
    _patch_tg_predicates(monkeypatch, creds=True, pool=False)
    orch.ensure_builtin_workers({})
    assert orch.get_worker_factory("telegram", "protocol") is not None


def test_factory_registers_with_credpool_and_no_local_creds(monkeypatch):
    """中央池豁免（修复点）：config 无 api_id 也要注册工厂。"""
    _patch_tg_predicates(monkeypatch, creds=False, pool=True)
    orch.ensure_builtin_workers({})
    assert orch.get_worker_factory("telegram", "protocol") is not None


def test_factory_skipped_without_creds_and_pool(monkeypatch):
    """两者皆无 → 维持不注册（自备凭据流程尚未配置，注册了也起不来）。"""
    _patch_tg_predicates(monkeypatch, creds=False, pool=False)
    orch.ensure_builtin_workers({})
    assert orch.get_worker_factory("telegram", "protocol") is None


def test_factory_skipped_when_protocol_disabled(monkeypatch):
    _patch_tg_predicates(monkeypatch, creds=True, pool=True, enabled=False)
    orch.ensure_builtin_workers({})
    assert orch.get_worker_factory("telegram", "protocol") is None


async def test_start_account_selfheals_missing_factory(monkeypatch, registry):
    """无工厂 → 先自愈重试注册再拉起（凭据晚到场景，第一次拉号即恢复）。"""
    registry.upsert("telegram", "1", mode="protocol", status="online")

    def _late_register(cfg):
        orch.register_worker("telegram", "protocol",
                             lambda a, c: FakeWorker(a, c))

    monkeypatch.setattr(orch, "ensure_builtin_workers", _late_register)
    o = AccountOrchestrator(registry=registry)
    ok = await o.start_account(registry.get("telegram", "1"))
    assert ok is True
    st = o.status()
    assert st["by_state"].get("running") == 1


async def test_start_account_fails_honestly_when_selfheal_cannot(
        monkeypatch, registry):
    """自愈也救不回（条件真不满足）→ 如实 error，不假装成功。"""
    registry.upsert("telegram", "1", mode="protocol", status="online")
    monkeypatch.setattr(orch, "ensure_builtin_workers", lambda cfg: None)
    o = AccountOrchestrator(registry=registry)
    ok = await o.start_account(registry.get("telegram", "1"))
    assert ok is False
    accounts = {a["key"]: a for a in o.status()["accounts"]}
    assert accounts["telegram:1"]["state"] == "error"
    assert "no worker factory" in accounts["telegram:1"]["last_error"]
