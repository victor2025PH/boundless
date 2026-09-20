# -*- coding: utf-8 -*-
"""Q-37（#318 VV7BRY）：调度钟三源同源 + 人设夜间不主动 + 文本时刻尺。

五例：① NY confirmed + 服务器 15:22 → peer_tz hour=3 不发；② 无人设外客户钟 +
人设美东 → persona_tz 03 不发；③ 客户白天 + 人设夜间 + 25 min 前入站 → 放行；
④ 服务器回落 14:00 → 照旧发；⑤ VV7BRY 原文被 C 拦。另：narrow 不升 replace、
Q-29 86 例零改动（本文件不改 test_peer_time_q29）。
"""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

from src.companion.user_clock import TRUST_NARROW, TRUST_REPLACE, UserClock
from src.companion.user_clock_resolver import (
    resolve_schedule_clock,
    resolve_schedule_user_clock,
)
from src.companion.world_clock_guard import (
    detect_daypart_conflict,
    detect_persona_self_time_conflict,
)
from src.integrations.companion_proactive import plan_proactive_sends
from src.contacts.care_dispatcher import CareDispatcher

_H = 3600.0
VV7BRY = (
    "It's 3am here and I'm wide awake wondering how the Marina's treating you lately"
)
NOW_1522 = datetime(2026, 9, 12, 15, 22, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
CID = "whatsapp:19892968016:15195555560"


def _profile(loc, status="confirmed", key="location"):
    return {"fields": {key: {"value": loc, "source": "customer", "status": status}}}


def _ts(hour, *, minute=0, day=19, month=6, year=2026):
    return time.mktime(time.struct_time(
        (year, month, day, hour, minute, 0, 0, 0, -1)))


def _conv(cid=CID, *, now=None, last_in_ts=0.0):
    base = now if now is not None else time.time()
    return {
        "conversation_id": cid, "platform": "whatsapp", "account_id": "19892968016",
        "chat_key": "15195555560", "last_ts": base - 48 * _H,
        "last_direction": "out", "archived": False, "memory_key": "u:1",
        "stage": "steady", "intimacy": 60.0, "last_emotion": "",
        "last_in_ts": last_in_ts, "language": "en",
    }


def _opener(**kw):
    return {"mode": "gentle_checkin", "directive": "好久不见", "fact": ""}


def _plan(now, *, sched=None, persona_hour=None, last_in_ts=0.0, diag=None):
    skips = [] if diag is None else diag
    return plan_proactive_sends(
        [_conv(now=now, last_in_ts=last_in_ts)],
        cooldown_map={}, opener_fn=_opener, now=now,
        schedule_clock_provider=sched,
        persona_hour_provider=(None if persona_hour is None
                               else (lambda _cid: persona_hour)),
        diagnostics=skips,
    )


# ---------------------------------------------------------------------------
# ① New York confirmed + 服务器 15:22 → peer_tz hour=3 → 不发
# ---------------------------------------------------------------------------

def test_q37_ny_confirmed_1522_peer_tz_quiet():
    hour, src = resolve_schedule_clock(
        CID, now=NOW_1522, profile=_profile("New York"))
    assert src == "peer_tz" and hour == 3
    clock, src2 = resolve_schedule_user_clock(
        CID, now=NOW_1522, profile=_profile("New York"))
    assert src2 == "peer_tz" and clock is not None
    assert clock.trust == TRUST_REPLACE and clock.tz_name == "America/New_York"
    skips = []
    plans = _plan(NOW_1522, sched=lambda _c: (hour, src), diag=skips)
    assert plans == []
    assert any(s.get("reason") == "user_quiet_hours" for s in skips)


def test_q37_ny_mentioned_also_peer_tz():
    hour, src = resolve_schedule_clock(
        CID, now=NOW_1522, profile=_profile("New York", status="mentioned"))
    assert src == "peer_tz" and hour == 3


# ---------------------------------------------------------------------------
# ② 无客户钟 + 人设美东 → persona_tz 03 → 不发
# ---------------------------------------------------------------------------

def test_q37_no_peer_persona_et_quiet():
    persona = {"location": "new_york"}
    hour, src = resolve_schedule_clock(
        CID, now=NOW_1522, profile={"fields": {}}, persona=persona)
    assert src == "persona_tz" and hour == 3
    skips = []
    plans = _plan(NOW_1522, sched=lambda _c: (hour, src),
                  persona_hour=3, diag=skips)
    assert plans == []
    assert any(s.get("reason") == "user_quiet_hours" for s in skips)


# ---------------------------------------------------------------------------
# ③ 客户白天 + 人设夜间 + 25 min 前入站 → 放行；无入站 → persona_quiet
# ---------------------------------------------------------------------------

def test_q37_persona_night_recent_inbound_allows():
    # 客户东京（上海 15:22 = 东京 16:22，白天）+ 人设美东 03
    hour, src = resolve_schedule_clock(
        CID, now=NOW_1522, profile=_profile("東京"),
        persona={"location": "new_york"})
    assert src == "peer_tz" and hour == 16
    plans = _plan(
        NOW_1522, sched=lambda _c: (hour, src), persona_hour=3,
        last_in_ts=NOW_1522 - 25 * 60)
    assert len(plans) == 1
    assert plans[0]["clock_source"] == "peer_tz"
    assert plans[0]["local_hour"] == 16
    assert plans[0]["persona_hour"] == 3


def test_q37_persona_night_no_inbound_skips():
    skips = []
    plans = _plan(
        NOW_1522, sched=lambda _c: (16, "peer_tz"), persona_hour=3,
        last_in_ts=NOW_1522 - 2 * _H, diag=skips)
    assert plans == []
    assert any(s.get("reason") == "persona_quiet_hours" for s in skips)


# ---------------------------------------------------------------------------
# ④ 服务器回落且 14:00 → 照旧发
# ---------------------------------------------------------------------------

def test_q37_server_1400_still_sends():
    now = _ts(14)
    hour, src = resolve_schedule_clock("x", now=now)
    assert src == "server" and hour == 14
    plans = _plan(now, sched=lambda _c: (hour, src))
    assert len(plans) == 1
    assert plans[0]["clock_source"] == "server"
    assert plans[0]["local_hour"] == 14


# ---------------------------------------------------------------------------
# ⑤ VV7BRY 回放：事故原文被 C 拦
# ---------------------------------------------------------------------------

def test_q37_vv7bry_text_blocked_by_c():
    # 事故上下文：调度钟 15（客户下午）+ 人设钟 3 + 原文「It's 3am here…」
    assert detect_daypart_conflict(VV7BRY, 15, persona_hour=3) == "persona_late"
    # 人设钟被拨到下午：自述 3am 与人设钟也矛盾
    assert detect_persona_self_time_conflict(VV7BRY, 15) == "persona_late"
    # 两边都是凌晨：自述与人设钟一致，调度钟也非白天 → C 不误伤
    assert detect_daypart_conflict(VV7BRY, 3, persona_hour=3) is None
    # 客户时段词仍按调度钟
    assert detect_daypart_conflict("good morning", 15) == "morning"
    assert detect_daypart_conflict("good morning", 8) is None


# ---------------------------------------------------------------------------
# 红线：narrow 不升 replace；care 同源读钟
# ---------------------------------------------------------------------------

def test_q37_narrow_behavior_not_elevated(monkeypatch):
    from src.companion import user_clock_resolver as r
    narrow = UserClock(
        tz_name="UTC-05:00", offset_hours=-5.0, source="behavior",
        confidence=0.6, country="US", city_slug="", trust=TRUST_NARROW)

    def _fake(*_a, **_k):
        return narrow

    monkeypatch.setattr(r, "resolve_for_conversation", _fake)
    clock, src = r.resolve_schedule_user_clock(
        "c1", now=NOW_1522, cfg={"enabled": True},
        inbox_store=object())
    assert src == "server" and clock is None


def test_q37_care_reads_schedule_clock():
    class _AI:
        pass

    async def _send(*_a, **_k):
        return True

    from src.contacts.care_schedule import CareScheduleStore
    d = CareDispatcher(
        store=CareScheduleStore(":memory:"), ai_client=_AI(),
        send_callback=_send,
        schedule_clock_provider=lambda it: (
            UserClock(tz_name="America/New_York", offset_hours=-4.0,
                      source="peer_tz", confidence=1.0, country="US",
                      city_slug="nyc", trust=TRUST_REPLACE),
            "peer_tz",
        ),
    )
    clock, basis = d._resolve_clock({"contact_key": CID})
    assert basis == "peer_tz" and clock.tz_name == "America/New_York"


def test_q37_static_pins():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    uc = (root / "src" / "companion" / "user_clock.py").read_text(encoding="utf-8")
    assert "trust == TRUST_REPLACE" in uc or "clock.trust == TRUST_REPLACE" in uc
    bg = (root / "src" / "bootstrap" / "background_tasks.py").read_text(encoding="utf-8")
    assert "schedule_clock_provider=_care_schedule_clock" in bg
    pt = (root / "src" / "companion" / "proactive_topic.py").read_text(encoding="utf-8")
    assert "detect_daypart_conflict" in pt
    assert "schedule_clock_provider=_schedule_clock_for_plan" in pt
    cp = (root / "src" / "integrations" / "companion_proactive.py").read_text(
        encoding="utf-8")
    assert "skip=persona_quiet" in cp
    assert "persona_quiet_hours" in cp
