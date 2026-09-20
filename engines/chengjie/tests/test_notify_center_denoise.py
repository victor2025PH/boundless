# -*- coding: utf-8 -*-
"""通知中心去客户聊天消息门禁（impl85 阶段5，工单#30 钧拍板）。

「铃铛的消息中心，不用显示客户聊天记录，只需要系统的消息或需要人工处理的消息」。
值守承诺：客户消息默认不弹系统通知、不进通知中心（隐私+噪音），设置开关默认关；
发版提醒/协议掉线/SLA/需人工等系统类通知保留不受影响。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.web.routes.unified_inbox_realtime_routes import (  # noqa: E402
    _notif_content_ok,
    customer_msgs_in_center,
)

ON_CFG = {"workspace": {"notify_center": {"customer_messages": True}}}
OFF_CFG = {"workspace": {"notify_center": {"customer_messages": False}}}


# ── 纯函数：开关语义 ─────────────────────────────────────────────────────────

def test_customer_msgs_default_off_and_override():
    assert customer_msgs_in_center(None) is False        # 缺省=不进（拍板）
    assert customer_msgs_in_center({}) is False
    assert customer_msgs_in_center(ON_CFG) is True       # 想盯消息的人自己打开
    assert customer_msgs_in_center(OFF_CFG) is False
    assert customer_msgs_in_center({"workspace": {"notify_center": "junk"}}) is False


def test_content_gate_blocks_inbox_message_keeps_system_events():
    msg = {"type": "inbox_message", "data": {"preview": "客户私聊内容"}}
    assert _notif_content_ok(msg) is False               # 默认不进铃铛
    assert _notif_content_ok(msg, OFF_CFG) is False
    assert _notif_content_ok(msg, ON_CFG) is True        # 开关打开才进
    # 系统类事件不受影响（值守承诺「运维通知保留」）
    for etype in ("sla_alert", "health_alert", "escalation", "draft_sla_breach",
                  "conversation_assigned", "ops_report"):
        assert _notif_content_ok({"type": etype, "data": {}}) is True
    # bot_peer_alert 既有语义不变：只收 daily_budget
    assert _notif_content_ok(
        {"type": "bot_peer_alert", "data": {"reason": "daily_budget"}}) is True
    assert _notif_content_ok(
        {"type": "bot_peer_alert", "data": {"reason": "tier0"}}) is False


# ── 路由：读取侧过滤存量 + 回传开关 ──────────────────────────────────────────

def _mk_app(cfg):
    from fastapi import FastAPI
    from src.web.routes.unified_inbox_batch_notif_routes import (
        register_batch_notif_routes,
    )
    app = FastAPI()
    register_batch_notif_routes(app, api_auth=lambda request: None)
    app.state.config_manager = SimpleNamespace(config=cfg)
    app.state.inbox_store = None
    app.state.notif_queue = [
        {"type": "inbox_message", "data": {"preview": "客户内容"}, "_notif_ts": 1},
        {"type": "sla_alert", "data": {"conversation_id": "x"}, "_notif_ts": 2},
        {"type": "sys_status", "data": {"id": "maint", "text": "维护"}, "_notif_ts": 3},
    ]
    return app


def test_notifications_endpoint_filters_customer_rows_by_default():
    from fastapi.testclient import TestClient
    with TestClient(_mk_app({})) as c:
        d = c.get("/api/workspace/notifications").json()
    assert d["ok"] is True
    assert d["customer_messages"] is False               # 前端同口径开关
    types = [n["type"] for n in d["notifications"]]
    assert "inbox_message" not in types                  # 存量客户消息条目被滤
    assert "sla_alert" in types and "sys_status" in types


def test_notifications_endpoint_keeps_rows_when_enabled():
    from fastapi.testclient import TestClient
    with TestClient(_mk_app(ON_CFG)) as c:
        d = c.get("/api/workspace/notifications").json()
    assert d["customer_messages"] is True
    assert "inbox_message" in [n["type"] for n in d["notifications"]]


# ── 前端接线钉（模板热更新直上生产，静态钉防回退） ────────────────────────────

def test_frontend_wiring_pinned():
    base = (REPO / "src/web/templates/workspace_base.html").read_text(
        encoding="utf-8", errors="replace")
    # ① 铃铛 live 写入按服务端开关（单一事实源随 GET /notifications 回传）
    assert "__wsNotifCenterCustomerMsgs" in base
    assert "msg.type!=='inbox_message' || window.__wsNotifCenterCustomerMsgs===true" in base
    assert "d.customer_messages===true" in base
    # ② 客户消息弹窗默认关（带内容预览，隐私；提示音保留默认开）
    assert "popup:'off'" in base
    assert "sound:true" in base
    # ③ SSE 写入侧带 config 判定
    rt = (REPO / "src/web/routes/unified_inbox_realtime_routes.py").read_text(
        encoding="utf-8", errors="replace")
    assert "customer_msgs_in_center" in rt


def test_takeover_pill_cause_keys_exist_and_wired():
    """阶段2 遗留补口：让位横幅「触发原因」短语（zh+en 齐备 + 模板已消费）。"""
    from src.web.web_i18n import get_translations
    assert get_translations("zh").get("inbox.takeover.pill_cause")
    assert get_translations("en").get("inbox.takeover.pill_cause")
    tpl = (REPO / "src/web/templates/unified_inbox.html").read_text(
        encoding="utf-8", errors="replace")
    assert "inbox.takeover.pill_cause" in tpl
