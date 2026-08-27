# -*- coding: utf-8 -*-
"""账号资产中心路由门禁（真 InboxStore@tmp / 假注册表 / 台账@tmp，零生产依赖）。

覆盖：账号并集（registry ∪ 仅历史）/ banned 覆盖状态与排序 / 「人的并集」联系人
口径 / reachability 分桶 / 媒体计数与磁盘总量 / 备份扫描 / feature-probe /
台账读数（默认 kind 集过滤）/ 角色闸（agent 页面 302、API 403）/ TTL 缓存 /
模板 Jinja 可编译。
"""
from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import src.integrations.account_registry as areg
import src.integrations.protocol_bridge as pb
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.ops import ops_events as oe
from src.web.routes import asset_center_routes as acr
from src.web.routes.asset_center_routes import register_asset_center_routes


# ── 桩件 ─────────────────────────────────────────────────────────────────────

class _FakeRegistry:
    def __init__(self, rows):
        self._rows = rows

    def list(self, include_removed=False):
        if include_removed:
            return list(self._rows)
        return [r for r in self._rows if r.get("status") != "removed"]


class _FakeTemplates:
    def TemplateResponse(self, request, name, ctx=None):
        return HTMLResponse("tpl:" + str(name))


def _conv(cid, plat, acct, ck, *, username="", phone="", chat_type="private",
          last_ts=0.0):
    return InboxConversation(
        conversation_id=cid, platform=plat, account_id=acct, chat_key=ck,
        display_name=ck, username=username, phone=phone, chat_type=chat_type,
        last_ts=last_ts, last_text="hi")


def _msg(cid, pmid, *, media_ref="", ts=1.0):
    return InboxMessage(conversation_id=cid, platform_msg_id=pmid,
                        text="t", ts=ts, media_ref=media_ref,
                        media_type=("photo" if media_ref else ""))


def _seed_store(tmp_path) -> InboxStore:
    store = InboxStore(tmp_path / "inbox.db")
    # alive：私聊×2（一条双句柄、一条无句柄）+ 群聊×1（不进 reachability/联系人并集）
    store.ingest_batch(
        _conv("telegram:alive:u1", "telegram", "alive", "u1",
              username="ann", phone="+15550001", last_ts=100),
        [_msg("telegram:alive:u1", "m1",
              media_ref="/static/protocol_media/telegram/a.jpg", ts=99),
         _msg("telegram:alive:u1", "m2", ts=100)])
    store.ingest_batch(
        _conv("telegram:alive:u2", "telegram", "alive", "u2", last_ts=90), [])
    store.ingest_batch(
        _conv("telegram:alive:g1", "telegram", "alive", "g1",
              chat_type="group", last_ts=95), [])
    # dead（registry 标 banned）：私聊×1 仅用户名
    store.ingest_batch(
        _conv("telegram:dead:d1", "telegram", "dead", "d1",
              username="bob", last_ts=80),
        [_msg("telegram:dead:d1", "m3", media_ref="x.jpg", ts=80)])
    # ghost：只在会话目录（不在 registry）→ history_only
    store.ingest_batch(
        _conv("telegram:ghost:g9", "telegram", "ghost", "g9", last_ts=70), [])
    return store


_REG_ROWS = [
    {"platform": "telegram", "account_id": "alive", "label": "主号",
     "mode": "protocol", "status": "online", "meta": {},
     "last_online_at": 123.0},
    {"platform": "telegram", "account_id": "dead", "label": "老号",
     "mode": "protocol", "status": "offline",
     "meta": {"banned": True, "ban_reason": "peer_flood"},
     "last_online_at": 50.0},
]


def _mk_app(store) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = store

    @app.get("/_test/login/{role}")
    async def _login(request: Request, role: str):
        request.session["role"] = role
        request.session["username"] = "t-" + role
        return {"ok": True}

    register_asset_center_routes(
        app, page_auth=lambda r: None, api_auth=lambda r: None,
        templates=_FakeTemplates())
    return app


