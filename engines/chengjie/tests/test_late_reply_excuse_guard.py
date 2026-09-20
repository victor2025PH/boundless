"""O-1 E（#255 8FJDUK ①）：>72h 沉寂后的首回不编造迟回理由。

事故：客户 7 天后收到「Sorry, I've been buried in work」——AI 不知道这 7 天发生了什么。钉住：
  - 三句编造理由（buried in work / been so busy / phone died；忙死了 / 手机坏了 / 出差）被剥；
  - 「刚看到 / just saw this」是事实陈述，放行；含理由的句子里事实半句保留；
  - 整段都是理由 → 换一句如实「刚看到」（按文字系统选语种）；
  - 对方的「are you busy?」不误伤；
  - enrich_draft：沉寂 ≥72h 才剥（<72h / 首次接触 / 无出站 → 原稿不动）+ [persona-guard] late_excuse 日志；
  - 协议链同守卫（静态钉）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from src.utils import persona_guard as pg

ROOT = Path(__file__).resolve().parents[1]


def test_three_fabricated_excuses_are_stripped():
    out, rep = pg.strip_late_reply_excuses("Sorry, I've been buried in work. How have you been?")
    assert out == "How have you been?" and rep["action"] == "strip"
    assert any("buried in work" in h for h in rep["hits"])
    out, rep = pg.strip_late_reply_excuses("Hey! Been so busy this week. Miss you.")
    assert out == "Hey! Miss you." and rep["action"] == "strip"
    out, rep = pg.strip_late_reply_excuses("My phone died and I only got it back today. What's new with you?")
    assert out == "What's new with you?" and rep["action"] == "strip"
    out, rep = pg.strip_late_reply_excuses("不好意思，这几天忙死了。你最近怎么样？")
    assert out == "你最近怎么样？" and rep["action"] == "strip"
    out, rep = pg.strip_late_reply_excuses("手机坏了刚修好。想你了。")
    assert out == "想你了。" and rep["action"] == "strip"
    out, rep = pg.strip_late_reply_excuses("前几天出差了，没看到消息。你吃饭了吗？")
    assert out == "你吃饭了吗？" and rep["action"] == "strip"


def test_honest_just_saw_this_passes():
    for t in ("Just saw this. How are you?", "刚看到。你还好吗？", "Sorry for the late reply, how was your weekend?",
              "才看到你的消息，今天过得怎么样？"):
        out, rep = pg.strip_late_reply_excuses(t)
        assert out == t and rep["action"] == "clean", t
    # 理由 + 事实同句：只留事实那半句
    out, rep = pg.strip_late_reply_excuses("Just saw this, I've been so busy. How are you?")
    assert rep["action"] == "strip" and "busy" not in out and out.startswith("Just saw this")
    assert out.endswith("How are you?")


def test_all_excuse_replaced_with_honest_line():
    out, rep = pg.strip_late_reply_excuses("Sorry, I've been so busy with work.")
    assert out == "Just saw this." and rep["action"] == "replace"
    out, rep = pg.strip_late_reply_excuses("这几天忙死了，手机也坏了。")
    assert out == "刚看到。" and rep["action"] == "replace"
    out, rep = pg.strip_late_reply_excuses("I've been swamped.", lang="ja")
    assert out == "今見た。" and rep["action"] == "replace"


def test_peer_question_and_everyday_text_not_hit():
    for t in ("Are you busy tonight?", "You've been busy lately, huh?", "Is your phone dead?",
              "你今天忙不忙？", "That movie was terrible though.", "我明天要出差，你呢？" if False else "晚上一起吃饭？"):
        assert pg.detect_late_reply_excuses(t) == [], t
        out, rep = pg.strip_late_reply_excuses(t)
        assert out == t and rep["action"] == "clean"
    assert pg.strip_late_reply_excuses("")[1]["action"] == "clean"
    assert pg.strip_late_reply_excuses(None)[1]["action"] == "clean"   # type: ignore[arg-type]


# ── enrich_draft 接线 ─────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    from src.inbox.store import InboxStore
    s = InboxStore(tmp_path / "o1e.db")
    yield s
    s.close()


def _conv(cid="telegram:acct1:u1", ck="u1"):
    return {"conversation_id": cid, "platform": "telegram", "account_id": "acct1",
            "chat_key": ck, "display_name": "T"}


def _seed(store, cid, *, gap_hours, first_contact=False):
    from src.inbox.models import InboxMessage
    now = time.time()
    if not first_contact:
        store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="o1", direction="out",
                                          text="talk later", ts=now - gap_hours * 3600.0))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="i1", direction="in",
                                      text="hey you there?", ts=now - 5))


def test_enrich_draft_strips_excuse_only_after_72h(store, monkeypatch, caplog):
    from src.inbox import autosend_policy as pol
    from src.inbox.drafts import DraftService
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    caplog.set_level(logging.INFO)
    svc = DraftService(inbox_store=store)
    excuse = "Sorry, I've been buried in work. How have you been?"
    # 沉寂 7 天 → 剥
    _seed(store, "telegram:acct1:u1", gap_hours=7 * 24)
    d1 = svc.auto_generate_draft(_conv(), "hey you there?", automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d1, reply_text=excuse, automation_mode="auto_ai")
    assert store.get_draft(d1)["draft_text"] == "How have you been?"
    assert any("[persona-guard] late_excuse=" in r.getMessage() and "action=strip" in r.getMessage()
               for r in caplog.records)
    # 沉寂 2h → 不动（日常对话里的「忙」不是编造）
    _seed(store, "telegram:acct1:u2", gap_hours=2)
    d2 = svc.auto_generate_draft(_conv("telegram:acct1:u2", "u2"), "hey you there?",
                                 automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d2, reply_text=excuse, automation_mode="auto_ai")
    assert store.get_draft(d2)["draft_text"] == excuse
    # 首次接触（无出站）→ 不是「迟回」→ 不动
    _seed(store, "telegram:acct1:u3", gap_hours=0, first_contact=True)
    d3 = svc.auto_generate_draft(_conv("telegram:acct1:u3", "u3"), "hey you there?",
                                 automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d3, reply_text=excuse, automation_mode="auto_ai")
    assert store.get_draft(d3)["draft_text"] == excuse
    # 沉寂 7 天 + 整段理由 → 换「刚看到」
    _seed(store, "telegram:acct1:u4", gap_hours=7 * 24)
    d4 = svc.auto_generate_draft(_conv("telegram:acct1:u4", "u4"), "hey you there?",
                                 automation_mode="auto_ai", enrich=True)
    assert svc.enrich_draft(d4, reply_text="Sorry, been so busy with work lately.", automation_mode="auto_ai")
    assert store.get_draft(d4)["draft_text"] == "Just saw this."


def test_protocol_chain_wired():
    src = (ROOT / "src" / "integrations" / "protocol_autoreply.py").read_text(encoding="utf-8")
    assert "strip_late_reply_excuses" in src and "LATE_REPLY_SILENCE_HOURS" in src
    # 在客服腔守卫之后、风险判定之前
    assert src.index("客服腔守卫异常（放行原稿）") < src.index("strip_late_reply_excuses as _slre") \
        < src.index("risk = (risk_fn(reply) if risk_fn else \"low\") or \"low\"")
    assert 'stage=protocol' in src[src.index("late_excuse="):src.index("late_excuse=") + 200]
