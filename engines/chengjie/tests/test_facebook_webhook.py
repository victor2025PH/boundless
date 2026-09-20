"""Facebook Page Webhook 单元测试（与 line_webhook 同构覆盖）。

不依赖真 FB；用 fastapi.TestClient + AsyncMock 模拟 SkillManager。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations.facebook_webhook import (
    _maybe_alert_page_token,
    parse_page_probe,
    register_fb_messenger_routes,
    verify_fb_signature,
)


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()


def test_verify_signature_basic():
    secret = "topsecret"
    body = b'{"object":"page"}'
    assert verify_fb_signature(body, _sign(secret, body), secret) is True
    # 错的 secret
    assert verify_fb_signature(body, _sign("wrong", body), secret) is False
    # 错的 body
    assert verify_fb_signature(b"changed", _sign(secret, body), secret) is False
    # 空 secret
    assert verify_fb_signature(body, _sign(secret, body), "") is False
    # 错的 prefix
    assert verify_fb_signature(body, "sha1=abc", secret) is False


def test_routes_mount_when_disabled_and_gate_403():
    """热挂载契约（2026-08-10）：路由常驻，未启用时 GET/POST 一律 403。

    旧契约「disabled → 不挂路由」正是「向导保存凭证后入站要等重启」的根因
    （出站 worker 热注册了、/fb/webhook 却 404）。新语义：路由无条件常驻，
    可不可用逐请求看实时配置（SkillManager 亦为请求期解析）。
    """
    app = FastAPI()
    cm = MagicMock()
    cm.config = {"facebook_messenger": {"enabled": False}}
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    register_fb_messenger_routes(app, cm, tc)
    assert app.state.fb_webhook_path == "/fb/webhook"
    client = TestClient(app)
    r = client.get(
        "/fb/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "x",
                "hub.challenge": "1"},
    )
    assert r.status_code == 403
    assert client.post("/fb/webhook", content=b"{}").status_code == 403


def test_mounts_without_skill_manager_and_resolves_at_request_time():
    """协议号未配置的部署 telegram_client=None（2026-08-10 生产实锤）：路由照常
    挂载；SkillManager 请求期经 resolve_skill_manager（telegram_client →
    app.state）解析——就绪前真实事件回 503 让 Meta 重投（不假 ack 不丢事件、
    不污染到达统计），app.state 挂上后立即可处理。"""
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    register_fb_messenger_routes(app, cm, None)   # telegram_client=None
    assert app.state.fb_webhook_path == "/fb/webhook"
    client = TestClient(app)
    body = b'{"object":"page","entry":[]}'
    hdr = {"X-Hub-Signature-256": _sign("sec", body)}
    r = client.post("/fb/webhook", content=body, headers=hdr)
    assert r.status_code == 503
    # web_app 启动尾部把 SkillManager 挂上 app.state（真实时序）→ 立即可处理
    app.state.skill_manager = MagicMock()
    r = client.post("/fb/webhook", content=body, headers=hdr)
    assert r.status_code == 200


def test_hot_enable_after_wizard_save():
    """接入向导保存凭证 → 同一进程内 webhook 立即可用（免重启）。

    模拟真实时序：启动时渠道未启用（路由已常驻挂载）→ 运营在向导保存凭证
    （overlay 深合并进同一 config 对象、enabled 置 true）→ Meta 握手与签名
    事件立即放行。这是「出站热、入站冷」修复的核心断言。
    """
    app = FastAPI()
    cm = MagicMock()
    cm.config = {"facebook_messenger": {"enabled": False}}
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    register_fb_messenger_routes(app, cm, tc)
    client = TestClient(app)
    params = {"hub.mode": "subscribe", "hub.verify_token": "myvtoken",
              "hub.challenge": "777"}
    assert client.get("/fb/webhook", params=params).status_code == 403
    # 向导保存后的进程内配置形态
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "myvtoken",
        }
    }
    r = client.get("/fb/webhook", params=params)
    assert r.status_code == 200
    assert r.text == "777"
    body = b'{"object":"page","entry":[]}'
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200


def test_post_rejected_when_app_secret_missing():
    """启用但缺 app_secret：入站硬拒（无签名校验的公网 webhook 不放行）。"""
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_access_token": "tok",
            "app_secret": "",
            "verify_token": "v",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    register_fb_messenger_routes(app, cm, tc)
    client = TestClient(app)
    body = b'{"object":"page","entry":[]}'
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("whatever", body)},
    )
    assert r.status_code == 403


def test_get_verify_handshake_ok():
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "myvtoken",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    tc.skill_manager.process_message = AsyncMock(return_value="echo")
    register_fb_messenger_routes(app, cm, tc)
    assert app.state.fb_webhook_path == "/fb/webhook"

    client = TestClient(app)
    # 正确 verify_token
    r = client.get(
        "/fb/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "myvtoken",
            "hub.challenge": "1234567",
        },
    )
    assert r.status_code == 200
    assert r.text == "1234567"
    # 错的 verify_token
    r = client.get(
        "/fb/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG",
            "hub.challenge": "abc",
        },
    )
    assert r.status_code == 403


def test_post_event_signature_required():
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    tc = MagicMock()
    tc.skill_manager = MagicMock()
    tc.skill_manager.process_message = AsyncMock(return_value="echo")
    register_fb_messenger_routes(app, cm, tc)

    client = TestClient(app)
    body = b'{"object":"page","entry":[]}'
    # 没有签名 → 403
    r = client.post("/fb/webhook", content=body)
    assert r.status_code == 403
    # 错误签名 → 403
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("WRONG", body)},
    )
    assert r.status_code == 403
    # 正确签名（空 entry）→ 200
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200


def test_post_event_ignores_echo_and_delivery(monkeypatch):
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    sm = MagicMock()
    sm.process_message = AsyncMock(return_value="this should not be sent")
    tc = MagicMock()
    tc.skill_manager = sm

    # 拦截真实的 SDK 网络调用
    sent: list[Any] = []

    async def fake_send(psid, text, token, **kw):
        sent.append((psid, text, kw))
        return {"ok": True, "data": {}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_with_window_fallback",
        fake_send,
    )

    register_fb_messenger_routes(app, cm, tc)
    client = TestClient(app)

    body_dict = {
        "object": "page",
        "entry": [
            {
                "id": "100000",
                "time": 0,
                "messaging": [
                    # echo（Page 自己发出的回声）
                    {
                        "sender": {"id": "100000"},
                        "recipient": {"id": "USER1"},
                        "timestamp": 1,
                        "message": {"text": "ignored", "is_echo": True, "mid": "m1"},
                    },
                    # delivery（已送达回执）
                    {
                        "sender": {"id": "USER1"},
                        "recipient": {"id": "100000"},
                        "delivery": {"mids": ["m1"], "watermark": 1},
                    },
                    # read 回执
                    {
                        "sender": {"id": "USER1"},
                        "recipient": {"id": "100000"},
                        "read": {"watermark": 1},
                    },
                ],
            }
        ],
    }
    body = json.dumps(body_dict).encode("utf-8")
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200
    sm.process_message.assert_not_called()
    assert sent == []


def test_post_event_routes_text_to_skill_manager(monkeypatch):
    app = FastAPI()
    cm = MagicMock()
    cm.config = {
        "facebook_messenger": {
            "enabled": True,
            "page_id": "100000",
            "page_access_token": "tok",
            "app_secret": "sec",
            "verify_token": "v",
        }
    }
    sm = MagicMock()
    sm.process_message = AsyncMock(return_value="hello back!")
    tc = MagicMock()
    tc.skill_manager = sm

    sent: list[Any] = []

    async def fake_send(psid, text, token, **kw):
        sent.append({"psid": psid, "text": text, "kw": kw})
        return {"ok": True, "data": {}}

    monkeypatch.setattr(
        "src.integrations.facebook_webhook.fb_send_with_window_fallback",
        fake_send,
    )

    register_fb_messenger_routes(app, cm, tc)
    client = TestClient(app)

    body_dict = {
        "object": "page",
        "entry": [
            {
                "id": "100000",
                "time": 0,
                "messaging": [
                    {
                        "sender": {"id": "USER42"},
                        "recipient": {"id": "100000"},
                        "timestamp": 1700000000000,
                        "message": {"text": "hi page", "mid": "m99"},
                    }
                ],
            }
        ],
    }
    body = json.dumps(body_dict).encode("utf-8")
    r = client.post(
        "/fb/webhook",
        content=body,
        headers={"X-Hub-Signature-256": _sign("sec", body)},
    )
    assert r.status_code == 200
    sm.process_message.assert_awaited_once()
    args, kwargs = sm.process_message.await_args
    assert kwargs["text"] == "hi page"
    assert kwargs["user_id"] == "fb:USER42"
    assert kwargs["context"]["channel"] == "facebook_messenger"
    assert kwargs["context"]["fb_psid"] == "USER42"
    # 回复发出去
    assert len(sent) == 1
    assert sent[0]["psid"] == "USER42"
    assert sent[0]["text"] == "hello back!"


def test_page_token_alert_only_on_auth_failure(monkeypatch):
    """Token 失效（OAuthException/190/401）才告警；普通参数错误不惊动机主。

    Graph 的鉴权错误多以 HTTP 400 + OAuthException 返回，不能只看 401——
    这正是「token 吊销后静默积灰」的检测口径。去抖由 host_alert 自身负责，
    此处只钉「该叫的叫、不该叫的不叫、key 稳定」。
    """
    calls: list = []
    monkeypatch.setattr(
        "src.utils.host_alert.notify_host",
        lambda title, message, **kw: calls.append((title, kw)) or True,
    )
    _maybe_alert_page_token(
        400,
        '{"error":{"message":"Error validating access token: Session has '
        'expired","type":"OAuthException","code":190}}',
    )
    assert len(calls) == 1
    assert calls[0][1].get("key") == "fb_page_token"
    # 非鉴权错误（参数错）不告警
    _maybe_alert_page_token(
        400,
        '{"error":{"message":"(#100) Invalid parameter",'
        '"type":"GraphMethodException","code":100}}',
    )
    assert len(calls) == 1
    # 裸 401 无 body 也算
    _maybe_alert_page_token(401, "")
    assert len(calls) == 2


def test_parse_page_probe_shapes():
    """探针解析纯函数：成功带回主页身份；auth 与 http 失败分开；坏 JSON 不炸。"""
    ok = parse_page_probe(200, json.dumps({
        "id": "17891234", "name": "Boundless Page",
        "picture": {"data": {"url": "https://p.example/x.jpg"}},
    }))
    assert ok == {"ok": True, "page_id": "17891234", "name": "Boundless Page",
                  "picture": "https://p.example/x.jpg"}
    bad = parse_page_probe(400, json.dumps({
        "error": {"message": "Invalid OAuth access token",
                  "type": "OAuthException", "code": 190},
    }))
    assert bad["ok"] is False
    assert bad["error_kind"] == "auth"
    assert "Invalid OAuth" in bad["error"]
    weird = parse_page_probe(500, "oops not json")
    assert weird["ok"] is False
    assert weird["error_kind"] == "http"
    # 200 但没有 id（异常形态）也算失败，不能装作成功
    empty = parse_page_probe(200, "{}")
    assert empty["ok"] is False
