# -*- coding: utf-8 -*-
"""账号级人设换绑热生效门禁（2026-08-18 报障群工单#1 根因修复）。

事故：`telegram_client.__init__` 只在启动时读一次 ``persona_ids`` 快照，
换绑路由只写注册表 → 运行中的账号要重启才用上新人设（客户实录
「人设植入了为什么用不了」+「绑定账号了」）。修复＝换绑路由写库后
``_hot_apply_account_persona`` best-effort 直写在跑 client 的快照。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


def _patch_orch(monkeypatch, orch):
    from src.integrations import account_orchestrator as ao
    monkeypatch.setattr(ao, "get_orchestrator_if_running", lambda: orch)


def test_hot_apply_updates_running_client(monkeypatch):
    from src.web.routes.persona_routes import _hot_apply_account_persona
    client = SimpleNamespace(account_persona_ids=["lin_xiaoyu"])
    worker = SimpleNamespace(client=client)
    orch = SimpleNamespace(worker_for=lambda p, a: worker)
    _patch_orch(monkeypatch, orch)
    assert _hot_apply_account_persona("telegram", "123", "chatx_support") is True
    assert client.account_persona_ids == ["chatx_support"]
    # 清除绑定 → 空列表（回域默认；重启后回 default_persona_id 属已知轻微分叉，
    # 见路由注释）
    assert _hot_apply_account_persona("telegram", "123", "") is True
    assert client.account_persona_ids == []


def test_hot_apply_worker_offline_or_alien_client(monkeypatch):
    from src.web.routes.persona_routes import _hot_apply_account_persona
    # 编排器没起
    _patch_orch(monkeypatch, None)
    assert _hot_apply_account_persona("telegram", "123", "x") is False
    # worker 在但 client 缺属性（LINE/WA 形态）→ False 且不抛
    alien = SimpleNamespace(client=SimpleNamespace())
    _patch_orch(monkeypatch, SimpleNamespace(worker_for=lambda p, a: alien))
    assert _hot_apply_account_persona("line", "U1", "x") is False
    # worker_for 抛异常 → False 且不抛
    def _boom(p, a):
        raise RuntimeError("x")
    _patch_orch(monkeypatch, SimpleNamespace(worker_for=_boom))
    assert _hot_apply_account_persona("telegram", "123", "x") is False


def test_route_wired_to_hot_apply():
    src = (ROOT / "src/web/routes/persona_routes.py").read_text(encoding="utf-8")
    # 换绑路由必须在写库后调用热生效 helper 并回传 hot_applied
    idx_upsert = src.index('"persona_id_auto": False')
    seg = src[idx_upsert:idx_upsert + 2000]
    assert "_hot_apply_account_persona(plat, acct, profile_id)" in seg
    assert '"hot_applied": hot_applied' in seg
