# -*- coding: utf-8 -*-
"""#144-B（0902 skuio 截图 6834964252_1019/1020）门禁：「待处理」徽标与清单同口径。

事故：两会话等了 40 分钟（warn=30min < 40min < crit=2h）→ 徽标亮「待处理 2」，
点开面板却「暂无需要处理的会话」——后端 items 只装 ≥crit，徽标数字却取 breaching。

承诺修法：``/api/workspace/sla-alerts`` 的 items 同时返回 warn 档与 crit 档并带
``level``；面板分「严重超时 / 超过提醒线」两段渲染；徽标数字＝面板条数；
「全部忽略」对两段同样生效；SSE toast 仍只对 crit 边沿触发。
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]


class _Templates:
    def TemplateResponse(self, *a, **k):  # pragma: no cover - 页面路由不在本测范围
        raise NotImplementedError


def _client(inbox, cfg):
    from types import SimpleNamespace as NS
    from src.web.routes.unified_inbox_routes import register_unified_inbox_routes
    app = FastAPI()

    @app.middleware("http")
    async def _inject_session(request: Request, call_next):
        request.scope["session"] = {"role": "admin", "username": "sup", "user_id": "sup"}
        return await call_next(request)

    def _auth(request: Request):     # Depends(api_auth) 需要真 Request 签名
        return True

    register_unified_inbox_routes(
        app, page_auth=_auth, api_auth=_auth,
        templates=_Templates(), config_manager=NS(config=cfg))
    app.state.inbox_store = inbox
    return TestClient(app)


def _seed(inbox, key, age_sec, direction="in"):
    from src.inbox.models import InboxConversation, InboxMessage
    now = time.time()
    cid = f"whatsapp:acc:{key}"
    inbox.ingest_batch(
        InboxConversation(conversation_id=cid, platform="whatsapp", account_id="acc",
                          chat_key=key, display_name="N_" + key, language="en",
                          last_text="x", last_ts=now - age_sec, unread=1),
        [InboxMessage(conversation_id=cid, platform_msg_id="", direction=direction,
                      text="x", original_text="x", translated_text="x",
                      source_lang="en", ts=now - age_sec)])
    return cid


def test_two_conversations_waiting_40min_show_two_items_with_warn_level():
    """验收：两会话等 40 分钟（<2h）→ 徽标 2 且面板列出 2 条（level=warn）。"""
    from src.inbox.store import InboxStore
    inbox = InboxStore(Path(tempfile.mkdtemp()) / "i.db")
    try:
        a = _seed(inbox, "a", 40 * 60)
        b = _seed(inbox, "b", 41 * 60)
        _seed(inbox, "fresh", 60)                    # 5 分钟内，不计
        _seed(inbox, "answered", 3 * 3600, "out")     # 我方最后发言，不计
        cli = _client(inbox, {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}})
        r = cli.get("/api/workspace/sla-alerts").json()
        assert r["ok"] and r["quiet"] is False
        assert r["critical"] == 0
        assert r["breaching"] == 2
        assert len(r["items"]) == 2 == r["breaching"]     # 徽标数字＝面板条数
        assert {it["level"] for it in r["items"]} == {"warn"}
        assert [it["conversation_id"] for it in r["items"]] == [b, a]  # 等待久的在前
        for it in r["items"]:
            assert it["wait_sec"] >= 1800 and it["name"].startswith("N_")
    finally:
        inbox.close()


def test_mixed_levels_crit_first_then_warn():
    from src.inbox.store import InboxStore
    inbox = InboxStore(Path(tempfile.mkdtemp()) / "i.db")
    try:
        w = _seed(inbox, "w", 40 * 60)
        c = _seed(inbox, "c", 3 * 3600)
        cli = _client(inbox, {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}})
        r = cli.get("/api/workspace/sla-alerts").json()
        assert r["critical"] == 1 and r["breaching"] == 2
        assert [(it["conversation_id"], it["level"]) for it in r["items"]] == [
            (c, "crit"), (w, "warn")]
    finally:
        inbox.close()


def test_ignore_all_archives_warn_level_too():
    """「全部忽略」对两段同样生效：归档 warn 档会话后徽标/面板同步清零。"""
    from src.inbox.store import InboxStore
    inbox = InboxStore(Path(tempfile.mkdtemp()) / "i.db")
    try:
        a = _seed(inbox, "a", 40 * 60)
        c = _seed(inbox, "c", 3 * 3600)
        cli = _client(inbox, {"inbox": {"sla_warn_sec": 1800, "sla_crit_sec": 7200}})
        assert len(cli.get("/api/workspace/sla-alerts").json()["items"]) == 2
        # 前端「全部忽略」＝对面板并集（两段）批量归档
        rr = cli.post("/api/workspace/batch/archive",
                      json={"conversation_ids": [a, c], "archived": True}).json()
        assert rr.get("ok")
        r2 = cli.get("/api/workspace/sla-alerts").json()
        assert r2["items"] == [] and r2["breaching"] == 0 and r2["critical"] == 0
    finally:
        inbox.close()


def test_frontend_pill_and_panel_share_one_item_set():
    """静态钉：徽标数字与面板都出自 _slaPanelItems 并集；面板按 level 分两段。"""
    html = (REPO / "src/web/templates/workspace_base.html").read_text(encoding="utf-8")
    assert "function _slaPanelItems()" in html
    # 徽标：n 取并集长度，不再 sev?crit:brk
    pill = html.split("function _renderSlaPill()", 1)[1][:2400]
    assert "var g=_slaPanelItems();" in pill and "var n=g.all.length;" in pill
    assert "sev?crit:brk" not in pill
    # R87 #325：「急需处理」只在全部都是严重档时才标；混着提醒线档 → 「待处理」（数字仍＝面板条数）
    assert "var urgentAll = sev && g.warn.length===0;" in pill
    assert "urgentAll?window.T('base.pill.sla_urgent'):window.T('base.pill.sla_normal')" in pill
    # 面板：两段标题键 + 同一并集
    panel = html.split("function _renderSlaPanel()", 1)[1][:2600]
    assert "var g=_slaPanelItems();" in panel
    assert "base.drill.sec_crit" in panel and "base.drill.sec_warn" in panel
    # 全部忽略：并集覆盖 SLA 清单（含 warn 档）与升级清单
    assert "(_slaItems||[]).forEach(function(it){ if(it&&it.conversation_id) _idset[it.conversation_id]=1; });" in html
    from src.web.i18n_packs.workspace_shell import EN, ZH
    for k in ("base.drill.sec_crit", "base.drill.sec_warn",
              "base.sla.tip_head_mix", "base.sla.tip_esc_n"):
        assert k in ZH and k in EN, k
    assert "严重超时" in ZH["base.drill.sec_crit"] and "提醒线" in ZH["base.drill.sec_warn"]


def test_sse_toast_still_crit_edge_only():
    """快照 items 含 warn 档后，SSE「新转入严重超时」toast 仍只对 crit 边沿触发。"""
    src = (REPO / "src/web/routes/unified_inbox_realtime_routes.py").read_text(
        encoding="utf-8")
    seg = src.split("def _sla_pushes", 1)[1][:1400]
    assert 'str(it.get("level") or "crit") == "crit"' in seg
    assert "_edge_pick(crit_items, _sla_seen)" in seg
