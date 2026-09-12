# -*- coding: utf-8 -*-
"""Q-30 D（#308 R6S584，2026-09-12）复核结论 + 修法：「顶栏本会话全自动 vs 草稿条全局默认半自动」。

复核（代码时序）：
1. ``derive_l1_reason`` 在 ``explicit_mode == "auto_ai"`` 时**不会**推出 ``global_default``
   （③ 只对 review/multi_choice/manual 返 manual_review，其余走封顶层 / 证据 / 账号层 / 全局兜底，
   兜底只在 mode≠auto_ai 才到）——所以「显式全自动 + 全局默认」只有两条路：
   a) **陈旧稿**：稿生成于切档之前（当时无显式档、全局默认半自动 → L1 + global_default），坐席随后
      在顶栏切「全自动」；AutosendWorker 只捞 L2，这条 L1 既不发也不重拟，就一直挂着；
   b) **预算软停**：peer_bot_guard daily_budget 软停把 auto_ai 压成 review **不经 caps 层**，
      derive 兜底成 global_default——顶栏全自动、草稿条「全局默认半自动」，这次是真误标。
2. 档位写入路径正常：UI 下拉 → ``_write_automation_mode`` → ``store.set_automation_mode(source=human)``
   → ``get_automation_mode_if_set`` 立即可读（本文件用真 store 钉住）。
修法：a) 切到 auto_ai 的路由作废该会话 pending/enriching 的 L1 旧稿（``cancel_pending_l1_drafts``，
decided_by=mode_upgraded，响应 ``cancelled_l1`` → 前端 toast）；b) 软停封顶登记 ``peer_budget``。
"""
from __future__ import annotations

from pathlib import Path

from src.inbox.l1_reason import derive_l1_reason
from src.inbox.store import InboxStore

ROOT = Path(__file__).resolve().parents[1]


def _seed(store: InboxStore, cid: str, draft_id: str, level: str, status: str = "pending"):
    store.upsert_draft({
        "draft_id": draft_id, "conversation_id": cid, "platform": "telegram",
        "account_id": "vanessa", "chat_key": "juris", "source_kind": "inbox",
        "source_id": draft_id, "peer_text": "hi", "draft_text": "hello",
        "autopilot_level": level, "status": status, "risk_level": "low",
    })


def test_cancel_pending_l1_drafts_only_touches_pending_l1(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:vanessa:juris"
    _seed(store, cid, "l1-pending", "L1", "pending")
    _seed(store, cid, "l1-enrich", "L1", "enriching")
    _seed(store, cid, "l1-done", "L1", "approved")
    _seed(store, cid, "l2-pending", "L2", "pending")
    _seed(store, "telegram:vanessa:other", "l1-other", "L1", "pending")
    n = store.cancel_pending_l1_drafts(cid, decided_by="mode_upgraded")
    assert n == 2
    rows = {d["draft_id"]: d for d in store.list_drafts(conversation_id=cid, limit=20)}
    assert rows["l1-pending"]["status"] == "cancelled" and rows["l1-pending"]["decided_by"] == "mode_upgraded"
    assert rows["l1-enrich"]["status"] == "cancelled"
    assert rows["l1-done"]["status"] == "approved"          # 已处置不动
    assert rows["l2-pending"]["status"] == "pending"        # L2 是 worker 的，不动
    other = {d["draft_id"]: d for d in store.list_drafts(conversation_id="telegram:vanessa:other", limit=20)}
    assert other["l1-other"]["status"] == "pending"         # 别的会话不动
    assert store.cancel_pending_l1_drafts("", decided_by="x") == 0
    store.close()


def test_mode_write_path_is_immediately_readable(tmp_path):
    """#308 的第二种假设「档位没落库」不成立：写完即可读，且 L1 原因推导对显式 auto_ai 不出 global_default。"""
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:vanessa:juris"
    assert store.get_automation_mode_if_set(cid) is None
    try:
        store.set_automation_mode(cid, "auto_ai", source="human")
    except TypeError:
        store.set_automation_mode(cid, "auto_ai")
    assert store.get_automation_mode_if_set(cid) == "auto_ai"
    # 显式 auto_ai 且 mode 仍 auto_ai → 不是 L1，无原因码
    assert derive_l1_reason(mode="auto_ai", explicit_mode="auto_ai", conv={"conversation_id": cid},
                            store=store, peer_text="hello there how are you") == ""
    # 陈旧稿的来历：切档**之前**无显式档、全局默认半自动 → global_default
    assert derive_l1_reason(mode="review", explicit_mode=None, conv={"conversation_id": cid},
                            store=None, peer_text="hello there how are you") == "global_default"
    store.close()


def test_route_cancels_stale_l1_on_switch_to_auto_and_returns_count():
    src = (ROOT / "src" / "web" / "routes" / "unified_inbox_stored_read_routes.py").read_text(encoding="utf-8")
    i = src.index('if mode == "auto_ai":\n            agent_yield_resumed = _resume_agent_yield_q18(')
    block = src[i:i + 1600]
    assert 'cancel_pending_l1_drafts(' in block and 'decided_by="mode_upgraded"' in block
    assert '"cancelled_l1": int(cancelled_l1 or 0),' in src
    # 前端 toast 一行（inbox.mode.cancelled_l1_stale）+ 键三语齐
    html = (ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    assert "window.Tf('inbox.mode.cancelled_l1_stale',{n:Number(d.cancelled_l1)})" in html
    from src.web.web_i18n import get_translations
    for lg in ("zh", "en", "zh_hant"):
        assert "{n}" in get_translations(lg)["inbox.mode.cancelled_l1_stale"]


def test_soft_budget_cap_registers_peer_budget_not_global_default():
    src = (ROOT / "src" / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    i = src.index('_l1_reason = derive_l1_reason(')
    block = src[i:i + 1200]
    assert 'elif _pbg_soft and mode != "auto_ai"' in block and '_l1_reason = "peer_budget"' in block
    # 顺序：risk_hold > asr_suspect > peer_budget（前两者是更具体的封顶原因）
    assert block.index('"risk_hold"') < block.index('"asr_suspect"') < block.index('"peer_budget"')
    # 前端查表有键（不再落到「需确认：peer_budget」回落句）+ 三语人话
    html = (ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    assert "_L1R_KEYS.peer_budget='inbox.l1r.peer_budget';" in html
    from src.web.web_i18n import get_translations
    for lg in ("zh", "en", "zh_hant"):
        assert get_translations(lg).get("inbox.l1r.peer_budget"), lg
