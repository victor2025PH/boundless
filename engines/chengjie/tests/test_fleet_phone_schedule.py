"""每日计划。假时钟、假派发；不碰真机，也不碰受保护的直播机。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.fleet.phone_ops import MAX_CONCURRENT, MIN_INTERVAL_SEC
from src.fleet.phone_schedule import (
    ScheduleError, bundled_plan, effective_plan, list_runs, local_window, node_view, normalize_override,
    plan_day, run_tick, save_global, save_node, save_phone,
)
from src.fleet.phones import is_protected
from src.fleet.protocol import HEARTBEAT_KEYS, sanitize_heartbeat
from src.fleet.store import set_store
from tests.test_fleet_control import OP, _client, _enroll, st  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_store():
    set_store(None)
    yield
    set_store(None)

CAP = ["phone_ops_v1", "phone_flows_v1", "phone_flows_v2"]
PROTECTED = "3B1F4KE5MS140P4X"


def _at(hour, minute, tz=480, day=(2020, 1, 15)):
    """本地钟面 → epoch。默认 UTC+8。"""
    local = datetime(day[0], day[1], day[2], hour, minute, tzinfo=timezone.utc)
    return (local - timedelta(minutes=tz)).timestamp()


DUE = _at(22, 50)
EARLY = _at(7, 0)
LATE = _at(23, 30)


def _quiet(**daily):
    base = {
        "jitter_sec": [0, 0],
        "min_gap_sec": 60,
        "apps": ["facebook"],
        "pause_holds": ["post"],
        "daily": {
            "warmup": {"count": 0, "scrolls": 4, "likes": 0},
            "post": {"min": 0, "max": 0, "texts": [], "media": 0},
            "like": {"count": 0, "scrolls": 0},
            "comment": {"count": 0, "texts": [], "scrolls": 0},
            "watch": {"count": 0, "watches": 3, "likes": 0},
            "follow": {"count": 0, "handles": []},
            "dm": {"count": 0, "handles": [], "texts": []},
        },
    }
    for flow, spec in daily.items():
        base["daily"][flow].update(spec)
    return base


def _node(st, *, serials=("S1",), caps=None, live_stream=False, live_session=False, remote=True,
          mid="m-sch", now=DUE, omit_live=False, states=None):
    nid = _enroll(st, mid=mid)["node_id"]
    phones = []
    for i, serial in enumerate(serials):
        state = "device" if not states else states[i]
        phones.append({"serial": serial, "state": state, "model": "Pixel", "transport": "usb"})
    hb = {"agent_version": "0.3.8", "caps": list(CAP if caps is None else caps), "phones": phones}
    if not omit_live:
        hb["live_stream"] = live_stream
    if live_session:
        hb["live_session"] = True
    st.heartbeat(nid, hb, now=now)
    if remote:
        st.set_remote_ops(nid, True)
    return nid


def _enable(st, plan, now=DUE):
    return save_global(st, enabled=True, plan=plan, now=now)


def _tasks(st, nid, now=DUE):
    return [t for t in st.list_tasks(node_id=nid, limit=50, now=now) if str(t["kind"]).startswith("phone_")]


def _ack(st, nid, task, now):
    st.ack(task["task_id"], node_id=nid, status="done", result={"app": "facebook", "steps": 1}, now=now)


def _slot_count(st):
    return st.schedule_txn(lambda conn: conn.execute("SELECT COUNT(*) AS n FROM phone_schedule_slots").fetchone()["n"])


# ── 计划本身 ──────────────────────────────────────────────────────────────────
def test_defaults_are_off_and_match_the_one_to_two_posts_rule():
    plan = bundled_plan()
    assert plan["active_hours"] == {"start": "08:00", "end": "23:00"}
    assert plan["tz_offset_min"] == 480
    assert plan["pause_holds"] == ["post"]
    assert plan["daily"]["post"]["min"] == 1 and plan["daily"]["post"]["max"] == 2
    assert plan["daily"]["post"]["texts"] == []
    assert plan["daily"]["follow"]["count"] == 0 and plan["daily"]["dm"]["count"] == 0
    assert "enabled" not in plan
    assert "enabled" not in normalize_override({"enabled": True, "daily": {"warmup": {"count": 1}}})
    assert normalize_override({"daily": {"warmup": {"count": 1}}})["daily"]["warmup"] == {"count": 1}
    with pytest.raises(ScheduleError) as ei:
        normalize_override({"active_hours": {"start": "23:00", "end": "08:00"}})
    assert ei.value.code == "bad_hours"
    with pytest.raises(ScheduleError) as ei:
        normalize_override({"daily": {"post": {"texts": ["你好"]}}})
    assert ei.value.code == "text_non_ascii_unsupported"
    with pytest.raises(ScheduleError) as ei:
        effective_plan({"daily": {"post": {"min": 3, "max": 1}}})
    assert ei.value.code == "bad_count"


def test_day_is_stable_inside_the_window_and_respects_the_gap():
    plan = effective_plan(_quiet(warmup={"count": 4}))
    day, start, end = local_window(DUE, plan)
    assert day == "2020-01-15"
    assert start < DUE < end
    first = plan_day(plan, day=day, node_id="n", serial="S1", window_start=start, window_end=end)
    second = plan_day(plan, day=day, node_id="n", serial="S1", window_start=start, window_end=end)
    assert [s["slot_at"] for s in first] == [s["slot_at"] for s in second]
    times = [s["slot_at"] for s in first if s["state"] == "pending"]
    assert times == sorted(times)
    assert all(start <= t < end for t in times)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and min(gaps) >= plan["min_gap_sec"] - 1e-6
    other = plan_day(plan, day=day, node_id="n", serial="S2", window_start=start, window_end=end)
    # 抖动为 0 时格子中心相同；换一部手机也不出窗口
    assert all(start <= s["slot_at"] < end or s["detail"] == "window_full" for s in other)


def test_one_action_lands_on_the_midpoint_when_jitter_is_zero():
    plan = effective_plan(_quiet(warmup={"count": 1}))
    _day, start, end = local_window(DUE, plan)
    slots = plan_day(plan, day="2020-01-15", node_id="n", serial="S1", window_start=start, window_end=end)
    assert len(slots) == 1
    assert slots[0]["slot_at"] == pytest.approx(start + (end - start) / 2)
    assert slots[0]["kind"] == "phone_warmup"


def test_overflow_is_marked_window_full_and_not_pushed_past_the_window():
    raw = _quiet(warmup={"count": 4})
    raw["min_gap_sec"] = 40000
    plan = effective_plan(raw)
    _day, start, end = local_window(DUE, plan)
    slots = plan_day(plan, day="2020-01-15", node_id="n", serial="S1", window_start=start, window_end=end)
    pending = [s for s in slots if s["state"] == "pending"]
    full = [s for s in slots if s["detail"] == "window_full"]
    assert len(pending) == 2 and len(full) == 2
    assert all(start <= s["slot_at"] < end for s in pending)
    assert min(b["slot_at"] - a["slot_at"] for a, b in zip(pending, pending[1:])) >= 40000 - 1e-6


def test_nonzero_jitter_stays_in_the_window_and_is_seeded():
    plan = effective_plan(_quiet(warmup={"count": 3}, **{}))
    plan = effective_plan({"jitter_sec": [90, 600], "min_gap_sec": 900, "daily": {"warmup": {"count": 3},
                                                                                   "post": {"min": 0, "max": 0}}})
    day, start, end = local_window(DUE, plan)
    a = plan_day(plan, day=day, node_id="n", serial="S1", window_start=start, window_end=end)
    b = plan_day(plan, day=day, node_id="n", serial="S1", window_start=start, window_end=end)
    assert [s["slot_at"] for s in a] == [s["slot_at"] for s in b]
    times = [s["slot_at"] for s in a if s["state"] == "pending"]
    assert all(start <= t < end for t in times)
    gaps = [x - y for y, x in zip(times, times[1:])]
    assert all(g >= 900 - 1e-6 for g in gaps)


# ── 调度 ──────────────────────────────────────────────────────────────────────
def test_gate_off_writes_nothing(st):
    nid = _node(st)
    out = run_tick(st, now=DUE)
    assert out["enabled"] is False and out["dispatched"] == [] and out["held"] == []
    assert _slot_count(st) == 0 and _tasks(st, nid) == []


def test_only_json_true_enables(st):
    with pytest.raises(ScheduleError) as ei:
        save_global(st, enabled=1, now=DUE)
    assert ei.value.code == "bad_enabled"
    save_global(st, enabled=False, now=DUE)
    assert run_tick(st, now=DUE)["enabled"] is False
    save_global(st, enabled=True, plan=_quiet(warmup={"count": 1}), now=DUE)
    assert run_tick(st, now=DUE)["enabled"] is True


def test_due_mix_dispatches_runnable_flows_and_holds_posts_without_captions(st):
    nid = _node(st)
    save_global(st, enabled=True, plan={"jitter_sec": [0, 0]}, now=DUE)  # 随包默认：发帖 1–2 条但没有文案
    out = run_tick(st, now=DUE)
    kinds = [d["kind"] for d in out["dispatched"]]
    assert "phone_post" not in kinds and "phone_comment" not in kinds and "phone_dm" not in kinds
    assert len(kinds) == 1  # 同一部手机一轮只排一条
    assert kinds[0] in {"phone_warmup", "phone_like", "phone_watch"}
    held = {h["detail"] for h in out["held"]}
    assert "post_text_missing" in held
    blob = json.dumps(out)
    assert "hello" not in blob and "handle" not in blob
    runs = list_runs(st, node_id=nid)
    assert "hello" not in json.dumps(runs)
    again = run_tick(st, now=DUE + 1)
    assert again["dispatched"] == []
    assert len(_tasks(st, nid, now=DUE + 1)) == 1


def test_post_caption_is_queued_for_the_node_but_absent_from_history(st):
    nid = _node(st)
    _enable(st, _quiet(post={"min": 1, "max": 1, "texts": ["hello fleet"]}))
    out = run_tick(st, now=DUE)
    assert [d["kind"] for d in out["dispatched"]] == ["phone_post"]
    assert "hello fleet" not in json.dumps(out)
    task = _tasks(st, nid)[0]
    assert task["payload"]["text"] == "hello fleet" and task["created_by"] == "schedule"
    assert task["target"] == {"serial": "S1"}
    runs = list_runs(st, node_id=nid)
    assert "hello fleet" not in json.dumps(runs)
    assert all("handle" not in json.dumps(r) or r.get("detail") == "" for r in runs)
    view = node_view(st, nid, now=DUE)
    assert view["plan"]["daily"]["post"]["texts"] == ["hello fleet"]
    assert "hello fleet" not in json.dumps(view["slots"])


def test_dm_handle_is_not_copied_into_the_run_ledger(st):
    nid = _node(st)
    _enable(st, _quiet(dm={"count": 1, "handles": ["some.user"], "texts": ["hello fleet"]}))
    out = run_tick(st, now=DUE)
    assert out["dispatched"][0]["kind"] == "phone_dm"
    assert "some.user" not in json.dumps(out)
    assert "some.user" not in json.dumps(list_runs(st, node_id=nid))
    assert _tasks(st, nid)[0]["payload"]["handle"] == "some.user"


def test_protected_phone_is_never_dispatched(st):
    calls = []

    def dispatch(**kw):
        calls.append(kw["serial"])
        return {"ok": True, "task_id": "t-mock"}

    nid = _node(st, serials=("S1", PROTECTED, "192.168.0.148:5555"), mid="m-prot")
    _enable(st, _quiet(warmup={"count": 1}))
    out = run_tick(st, now=DUE, dispatch=dispatch)
    assert calls == ["S1"]
    assert PROTECTED not in calls and not any(is_protected(s) for s in calls)
    assert any(s["detail"] == "protected_phone" and s["serial"] == PROTECTED for s in out["skipped"])
    assert not any(t["target"].get("serial") == PROTECTED for t in _tasks(st, nid))


def test_live_stream_host_and_unknown_flag_dispatch_nothing(st):
    calls = []

    def dispatch(**kw):
        calls.append(kw)
        return {"ok": True, "task_id": "t-mock"}

    host = _node(st, live_stream=True, mid="m-live")
    unknown = _node(st, omit_live=True, mid="m-unknown")
    _enable(st, _quiet(warmup={"count": 1}))
    out = run_tick(st, now=DUE, dispatch=dispatch)
    assert calls == []
    assert any(s["node_id"] == host and s["detail"] == "live_stream_host" for s in out["skipped"])
    assert any(s["node_id"] == unknown and s["detail"] == "live_stream_unknown" for s in out["held"])
    assert _tasks(st, host) == [] and _tasks(st, unknown) == []
    # 老 agent 后来报了明确的 false，同一天可以排
    st.heartbeat(unknown, {"caps": CAP, "phones": [{"serial": "S1", "state": "device"}], "live_stream": False}, now=DUE)
    out2 = run_tick(st, now=DUE, dispatch=dispatch)
    assert any(c["node_id"] == unknown for c in calls)
    assert all(c["node_id"] != host for c in calls)
    assert _tasks(st, host) == []


def test_pause_and_live_session_hold_posts_until_the_window_allows_them(st):
    nid = _node(st, live_session=True, mid="m-live-session")
    _enable(st, _quiet(warmup={"count": 1}, post={"min": 1, "max": 1, "texts": ["hello fleet"]}))
    out = run_tick(st, now=DUE)
    assert [d["kind"] for d in out["dispatched"]] == ["phone_warmup"]
    assert any(h["kind"] == "phone_post" and h["detail"] == "live_session" for h in out["held"])
    assert "hello fleet" not in json.dumps(out)
    _ack(st, nid, _tasks(st, nid)[0], DUE + 1)
    st.heartbeat(nid, {"caps": CAP, "phones": [{"serial": "S1", "state": "device"}], "live_stream": False}, now=DUE + 1)
    out2 = run_tick(st, now=DUE + 1)
    assert [d["kind"] for d in out2["dispatched"]] == ["phone_post"]
    assert "hello fleet" not in json.dumps(list_runs(st, node_id=nid))


def test_global_pause_holds_only_pause_holds_and_does_not_catch_up_after_hours(st):
    nid = _node(st, mid="m-pause")
    _enable(st, _quiet(warmup={"count": 1}, post={"min": 1, "max": 1, "texts": ["hello fleet"]}))
    save_global(st, paused=True, now=DUE)
    out = run_tick(st, now=DUE)
    assert [d["kind"] for d in out["dispatched"]] == ["phone_warmup"]
    assert any(h["kind"] == "phone_post" and h["detail"] == "paused" for h in out["held"])
    late = run_tick(st, now=LATE)
    assert late["dispatched"] == []
    assert any(s["kind"] == "phone_post" and s["detail"] == "missed_while_paused" for s in late["skipped"])
    assert not any(t["kind"] == "phone_post" for t in _tasks(st, nid, now=LATE))


def test_node_pause_is_independent(st):
    nid = _node(st, mid="m-node-pause")
    _enable(st, _quiet(post={"min": 1, "max": 1, "texts": ["hello fleet"]}))
    save_node(st, nid, paused=True, now=DUE)
    out = run_tick(st, now=DUE)
    assert out["dispatched"] == []
    assert any(h["detail"] == "paused" and h["kind"] == "phone_post" for h in out["held"])


def test_before_window_materializes_but_does_not_dispatch(st):
    nid = _node(st, now=EARLY, mid="m-early")
    _enable(st, _quiet(warmup={"count": 1}), now=EARLY)
    out = run_tick(st, now=EARLY)
    assert out["dispatched"] == [] and _tasks(st, nid, now=EARLY) == []
    assert _slot_count(st) == 1
    assert list_runs(st, node_id=nid)[0]["state"] == "pending"


def test_after_window_does_not_burst(st):
    nid = _node(st, now=LATE, mid="m-late")
    _enable(st, _quiet(warmup={"count": 1}, post={"min": 1, "max": 1, "texts": ["hello fleet"]}), now=LATE)
    out = run_tick(st, now=LATE)
    assert out["dispatched"] == [] and _tasks(st, nid, now=LATE) == []
    assert any(s["detail"] == "window_closed" for s in out["skipped"])


def test_same_phone_stays_single_inflight_and_node_stays_at_max_concurrent(st):
    assert MIN_INTERVAL_SEC == 0.5 and MAX_CONCURRENT == 2
    one = _node(st, serials=("S1",), mid="m-one")
    _enable(st, _quiet(warmup={"count": 2}))
    first = run_tick(st, now=DUE)
    assert len(first["dispatched"]) == 1
    second = run_tick(st, now=DUE + 1)
    assert second["dispatched"] == []
    assert len(_tasks(st, one, now=DUE + 1)) == 1
    _ack(st, one, _tasks(st, one, now=DUE + 1)[0], DUE + 2)
    third = run_tick(st, now=DUE + 2)
    assert len(third["dispatched"]) == 1
    assert len(_tasks(st, one, now=DUE + 2)) == 2

    many = _node(st, serials=("A1", "B1", "C1"), mid="m-many")
    # 这部节点用自己的计划：每部手机 1 次养号。全局计划仍是 2 次，手机会盖过。
    for serial in ("A1", "B1", "C1"):
        save_phone(st, many, serial, plan=_quiet(warmup={"count": 1}), now=DUE)
    # 上面的 save 是覆盖；全局 warmup count 2 会被手机的 count 1 盖住。
    wave = run_tick(st, now=DUE + 3)
    mine = [d for d in wave["dispatched"] if d["node_id"] == many]
    assert len(mine) == 2
    assert len({d["serial"] for d in mine}) == 2
    assert len(_tasks(st, many, now=DUE + 3)) == 2
    again = run_tick(st, now=DUE + 4)
    assert [d for d in again["dispatched"] if d["node_id"] == many] == []
    assert len(_tasks(st, many, now=DUE + 4)) <= MAX_CONCURRENT


def test_offline_and_missing_cap_wait_instead_of_enqueueing(st):
    off = _node(st, now=EARLY, mid="m-off")
    bare = _node(st, caps=["phone_ops_v1"], mid="m-bare")
    closed = _node(st, remote=False, mid="m-closed")
    _enable(st, _quiet(warmup={"count": 1}))
    out = run_tick(st, now=DUE)
    assert all(d["node_id"] != off for d in out["dispatched"])
    assert any(h["node_id"] == off and h["detail"] == "node_offline" for h in out["held"])
    assert any(h["node_id"] == bare and h["detail"] == "node_lacks_cap:phone_flows_v2" for h in out["held"])
    assert any(h["node_id"] == closed and h["detail"] == "remote_ops_disabled" for h in out["held"])
    assert _tasks(st, off, now=DUE) == [] and _tasks(st, bare, now=DUE) == [] and _tasks(st, closed, now=DUE) == []


def test_plan_edits_do_not_reshuffle_after_the_first_dispatch(st):
    nid = _node(st, mid="m-edit")
    _enable(st, _quiet(warmup={"count": 1}), now=EARLY)
    run_tick(st, now=EARLY)
    before = [r["slot_key"] for r in list_runs(st, node_id=nid)]
    save_global(st, plan=_quiet(warmup={"count": 2}), now=EARLY)
    run_tick(st, now=EARLY + 1)
    assert len(list_runs(st, node_id=nid)) == 2  # 还没发出去，次数改了就重排
    st.heartbeat(nid, {"caps": CAP, "phones": [{"serial": "S1", "state": "device"}], "live_stream": False}, now=DUE)
    run_tick(st, now=DUE)
    assert len(_tasks(st, nid)) == 1
    save_global(st, plan=_quiet(warmup={"count": 1}), now=DUE)
    run_tick(st, now=DUE + 1)
    keys = [r["slot_key"] for r in list_runs(st, node_id=nid)]
    assert len(keys) == 2 and _tasks(st, nid, now=DUE + 1)[0]["kind"] == "phone_warmup"
    assert before  # 早期那次确实建过槽


def test_queued_schedule_task_is_cancelled_when_the_host_becomes_a_live_stream(st):
    nid = _node(st, mid="m-flip")
    _enable(st, _quiet(warmup={"count": 1}))
    out = run_tick(st, now=DUE)
    assert len(out["dispatched"]) == 1
    task_id = out["dispatched"][0]["task_id"]
    st.heartbeat(nid, {"caps": CAP, "phones": [{"serial": "S1", "state": "device"}], "live_stream": True}, now=DUE + 1)
    run_tick(st, now=DUE + 1)
    assert st.get_task(task_id)["status"] == "cancelled"
    assert _tasks(st, nid, now=DUE + 1) == [] or all(t["status"] != "queued" for t in _tasks(st, nid, now=DUE + 1))


# ── 操作面 ────────────────────────────────────────────────────────────────────
def test_api_get_set_and_tick(st, monkeypatch):
    monkeypatch.setattr("src.fleet.phone_schedule.clock", lambda: DUE)
    c = _client(st)
    nid = _node(st)
    assert c.get("/api/fleet/schedule").status_code == 401
    got = c.get("/api/fleet/schedule", headers=OP)
    assert got.status_code == 200 and got.json()["enabled"] is False
    bad = c.post("/api/fleet/schedule", headers=OP, json={"enabled": 1})
    assert bad.status_code == 400 and bad.json()["detail"] == "bad_enabled"
    turned = c.post("/api/fleet/schedule", headers=OP, json={
        "enabled": True,
        "plan": {"daily": {"post": {"min": 1, "max": 1, "texts": ["hello fleet"]}, "warmup": {"count": 0},
                           "like": {"count": 0}, "comment": {"count": 0}, "watch": {"count": 0}}},
    })
    assert turned.status_code == 200 and turned.json()["enabled"] is True
    assert turned.json()["plan"]["daily"]["post"]["texts"] == ["hello fleet"]
    view = c.get(f"/api/fleet/nodes/{nid}/schedule", headers=OP).json()
    assert view["enabled"] is True and view["phones"][0]["serial"] == "S1"
    assert "hello fleet" not in json.dumps(view["slots"])
    ticked = c.post("/api/fleet/schedule/tick", headers=OP)
    assert ticked.status_code == 200
    body = ticked.json()
    assert body["enabled"] is True and "hello fleet" not in json.dumps(body)
    runs = c.get(f"/api/fleet/schedule/runs?node_id={nid}", headers=OP).json()["runs"]
    assert runs and "hello fleet" not in json.dumps(runs)
    assert c.post(f"/api/fleet/nodes/{nid}/phones/{PROTECTED}/schedule", headers=OP,
                  json={"plan": {"daily": {"like": {"count": 1}}}}).status_code == 403
    assert c.post(f"/api/fleet/nodes/{nid}/phones/192.168.0.148:5555/schedule", headers=OP,
                  json={"plan": {}}).json()["detail"] == "protected_phone"
    assert c.post("/api/fleet/nodes/n_missing/schedule", headers=OP, json={"paused": True}).status_code == 404


def test_heartbeat_reports_live_flags_without_changing_detection(tmp_path, monkeypatch):
    from src.fleet import agent as agent_mod
    from src.fleet.detect import is_live_stream_host

    monkeypatch.setattr(agent_mod, "is_live_stream_host", is_live_stream_host)
    cfg = agent_mod.AgentConfig(tmp_path / "state")
    cfg.data.update({"controller_url": "http://127.0.0.1:1", "instances": []})
    agent = agent_mod.NodeAgent(cfg, http=lambda *_a, **_k: (200, {}), app_version="t")
    agent.phones.collect = lambda: ([], "")
    hb = agent.build_heartbeat()
    assert hb["live_stream"] is (is_live_stream_host(cfg.state_dir) is True)
    assert hb["live_session"] is False
    flag = tmp_path / "live-stream.flag"
    flag.write_text("1", encoding="ascii")
    assert is_live_stream_host(cfg.state_dir) is True
    assert agent.build_heartbeat()["live_stream"] is True
    cfg.data["phone_live_session"] = True
    assert agent.build_heartbeat()["live_session"] is True
    cfg.data["phone_live_session"] = "yes"
    assert agent.build_heartbeat()["live_session"] is False
    assert "phone_live_session" in agent_mod.AgentConfig.OPERATOR_KEYS
    assert "live_stream" in HEARTBEAT_KEYS and "live_session" in HEARTBEAT_KEYS
    assert "live_stream" not in sanitize_heartbeat({"live_stream": "yes", "evil": 1})
    assert sanitize_heartbeat({"live_stream": False, "live_session": True}) == {
        "live_stream": False, "live_session": True,
    }


def test_scheduler_source_does_not_touch_adb_or_the_pacing_floor():
    root = Path(__file__).resolve().parents[1] / "src" / "fleet"
    text = (root / "phone_schedule.py").read_text(encoding="utf-8")
    for bad in ("kill-server", "start-server", "tcpip", "reboot", "screencap", "input text"):
        assert bad not in text
    assert MIN_INTERVAL_SEC == 0.5 and MAX_CONCURRENT == 2


# ── 控制台 ────────────────────────────────────────────────────────────────────
_CONSOLE = Path(__file__).resolve().parents[1] / "domains" / "fleet_control" / "web" / "templates" / "fleet_console.html"
_HARNESS = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const js = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const els = {}, calls = [];
function el(id) {
  if (!els[id]) els[id] = {id, textContent: '', innerHTML: '', value: '', className: '', style: {}, attrs: {}, handlers: {},
    disabled: false, checked: false, setAttribute(k, v) { this.attrs[k] = String(v); }, getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; }, addEventListener(ev, fn) { this.handlers[ev] = fn; },
    scrollIntoView() {}, focus() {}, select() {}};
  return els[id];
}
global.document = {getElementById: el};
global.setInterval = () => 0; global.clearTimeout = () => {}; global.setTimeout = () => 1;
const step = JSON.parse(process.argv[3]);
function reply(d) { return Promise.resolve({ok: true, json: () => Promise.resolve(d)}); }
global.apiFetch = (url, opt) => {
  calls.push([(opt && opt.method) || 'GET', url, opt && opt.body ? JSON.parse(opt.body) : null]);
  if (url.startsWith('/api/fleet/overview')) return reply({nodes: {total: 1, online: 1}});
  if (url.startsWith('/api/fleet/nodes?')) return reply({nodes: step.nodes});
  if (url.startsWith('/api/fleet/tasks?')) return reply({tasks: []});
  if (url.indexOf('/schedule') >= 0 && (!opt || !opt.method || opt.method === 'GET'))
    return reply({ok: true, enabled: false, node_paused: false, plan: {active_hours: {start: '08:00', end: '23:00'}, daily: {}}, slots: []});
  return reply({pending: [], room_keys: [], ok: true});
};
eval(js);
const tick = () => new Promise((r) => setImmediate(r));
(async () => {
  for (let i = 0; i < 5; i++) await tick();
  const afterLoad = calls.map((c) => c[1]);
  if (step.plan) {
    const btn = {getAttribute: (k) => ({'data-n': step.plan.node, 'data-k': '__plan'})[k]};
    el('fc-list').handlers.click({target: {closest: () => btn}});
    for (let i = 0; i < 5; i++) await tick();
    const fields = step.plan.fields || {};
    Object.keys(fields).forEach((id) => { el(id).value = String(fields[id]); });
    if (step.plan.enabled) el('fc-plan-on').checked = true;
    el('fc-dlg-ok').handlers.click();
    for (let i = 0; i < 6; i++) await tick();
  }
  console.log(JSON.stringify({list: el('fc-list').innerHTML, dlg: el('fc-dlg-body').innerHTML,
    err: el('fc-dlg-err').textContent, toast: el('fc-toast').textContent, calls, afterLoad}));
})();
"""


