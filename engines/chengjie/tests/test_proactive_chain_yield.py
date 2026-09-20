# -*- coding: utf-8 -*-
"""实施92b 门禁：主动触达给在途跟进 SOP 链让位（防双打扰划界）。

不变量：
- 会话有在途链（has_running_chain 谓词 True）→ plan_proactive_sends 跳过该
  会话，诊断原因 ``chain_running``（预览页可读，与 care_pending 同格式）；
- 谓词缺席（None）＝旧行为零变化；谓词抛异常＝放行（fail-open）；
- 其余会话不受影响。
"""

import time

from src.integrations.companion_proactive import plan_proactive_sends


def _conv(cid, silent_hours=30.0, now=None):
    n = float(now if now is not None else time.time())
    return {
        "conversation_id": cid,
        "platform": "telegram",
        "account_id": "a1",
        "chat_key": cid.split(":")[-1],
        "last_ts": n - silent_hours * 3600,
        "last_direction": "out",
        "archived": False,
        "memory_key": cid,
        "stage": "",
        "intimacy": 50,
    }


def _opener(**_kw):
    return {"mode": "gentle_checkin", "directive": "问候", "fact": "",
            "context_facts": []}


def _plan(convs, *, has_running_chain=None, diagnostics=None, now=None):
    return plan_proactive_sends(
        convs, cooldown_map={}, opener_fn=_opener,
        now=now, min_silent_hours=24.0, cooldown_hours=72.0,
        max_per_tick=10, quiet_start_hour=0, quiet_end_hour=0,
        has_running_chain=has_running_chain, diagnostics=diagnostics)


def test_running_chain_conversation_skipped():
    now = time.time()
    convs = [_conv("tg:a1:c1", now=now), _conv("tg:a1:c2", now=now)]
    diags = []
    plans = _plan(convs, now=now,
                  has_running_chain=lambda cid: cid == "tg:a1:c1",
                  diagnostics=diags)
    ids = [p["conversation_id"] for p in plans]
    assert "tg:a1:c1" not in ids and "tg:a1:c2" in ids
    reasons = {d["conversation_id"]: d["reason"] for d in diags}
    assert reasons.get("tg:a1:c1") == "chain_running"


def test_none_predicate_keeps_old_behavior():
    now = time.time()
    plans = _plan([_conv("tg:a1:c3", now=now)], now=now)
    assert [p["conversation_id"] for p in plans] == ["tg:a1:c3"]


def test_predicate_exception_fails_open():
    now = time.time()

    def _boom(_cid):
        raise RuntimeError("db down")

    plans = _plan([_conv("tg:a1:c4", now=now)], now=now,
                  has_running_chain=_boom)
    assert [p["conversation_id"] for p in plans] == ["tg:a1:c4"]


def test_predicate_false_not_skipped():
    now = time.time()
    plans = _plan([_conv("tg:a1:c5", now=now)], now=now,
                  has_running_chain=lambda _cid: False)
    assert [p["conversation_id"] for p in plans] == ["tg:a1:c5"]
