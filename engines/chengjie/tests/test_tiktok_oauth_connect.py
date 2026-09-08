# -*- coding: utf-8 -*-
"""TikTok 面板一键接入（实施100 T3，2026-09-08）：保存应用凭证 → 授权跳转（state 带注册地）→ 公开回调换令牌
→ 账号落注册表（资料、令牌、region）→ 自动注册 webhook → 页面显示令牌自动续期；scope 不齐 / 坏 state / 拒绝授权 /
手动注册 webhook。全应用（admin app）+ 假 transport。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

from src.integrations import tiktok_official as tk

APP, SECRET = "app-77", "s3cr3t"


class FakeTransport:
    def __init__(self):
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.responses: Dict[str, Tuple[int, Dict[str, Any]]] = {}

    async def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.get(url, (200, {"code": 0, "data": {}}))


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


@pytest.fixture()
def wired(monkeypatch):
    reg = FakeRegistry()
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry", lambda: reg)
    tr = FakeTransport()
    monkeypatch.setattr(tk, "TikTokApi", lambda *a, **k: _Api(tr))
    return reg, tr


class _Api(tk.TikTokApi):
    def __init__(self, tr):
        super().__init__(transport=tr)


def test_tiktok_one_click_flow(auth_client, app, wired):
    reg, tr = wired
    cm = app.state.config_manager
    ref = {"Referer": "http://testserver/help/onboarding/tiktok"}
    # 未配置：授权/注册按钮禁用，回调地址按 Host 生成
    r = auth_client.get("/help/onboarding/tiktok")
    assert r.status_code == 200
    assert "http://testserver/webhook/tiktok/oauth/callback" in r.text and 'id="obg-tt-authorize"' in r.text
    assert 'aria-disabled="true"' in r.text
    r = auth_client.get("/help/onboarding/tiktok/authorize?region=SG", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?error=missing_credentials")
    # 保存应用凭证 → overlay + enabled
    r = auth_client.post("/help/onboarding/tiktok/credentials", data={"app_id": APP}, headers=ref, follow_redirects=False)
    assert "missing_secret" in r.headers["location"]
    r = auth_client.post("/help/onboarding/tiktok/credentials", data={"app_id": APP, "secret": SECRET}, headers=ref,
                         follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?saved=1")
    assert cm.config["tiktok"] == {"app_id": APP, "secret": SECRET, "enabled": True}
    r = auth_client.get("/help/onboarding/tiktok?saved=1")
    assert "应用凭证已保存" in r.text and SECRET not in r.text and 'aria-disabled="true"' not in r.text
    # 授权跳转：region 必填；302 去 TikTok，state 带 SG
    r = auth_client.get("/help/onboarding/tiktok/authorize", follow_redirects=False)
    assert r.headers["location"].endswith("?error=missing_region")
    r = auth_client.get("/help/onboarding/tiktok/authorize?region=sg", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(tk.AUTHORIZE_URL)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_key"] == [APP] and q["redirect_uri"] == ["http://testserver/webhook/tiktok/oauth/callback"]
    assert set(q["scope"][0].split(",")) >= set(tk.MESSAGING_SCOPES)
    state = q["state"][0]
    assert tk.verify_oauth_state(SECRET, state) == "SG"
    # 回调：坏 state / 用户拒绝 / scope 不齐 / 成功（含自动注册 webhook）
    r = auth_client.get("/webhook/tiktok/oauth/callback?code=c&state=1.SG.bad", follow_redirects=False)
    assert r.headers["location"].endswith("?error=bad_state")
    r = auth_client.get(f"/webhook/tiktok/oauth/callback?error=access_denied&state={state}", follow_redirects=False)
    assert "error=denied:access_denied" in r.headers["location"]
    tr.responses[tk.TOKEN_URL] = (200, {"code": 0, "data": {"access_token": "a", "open_id": "open-7",
                                                            "scope": "user.info.basic,message.list.read"}})
    r = auth_client.get(f"/webhook/tiktok/oauth/callback?code=c1&state={state}", follow_redirects=False)
    assert r.headers["location"].endswith("?error=ungranted_scopes") and reg.get("tiktok", "open-7") is None
    tr.responses[tk.TOKEN_URL] = (200, {"code": 0, "data": {
        "access_token": "act", "expires_in": 86400, "refresh_token": "rft", "refresh_token_expires_in": 31536000,
        "open_id": "open-7", "scope": ",".join(tk.OAUTH_SCOPES)}})
    tr.responses[tk.BUSINESS_GET_URL] = (200, {"code": 0, "data": {"username": "shop_sg", "display_name": "Shop SG"}})
    tr.responses[tk.WEBHOOK_LIST_URL] = (200, {"code": 0, "data": {"callback_url": ""}})
    tr.responses[tk.WEBHOOK_UPDATE_URL] = (200, {"code": 0, "data": {"callback_url": "http://testserver/webhook/tiktok"}})
    r = auth_client.get(f"/webhook/tiktok/oauth/callback?code=c2&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?connected=open-7&region=SG&webhook=ok"), r.headers
    row = reg.get("tiktok", "open-7")
    assert row["mode"] == "official" and row["label"] == "Shop SG"
    assert row["meta"]["access_token"] == "act" and row["meta"]["refresh_token"] == "rft" and row["meta"]["region"] == "SG"
    exch = next(c for c in tr.calls if c[1] == tk.TOKEN_URL and c[2]["json_body"]["auth_code"] == "c2")
    assert exch[2]["json_body"]["redirect_uri"] == "http://testserver/webhook/tiktok/oauth/callback"
    wh = next(c for c in tr.calls if c[1] == tk.WEBHOOK_UPDATE_URL)
    assert wh[2]["json_body"] == {"app_id": APP, "secret": SECRET, "event_type": "DIRECT_MESSAGE",
                                  "callback_url": "http://testserver/webhook/tiktok"}
    # 页面：账号行 + 令牌自动续期 + 成功态下一步
    r = auth_client.get("/help/onboarding/tiktok?connected=open-7&region=SG&webhook=ok")
    html = r.text
    assert "open-7" in html and "@shop_sg" in html and "令牌自动续期" in html and "私信 API 可用" in html
    assert "私信 Webhook 已注册" in html and "发一条私信" in html
    # 手动注册 webhook（幂等：已一致 → 不再 update）
    n = len([c for c in tr.calls if c[1] == tk.WEBHOOK_UPDATE_URL])
    tr.responses[tk.WEBHOOK_LIST_URL] = (200, {"code": 0, "data": {"callback_url": "http://testserver/webhook/tiktok"}})
    r = auth_client.post("/help/onboarding/tiktok/webhook", headers=ref, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("?webhook=ok")
    assert len([c for c in tr.calls if c[1] == tk.WEBHOOK_UPDATE_URL]) == n
    tr.responses[tk.WEBHOOK_LIST_URL] = (200, {"code": 0, "data": {"callback_url": ""}})
    tr.responses[tk.WEBHOOK_UPDATE_URL] = (200, {"code": 40001, "message": "no access"})
    r = auth_client.post("/help/onboarding/tiktok/webhook", headers=ref, follow_redirects=False)
    assert "error=webhook:40001" in r.headers["location"]
    r = auth_client.get("/help/onboarding/tiktok?error=webhook:40001")
    assert "Webhook 注册被 TikTok 拒绝" in r.text
    # 引流链接按 username 生成；自检 API：凭证在、账号已授权，但 worker 未在启动期注册 → 需重启（诚实）
    assert "https://tiktok.me/shop_sg" in html and 'data-me-base="https://tiktok.me/shop_sg"' in html
    st = auth_client.get("/api/onboarding/tiktok/status").json()
    assert st["slug"] == "tiktok" and st["checks"]["credentials_configured"] is True
    assert st["checks"]["accounts_authorized"] == 1 and st["checks"]["restart_required"] is True
    assert st["light"] == "amber" and st["hint"] == "restart_required" and st["step"] == 4
    assert st["checks"]["public_https"] is False
    assert 'data-light="amber"' in html and "重启智聊一次" in html
    assert auth_client.get("/api/onboarding/nope/status").status_code == 404
    # 事件到达后（假装 worker 已注册）→ 等首条私信 / 已连通；静默 25 小时 → amber
    from types import SimpleNamespace
    from src.integrations import account_orchestrator as ao
    from src.integrations import tiktok_official as tk_mod
    ao._WORKER_FACTORIES["tiktok:official"] = lambda acc, cfg: None
    try:
        tk_mod._reset_for_tests()
        store_ = tk_mod.get_state_store()
        st = auth_client.get("/api/onboarding/tiktok/status").json()
        assert st["hint"] == "restart_required" and st["checks"]["webhook_mounted"] is False   # 路由仍未挂：仍需重启
        tk_mod.register_tiktok_routes(app, SimpleNamespace(config=cm.config))                    # 模拟重启后装载
        st = auth_client.get("/api/onboarding/tiktok/status").json()
        assert st["light"] == "blue" and st["hint"] == "await_first_dm" and st["step"] == 5
        store_.record_event("open-7", time.time())
        store_.record_inbound("open-7", "u1", conversation_id="c1", msg_id="m1", ts=time.time(), ref="ig_bio")
        st = auth_client.get("/api/onboarding/tiktok/status").json()
        assert st["light"] == "green" and st["hint"] == "connected"
        html = auth_client.get("/help/onboarding/tiktok").text
        assert "ig_bio · 1" in html and "事件 1" in html and 'data-light="green"' in html
        store_.record_event("open-7", time.time() - 25 * 3600)   # MAX 保留最新，改用直写模拟静默
        with store_._lock:
            store_._conn.execute("UPDATE biz_stats SET last_event_ts=? WHERE business_id=?", (time.time() - 25 * 3600, "open-7"))
            store_._conn.commit()
        st = auth_client.get("/api/onboarding/tiktok/status").json()
        assert st["light"] == "amber" and st["hint"] == "webhook_silent"
        assert "webhook 静默 25 小时" in auth_client.get("/help/onboarding/tiktok").text
    finally:
        ao._WORKER_FACTORIES.pop("tiktok:official", None)
        tk_mod._reset_for_tests()
    # 未登录：面板路由不可达，公开回调可达
    from starlette.testclient import TestClient
    with TestClient(app) as anon:
        assert anon.get("/help/onboarding/tiktok/authorize?region=SG", follow_redirects=False).status_code in (302, 303, 401, 403)
        r = anon.get("/webhook/tiktok/oauth/callback?code=c&state=1.SG.bad", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("?error=bad_state")
