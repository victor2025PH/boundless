"""坐席「退出/删除」必须真的通知 Messenger 微服务（2026-07-30 无限弹窗事故）。

事故：``_remote_logout`` 只实现了 WhatsApp 分支，Messenger 走到那里直接 return——
坐席点「退出」只停了 Python 侧 worker + 把注册表置 offline，而 Node 微服务（headed
Chromium）对此一无所知：浏览器上下文还开着，持久化 profile 与
``<login_id>.cookies.json`` 快照都还在。后果：

- 关掉那个窗口 → Node 把 context ``close`` 当崩溃自愈 → 3s 后回灌 cookie 快照重新
  登录，而每次登录成功又清零退避计数 → **永远到不了「放弃自愈」那个上限**
  （生产日志实测三分钟弹回 6 次）；
- 在网页里手动退出登录 → 下一轮自愈把快照灌回去，等于没退。

只有 Node 自己的 ``POST /accounts/{id}/logout`` 会打「主动关闭」免疫标记并删掉
持久化 profile，所以这一跳是**必须打通**的，不是可选的礼貌通知。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_MSG_BASE = "http://127.0.0.1:18791"
_WA_BASE = "http://127.0.0.1:18792"


@pytest.fixture()
def calls(monkeypatch):
    """拦下两家微服务的 HTTP 薄封装，记录 (platform, url)。"""
    import src.integrations.messenger_web_login as mg
    import src.integrations.whatsapp_baileys_login as wa

    seen: list[tuple[str, str]] = []

    async def _mg_post(url, payload, timeout=20.0):
        seen.append(("messenger", url))
        return {"ok": True}

    async def _wa_post(url, payload, timeout=20.0):
        seen.append(("whatsapp", url))
        return {"ok": True}

    monkeypatch.setattr(mg, "_post_json", _mg_post)
    monkeypatch.setattr(wa, "_post_json", _wa_post)
    return seen


def _client(*, messenger_web=True, whatsapp_protocol=True):
    from src.web.routes.unified_inbox_account_routes import (
        register_account_routes,
    )

    cfg = {"platform_login": {
        "messenger": {"web_enabled": messenger_web, "web_url": _MSG_BASE},
        "whatsapp": {"protocol_enabled": whatsapp_protocol,
                     "baileys_url": _WA_BASE},
    }}
    app = FastAPI()
    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=SimpleNamespace(config=cfg))
    return TestClient(app)


def _registered(platform, account_id, mode):
    from src.integrations.account_registry import get_account_registry

    reg = get_account_registry()
    reg.upsert(platform, account_id, mode=mode, status="online")
    return reg


def test_messenger_logout_reaches_node_service(calls):
    """核心不变量：坐席登出 → Node 收到 logout（否则窗口关一次弹一次）。"""
    reg = _registered("messenger", "100089088819384", "web")
    r = _client().post("/api/accounts/messenger/100089088819384/logout")
    assert r.status_code == 200 and r.json().get("ok") is True
    assert calls == [("messenger",
                      f"{_MSG_BASE}/accounts/100089088819384/logout")]
    assert reg.get("messenger", "100089088819384")["status"] == "offline"


def test_messenger_remove_reaches_node_service(calls):
    """删除走同一条远端登出（否则删了账号窗口还在自己复活）。"""
    reg = _registered("messenger", "acct_mg_rm", "web")
    r = _client().post("/api/accounts/messenger/acct_mg_rm/remove")
    assert r.status_code == 200 and r.json().get("ok") is True
    assert calls == [("messenger", f"{_MSG_BASE}/accounts/acct_mg_rm/logout")]
    assert reg.get("messenger", "acct_mg_rm")["status"] == "removed"


def test_messenger_logout_skipped_when_web_mode_disabled(calls):
    """网页模式没开 → 没有微服务可通知，别对着不存在的端点发请求。"""
    _registered("messenger", "acct_mg_off", "web")
    r = _client(messenger_web=False).post(
        "/api/accounts/messenger/acct_mg_off/logout")
    assert r.status_code == 200 and r.json().get("ok") is True
    assert calls == []


def test_messenger_logout_survives_node_down(monkeypatch, calls):
    """微服务挂了 → 登出仍按 best-effort 完成（凭据照清），绝不 500。"""
    import src.integrations.messenger_web_login as mg

    async def _boom(url, payload, timeout=20.0):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mg, "_post_json", _boom)
    reg = _registered("messenger", "acct_mg_down", "web")
    r = _client().post("/api/accounts/messenger/acct_mg_down/logout")
    assert r.status_code == 200 and r.json().get("ok") is True
    assert reg.get("messenger", "acct_mg_down")["status"] == "offline"


def test_whatsapp_logout_still_reaches_baileys(calls):
    """回归钉：WhatsApp 那条老路（解除设备关联）不能被 messenger 分支挤掉。"""
    _registered("whatsapp", "acct_wa_lo", "protocol")
    r = _client().post("/api/accounts/whatsapp/acct_wa_lo/logout")
    assert r.status_code == 200 and r.json().get("ok") is True
    assert calls == [("whatsapp", f"{_WA_BASE}/accounts/acct_wa_lo/logout")]


def test_other_platforms_make_no_remote_call(calls):
    """爆炸半径不许扩大：telegram/line 等没有这种 Node 微服务，一个请求都不该发。"""
    _registered("telegram", "acct_tg_lo", "protocol")
    r = _client().post("/api/accounts/telegram/acct_tg_lo/logout")
    assert r.status_code == 200 and r.json().get("ok") is True
    assert calls == []
