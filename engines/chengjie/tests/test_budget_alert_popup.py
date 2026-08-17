# -*- coding: utf-8 -*-
"""预算触顶「触发时可见」链路门禁（P3 2026-08-17）。

背景：预算熔断的横幅只长在收件箱会话内——触顶发生时坐席不开着那个会话就
零感知（198「静默哑火」修复后仅剩的盲区）。本批闭环：
peer_bot_guard._maybe_alert(daily_budget) → EventBus ``bot_peer_alert``
（payload 增带 relief 三元组 + budget 段）→ SSE 白名单放行 →
workspace_base ``__wsBudgetPop`` 中央弹窗（服务端每会话每日一次 + 前端按
会话×日 localStorage 去抖）→「今日继续」直投既有 relief 端点 →
``aitr:budget-relieved`` 广播让收件箱即时清横幅。

同批不变量：
1) 默认预算 500（2026-08-17 拍板：真人客户打不满，预算回归「对面也是 LLM」
   保险丝定位）且设置页 spec 与代码默认同值、clamp 上限恒 ≥ 默认值；
2) ``_budget_alert_extra`` 纯函数语义（非预算原因不带段 / 台账缺席按 limit
   兜底展示不失真）；
3) SSE 白名单登记 + 前端消费三件套（__wsBudgetPop / __wsModal / 确认弹窗
   替换原生 confirm）静态可见；
4) 新 i18n 键 zh/en 双语齐备（弹窗不许裸键名上生产）。
"""
from __future__ import annotations

import pathlib

import src.inbox.peer_bot_guard as pbg

_REPO = pathlib.Path(__file__).resolve().parents[1]
_BASE_TPL = _REPO / "src/web/templates/workspace_base.html"
_INBOX_TPL = _REPO / "src/web/templates/unified_inbox.html"


def _drain(q, etype: str):
    out = []
    while True:
        try:
            evt = q.get_nowait()
        except Exception:
            break
        if evt.get("type") == etype:
            out.append(evt)
    return out


def test_default_budget_is_500_and_spec_in_sync():
    assert pbg._DEFAULTS["daily_reply_budget"] == 500
    from src.inbox import reply_pacing_settings as rps
    spec = rps.FIELDS["inbox.peer_bot_guard.daily_reply_budget"]
    assert spec["default"] == 500
    # clamp 上限必须 ≥ 默认值，否则设置页快照展示的缺省本身就「超范围」
    assert spec["lo"] <= 500 <= spec["hi"]


def test_budget_alert_extra_semantics():
    cfg = pbg.parse_cfg({"inbox": {"peer_bot_guard": {
        "enabled": True, "daily_reply_budget": 500}}})
    soft = pbg.Verdict(True, "daily_budget", "", "x", budget_soft=True)
    d = pbg._budget_alert_extra(soft, cfg, 512)
    assert d == {"used": 512, "limit": 500, "soft": True, "hard": False}
    # 台账缺席（旧口径拦截）→ used 按 limit 兜底：触顶时 used>=limit 恒成立，
    # 弹窗「500/500」不失真
    hard = pbg.Verdict(True, "daily_budget", "", "x", budget_soft=False)
    d2 = pbg._budget_alert_extra(hard, cfg, None)
    assert d2 == {"used": 500, "limit": 500, "soft": False, "hard": True}
    # 非预算原因（Tier0/复读/秒回）不带 budget 段——前端只对 daily_budget 弹窗
    other = pbg.Verdict(True, "inbound_repeat", "review", "x")
    assert pbg._budget_alert_extra(other, cfg, 3) is None


def test_maybe_alert_payload_carries_triplet_and_budget():
    from src.integrations.shared.event_bus import get_event_bus
    pbg._reset_for_tests()
    bus = get_event_bus()
    q = bus.subscribe()
    try:
        v = pbg.Verdict(True, "daily_budget", "",
                        "今日自动回复500轮 ≥ 预算500（软停·转人审）",
                        budget_soft=True)
        pbg._maybe_alert(
            "telegram:acct1:777", v, platform="telegram",
            display_name="客户A", username="",
            account_id="acct1", chat_key="777",
            budget=pbg._budget_alert_extra(v, pbg.parse_cfg({"inbox": {
                "peer_bot_guard": {"enabled": True,
                                   "daily_reply_budget": 500}}}), 500))
        evts = _drain(q, "bot_peer_alert")
        assert len(evts) == 1
        data = evts[0].get("data") or {}
        # relief 三元组（弹窗「今日继续」直投 relief 端点所需）
        assert data["platform"] == "telegram"
        assert data["account_id"] == "acct1"
        assert data["chat_key"] == "777"
        assert data["reason"] == "daily_budget"
        assert data["budget"] == {"used": 500, "limit": 500,
                                  "soft": True, "hard": False}
        # 每会话每日一次：同日重报静默
        pbg._maybe_alert(
            "telegram:acct1:777", v, platform="telegram",
            account_id="acct1", chat_key="777")
        assert _drain(q, "bot_peer_alert") == []
    finally:
        bus.unsubscribe(q)
        pbg._reset_for_tests()


