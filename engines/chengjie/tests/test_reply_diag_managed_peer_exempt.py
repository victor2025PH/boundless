# -*- coding: utf-8 -*-
"""reply_diagnosis.managed_peer 黄牌 × peer_bot_guard.never_auto_reply.exempt_peers（2026-09-19）。

排演舞台用本租户账号扮客户：guard 侧已用 exempt_peers 放行（不当同事拦），AI 状态面板若仍举
「对方也是本系统管理的账号：两个 AI 可能互相聊起来」黄牌，教学片里「全自动运行中」旁永远挂一条
留意项，与实际放行行为对不上。契约：对端在 exempt_peers → 不出 managed_peer；不在 → 照旧出。
判定源同 peer_bot_guard.never_auto_reply_ids（含 @ 前缀 / 大小写归一）。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import pytest

from src.inbox import peer_bot_guard as pbg
from src.inbox.reply_diagnosis import diagnose_conversation


class _Store:
    def __init__(self, recent: Optional[List[Dict[str, Any]]] = None):
        self._recent = recent or [{"direction": "out", "text": "hola", "ts": time.time() - 30}]

    def get_conversation(self, cid: str) -> Dict[str, Any]:
        return {"conversation_id": cid, "platform": "telegram",
                "chat_type": "private", "display_name": "Katie", "peer_is_bot": 0}

    def get_automation_mode_if_set(self, cid: str) -> str:
        return "auto_ai"

    def get_automation_mode_meta(self, cid: str) -> Dict[str, Any]:
        return {"mode": "auto_ai", "source": "human", "updated_at": 0.0}

    def list_automation_mode_log(self, cid: str, limit: int = 10) -> list:
        return []

    def list_recent_messages(self, cid: str, limit: int = 120, include_deleted: bool = True) -> list:
        return list(self._recent[-limit:])

    def list_drafts(self, status: str = "pending", conversation_id: str = "", limit: int = 20) -> list:
        return []


@pytest.fixture(autouse=True)
def _reg(monkeypatch):
    import src.integrations.account_registry as reg
    # acct 与 8244899900 都是本机受管账号（两方同租户 = 排演舞台形态）
    monkeypatch.setattr(reg, "peek_account",
                        lambda p, a: ({"status": "online", "mode": "protocol", "created_at": 0.0}
                                      if str(a) in ("acct", "8244899900") else None))
    # never_auto_reply_ids 有 120s 缓存，按 config 身份分桶；每个用例用新 dict 即可，这里再清一次保险
    pbg._NEVER_AUTO_CACHE.update({"ts": 0.0, "key": None, "ids": None})
    yield


def _cfg(exempt=None):
    cfg: Dict[str, Any] = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True},
                                     "peer_bot_guard": {"enabled": True, "never_auto_reply": {}}}}
    if exempt is not None:
        cfg["inbox"]["peer_bot_guard"]["never_auto_reply"]["exempt_peers"] = exempt
    return cfg


def _codes(cfg, chat_key="8244899900"):
    out = diagnose_conversation(_Store(), cfg, platform="telegram", account_id="acct", chat_key=chat_key)
    return {f["code"] for f in out.get("findings", [])}


def test_managed_peer_warn_by_default():
    """未豁免：对端是受管账号 → 照旧举黄牌（互聊环提醒不能因为本改动消失）。"""
    assert "managed_peer" in _codes(_cfg())


def test_exempt_peer_no_warn():
    """exempt_peers 含对端 → 不举 managed_peer；其余诊断不受影响（仍 looks_alive）。"""
    codes = _codes(_cfg(["8244899900"]))
    assert "managed_peer" not in codes
    assert "looks_alive" in codes


def test_exempt_normalizes_at_and_case():
    """@ 前缀 / CSV 字串 / 大小写：与 guard 同一归一口径。"""
    assert "managed_peer" not in _codes(_cfg("@8244899900, other"))
    assert "managed_peer" not in _codes(_cfg(["KATIE"]), chat_key="katie")


def test_exempt_does_not_cover_other_managed_peer():
    """豁免的是名单里那一个对端，别的受管对端照旧提醒。"""
    assert "managed_peer" in _codes(_cfg(["someone_else"]))
