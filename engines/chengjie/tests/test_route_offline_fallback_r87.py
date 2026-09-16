# -*- coding: utf-8 -*-
"""R87 #330（S5NVGQ）：会话「无限制」本机端点离线 → 不再静默零回复。

真闸回放：钧 JUN 会话 11:47–12:45 六次 ``[conv_route] unrestricted endpoint offline → 本轮不回落云端``
+ ``AI 全链失败：本轮不回复``，会话头全程绿色。本文件钉四层：

A. conv_route：``note_offline`` 开端点级离线窗；窗内 ``attach`` 对无限制会话直接按标准档装配
   （``_route_fallback`` 带原因）；``skip_for_conv`` 窗内规则全开；真端点出话 → ``record_reply`` 关窗；
   ``begin_fallback`` 只在 ``ai.unrestricted.offline_fallback``（默认 True）时改写上下文。
B. conv_state：新状态 ``route_offline``（回退开＝琥珀会发 / 关＝红不发）+ 动作 ``route_standard``；
   会话切回标准 / note 陈旧 → 不显示。
C. 词条三语齐平 + 占位符守恒。
D. 接线静态钉：skill_manager A/B 两线都调 ``begin_fallback``；模板 ``_CS_ACT_KEYS`` / ``_csAction`` /
   选项屏 ``data-off`` 都在。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from src.ai import conv_route
from src.ai.conv_route import Route
from src.inbox import conv_state

_ROOT = Path(__file__).resolve().parents[1]
CID = "telegram:7340576921:8852939166"
CFG_UNR = {"ai": {"unrestricted": {"enabled": True},
                  "fallback": {"base_url": "http://173.0.0.1:8001/v1", "model": "chatx"}}}
CFG_UNR_NOFB = {"ai": {"unrestricted": {"enabled": True, "offline_fallback": False},
                       "fallback": {"base_url": "http://173.0.0.1:8001/v1", "model": "chatx"}}}


class _KV:
    def __init__(self, mode="auto_ai"):
        self.kv = {}
        self.mode = mode

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

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_tags(self, cid):
        return []

    def get_handoff_meta(self, cid):
        return {}

    def recent_failed_outbound(self, cid, **kw):
        return []


@pytest.fixture(autouse=True)
def _reset():
    conv_route.reset_for_tests()
    yield
    conv_route.reset_for_tests()


def _unr_store():
    st = _KV()
    conv_route.set(st, CID, {"profile": "unrestricted"}, by="t")
    assert conv_route.get(st, CID).unrestricted
    return st


# ── A. conv_route ────────────────────────────────────────────────────────────

def test_default_fallback_enabled_and_config_switch():
    assert conv_route.offline_fallback_enabled(CFG_UNR) is True
    assert conv_route.offline_fallback_enabled({}) is True
    assert conv_route.offline_fallback_enabled(CFG_UNR_NOFB) is False


def test_note_offline_opens_window_and_writes_conv_note(monkeypatch):
    st = _unr_store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    assert conv_route.recent_offline(CFG_UNR) is None
    conv_route.note_offline(CID, "connect", CFG_UNR)
    rec = conv_route.recent_offline(CFG_UNR)
    assert rec and rec["reason"] == "connect"
    note = conv_route.offline_note(CID, store=st)
    assert note and note["reason"] == "connect" and note["n"] == 1 and note["fallback"] is False
    # 窗过 → 不再算「最近离线」（真端点会被再试一次）
    assert conv_route.recent_offline(CFG_UNR, now=time.time() + conv_route.OFFLINE_FALLBACK_TTL_SEC + 1) is None


def test_attach_preempts_to_standard_inside_window(monkeypatch):
    st = _unr_store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    ctx = {}
    r = conv_route.attach(ctx, CID, store=st, config=CFG_UNR)
    assert r.unrestricted and ctx.get("_unrestricted") and ctx.get("_route_strict")
    assert "_route_fallback" not in ctx
    conv_route.note_offline(CID, "timeout", CFG_UNR)
    ctx2 = {"_unrestricted": True, "_route_strict": True}
    r2 = conv_route.attach(ctx2, CID, store=st, config=CFG_UNR)
    assert r2.is_default
    assert "_unrestricted" not in ctx2 and "_route_strict" not in ctx2 and "_route" not in ctx2
    fb = ctx2.get("_route_fallback")
    assert fb and fb["from"] == "unrestricted" and fb["reason"] == "timeout" and fb["mode"] == "preemptive"
    # 回退关着 → 窗内仍按无限制装配（旧行为）
    ctx3 = {}
    r3 = conv_route.attach(ctx3, CID, store=st, config=CFG_UNR_NOFB)
    assert r3.unrestricted and ctx3.get("_route_strict") and "_route_fallback" not in ctx3


def test_guards_stay_on_during_fallback_window():
    st = _unr_store()
    assert conv_route.skip_for_conv(st, CID, "persona_guard", CFG_UNR) is True
    conv_route.note_offline(CID, "connect", CFG_UNR)
    assert conv_route.skip_for_conv(st, CID, "persona_guard", CFG_UNR) is False
    assert conv_route.skip_for_conv(st, CID, "outbound_dup", CFG_UNR) is False
    # 回退关着：窗内仍按无限制跳质量层（旧行为）
    assert conv_route.skip_for_conv(st, CID, "persona_guard", CFG_UNR_NOFB) is True


def test_record_reply_ok_closes_window():
    conv_route.note_offline(CID, "connect", CFG_UNR)
    assert conv_route.recent_offline(CFG_UNR)
    conv_route.record_reply({"unrestricted": True}, ok=False)
    assert conv_route.recent_offline(CFG_UNR)
    conv_route.record_reply({"unrestricted": True}, ok=True)
    assert conv_route.recent_offline(CFG_UNR) is None


def test_begin_fallback_rewrites_context_only_when_enabled(monkeypatch):
    st = _unr_store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    ctx = {}
    Route(profile="unrestricted").apply_context(ctx)
    ctx["_route_offline"] = "connect"
    assert conv_route.begin_fallback(ctx, CID, reason="connect", config=CFG_UNR_NOFB) is None
    assert ctx.get("_unrestricted") and ctx.get("_route_offline") == "connect"
    info = conv_route.begin_fallback(ctx, CID, reason="connect", config=CFG_UNR, store=st)
    assert info and info["mode"] == "reactive" and info["reason"] == "connect"
    assert "_unrestricted" not in ctx and "_route_strict" not in ctx and "_route_offline" not in ctx
    assert ctx["_route_fallback"] is info
    note = conv_route.offline_note(CID, store=st)
    assert note and note["fallback"] is True


def test_offline_note_ttl_and_clear():
    st = _KV()
    conv_route.mark_offline_note(CID, reason="connect", store=st, ts=time.time() - 25 * 3600)
    assert conv_route.offline_note(CID, store=st) is None          # 24h 陈旧不显
    conv_route.mark_offline_note(CID, reason="connect", store=st)
    assert conv_route.offline_note(CID, store=st)
    assert conv_route.clear_offline_note(CID, store=st) is True
    assert conv_route.offline_note(CID, store=st) is None
    assert conv_route.clear_offline_note(CID, store=st) is False   # 没 note 零写


# ── B. conv_state ────────────────────────────────────────────────────────────

def _cs(st, cfg=CFG_UNR):
    return conv_state.compute(st, CID, platform="telegram", account_id="7340576921", config=cfg)


def test_conv_state_route_offline_fallback_on_is_amber_and_actionable(monkeypatch):
    st = _unr_store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    conv_route.note_offline(CID, "connect", CFG_UNR)
    conv_route.mark_offline_note(CID, reason="connect", fallback=True, store=st)
    s = _cs(st)
    assert s["state"] == "route_offline" and s["tone"] == "warn" and s["will_send"] is True
    assert s["action"] == "route_standard"
    assert s["reason_text_key"] == "inbox.cs.route_offline"
    p = s["params"]
    assert p["reason"] == "connect" and re.match(r"^\d\d:\d\d$", p["hhmm"]) and p["still_offline"] is True
    assert s["ext"]["route_offline"]["fallback"] is True
    # 六源契约不动（route_offline 走 ext）
    assert "route_offline" not in s["sources"]


def test_conv_state_route_offline_fallback_off_is_red_hold(monkeypatch):
    st = _unr_store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    conv_route.note_offline(CID, "no_endpoint", CFG_UNR_NOFB)
    s = _cs(st, CFG_UNR_NOFB)
    assert s["state"] == "route_offline" and s["tone"] == "danger" and s["will_send"] is False
    assert s["reason_text_key"] == "inbox.cs.route_offline.hold" and s["action"] == "route_standard"


def test_conv_state_hidden_when_switched_back_or_recovered(monkeypatch):
    st = _unr_store()
    monkeypatch.setattr(conv_route, "default_store", lambda: st)
    conv_route.note_offline(CID, "connect", CFG_UNR)
    assert _cs(st)["state"] == "route_offline"
    # 切回标准 → 不显示
    conv_route.set(st, CID, {"profile": "standard"}, by="t")
    assert _cs(st)["state"] == "auto"
    # 仍无限制但端点已恢复（窗关）且 note > 15 分钟 → 不显示
    conv_route.set(st, CID, {"profile": "unrestricted"}, by="t")
    conv_route.clear_offline(CFG_UNR)
    conv_route.mark_offline_note(CID, reason="connect", store=st, ts=time.time() - 20 * 60)
    assert _cs(st)["state"] == "auto"
    # 窗关但 note 还新（< 15 分钟）→ 仍提示一阵（刚回退过的那几轮坐席要知道）
    conv_route.mark_offline_note(CID, reason="connect", store=st, ts=time.time() - 60)
    assert _cs(st)["state"] == "route_offline" and _cs(st)["params"]["still_offline"] is False


def test_conv_state_enums_extended():
    assert "route_offline" in conv_state.STATES and "route_offline" in conv_state.PRIORITY
    assert conv_state.TONE["route_offline"] == "warn"
    assert "route_standard" in conv_state.ACTIONS
    assert conv_state.PRIORITY.index("route_offline") > conv_state.PRIORITY.index("lang_unknown")
    assert conv_state.PRIORITY.index("route_offline") < conv_state.PRIORITY.index("xlate_hold")


# ── C. 词条 ──────────────────────────────────────────────────────────────────

def test_i18n_pack_three_langs_and_placeholders():
    from src.web.i18n_packs import route_offline_r87 as P
    keys = set(P.ZH)
    assert keys == set(P.EN) == set(P.ZH_HANT)
    for k in ("inbox.cs.route_offline", "inbox.cs.route_offline_t", "inbox.cs.route_offline.hold",
              "inbox.cs.route_offline.hold_t", "inbox.cs.act.route_standard",
              "inbox.cs.act.route_standard_t", "inbox.cs.route_standard_ok",
              "inbox.cs.route_standard_fail", "inbox.route.unr_offline",
              "inbox.route.unr_offline_pick"):
        assert k in keys, k
    ph = lambda s: set(re.findall(r"\{(\w+)\}", s))  # noqa: E731
    for k in keys:
        assert ph(P.ZH[k]) == ph(P.EN[k]) == ph(P.ZH_HANT[k]), k
    for k, v in P.EN.items():
        assert not re.search(r"[\u4e00-\u9fff]", v), k
    # 与全站合并不冲突
    from src.web.i18n_packs import collect_all
    zh, en, extras = collect_all()
    assert zh["inbox.cs.route_offline"] == P.ZH["inbox.cs.route_offline"]
    assert extras.get("zh_hant", {}).get("inbox.cs.act.route_standard") == P.ZH_HANT["inbox.cs.act.route_standard"]


# ── D. 接线静态钉 ─────────────────────────────────────────────────────────────

def test_skill_manager_wires_fallback_on_both_lines():
    src = (_ROOT / "src" / "skills" / "skill_manager.py").read_text(encoding="utf-8", errors="ignore")
    assert src.count("begin_fallback(") >= 2, "A 线 process_message + B 线 generate_inbox_draft 都要接"
    assert '"route_fallback": user_context.get("_route_fallback")' in src
    assert src.count('"_route_fallback"') >= 2, "两处瞬时键清理表都要带 _route_fallback"


def test_ai_client_passes_reason_to_note_offline():
    src = (_ROOT / "src" / "ai" / "ai_client.py").read_text(encoding="utf-8", errors="ignore")
    assert 'or ""), "no_endpoint")' in src
    assert "str(_ro_reason or \"\"))" in src


def test_template_wires_route_standard_action_and_offline_pick():
    html = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8", errors="ignore")
    assert "route_standard:'inbox.cs.act.route_standard'" in html
    assert "a==='route_standard'" in html and "_mpSet({profile:'standard'})" in html
    assert "s==='route_offline'" in html
    assert "inbox.route.unr_offline" in html and 'data-off="' in html
    assert "inbox.route.unr_offline_pick" in html
