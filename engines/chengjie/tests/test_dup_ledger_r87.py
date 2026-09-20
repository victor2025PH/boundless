# -*- coding: utf-8 -*-
"""R87 #331（X9B22T）：节奏页「最近 24 小时」原因列直出 dup_guard_blocked——人话 + 细节 + 二次改写。

钉四层：
A. abort_ledger：``record(detail=)`` 落 ``detail``；``annotate_last`` 给某会话最近一条同 reason 的行补
   hit / detail / draft_id（超窗不补）；``summary.recent`` 带 detail / draft_id。
B. outbound_dup_guard：``build_dup_rewrite_prompt(attempt=2)`` 是更硬的换角度指令；``attach_rewrite_fn``
   同时挂 ``rewrite_fn_alt``（温度 0.95，旧签名回落）。
C. autosend_worker：第一次重写仍雷同 → 第二次 ``attempt=2`` 用 ``rewrite_fn_alt`` 再试一次才转人工；
   ``_dup_blocked_escalate`` 打标后 ``annotate_last`` 补 sim / matched_at / rewrites。
D. reply_settings.html：``RPS_AL_I18N.sub`` 子原因人话 + ``rpsAlSubText`` / ``rpsAlDetailText`` + 去会话链接；
   词条三语齐平。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from src.inbox import abort_ledger as al
from src.inbox import outbound_dup_guard as odg

_ROOT = Path(__file__).resolve().parents[1]
CID = "whatsapp:19892968016:15635715247"


class _KV:
    def __init__(self):
        self.kv = {}

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.kv[key] = value
        return True


# ── A ────────────────────────────────────────────────────────────────────────

def test_record_detail_and_summary_surfaces_it():
    st = _KV()
    assert al.record(st, conversation_id=CID, code="needs_human", stage="tag", reason="dup_guard_blocked",
                     detail="sim=0.87 matched_at=07:39 rewrites=2\nx")
    s = al.summary(st)
    r = s["recent"][0]
    assert r["reason"] == "dup_guard_blocked" and r["detail"].startswith("sim=0.87") and "\n" not in r["detail"]
    assert "draft_id" in r


def test_annotate_last_fills_hit_detail_draft_only_recent_same_reason():
    st = _KV()
    now = time.time()
    al.record(st, conversation_id=CID, code="needs_human", stage="tag", reason="dup_guard_blocked", ts=now - 60)
    al.record(st, conversation_id="other:1:2", code="needs_human", stage="tag", reason="dup_guard_blocked", ts=now - 30)
    al.record(st, conversation_id=CID, code="needs_human", stage="tag", reason="empty_reply", ts=now - 10)
    assert al.annotate_last(st, conversation_id=CID, reason="dup_guard_blocked",
                            hit="That palm tree tank suits you", detail="sim=0.90 matched_at=07:39 rewrites=2",
                            draft_id="d1", now=now)
    rows = al.rows(st)
    mine = [r for r in rows if r["conv"] == CID and r["reason"] == "dup_guard_blocked"][0]
    assert mine["hit"].startswith("That palm") and mine["detail"].startswith("sim=0.90") and mine["draft_id"] == "d1"
    other = [r for r in rows if r["conv"] == "other:1:2"][0]
    assert not other.get("detail")
    # 超窗不补
    assert al.annotate_last(st, conversation_id=CID, reason="dup_guard_blocked", detail="late",
                            now=now + 3600) is False
    # 没有该 reason 的行 → False
    assert al.annotate_last(st, conversation_id=CID, reason="nope", detail="x", now=now) is False


# ── B ────────────────────────────────────────────────────────────────────────

def test_rewrite_prompt_attempt2_is_harder():
    p1 = odg.build_dup_rewrite_prompt("hello there")
    p2 = odg.build_dup_rewrite_prompt("hello there", attempt=2)
    assert p1 != p2 and "第二次" in p2 and "hello there" in p2 and "hello there" in p1
    assert len(p2) > len(p1)


def test_attach_rewrite_fn_adds_alt_with_temperature():
    calls = []

    class _AI:
        async def rewrite_local(self, system_prompt, user_text, *, temperature=0.7, **kw):
            calls.append((system_prompt, temperature))
            return user_text + " (rw)"

    cfg = odg.attach_rewrite_fn({"rewrite_retry": True}, _AI())
    assert callable(cfg["rewrite_fn"]) and callable(cfg["rewrite_fn_alt"])
    asyncio.run(cfg["rewrite_fn"]("x", "m"))
    asyncio.run(cfg["rewrite_fn_alt"]("x", "m"))
    assert calls[0][1] == 0.7 and calls[1][1] == 0.95 and "第二次" in calls[1][0]

    class _Old:
        async def rewrite_local(self, system_prompt, user_text):
            return "ok"

    cfg2 = odg.attach_rewrite_fn({}, _Old())
    assert asyncio.run(cfg2["rewrite_fn_alt"]("x", "m")) == "ok"   # 旧签名回落


# ── C ────────────────────────────────────────────────────────────────────────

def test_worker_second_rewrite_then_escalate_wired():
    src = (_ROOT / "src" / "inbox" / "autosend_worker.py").read_text(encoding="utf-8", errors="ignore")
    i1 = src.index("_rw = await self._try_dup_rewrite(\n                        item, send_text, _hit, since_ts=_since, rows=_rows)")
    i2 = src.index("rows=_rows, attempt=2)")
    i3 = src.index("self._dup_blocked_escalate(item, _hit, _rows)")
    assert i1 < i2 < i3, "第二次重写必须在第一次之后、转人工之前"
    assert 'self._dup_guard_cfg.get("rewrite_fn_alt") or _rf' in src
    assert "_al.annotate_last(" in src and 'reason="dup_guard_blocked"' in src
    assert 'detail="sim=%.2f matched_at=%s rewrites=%d"' in src


def test_worker_try_dup_rewrite_attempt2_uses_alt(monkeypatch):
    from src.inbox.autosend_worker import AutosendWorker
    seen = {}

    async def _fake_attempt(**kw):
        seen.update(kw)
        return "rw"

    monkeypatch.setattr(odg, "attempt_dup_rewrite", _fake_attempt)

    class _W:
        _dup_guard_cfg = {"rewrite_fn": "primary", "rewrite_fn_alt": "alt"}

        def _dup_guard_rows(self, conv):
            return []

    f = AutosendWorker._try_dup_rewrite
    asyncio.run(f(_W(), {"conversation_id": CID}, "t", {"matched_text": "m"}, rows=[]))
    assert seen["rewrite_fn"] == "primary" and seen["source"] == "autosend"
    asyncio.run(f(_W(), {"conversation_id": CID}, "t", {"matched_text": "m"}, rows=[], attempt=2))
    assert seen["rewrite_fn"] == "alt" and seen["source"] == "autosend_retry2"


# ── D ────────────────────────────────────────────────────────────────────────

def test_reply_settings_template_humanizes_sub_reason():
    html = (_ROOT / "src" / "web" / "templates" / "reply_settings.html").read_text(encoding="utf-8", errors="ignore")
    assert "rps_al_sub_dup_guard_blocked" in html and "function rpsAlSubText" in html and "function rpsAlDetailText" in html
    assert "rps_al_dup_detail" in html and "rps_al_go_conv" in html
    assert "/workspace?conv=" in html
    # 原码只留 title：原因单元格 title 带 reason
    assert "title=\"' + rpsEsc(String(x.reason || x.code || ''))" in html


def test_abort_ledger_r87_pack_three_langs():
    from src.web.i18n_packs import abort_ledger_r87 as P
    assert set(P.ZH) == set(P.EN) == set(P.ZH_HANT)
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in P.ZH:
        assert ph(P.ZH[k]) == ph(P.EN[k]) == ph(P.ZH_HANT[k]), k
    for k, v in P.EN.items():
        assert not re.search(r"[\u4e00-\u9fff]", v), k
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    assert zh["rps_al_sub_dup_guard_blocked"] == P.ZH["rps_al_sub_dup_guard_blocked"]
