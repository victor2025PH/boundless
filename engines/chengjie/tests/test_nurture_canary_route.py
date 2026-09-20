# -*- coding: utf-8 -*-
"""智能养号「试点账号」写路径 + status 契约门禁（P1 2026-08-22 试点 UI 化）。

``ops.nurture.canary_accounts`` 此前只能手改 YAML；``POST /api/nurture/config
{canary:{key,pilot}}`` 是它唯一的 UI 写入口——本门禁钉住：

1. 整表回写语义：追加保序、重复设置＝剔旧置尾、取消＝剔除，全程去重；
2. 写必须经 ``config_manager.set_overlay_flag``（保注释热生效链，勿换裸写）；
3. 坏 key（无冒号/空）400，不碰配置；
4. status 恒带 ``can_write``（viewer 判定与写路由 403 同源；无 Session 中间件时
   _is_viewer 软失败为 False＝可写，测试环境即此路径）。

fleet_overview 依赖真实注册表（conftest 已把 AITR_DATA_DIR 指向进程级 tmp → 空注册表），
accounts 为空不影响写路径断言；账号行 is_canary 渲染契约由 tools/verify_nurture_ui.py
浏览器门禁按 stub 契约钉住。
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.nurture_routes import register_nurture_routes


class _FakeCfgMgr:
    """与真实 config_manager 同语义的最小替身：set_overlay_flag 写进内存配置。"""

    def __init__(self) -> None:
        self.config: Dict[str, Any] = {"ops": {"nurture": {"canary_accounts": []}}}
        self.writes: List[Tuple[str, Any]] = []

    def set_overlay_flag(self, path: str, value: Any):
        self.writes.append((path, value))
        node = self.config
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        return True, ""


@pytest.fixture()
def client_cfg():
    app = FastAPI()
    cfg = _FakeCfgMgr()

    async def _auth():
        return True

    register_nurture_routes(app, _auth, audit_store=None, config_manager=cfg)
    return TestClient(app), cfg


def _post_canary(client, key, pilot):
    return client.post("/api/nurture/config", json={"canary": {"key": key, "pilot": pilot}})


def test_canary_add_preserves_order_and_dedupes(client_cfg):
    client, cfg = client_cfg
    r = _post_canary(client, "line:u1", True)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["canary_accounts"] == ["line:u1"]
    r = _post_canary(client, "telegram:t9", True)
    assert r.json()["canary_accounts"] == ["line:u1", "telegram:t9"]
    # 重复设置＝剔旧置尾（不产生重复条目）
    r = _post_canary(client, "line:u1", True)
    assert r.json()["canary_accounts"] == ["telegram:t9", "line:u1"]
    # 全部写都必须走 set_overlay_flag 的 canary_accounts 键
    assert cfg.writes and all(p == "ops.nurture.canary_accounts" for p, _v in cfg.writes)


def test_canary_remove(client_cfg):
    client, cfg = client_cfg
    _post_canary(client, "line:u1", True)
    _post_canary(client, "telegram:t9", True)
    r = _post_canary(client, "line:u1", False)
    assert r.status_code == 200
    assert r.json()["pilot"] is False
    assert r.json()["canary_accounts"] == ["telegram:t9"]
    # 剔不存在的 key＝幂等（不报错、表不变）
    r = _post_canary(client, "whatsapp:none", False)
    assert r.status_code == 200 and r.json()["canary_accounts"] == ["telegram:t9"]


def test_canary_bad_key_rejected_without_write(client_cfg):
    client, cfg = client_cfg
    before = len(cfg.writes)
    assert _post_canary(client, "nocolon", True).status_code == 400
    assert _post_canary(client, "", True).status_code == 400
    assert _post_canary(client, "   ", True).status_code == 400
    assert len(cfg.writes) == before, "坏 key 不得产生任何配置写"


def test_status_carries_can_write(client_cfg):
    client, _cfg = client_cfg
    r = client.get("/api/nurture/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["can_write"] is True   # 无 session＝非 viewer＝可写（软失败语义）
    assert "accounts" in body and "engine" in body
