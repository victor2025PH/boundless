# -*- coding: utf-8 -*-
"""外观个性化（appearance）主线门禁：净化白名单 / 店内漫游存取 / prefs 路由局部更新语义。

覆盖三层：
1. sanitize_appearance 纯函数——白名单、值域夹紧、坏输入拒绝（这是防注入唯一闸门）；
2. InboxStore.set_agent_appearance 往返——只动 appearance 列，不碰告警偏好；
3. /api/workspace/prefs POST 的局部更新语义（slice-14 同款假 app 直调 handler）——
   **只带 appearance 的 POST 绝不清零告警偏好**（修复前旧语义会整条覆盖成默认值，
   这是本批附带的兼容性修复，此处钉住防回退）。
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
)


# ---------------------------------------------------------------------------
# 1) sanitize_appearance 纯函数
# ---------------------------------------------------------------------------

def test_sanitize_valid_full_payload_roundtrip():
    ap = sanitize_appearance({
        "v": 1, "theme": "aurora", "out": "#0F766E", "in": "#ecf7f1",
        "wall": "mist", "radius": 12, "fs": 15,
        "night": {"mode": "schedule", "start": 1320, "end": 480},
        "anim": False, "ts": 1754200000000,
    })
    assert ap is not None
    assert ap["theme"] == "aurora"
    assert ap["out"] == "#0F766E" and ap["in"] == "#ecf7f1"
    assert ap["wall"] == "mist"
    assert ap["radius"] == 12 and ap["fs"] == 15
    assert ap["night"] == {"mode": "schedule", "start": 1320, "end": 480}
    assert ap["anim"] is False and ap["ts"] == 1754200000000
    # 序列化产物可再被 loads 解析（API 回显口径）
    assert loads_appearance(dumps_appearance(ap))["theme"] == "aurora"


@pytest.mark.parametrize("bad", [None, 42, "x", [], ["a"], True])
def test_sanitize_non_dict_rejected(bad):
    assert sanitize_appearance(bad) is None


def test_sanitize_clamps_and_whitelists():
    ap = sanitize_appearance({
        "theme": "hacker-theme", "out": "url(javascript:1)", "in": "#12345",
        "wall": "../etc", "radius": 999, "fs": 99,
        "night": {"mode": "blackout", "start": -5, "end": 99999},
        "anim": 1, "ts": "not-a-number", "evil_key": "dropped",
    })
    assert ap is not None
    assert ap["theme"] == "brand"          # 未知主题回默认
    assert ap["out"] == "" and ap["in"] == ""   # 非法颜色清空（回落主题默认色）
    assert ap["wall"] == "flat"
    assert ap["radius"] == 16              # 非档位值回默认
    assert ap["fs"] == 16                  # 夹紧到上限
    assert ap["night"]["mode"] == "auto"
    assert ap["night"]["start"] == 1320 and ap["night"]["end"] == 480
    assert ap["anim"] is True
    assert ap["ts"] == 0
    assert "evil_key" not in ap            # 白名单：未知键不落库


def test_loads_appearance_bad_blob_falls_back_empty():
    assert loads_appearance("") == {}
    assert loads_appearance(None) == {}
    assert loads_appearance("{broken json") == {}
    assert loads_appearance('"a string"') == {}


# ---------------------------------------------------------------------------
# 2) InboxStore.set_agent_appearance 往返
# ---------------------------------------------------------------------------

def test_store_appearance_roundtrip_and_isolation(tmp_path):
    from src.inbox.store import InboxStore
    store = InboxStore(tmp_path / "inbox.db")
    # 默认：无行时回空串（前端回落内置默认主题）
    assert store.get_agent_prefs("a1")["appearance"] == ""
    # 先写告警偏好，再写外观——两者互不覆盖
    store.set_agent_prefs("a1", warn_sec=120, crit_sec=300, muted=0,
                          dnd_start=-1, dnd_end=-1)
    blob = dumps_appearance(sanitize_appearance({"theme": "sunset", "fs": 14}))
    prefs = store.set_agent_appearance("a1", blob)
    assert json.loads(prefs["appearance"])["theme"] == "sunset"
    assert prefs["warn_sec"] == 120 and prefs["crit_sec"] == 300
    # 反向：改告警偏好不动外观
    prefs2 = store.set_agent_prefs("a1", warn_sec=60, crit_sec=300, muted=0,
                                   dnd_start=-1, dnd_end=-1)
    assert json.loads(prefs2["appearance"])["theme"] == "sunset"
    assert prefs2["warn_sec"] == 60


# ---------------------------------------------------------------------------
# 3) POST /api/workspace/prefs 局部更新语义（假 app 直调 handler）
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
    def __init__(self, body):
        self._body = body
        self.state = SimpleNamespace(ui_lang="zh")

    async def json(self):
        return self._body


def _route_env(tmp_path):
    """把 prefs 路由挂到假 app 上，store 用真 InboxStore（route→store 集成口径）。"""
    from src.inbox.store import InboxStore
    from src.web.routes import unified_inbox_workspace_prefs_routes as mod
    store = InboxStore(tmp_path / "inbox.db")
    app = _App()
    orig_inbox, orig_agent = mod._inbox_store, mod._session_agent
    mod._inbox_store = lambda request: store
    mod._session_agent = lambda request: {"agent_id": "a1", "display_name": "A"}
    mod.register_workspace_prefs_routes(app, api_auth=lambda request: None)
    return mod, app, store, (orig_inbox, orig_agent)


def _restore(mod, origs):
    mod._inbox_store, mod._session_agent = origs


def test_post_appearance_only_does_not_clobber_alert_prefs(tmp_path):
    mod, app, store, origs = _route_env(tmp_path)
    try:
        store.set_agent_prefs("a1", warn_sec=120, crit_sec=600, muted=1,
                              dnd_start=60, dnd_end=480)
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        out = asyncio.run(fn(_Req({"appearance": {"theme": "graphite", "radius": 12}})))
        assert out["ok"] is True
        # 外观已存 + 回显为解析后的 dict
        assert out["prefs"]["appearance"]["theme"] == "graphite"
        assert out["prefs"]["appearance"]["radius"] == 12
        # 告警偏好一个都不能少（修复前旧语义会整条覆盖成 0/-1）
        assert out["prefs"]["warn_sec"] == 120
        assert out["prefs"]["crit_sec"] == 600
        assert out["prefs"]["muted"] == 1
        assert out["prefs"]["dnd_start"] == 60 and out["prefs"]["dnd_end"] == 480
    finally:
        _restore(mod, origs)


def test_post_alert_keys_still_write_and_keep_appearance(tmp_path):
    mod, app, store, origs = _route_env(tmp_path)
    try:
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        asyncio.run(fn(_Req({"appearance": {"theme": "sakura"}})))
        out = asyncio.run(fn(_Req({"warn_sec": 90, "crit_sec": 0, "muted": 0,
                                   "dnd_start": -1, "dnd_end": -1})))
        assert out["prefs"]["warn_sec"] == 90
        assert out["prefs"]["appearance"]["theme"] == "sakura"  # 外观不被告警写入抹掉
    finally:
        _restore(mod, origs)


def test_post_invalid_appearance_rejected_400(tmp_path):
    from fastapi import HTTPException
    mod, app, store, origs = _route_env(tmp_path)
    try:
        fn = app.handlers[("POST", "/api/workspace/prefs")]
        with pytest.raises(HTTPException) as ei:
            asyncio.run(fn(_Req({"appearance": ["not", "a", "dict"]})))
        assert ei.value.status_code == 400
        # 拒绝即不落库
        assert store.get_agent_prefs("a1")["appearance"] == ""
    finally:
        _restore(mod, origs)
