# -*- coding: utf-8 -*-
"""抖音渠道端到端可用性 + 与其它平台对齐（实施96 P0-3 验收）。

用 conftest 的**完整 admin app**（create_app 全量路由）+ 抖音演示 worker，走**未来边车会用的
同一条 HTTP 路径**：`POST /api/internal/protocol/ingest` 进线 → `GET /api/unified-inbox/chats`
列表可见 → `GET /api/unified-inbox/thread` 线程可读 → `POST /api/unified-inbox/send` 人工发送
（含策略拦截 409 与正常送达）→ 出站镜像回线程。再把「其它平台自动享有的服务端能力」逐项
对抖音会话核一遍（自动化档位、平台封顶、翻译、拟人能力位、消息管理元数据）。
"""
from __future__ import annotations

import time

import pytest

from src.integrations import account_orchestrator as ao
from src.integrations import douyin_mock_worker as dm

CHAT = "douyin:user:u1"
ACCT = "demo"
CFG = {"platform_login": {"douyin": {"mock_enabled": True, "mock_echo_delay_sec": -1}}}


@pytest.fixture()
def douyin_ready(app, tmp_path):
    ao._WORKER_FACTORIES.pop("douyin:web", None)   # pop 而非 monkeypatch.delitem（后者收尾会恢复）
    from src.inbox.store import InboxStore
    # 完整 app 的 inbox_store 由 main.py 生命周期挂载；测试里按其它收件箱用例同款手挂
    app.state.inbox_store = InboxStore(tmp_path / "inbox_e2e.db")
    from src.integrations.account_registry import get_account_registry
    reg = get_account_registry()
    assert dm.register_douyin_mock_worker(CFG, registry=reg) is True
    orch = ao.get_orchestrator()
    w = dm.DouyinMockWorker({"account_id": ACCT, "meta": {}}, CFG)
    w.running = True
    key = ao.account_key("douyin", ACCT)
    orch._managed[key] = ao._Managed(key=key, platform="douyin", account_id=ACCT,
                                     mode="web", worker=w, state="running")
    try:
        yield w
    finally:
        orch._managed.pop(key, None)
        # 工厂显式摘除：否则泄漏到 test_platform_matrix（「编排器注册了 douyin:web 但矩阵没有」）
        ao._WORKER_FACTORIES.pop("douyin:web", None)


def _ingest(client, text: str, msg_id: str, ts: float | None = None):
    return client.post("/api/internal/protocol/ingest", json={
        "platform": "douyin", "account_id": ACCT, "chat_key": CHAT,
        "name": "抖音客户小王", "text": text, "direction": "in",
        "msg_id": msg_id, "ts": ts or time.time(),
        "source": {"conversation_id": CHAT, "server_message_id": msg_id},
    })


def test_douyin_inbound_list_thread_send_roundtrip(auth_client, app, douyin_ready):
    w = douyin_ready
    # 1) 进线：与 Baileys / Messenger / Zalo / IG 边车同一条内部桥
    r = _ingest(auth_client, "你们这个多少钱？", "dy-in-1")
    assert r.status_code == 200, r.text

    # 2) 收件箱列表可见（平台无关的 store 读路径）
    r = auth_client.get("/api/unified-inbox/chats", params={"platform": "douyin", "limit": 30})
    assert r.status_code == 200, r.text
    chats = r.json().get("chats") or []
    mine = [c for c in chats if c.get("platform") == "douyin" and c.get("chat_key") == CHAT]
    assert mine, f"抖音会话未出现在列表：{r.json()!r}"
    c0 = mine[0]
    assert c0.get("account_id") == ACCT
    assert (c0.get("name") or c0.get("display_name")) == "抖音客户小王"
    assert c0.get("platform_name") in ("Douyin", "douyin", "抖音")
    # 与其它平台同一份会话形状：可发送 + 发送模式 + 自动化档位（D-M1 出厂 review）
    assert c0.get("can_send") is True
    assert "multi_choice" in (c0.get("send_modes") or [])
    assert c0.get("automation_mode") in ("review", "manual", "auto_ai", "multi_choice")

    # 3) 线程可读
    r = auth_client.get("/api/unified-inbox/thread",
                        params={"platform": "douyin", "account_id": ACCT, "chat_key": CHAT})
    assert r.status_code == 200, r.text
    body = r.json()
    msgs = body.get("messages") or []
    assert any(m.get("direction") == "in" and "多少钱" in str(m.get("text")) for m in msgs)

    # 4) 人工发送带外链 → 平台策略 409（与 Kill-Switch/配额拦截同一回执形状）
    r = auth_client.post("/api/unified-inbox/send", json={
        "platform": "douyin", "account_id": ACCT, "chat_key": CHAT,
        "text": "戳这里下单 https://bd2026.cc/order", "skip_translate": True,
    })
    assert r.status_code == 409, r.text
    d = r.json().get("detail") or r.json()
    assert d.get("code") == "send_blocked" and d.get("reason") == "policy_link_denied"
    # 出的是针对平台规则的人话（不是「安全护栏」那句通用文案，也不是裸 i18n 键）
    msg = str(d.get("message") or "")
    assert "err." not in msg and ("链接" in msg or "link" in msg.lower()), msg
    assert w.sent == []

    # 5) 正常发送 → 送达 + 出站镜像回线程
    r = auth_client.post("/api/unified-inbox/send", json={
        "platform": "douyin", "account_id": ACCT, "chat_key": CHAT,
        "text": "亲，这款 99 元，今天下单有活动", "skip_translate": True,
    })
    assert r.status_code == 200, r.text
    assert w.sent == [(CHAT, "亲，这款 99 元，今天下单有活动")]
    r = auth_client.get("/api/unified-inbox/thread",
                        params={"platform": "douyin", "account_id": ACCT, "chat_key": CHAT})
    msgs = r.json().get("messages") or []
    assert any(m.get("direction") == "out" and "99 元" in str(m.get("text")) for m in msgs)

    # 6) send-caps 带 chat_key → 回复窗快照（倒计时 / 剩余条数）+ 气泡上限按平台封顶到 2
    r = auth_client.get("/api/unified-inbox/send-caps",
                        params={"platform": "douyin", "account_id": ACCT, "chat_key": CHAT})
    assert r.status_code == 200, r.text
    caps = r.json()
    rw = caps.get("reply_window") or {}
    assert rw.get("cap") == 6 and rw.get("sent") == 1 and rw.get("remaining") == 5
    assert rw.get("remaining_sec") > 23 * 3600 and rw.get("manual_allowed") is True
    assert caps.get("bubbles_max_parts") <= 2

    # 7) 24h 内 6 条配额：再发 5 条成功，第 7 条 409 policy_window_exhausted（人话说清等客户再发言）
    for i in range(5):
        r = auth_client.post("/api/unified-inbox/send", json={
            "platform": "douyin", "account_id": ACCT, "chat_key": CHAT,
            "text": f"补充说明 {i}", "skip_translate": True})
        assert r.status_code == 200, (i, r.text)
    r = auth_client.post("/api/unified-inbox/send", json={
        "platform": "douyin", "account_id": ACCT, "chat_key": CHAT,
        "text": "第七条", "skip_translate": True})
    assert r.status_code == 409, r.text
    d = r.json().get("detail") or r.json()
    assert d.get("reason") == "policy_window_exhausted"
    assert "配额" in str(d.get("message")) or "allowance" in str(d.get("message")).lower()
    assert len(w.sent) == 6
    # 客户再发言 → 配额重置，又能发
    assert _ingest(auth_client, "还有优惠吗", "dy-in-2").status_code == 200
    r = auth_client.post("/api/unified-inbox/send", json={
        "platform": "douyin", "account_id": ACCT, "chat_key": CHAT,
        "text": "有的，今天下单再减 10", "skip_translate": True})
    assert r.status_code == 200, r.text
    assert len(w.sent) == 7


