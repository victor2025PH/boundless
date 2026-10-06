"""每日计划：主控按手机把已有的社交动作排进活跃时段，到点再入队。

默认关（``phone_schedule`` 全局 ``enabled`` 只有 JSON true 才打开）。计划是数据
（``phone_schedule.json``，主控库里的覆盖盖过文件），不改节点上的点击、锁、
0.5 秒最短间隔或同时最多 2 路。节点仍是唯一会碰手机的一侧。

安全：受保护直播机不入队；心跳 ``live_stream`` 不是明确的 false 就整天不排
（缺字段也算，老 agent 不会被自动点）；全局 / 节点暂停，或心跳 ``live_session``
为 true 时，``pause_holds`` 里的动作让路（默认只让发帖）。活跃时段结束后不补发。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from .phone_flow_rules import FLOW_BY_KIND, HANDLE_RE, kind_for_flow
from .phone_ops import MAX_CONCURRENT
from .phone_rules import TEXT_ALLOWED, PhoneOpError, validate_payload, valid_serial
from .phones import is_protected
from .protocol import (
    PHONE_FLOW_TTL_SEC, REMOTE_PHONE_KINDS, SOCIAL_APPS, STATUS_PULLED, STATUS_QUEUED,
    TASK_PHONE_TEXT, missing_cap,
)

logger = logging.getLogger(__name__)

FLOW_ORDER = ("warmup", "post", "like", "comment", "watch", "follow", "dm")
_HOUR = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_UNSET = object()
UNSET = _UNSET
WATCH_INTERVAL_SEC = 60.0
ENV_WATCH = "CHATX_FLEET_SCHEDULE_WATCH"

SCHEDULE_SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_schedule (
    scope      TEXT PRIMARY KEY,
    plan_json  TEXT NOT NULL DEFAULT '',
    enabled    INTEGER,
    paused     INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phone_schedule_slots (
    slot_key    TEXT PRIMARY KEY,
    day         TEXT NOT NULL,
    node_id     TEXT NOT NULL,
    serial      TEXT NOT NULL DEFAULT '',
    idx         INTEGER NOT NULL,
    kind        TEXT NOT NULL DEFAULT '',
    app         TEXT NOT NULL DEFAULT '',
    kind_index  INTEGER NOT NULL DEFAULT 0,
    slot_at     REAL NOT NULL DEFAULT 0,
    window_end  REAL NOT NULL DEFAULT 0,
    plan_fp     TEXT NOT NULL DEFAULT '',
    state       TEXT NOT NULL DEFAULT 'pending',
    detail      TEXT NOT NULL DEFAULT '',
    task_id     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pss_lookup ON phone_schedule_slots(day, node_id, serial);
"""

_bundled_cache: Optional[Dict[str, Any]] = None
_watch_lock = threading.Lock()
_watch_started = False


class ScheduleError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = str(code)


def clock() -> float:
    return time.time()


def bundled_plan() -> Dict[str, Any]:
    """随包的完整默认计划（副本）。"""
    global _bundled_cache
    if _bundled_cache is None:
        raw = json.loads(Path(__file__).with_name("phone_schedule.json").read_text(encoding="utf-8"))
        _bundled_cache = _full_plan(raw)
    return copy.deepcopy(_bundled_cache)


def normalize_override(raw: Any) -> Dict[str, Any]:
    """操作员提交的片段。多余的键丢掉。不合法 → ScheduleError。"""
    if not isinstance(raw, dict):
        raise ScheduleError("bad_plan")
    out: Dict[str, Any] = {}
    if "active_hours" in raw:
        hours = raw.get("active_hours")
        if not isinstance(hours, dict):
            raise ScheduleError("bad_hours")
        start, end = _hhmm(hours.get("start")), _hhmm(hours.get("end"))
        if _minutes(end) <= _minutes(start):
            raise ScheduleError("bad_hours")
        out["active_hours"] = {"start": start, "end": end}
    if "tz_offset_min" in raw:
        out["tz_offset_min"] = _int(raw.get("tz_offset_min"), -720, 840, "bad_tz")
    if "jitter_sec" in raw:
        out["jitter_sec"] = _jitter(raw.get("jitter_sec"))
    if "min_gap_sec" in raw:
        out["min_gap_sec"] = _int(raw.get("min_gap_sec"), 0, 86400, "bad_gap")
    if "apps" in raw:
        out["apps"] = _apps(raw.get("apps"))
    if "pause_holds" in raw:
        out["pause_holds"] = _holds(raw.get("pause_holds"))
    if "daily" in raw:
        daily_raw = raw.get("daily")
        if not isinstance(daily_raw, dict):
            raise ScheduleError("bad_plan")
        daily: Dict[str, Any] = {}
        for flow, spec in daily_raw.items():
            if flow not in FLOW_ORDER:
                continue
            if not isinstance(spec, dict):
                raise ScheduleError("bad_plan")
            daily[flow] = _flow_override(flow, spec)
        out["daily"] = daily
    return out


