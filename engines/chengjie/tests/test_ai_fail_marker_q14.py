# -*- coding: utf-8 -*-
"""Q-14 #262 B：「AI 本轮未生成」对坐席可见——灰标 marker（app_settings KV，不动 store.py）、
起草侧接住点 ``consume_and_mark`` 一行 ``[ai] fail`` + 灰标、成功即清；``reply_diagnosis``
出 ``ai_last_fail`` finding；i18n 三处词条在包里。本文件不触网、不建库。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import pytest

from src.inbox import ai_fail_marker as m
from src.inbox.reply_diagnosis import diagnose_conversation


class _KV:
    """InboxStore.app_settings 三个方法的最小替身。"""

    def __init__(self):
        self.d: Dict[str, str] = {}

    def get_app_setting(self, key, default=""):
        return self.d.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        if not str(value or "").strip():
            self.d.pop(key, None)
        else:
            self.d[key] = str(value)
        return True

    def list_app_settings(self, prefix=""):
        return [{"key": k, "value": v} for k, v in sorted(self.d.items()) if k.startswith(prefix)]


class _AI:
    def __init__(self, recs: Optional[Dict[str, Dict[str, Any]]] = None):
        self._last_fail = dict(recs or {})

    def pop_last_fail(self, conv=""):
        return self._last_fail.pop(conv, None)


def test_mark_get_clear_roundtrip():
    st = _KV()
    rec = m.mark(st, "telegram:a:1", reason="timeout", latency_ms=60123, attempt=1, draft_id="d1")
    assert rec and rec["reason"] == "timeout"
    got = m.get(st, "telegram:a:1")
    assert got["reason"] == "timeout" and got["draft_id"] == "d1" and got["latency_ms"] == 60123
    assert m.get(st, "telegram:a:2") is None
    m.mark(st, "telegram:a:2", reason="weird")
    assert m.get(st, "telegram:a:2")["reason"] == "other"     # 未知码归 other
    assert set(m.all_marks(st)) == {"telegram:a:1", "telegram:a:2"}
    assert m.clear(st, "telegram:a:1") is True
    assert m.get(st, "telegram:a:1") is None
    assert m.mark(None, "x", reason="timeout") is None          # store 缺席不抛
    assert m.mark(st, "", reason="timeout") is None


def test_consume_and_mark_logs_one_line_and_marks(caplog):
    st = _KV()
    ai = _AI({"telegram:a:9": {"reason": "connect", "ts": 1700000000.0,
                               "latency_ms": 5200, "attempt": 2, "model": "m", "request_id": "r9"}})
    with caplog.at_level(logging.ERROR):
        rec = m.consume_and_mark(ai, st, "telegram:a:9", draft_id="dr9")
    assert rec["reason"] == "connect" and rec["attempt"] == 2 and rec["draft_id"] == "dr9"
    lines = [r.getMessage() for r in caplog.records if "[ai] fail" in r.getMessage()]
    assert len(lines) == 1
    assert "conv=telegram:a:9" in lines[0] and "reason=connect" in lines[0]
    assert "stage=autodraft" in lines[0] and "draft_id=dr9" in lines[0]
    assert ai._last_fail == {}                                  # 一次性取走
    # ai_client 无记录（失败在 AI 之外）→ unknown 仍可见
    rec2 = m.consume_and_mark(ai, st, "telegram:a:10", draft_id="")
    assert rec2["reason"] == "unknown"


def test_hhmm_formats_local_time():
    ts = time.mktime((2026, 9, 8, 23, 41, 0, 0, 0, -1))
    assert m.hhmm(ts) == "23:41"
    assert m.hhmm("junk") == "--:--"


# ── reply_diagnosis：ai_last_fail finding ──────────────────────────────────

class _Store(_KV):
    def __init__(self):
        super().__init__()

    def get_conversation(self, cid):
        return {"conversation_id": cid, "platform": "telegram", "chat_type": "private",
                "display_name": "十翼", "peer_is_bot": 0}

    def get_automation_mode_if_set(self, cid):
        return "auto_ai"

    def get_automation_mode_meta(self, cid):
        return {"mode": "auto_ai", "source": "human", "updated_at": 0.0}

    def list_automation_mode_log(self, cid, limit=10):
        return []

    def list_recent_messages(self, cid, limit=120, include_deleted=True):
        return [{"direction": "out", "ts": time.time()}]

    def list_drafts(self, status="pending", conversation_id="", limit=20):
        return []


@pytest.fixture(autouse=True)
def _reg(monkeypatch):
    import src.integrations.account_registry as reg
    monkeypatch.setattr(reg, "peek_account",
                        lambda p, a: ({"status": "online", "mode": "protocol", "created_at": 0.0}
                                      if str(a) == "acct" else None))
    yield


_CFG = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}


def test_reply_diagnosis_reports_ai_last_fail():
    st = _Store()
    cid = "telegram:acct:543"
    ts = time.time() - 120
    m.mark(st, cid, reason="timeout", ts=ts, latency_ms=60010, attempt=1, draft_id="d5")
    out = diagnose_conversation(st, _CFG, platform="telegram", account_id="acct", chat_key="543")
    f = next(x for x in out["findings"] if x["code"] == "ai_last_fail")
    assert f["level"] == "warn"
    assert f["params"]["reason"] == "timeout" and f["params"]["draft_id"] == "d5"
    assert f["params"]["hhmm"] == m.hhmm(ts)
    assert 100 <= f["params"]["ago_sec"] <= 200
    assert out["ai_last_fail"]["latency_ms"] == 60010
    # 成功后清 → 不再出现
    m.clear(st, cid)
    out2 = diagnose_conversation(st, _CFG, platform="telegram", account_id="acct", chat_key="543")
    assert not any(x["code"] == "ai_last_fail" for x in out2["findings"])


def test_i18n_keys_present_zh_en():
    from src.web.i18n_packs.inbox_takeover_diag import ZH, EN
    for pack in (ZH, EN):
        assert "inbox.diag.ai_last_fail" in pack
        assert "inbox.aif.chip" in pack and "inbox.aif.retry" in pack
        for r in m.REASONS:
            assert f"inbox.aif.reason.{r}" in pack
        # 不引入罐头句：词条只描述失败，不含可发给客户的应答
        assert "稍等" not in pack["inbox.diag.ai_last_fail"]


def test_template_wires_chip_and_diag_code():
    from pathlib import Path
    html = (Path(__file__).resolve().parent.parent / "src/web/templates/unified_inbox.html").read_text(
        encoding="utf-8")
    assert 'id="aif-chip"' in html and "function _aifRender(" in html
    assert "d.ai_last_fail" in html
    assert "ai_last_fail:1" in html          # 体检面板 known 码表
    assert "/api/drafts/'+enc(did)+'/regenerate" in html
