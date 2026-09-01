# -*- coding: utf-8 -*-
"""报障工单处置台门禁（实施81 P0-2/P0-3/P1-4，2026-08-28）。

守住的决策（每条对应一个真实失效面）：

1. ``fix_note``（修复说明）——标 fixed 顺手写一句改了什么，回访文案引用；
   空值不得抹掉已写说明（回访重试场景）。
2. ``build_fix_notify_text`` 向后兼容：无 fix_note/update_hint 时文案形状不变
   （老部署零感知）；有则出「本次改动/获取方式」段且 HTML 转义。
3. ``resolve_update_hint``＝配置单点（bug_intake.update_hint），缺省空串＝
   回访**不带**更新段（服务端修复即时生效，硬塞「请更新」是误导）。
4. ``build_reply_text``：@报障人深链 + 工单号 footer 由代码拼（LLM/人抄编号
   会错），正文转义不代改。
5. ``/reply`` 端点行为：空文本 400 / 工单不存在 404 / 成功回写台账 note
   / 可选顺手流转状态——真 TestClient 打路由，不是字符串对着看。
6. 处置页三件套接线：页面路由 + nav_schema 项（图标已登记/进安全合规组）
   + admin.py 高亮映射；状态标签 zh/en 与 VALID_STATUSES 逐一对应
   （漏一个＝坐席看到英文原文，实施74 P0-4 的老病）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture()
def bi(tmp_path, monkeypatch):
    from src.ops import bug_intake
    bug_intake.reset_state_for_tests()
    monkeypatch.setattr(
        bug_intake, "_db_path", lambda: tmp_path / "bug_intake.db")
    yield bug_intake
    bug_intake.reset_state_for_tests()


def _mk_ticket(bi, **kw):
    args = dict(chat_id="-100123", account_id="777", reporter_id="42",
                reporter_name="张三", text="消息发不出去了", now=1000.0)
    args.update(kw)
    return bi.record_bug_ticket(**args)


# ── fix_note（P1-4）───────────────────────────────────────────────────────────
def test_fix_note_written_and_kept(bi):
    tid = _mk_ticket(bi)["ticket_id"]
    assert bi.set_ticket_status(tid, "fixed", fix_note="改了发送重试逻辑")
    row = bi.get_ticket(tid)
    assert row["status"] == "fixed"
    assert row["fix_note"] == "改了发送重试逻辑"
    # 空 fix_note 的后续流转不得抹掉已写说明
    assert bi.set_ticket_status(tid, "verified")
    assert bi.get_ticket(tid)["fix_note"] == "改了发送重试逻辑"
    # 非法状态仍拒绝
    assert not bi.set_ticket_status(tid, "nope", fix_note="x")


def test_fix_note_truncated(bi):
    tid = _mk_ticket(bi)["ticket_id"]
    bi.set_ticket_status(tid, "fixed", fix_note="x" * 999)
    assert len(bi.get_ticket(tid)["fix_note"]) == 300


# ── 回访文案（P0-3a）─────────────────────────────────────────────────────────
def test_notify_text_backward_compatible(bi):
    row = {"id": 7, "reporter_name": "张三", "reporter_id": "42",
           "title": "发不出消息"}
    txt = bi.build_fix_notify_text(row)
    assert "（#7）已修复上线。" in txt
    assert 'tg://user?id=42' in txt
    assert "本次改动" not in txt and "获取方式" not in txt
    assert txt.endswith("谢谢反馈！")


def test_notify_text_with_fix_note_and_hint(bi):
    row = {"id": 7, "reporter_name": "张三", "reporter_id": "42",
           "title": "发不出消息", "fix_note": "重写了<发送>链"}
    txt = bi.build_fix_notify_text(row, update_hint="重启智聊即生效")
    assert "本次改动：重写了&lt;发送&gt;链" in txt      # HTML 转义
    assert "📦 获取方式：重启智聊即生效。" in txt
    # 段落顺序：修复说明先于获取方式先于「帮忙验证」
    assert txt.index("本次改动") < txt.index("获取方式") < txt.index("帮忙验证")


def test_resolve_update_hint(bi):
    assert bi.resolve_update_hint(None) == ""
    assert bi.resolve_update_hint({}) == ""
    assert bi.resolve_update_hint(
        {"bug_intake": {"update_hint": "  已推送热补丁 "}}) == "已推送热补丁"
    assert len(bi.resolve_update_hint(
        {"bug_intake": {"update_hint": "y" * 999}})) == 200
    assert bi.resolve_update_hint({"bug_intake": "junk"}) == ""


# ── 群内回复文案（P0-2）──────────────────────────────────────────────────────
def test_reply_text_mention_and_footer(bi):
    row = {"id": 9, "reporter_name": "李四", "reporter_id": "88"}
    txt = bi.build_reply_text(row, "收到，<稍等>验证一下")
    assert txt.startswith('<a href="tg://user?id=88">李四</a> ')
    assert "收到，&lt;稍等&gt;验证一下" in txt
    assert txt.endswith("\n🎫 工单 #9")
    plain = bi.build_reply_text(row, "hi", mention=False)
    assert not plain.startswith("<a ")
    # 非数字 reporter_id 退化为纯文本称呼
    txt2 = bi.build_reply_text(
        {"id": 3, "reporter_name": "王五", "reporter_id": "webuser:x"}, "ok")
    assert "<a " not in txt2 and txt2.startswith("王五 ")


# ── /reply 端点行为（真路由）─────────────────────────────────────────────────
@pytest.fixture()
def client(bi, monkeypatch):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from src.web.routes import bug_intake_routes as routes

    sent = []

    async def _fake_send(app, row, text, buttons=None):
        sent.append((int(row.get("id") or 0), text, buttons))
        return True, "", 321          # 实施82 起 3-tuple（ok, note, msg_id）

    monkeypatch.setattr(routes, "_send_group_text", _fake_send)
    app = fastapi.FastAPI()
    routes.register_bug_intake_routes(
        app, api_auth=lambda request: None,
        page_auth=lambda request: None, config_manager=None)
    return TestClient(app), sent


def test_reply_endpoint_full_loop(bi, client):
    cli, sent = client
    tid = _mk_ticket(bi)["ticket_id"]
    # 空文本 400
    assert cli.post(f"/api/admin/bug-intake/{tid}/reply",
                    json={"text": "  "}).status_code == 400
    # 工单不存在 404
    assert cli.post("/api/admin/bug-intake/99999/reply",
                    json={"text": "hi"}).status_code == 404
    # 成功：发送 + 台账回写 + 顺手流转
    r = cli.post(f"/api/admin/bug-intake/{tid}/reply",
                 json={"text": "收到，正在查", "status": "confirmed"})
    assert r.status_code == 200 and r.json()["sent"] is True
    assert sent and sent[0][0] == tid and "收到，正在查" in sent[0][1]
    row = bi.get_ticket(tid)
    assert "[值守回复] 收到，正在查" in row["body"]
    assert row["status"] == "confirmed"


def test_reply_endpoint_send_failure_is_502(bi, client, monkeypatch):
    cli, _ = client
    from src.web.routes import bug_intake_routes as routes

    async def _fail(app, row, text):
        return False, "worker_offline", 0

    async def _fail2(app, row, text, buttons=None):
        return await _fail(app, row, text)

    monkeypatch.setattr(routes, "_send_group_text", _fail2)
    tid = _mk_ticket(bi)["ticket_id"]
    r = cli.post(f"/api/admin/bug-intake/{tid}/reply", json={"text": "hi"})
    assert r.status_code == 502
    # 发送失败不得回写台账（没发出去的话不算回复过）
    assert "[值守回复]" not in bi.get_ticket(tid)["body"]


def test_status_endpoint_accepts_fix_note_and_hint(bi, client):
    cli, sent = client
    tid = _mk_ticket(bi)["ticket_id"]
    r = cli.post(f"/api/admin/bug-intake/{tid}/status",
                 json={"status": "fixed", "fix_note": "改了重试",
                       "update_hint": "重启即生效"})
    assert r.status_code == 200 and r.json()["notified"] is True
    row = bi.get_ticket(tid)
    assert row["fix_note"] == "改了重试"
    # 回访文案带两段
    assert sent and "本次改动：改了重试" in sent[-1][1]
    assert "获取方式：重启即生效" in sent[-1][1]
    # 实施82：bot 出站消息 id 落库（reaction 验证/编辑消息的锚）
    assert row["notify_msg_id"] == 321
    # 实施82 P2：回访必须挂验证键盘（✅/❌，callback 带工单号+报障人）
    kb = sent[-1][2]
    assert kb and kb["inline_keyboard"][0][0]["callback_data"] == f"btv:{tid}:y:42"


def test_mark_notified_msg_id_not_clobbered_by_zero(bi):
    """pyro 回落链拿不到 msg_id（传 0）——重试失败不得抹掉已存的 bot 消息锚。"""
    tid = _mk_ticket(bi)["ticket_id"]
    bi.mark_notified(tid, True, "", msg_id=555)
    assert bi.get_ticket(tid)["notify_msg_id"] == 555
    bi.mark_notified(tid, False, "retry_fail", msg_id=0)
    assert bi.get_ticket(tid)["notify_msg_id"] == 555, "0 不覆盖非零锚"


# ── 处置页接线三件套 ─────────────────────────────────────────────────────────
def test_wiring_page_route_and_reply_route():
    src = _read("src/web/routes/bug_intake_routes.py")
    assert '/admin/bug-tickets' in src
    assert '/api/admin/bug-intake/{ticket_id}/reply' in src
    assert "_send_group_text" in src
    assert "bug_tickets.html" in src


def test_wiring_admin_registration():
    src = _read("src/web/admin.py")
    assert "page_auth=_admin_ctx.page_auth" in src.split(
        "register_bug_intake_routes(")[1][:200], (
        "admin.py 注册必须传 page_auth，否则处置页路由整体不注册")
    assert '"/admin/bug-tickets": "bug_tickets"' in src, (
        "侧栏高亮映射缺失：页面打开后导航不高亮")


def test_nav_item_wired_and_bilingual():
    from src.web.nav_schema import NAV_GROUPS_FULL, NAV_ICONS, NAV_ITEMS
    from src.web.web_i18n import get_translations

    it = NAV_ITEMS["bug_tickets"]
    assert it["path"] == "/admin/bug-tickets"
    assert it.get("master_only") is True, "值守/老板工具，坐席不该看到"
    assert it["icon"] in NAV_ICONS, "icon 未登记 SVG＝侧栏空白格子"
    comp = next(g for g in NAV_GROUPS_FULL
                if g["label_key"] == "section_compliance")
    assert "bug_tickets" in comp["items"]
    zh, en = get_translations("zh"), get_translations("en")
    assert zh.get("bug_tickets") and en.get("bug_tickets")


def test_status_labels_cover_valid_statuses():
    """状态标签 zh/en 与 VALID_STATUSES 逐一对应——漏键＝前端显英文原文
    （实施74 P0-4 修过的同类病，这里防我自己的新面回潮）。"""
    from src.ops.bug_intake import VALID_STATUSES
    from src.web.i18n_packs.bug_tickets_page import EN, ZH

    for s in VALID_STATUSES:
        assert ZH.get(f"bt_st_{s}"), f"缺 zh 状态标签 bt_st_{s}"
        assert EN.get(f"bt_st_{s}"), f"缺 en 状态标签 bt_st_{s}"
    assert set(ZH) == set(EN), "pack zh/en 键集必须一致"


def test_template_consumes_status_select_and_reply():
    src = _read("src/web/templates/bug_tickets.html")
    assert 'set active="bug_tickets"' in src
    assert "bt_st_" in src and "/reply" in src and "/shots" in src
    assert "wsFmtDateTime" in src, "时间渲染必须走 wsFmt* 家族（勿裸 toLocale*）"


def test_bulk_triage_excludes_fixed():
    """批量分诊（P2-2）三不变量：① 批量状态选项**不含 fixed**——status 端点对
    fixed 自动 @报障人回访，批量放开＝一次 @ 几十人刷屏；② 全选/应用函数在
    顶层（内联 onclick 可达）；③ 勾选与行选中是两种手势（checkbox 不触发详情）。"""
    src = _read("src/web/templates/bug_tickets.html")
    blk = src.split('id="bt-blk-bar"')[1].split("</div>")[0]
    assert 'value="confirmed"' in blk and 'value="closed"' in blk
    assert 'value="fixed"' not in blk, "批量选项混进 fixed＝回访 @人 刷屏事故"
    assert "function btBulkApply" in src and "function btToggleAll" in src
    assert "bt-row-chk" in src and "btSyncBulkBar" in src
    assert "classList.contains('bt-row-chk')" in src, "勾选必须不触发行选中"