@pytest.fixture()
def env(monkeypatch, tmp_path):
    store = _seed_store(tmp_path)
    monkeypatch.setattr(areg, "get_account_registry",
                        lambda: _FakeRegistry(_REG_ROWS))
    # 媒体磁盘根 → tmp（主根 2 文件 / 旧根 1 文件；求和口径）
    main_root = tmp_path / "media_main"
    (main_root / "telegram").mkdir(parents=True)
    (main_root / "telegram" / "a.jpg").write_bytes(b"aaa")
    (main_root / "telegram" / "b.jpg").write_bytes(b"bbbbb")
    legacy_root = tmp_path / "media_legacy"
    (legacy_root / "telegram").mkdir(parents=True)
    (legacy_root / "telegram" / "c.jpg").write_bytes(b"cc")
    monkeypatch.setattr(pb, "protocol_media_root", lambda: main_root)
    monkeypatch.setattr(pb, "legacy_protocol_media_root", lambda: legacy_root)
    # 备份目录：数据根契约 = AITR_DATA_DIR（清掉更高优先的 AITR_CONFIG_PATH）
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    data_root = tmp_path / "inst" / "data"
    data_root.mkdir(parents=True)
    monkeypatch.setenv("AITR_DATA_DIR", str(data_root))
    bdir = tmp_path / "inst" / "backups"
    bdir.mkdir(parents=True)
    with zipfile.ZipFile(bdir / "instance-backup-t-20260819-000000.zip",
                         "w") as zf:
        zf.writestr("x.txt", "y")
    # 台账 singleton → tmp
    oe.reset_ops_event_store()
    evs = oe.get_ops_event_store(str(tmp_path / "ops_events.db"))
    evs.record("account_export", account_id="dead", platform="telegram",
               reason="ok", detail="convs=1;msgs=1")
    evs.record("profile_push", account_id="alive", platform="telegram",
               reason="ok", detail="unrelated")
    evs.record("account_purge", account_id="ghost", platform="telegram",
               reason="db_error", detail="rows=0")
    # 汇总缓存复位（模块级）
    acr._summary_cache["ts"] = 0.0
    acr._summary_cache["payload"] = None

    client = TestClient(_mk_app(store))
    yield client, store
    oe.reset_ops_event_store()
    try:
        store.close()
    except Exception:
        pass


# ── 用例 ─────────────────────────────────────────────────────────────────────

def test_summary_union_status_counts(env):
    client, _ = env
    r = client.get("/api/workspace/assets/summary")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["cached"] is False
    by = {a["account_id"]: a for a in d["accounts"]}
    assert set(by) == {"alive", "dead", "ghost"}
    # banned 覆盖 registry status 且排最前
    assert d["accounts"][0]["account_id"] == "dead"
    assert by["dead"]["status"] == "banned"
    assert by["dead"]["ban_reason"] == "peer_flood"
    assert by["alive"]["status"] == "online"
    assert by["alive"]["label"] == "主号"
    # 仅历史账号（registry 之外）
    assert by["ghost"]["status"] == "history_only"
    # 计数：count_account_data 口径（含群聊会话）
    assert by["alive"]["counts"]["conversations"] == 3
    assert by["alive"]["counts"]["messages"] == 2
    assert by["dead"]["counts"]["conversations"] == 1
    assert by["dead"]["counts"]["messages"] == 1
    assert by["ghost"]["counts"]["messages"] == 0
    # 联系人 = 「人的并集」（通讯录 ∪ 私聊 peer；群聊不入）
    assert by["alive"]["contacts_detail"]["total"] == 2
    assert by["dead"]["contacts_detail"]["total"] == 1
    assert by["ghost"]["contacts_detail"]["total"] == 1
    # 媒体计数（media_ref 非空的消息）
    assert by["alive"]["counts"]["media_files"] == 1
    assert by["dead"]["counts"]["media_files"] == 1


