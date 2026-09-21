# -*- coding: utf-8 -*-
"""出站事实门（P0-1 2026-09-21，Bug #341/#342）· 判定 + autosend 接线门禁。

锁定：
  - 「你妈妈 / your mom」类第三方点名：客户原话 / 口述事实里没这个人 → 拦；有 → 放；
  - AI 推断事实不算依据（provenance 分层——装配层只喂 user_stated，这里锁纯函数口径）；
  - 「your client」运营称谓无条件拦；
  - 客户否认（「我没说过」）之后 deny_turns 轮内再提同一个人 → 拦，哪怕支撑集里有；
  - 不点名任何第三方 / 说 AI 自己的家人（「我妈」）→ 不管；
  - worker：命中且重写救回 → 发重写稿并回写 item text；未救回 → 不发、不计投递错误、
    打「需人工」(fact_gate_blocked)、计数进快照；真失败重试免检；门关 = 零行为。
"""

from __future__ import annotations

import uuid

import pytest

from src.inbox import outbound_fact_gate as fg
from src.inbox.autosend_worker import AutosendWorker


def _rows(*pairs):
    out = []
    for i, (direction, text) in enumerate(pairs):
        out.append({"direction": direction, "text": text, "ts": 1000.0 + i, "status": "sent"})
    return out


# ── 判定 ─────────────────────────────────────────────────────────────────────

def test_ungrounded_mother_blocked_zh_and_en():
    rows = _rows(("in", "今天好累，加班到十点"), ("out", "辛苦了，早点休息"))
    hit = fg.check("你妈妈的装修弄好了吗？", rows)
    assert hit and hit["kind"] == "ungrounded_third_party" and "妈" in hit["hit"]
    hit = fg.check("How is your mom's renovation going?", rows)
    assert hit and hit["kind"] == "ungrounded_third_party"


def test_grounded_by_peer_text_passes():
    rows = _rows(("in", "我妈最近在装修房子，烦死了"), ("out", "装修确实累人"))
    assert fg.check("你妈妈的装修弄好了吗？", rows) is None
    rows = _rows(("in", "my mother is visiting next week"))
    assert fg.check("Is your mom still visiting next week?", rows) is None


def test_grounded_by_user_stated_fact_passes_but_not_by_empty_facts():
    rows = _rows(("in", "在忙"))
    assert fg.check("你女儿放学了吗", rows, facts=["有一个 6 岁的女儿"]) is None
    assert fg.check("你女儿放学了吗", rows, facts=[]) is not None


def test_perspective_leak_blocked_regardless_of_support():
    rows = _rows(("in", "my mom is here"))
    hit = fg.check("Tell your client's mom I said hi", rows)
    assert hit and hit["kind"] == "perspective_leak"
    hit = fg.check("你的客户今天有空吗", rows)
    assert hit and hit["kind"] == "perspective_leak"


def test_denied_recall_blocked_even_when_supported():
    rows = _rows(
        ("in", "我妈身体不太好"),
        ("out", "你妈妈现在好点了吗"),
        ("in", "我没说过我妈啊，你在说什么"),
        ("out", "抱歉我记混了"),
    )
    hit = fg.check("你妈妈今天怎么样？", rows)
    assert hit and hit["kind"] == "denied_recall"
    # 否认期耗尽后解禁（deny_turns=1：否认后已有 1 条出站）
    assert fg.check("你妈妈今天怎么样？", rows, deny_turns=1) is None


def test_denial_only_counts_when_prev_out_named_the_entity():
    rows = _rows(("in", "我妈来了"), ("out", "周末打算做什么"), ("in", "你在说什么"))
    assert fg.denied_groups(rows) == set()
    assert fg.check("你妈妈来玩几天？", rows) is None


def test_no_third_party_or_ai_own_family_ignored():
    rows = _rows(("in", "hi"))
    assert fg.check("我妈今天让我早点回家，你呢？", rows) is None
    assert fg.check("Today was long, how was yours?", rows) is None
    assert fg.check("", rows) is None
    assert fg.mentioned_groups("my mom called") == {}


def test_check_never_raises_on_garbage():
    assert fg.check("你妈妈好吗", [None, 1, {"direction": "in", "ts": "x"}], None) is not None
    assert fg.check("hello", None, None) is None


def test_resolve_cfg_defaults_on_and_overrides():
    c = fg.resolve_cfg({})
    assert c["enabled"] is True and c["deny_turns"] == fg.DEFAULT_DENY_TURNS
    c = fg.resolve_cfg({"inbox": {"outbound_fact_gate": {"enabled": False, "deny_turns": 3}}})
    assert c["enabled"] is False and c["deny_turns"] == 3
    assert fg.attach_sources(None) is None


def test_build_rewrite_prompt_mentions_entity():
    p = fg.build_rewrite_prompt({"kind": "ungrounded_third_party", "hit": "你妈妈"})
    assert "你妈妈" in p and "删掉" in p
    p = fg.build_rewrite_prompt({"kind": "perspective_leak", "hit": "your client"})
    assert "your client" in p


# ── worker 接线 ───────────────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def list_recent_messages(self, conv, limit=200):
        return list(self.rows)


class _FakeSvc:
    def __init__(self, rows=None):
        self.queue = []
        self.resolved = []
        self._store = _FakeStore(rows or [])

    def list_drafts(self, status="pending", limit=200):
        batch, self.queue = self.queue, []
        return batch

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append((draft_id, action))
        return {"ok": True}


