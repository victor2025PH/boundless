# -*- coding: utf-8 -*-
"""Q-14 #262 E：告警「一眼看」链接——令牌铸/验、notifier 三种链接形态、/ops/glance 只读页。"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.utils import ops_glance_token as ogt  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_secret():
    ogt._reset_for_tests()
    ogt.configure("unit-test-secret-0123456789")
    yield
    ogt._reset_for_tests()


# ── 令牌 ──────────────────────────────────────────────────────────────────

def test_mint_verify_roundtrip_and_single_use():
    to = "/workspace?conv=tg:123"
    tok = ogt.mint(to)
    assert tok and "=" not in tok
    assert ogt.verify(tok, to) == (True, "ok")
    assert ogt.verify(tok, to) == (False, "used")


def test_token_bound_to_path_and_expiry():
    to = "/workspace?conv=a"
    tok = ogt.mint(to)
    assert ogt.verify(tok, "/workspace?conv=b") == (False, "bad_sig")
    assert ogt.verify(tok + "x", to) == (False, "bad_sig")
    assert ogt.verify("", to) == (False, "bad_sig")
    old = ogt.mint(to, ttl_sec=60, now=time.time() - 3600)
    assert ogt.verify(old, to) == (False, "expired")


def test_unconfigured_or_placeholder_secret_mints_nothing():
    ogt.configure("change-me-in-production")
    assert ogt.mint("/workspace") is None
    assert ogt.verify("abc", "/workspace") == (False, "unconfigured")
    assert ogt.glance_url("https://katie.bd2026.cc", "/workspace") is None


def test_mint_rejects_non_relative_targets():
    assert ogt.mint("https://evil.example/x") is None
    assert ogt.mint("//evil.example/x") is None
    assert ogt.mint("") is None


def test_glance_url_shape():
    url = ogt.glance_url("https://katie.bd2026.cc/", "/workspace?conv=tg:1&x=2")
    assert url.startswith("https://katie.bd2026.cc/ops/glance?t=")
    qs = parse_qs(urlsplit(url).query)
    assert qs["to"] == ["/workspace?conv=tg:1&x=2"]
    assert ogt.verify(qs["t"][0], qs["to"][0]) == (True, "ok")


# ── notifier 链接形态 ──────────────────────────────────────────────────────

def test_plainify_link_modes():
    from src.inbox.webhook_notifier import _plainify
    text = "看 [会话](/workspace?conv=tg:9) 与 [官网](https://bd2026.cc/x)"
    base = "https://katie.bd2026.cc"
    login = _plainify(text, base)
    assert "会话: https://katie.bd2026.cc/workspace?conv=tg:9" in login
    assert "官网: https://bd2026.cc/x" in login
    magic = _plainify(text, base, "magic")
    assert "会话: https://katie.bd2026.cc/ops/glance?t=" in magic
    assert "to=%2Fworkspace%3Fconv%3Dtg%3A9" in magic
    assert "官网: https://bd2026.cc/x" in magic          # 外链不套速览
    off = _plainify(text, base, "off")
    assert "http" not in off and "会话" in off and "官网" in off
    ogt.configure("")                                    # 铸不出令牌 → 回落 login 形态
    assert _plainify(text, base, "magic") == login


def test_matcher_carries_links_mode_with_default_login():
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier([
        {"name": "a", "format": "telegram", "token": "t", "target": "1", "events": ["escalation"], "links": "magic"},
        {"name": "b", "format": "telegram", "token": "t", "target": "1", "events": ["escalation"]},
        {"name": "c", "format": "telegram", "token": "t", "target": "1", "events": ["escalation"], "links": "weird"},
    ])
    modes = {m["name"]: m["links"] for m in n._matchers}
    assert modes == {"a": "magic", "b": "login", "c": "login"}


def test_store_sanitize_links():
    from src.integrations import notify_webhooks_store as ws
    base = {"name": "x", "format": "telegram", "token": "t", "target": "1", "events": ["escalation"]}
    assert ws.sanitize_webhook({**base, "links": "magic"})["links"] == "magic"
    assert ws.sanitize_webhook({**base, "links": "OFF"})["links"] == "off"
    assert ws.sanitize_webhook({**base, "links": "login"})["links"] == "login"
    assert "links" not in ws.sanitize_webhook({**base, "links": "nope"})
    assert "links" not in ws.sanitize_webhook(base)
    # 面板回传缺 links 键 → 沿用旧 magic；显式 login → 以回传为准
    old = {"x": {**base, "token": "real", "links": "magic"}}
    kept = ws.merge_preserve_secrets(ws.sanitize_list([{**base, "token": "re***"}]), old)
    assert kept[0]["links"] == "magic" and kept[0]["token"] == "real"
    reset = ws.merge_preserve_secrets(ws.sanitize_list([{**base, "links": "login"}]), old)
    assert reset[0]["links"] == "login"


# ── /ops/glance 页面 ───────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self):
        self.writes = 0

    def get_conversation(self, cid):
        if cid != "tg:42":
            return None
        return {"conversation_id": cid, "platform": "telegram", "account_id": "acc1",
                "display_name": "Ann", "contact_id": "c1"}

    def list_recent_messages(self, cid, *, limit=50, before_ts=None, include_deleted=True):
        assert include_deleted is False
        return [{"direction": "in", "ts": 1.0, "text": "hi", "media_type": ""},
                {"direction": "out", "ts": 2.0, "text": "hello", "media_type": ""}][-limit:]

    def get_automation_mode(self, cid):
        return "auto_ai"

    def __getattr__(self, name):          # 任何写方法被调都算失败
        if name.startswith(("set_", "record_", "upsert_", "mark_", "create_")):
            raise AssertionError(f"glance page must not write: {name}")
        raise AttributeError(name)


@pytest.fixture
def client():
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from fastapi.testclient import TestClient
    from src.web.routes.ops_glance_routes import register_ops_glance_routes

    app = FastAPI()
    app.state.inbox_store = _FakeStore()
    templates = Jinja2Templates(directory=str(ROOT / "src" / "web" / "templates"))
    register_ops_glance_routes(app, templates=templates)
    return TestClient(app)


def test_glance_ok_renders_summary_and_burns_token(client):
    to = "/workspace?conv=tg:42"
    url = ogt.glance_url("http://testserver", to)
    path = url[len("http://testserver"):]
    r = client.get(path)
    assert r.status_code == 200
    body = r.text
    assert "tg:42" in body and "Ann" in body and "acc1" in body and "telegram" in body
    assert "hi" in body and "hello" in body
    assert "AI 自动回" in body or "AI auto-reply" in body
    assert f'href="{to}"' in body                         # 「在工作台打开」→ 原地址（那页自会要登录）
    r2 = client.get(path)                                # 一次性
    assert r2.status_code == 410
    assert "/login?next=" in r2.text


def test_glance_bad_sig_and_missing_conv(client):
    r = client.get("/ops/glance?t=garbage&to=%2Fworkspace%3Fconv%3Dtg%3A42")
    assert r.status_code == 403
    assert "/login?next=" in r.text
    url = ogt.glance_url("http://testserver", "/workspace?conv=tg:404")
    r = client.get(url[len("http://testserver"):])
    assert r.status_code == 200
    assert "tg:404" in r.text


def test_glance_without_conv_shows_target_only(client):
    url = ogt.glance_url("http://testserver", "/admin/ops")
    r = client.get(url[len("http://testserver"):])
    assert r.status_code == 200
    assert "/admin/ops" in r.text


def test_glance_rejects_absolute_target(client):
    tok = ogt.mint("/workspace")
    r = client.get(f"/ops/glance?t={tok}&to=https%3A%2F%2Fevil.example%2F")
    assert r.status_code == 403                          # to 被归零 → 签名对不上
