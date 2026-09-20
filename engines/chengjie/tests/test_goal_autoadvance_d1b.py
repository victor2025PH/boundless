# -*- coding: utf-8 -*-
"""D1b「自动推进怎么才算真开」P0 门禁（2026-09-05）。

P0-1 冲刺行脱离 care 灰度门：
- ``proactive_care.enabled=false`` → 派发器进 ``goal_only`` 模式：只放
  ``goal_row_policy.live`` 的 goal 行，其余到期行原样留 pending（不消费）；
- ``proactive_care.dry_run=true`` → live 的 goal 行**照样真发**，非 live 行仍 dry；
- policy 未注入 / 非 goal 行 / policy 返回 live=False → 完全旧行为。

P0-2 / P0-3 的纯函数与状态口径在 ``test_goal_sprint_ticker.py`` /
``test_goal_engine_status_166.py``；这里钉 bootstrap 侧的静态接线
（policy 的 live 来自 goals.sprint.dry_run 而不是 care；daily 行不豁免预算/安静）。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import CareDispatcher
from src.contacts.care_schedule import CareScheduleStore

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()
_ROOT = Path(__file__).resolve().parents[1]


class _AI:
    def __init__(self, reply="到点了，我们把那件事往前推一步吧～"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(record):
    async def _send(channel, account_id, chat_name, reply, defer_until, reason,
                    staleness, extra):
        record.append({"chat": chat_name, "reply": reply, "extra": extra})
        return 1
    return _send


def _store_mixed():
    """一条普通 care 行 + 一条冲刺 goal 行，都已到期。"""
    s = CareScheduleStore(":memory:")
    due = NOW - 60
    c = CareCommitment(due_at=due, event_at=due, topic="面试",
                       sentiment="neutral", anchor_text="x",
                       source_text="明天面试好紧张", confidence=0.85)
    s.add_commitment(c, contact_key="tg:care", platform="telegram",
                     account_id="default", chat_key="care")
    s.add_scheduled_care(
        contact_key="tg:goal", platform="telegram", account_id="default",
        chat_key="goal", due_at=due, event_at=due + 3600,
        topic="限时推进", topic_norm="goal:abc123:p1",
        source_text="限时推进「拿到微信号」", confidence=1.0, dedup_days=3.0)
    return s


def _cfg(enabled=True, dry_run=False):
    d = {"enabled": enabled, "dry_run": dry_run, "max_per_tick": 10}
    return lambda: dict(d)


def _policy(live):
    return lambda item: {"exempt_budget": True, "ignore_quiet": True,
                         "jitter": None, "live": live}


async def test_care_disabled_goal_only_dispatches_live_goal_rows():
    s = _store_mixed()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       cfg_provider=_cfg(enabled=False),
                       goal_row_policy=_policy(True))
    n = await d.run_once(now=NOW)
    assert n == 1 and [r["chat"] for r in rec] == ["goal"]
    # 普通 care 行原样 pending（care 开闸后按期发出）；goal 行已 sent
    rows = {r["chat_key"]: r["status"] for r in s.list_recent(limit=10)}
    assert rows == {"care": "pending", "goal": "sent"}
    assert d.last_tick_gated is True, "care 自身仍算被闸（看板口径不变）"


async def test_care_disabled_without_policy_is_old_noop():
    s = _store_mixed()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       cfg_provider=_cfg(enabled=False))
    assert await d.run_once(now=NOW) == 0 and not rec
    assert s.count(status="pending") == 2


async def test_care_disabled_goal_row_not_live_stays_pending():
    """policy 说 live=False（goals.sprint.dry_run 开）→ care 关闸下 goal 行也不派。"""
    s = _store_mixed()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       cfg_provider=_cfg(enabled=False),
                       goal_row_policy=_policy(False))
    assert await d.run_once(now=NOW) == 0 and not rec
    assert s.count(status="pending") == 2


async def test_care_dry_run_live_goal_row_really_sends():
    """care dry_run=true：普通 care 行只拟稿留 pending；live 冲刺行真发 → sent。"""
    s = _store_mixed()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       cfg_provider=_cfg(enabled=True, dry_run=True),
                       goal_row_policy=_policy(True))
    n = await d.run_once(now=NOW)
    assert n == 2, "dry 拟稿计 1 + 真发计 1"
    assert [r["chat"] for r in rec] == ["goal"]
    rows = {r["chat_key"]: r["status"] for r in s.list_recent(limit=10)}
    assert rows == {"care": "pending", "goal": "sent"}


async def test_care_dry_run_goal_row_not_live_stays_dry():
    s = _store_mixed()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       cfg_provider=_cfg(enabled=True, dry_run=True),
                       goal_row_policy=_policy(False))
    assert await d.run_once(now=NOW) == 2 and not rec
    assert s.count(status="pending") == 2


async def test_care_live_goal_row_policy_exception_is_safe():
    def _boom(item):
        raise RuntimeError("x")
    s = _store_mixed()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx",
                       cfg_provider=_cfg(enabled=True, dry_run=False),
                       goal_row_policy=_boom)
    # policy 抛 → 按默认（非 live、无豁免），care 本身开着 → 两行都真发
    assert await d.run_once(now=NOW) == 2 and len(rec) == 2


# ── bootstrap 静态接线 ────────────────────────────────────────────────────

def _bg_src() -> str:
    return (_ROOT / "src" / "bootstrap" / "background_tasks.py").read_text(
        encoding="utf-8")


def test_goal_row_policy_live_comes_from_sprint_dry_run_not_care():
    src = _bg_src()
    m = re.search(r"def _goal_row_policy\(item: dict\) -> dict:(.*?)\n        dispatcher = CareDispatcher\(",
                  src, re.S)
    assert m, "background_tasks 缺 _goal_row_policy"
    body = m.group(1)
    assert '"live": live' in body
    assert 'scfg.get("dry_run")' in body, "live 必须看 goals.sprint.dry_run"
    assert "proactive_care" not in body, "live 不许再看 care 的开关"
    # daily 行：live 但不豁免预算/安静时段
    assert re.search(r'kind == "daily".*?"exempt_budget": False,\s*"ignore_quiet": False',
                     body, re.S), "自然档每日拍不得继承冲刺的骚扰型豁免"


def test_sent_hook_routes_daily_and_sprint_rows():
    src = _bg_src()
    assert "record_natural_beat_sent" in src and "record_sprint_beat_sent" in src
    assert "parse_goal_care_kind" in src


def test_startup_warns_when_sprint_platforms_not_explicit():
    src = _bg_src()
    assert "sprint_platforms_explicit" in src
    assert "goals.sprint.platforms 未显式配置" in src


def test_care_dispatcher_policy_live_contract_documented():
    src = (_ROOT / "src" / "contacts" / "care_dispatcher.py").read_text(
        encoding="utf-8")
    for needle in ("goal_only", "_goal_live(", 'goal_policy.get("live")'):
        assert needle in src, f"care_dispatcher 缺 D1b 接线: {needle}"