def _draft(conv, text, draft_id="d1"):
    return {
        "draft_id": draft_id, "autopilot_level": "L2",
        "final_text": text, "platform": "whatsapp",
        "account_id": "a1", "chat_key": "c1", "conversation_id": conv,
    }


def _worker(svc, sent, *, enabled=True, rewrite=None, facts=None, fail_first=False):
    calls = {"n": 0}

    async def _send_cb(platform, account_id, chat_key, text):
        calls["n"] += 1
        if fail_first and calls["n"] == 1:
            raise RuntimeError("boom")
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    cfg = {"enabled": enabled, "history_limit": 50, "deny_turns": 10, "rewrite_retry": True}
    if rewrite is not None:
        cfg["rewrite_fn"] = rewrite
    if facts is not None:
        cfg["facts_fn"] = lambda p, a, c: list(facts)
    return AutosendWorker(draft_service=svc, send_callback=_send_cb, sleep=_sleep,
                          fact_gate_cfg=cfg)


def _conv():
    return f"test-fg-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def tagged(monkeypatch):
    calls = []

    def _tag(store, conv_ref, reason="", source=""):
        calls.append(reason)
        return True

    import src.integrations.protocol_autoreply as pa
    monkeypatch.setattr(pa, "tag_needs_human", _tag)
    return calls


@pytest.mark.asyncio
async def test_worker_blocks_ungrounded_and_flags_needs_human(tagged):
    svc = _FakeSvc(_rows(("in", "今天上班好累")))
    sent = []
    w = _worker(svc, sent)
    svc.queue = [_draft(_conv(), "你妈妈的装修怎么样了？")]
    await w._tick()
    assert sent == []
    assert w.total_fact_gate_blocked == 1
    assert w.total_deliver_errors == 0 and w._consecutive_errors == 0
    assert tagged == ["fact_gate_blocked"]
    snap = w.status_snapshot()
    assert snap["total_fact_gate_blocked"] == 1 and snap["fact_gate_enabled"] is True


@pytest.mark.asyncio
async def test_worker_rewrite_rescues_and_rewrites_item_text(tagged):
    svc = _FakeSvc(_rows(("in", "今天上班好累")))
    sent = []

    async def _rw(text, prompt):
        assert "你妈妈" in prompt
        return "装修的事进展如何？"

    w = _worker(svc, sent, rewrite=_rw)
    svc.queue = [_draft(_conv(), "你妈妈的装修怎么样了？")]
    await w._tick()
    assert sent == ["装修的事进展如何？"]
    assert w.total_fact_gate_rewritten == 1 and w.total_fact_gate_blocked == 0
    assert tagged == []


@pytest.mark.asyncio
async def test_worker_rewrite_still_ungrounded_blocks(tagged):
    svc = _FakeSvc(_rows(("in", "hi")))
    sent = []

    async def _rw(text, prompt):
        return "Anyway, how's your mom?"

    w = _worker(svc, sent, rewrite=_rw)
    svc.queue = [_draft(_conv(), "How's your mom doing today?")]
    await w._tick()
    assert sent == [] and w.total_fact_gate_blocked == 1 and tagged == ["fact_gate_blocked"]


@pytest.mark.asyncio
async def test_worker_grounded_by_facts_passes(tagged):
    svc = _FakeSvc(_rows(("in", "hi")))
    sent = []
    w = _worker(svc, sent, facts=["has a daughter, 6 years old"])
    svc.queue = [_draft(_conv(), "Is your daughter back from school?")]
    await w._tick()
    assert len(sent) == 1 and w.total_fact_gate_blocked == 0


@pytest.mark.asyncio
async def test_worker_gate_disabled_is_noop(tagged):
    svc = _FakeSvc(_rows(("in", "hi")))
    sent = []
    w = _worker(svc, sent, enabled=False)
    svc.queue = [_draft(_conv(), "你妈妈的装修怎么样了？")]
    await w._tick()
    assert len(sent) == 1 and w.total_fact_gate_blocked == 0
    assert w.status_snapshot()["fact_gate_enabled"] is False


@pytest.mark.asyncio
async def test_worker_true_retry_skips_gate(tagged):
    """第一次真发失败 → 重试项免检（recoverable 语义与 dup_guard 一致）。"""
    svc = _FakeSvc(_rows(("in", "我妈在装修")))
    sent = []
    calls = {"n": 0}

    async def _send_cb(platform, account_id, chat_key, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient boom")
        sent.append(text)
        return {"ok": True}

    async def _sleep(d):
        return None

    w = AutosendWorker(
        draft_service=svc, send_callback=_send_cb, sleep=_sleep,
        fact_gate_cfg={"enabled": True},
        config={"recoverable": {"enabled": True, "max_attempts": 3,
                                "backoff_base_sec": 0, "backoff_max_sec": 0}},
    )
    svc.queue = [_draft(_conv(), "你妈妈的装修怎么样了？")]
    await w._tick()
    assert sent == [] and w.total_retry_scheduled == 1
    # 客户原话此刻「消失」（模拟回看窗滑走）——真失败重试仍免检照发
    svc._store.rows = []
    await w._tick()
    assert sent == ["你妈妈的装修怎么样了？"] and w.total_fact_gate_blocked == 0
