# -*- coding: utf-8 -*-
"""B113（实施68 P1-17）：「一片绿却不回」定向诊断门禁。

钧 _543：会话全自动绿、客户已回「Oh really?/Haha…」、AI 未跟，右栏只有
「初识 100%+确认进阶」。核查结论——relationship_stage 的进阶确认是**纯展示层**，
回复链（skill_manager / persona_reply）从不读 needs_confirmation/pending_advancement
→ 它不 gate auto_ai。所以无闸拦下时不回，需要一档明确的诊断而非一片绿。

契约：无 block finding + 无 pending 草稿 + 末条入站且超宽限（600s）无出站
→ 出 ``customer_waiting_no_gate`` warn（带 wait_sec/effective）；否则不出
（末条是出站/等待未超宽限/有 block/有草稿 → 各归各的 finding）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from src.inbox.reply_diagnosis import diagnose_conversation


class _Store:
    def __init__(self, recent: Optional[List[Dict[str, Any]]] = None,
                 drafts: Optional[List[Dict[str, Any]]] = None,
                 mode: str = "auto_ai"):
        self._recent = recent or []
        self._drafts = drafts or []
        self._mode = mode

    def get_conversation(self, cid: str) -> Dict[str, Any]:
        return {"conversation_id": cid, "platform": "telegram",
                "chat_type": "private", "display_name": "十翼", "peer_is_bot": 0}

    def get_automation_mode_if_set(self, cid: str) -> str:
        return self._mode

    def get_automation_mode_meta(self, cid: str) -> Dict[str, Any]:
        return {"mode": self._mode, "source": "human", "updated_at": 0.0}

    def list_automation_mode_log(self, cid: str, limit: int = 10) -> list:
        return []

    def list_recent_messages(self, cid: str, limit: int = 120,
                             include_deleted: bool = True) -> list:
        return list(self._recent[-limit:])

    def list_drafts(self, status: str = "pending", conversation_id: str = "",
                    limit: int = 20) -> list:
        return list(self._drafts)


@pytest.fixture(autouse=True)
def _reg(monkeypatch):
    import src.integrations.account_registry as reg
    # 只认本账号 acct（peer chat_key=543 不是受管账号，否则会多一条 managed_peer warn）
    monkeypatch.setattr(reg, "peek_account",
                        lambda p, a: ({"status": "online", "mode": "protocol",
                                       "created_at": 0.0}
                                      if str(a) == "acct" else None))
    yield


# 全自动绿档：deliver + l2 都开（否则 telegram 非 companion 会先报 deliver_off/l2_off
# block——那本身就是「有闸」，B113 的前提是「没有任何闸」）
_CFG_AUTO = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}


def _diag(store, cfg=None):
    return diagnose_conversation(
        store, cfg if cfg is not None else _CFG_AUTO,
        platform="telegram", account_id="acct", chat_key="543")


def _codes(out):
    return {f["code"] for f in out.get("findings", [])}


def test_customer_waiting_no_gate_fires(monkeypatch):
    """末条入站 + 等了 15min + 无闸无草稿 → 定向诊断出现。"""
    store = _Store(recent=[
        {"direction": "out", "text": "在的~", "ts": time.time() - 3600},
        {"direction": "in", "text": "Oh really?", "ts": time.time() - 900},
    ])
    out = _diag(store)
    codes = _codes(out)
    assert "customer_waiting_no_gate" in codes
    assert "looks_alive" in codes   # 仍是 ok（无 block），只是多一条 warn
    f = next(f for f in out["findings"] if f["code"] == "customer_waiting_no_gate")
    assert f["level"] == "warn"
    assert f["params"]["wait_sec"] >= 600
    assert out["silent_wait_sec"] >= 600


def test_no_fire_when_last_is_outbound():
    """末条是 AI 出站（已回过）→ 不是「客户在等」，不出诊断。"""
    store = _Store(recent=[
        {"direction": "in", "text": "Haha", "ts": time.time() - 1200},
        {"direction": "out", "text": "哈哈是呀", "ts": time.time() - 60},
    ])
    assert "customer_waiting_no_gate" not in _codes(_diag(store))


def test_no_fire_within_grace():
    """末条入站但只等了 2min（宽限内，可能正在生成）→ 不误报。"""
    store = _Store(recent=[
        {"direction": "in", "text": "在吗", "ts": time.time() - 120},
    ])
    out = _diag(store)
    assert "customer_waiting_no_gate" not in _codes(out)
    assert out["silent_wait_sec"] < 600


def test_no_fire_when_pending_drafts_exist():
    """有待审草稿 → 归 pending_drafts（AI 已拟稿等人审），不重复报「无闸不回」。"""
    store = _Store(
        recent=[{"direction": "in", "text": "Oh really?",
                 "ts": time.time() - 3600}],
        drafts=[{"created_at": time.time() - 3600}])
    codes = _codes(_diag(store))
    assert "pending_drafts" in codes
    assert "customer_waiting_no_gate" not in codes


def test_no_fire_when_blocked():
    """有 block（如显式非全自动）→ 那才是不回的原因，不叠加「无闸不回」。"""
    store = _Store(
        recent=[{"direction": "in", "text": "Oh really?",
                 "ts": time.time() - 3600}],
        mode="manual")
    codes = _codes(_diag(store))
    assert "explicit_non_auto" in codes
    assert "customer_waiting_no_gate" not in codes
