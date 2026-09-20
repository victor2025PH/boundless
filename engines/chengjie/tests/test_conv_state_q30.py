# -*- coding: utf-8 -*-
"""Q-30 B/C（#311 #312 #314，2026-09-12）：状态带两个新源 + W2CSGA 回放。

B  ``pacing_hold``：worker 拟人首回延迟（O-1 D first_reply hold）→ deferred / first_reply_hold，
   带 until 倒计时、无「立即接回」动作；人审档不出；让位（agent_yield）优先于它。
   **W2CSGA 回放**（whatsapp:12084403608:917983336714，2026-09-12 12:11:18 起三稿）：沉寂 9.9h 后
   首稿 hold=115s（真 worker `_first_reply_remaining` 算出来的），此前状态带全程绿色「AI 会自动回」
   → 现在琥珀「AI N 秒后发出 · 沉寂 9.9 小时后的首条回复刻意慢一点」；到点回绿。
C  ``peer_guard``：peer_bot_guard 硬拦拟稿的会话标（autodraft 跳过处写 / 放行处清）→ held，
   reason_code 原码 ``colleague:<id>`` / ``ops_group:<id>``，文案键按前缀；软停不算；需人工标优先。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.inbox import conv_state, l1_reason, peer_guard_marker, risk_hold
from src.inbox.conv_state import compute
from src.integrations.protocol_autoreply import HANDOFF_TAG

ROOT = Path(__file__).resolve().parents[1]
CID = "whatsapp:12084403608:917983336714"


class _Store:
    def __init__(self, mode="auto_ai", rows=None):
        self.kv = {}
        self.mode = mode
        self.tags = []
        self.handoff_meta = {}
        self.failed = []
        self.rows = list(rows or [])
        self.writes = 0

    def get_app_setting(self, key, default=""):
        return self.kv.get(key, default)

    def set_app_setting(self, key, value, updated_by=""):
        self.writes += 1
        if value == "":
            self.kv.pop(key, None)
        else:
            self.kv[key] = value
        return True

    def get_automation_mode(self, cid):
        return self.mode

    def get_conv_tags(self, cid):
        return list(self.tags)

    def get_handoff_meta(self, cid):
        return dict(self.handoff_meta)

    def recent_failed_outbound(self, cid, *, within_sec=86400.0, limit=10):
        return list(self.failed)[:limit]

    def list_recent_messages(self, cid, limit=50):
        return list(self.rows)[-limit:]


class _Health:
    def dump(self):
        return {"inbox_health": {}, "sessions": {}}


class _Worker:
    """假 worker：让位 + 首回延迟两个只读口。"""

    def __init__(self, yield_active=False, hold=None):
        self._yield = {"active": yield_active, "by": "agent_sent", "since": time.time() - 30,
                       "until": time.time() + 30, "remaining": 30.0, "draft_id": "dy"}
        self._hold = hold or {"active": False}

    def agent_yield_state(self, cid):
        return dict(self._yield)

    def first_reply_hold_state(self, cid):
        return dict(self._hold)


@pytest.fixture(autouse=True)
def _clean():
    l1_reason._REG.clear()
    yield
    l1_reason._REG.clear()


def _c(store, **kw):
    kw.setdefault("platform", "whatsapp")
    kw.setdefault("account_id", "12084403608")
    kw.setdefault("health", _Health())
    kw.setdefault("config", {})
    return compute(store, CID, **kw)


def _hold(remaining=104.0, silence_sec=9.9 * 3600, first_contact=False):
    return {"active": True, "until": time.time() + remaining, "remaining": remaining,
            "draft_id": "inbox:" + CID, "silence_sec": silence_sec,
            "first_contact": first_contact, "hold_sec": 115.0}


# ── B：拟人首回延迟进状态带 ─────────────────────────────────────────────

def test_pacing_hold_is_amber_deferred_with_countdown_and_no_action():
    st = _c(_Store("auto_ai"), worker=_Worker(hold=_hold()))
    assert st["state"] == "deferred" and st["tone"] == "warn" and st["will_send"] is True
    assert st["reason_code"] == "first_reply_hold"
    assert st["reason_text_key"] == "inbox.cs.deferred.first_reply"
    assert st["action"] == "none"                       # 刻意的节奏，不给「立即接回」
    assert st["until_ts"] and st["until_ts"] > time.time() + 100
    p = st["params"]
    assert p["remaining_sec"] == 104 and p["silence_h"] == 9.9 and p["hold_sec"] == 115
    assert p["first_contact"] is False and p["draft_id"] == "inbox:" + CID


def test_pacing_hold_first_contact_variant():
    st = _c(_Store("auto_ai"), worker=_Worker(hold=_hold(silence_sec=None, first_contact=True)))
    assert st["state"] == "deferred" and st["reason_code"] == "first_reply_hold"
    assert st["reason_text_key"] == "inbox.cs.deferred.first_contact"
    assert st["params"]["first_contact"] is True and st["params"].get("silence_h") is None


def test_pacing_hold_inactive_or_missing_method_is_green():
    assert _c(_Store("auto_ai"), worker=_Worker(hold={"active": False}))["state"] == "auto"

    class _Old:  # 旧 worker 没有 first_reply_hold_state
        def agent_yield_state(self, cid):
            return {"active": False}
    assert _c(_Store("auto_ai"), worker=_Old())["state"] == "auto"
    # 到点条目（remaining<=0）worker 自己判 inactive；带侧再兜一层：active=False 即不显示
    assert _c(_Store("auto_ai"), worker=_Worker(hold={"active": False, "until": time.time() - 1}))["state"] == "auto"


def test_pacing_hold_not_shown_in_review_mode():
    st = _c(_Store("review"), worker=_Worker(hold=_hold()))
    assert st["state"] == "human" and st["sources"]["pacing_hold"] is None


def test_agent_yield_beats_pacing_hold():
    st = _c(_Store("auto_ai"), worker=_Worker(yield_active=True, hold=_hold()))
    assert st["state"] == "deferred" and st["reason_code"] == "agent_yield" and st["action"] == "resume"
    assert st["sources"]["pacing_hold"] is not None       # 源仍采集，只是优先级更低


# ── W2CSGA 回放（真 worker 判定 → 真 conv_state）────────────────────────

def _make_worker(store, cfg_block=None):
    from src.inbox.autosend_worker import AutosendWorker

    class _Svc:
        def __init__(self, s):
            self._store = s

        def list_drafts(self, status="pending", limit=200):
            return []

        def resolve_with_audit(self, *a, **k):
            return {"ok": True}

    async def _sleep(_d):
        return None

    async def _send(*a, **k):
        return True

    return AutosendWorker(draft_service=_Svc(store), sleep=_sleep, send_callback=_send,
                          config={"deliver_delay": cfg_block or {"profile": "natural"}})


def test_w2csga_replay_first_reply_hold_shows_on_band_then_clears(monkeypatch):
    """回放 tmp_diag/W2CSGA：11:41 后无 abort / agent_typing / agent_sent / claim 相关日志；
    唯一闸＝`[pacing] first_reply hold conv=… silence=9.9h hold=115s remain=104s`（12:11:20）。
    12:11:18 稿在带上必须是琥珀倒计时而不是绿色「AI 会自动回」；到点后回绿。"""
    import time as _t

    t_in = 1_788_000_000.0                       # 12:11:18 客户来信（沉寂 9.9h 后）
    rows = [{"direction": "out", "ts": t_in - 9.9 * 3600, "text": "ok see you"},
            {"direction": "in", "ts": t_in, "text": "Why are you on Tinder as Alicia?"}]
    store = _Store("auto_ai", rows=rows)
    w = _make_worker(store)
    d = {"draft_id": "inbox:" + CID, "platform": "whatsapp", "account_id": "12084403608",
         "chat_key": "917983336714", "conversation_id": CID, "created_ts": t_in + 0.5,
         "peer_text": rows[-1]["text"]}
    monkeypatch.setattr(_t, "time", lambda: t_in + 2.0)     # 12:11:20 worker 首次判定
    remain = w._first_reply_remaining(d, CID)
    assert 60.0 - 2.0 <= remain <= 300.0                     # hold ∈ [60,300]（W2CSGA 实录 115s）
    hs = w.first_reply_hold_state(CID)
    assert hs["active"] and hs["first_contact"] is False and abs(hs["silence_sec"] - 9.9 * 3600) < 1.0
    assert abs(hs["remaining"] - remain) < 1e-6 and hs["draft_id"] == "inbox:" + CID

    st = _c(store, worker=w)
    assert st["state"] == "deferred" and st["reason_code"] == "first_reply_hold"
    assert st["reason_text_key"] == "inbox.cs.deferred.first_reply" and st["will_send"] is True
    assert st["params"]["silence_h"] == 9.9 and st["params"]["remaining_sec"] == int(round(remain))
    assert st["action"] == "none" and st["until_ts"] == pytest.approx(hs["until"])
    # 状态带把这段等待说成人话（zh/en/繁体三份都有键，{n} 秒倒计时由前端 deferred 分支填）
    from src.web.web_i18n import get_translations
    for lang in ("zh", "en", "zh_hant"):
        assert "{n}" in get_translations(lang)["inbox.cs.deferred.first_reply"]
        assert get_translations(lang).get("inbox.cs.deferred.first_reply_t")

    # 到点：worker 放行（remain=0，登记 pop）→ 带回绿
    monkeypatch.setattr(_t, "time", lambda: t_in + 2.0 + remain + 1.0)
    assert w._first_reply_remaining(d, CID) == 0.0
    assert w.first_reply_hold_state(CID)["active"] is False
    assert _c(store, worker=w)["state"] == "auto"


def test_w2csga_replay_report_has_no_claim_or_typing_gate():
    """报告包本身就是证据：40 分钟 backend.log 按 abort / agent_sent / claim 关键词捞，该会话
    零 abort、零 agent_typing/agent_sent defer、零认领拦发（后端从不按 claim 拦发送）；
    唯一 hold 行是 first_reply。回放包不在 → 跳过（不伪造证据）。"""
    rep = ROOT.parents[1] / "tmp_diag" / "W2CSGA" / "20260912-121428_report_report.md"
    if not rep.is_file():
        pytest.skip("W2CSGA 报告包不在本机")
    txt = rep.read_text(encoding="utf-8", errors="replace")
    conv_lines = [ln for ln in txt.splitlines() if CID in ln]
    assert any("[pacing] first_reply hold" in ln and "hold=115s" in ln for ln in conv_lines)
    assert not any("[autosend] abort" in ln or "defer=agent_typing" in ln
                   or "defer=agent_sent" in ln for ln in conv_lines)
    # 三稿都到了投递链（第二、三稿 [xlate] outbound 即在发），不是「全线不发」
    assert sum(1 for ln in conv_lines if "auto_generate_draft OK" in ln) >= 3
    assert sum(1 for ln in conv_lines if "[xlate] outbound" in ln) >= 2


# ── C：peer_bot_guard 硬拦进状态带 ──────────────────────────────────────

def test_peer_guard_marker_roundtrip_and_text_keys():
    s = _Store()
    assert peer_guard_marker.get(s, CID) is None
    rec = peer_guard_marker.mark(s, CID, reason="colleague:8852939166")
    assert rec and rec["reason"] == "colleague:8852939166" and rec["soft"] is False
    got = peer_guard_marker.get(s, CID)
    assert got and got["reason"] == "colleague:8852939166"
    assert peer_guard_marker.text_key_for("colleague:8852939166") == "inbox.cs.held.colleague"
    assert peer_guard_marker.text_key_for("ops_group:-1001") == "inbox.cs.held.ops_group"
    assert peer_guard_marker.text_key_for("tg_is_bot") == "inbox.cs.held.peer_guard"
    assert peer_guard_marker.split_reason("ops_group:-1001") == ("ops_group", "-1001")
    assert peer_guard_marker.split_reason("repeat") == ("repeat", "")
    writes = s.writes
    assert peer_guard_marker.clear(s, CID) is True and peer_guard_marker.get(s, CID) is None
    assert peer_guard_marker.clear(s, CID) is False and s.writes == writes + 1   # 标不在场不落盘
    assert peer_guard_marker.mark(None, CID, reason="x") is None
    assert peer_guard_marker.mark(s, CID, reason="") is None


def test_peer_guard_colleague_is_red_held_without_ack():
    s = _Store("auto_ai")
    peer_guard_marker.mark(s, CID, reason="colleague:8852939166")
    st = _c(s)
    assert st["state"] == "held" and st["tone"] == "danger" and st["will_send"] is False
    assert st["reason_code"] == "colleague:8852939166"
    assert st["reason_text_key"] == "inbox.cs.held.colleague"
    assert st["action"] == "none"                          # 出路是改名单，不是摘标
    assert st["params"]["id"] == "8852939166" and st["params"]["kind"] == "colleague"
    assert st["params"]["settings_url"].startswith("/reply-settings#")


def test_peer_guard_ops_group_and_generic_codes():
    s = _Store("review")                                   # 人审档也要知道「AI 根本没拟稿」
    peer_guard_marker.mark(s, CID, reason="ops_group:-100123")
    st = _c(s)
    assert st["state"] == "held" and st["reason_text_key"] == "inbox.cs.held.ops_group"
    assert st["params"]["id"] == "-100123"
    peer_guard_marker.mark(s, CID, reason="tg_is_bot")
    st = _c(s)
    assert st["state"] == "held" and st["reason_text_key"] == "inbox.cs.held.peer_guard"
    assert st["reason_code"] == "tg_is_bot" and st["params"]["code"] == "tg_is_bot"


def test_peer_guard_soft_budget_is_not_held():
    s = _Store("auto_ai")
    peer_guard_marker.mark(s, CID, reason="daily_budget", soft=True)
    assert _c(s)["state"] == "auto"


def test_needs_human_beats_peer_guard_and_peer_guard_beats_sidecar_and_yield():
    s = _Store("auto_ai")
    peer_guard_marker.mark(s, CID, reason="colleague:1")
    s.tags = [HANDOFF_TAG]
    st = _c(s)
    assert st["state"] == "held" and st["action"] == "ack" and st["reason_code"] == "needs_human"
    s.tags = []
    st = _c(s, worker=_Worker(yield_active=True, hold=_hold()))
    assert st["state"] == "held" and st["reason_code"] == "colleague:1"


def test_compute_stays_read_only_with_new_sources():
    s = _Store("auto_ai")
    peer_guard_marker.mark(s, CID, reason="colleague:1")
    n = s.writes
    _c(s, worker=_Worker(hold=_hold()))
    assert s.writes == n


def test_autodraft_marks_on_skip_and_clears_on_pass():
    """静态接线：跳过处写标、放行处清标，且都在 guard_auto_draft_action 之后、不改判定。"""
    src = (ROOT / "src" / "inbox" / "autodraft_helpers.py").read_text(encoding="utf-8")
    i = src.index("_pbg_reason, _pbg_soft = guard_auto_draft_action(")
    j = src.index("# 每会话档位：坐席显式设置 > 全局 auto_draft.automation_mode。", i)
    block = src[i:j]
    assert "from src.inbox.peer_guard_marker import mark as _pg_mark" in block
    assert "from src.inbox.peer_guard_marker import clear as _pg_clear" in block
    assert block.index("_pg_mark(") < block.index("return") < block.index("_pg_clear(")
    guard = (ROOT / "src" / "inbox" / "peer_bot_guard.py").read_text(encoding="utf-8")
    assert "peer_guard_marker" not in guard          # 守卫判定文件一字不碰


def test_ledger_names_the_true_gate_for_311():
    """落点表必须写明 #311 真闸（首回延迟，不是 claim / agent_typing）——否则下次又去查认领。
    docs 不在（拆仓 / 只有引擎目录）→ 跳过。"""
    doc = ROOT.parents[1] / "docs" / "发版对账_v1.0.85_Q30.md"
    if not doc.is_file():
        pytest.skip("发版对账文档不在本树")
    txt = doc.read_text(encoding="utf-8")
    assert "first_reply" in txt and "W2CSGA" in txt