def _console(tmp_path, step):
    node = __import__("shutil").which("node")
    if not node:
        pytest.skip("node not installed")
    path = tmp_path / "hplan.js"
    path.write_text(_HARNESS, encoding="utf-8")
    out = __import__("subprocess").run([node, str(path), str(_CONSOLE), json.dumps(step)],
                                       capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_console_plan_button_loads_on_click_and_saves(tmp_path):
    res = _console(tmp_path, {"nodes": [
        {"node_id": "n1", "status": "active", "state": "online", "label": "N1", "last_seen": 1.9e9,
         "caps": CAP, "remote_ops_enabled": True, "phones": [{"serial": "S1", "state": "device", "model": "Pixel"}]},
        {"node_id": "n2", "status": "active", "state": "online", "label": "N2", "caps": CAP, "remote_ops_enabled": False,
         "phones": [{"serial": "S1", "state": "device"}]},
    ], "plan": {"node": "n1", "enabled": True, "fields": {
        "fc-plan-warm": "1", "fc-plan-post-min": "1", "fc-plan-post-max": "2", "fc-plan-texts": "hello fleet",
        "fc-plan-like": "1", "fc-plan-comment": "0", "fc-plan-ctexts": "", "fc-plan-watch": "1",
    }}})
    assert res["list"].count("社交动作") == 1
    assert res["list"].count("每日计划") == 1
    assert all("/schedule" not in url for url in res["afterLoad"])
    gets = [c for c in res["calls"] if c[0] == "GET" and c[1].endswith("/schedule")]
    assert gets == [["GET", "/api/fleet/nodes/n1/schedule", None]]
    posts = [c for c in res["calls"] if c[0] == "POST"]
    assert posts[0][1] == "/api/fleet/schedule" and posts[0][2] == {"enabled": True}
    assert posts[1][1] == "/api/fleet/nodes/n1/schedule"
    assert posts[1][2]["paused"] is False
    assert posts[1][2]["plan"]["daily"]["post"]["texts"] == ["hello fleet"]
    assert posts[1][2]["plan"]["daily"]["warmup"]["count"] == 1
    assert "受保护的直播机" in res["dlg"]
    assert "hello fleet" not in res["dlg"]
