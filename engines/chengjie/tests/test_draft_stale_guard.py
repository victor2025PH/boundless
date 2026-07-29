# -*- coding: utf-8 -*-
"""陈旧草稿护栏：太老的稿子不许**原样**发出（会当场穿帮）。

背景（2026-07-29）：「点通过」此前只标记不发送（断链）；修好后一键即真发，于是
队列里的老稿子变成了实弹。生产实测待审年龄 **5.0h ～ 213.1h（8.9 天）**、5/7 超 24h，
内容又极度依赖当下情境（「我刚到家，娃正在客厅拼乐高」「我现在就在 Seawall 这边」）。
修断链等于激活风险，护栏必须同轮存在——本文件钉住它，也钉住**不该误拦**的那些情形。
"""

from __future__ import annotations

import time
from typing import Any, Dict

import pytest

from src.inbox.drafts import DraftService


class _Store:
    """只提供 _stale_check 需要的 get_draft / list_recent_messages 语义。"""

    def __init__(self, row: Dict[str, Any],
                 messages: Any = None) -> None:
        self._row = row
        self._messages = messages if messages is not None else []

    def get_draft(self, draft_id: str):
        return dict(self._row) if draft_id == self._row.get("draft_id") else None

    def list_recent_messages(self, conversation_id: str, *, limit: int = 50,
                             before_ts: Any = None):
        if isinstance(self._messages, Exception):
            raise self._messages
        return list(self._messages)


def _svc(age_hours: float, *, draft_id: str = "inbox:42",
         wired: bool = True, stale_h: float = 24.0,
         created: Any = None, messages: Any = None) -> DraftService:
    row = {
        "draft_id": draft_id,
        "source_kind": "inbox",
        "platform": "telegram",
        "conversation_id": "conv1",
        "status": "pending",
        "draft_text": "我刚到家，娃正在客厅拼乐高",
        "created_ts": (time.time() - age_hours * 3600.0) if created is None else created,
    }
    svc = DraftService(inbox_store=_Store(row, messages))
    if wired:
        async def _cb(_row):
            return {"ok": True}
        svc.set_inbox_deliver_callback(_cb, stale_approve_hours=stale_h)
    return svc


def _check(svc: DraftService, action: str, **kw):
    return svc._stale_check(svc.get_draft("inbox:42") or {}, action, **kw)


def test_blocks_approve_of_stale_draft():
    """8.9 天的稿子原样发＝穿帮，必须拦下并说清原因与稿龄。"""
    v = _check(_svc(213.1), "approve")
    assert v is not None
    assert v["ok"] is False and v["code"] == 409 and v["too_stale"] is True
    assert v["age_hours"] == pytest.approx(213.1, abs=0.5)
    assert v["max_age_hours"] == 24.0


def test_allows_fresh_draft():
    assert _check(_svc(5.0), "approve") is None


def test_boundary_is_inclusive_at_limit():
    """恰好等于上限不拦（阈值语义＝「超过」才拦，避免边界抖动误拦）。"""
    assert _check(_svc(23.99), "approve") is None
    assert _check(_svc(24.5), "approve") is not None


def test_edit_send_is_allowed_even_when_stale():
    """坐席已改写过文本 → 终稿是人写的，稿龄不再代表内容陈旧，放行。"""
    assert _check(_svc(500.0), "edit_send") is None


def test_reject_is_never_blocked():
    """拒绝老草稿是**清理队列**的正常操作，拦它等于让积压无法收拾。"""
    assert _check(_svc(500.0), "reject") is None


def test_force_override_is_an_escape_hatch():
    """主管明知故发的逃生门必须留。"""
    assert _check(_svc(500.0), "approve", force_override=True) is None


def test_not_blocked_when_delivery_not_wired():
    """没接线＝压根不会发出去，护栏无意义；此时拦下只会白挡坐席清队列。"""
    assert _check(_svc(500.0, wired=False), "approve") is None