def test_non_budget_alert_payload_has_no_budget_section():
    from src.integrations.shared.event_bus import get_event_bus
    pbg._reset_for_tests()
    bus = get_event_bus()
    q = bus.subscribe()
    try:
        v = pbg.Verdict(True, "tg_is_bot", "manual", "@somebot",
                        persist_is_bot=1)
        pbg._maybe_alert("telegram:acct1:888", v, platform="telegram",
                         account_id="acct1", chat_key="888", budget=None)
        evts = _drain(q, "bot_peer_alert")
        assert len(evts) == 1
        assert "budget" not in (evts[0].get("data") or {})
    finally:
        bus.unsubscribe(q)
        pbg._reset_for_tests()


def test_sse_whitelist_and_frontend_wiring():
    import src.web.routes.unified_inbox_realtime_routes as rt
    assert "bot_peer_alert" in rt._SSE_EVENT_TYPES
    base = _BASE_TPL.read_text(encoding="utf-8")
    # 中央弹窗组件 + SSE 消费 + 「今日继续」直投 relief + 救济广播
    assert "window.__wsBudgetPop" in base
    assert "msg.type==='bot_peer_alert'" in base
    assert "window.__wsModal" in base
    assert "reply-budget/relief" in base
    assert "aitr:budget-relieved" in base
    # 弹窗默认焦点必须落安全按钮（focus:true 只在「知道了/取消」上），
    # SSE 重放缓冲窗内静默（防刷新弹历史）
    assert "_sseSettling()" in base
    inbox = _INBOX_TPL.read_text(encoding="utf-8")
    # 原生 confirm 已收编进结构化确认（保留 fallback 分支不算裸用）
    assert "_budgetReliefConfirm" in inbox
    assert "aitr:budget-relieved" in inbox
    # 三级视觉 + 触发闪烁 + 无障碍禁动效样式在位
    for marker in ("bb-near", "bb-soft", "bb-hard", "bb-flash",
                   "prefers-reduced-motion"):
        assert marker in inbox, marker


def test_popup_i18n_keys_bilingual():
    from src.web.i18n_packs import inbox_budget
    keys = [
        "inbox.budget.pop_title", "inbox.budget.pop_lead",
        "inbox.budget.pop_soft_note", "inbox.budget.pop_hard_note",
        "inbox.budget.pop_view_btn", "inbox.budget.pop_later_btn",
        "inbox.budget.relief_title", "inbox.budget.relief_lead",
        "inbox.budget.relief_p1", "inbox.budget.relief_p2",
        "inbox.budget.relief_p3", "inbox.budget.relief_go",
        "inbox.budget.relief_cancel",
    ]
    for k in keys:
        assert k in inbox_budget.ZH and str(inbox_budget.ZH[k]).strip(), k
        assert k in inbox_budget.EN and str(inbox_budget.EN[k]).strip(), k


def test_bell_history_wiring():
    """P3.1（2026-08-17）：预算触顶进铃铛历史——弹窗是瞬时的，错过（离线/
    别的标签页/settle 窗）就没了；铃铛队列给离线补看。三道闸钉住：

    - 类型白名单 + **内容级准入**（``_notif_content_ok``）：bot_peer_alert
      只收 reason=daily_budget——Tier0/复读/秒回属身份标注，收件箱 🤖 徽章
      与 webhook 已覆盖，进铃铛=噪音；
    - **按会话合并**（_COALESCE_NOTIF_TYPES / 前端 _COALESCE_TYPES）：SSE
      每个新连接重放 recent_events 会再次经过 _maybe_push_notif，本队列无
      conv_note 式幂等，coalesce 保最新一条即天然去重（跨日同理）。
    """
    import src.web.routes.unified_inbox_realtime_routes as rt
    assert "bot_peer_alert" in rt._NOTIF_EVENT_TYPES
    assert "bot_peer_alert" in rt._COALESCE_NOTIF_TYPES
    assert rt._notif_content_ok(
        {"type": "bot_peer_alert", "data": {"reason": "daily_budget"}})
    assert not rt._notif_content_ok(
        {"type": "bot_peer_alert", "data": {"reason": "tg_is_bot"}})
    assert not rt._notif_content_ok({"type": "bot_peer_alert", "data": {}})
    # 其他类型不受内容闸影响（维持旧行为）
    assert rt._notif_content_ok({"type": "sla_alert", "data": {}})
    base = _BASE_TPL.read_text(encoding="utf-8")
    # 前端四件套：dedupe 合并清单 / _TYPE_META 条目 / sub 文案 / 点击跳会话
    assert "bot_peer_alert:1" in base
    assert "base.notif.type_budget_hit" in base
    assert "base.notif.budget_sub_soft" in base
    assert "base.notif.budget_sub_hard" in base
    assert "n.type==='bot_peer_alert'" in base
    # P3.1 二次优化：告警 chip 能筛到触顶；「今日继续」后铃铛改写 relieved
    assert "bot_peer_alert:'alert'" in base
    assert "markBudgetRelieved" in base
    assert "budget.relieved=true" in base


def test_bell_i18n_keys_bilingual():
    from src.web.i18n_packs import workspace_shell
    for k in (
        "base.notif.type_budget_hit",
        "base.notif.budget_sub",
        "base.notif.budget_sub_soft",
        "base.notif.budget_sub_hard",
        "base.notif.budget_sub_relieved",
    ):
        assert k in workspace_shell.ZH and str(workspace_shell.ZH[k]).strip(), k
        assert k in workspace_shell.EN and str(workspace_shell.EN[k]).strip(), k
