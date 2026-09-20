# -*- coding: utf-8 -*-
"""R87 P1-2：AI 体检面板「为什么没回」补两条 finding——route_offline（无限制端点离线）与 abort_recent
（拦截时间线）。三条「为什么不回复」新单（#328 #329c #330）都得靠拉日志才能定，这里让面板自己说。

钉：reply_diagnosis 出 finding + recent_aborts 结构；模板 known 表 / 修复钮 / 时间线接线；词条三语齐平。
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.ai import conv_route
from src.inbox import abort_ledger as al
from src.inbox.reply_diagnosis import diagnose_conversation

_ROOT = Path(__file__).resolve().parents[1]
CID = "telegram:acct:543"
CFG_UNR = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
           "ai": {"unrestricted": {"enabled": True},
                  "fallback": {"base_url": "http://173.0.0.1:8001/v1", "model": "chatx"}}}


class _Store:
    def __init__(self, mode: str = "auto_ai"):
        self._mode = mode
        self.kv: Dict[str, str] = {}

    def get_conversation(self, cid: str) -> Dict[str, Any]:
        return {"conversation_id": cid, "platform": "telegram", "chat_type": "private",
                "display_name": "十翼", "peer_is_bot": 0}

    def get_automation_mode_if_set(self, cid: str) -> str:
        return self._mode

    def get_automation_mode_meta(self, cid: str) -> Dict[str, Any]:
        return {"mode": self._mode, "source": "human", "updated_at": 0.0}

    def list_automation_mode_log(self, cid: str, limit: int = 10) -> list:
        return []

    def list_recent_messages(self, cid: str, limit: int = 120, include_deleted: bool = True) -> list:
        return [{"direction": "out", "ts": time.time() - 30, "text": "ok"}]

    def list_drafts(self, status: str = "pending", conversation_id: str = "", limit: int = 20) -> list:
        return []

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def list_app_settings(self, prefix=""):
        return [{"key": k, "value": v} for k, v in self.kv.items() if k.startswith(prefix)]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import src.integrations.account_registry as reg
    monkeypatch.setattr(reg, "peek_account",
                        lambda p, a: ({"status": "online", "mode": "protocol", "created_at": 0.0}
                                      if str(a) == "acct" else None))
    conv_route.reset_for_tests()
    yield
    conv_route.reset_for_tests()


def _diag(store, cfg=CFG_UNR):
    return diagnose_conversation(store, cfg, platform="telegram", account_id="acct", chat_key="543")


def _find(out, code):
    return [f for f in out["findings"] if f["code"] == code]


def test_route_offline_finding_warn_with_fallback_and_hidden_when_standard(monkeypatch):
    st = _Store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    conv_route.set(st, CID, {"profile": "unrestricted"}, by="t")
    conv_route.note_offline(CID, "connect", CFG_UNR)
    out = _diag(st)
    f = _find(out, "route_offline")
    assert len(f) == 1 and f[0]["level"] == "warn"
    p = f[0]["params"]
    assert p["reason"] == "connect" and p["fallback"] is True and p["still_offline"] is True and p["n"] == 1
    assert out["route_offline"]["fallback"] is True
    # 切回标准 → 不出
    conv_route.set(st, CID, {"profile": "standard"}, by="t")
    assert not _find(_diag(st), "route_offline")


def test_route_offline_block_when_fallback_disabled(monkeypatch):
    st = _Store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    conv_route.set(st, CID, {"profile": "unrestricted"}, by="t")
    cfg = {**CFG_UNR, "ai": {**CFG_UNR["ai"], "unrestricted": {"enabled": True, "offline_fallback": False}}}
    conv_route.note_offline(CID, "no_endpoint", cfg)
    out = _diag(st, cfg)
    f = _find(out, "route_offline")
    assert f and f[0]["level"] == "block" and f[0]["params"]["fallback"] is False
    assert not _find(out, "looks_alive")


def test_abort_recent_timeline_from_ledger():
    st = _Store()
    now = time.time()
    al.record(st, conversation_id=CID, code="needs_human", stage="tag", reason="dup_guard_blocked",
              detail="sim=0.87 matched_at=07:39 rewrites=2", hit="That palm tree", ts=now - 600)
    al.record(st, conversation_id=CID, code="agent_sent", stage="batch", reason="agent_sent", ts=now - 300)
    al.record(st, conversation_id="telegram:acct:999", code="adult", stage="tag", reason="adult:explicit", ts=now - 100)
    al.record(st, conversation_id=CID, code="needs_human", stage="tag", reason="empty_reply", ts=now - 30 * 3600)  # 超 24h
    out = _diag(st)
    rows = out["recent_aborts"]
    assert [r["reason"] for r in rows] == ["agent_sent", "dup_guard_blocked"]   # 新 → 旧，只本会话，只 24h
    assert rows[1]["detail"].startswith("sim=0.87") and rows[1]["hit"] == "That palm tree"
    f = _find(out, "abort_recent")
    assert f and f[0]["level"] == "warn" and f[0]["params"]["n"] == 2 and f[0]["params"]["reason"] == "agent_sent"


def test_no_ledger_rows_no_finding():
    out = _diag(_Store())
    assert not _find(out, "abort_recent") and "recent_aborts" not in out


def test_template_wires_two_findings():
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    assert "route_offline:1,abort_recent:1" in html
    assert "function _diagAbortReason" in html and "function _diagAbortDetail" in html
    assert "inbox.diag.fix_route_standard" in html and "inbox.diag.abort_timeline" in html
    assert "__diagLast.recent_aborts" in html
    assert "inbox.diag.route_offline_fb_on" in html and "inbox.diag.route_offline_fb_off" in html


def test_diag_pack_three_langs_and_placeholders():
    from src.web.i18n_packs import diag_r87 as P
    assert set(P.ZH) == set(P.EN) == set(P.ZH_HANT)
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in P.ZH:
        assert ph(P.ZH[k]) == ph(P.EN[k]) == ph(P.ZH_HANT[k]), k
    for k, v in P.EN.items():
        assert not re.search(r"[\u4e00-\u9fff]", v), k
    for k in ("inbox.diag.route_offline", "inbox.diag.abort_recent", "inbox.diag.fix_route_standard",
              "inbox.diag.abort_timeline", "inbox.cs.dup_detail"):
        assert k in P.ZH
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    assert zh["inbox.diag.fix_route_standard"] == "切回标准"