def test_disabled_by_zero_threshold():
    assert _check(_svc(500.0, stale_h=0), "approve") is None


def test_missing_timestamp_does_not_block():
    """无 created_ts → 无从判断，宁可放过不误拦（老数据/异常行不该卡死坐席）。"""
    assert _check(_svc(0, created=0), "approve") is None


def test_only_inbox_kind_is_guarded():
    """LINE/WA/Messenger 渠道草稿由各自 runner 消费，不走本投递链 → 不介入。"""
    svc = _svc(500.0, draft_id="line:7")
    assert svc._stale_check(svc.get_draft("line:7") or {}, "approve") is None


def test_resolve_with_audit_returns_stale_verdict(monkeypatch):
    """端到端：resolve_with_audit 必须在**处置之前**拦下（否则又变成「标记了没发」）。"""
    svc = _svc(213.1)
    called = {"resolved": False}

    def _boom(*a, **k):
        called["resolved"] = True
        return {"ok": True}

    monkeypatch.setattr(svc, "resolve", _boom)
    out = svc.resolve_with_audit("inbox:42", "approve", by="agent1")
    assert out.get("too_stale") is True and out.get("code") == 409
    assert called["resolved"] is False, "拦下时绝不能已经把草稿标成 approved"


def _msg(direction: str, ago_h: float) -> Dict[str, Any]:
    return {"direction": direction, "ts": time.time() - ago_h * 3600.0, "text": "x"}


def test_blocks_when_already_replied_even_if_within_age_limit():
    """已经回过了 → 再原样发一遍＝重复/自相矛盾，比单纯过时更糟，必须拦。

    根因（实测）：坐席常走「采用文案→改写→手动发送」，而发送路由**不处置草稿行**，
    那行永远 pending；投递接通后任何窗口点「通过」就是再发一遍。
    """
    # 稿龄 6h（未超 24h 上限），但 1h 前已发出过回复
    v = _check(_svc(6.0, messages=[_msg("out", 1.0)]), "approve")
    assert v is not None and v["too_stale"] is True
    assert v["stale_reason"] == "replied"


def test_inbound_only_progress_does_not_block():
    """客户连发两条（纯入站推进）只说明回复迟了，原样发仍合理——拦它只会白挡坐席。"""
    assert _check(_svc(6.0, messages=[_msg("in", 0.5), _msg("in", 1.0)]),
                  "approve") is None


def test_reply_inside_grace_window_does_not_block():
    """秒回/连发的正常节奏（稿龄 < grace 2h）不拦，避免把常态当异常。"""
    assert _check(_svc(0.5, messages=[_msg("out", 0.1)]), "approve") is None


def test_reply_before_draft_does_not_block():
    """草稿生成**之前**的旧回复无关——只看生成之后有没有再回过。"""
    assert _check(_svc(6.0, messages=[_msg("out", 9.0)]), "approve") is None


def test_age_reason_reported_when_over_limit():
    v = _check(_svc(213.1, messages=[]), "approve")
    assert v["stale_reason"] == "age"


def test_message_read_failure_falls_back_to_allow():
    """读消息异常时放行（宁可放过不误拦）——护栏不该因为 DB 抖动卡死坐席。"""
    assert _check(_svc(6.0, messages=RuntimeError("db down")), "approve") is None


def test_i18n_keys_present_and_bilingual():
    """错误文案必须中英齐备，且占位符不与 tr() 形参撞名（request/key/default 禁用）。"""
    from src.web.i18n_packs.errors_stock import EN, ZH

    for pack in (ZH, EN):
        for key, need in (("err.draft.too_stale", ("{age}", "{limit}")),
                          ("err.draft.stale_replied", ("{age}",))):
            msg = pack.get(key)
            assert msg, f"{key} 缺失"
            for ph in need:
                assert ph in msg, f"{key} 缺占位符 {ph}"
            for reserved in ("{request}", "{key}", "{default}"):
                assert reserved not in msg
