"""Facebook per-account pace: caps, active hours, probe bypass, counter reset.

No phone is touched. The pace clock is pinned by the suite fixture; cases that
care about the window pass ``now`` themselves.
"""
from __future__ import annotations

from src.fleet.phone_flows import PhoneFlows
from src.fleet.protocol import (
    CAP_PHONE_FLOWS_V1, CAP_PHONE_OPS_V1, STATUS_DONE, STATUS_REJECTED, TASK_PHONE_COMMENT, TASK_PHONE_FOLLOW,
    TASK_PHONE_LIKE, TASK_PHONE_POST,
)
from src.fleet.social_pace import (
    REASON_CAPPED, REASON_DELAY, REASON_DISABLED, REASON_HOURS, FacebookPace, PaceLedger, account_key, decide,
    identity_token, load_facebook_policy, local_hour, spacing_sec,
)
from tests.test_fleet_control import OP, _client, st  # noqa: F401
from tests.test_fleet_phone_flows import _flow_node, _ops

# 2025-01-01 08:00 Asia/Manila (UTC+8). Hour 8 is inside the default [8, 22) window.
INSIDE = 1735689600.0


def _pace(**kw) -> FacebookPace:
    hourly = {"like": 6, "comment": 3, "follow": 3, "post": 1}
    daily = {"like": 40, "comment": 15, "follow": 15, "post": 4}
    hourly.update(kw.pop("hourly", {}))
    daily.update(kw.pop("daily", {}))
    base = dict(enabled=True, tz_offset_hours=8, active_start=8, active_end=22,
                min_delay_sec=0, jitter_sec=0, hourly=hourly, daily=daily)
    base.update(kw)
    return FacebookPace(**base)


def _like(events, now, **kw):
    app = kw.pop("app", "facebook")
    action = kw.pop("action", "like")
    key = kw.pop("account_key", "n|facebook|serial:S1")
    probe = kw.pop("probe", False)
    dry_run = kw.pop("dry_run", False)
    policy = kw.pop("policy", None) or _pace(**kw)
    return decide(policy, app=app, action=action, events=events, now=now,
                  account_key=key, probe=probe, dry_run=dry_run)


def test_shipped_defaults_match_the_yaml():
    policy = load_facebook_policy()
    assert policy.enabled is True
    assert policy.tz_offset_hours == 8
    assert (policy.active_start, policy.active_end) == (8, 22)
    assert policy.min_delay_sec == 60 and policy.jitter_sec == 20
    assert policy.hourly["like"] == 6 and policy.daily["like"] == 40
    assert policy.hourly["comment"] == 3 and policy.daily["comment"] == 15
    assert policy.hourly["follow"] == 3 and policy.daily["follow"] == 15
    assert policy.hourly["post"] == 1 and policy.daily["post"] == 4
    assert local_hour(INSIDE, 8) == 8


def test_under_cap_is_allowed_and_counted():
    decision = _like([], INSIDE)
    assert decision.allow is True and decision.counted is True and decision.reason == ""


def test_over_hourly_cap_is_rate_capped():
    events = [("like", INSIDE - 10), ("like", INSIDE - 20)]
    decision = _like(events, INSIDE, hourly={"like": 2})
    assert decision.allow is False
    assert decision.reason == REASON_CAPPED and decision.window == "hour"
    # An action that fell out of the rolling hour no longer counts.
    aged = [("like", INSIDE - 3600)]
    assert _like(aged, INSIDE, hourly={"like": 1}).allow is True
    fresh = [("like", INSIDE - 3599)]
    assert _like(fresh, INSIDE, hourly={"like": 1}).window == "hour"


def test_over_daily_cap_is_rate_capped_when_the_hour_still_has_room():
    events = [("like", INSIDE - 7200), ("like", INSIDE - 7000)]
    decision = _like(events, INSIDE, hourly={"like": 6}, daily={"like": 2})
    assert decision.allow is False
    assert decision.reason == REASON_CAPPED and decision.window == "day"


def test_outside_active_hours_is_skipped():
    assert local_hour(INSIDE - 3600, 8) == 7
    assert local_hour(INSIDE + 14 * 3600, 8) == 22
    assert _like([], INSIDE - 3600).reason == REASON_HOURS
    assert _like([], INSIDE + 14 * 3600).reason == REASON_HOURS
    assert _like([], INSIDE + 13 * 3600).allow is True  # 21:00


def test_probe_and_dry_run_bypass_caps_and_quiet_hours():
    full = [("like", INSIDE - 5)] * 10
    probe = _like(full, INSIDE - 3600, probe=True, hourly={"like": 1}, daily={"like": 1})
    dry = _like(full, INSIDE - 3600, dry_run=True, hourly={"like": 1}, daily={"like": 1})
    assert probe.allow is True and probe.counted is False and probe.reason == ""
    assert dry.allow is True and dry.counted is False
    other = _like(full, INSIDE - 3600, app="instagram", hourly={"like": 1})
    assert other.allow is True and other.counted is False


