# -*- coding: utf-8 -*-
"""账号坞排序漫游（Dock P3）门禁：sanitize_dock_order 净化 / 路由写键 / carry-over 保全。

三条不变量（改 appearance_prefs.sanitize_dock_order / workspace_prefs 路由前先读）：
1. sanitize_dock_order 值域封闭——plat/aid 白名单正则、条数上限、去重；空 plats 合法
   （显式「已清空」要跨设备传播，与「从未设置」区分）。
2. POST dock_order 只改 appearance JSON 的 dock_order 子键，其余外观键原样保留。
3. appearance 整写（appearance.js 的全状态推送，不带 dock_order）绝不抹掉库里已有的
   dock_order——路由层 carry-over。这是本设计能与 appearance.js 零改动共存的全部前提。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.web.appearance_prefs import (
    dumps_appearance,
    loads_appearance,
    sanitize_appearance,
    sanitize_dock_order,
)


# ---------------------------------------------------------------------------
# 1) sanitize_dock_order 纯函数
# ---------------------------------------------------------------------------

def test_dock_order_valid_roundtrip():
    dk = sanitize_dock_order({
        "ts": 1755380000000,
        "plats": {
            "telegram": ["8244899900", "tg-desktop"],
            "line": ["Uk4bhjJ4-HC2jhfuF0CwF4LlyIJ_WgLnScdIt1OIkQOg"],
        },
    })
    assert dk is not None
    assert dk["ts"] == 1755380000000
    assert dk["plats"]["telegram"] == ["8244899900", "tg-desktop"]
    assert len(dk["plats"]["line"]) == 1


@pytest.mark.parametrize("bad", [None, 42, "x", [], {"plats": "no"}, {"plats": None}])
def test_dock_order_bad_shape_rejected(bad):
    assert sanitize_dock_order(bad) is None


def test_dock_order_empty_plats_is_valid_explicit_clear():
    dk = sanitize_dock_order({"ts": 5, "plats": {}})
    assert dk == {"ts": 5, "plats": {}}


def test_dock_order_whitelists_and_caps():
    dk = sanitize_dock_order({
        "ts": "nan",
        "plats": {
            "telegram": ["1", "1", "2", '<script>"x"</script>', "ok_id"],
            "BAD PLAT!": ["1"],
            "web": "not-a-list",
            "many": [str(i) for i in range(99)],
        },
    })
    assert dk is not None and dk["ts"] == 0
    assert dk["plats"]["telegram"] == ["1", "2", "ok_id"]   # 去重 + 恶意 id 剔除
    assert "BAD PLAT!" not in dk["plats"]                    # plat 正则白名单
    assert "web" not in dk["plats"]                          # 值必须是 list
    assert len(dk["plats"]["many"]) == 30                    # 每平台条数封顶


def test_sanitize_appearance_passes_dock_order_through():
    ap = sanitize_appearance({
        "theme": "aurora",
        "dock_order": {"ts": 7, "plats": {"telegram": ["1"]}},
    })
    assert ap is not None
    assert ap["dock_order"] == {"ts": 7, "plats": {"telegram": ["1"]}}
    # 非法 dock_order 静默丢弃（不 400 掉整个 appearance 写链）
    ap2 = sanitize_appearance({"theme": "aurora", "dock_order": ["bad"]})
    assert ap2 is not None and "dock_order" not in ap2
    # 序列化回放
    assert loads_appearance(dumps_appearance(ap))["dock_order"]["plats"]["telegram"] == ["1"]


# ---------------------------------------------------------------------------
# 2/3) 路由集成（假 app 直调 handler，真 InboxStore）
# ---------------------------------------------------------------------------

class _App:
    def __init__(self):
        self.handlers = {}

    def get(self, path):
        def deco(fn):
            self.handlers[("GET", path)] = fn
            return fn
        return deco

    def post(self, path):
        def deco(fn):
            self.handlers[("POST", path)] = fn
            return fn
        return deco


class _Req:
    def __init__(self, body=None):
        self._body = body or {}
        self.state = SimpleNamespace(ui_lang="zh")

    async def json(self):
        return self._body


def _route_env(tmp_path):
    from src.inbox.store import InboxStore
    from src.web.routes import unified_inbox_workspace_prefs_routes as mod
    store = InboxStore(tmp_path / "inbox.db")
    app = _App()
    origs = (mod._inbox_store, mod._session_agent, mod._sla_cfg, mod._agent_sla_cfg)
    mod._inbox_store = lambda request: store
    mod._session_agent = lambda request: {"agent_id": "a1", "display_name": "A"}
    # GET 分支要的 SLA 全局阈值与生效值——本门禁只关心 appearance 回显，stub 掉
    mod._sla_cfg = lambda request: {"warn": 0, "crit": 0}
    mod._agent_sla_cfg = lambda request: {"warn": 0, "crit": 0}
    mod.register_workspace_prefs_routes(app, api_auth=lambda request: None)
    return mod, app, store, origs


def _restore(mod, origs):
    (mod._inbox_store, mod._session_agent,
     mod._sla_cfg, mod._agent_sla_cfg) = origs


def test_post_dock_order_writes_subkey_and_keeps_appearance(tmp_path):
    mod, app, store, origs = _route_env(tmp_path)
    try:
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        asyncio.run(fn(_Req({"appearance": {"theme": "sunset", "fs": 14}})))
        out = asyncio.run(fn(_Req({"dock_order": {"ts": 9, "plats": {"telegram": ["2", "1"]}}})))
        assert out["ok"] is True
        ap = out["prefs"]["appearance"]
        assert ap["theme"] == "sunset" and ap["fs"] == 14      # 外观键原样保留
        assert ap["dock_order"]["plats"]["telegram"] == ["2", "1"]
        # 落库口径同样带子键
        stored = json.loads(store.get_agent_prefs("a1")["appearance"])
        assert stored["dock_order"]["ts"] == 9
    finally:
        _restore(mod, origs)


def test_appearance_full_write_carries_over_stored_dock_order(tmp_path):
    """appearance.js 整状态推送（不带 dock_order）绝不抹掉已存排序——本设计核心不变量。"""
    mod, app, store, origs = _route_env(tmp_path)
    try:
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        asyncio.run(fn(_Req({"dock_order": {"ts": 9, "plats": {"line": ["a", "b"]}}})))
        out = asyncio.run(fn(_Req({"appearance": {"theme": "graphite"}})))   # 模拟 appearance.js pushSrv
        ap = out["prefs"]["appearance"]
        assert ap["theme"] == "graphite"
        assert ap["dock_order"]["plats"]["line"] == ["a", "b"]   # carry-over 成立
    finally:
        _restore(mod, origs)


def test_post_dock_order_empty_plats_propagates_clear(tmp_path):
    mod, app, store, origs = _route_env(tmp_path)
    try:
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        asyncio.run(fn(_Req({"dock_order": {"ts": 1, "plats": {"telegram": ["1"]}}})))
        out = asyncio.run(fn(_Req({"dock_order": {"ts": 2, "plats": {}}})))
        assert out["prefs"]["appearance"]["dock_order"] == {"ts": 2, "plats": {}}
    finally:
        _restore(mod, origs)


def test_post_invalid_dock_order_rejected_400(tmp_path):
    from fastapi import HTTPException
    mod, app, store, origs = _route_env(tmp_path)
    try:
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        with pytest.raises(HTTPException) as ei:
            asyncio.run(fn(_Req({"dock_order": ["not", "a", "dict"]})))
        assert ei.value.status_code == 400
        assert store.get_agent_prefs("a1")["appearance"] == ""   # 拒绝即不落库
    finally:
        _restore(mod, origs)


def test_get_echoes_dock_order(tmp_path):
    mod, app, store, origs = _route_env(tmp_path)
    try:
        post = app.handlers[("POST", "/api/workspace/prefs")]
        get = app.handlers[("GET", "/api/workspace/prefs")]
        asyncio.run(post(_Req({"dock_order": {"ts": 3, "plats": {"whatsapp": ["66123"]}}})))
        out = asyncio.run(get(_Req()))
        assert out["prefs"]["appearance"]["dock_order"]["plats"]["whatsapp"] == ["66123"]
    finally:
        _restore(mod, origs)
