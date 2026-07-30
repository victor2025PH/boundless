# -*- coding: utf-8 -*-
"""会话列表行级「生效人设」富集（eff_persona）门禁（2026-07-30）。

背景：会话级人设覆写（inbox.persona_conv_override）让一条会话以另一个人设出站，
回复区身份条已由 _identBarUpgrade 校正到 /api/persona/effective 读侧真相，但
**列表行徽章**只读 accountMeta 的账号级绑定 → 覆写会话在列表里显示错人设。
修复＝`_enrich_chat_list` 给命中覆写的行附 `eff_persona{id,name,tier}`，
前端徽章优先消费、字段缺失按账号级回落。

钉住的不变量：
  1. 开关开 + 3 段键绑定命中 + profile 有效 → 该行带 eff_persona（tier=conv_override）；
  2. 未绑定的行**不带**该字段（防「兜底人设」吞掉账号未绑的黄点警示）；
  3. 开关关 → 整块零介入（任何行都不带字段）——kill-switch 语义与出站链一致；
  4. 悬空引用（profile 已删）→ 视同未覆写（与 resolve_effective_persona 同语义）；
  5. scoped 查询路径（platform/account_id）同样被富集——单一注入点覆盖三条响应路径。

fixture 风格与 test_unified_inbox_scoped_chats / test_persona_effective 同源：
自建 FastAPI app + 临时 InboxStore + PersonaManager.reset() 隔离单例。
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.ai.persona_voice import conv_binding_key
from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.utils.persona_manager import PersonaManager
from src.web.routes.unified_inbox_read_routes import register_read_routes

_PROFILES = {
    "personas": {
        "profiles": [
            {"id": "chen_mo", "name": "陈默", "role": "陪伴"},
            {"id": "lin_xiaoyu", "name": "林小雨", "role": "陪伴"},
        ]
    }
}


class _CfgMgr:
    """register_read_routes 只读 .config；最小桩。

    read_from_store=True：主路径（无参数）的数据源是「live 聚合 → A1 灰度开关 →
    store 视图」，本 fixture 无 live 适配器，必须开 store 读才有行可断言。"""

    def __init__(self, enabled: bool):
        self.config = {"inbox": {
            "read_from_store": True,
            "persona_conv_override": {"enabled": enabled},
        }}


@pytest.fixture()
def pm():
    PersonaManager.reset()
    m = PersonaManager.get_instance()
    m.load_profiles_from_config(_PROFILES)
    yield m
    PersonaManager.reset()


def _client(store, cfg_mgr):
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_read_routes(app, api_auth=api_auth, config_manager=cfg_mgr)
    app.state.inbox_store = store
    app.state.config_manager = cfg_mgr   # _read_from_store_enabled 从 app.state 读
    return TestClient(app)


def _seed(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    base = time.time()
    for k in range(3):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:tg-a:{k}",
            platform="telegram", account_id="tg-a", chat_key=str(k),
            display_name=f"客户{k}", last_text=f"hi {k}", last_ts=base - k * 60,
        ))
    return store


def _rows_by_ck(payload):
    return {str(c.get("chat_key")): c for c in payload["chats"]}


def test_override_row_carries_eff_persona(tmp_path, pm):
    pm._chat_bindings[conv_binding_key("telegram", "tg-a", "1")] = "chen_mo"
    store = _seed(tmp_path)
    try:
        client = _client(store, _CfgMgr(enabled=True))
        r = client.get("/api/unified-inbox/chats", params={"limit": 30})
        assert r.status_code == 200
        rows = _rows_by_ck(r.json())
        eff = rows["1"].get("eff_persona")
        assert eff == {"id": "chen_mo", "name": "陈默", "tier": "conv_override"}
        # 未绑定的行不带字段（回落账号级；也别吞掉「账号未绑人设」的警示语义）
        assert "eff_persona" not in rows["0"]
        assert "eff_persona" not in rows["2"]
    finally:
        store.close()


def test_switch_off_is_kill_switch(tmp_path, pm):
    """开关关 = 出站链覆写整体失效，行富集必须同步闭嘴（两处不一致=列表说谎）。"""
    pm._chat_bindings[conv_binding_key("telegram", "tg-a", "1")] = "chen_mo"
    store = _seed(tmp_path)
    try:
        client = _client(store, _CfgMgr(enabled=False))
        rows = _rows_by_ck(client.get(
            "/api/unified-inbox/chats", params={"limit": 30}).json())
        assert all("eff_persona" not in c for c in rows.values())
    finally:
        store.close()


def test_dangling_ref_treated_as_no_override(tmp_path, pm):
    """profile 已删的悬空绑定 → 不附字段（与 resolve_effective_persona 同语义，
    绝不让列表拿到悬空 id 渲染出一个不存在的人设名）。"""
    pm._chat_bindings[conv_binding_key("telegram", "tg-a", "1")] = "ghost_pid"
    store = _seed(tmp_path)
    try:
        client = _client(store, _CfgMgr(enabled=True))
        rows = _rows_by_ck(client.get(
            "/api/unified-inbox/chats", params={"limit": 30}).json())
        assert "eff_persona" not in rows["1"]
    finally:
        store.close()


def test_scoped_path_also_enriched(tmp_path, pm):
    """账号视角（platform/account_id scoped 直查 store）走同一富集口——
    覆写徽章不能「全部对话里有、点进账号视角就没了」。"""
    pm._chat_bindings[conv_binding_key("telegram", "tg-a", "2")] = "lin_xiaoyu"
    store = _seed(tmp_path)
    try:
        client = _client(store, _CfgMgr(enabled=True))
        r = client.get("/api/unified-inbox/chats",
                       params={"platform": "telegram", "account_id": "tg-a"})
        rows = _rows_by_ck(r.json())
        assert rows["2"].get("eff_persona", {}).get("id") == "lin_xiaoyu"
        assert rows["2"]["eff_persona"]["tier"] == "conv_override"
    finally:
        store.close()


def test_no_config_manager_never_breaks_list(tmp_path, pm):
    """config_manager=None（最小挂载）→ 开关按 False 处理，列表照常返回。

    走 scoped 路径（store 直查，不依赖 A1 灰度开关）——config_manager=None 时
    主路径的 live 聚合本就为空，用它断言「不崩」没有区分度。"""
    pm._chat_bindings[conv_binding_key("telegram", "tg-a", "1")] = "chen_mo"
    store = _seed(tmp_path)
    try:
        client = _client(store, None)
        r = client.get("/api/unified-inbox/chats",
                       params={"platform": "telegram", "account_id": "tg-a"})
        assert r.status_code == 200
        assert len(r.json()["chats"]) == 3
        assert all("eff_persona" not in c for c in r.json()["chats"])
    finally:
        store.close()
