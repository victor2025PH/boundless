# -*- coding: utf-8 -*-
"""迁移包导出路由门禁（真 InboxStore@tmp / 真 zip 下载 / 台账@tmp）。

覆盖：角色闸（agent/viewer 403）、store 缺席 503、预览与真导出**同一取数口径**、
zip 可解且 manifest 带注册表事实（label/ban）、`?media=1` 透传、审计事件字段、
临时包下载后被清理、以及 deny 集与账号写口的跨模块契约。
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import src.integrations.account_registry as areg
import src.integrations.protocol_bridge as pb
from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.ops import ops_events as oe
from src.web.routes.migration_export_routes import (
    _MANAGE_DENY_ROLES,
    register_migration_export_routes,
)

PLAT, ACCT = "telegram", "acct1"


class _FakeRegistry:
    def __init__(self, row):
        self._row = row

    def get(self, platform, account_id):
        if (platform, account_id) == (PLAT, ACCT):
            return self._row
        return None


def _conv(cid, ck, *, username="", phone="", name="", last_ts=0.0):
    return InboxConversation(
        conversation_id=cid, platform=PLAT, account_id=ACCT, chat_key=ck,
        display_name=name or ck, username=username, phone=phone,
        chat_type="private", last_ts=last_ts, last_text="hi")


def _msg(cid, pmid, *, media_ref="", ts=1.0):
    return InboxMessage(conversation_id=cid, platform_msg_id=pmid, text="t",
                        ts=ts, media_ref=media_ref,
                        media_type=("photo" if media_ref else ""))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    store = InboxStore(tmp_path / "inbox.db")
    store.ingest_batch(
        _conv(f"{PLAT}:{ACCT}:u1", "u1", username="ann", phone="+1",
              name="Ann", last_ts=200),
        [_msg(f"{PLAT}:{ACCT}:u1", "m1",
              media_ref="/static/protocol_media/telegram/a.jpg", ts=190)])
    store.ingest_batch(_conv(f"{PLAT}:{ACCT}:u2", "u2", name="Bob",
                             last_ts=100), [])

    monkeypatch.setattr(areg, "get_account_registry", lambda: _FakeRegistry({
        "platform": PLAT, "account_id": ACCT, "label": "主号",
        "status": "offline",
        "meta": {"banned": True, "ban_reason": "peer_flood",
                 "banned_at": 1700.0},
    }))
    main = tmp_path / "media_main"
    (main / "telegram").mkdir(parents=True)
    (main / "telegram" / "a.jpg").write_bytes(b"JPEG-A")
    legacy = tmp_path / "media_legacy"
    legacy.mkdir(parents=True)
    monkeypatch.setattr(pb, "protocol_media_root", lambda: main)
    monkeypatch.setattr(pb, "legacy_protocol_media_root", lambda: legacy)

    oe.reset_ops_event_store()
    oe.get_ops_event_store(str(tmp_path / "ops_events.db"))

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = store

    @app.get("/_test/login/{role}")
    async def _login(request: Request, role: str):
        request.session["role"] = role
        request.session["username"] = "t-" + role
        return {"ok": True}

    register_migration_export_routes(app, api_auth=lambda r: None)
    client = TestClient(app)
    yield client, store, tmp_path
    oe.reset_ops_event_store()
    try:
        store.close()
    except Exception:
        pass


def _download_zip(client, tmp_path, query=""):
    r = client.get(
        f"/api/accounts/{PLAT}/{ACCT}/export-migration{query}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    out = tmp_path / "dl.zip"
    out.write_bytes(r.content)
    return r, zipfile.ZipFile(out, "r")


# ── 权限 / 前置 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("role", sorted(_MANAGE_DENY_ROLES))
def test_low_privilege_roles_denied(env, role):
    client, _, _ = env
    client.get(f"/_test/login/{role}")
    assert client.get(
        f"/api/accounts/{PLAT}/{ACCT}/export-migration").status_code == 403
    assert client.get(
        f"/api/accounts/{PLAT}/{ACCT}/migration-preview").status_code == 403


def test_supervisor_allowed(env, tmp_path):
    client, _, _ = env
    client.get("/_test/login/supervisor")
    assert client.get(
        f"/api/accounts/{PLAT}/{ACCT}/migration-preview").status_code == 200


def test_store_missing_returns_503(tmp_path):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.inbox_store = None
    register_migration_export_routes(app, api_auth=lambda r: None)
    c = TestClient(app)
    assert c.get(
        f"/api/accounts/{PLAT}/{ACCT}/export-migration").status_code == 503
    assert c.get(
        f"/api/accounts/{PLAT}/{ACCT}/migration-preview").status_code == 503


# ── 预览 ─────────────────────────────────────────────────────────────────────

def test_preview_shape_and_registry_facts(env):
    client, _, _ = env
    d = client.get(f"/api/accounts/{PLAT}/{ACCT}/migration-preview").json()
    assert d["ok"] is True
    assert d["conversations"] == 2 and d["messages"] == 1
    assert d["contacts"] == 2
    assert d["reachability"] == {"both": 1, "username": 0, "phone": 0,
                                 "none": 1, "total": 2, "covered": 1}
    assert d["label"] == "主号"
    assert d["ban"]["banned"] is True and d["ban"]["reason"] == "peer_flood"


def test_preview_matches_real_export(env, tmp_path):
    """预览与真导出必须同口径——预览说 1 人可加回、导出出来 0 人比没有预览更糟。"""
    client, _, _ = env
    pv = client.get(f"/api/accounts/{PLAT}/{ACCT}/migration-preview").json()
    _, zf = _download_zip(client, tmp_path)
    with zf:
        man = json.loads(zf.read("manifest.json"))
    assert man["reachability"] == pv["reachability"]
    assert man["counts"]["contacts"] == pv["contacts"]
    assert man["counts"]["conversations"] == pv["conversations"]
    assert man["counts"]["messages"] == pv["messages"]


def test_preview_unknown_account_is_empty_not_error(env):
    client, _, _ = env
    d = client.get("/api/accounts/telegram/nope/migration-preview").json()
    assert d["ok"] is True and d["contacts"] == 0
    assert d["label"] == "" and d["ban"] == {}


# ── 导出 ─────────────────────────────────────────────────────────────────────

def test_export_downloads_valid_kit(env, tmp_path):
    client, _, _ = env
    r, zf = _download_zip(client, tmp_path)
    with zf:
        names = set(zf.namelist())
        assert {"manifest.json", "contacts.jsonl", "contacts.csv",
                "conversations.jsonl", "media_index.jsonl",
                "README.txt"} <= names
        man = json.loads(zf.read("manifest.json"))
    # 文件名进 Content-Disposition（浏览器直存即得可辨识的包）
    cd = r.headers.get("content-disposition", "")
    assert "chatx-migration_telegram_acct1_" in cd and cd.endswith('.zip"')
    # 注册表事实进 manifest（「这个包是在什么处境下导出的」属交付内容）
    assert man["label"] == "主号"
    assert man["ban"] == {"banned": True, "reason": "peer_flood",
                          "banned_at": 1700.0}
    assert man["media"]["included"] is False


def test_export_media_flag_passthrough(env, tmp_path):
    client, _, _ = env
    _, zf = _download_zip(client, tmp_path, "?media=1")
    with zf:
        assert zf.read("media/telegram/a.jpg") == b"JPEG-A"
        man = json.loads(zf.read("manifest.json"))
    assert man["media"]["included"] is True
    assert man["counts"]["media_files"] == 1


def test_export_temp_file_cleaned_up(env, tmp_path):
    """下载完临时包必须消失（一次性产物，不许在磁盘上堆积）。"""
    client, _, _ = env
    _download_zip(client, tmp_path)
    leftovers = list((tmp_path / "tmp_migration").glob("*.zip")) \
        if (tmp_path / "tmp_migration").is_dir() else []
    assert leftovers == [], f"临时包未清理: {leftovers}"


def test_export_writes_audit_row(env, tmp_path):
    client, _, _ = env
    _download_zip(client, tmp_path)
    rows = oe.get_ops_event_store().recent_kinds(
        ["account_export_migration"], limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["platform"] == PLAT and row["account_id"] == ACCT
    assert row["reason"] == "ok"
    d = row["detail"]
    assert "convs=2" in d and "msgs=1" in d and "contacts=2" in d
    assert "reach=1/2" in d and "reconciled=1" in d
    # 审计只记数字与操作者，绝不含联系人内容
    assert "Ann" not in d and "ann" not in d and "+1" not in d


def test_export_audit_absent_when_denied(env, tmp_path):
    client, _, _ = env
    client.get("/_test/login/agent")
    client.get(f"/api/accounts/{PLAT}/{ACCT}/export-migration")
    assert oe.get_ops_event_store().recent_kinds(
        ["account_export_migration"], limit=5) == []


# ── 跨模块契约 ───────────────────────────────────────────────────────────────

def test_deny_roles_match_account_routes():
    """本模块的 deny 集必须与账号写口的 `_ACCOUNT_MANAGE_DENY_ROLES` 一致。

    两处刻意各自持有 5 行守卫（不 import 5900 行路由模块），靠这条静态比对钉住：
    谁放宽了一侧，这里就红。资产中心那一份由它自己的门禁负责。
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "routes"
           / "unified_inbox_account_routes.py").read_text(encoding="utf-8")
    m = re.search(r"_ALERT_CFG_DENY_ROLES\s*=\s*\{([^}]*)\}", src)
    assert m, "未能在账号路由里定位 deny 集定义"
    theirs = {x.strip().strip('"\'') for x in m.group(1).split(",")
              if x.strip()}
    assert theirs == _MANAGE_DENY_ROLES


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
