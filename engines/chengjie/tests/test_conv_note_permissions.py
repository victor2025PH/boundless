"""P0-协作闭环：内部注解「编辑/删除」权限闸（仅作者或主管）。

此前任何登录坐席可改删任何人的注解（store 层按 note_id 全库删、API 层不校验作者），
且 note_id 与路径 conversation_id 不做归属绑定。本组测试钉住：
  - 作者可编辑/删除自己的注解；
  - 非作者且非主管 → 403（err.ws.note_forbidden）；
  - 主管（master/admin）可代管他人注解；
  - note 不属于路径里的 conversation → 404（跨会话越权按不存在处理）；
  - 身份口径与写入侧一致（session.username——登录从不写 user_name 键）。
"""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_collab_mention_routes import (
    register_collab_mention_routes,
)


def _build(tmp_path):
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")

    def api_auth(request: Request):
        return True

    register_collab_mention_routes(app, api_auth=api_auth, config_manager=None)

    # 测试专用：写 session 身份（与生产登录同键：username/role；display_name 可选）
    @app.post("/__login")
    async def _login(request: Request):
        body = await request.json()
        request.session.clear()
        for k in ("username", "role", "display_name", "user_id"):
            if k in body:
                request.session[k] = body[k]
        return {"ok": True}

    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return TestClient(app), store


def _login_as(client, username, role=""):
    r = client.post("/__login", json={"username": username, "role": role})
    assert r.status_code == 200


def test_author_can_edit_and_delete_own_note(tmp_path):
    client, store = _build(tmp_path)
    note = store.add_conv_note("conv-1", "hello", agent_id="alice", agent_name="Alice")
    _login_as(client, "alice")

    r = client.patch(
        f"/api/workspace/conv/conv-1/notes/{note['note_id']}",
        json={"body": "hello v2"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.list_conv_notes("conv-1")[0]["body"] == "hello v2"

    r = client.delete(f"/api/workspace/conv/conv-1/notes/{note['note_id']}")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.list_conv_notes("conv-1") == []


def test_non_author_agent_gets_403(tmp_path):
    client, store = _build(tmp_path)
    note = store.add_conv_note("conv-1", "hello", agent_id="alice", agent_name="Alice")
    _login_as(client, "bob", role="agent")

    r = client.patch(
        f"/api/workspace/conv/conv-1/notes/{note['note_id']}",
        json={"body": "hijack"},
    )
    assert r.status_code == 403

    r = client.delete(f"/api/workspace/conv/conv-1/notes/{note['note_id']}")
    assert r.status_code == 403
    # 原文未被改动、注解仍在
    assert store.list_conv_notes("conv-1")[0]["body"] == "hello"


def test_supervisor_can_manage_others_notes(tmp_path):
    client, store = _build(tmp_path)
    n1 = store.add_conv_note("conv-1", "a", agent_id="alice", agent_name="Alice")
    n2 = store.add_conv_note("conv-1", "b", agent_id="alice", agent_name="Alice")

    for i, role in enumerate(("master", "admin")):
        _login_as(client, f"boss{i}", role=role)
        target = (n1, n2)[i]
        r = client.patch(
            f"/api/workspace/conv/conv-1/notes/{target['note_id']}",
            json={"body": f"fixed by {role}"},
        )
        assert r.status_code == 200, r.text

    _login_as(client, "boss0", role="master")
    r = client.delete(f"/api/workspace/conv/conv-1/notes/{n1['note_id']}")
    assert r.status_code == 200


def test_note_must_belong_to_conversation(tmp_path):
    client, store = _build(tmp_path)
    note = store.add_conv_note("conv-1", "hello", agent_id="alice", agent_name="Alice")
    _login_as(client, "alice")

    # 同一 note_id 换个 conversation 路径 → 404（不因作者匹配而放行）
    r = client.patch(
        f"/api/workspace/conv/conv-OTHER/notes/{note['note_id']}",
        json={"body": "x"},
    )
    assert r.status_code == 404
    r = client.delete(f"/api/workspace/conv/conv-OTHER/notes/{note['note_id']}")
    assert r.status_code == 404
    assert store.list_conv_notes("conv-1")[0]["body"] == "hello"


def test_missing_note_is_404(tmp_path):
    client, _store = _build(tmp_path)
    _login_as(client, "alice")
    r = client.patch("/api/workspace/conv/conv-1/notes/nope", json={"body": "x"})
    assert r.status_code == 404
    r = client.delete("/api/workspace/conv/conv-1/notes/nope")
    assert r.status_code == 404


def test_author_identity_uses_username_convention(tmp_path):
    """写入侧 agent_id 取 user_name||username；登录只写 username →
    权限比对必须同口径（曾有读 user_name 恒空串的缺陷形态）。"""
    client, store = _build(tmp_path)
    # 经路由创建（走生产身份链），再验证作者可删
    _login_as(client, "carol")
    r = client.post(
        "/api/workspace/conv/conv-9/notes", json={"body": "note by carol"},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    note_id = r.json()["note"]["note_id"]
    assert r.json()["note"]["agent_id"] == "carol"

    r = client.delete(f"/api/workspace/conv/conv-9/notes/{note_id}")
    assert r.status_code == 200 and r.json()["ok"] is True
