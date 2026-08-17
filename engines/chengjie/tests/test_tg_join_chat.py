"""Telegram「加入群组」端点门禁（2026-08-12）。

覆盖两层：
- ``parse_tg_join_target`` 纯函数——公开用户名 / ``+hash`` / ``joinchat`` 邀请链 /
  ``s/`` 预览深链 / 消息深链 各形态，以及 share/proxy/贴纸包/``c/`` 深链等**不可
  入群目标必须拒绝**（拒绝=前端不出按钮+后端 400，双层同口径）。
- 路由行为——成功/已在群/申请待批/FloodWait/坏链/client 不在线 的状态码与语义；
  **受管账号 worker 不在线绝不回落主 client**（写动作回落=换号入群，比失败更糟）。
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.unified_inbox_tg_join_routes import (
    parse_tg_join_target,
    register_tg_join_routes,
)


# ── 纯函数：链接解析 ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("link,expected", [
    # 公开用户名各形态
    ("https://t.me/deepfakelive", ("username", "deepfakelive")),
    ("http://t.me/deepfakelive", ("username", "deepfakelive")),
    ("t.me/deepfakelive", ("username", "deepfakelive")),
    ("www.t.me/deepfakelive", ("username", "deepfakelive")),
    ("https://telegram.me/some_channel", ("username", "some_channel")),
    ("@some_channel", ("username", "some_channel")),
    ("https://t.me/s/durov", ("username", "durov")),          # 预览深链
    ("https://t.me/durov/12345", ("username", "durov")),      # 消息深链 → 群本体
    ("https://t.me/durov?start=ref123", ("username", "durov")),  # query 掐掉
    # 邀请链两种形态 → 保留完整链接给 pyrogram
    ("https://t.me/+AbCdEf12345", ("invite", "https://t.me/+AbCdEf12345")),
    ("t.me/+AbCdEf12345", ("invite", "https://t.me/+AbCdEf12345")),
    ("https://t.me/joinchat/AAAAAEkk2WdoDrB4-Q8-gg",
     ("invite", "https://t.me/joinchat/AAAAAEkk2WdoDrB4-Q8-gg")),
])
def test_parse_accepts_joinable(link, expected):
    assert parse_tg_join_target(link) == expected


@pytest.mark.parametrize("link", [
    "",                                   # 空
    "hello world",                        # 裸文本
    "https://example.com/whatever",       # 非 t.me 主机
    "https://t.me/share/url?url=x",       # 分享深链
    "https://t.me/proxy?server=1.2.3.4",  # 代理配置
    "https://t.me/addstickers/foo",       # 贴纸包
    "https://t.me/setlanguage/zh",        # 语言包
    "https://t.me/c/1234567/89",          # 私有频道消息深链（无法经此入群）
    "https://t.me/+",                     # 空邀请 hash
    "https://t.me/joinchat/",             # 空 joinchat hash
    "@ab",                                # 用户名过短
    "https://t.me/1abc",                  # 用户名不能数字开头
])
def test_parse_rejects_unjoinable(link):
    assert parse_tg_join_target(link) == ("", "")


# ── 路由行为 ────────────────────────────────────────────────────────────────

class _FakePyro:
    """鸭子型 pyrogram client：`.loop` + `.get_chat`（_extract_pyro 判定用）+ join_chat。"""

    def __init__(self, loop, result=None, exc=None):
        self.loop = loop
        self.result = result
        self.exc = exc
        self.calls = []

    async def get_chat(self, *a, **k):  # pragma: no cover - 仅供鸭子判定
        raise NotImplementedError

    async def join_chat(self, target):
        self.calls.append(target)
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture()
def bg_loop():
    """独立线程上的常驻事件循环——复刻生产「pyrogram client 活在自己 loop」的形态。"""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


def _page_auth() -> None:
    """无参依赖（lambda request 会被 FastAPI 当 query 参数 → 422）。"""
    return None


def _client(pyro=None) -> TestClient:
    app = FastAPI()
    register_tg_join_routes(app, page_auth=_page_auth)
    if pyro is not None:
        # A 线包装器形态（.client 才是 pyro）——_extract_pyro 两种形态都认
        app.state.telegram_client = SimpleNamespace(client=pyro)
    return TestClient(app)


def _post(c: TestClient, link: str, account_id: str = "default"):
    return c.post("/api/unified-inbox/tg-join-chat",
                  json={"account_id": account_id, "link": link})


def test_join_ok_returns_chat_summary(bg_loop):
    chat = SimpleNamespace(id=-1001234, title="测试群", username="testgrp",
                           type=SimpleNamespace(value="supergroup"))
    pyro = _FakePyro(bg_loop, result=chat)
    r = _post(_client(pyro), "https://t.me/testgrp")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["chat"]["title"] == "测试群"
    assert d["chat"]["id"] == -1001234 and d["chat"]["type"] == "supergroup"
    assert pyro.calls == ["testgrp"]      # 公开用户名按 username 传给 join_chat


def test_join_invite_link_passed_verbatim(bg_loop):
    pyro = _FakePyro(bg_loop, result=SimpleNamespace(id=1, title="g"))
    r = _post(_client(pyro), "t.me/+AbCdEf12345")
    assert r.status_code == 200
    assert pyro.calls == ["https://t.me/+AbCdEf12345"]   # 邀请链保留完整形态


def test_join_bad_link_400_without_touching_client(bg_loop):
    pyro = _FakePyro(bg_loop, result=None)
    r = _post(_client(pyro), "https://t.me/share/url?url=x")
    assert r.status_code == 400
    assert pyro.calls == []               # 坏链在解析层拦下，不打 API


def test_join_client_unavailable_409():
    r = _post(_client(pyro=None), "https://t.me/testgrp")
    assert r.status_code == 409


def test_managed_account_never_falls_back_to_main_client(bg_loop):
    """受管账号 worker 不在线 → 409；绝不拿主 client 顶包（换号入群比失败更糟）。"""
    pyro = _FakePyro(bg_loop, result=SimpleNamespace(id=1, title="g"))
    r = _post(_client(pyro), "https://t.me/testgrp", account_id="8244899900")
    assert r.status_code == 409
    assert pyro.calls == []               # 主 client 全程没被碰


def test_join_already_participant_is_ok(bg_loop):
    class UserAlreadyParticipant(Exception):
        pass
    pyro = _FakePyro(bg_loop, exc=UserAlreadyParticipant())
    r = _post(_client(pyro), "https://t.me/testgrp")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["already"] is True


def test_join_request_pending_is_ok(bg_loop):
    class InviteRequestSent(Exception):
        pass
    pyro = _FakePyro(bg_loop, exc=InviteRequestSent())
    r = _post(_client(pyro), "t.me/+AbCdEf12345")
    assert r.status_code == 200
    assert r.json().get("pending") is True


def test_join_flood_wait_429_with_seconds(bg_loop):
    class FloodWait(Exception):
        def __init__(self, value):
            super().__init__(str(value))
            self.value = value
    pyro = _FakePyro(bg_loop, exc=FloodWait(17))
    r = _post(_client(pyro), "https://t.me/testgrp")
    assert r.status_code == 429
    assert "17" in str(r.json().get("detail") or "")


def test_join_expired_invite_400(bg_loop):
    class InviteHashExpired(Exception):
        pass
    pyro = _FakePyro(bg_loop, exc=InviteHashExpired())
    r = _post(_client(pyro), "t.me/+AbCdEf12345")
    assert r.status_code == 400


def test_join_unknown_error_502(bg_loop):
    pyro = _FakePyro(bg_loop, exc=RuntimeError("boom"))
    r = _post(_client(pyro), "https://t.me/testgrp")
    assert r.status_code == 502