def test_kill_switch_and_min_delay():
    assert _like([], INSIDE, enabled=False).reason == REASON_DISABLED
    recent = [("comment", INSIDE - 30)]
    waited = _like(recent, INSIDE, action="like", min_delay_sec=60, hourly={"like": 10}, daily={"like": 10})
    assert waited.reason == REASON_DELAY
    ready = _like(recent, INSIDE + 30, action="like", min_delay_sec=60, hourly={"like": 10}, daily={"like": 10})
    assert ready.allow is True and ready.counted is True


def test_spacing_is_stable_and_bounded():
    policy = FacebookPace()
    first = spacing_sec(policy, "n|facebook|acct:ada", 100.0)
    assert first == spacing_sec(policy, "n|facebook|acct:ada", 100.0)
    assert policy.min_delay_sec <= first <= policy.min_delay_sec + policy.jitter_sec


def test_counters_increment_and_reset_on_the_ledger():
    policy = _pace(hourly={"like": 1}, daily={"like": 1}, min_delay_sec=0)
    ledger = PaceLedger(policy)
    assert ledger.allow(app="facebook", action="like", serial="S1", now=INSIDE).allow is True
    blocked = ledger.allow(app="facebook", action="like", serial="S1", now=INSIDE + 1)
    assert blocked.reason == REASON_CAPPED and blocked.window == "hour"
    nxt = ledger.allow(app="facebook", action="like", serial="S1", now=INSIDE + 86400)
    assert nxt.allow is True and nxt.counted is True  # local midnight reset the day and the hour
    # A different serial does not share the budget.
    other = ledger.allow(app="facebook", action="like", serial="S2", now=INSIDE + 1)
    assert other.allow is True


def test_account_beats_wallpaper_and_wallpaper_beats_serial():
    assert identity_token("S1", "Ada", "09").startswith("acct:")
    assert identity_token("S1", "", "09") == identity_token("S2", "", "9") == "wall:9"
    assert account_key("n1", "facebook", "S1", account="Ada") == account_key("n1", "Facebook", "S9", account="ada")
    assert account_key("n1", "facebook", "S1") != account_key("n1", "instagram", "S1")


def test_store_skips_over_cap_and_reports_usage(st):
    st.pace_policy = _pace(hourly={"like": 2}, daily={"like": 5}, min_delay_sec=0)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], mid="m-pace")
    target = {"serial": "S1", "account": "Ada", "wallpaper": "09"}
    assert st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook"}, target=target, now=INSIDE)
    assert st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook"}, target=target, now=INSIDE + 1)
    assert st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook"}, target=target, now=INSIDE + 2) is None
    assert st.take_social_pace_refusal() == REASON_CAPPED
    usage = st.social_pace_usage(now=INSIDE + 2)
    assert usage["actions_today"] == 2
    row = usage["accounts"][0]
    assert row["account"] == "Ada" and row["actions"]["like"]["hour"] == 2
    assert row["actions"]["like"]["hour_cap"] == 2 and row["actions"]["like"]["day_cap"] == 5
    assert row["actions"]["comment"]["day"] == 0
    # Same login on another serial shares the account budget.
    assert st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook"},
                      target={"serial": "S2", "account": "ada"}, now=INSIDE + 3) is None
    assert st.social_pace_usage(now=INSIDE + 3)["actions_today"] == 2


def test_store_daily_reset_probe_bypass_and_remote_ops_first(st):
    st.pace_policy = _pace(hourly={"like": 5}, daily={"like": 1}, min_delay_sec=0)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], mid="m-pace-day")
    assert st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook"}, target={"serial": "S1"}, now=INSIDE)
    assert st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook"}, target={"serial": "S1"},
                      now=INSIDE + 86400)
    usage = st.social_pace_usage(now=INSIDE + 86400)
    assert usage["accounts"][0]["actions"]["like"]["day"] == 1
    assert usage["accounts"][0]["actions"]["like"]["hour"] == 1
    probe = st.enqueue(nid, TASK_PHONE_LIKE, payload={"app": "facebook", "like_probe": True},
                       target={"serial": "S1"}, now=INSIDE + 86400)
    assert probe and probe["payload"]["like_probe"] is True
    assert st.social_pace_usage(now=INSIDE + 86400)["actions_today"] == 1
    quiet = st.enqueue(nid, TASK_PHONE_COMMENT, payload={"app": "facebook", "text": "hi"},
                       target={"serial": "S1"}, now=INSIDE - 3600)
    assert quiet is None and st.take_social_pace_refusal() == REASON_HOURS
    off = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], remote=False, mid="m-pace-off")
    assert st.task_refusal(off, TASK_PHONE_POST) == "remote_ops_disabled"
    assert st.enqueue(off, TASK_PHONE_POST, payload={"app": "facebook", "text": "hi"},
                      target={"serial": "S1"}, now=INSIDE) is None
    assert st.take_social_pace_refusal() == ""
    # Instagram is not on this budget.
    ig = st.enqueue(nid, TASK_PHONE_FOLLOW, payload={"app": "instagram", "handle": "ada"},
                    target={"serial": "S1"}, now=INSIDE - 3600)
    assert ig and ig["payload"]["app"] == "instagram"


