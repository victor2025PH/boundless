# -*- coding: utf-8 -*-
"""抖音企业号接入面板（实施96 P1-1 收尾，2026-09-08）：state 防篡改 / 授权 URL / code 换令牌落注册表 /
教程页表单保存凭证（overlay）/ 授权跳转 / 公开回调 → 账号登记 → 页面列出令牌状态。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

from src.integrations import douyin_official as dy

KEY, SECRET = "awtestkey", "s3cr3t"


class FakeTransport:
    def __init__(self, grant: Dict[str, Any] | None = None):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.grant = grant if grant is not None else {
            "access_token": "act.1", "expires_in": 1296000, "refresh_token": "rft.1", "refresh_expires_in": 2592000,
            "open_id": "open-abc", "scope": dy.OAUTH_SCOPES, "error_code": 0, "description": ""}

    async def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return 200, {"data": self.grant, "message": "success"}


class FakeRegistry:
    def __init__(self):
        self.rows: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(self, platform, account_id):
        return self.rows.get((platform, account_id))

    def list(self, platform=None, **_):
        return [r for (p, _a), r in self.rows.items() if platform is None or p == platform]

    def upsert(self, platform, account_id, *, meta=None, merge_meta=False, **kw):
        row = self.rows.setdefault((platform, account_id), {"platform": platform, "account_id": account_id, "meta": {}})
        for k, v in kw.items():
            if v is not None:
                row[k] = v
        if meta is not None:
            if merge_meta:
                row["meta"].update(meta)
            else:
                row["meta"] = dict(meta)
        return row


def test_oauth_state_roundtrip_and_tamper():
    now = 1_800_000_000.0
    st = dy.oauth_state(SECRET, now)
    assert dy.verify_oauth_state(SECRET, st, now + 5)
    assert not dy.verify_oauth_state(SECRET, st, now + dy.OAUTH_STATE_TTL_SEC + 1)   # 过期
    assert not dy.verify_oauth_state(SECRET, st, now - 5)                             # 未来时间戳
    assert not dy.verify_oauth_state("other", st, now + 5)                            # 换密钥
    ts, mac = st.split(".")
    assert not dy.verify_oauth_state(SECRET, f"{int(ts) + 1}.{mac}", now + 5)        # 改时间戳
    assert not dy.verify_oauth_state(SECRET, "garbage", now) and not dy.verify_oauth_state("", st, now)


def test_authorize_url_shape():
    u = dy.authorize_url(KEY, "https://x.example.com/webhook/douyin/oauth/callback", "1.abc")
    p = urlparse(u)
    assert f"{p.scheme}://{p.netloc}{p.path}" == dy.AUTHORIZE_URL
    q = parse_qs(p.query)
    assert q["client_key"] == [KEY] and q["response_type"] == ["code"] and q["state"] == ["1.abc"]
    assert q["redirect_uri"] == ["https://x.example.com/webhook/douyin/oauth/callback"]
    assert set(q["scope"][0].split(",")) == {"im.direct_message", "im.message_card", "tool.image.upload"}


async def test_complete_oauth_registers_account_and_keeps_persona():
    tr = FakeTransport()
    reg = FakeRegistry()
    reg.upsert("douyin", "open-abc", label="我的抖音号", meta={"persona_id": "p-1"})
    now = 1_800_000_000.0
    res = await dy.complete_oauth("code-1", config={"douyin": {"client_key": KEY, "client_secret": SECRET}},
                                  registry=reg, api=dy.DouyinApi(transport=tr), now=now)
    assert res["ok"] and res["open_id"] == "open-abc"
    method, url, kw = tr.calls[0]
    assert (method, url) == ("POST", dy.ACCESS_TOKEN_URL)
    assert kw["form"] == {"client_key": KEY, "client_secret": SECRET, "code": "code-1", "grant_type": "authorization_code"}
    row = reg.get("douyin", "open-abc")
    assert row["mode"] == "official" and row["status"] == "active" and row["label"] == "我的抖音号"
    m = row["meta"]
    assert m["persona_id"] == "p-1" and m["access_token"] == "act.1" and m["refresh_token"] == "rft.1"
    assert m["access_expires_at"] == now + 1296000 and m["refresh_expires_at"] == now + 2592000
    assert m["renew_count"] == 0 and m["client_key"] == KEY
    assert dy.token_state(m, now) == "ok"
    # worker 能直接吃这份 meta
    w = dy.DouyinOfficialWorker({"account_id": "open-abc", "meta": m}, {"douyin": {"client_key": KEY}},
                                api=dy.DouyinApi(transport=tr), state=dy.DouyinStateStore(":memory:"), now=lambda: now)
    assert w.token_state == "ok"


async def test_complete_oauth_failures():
    reg = FakeRegistry()
    assert (await dy.complete_oauth("c", config={}, registry=reg))["error"] == "missing_credentials"
    bad = FakeTransport(grant={"error_code": 10008, "description": "invalid code"})
    res = await dy.complete_oauth("c", config={"douyin": {"client_key": KEY, "client_secret": SECRET}},
                                  registry=reg, api=dy.DouyinApi(transport=bad))
    assert res == {"ok": False, "error": "exchange_failed", "error_code": 10008, "description": "invalid code"}
    assert reg.rows == {}


@pytest.fixture()
def _fake_registry(monkeypatch):
    reg = FakeRegistry()
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry", lambda: reg)
    return reg


def test_panel_flow_end_to_end(auth_client, app, _fake_registry, monkeypatch):
    cm = app.state.config_manager
    ref = {"Referer": "http://testserver/help/onboarding/douyin"}
    # 未配置：面板可见、授权按钮禁用、回调地址按 Host 生成
    r = auth_client.get("/help/onboarding/douyin")
    assert r.status_code == 200
    assert 'action="/help/onboarding/douyin/credentials"' in r.text
    assert "http://testserver/webhook/douyin/oauth/callback" in r.text
    assert 'id="obg-authorize"' in r.text and "obg-btn pri dis" in r.text
    # 未配置就点授权 → 回教程页带 error
    r = auth_client.get("/help/onboarding/douyin/authorize", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?error=missing_credentials")
    # 缺 secret 拒绝；保存成功 → overlay + 内存
    r = auth_client.post("/help/onboarding/douyin/credentials", data={"client_key": KEY}, headers=ref,
                         follow_redirects=False)
    assert r.status_code == 303 and "missing_secret" in r.headers["location"]
    r = auth_client.post("/help/onboarding/douyin/credentials", data={"client_key": KEY, "client_secret": SECRET},
                         headers=ref, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=1"), r.headers
    assert cm.config["douyin"]["client_key"] == KEY and cm.config["douyin"]["client_secret"] == SECRET
    assert cm.config["douyin"]["enabled"] is True
    # 密钥留空 = 保留
    r = auth_client.post("/help/onboarding/douyin/credentials", data={"client_key": "aw-new", "client_secret": ""},
                         headers=ref, follow_redirects=False)
    assert r.status_code == 303 and cm.config["douyin"]["client_secret"] == SECRET and cm.config["douyin"]["client_key"] == "aw-new"
    r = auth_client.get("/help/onboarding/douyin?saved=1")
    assert "config.local.yaml" in r.text and 'value="aw-new"' in r.text and SECRET not in r.text   # 密钥不回显
    assert "obg-btn pri dis" not in r.text                                                     # 授权按钮启用
    # 授权跳转：302 去抖音，redirect_uri 指回本机回调，state 可验
    r = auth_client.get("/help/onboarding/douyin/authorize", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(dy.AUTHORIZE_URL) and q["client_key"] == ["aw-new"]
    assert q["redirect_uri"] == ["http://testserver/webhook/douyin/oauth/callback"]
    state = q["state"][0]
    assert dy.verify_oauth_state(SECRET, state)
    # 回调：坏 state / 用户拒绝 / 成功登记
    r = auth_client.get("/webhook/douyin/oauth/callback?code=c&state=1.bad", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?error=bad_state")
    r = auth_client.get(f"/webhook/douyin/oauth/callback?error=access_denied&state={state}", follow_redirects=False)
    assert "error=denied:access_denied" in r.headers["location"]
    tr = FakeTransport()
    monkeypatch.setattr(dy, "DouyinApi", lambda *a, **k: _ApiWith(tr))
    r = auth_client.get(f"/webhook/douyin/oauth/callback?code=code-9&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?connected=open-abc"), r.headers
    assert tr.calls[0][2]["form"]["code"] == "code-9" and tr.calls[0][2]["form"]["client_key"] == "aw-new"
    row = _fake_registry.get("douyin", "open-abc")
    assert row and row["mode"] == "official" and row["meta"]["access_token"] == "act.1"
    # 页面列出已授权账号与令牌状态
    r = auth_client.get("/help/onboarding/douyin?connected=open-abc")
    assert "open-abc" in r.text and "令牌正常" in r.text and "授权成功" in r.text
    # 未登录不能进面板 / 授权，但公开回调路径可达（抖音回跳无会话）
    from starlette.testclient import TestClient
    with TestClient(app) as anon:
        assert anon.get("/help/onboarding/douyin/authorize", follow_redirects=False).status_code in (302, 303, 401, 403)
        r = anon.get("/webhook/douyin/oauth/callback?code=c&state=1.bad", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("?error=bad_state")


class _ApiWith(dy.DouyinApi):
    def __init__(self, tr):
        super().__init__(transport=tr)