def test_douyin_gets_the_same_server_side_capabilities(app, douyin_ready):
    """其它平台「自动享有」的服务端能力，对抖音会话逐项核对（平台无关 = 已对齐）。"""
    cfg = getattr(app.state.config_manager, "config", None) or {}
    # 平台封顶：未配抖音 → 不封顶（与 telegram 同）
    from src.inbox.effective_automation import platform_ceilings_from_config
    caps = platform_ceilings_from_config(cfg, {})
    assert "douyin" not in caps
    # 拟人链能力位：mock worker 已读/正在输入都有（TG/WA 同款）
    from src.integrations.platform_capabilities import worker_capabilities
    assert worker_capabilities(douyin_ready) == {
        "send_text": True, "send_media": True, "mark_read": True, "typing": True}
    # 出站语言决议 / 语言策略：平台无关函数可直接对抖音会话调用
    from src.ai.lang_policy import resolve_conversation_language
    assert resolve_conversation_language is not None
    # 群/私聊判定：抖音 chat_key 默认私聊（与官方渠道 user 键同）
    from src.inbox.normalizer import infer_chat_type
    assert infer_chat_type("douyin", CHAT, None) == "private"
    # 渠道策略：抖音有声明，其它平台无限制
    from src.inbox.channel_policy import policy_for, UNLIMITED
    assert policy_for("douyin").max_text_len == 1000 and policy_for("telegram") is UNLIMITED


def test_registry_json_served_and_inbox_falls_back_to_it(auth_client):
    """收件箱前端：PC/PN 手写表没有的平台回落到 /static/platform_registry.json（品牌色 + 正名）。"""
    r = auth_client.get("/static/platform_registry.json")
    assert r.status_code == 200, r.text
    data = r.json()
    by_id = {p["id"]: p for p in data["platforms"]}
    assert by_id["douyin"]["color"] == "#FE2C55" and by_id["douyin"]["name"] == "Douyin"
    assert by_id["tiktok"]["region_aware"] is True
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src/web/templates/unified_inbox.html").read_text(
        encoding="utf-8")
    assert "fetch('/static/platform_registry.json'" in html
    assert "function platColor(p){ return PC[p]||(_PREG[p]&&_PREG[p].color)" in html
    assert "function platName(p){ return PN[p]||(_PREG[p]&&_PREG[p].name)" in html


def test_douyin_planned_status_is_honest_in_login_modes(auth_client, app):
    """接入向导 / 登录方式：抖音尚未实现 → 必须如实报「规划中」而不是给一张点了没反应的卡。"""
    r = auth_client.get("/api/platforms/douyin/login/modes")
    # 未登记平台可能 404，也可能返回空/不可用列表——两者都算「没有假出口」
    assert r.status_code in (200, 400, 404), r.text
    if r.status_code == 200:
        modes = r.json().get("modes") or []
        assert all(not m.get("available", False) for m in modes), modes