def test_social_route_skips_with_reason_and_probe_still_queues(st):
    st.pace_policy = _pace(hourly={"like": 1}, daily={"like": 4}, min_delay_sec=0)
    client = _client(st)
    nid = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], mid="m-pace-http")
    url = f"/api/fleet/nodes/{nid}/phones/S1/social/like"
    first = client.post(url, headers=OP, json={"app": "facebook", "account": "Ada", "wallpaper": "09"})
    assert first.status_code == 200, first.text
    assert first.json()["task"]["target"]["account"] == "Ada"
    assert first.json()["task"]["target"]["wallpaper"] == "9"
    assert "account" not in first.json()["task"]["payload"]
    second = client.post(url, headers=OP, json={"app": "facebook", "account": "Ada"})
    assert second.status_code == 409 and second.json()["detail"] == REASON_CAPPED
    probe = client.post(url, headers=OP, json={"app": "facebook", "account": "Ada", "like_probe": True})
    assert probe.status_code == 200, probe.text
    assert probe.json()["task"]["payload"]["like_probe"] is True
    bad = client.post(url, headers=OP, json={"app": "facebook", "account": "bad name"})
    assert bad.status_code == 400 and bad.json()["detail"] == "bad_account"
    st.pace_policy = _pace(active_start=10, active_end=12, min_delay_sec=0)
    quiet = client.post(url, headers=OP, json={"app": "facebook", "account": "Bea"})
    assert quiet.status_code == 409 and quiet.json()["detail"] == REASON_HOURS
    st.pace_policy = FacebookPace(enabled=False)
    paused = client.post(url, headers=OP, json={"app": "facebook", "account": "Bea"})
    assert paused.status_code == 409 and paused.json()["detail"] == REASON_DISABLED
    off = _flow_node(st, [CAP_PHONE_OPS_V1, CAP_PHONE_FLOWS_V1], remote=False, mid="m-pace-http-off")
    denied = client.post(f"/api/fleet/nodes/{off}/phones/S1/social/like", headers=OP, json={"app": "facebook"})
    assert denied.status_code == 409 and denied.json()["detail"] == "remote_ops_disabled"
    snap = client.get("/api/fleet/social-pace", headers=OP)
    assert snap.status_code == 200
    assert snap.json()["actions_today"] == 1
    assert any(row["actions"]["like"]["day"] == 1 for row in snap.json()["accounts"])


def test_phone_flows_skip_capped_like_and_still_probe(tmp_path):
    ops, fake, _slept = _ops()
    policy = _pace(hourly={"like": 0}, daily={"like": 0}, min_delay_sec=0)
    flows = PhoneFlows(enabled=True, pace=PaceLedger(policy))
    status, _result, detail = flows.execute(TASK_PHONE_LIKE, {"app": "facebook"}, {"serial": "S1"}, ops=ops)
    assert (status, detail) == (STATUS_REJECTED, REASON_CAPPED)
    assert [c for c in fake.calls if c not in (("version",), ("devices", "-l"))] == []
    _status, _result, probe_detail = flows.execute(
        TASK_PHONE_LIKE, {"app": "facebook", "like_probe": True}, {"serial": "S1"}, ops=ops)
    assert probe_detail != REASON_CAPPED
    dry_status, _result, dry_detail = flows.execute(
        TASK_PHONE_LIKE, {"app": "facebook", "dry_run": True}, {"serial": "S1"}, ops=ops)
    assert (dry_status, dry_detail) == (STATUS_DONE, "dry_run")
    # A local ledger file remembers a counted action and drops it after local midnight.
    ledger = PaceLedger(_pace(hourly={"post": 1}, daily={"post": 1}, min_delay_sec=0))
    ledger.bind(tmp_path)
    assert ledger.allow(app="facebook", action="post", serial="S1", now=INSIDE).allow is True
    reloaded = PaceLedger(_pace(hourly={"post": 1}, daily={"post": 1}, min_delay_sec=0))
    reloaded.bind(tmp_path)
    assert reloaded.allow(app="facebook", action="post", serial="S1", now=INSIDE + 1).reason == REASON_CAPPED
    assert reloaded.allow(app="facebook", action="post", serial="S1", now=INSIDE + 86400).allow is True