def merge_plan(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    if not override:
        return out
    for key in ("tz_offset_min", "min_gap_sec"):
        if key in override:
            out[key] = override[key]
    if "jitter_sec" in override:
        out["jitter_sec"] = list(override["jitter_sec"])
    if "active_hours" in override:
        out["active_hours"] = dict(override["active_hours"])
    if "apps" in override:
        out["apps"] = list(override["apps"])
    if "pause_holds" in override:
        out["pause_holds"] = list(override["pause_holds"])
    for flow, spec in (override.get("daily") or {}).items():
        if flow not in out["daily"] or not isinstance(spec, dict):
            continue
        for k, v in spec.items():
            out["daily"][flow][k] = copy.deepcopy(v)
    return out


def effective_plan(*overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    plan = bundled_plan()
    for ov in overrides:
        if ov:
            plan = merge_plan(plan, ov)
    check_plan(plan)
    return plan


def check_plan(plan: Dict[str, Any]) -> None:
    if not isinstance(plan, dict):
        raise ScheduleError("bad_plan")
    hours = plan.get("active_hours") if isinstance(plan.get("active_hours"), dict) else {}
    if _minutes(_hhmm(hours.get("end"))) <= _minutes(_hhmm(hours.get("start"))):
        raise ScheduleError("bad_hours")
    _int(plan.get("tz_offset_min"), -720, 840, "bad_tz")
    lo, hi = _jitter(plan.get("jitter_sec"))
    if lo > hi:
        raise ScheduleError("bad_jitter")
    _int(plan.get("min_gap_sec"), 0, 86400, "bad_gap")
    apps = plan.get("apps")
    if not isinstance(apps, list) or not apps or any(a not in SOCIAL_APPS for a in apps):
        raise ScheduleError("bad_app")
    holds = plan.get("pause_holds")
    if holds != ["*"]:
        _holds(holds)
    daily = plan.get("daily") if isinstance(plan.get("daily"), dict) else {}
    for flow in FLOW_ORDER:
        spec = daily.get(flow)
        if not isinstance(spec, dict):
            raise ScheduleError("bad_plan")
        _flow_override(flow, spec)
    warm = daily["warmup"]
    if int(warm["likes"]) > int(warm["scrolls"]):
        raise ScheduleError("bad_likes")
    post = daily["post"]
    if int(post["min"]) > int(post["max"]):
        raise ScheduleError("bad_count")
    watch = daily["watch"]
    if int(watch["likes"]) > int(watch["watches"]):
        raise ScheduleError("bad_likes")


def local_window(now: float, plan: Dict[str, Any]) -> tuple:
    """返回 (本地日期 YYYY-MM-DD, 窗口起点 epoch, 窗口终点 epoch)。终点不含。"""
    off = int(plan["tz_offset_min"]) * 60
    local = float(now) + off
    dt = datetime.fromtimestamp(local, timezone.utc)
    day = dt.strftime("%Y-%m-%d")
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1_000_000
    midnight_utc = local - secs - off
    sh, sm = _hhmm(plan["active_hours"]["start"]).split(":")
    eh, em = _hhmm(plan["active_hours"]["end"]).split(":")
    start = midnight_utc + int(sh) * 3600 + int(sm) * 60
    end = midnight_utc + int(eh) * 3600 + int(em) * 60
    return day, start, end


def plan_day(plan: Dict[str, Any], *, day: str, node_id: str, serial: str,
             window_start: float, window_end: float) -> List[Dict[str, Any]]:
    """一天的槽（尚未入库）。同一天、同一部手机，种子固定，时刻不随墙钟变。"""
    check_plan(plan)
    rng = random.Random(_seed(day, node_id, serial))
    actions = _expand(plan, rng)
    times = _place_times(
        len(actions), window_start, window_end, int(plan["min_gap_sec"]),
        plan["jitter_sec"], rng,
    )
    fp = plan_fingerprint(plan)
    slots: List[Dict[str, Any]] = []
    for idx, act in enumerate(actions):
        kind = kind_for_flow(act["flow"])
        base = {
            "day": day, "node_id": node_id, "serial": serial, "idx": idx, "kind": kind,
            "app": act["app"], "kind_index": act["kind_index"], "window_end": window_end,
            "plan_fp": fp, "task_id": "", "detail": "",
        }
        if idx < len(times):
            base.update(slot_at=times[idx], state="pending")
        else:
            base.update(slot_at=window_end, state="skipped", detail="window_full")
        base["slot_key"] = f"{day}|{node_id}|{serial}|{idx}"
        slots.append(base)
    return slots


def plan_fingerprint(plan: Dict[str, Any]) -> str:
    """不含文案 / 账号名：改字幕不改时刻。"""
    daily = plan["daily"]
    slim = {
        "active_hours": plan["active_hours"],
        "tz_offset_min": plan["tz_offset_min"],
        "jitter_sec": plan["jitter_sec"],
        "min_gap_sec": plan["min_gap_sec"],
        "apps": plan["apps"],
        "pause_holds": plan["pause_holds"],
        "counts": {
            "warmup": {"count": daily["warmup"]["count"], "scrolls": daily["warmup"]["scrolls"],
                       "likes": daily["warmup"]["likes"]},
            "post": {"min": daily["post"]["min"], "max": daily["post"]["max"], "media": daily["post"]["media"]},
            "like": {"count": daily["like"]["count"], "scrolls": daily["like"]["scrolls"]},
            "comment": {"count": daily["comment"]["count"], "scrolls": daily["comment"]["scrolls"]},
            "watch": {"count": daily["watch"]["count"], "watches": daily["watch"]["watches"],
                      "likes": daily["watch"]["likes"]},
            "follow": {"count": daily["follow"]["count"]},
            "dm": {"count": daily["dm"]["count"]},
        },
    }
    return hashlib.sha256(json.dumps(slim, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def payload_for(plan: Dict[str, Any], *, kind: str, app: str, kind_index: int) -> tuple:
    """(payload, reason)。缺文案 / 账号名时 payload 为 None，reason 是原因码。"""
    flow = FLOW_BY_KIND.get(kind, "")
    spec = (plan.get("daily") or {}).get(flow) or {}
    if flow == "warmup":
        return {"app": app, "scrolls": spec["scrolls"], "likes": spec["likes"]}, ""
    if flow == "like":
        return {"app": app, "scrolls": spec["scrolls"]}, ""
    if flow == "watch":
        return {"app": app, "watches": spec["watches"], "likes": spec["likes"]}, ""
    if flow == "post":
        texts = list(spec.get("texts") or [])
        if kind_index >= len(texts):
            return None, "post_text_missing"
        return {"app": app, "text": texts[kind_index], "media": int(spec.get("media") or 0)}, ""
    if flow == "comment":
        texts = list(spec.get("texts") or [])
        if kind_index >= len(texts):
            return None, "comment_text_missing"
        return {"app": app, "text": texts[kind_index], "scrolls": spec["scrolls"]}, ""
    if flow == "follow":
        handles = list(spec.get("handles") or [])
        if kind_index >= len(handles):
            return None, "follow_handle_missing"
        return {"app": app, "handle": handles[kind_index]}, ""
    if flow == "dm":
        handles = list(spec.get("handles") or [])
        texts = list(spec.get("texts") or [])
        if kind_index >= len(handles) or kind_index >= len(texts):
            return None, "dm_incomplete"
        return {"app": app, "handle": handles[kind_index], "text": texts[kind_index]}, ""
    return None, "bad_flow"


def global_view(store: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    ts = clock() if now is None else float(now)

    def op(conn):
        gate = _read_gate(conn)
        plan = effective_plan(gate["plan"])
        day, start, end = local_window(ts, plan)
        return {
            "enabled": gate["enabled"], "paused": gate["paused"], "plan": plan, "override": gate["plan"],
            "day": day, "window_start": start, "window_end": end,
        }

    return store.schedule_txn(op)


def save_global(store: Any, *, enabled: Any = _UNSET, paused: Any = _UNSET, plan: Any = _UNSET,
                now: Optional[float] = None) -> Dict[str, Any]:
    ts = clock() if now is None else float(now)
    _check_flag("enabled", enabled, "bad_enabled")
    _check_flag("paused", paused, "bad_paused")
    norm = _UNSET if plan is _UNSET else (None if plan is None else normalize_override(plan))
    if norm not in (_UNSET, None):
        check_plan(effective_plan(norm))

    def op(conn):
        _write_scope(conn, "global", plan=norm, enabled=enabled, paused=paused, now=ts)
        return global_view_conn(conn, ts)

    return store.schedule_txn(op)


def global_view_conn(conn, now: float) -> Dict[str, Any]:
    gate = _read_gate(conn)
    plan = effective_plan(gate["plan"])
    day, start, end = local_window(now, plan)
    return {
        "enabled": gate["enabled"], "paused": gate["paused"], "plan": plan, "override": gate["plan"],
        "day": day, "window_start": start, "window_end": end,
    }


def node_view(store: Any, node_id: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    ts = clock() if now is None else float(now)
    node = store.get_node(node_id, now=ts)
    if node is None:
        raise ScheduleError("node_not_found")

    def op(conn):
        return _node_view_conn(conn, node, ts)

    return store.schedule_txn(op)


def save_node(store: Any, node_id: str, *, paused: Any = _UNSET, plan: Any = _UNSET,
              now: Optional[float] = None) -> Dict[str, Any]:
    ts = clock() if now is None else float(now)
    node = store.get_node(node_id, now=ts)
    if node is None:
        raise ScheduleError("node_not_found")
    _check_flag("paused", paused, "bad_paused")
    norm = _UNSET if plan is _UNSET else (None if plan is None else normalize_override(plan))

    def op(conn):
        if norm not in (_UNSET, None):
            gate = _read_gate(conn)
            check_plan(effective_plan(gate["plan"], norm))
        _write_scope(conn, f"node:{node_id}", plan=norm, enabled=_UNSET, paused=paused, now=ts)
        fresh = store.get_node(node_id, now=ts) or node
        return _node_view_conn(conn, fresh, ts)

    return store.schedule_txn(op)


def save_phone(store: Any, node_id: str, serial: str, *, plan: Any = _UNSET,
               now: Optional[float] = None) -> Dict[str, Any]:
    ts = clock() if now is None else float(now)
    target = valid_serial(serial)
    if not target:
        raise ScheduleError("bad_serial")
    if is_protected(target):
        raise ScheduleError("protected_phone")
    node = store.get_node(node_id, now=ts)
    if node is None:
        raise ScheduleError("node_not_found")
    if plan is _UNSET:
        raise ScheduleError("bad_plan")
    norm = None if plan is None else normalize_override(plan)

    def op(conn):
        if norm is not None:
            gate = _read_gate(conn)
            node_plan = _read_scope(conn, f"node:{node_id}")["plan"]
            check_plan(effective_plan(gate["plan"], node_plan, norm))
        _write_scope(conn, f"phone:{node_id}:{target}", plan=norm, enabled=_UNSET, paused=_UNSET, now=ts)
        fresh = store.get_node(node_id, now=ts) or node
        return _node_view_conn(conn, fresh, ts)

    return store.schedule_txn(op)


def list_runs(store: Any, *, node_id: str = "", day: str = "", serial: str = "") -> List[Dict[str, Any]]:
    def op(conn):
        return _public_rows(conn, node_id=node_id, day=day, serial=serial)

    return store.schedule_txn(op)


def run_tick(store: Any, *, now: float, dispatch: Optional[Callable[..., Dict[str, Any]]] = None) -> Dict[str, Any]:
    """走一轮。``now`` 必填给测试；后台线程传 ``clock()``。不睡眠、不碰设备。"""
    ts = float(now)
    gate = _gate(store)
    if not gate["enabled"]:
        return {"enabled": False, "paused": False, "dispatched": [], "held": [], "skipped": []}
    nodes = store.list_nodes(include_revoked=False, now=ts)
    out: Dict[str, Any] = {"enabled": True, "paused": gate["paused"], "dispatched": [], "held": [], "skipped": []}

    def op(conn):
        try:
            store._expire_locked(ts)
        except Exception:
            logger.warning("fleet phone schedule expire failed", exc_info=True)
        for node in nodes:
            try:
                _tick_node(store, conn, node, gate, ts, dispatch, out)
            except ScheduleError as e:
                _emit(out["skipped"], node_id=node.get("node_id") or "", serial="", kind="", app="", detail=e.code)
        return out

    return store.schedule_txn(op)


def start_schedule_watch(get_store: Callable[[], Any], *, interval_sec: float = WATCH_INTERVAL_SEC) -> Optional[threading.Thread]:
    """后台每分钟走一轮。总开关关着时一轮什么都不写。测试进程里不跑，避免碰用例的时钟。"""
    global _watch_started
    if _watch_off():
        return None
    with _watch_lock:
        if _watch_started:
            return None
        _watch_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval_sec)
            if os.environ.get("PYTEST_CURRENT_TEST") or _watch_off():
                continue
            try:
                st = get_store()
                if st is not None:
                    run_tick(st, now=clock())
            except Exception:
                logger.warning("fleet phone schedule tick failed", exc_info=True)

    thread = threading.Thread(target=_loop, name="fleet-phone-schedule", daemon=True)
    thread.start()
    logger.info("fleet phone schedule watch started (every %ss)", interval_sec)
    return thread


# ── 计划规范化 ───────────────────────────────────────────────────────────────
def _full_plan(raw: Any) -> Dict[str, Any]:
    plan = normalize_override(raw)
    check_plan(plan)
    return plan


def _int(value: Any, lo: int, hi: int, code: str) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ScheduleError(code)
    return value


def _hhmm(value: Any) -> str:
    if not isinstance(value, str) or not _HOUR.match(value):
        raise ScheduleError("bad_hours")
    return value


def _minutes(value: str) -> int:
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


def _jitter(value: Any) -> List[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ScheduleError("bad_jitter")
    lo = _int(value[0], 0, 7200, "bad_jitter")
    hi = _int(value[1], 0, 7200, "bad_jitter")
    if lo > hi:
        raise ScheduleError("bad_jitter")
    return [lo, hi]


def _apps(value: Any) -> List[str]:
    if not isinstance(value, list) or not value or len(value) > len(SOCIAL_APPS):
        raise ScheduleError("bad_app")
    out: List[str] = []
    for item in value:
        name = item.strip().lower() if isinstance(item, str) else ""
        if name not in SOCIAL_APPS or name in out:
            raise ScheduleError("bad_app")
        out.append(name)
    return out


def _holds(value: Any) -> List[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ScheduleError("bad_pause_holds")
    out: List[str] = []
    for item in value:
        name = item.strip().lower() if isinstance(item, str) else ""
        if name == "*":
            return ["*"]
        if name not in FLOW_ORDER or name in out:
            raise ScheduleError("bad_pause_holds")
        out.append(name)
    return out


def _texts(value: Any) -> List[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ScheduleError("bad_text")
    out: List[str] = []
    for item in value:
        try:
            out.append(validate_payload(TASK_PHONE_TEXT, {"text": item})["text"])
        except PhoneOpError as e:
            raise ScheduleError(e.code)
    return out


def _handles(value: Any) -> List[str]:
    if not isinstance(value, list) or len(value) > 8:
        raise ScheduleError("bad_handle")
    out: List[str] = []
    for item in value:
        if not isinstance(item, str) or not HANDLE_RE.match(item) or not TEXT_ALLOWED.match(item) or item.startswith("-"):
            raise ScheduleError("bad_handle")
        out.append(item)
    return out


_FLOW_INTS = {
    "warmup": {"count": (0, 8, "bad_count"), "scrolls": (1, 8, "bad_scrolls"), "likes": (0, 8, "bad_likes")},
    "post": {"min": (0, 4, "bad_count"), "max": (0, 4, "bad_count"), "media": (0, 1, "bad_media")},
    "like": {"count": (0, 12, "bad_count"), "scrolls": (0, 8, "bad_scrolls")},
    "comment": {"count": (0, 8, "bad_count"), "scrolls": (0, 8, "bad_scrolls")},
    "watch": {"count": (0, 8, "bad_count"), "watches": (1, 8, "bad_watches"), "likes": (0, 8, "bad_likes")},
    "follow": {"count": (0, 4, "bad_count")},
    "dm": {"count": (0, 4, "bad_count")},
}


def _flow_override(flow: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, (lo, hi, code) in _FLOW_INTS[flow].items():
        if key in spec:
            out[key] = _int(spec.get(key), lo, hi, code)
    if "texts" in spec:
        out["texts"] = _texts(spec.get("texts"))
    if "handles" in spec:
        out["handles"] = _handles(spec.get("handles"))
    return out


def _check_flag(name: str, value: Any, code: str) -> None:
    if value is _UNSET:
        return
    if not isinstance(value, bool):
        raise ScheduleError(code)


def _seed(day: str, node_id: str, serial: str) -> int:
    digest = hashlib.sha256(f"{day}|{node_id}|{serial}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _expand(plan: Dict[str, Any], rng: random.Random) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    daily = plan["daily"]
    for flow in FLOW_ORDER:
        spec = daily[flow]
        count = rng.randint(int(spec["min"]), int(spec["max"])) if flow == "post" else int(spec["count"])
        for index in range(count):
            items.append({"flow": flow, "kind_index": index})
    rng.shuffle(items)
    apps = plan["apps"]
    for i, item in enumerate(items):
        item["app"] = apps[i % len(apps)]
    return items


def _place_times(count: int, start: float, end: float, min_gap: int, jitter: Sequence[int],
                 rng: random.Random) -> List[float]:
    span = float(end) - float(start)
    if count <= 0 or span <= 0:
        return []
    gap = float(max(0, int(min_gap)))
    keep = count if gap == 0 else min(count, 1 + int((span - 1e-9) // gap))
    if keep <= 0:
        return []
    step = span / keep
    lo_j, hi_j = float(jitter[0]), float(jitter[1])
    times: List[float] = []
    for i in range(keep):
        bin_lo = start + i * step
        bin_hi = start + (i + 1) * step
        if hi_j <= lo_j:
            t = (bin_lo + bin_hi) / 2.0
        else:
            frac = (rng.uniform(lo_j, hi_j) - lo_j) / (hi_j - lo_j)
            t = bin_lo + frac * (bin_hi - bin_lo)
        if times and t < times[-1] + gap:
            t = times[-1] + gap
        if t >= end:
            break
        times.append(t)
    return times


# ── 库 ────────────────────────────────────────────────────────────────────────
def _loads(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data else None


def _read_scope(conn, scope: str) -> Dict[str, Any]:
    row = conn.execute(
        "SELECT plan_json, enabled, paused FROM phone_schedule WHERE scope=?", (scope,)
    ).fetchone()
    if row is None:
        return {"plan": None, "enabled": False, "paused": False}
    return {
        "plan": _loads(row["plan_json"]),
        "enabled": row["enabled"] == 1,
        "paused": row["paused"] == 1,
    }


def _read_gate(conn) -> Dict[str, Any]:
    return _read_scope(conn, "global")


def _gate(store: Any) -> Dict[str, Any]:
    return store.schedule_txn(_read_gate)


def _write_scope(conn, scope: str, *, plan: Any, enabled: Any, paused: Any, now: float) -> None:
    row = conn.execute("SELECT plan_json, enabled, paused FROM phone_schedule WHERE scope=?", (scope,)).fetchone()
    if row is None:
        plan_json, en, pa = "", None, None
    else:
        plan_json, en, pa = row["plan_json"], row["enabled"], row["paused"]
    if plan is not _UNSET:
        plan_json = "" if plan is None else json.dumps(plan, ensure_ascii=True, sort_keys=True)
    if enabled is not _UNSET:
        en = 1 if enabled is True else 0
    if paused is not _UNSET:
        pa = 1 if paused is True else 0
    if row is None:
        conn.execute(
            "INSERT INTO phone_schedule(scope, plan_json, enabled, paused, updated_at) VALUES (?,?,?,?,?)",
            (scope, plan_json, en, pa, now),
        )
    else:
        conn.execute(
            "UPDATE phone_schedule SET plan_json=?, enabled=?, paused=?, updated_at=? WHERE scope=?",
            (plan_json, en, pa, now, scope),
        )


def _public(row) -> Dict[str, Any]:
    return {
        "slot_key": row["slot_key"], "day": row["day"], "node_id": row["node_id"], "serial": row["serial"],
        "idx": row["idx"], "kind": row["kind"], "app": row["app"], "slot_at": row["slot_at"],
        "state": row["state"], "detail": row["detail"], "task_id": row["task_id"],
    }


def _public_rows(conn, *, node_id: str = "", day: str = "", serial: str = "") -> List[Dict[str, Any]]:
    sql, args = "SELECT * FROM phone_schedule_slots WHERE 1=1", []
    if node_id:
        sql += " AND node_id=?"; args.append(node_id)
    if day:
        sql += " AND day=?"; args.append(day)
    if serial:
        sql += " AND serial=?"; args.append(serial)
    sql += " ORDER BY slot_at, idx LIMIT 500"
    return [_public(r) for r in conn.execute(sql, args).fetchall()]


def _node_view_conn(conn, node: Dict[str, Any], now: float) -> Dict[str, Any]:
    gate = _read_gate(conn)
    node_id = node["node_id"]
    node_scope = _read_scope(conn, f"node:{node_id}")
    try:
        plan = effective_plan(gate["plan"], node_scope["plan"])
    except ScheduleError:
        plan = bundled_plan()
    day, wstart, wend = local_window(now, plan)
    phones_out = []
    for phone in node.get("phones") or []:
        if not isinstance(phone, dict):
            continue
        serial = str(phone.get("serial") or "")
        protected = is_protected(serial)
        override = None if protected or not serial else _read_scope(conn, f"phone:{node_id}:{serial}")["plan"]
        pp = None
        if not protected and serial:
            try:
                pp = effective_plan(gate["plan"], node_scope["plan"], override)
            except ScheduleError:
                pp = None
        phones_out.append({
            "serial": serial, "model": phone.get("model") or "", "state": phone.get("state") or "",
            "protected": protected, "plan": pp, "override": override,
        })
    stored = _public_rows(conn, node_id=node_id, day=day)
    slots = list(stored)
    seen = {s["serial"] for s in stored}
    for phone in phones_out:
        if phone["serial"] in seen:
            continue
        if phone["protected"]:
            slots.append({
                "slot_key": "", "day": day, "node_id": node_id, "serial": phone["serial"], "idx": -1,
                "kind": "", "app": "", "slot_at": 0, "state": "skipped", "detail": "protected_phone", "task_id": "",
            })
            continue
        pp = phone["plan"]
        if not pp:
            continue
        try:
            pday, pstart, pend = local_window(now, pp)
        except ScheduleError:
            continue
        for slot in plan_day(pp, day=pday, node_id=node_id, serial=phone["serial"],
                             window_start=pstart, window_end=pend):
            pub = {k: slot[k] for k in ("slot_key", "day", "node_id", "serial", "idx", "kind", "app", "slot_at")}
            pub.update(state="preview", detail=slot["detail"], task_id="")
            slots.append(pub)
    return {
        "enabled": gate["enabled"], "paused": gate["paused"], "node_paused": node_scope["paused"],
        "plan": plan, "override": node_scope["plan"], "day": day, "window_start": wstart, "window_end": wend,
        "phones": phones_out, "slots": slots,
    }


def _rows(conn, *, day: str, node_id: str, serial: Optional[str] = None):
    if serial is None:
        return conn.execute(
            "SELECT * FROM phone_schedule_slots WHERE day=? AND node_id=? ORDER BY idx",
            (day, node_id),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM phone_schedule_slots WHERE day=? AND node_id=? AND serial=? ORDER BY idx",
        (day, node_id, serial),
    ).fetchall()


def _insert_slot(conn, slot: Dict[str, Any], *, now: float) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO phone_schedule_slots(slot_key, day, node_id, serial, idx, kind, app, kind_index, "
        "slot_at, window_end, plan_fp, state, detail, task_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slot["slot_key"], slot["day"], slot["node_id"], slot["serial"], slot["idx"], slot["kind"], slot["app"],
         slot.get("kind_index", 0), slot["slot_at"], slot.get("window_end", 0), slot.get("plan_fp", ""),
         slot["state"], _ascii(slot.get("detail") or ""), slot.get("task_id") or "", now),
    )


def _ascii(detail: str) -> str:
    return "".join(ch for ch in str(detail or "") if ch.isascii() and ch not in "\r\n")[:80]


def _mark(conn, slot_key: str, *, state: str, detail: str, task_id: Optional[str] = None) -> None:
    if task_id is None:
        conn.execute("UPDATE phone_schedule_slots SET state=?, detail=? WHERE slot_key=?",
                     (state, _ascii(detail), slot_key))
    else:
        conn.execute("UPDATE phone_schedule_slots SET state=?, detail=?, task_id=? WHERE slot_key=?",
                     (state, _ascii(detail), task_id, slot_key))


def _ensure_day_row(conn, *, day: str, node_id: str, detail: str, state: str, now: float, window_end: float) -> bool:
    key = f"{day}|{node_id}|*|{detail}"
    if conn.execute("SELECT 1 FROM phone_schedule_slots WHERE slot_key=?", (key,)).fetchone() is not None:
        return False
    _insert_slot(conn, {
        "slot_key": key, "day": day, "node_id": node_id, "serial": "", "idx": -1, "kind": "", "app": "",
        "kind_index": 0, "slot_at": 0, "window_end": window_end, "plan_fp": "", "state": state,
        "detail": detail, "task_id": "",
    }, now=now)
    return True


# ── 一轮调度 ─────────────────────────────────────────────────────────────────
def _emit(bucket: List[Dict[str, Any]], *, node_id: str, serial: str, kind: str, app: str,
          detail: str = "", task_id: str = "") -> None:
    item = {"node_id": node_id, "serial": serial, "kind": kind, "app": app}
    if detail:
        item["detail"] = _ascii(detail)
    if task_id:
        item["task_id"] = task_id
    bucket.append(item)


def _tick_node(store, conn, node: Dict[str, Any], gate: Dict[str, Any], now: float,
               dispatch: Optional[Callable], out: Dict[str, Any]) -> None:
    node_id = node["node_id"]
    hb = node.get("last_heartbeat") if isinstance(node.get("last_heartbeat"), dict) else {}
    node_scope = _read_scope(conn, f"node:{node_id}")
    try:
        plan = effective_plan(gate["plan"], node_scope["plan"])
    except ScheduleError as e:
        _emit(out["skipped"], node_id=node_id, serial="", kind="", app="", detail=e.code)
        return
    day, _wstart, wend = local_window(now, plan)
    live = hb.get("live_stream", None)
    if "live_stream" not in hb:
        live = None
    if live is True:
        _block_live(store, conn, node_id, day, "live_stream_host", now, wend, out)
        return
    if live is not False:
        _hold_unknown(conn, node_id, day, now, wend, out)
        return
    conn.execute(
        "DELETE FROM phone_schedule_slots WHERE day=? AND node_id=? AND serial='' AND detail=?",
        (day, node_id, "live_stream_unknown"),
    )
    if conn.execute(
        "SELECT 1 FROM phone_schedule_slots WHERE day=? AND node_id=? AND serial='' AND detail=? LIMIT 1",
        (day, node_id, "live_stream_host"),
    ).fetchone() is not None:
        return
    holds = _active_holds(plan, gate["paused"] or node_scope["paused"], hb.get("live_session") is True)
    hold_detail = "live_session" if hb.get("live_session") is True else ("paused" if (gate["paused"] or node_scope["paused"]) else "")
    budget, busy = _inflight(conn, node_id)
    for phone in node.get("phones") or []:
        if not isinstance(phone, dict):
            continue
        serial = str(phone.get("serial") or "")
        if not serial:
            continue
        if is_protected(serial):
            key = f"{day}|{node_id}|{serial}|protected"
            fresh = _ensure_protected(conn, key=key, day=day, node_id=node_id, serial=serial, now=now, window_end=wend)
            if fresh:
                _emit(out["skipped"], node_id=node_id, serial=serial, kind="", app="", detail="protected_phone")
            continue
        if not valid_serial(serial):
            continue
        override = _read_scope(conn, f"phone:{node_id}:{serial}")["plan"]
        try:
            phone_plan = effective_plan(gate["plan"], node_scope["plan"], override)
        except ScheduleError as e:
            _emit(out["skipped"], node_id=node_id, serial=serial, kind="", app="", detail=e.code)
            continue
        pday, pstart, pend = local_window(now, phone_plan)
        slots = _materialize(conn, phone_plan, day=pday, node_id=node_id, serial=serial,
                             window_start=pstart, window_end=pend, now=now, out=out)
        for slot in sorted(slots, key=lambda s: (float(s["slot_at"]), int(s["idx"]))):
            if slot["state"] != "pending":
                continue
            if now < pstart or now < float(slot["slot_at"]):
                continue
            flow = FLOW_BY_KIND.get(slot["kind"], "")
            if now >= pend:
                reason = "missed_while_paused" if flow in holds else (slot["detail"] or "window_closed")
                _mark(conn, slot["slot_key"], state="skipped", detail=reason)
                _emit(out["skipped"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"], detail=reason)
                continue
            if flow in holds:
                _mark(conn, slot["slot_key"], state="pending", detail=hold_detail or "paused")
                _emit(out["held"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"],
                      detail=hold_detail or "paused")
                continue
            payload, why = payload_for(phone_plan, kind=slot["kind"], app=slot["app"], kind_index=int(slot["kind_index"]))
            if why or payload is None:
                # 缺文案不占并发名额，原因码保持「还没填」，不要被「手机忙」盖住。
                _mark(conn, slot["slot_key"], state="pending", detail=why or "bad_payload")
                _emit(out["held"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"],
                      detail=why or "bad_payload")
                continue
            reason = _ready_reason(node, serial, slot["kind"])
            if reason:
                if now >= pend:
                    _mark(conn, slot["slot_key"], state="skipped", detail=reason)
                    _emit(out["skipped"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"], detail=reason)
                else:
                    _mark(conn, slot["slot_key"], state="pending", detail=reason)
                    _emit(out["held"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"], detail=reason)
                continue
            if serial in busy or budget <= 0:
                why = "phone_busy" if serial in busy else "node_busy"
                _mark(conn, slot["slot_key"], state="pending", detail=why)
                _emit(out["held"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"], detail=why)
                continue
            result = _dispatch(store, dispatch, node_id=node_id, serial=serial, kind=slot["kind"],
                               payload=payload, now=now)
            if result.get("ok") and result.get("task_id"):
                _mark(conn, slot["slot_key"], state="dispatched", detail="", task_id=str(result["task_id"]))
                budget -= 1
                busy.add(serial)
                slot["state"] = "dispatched"
                _emit(out["dispatched"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"],
                      task_id=str(result["task_id"]))
            else:
                detail = _ascii(result.get("detail") or "enqueue_refused")
                terminal = detail in {"bad_payload", "bad_flow", "protected_phone", "bad_serial"}
                _mark(conn, slot["slot_key"], state="skipped" if terminal else "pending", detail=detail)
                bucket = out["skipped"] if terminal else out["held"]
                _emit(bucket, node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"], detail=detail)


def _active_holds(plan: Dict[str, Any], paused: bool, live_session: bool) -> set:
    if not paused and not live_session:
        return set()
    holds = plan.get("pause_holds") or []
    if "*" in holds:
        return set(FLOW_ORDER)
    return set(holds)


def _block_live(store, conn, node_id: str, day: str, detail: str, now: float, window_end: float,
                out: Dict[str, Any]) -> None:
    created = _ensure_day_row(conn, day=day, node_id=node_id, detail=detail, state="skipped", now=now, window_end=window_end)
    pending = conn.execute(
        "SELECT slot_key, kind, app, serial, task_id, state FROM phone_schedule_slots "
        "WHERE day=? AND node_id=? AND serial<>'' AND state='pending'",
        (day, node_id),
    ).fetchall()
    for row in pending:
        _mark(conn, row["slot_key"], state="skipped", detail=detail)
        _emit(out["skipped"], node_id=node_id, serial=row["serial"], kind=row["kind"], app=row["app"], detail=detail)
    for row in conn.execute(
        "SELECT slot_key, task_id, kind, app, serial FROM phone_schedule_slots "
        "WHERE day=? AND node_id=? AND state='dispatched' AND task_id<>''",
        (day, node_id),
    ).fetchall():
        if store.cancel(row["task_id"], now=now):
            _mark(conn, row["slot_key"], state="skipped", detail=detail)
            _emit(out["skipped"], node_id=node_id, serial=row["serial"], kind=row["kind"], app=row["app"], detail=detail)
    if created and not pending:
        _emit(out["skipped"], node_id=node_id, serial="", kind="", app="", detail=detail)


def _hold_unknown(conn, node_id: str, day: str, now: float, window_end: float, out: Dict[str, Any]) -> None:
    detail = "live_stream_unknown"
    state = "skipped" if now >= window_end else "pending"
    key = f"{day}|{node_id}|*|{detail}"
    row = conn.execute("SELECT state FROM phone_schedule_slots WHERE slot_key=?", (key,)).fetchone()
    if row is None:
        _ensure_day_row(conn, day=day, node_id=node_id, detail=detail, state=state, now=now, window_end=window_end)
        bucket = out["skipped"] if state == "skipped" else out["held"]
        _emit(bucket, node_id=node_id, serial="", kind="", app="", detail=detail)
    elif state == "skipped" and row["state"] != "skipped":
        _mark(conn, key, state="skipped", detail=detail)
        _emit(out["skipped"], node_id=node_id, serial="", kind="", app="", detail=detail)
    phone_rows = _rows(conn, day=day, node_id=node_id)
    for slot in phone_rows:
        if slot["serial"] and slot["state"] == "pending":
            if now >= float(slot["window_end"] or window_end):
                _mark(conn, slot["slot_key"], state="skipped", detail=detail)
                _emit(out["skipped"], node_id=node_id, serial=slot["serial"], kind=slot["kind"], app=slot["app"], detail=detail)
            else:
                _mark(conn, slot["slot_key"], state="pending", detail=detail)
                _emit(out["held"], node_id=node_id, serial=slot["serial"], kind=slot["kind"], app=slot["app"], detail=detail)


def _ensure_protected(conn, *, key: str, day: str, node_id: str, serial: str, now: float, window_end: float) -> bool:
    if conn.execute("SELECT 1 FROM phone_schedule_slots WHERE slot_key=?", (key,)).fetchone() is not None:
        return False
    _insert_slot(conn, {
        "slot_key": key, "day": day, "node_id": node_id, "serial": serial, "idx": -1, "kind": "", "app": "",
        "kind_index": 0, "slot_at": 0, "window_end": window_end, "plan_fp": "", "state": "skipped",
        "detail": "protected_phone", "task_id": "",
    }, now=now)
    return True


def _materialize(conn, plan, *, day, node_id, serial, window_start, window_end, now, out) -> List[Dict[str, Any]]:
    existing = [dict(r) for r in _rows(conn, day=day, node_id=node_id, serial=serial)]
    fp = plan_fingerprint(plan)
    if existing:
        if any(r["state"] != "pending" for r in existing):
            return existing
        if existing[0]["plan_fp"] == fp:
            return existing
        conn.execute("DELETE FROM phone_schedule_slots WHERE day=? AND node_id=? AND serial=?", (day, node_id, serial))
    built = plan_day(plan, day=day, node_id=node_id, serial=serial, window_start=window_start, window_end=window_end)
    for slot in built:
        _insert_slot(conn, slot, now=now)
        if slot["state"] == "skipped":
            _emit(out["skipped"], node_id=node_id, serial=serial, kind=slot["kind"], app=slot["app"], detail=slot["detail"])
    return built


def _inflight(conn, node_id: str) -> tuple:
    marks = ",".join("?" * len(REMOTE_PHONE_KINDS))
    rows = conn.execute(
        f"SELECT target_json FROM node_tasks WHERE node_id=? AND status IN (?, ?) AND kind IN ({marks})",
        (node_id, STATUS_QUEUED, STATUS_PULLED, *REMOTE_PHONE_KINDS),
    ).fetchall()
    busy = set()
    for row in rows:
        try:
            target = json.loads(row["target_json"] or "{}")
        except (TypeError, ValueError):
            target = {}
        serial = target.get("serial") if isinstance(target, dict) else ""
        if serial:
            busy.add(str(serial))
    return max(0, MAX_CONCURRENT - len(rows)), busy


def _ready_reason(node: Dict[str, Any], serial: str, kind: str) -> str:
    if node.get("state") != "online":
        return "node_offline"
    if not node.get("remote_ops_enabled"):
        return "remote_ops_disabled"
    cap = missing_cap(kind, node.get("caps"))
    if cap:
        return f"node_lacks_cap:{cap}"
    phone = next((p for p in node.get("phones") or [] if isinstance(p, dict) and p.get("serial") == serial), None)
    if phone is None:
        return "phone_not_reported"
    if phone.get("state") != "device":
        return f"phone_not_ready:{phone.get('state') or 'unknown'}"
    return ""


def _dispatch(store, dispatch, *, node_id: str, serial: str, kind: str, payload: Dict[str, Any], now: float) -> Dict[str, Any]:
    if is_protected(serial):
        return {"ok": False, "detail": "protected_phone"}
    if dispatch is not None:
        return dispatch(node_id=node_id, serial=serial, kind=kind, payload=payload, now=now) or {"ok": False, "detail": "enqueue_refused"}
    from .phone_flow_rules import validate_flow_payload
    from .phone_rules import check_target
    try:
        target = check_target(serial)
        body = validate_flow_payload(kind, payload)
    except PhoneOpError as e:
        return {"ok": False, "detail": e.code}
    rec = store.enqueue(node_id, kind, payload=body, target={"serial": target}, ttl_sec=PHONE_FLOW_TTL_SEC,
                        created_by="schedule", now=now)
    if rec is None:
        refusal = ""
        try:
            refusal = store.task_refusal(node_id, kind)
        except Exception:
            refusal = ""
        return {"ok": False, "detail": refusal or "enqueue_refused"}
    return {"ok": True, "task_id": rec["task_id"]}


def _watch_off() -> bool:
    return str(os.environ.get(ENV_WATCH, "1")).strip().lower() in {"0", "false", "no", "off"}


__all__ = [
    "FLOW_ORDER", "SCHEDULE_SCHEMA", "ScheduleError", "bundled_plan", "normalize_override", "merge_plan",
    "effective_plan", "check_plan", "local_window", "plan_day", "plan_fingerprint", "payload_for",
    "global_view", "save_global", "node_view", "save_node", "save_phone", "list_runs", "run_tick",
    "start_schedule_watch", "clock", "UNSET",
]