def test_summary_reachability_buckets(env):
    client, _ = env
    d = client.get("/api/workspace/assets/summary").json()
    by = {a["account_id"]: a for a in d["accounts"]}
    ra = by["alive"]["reachability"]
    assert ra["both"] == 1 and ra["none"] == 1
    assert ra["username_only"] == 0 and ra["phone_only"] == 0
    assert ra["total"] == 2 and ra["covered"] == 1   # 群聊不计
    rd = by["dead"]["reachability"]
    assert rd["username_only"] == 1 and rd["total"] == 1 and rd["covered"] == 1
    rg = by["ghost"]["reachability"]
    assert rg["none"] == 1 and rg["covered"] == 0


def test_summary_totals_backup_features(env):
    client, _ = env
    d = client.get("/api/workspace/assets/summary").json()
    t = d["totals"]
    assert t["accounts"] == 3 and t["banned"] == 1
    assert t["conversations"] == 5 and t["messages"] == 3
    assert t["contacts"] == 4
    # 磁盘媒体总量 = 新根 2 文件 + 旧根 1 文件
    assert t["media_files"] == 3 and t["media_bytes"] == 10
    # 备份扫描（<数据根>.parent/backups 最新 zip）
    assert d["backup"] is not None
    assert d["backup"]["file"].startswith("instance-backup-")
    assert d["backup"]["bytes"] > 0
    # 本测试 app 未挂兄弟线端点 → 两个 feature 都 False
    assert d["features"] == {"export_migration": False, "reconnect": False}


def test_summary_ttl_cache_and_force(env):
    client, _ = env
    assert client.get("/api/workspace/assets/summary").json()["cached"] is False
    assert client.get("/api/workspace/assets/summary").json()["cached"] is True
    assert client.get(
        "/api/workspace/assets/summary?force=1").json()["cached"] is False


def test_feature_probe_detects_sibling_routes(env):
    _, store = env
    app2 = _mk_app(store)

    @app2.get("/api/accounts/{platform}/{account_id}/export-migration")
    async def _mig(platform: str, account_id: str):
        return {}

    @app2.get("/api/admin/asset/reconnect/candidates")
    async def _cand():
        return {}

    acr._summary_cache["ts"] = 0.0
    acr._summary_cache["payload"] = None
    d = TestClient(app2).get("/api/workspace/assets/summary").json()
    assert d["features"] == {"export_migration": True, "reconnect": True}


def test_ledger_filters_to_asset_kinds(env):
    client, _ = env
    r = client.get("/api/workspace/assets/ledger")
    assert r.status_code == 200
    rows = r.json()["rows"]
    kinds = [x["kind"] for x in rows]
    # 默认 kind 集只收资产家族：profile_push（无关 kind）不得混入
    assert kinds == ["account_purge", "account_export"]   # 新→旧
    assert rows[0]["reason"] == "db_error"
    # kinds 参数可覆写
    only = client.get(
        "/api/workspace/assets/ledger?kinds=account_export").json()["rows"]
    assert [x["kind"] for x in only] == ["account_export"]


def test_role_gates_page_and_api(env):
    client, _ = env
    # agent：页面 302 回工作台、API 403
    client.get("/_test/login/agent")
    pg = client.get("/workspace/assets", follow_redirects=False)
    assert pg.status_code == 302 and pg.headers["location"] == "/workspace"
    assert client.get("/api/workspace/assets/summary").status_code == 403
    assert client.get("/api/workspace/assets/ledger").status_code == 403
    # supervisor：页面渲染 + API 放行
    client.get("/_test/login/supervisor")
    pg2 = client.get("/workspace/assets")
    assert pg2.status_code == 200 and "tpl:workspace_assets.html" in pg2.text
    assert client.get("/api/workspace/assets/summary").status_code == 200


def test_store_missing_returns_503(env, tmp_path):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = None
    register_asset_center_routes(
        app, page_auth=lambda r: None, api_auth=lambda r: None,
        templates=_FakeTemplates())
    acr._summary_cache["ts"] = 0.0
    acr._summary_cache["payload"] = None
    assert TestClient(app).get(
        "/api/workspace/assets/summary").status_code == 503


def test_template_compiles():
    """Jinja 语法门禁：路由 .py 等 22:30 重启窗，模板语法错误不该等到那时才炸。"""
    from jinja2 import Environment, FileSystemLoader
    tdir = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(tdir)))
    env.get_template("workspace_assets.html")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
