# -*- coding: utf-8 -*-
"""坐席「个人常用语」门禁（P1 2026-08-18，cp-kb「我的常用语」层）。

存储＝app_settings KV ``inbox.quick_replies.{agent_id}``（JSON 列表）；
写入口唯一＝POST /api/unified-inbox/quick-replies（add/update/delete 多路复用）；
读随 GET /api/unified-inbox/templates 一并下发（source="mine"，排最前）。

钉住的不变量：
① 服务端守卫（上限 20 / 单条 ≤500 字 / 按文本去重 / 空文本拒）——面板是薄壳；
② 坏 KV 数据整体回空绝不半解析；新增排最前（刚存的马上要用）；
③ 个人层缺席（inbox 未挂载）绝不影响团队话术下发（merge 软失败回空）。
"""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_workspace_prefs_routes import (
    QUICK_REPLIES_MAX_ITEMS,
    QUICK_REPLY_MAX_CHARS,
    mutate_quick_replies,
    parse_quick_replies,
    register_workspace_prefs_routes,
)


# ── 纯函数：解析 ─────────────────────────────────────────────────────────


def test_parse_bad_json_returns_empty():
    assert parse_quick_replies("not json") == []
    assert parse_quick_replies('{"a": 1}') == []
    assert parse_quick_replies(None) == []
    assert parse_quick_replies("") == []


def test_parse_dedupes_and_caps_and_skips_bad_rows():
    rows = ([{"text": "重复的"}] * 3
            + [{"text": ""}, {"nope": 1}, "str-row",
               {"text": "x" * (QUICK_REPLY_MAX_CHARS + 1)}]
            + [{"text": f"第{i}条"} for i in range(QUICK_REPLIES_MAX_ITEMS + 5)])
    out = parse_quick_replies(json.dumps(rows, ensure_ascii=False))
    texts = [it["text"] for it in out]
    assert texts.count("重复的") == 1
    assert len(out) == QUICK_REPLIES_MAX_ITEMS
    assert all(it["id"] for it in out)


# ── 纯函数：状态机 ───────────────────────────────────────────────────────


def test_mutate_add_prepends_and_guards():
    items, err = mutate_quick_replies([], "add", text="  你好呀  ")
    assert err == "" and items[0]["text"] == "你好呀"
    items2, err = mutate_quick_replies(items, "add", text="第二条")
    assert err == "" and [it["text"] for it in items2] == ["第二条", "你好呀"]
    _, err = mutate_quick_replies(items2, "add", text="你好呀")
    assert err == "duplicate"
    _, err = mutate_quick_replies(items2, "add", text="")
    assert err == "empty_text"
    _, err = mutate_quick_replies(items2, "add", text="x" * (QUICK_REPLY_MAX_CHARS + 1))
    assert err == "too_long"


def test_mutate_add_full():
    items = [{"id": f"i{i}", "text": f"第{i}条"} for i in range(QUICK_REPLIES_MAX_ITEMS)]
    _, err = mutate_quick_replies(items, "add", text="再来一条")
    assert err == "full"


def test_mutate_update_and_delete():
    items, _ = mutate_quick_replies([], "add", text="原文")
    iid = items[0]["id"]
    items2, err = mutate_quick_replies(items, "update", text="改后", item_id=iid)
    assert err == "" and items2[0]["text"] == "改后"
    assert items2[0]["id"] != iid  # id=文本指纹，改文即换 id
    _, err = mutate_quick_replies(items2, "update", text="x", item_id="missing")
    assert err == "not_found"
    items3, err = mutate_quick_replies(items2, "delete", item_id=items2[0]["id"])
    assert err == "" and items3 == []
    _, err = mutate_quick_replies(items3, "delete", item_id="missing")
    assert err == "not_found"
    _, err = mutate_quick_replies(items3, "nope", text="x")
    assert err == "bad_action"


def test_mutate_update_duplicate_against_others_only():
    items, _ = mutate_quick_replies([], "add", text="甲")
    items, _ = mutate_quick_replies(items, "add", text="乙")
    # 改成与别的条目相同 → duplicate；「改回自己原文」不算重复
    _, err = mutate_quick_replies(items, "update", text="甲", item_id=items[0]["id"])
    assert err == "duplicate"
    same, err = mutate_quick_replies(items, "update", text="乙", item_id=items[0]["id"])
    assert err == "" and same[0]["text"] == "乙"


# ── 路由 + 与模板端点合并 ────────────────────────────────────────────────


class _FakeInbox:
    def __init__(self):
        self.kv = {}

    def get_app_setting(self, key):
        return self.kv.get(key)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True


class _Cfg:
    def __init__(self, cfg=None):
        self.config = cfg or {}

    def get_dynamic_templates_config(self):
        return self.config.get("templates_dyn") or {}


def _app(inbox=None, cfg=None):
    from src.web.routes.unified_inbox_aux_read_routes import register_aux_read_routes

    app = FastAPI()
    register_workspace_prefs_routes(app, api_auth=lambda request: None)
    register_aux_read_routes(app, api_auth=lambda request: None,
                             config_manager=cfg or _Cfg())
    if inbox is not None:
        app.state.inbox_store = inbox
    return TestClient(app)


def test_route_add_update_delete_roundtrip():
    inbox = _FakeInbox()
    c = _app(inbox)
    d = c.post("/api/unified-inbox/quick-replies",
               json={"action": "add", "text": "亲，稍等我查一下"}).json()
    assert d["ok"] is True and len(d["items"]) == 1
    iid = d["items"][0]["id"]
    # KV 落盘（无 SessionMiddleware → 共享身份 "agent"）
    assert "inbox.quick_replies.agent" in inbox.kv
    d = c.post("/api/unified-inbox/quick-replies",
               json={"action": "add", "text": "亲，稍等我查一下"}).json()
    assert d["ok"] is False and d["error"] == "duplicate"
    d = c.post("/api/unified-inbox/quick-replies",
               json={"action": "update", "id": iid, "text": "马上给您答复～"}).json()
    assert d["ok"] is True and d["items"][0]["text"] == "马上给您答复～"
    d = c.post("/api/unified-inbox/quick-replies",
               json={"action": "delete", "id": d["items"][0]["id"]}).json()
    assert d["ok"] is True and d["items"] == []


def test_route_without_inbox_store_soft_fails():
    c = _app(inbox=None)
    d = c.post("/api/unified-inbox/quick-replies",
               json={"action": "add", "text": "x"}).json()
    assert d["ok"] is False and d["error"] == "inbox_unavailable"


def test_templates_endpoint_merges_mine_first():
    inbox = _FakeInbox()
    cfg = _Cfg({"templates_dyn": {"greeting": ["你好呀"]}})
    c = _app(inbox, cfg)
    c.post("/api/unified-inbox/quick-replies",
           json={"action": "add", "text": "我的专属话术，很顺手"})
    d = c.get("/api/unified-inbox/templates").json()
    rows = d["templates"]
    assert rows, "个人常用语 + 团队话术都应下发"
    assert rows[0]["source"] == "mine"
    assert rows[0]["category"] == "mine"
    assert rows[0]["id"]
    assert rows[0]["label"] == "我的专属话术"  # 首句截断当显示名
    assert any(r["source"] == "templates" for r in rows)


def test_templates_endpoint_mine_absent_without_store():
    cfg = _Cfg({"templates_dyn": {"greeting": ["你好呀"]}})
    c = _app(inbox=None, cfg=cfg)
    d = c.get("/api/unified-inbox/templates").json()
    assert d["ok"] is True
    assert all(r["source"] != "mine" for r in d["templates"])
