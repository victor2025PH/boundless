"""把发帖 / 点赞 / 评论 / 关注，以及养号 / 私信 / 看短视频，编译成已有的截图、点击、滑动、按键、输入。

坐标在 ``phone_ui_map.json``（或 agent.json ``phone_ui_map`` 指向的文件）里，按应用分开，
单位是屏幕千分比。agent.json 里的路径优先；路径为空时用本模块旁边的那份，那份不在时再用
fleet 状态目录里的 ``phone_ui_map.json``。每次动作先截一张图量屏幕，再把千分比换成像素。不新增 adb 动词：
打开应用是 HOME 再点该应用的 ``app_icon`` 锚点；带图是点相册里已经在手机上的那一格。
停留（dwell）只在本机睡一会儿，不发 adb。

``phone_flows_enabled`` 缺省关。关着时不声明 phone_flows_v1 / phone_flows_v2，执行直接拒绝，不碰 adb。
回传只留应用名、动作、步数、字数和次数，不带回帖子、评论、私信原文或账号名。

dry_run (or predict_only) JSON true compiles the UI map at 1000x1000 permille and
returns the planned taps/text. It does not open an adb session, take a screenshot,
or send input. A missing flag is real execution, and only when flows are enabled.

Facebook ``phone_like`` does not tap the fixed ``like_button``. It screenshots,
finds the Like control (text row, thumbs-up template, action-bar slot), and taps
only when two signals agree or the template score is high. The fixed coordinate
stays in the dry_run plan and is marked deprecated. Warmup and watch still use it.
``like_probe`` launches and scrolls and does not tap Like. No posts after the
swipe budget is ``empty_feed``.

核对和随机间隔写在坐标文件的 ``robust`` 里，默认关。agent.json 的 ``phone_flow_verify`` /
``phone_flow_jitter_ms`` 可以盖过文件：只有 JSON ``true`` 才强制核对；抖动形状不对就仍用文件里的。
"""

from __future__ import annotations

import json
import random
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .phone_flow_robust import (
    MAX_DWELL_MS, MAX_JITTER_MS, MAX_RETRIES, MIN_DWELL_MS, dwell_seconds, frame_token, jitter_seconds,
    probe_matches, resolve_policy,
)
from .phone_flow_rules import FLOW_BY_KIND, validate_flow_payload
from .phone_rules import KEYCODES, SWIPE_MS_MAX, SWIPE_MS_MIN, PhoneOpError, check_target
from .phones import is_excluded
from .protocol import (
    PHONE_FLOW_KINDS, PHONE_SESSION_KINDS, SESSION_FLOW_NAMES, SOCIAL_APPS, STATUS_DONE, STATUS_FAILED,
    STATUS_REJECTED, TASK_PHONE_KEY, TASK_PHONE_POST, TASK_PHONE_SCREENSHOT, TASK_PHONE_SWIPE, TASK_PHONE_TAP,
    TASK_PHONE_TEXT,
)

_BUNDLED = Path(__file__).with_name("phone_ui_map.json")
_MAX_MAP_BYTES = 256 * 1024
_FLOW_KEYS = ("post", "post_media", "like", "comment", "follow", "warmup", "dm", "watch")
_STEP_KEYS = {
    "tap": {"op", "anchor", "slot_field", "slot_stride", "expect"},
    "swipe": {"op", "from", "to", "duration_ms", "repeat", "expect"},
    "key": {"op", "key", "expect"},
    "text": {"op", "field", "expect"},
    "dwell": {"op", "lo_ms", "hi_ms", "expect"},
}
_NESTED_EXTRA = {"when", "every"}
_DWELL_KIND = "dwell"
_DRY_WIDTH = 1000
_DRY_HEIGHT = 1000
_PLAN_MAX_STEPS = 128
_NAME_RE_SRC = r"^[a-z][a-z0-9_]{0,31}$"
_UNSET = object()
_CHECK_FAILS = ("screen_not_reached", "not_logged_in", "app_not_ready", "anchor_mismatch", "thread_not_open")

Step = Tuple[str, Dict[str, Any]]


def _invalid() -> None:
    raise PhoneOpError("ui_map_invalid")


def _as_int(v: Any, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        _invalid()
    return v


def _name(v: Any) -> str:
    if not isinstance(v, str) or not re.match(_NAME_RE_SRC, v):
        _invalid()
    return v


def _rgb(v: Any) -> Tuple[int, int, int]:
    if not isinstance(v, (list, tuple)) or len(v) != 3:
        _invalid()
    return (_as_int(v[0], 0, 255), _as_int(v[1], 0, 255), _as_int(v[2], 0, 255))


def _expect(raw: Any, probes: Dict[str, Any]) -> Dict[str, Any]:
    if raw == "change":
        return {"change": True}
    if not isinstance(raw, dict) or not raw or set(raw) - {"change", "probe", "retry"}:
        _invalid()
    out: Dict[str, Any] = {}
    if "change" in raw:
        if not isinstance(raw.get("change"), bool):
            _invalid()
        if raw["change"]:
            out["change"] = True
    if "probe" in raw:
        probe = _name(raw.get("probe"))
        if probe not in probes:
            _invalid()
        out["probe"] = probe
    if "retry" in raw:
        out["retry"] = _as_int(raw.get("retry"), 0, MAX_RETRIES)
    if "change" not in out and "probe" not in out:
        _invalid()
    return out


def _probes(raw: Any, anchors: Dict[str, Tuple[int, int]]) -> Dict[str, Dict[str, Any]]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > 16:
        _invalid()
    out: Dict[str, Dict[str, Any]] = {}
    for name, spec in raw.items():
        name = _name(name)
        if not isinstance(spec, dict) or set(spec) - {"at", "rgb", "tol", "note"}:
            _invalid()
        at = _name(spec.get("at"))
        if at not in anchors:
            _invalid()
        tol = 32
        if "tol" in spec:
            tol = _as_int(spec.get("tol"), 0, 255)
        out[name] = {"at": at, "rgb": list(_rgb(spec.get("rgb"))), "tol": tol}
    return out


def _names(raw: Any, probes: Dict[str, Any]) -> List[str]:
    if not isinstance(raw, list) or len(raw) > 8:
        _invalid()
    out = []
    for item in raw:
        name = _name(item)
        if name not in probes or name in out:
            _invalid()
        out.append(name)
    return out


def _tap_anchors(steps: List[Dict[str, Any]]) -> set:
    found = set()
    for step in steps:
        if step.get("op") == "tap":
            found.add(step.get("anchor"))
        elif step.get("op") == "repeat":
            found |= _tap_anchors(step.get("steps") or [])
    return found


def _validate_step(step: Any, anchors: Dict[str, Tuple[int, int]], probes: Dict[str, Any], *,
                   nested: bool) -> Dict[str, Any]:
    if not isinstance(step, dict):
        _invalid()
    op = step.get("op")
    if op == "repeat":
        if nested or set(step) - {"op", "count", "steps"}:
            _invalid()
        count = step.get("count")
        if count not in ("scrolls", "watches"):
            count = _as_int(count, 0, 8)
        inner = step.get("steps")
        if not isinstance(inner, list) or not inner or len(inner) > 8:
            _invalid()
        return {
            "op": "repeat", "count": count,
            "steps": [_validate_step(item, anchors, probes, nested=True) for item in inner],
        }
    if op not in _STEP_KEYS:
        _invalid()
    allowed = set(_STEP_KEYS[op])
    if nested:
        allowed |= _NESTED_EXTRA
        if op == "swipe" and "repeat" in step:
            _invalid()
    if set(step) - allowed:
        _invalid()
    clean: Dict[str, Any] = {"op": op}
    if "when" in step or "every" in step:
        when = step.get("when")
        if when == "like":
            if op != "tap" or "every" in step:
                _invalid()
            clean["when"] = "like"
        elif when == "dwell":
            if op != "dwell":
                _invalid()
            clean["when"] = "dwell"
            clean["every"] = _as_int(step.get("every"), 2, 8)
        else:
            _invalid()
    if op == "tap":
        clean["anchor"] = _name(step.get("anchor"))
        if clean["anchor"] not in anchors:
            _invalid()
        has_slot = "slot_field" in step or "slot_stride" in step
        if has_slot:
            if step.get("slot_field") != "media_slot":
                _invalid()
            stride = _name(step.get("slot_stride"))
            if stride not in anchors:
                _invalid()
            clean["slot_field"] = "media_slot"
            clean["slot_stride"] = stride
    elif op == "swipe":
        clean["from"] = _name(step.get("from"))
        clean["to"] = _name(step.get("to"))
        if clean["from"] not in anchors or clean["to"] not in anchors:
            _invalid()
        if "duration_ms" in step:
            clean["duration_ms"] = _as_int(step.get("duration_ms"), SWIPE_MS_MIN, SWIPE_MS_MAX)
        if "repeat" in step:
            rep = step.get("repeat")
            if rep == "scrolls":
                clean["repeat"] = "scrolls"
            else:
                clean["repeat"] = _as_int(rep, 0, 8)
    elif op == "key":
        key = step.get("key")
        if key not in KEYCODES:
            _invalid()
        clean["key"] = key
    elif op == "text":
        field = step.get("field")
        if field not in ("text", "handle"):
            _invalid()
        clean["field"] = field
    else:
        lo = _as_int(step.get("lo_ms"), MIN_DWELL_MS, MAX_DWELL_MS)
        hi = _as_int(step.get("hi_ms"), MIN_DWELL_MS, MAX_DWELL_MS)
        if lo > hi:
            _invalid()
        clean["lo_ms"] = lo
        clean["hi_ms"] = hi
    if "expect" in step:
        clean["expect"] = _expect(step.get("expect"), probes)
    return clean


def _validate_app(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"anchors", "flows", "note", "probes", "preflight"}:
        _invalid()
    anchors_raw = raw.get("anchors")
    flows_raw = raw.get("flows")
    if not isinstance(anchors_raw, dict) or not anchors_raw or len(anchors_raw) > 48:
        _invalid()
    if not isinstance(flows_raw, dict) or set(flows_raw) != set(_FLOW_KEYS):
        _invalid()
    anchors: Dict[str, Tuple[int, int]] = {}
    for name, pt in anchors_raw.items():
        name = _name(name)
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            _invalid()
        anchors[name] = (_as_int(pt[0], 0, 1000), _as_int(pt[1], 0, 1000))
    probes = _probes(raw.get("probes"), anchors) if "probes" in raw else {}
    flows: Dict[str, List[Dict[str, Any]]] = {}
    for flow in _FLOW_KEYS:
        steps_raw = flows_raw.get(flow)
        if not isinstance(steps_raw, list) or not steps_raw or len(steps_raw) > 32:
            _invalid()
        flows[flow] = [_validate_step(step, anchors, probes, nested=False) for step in steps_raw]
    preflight = None
    if "preflight" in raw:
        pf = raw.get("preflight")
        if not isinstance(pf, dict) or set(pf) - {"after_anchor", "require", "forbid", "note", "thread"}:
            _invalid()
        after = _name(pf.get("after_anchor"))
        if after not in anchors:
            _invalid()
        require = _names(pf["require"], probes) if "require" in pf else []
        forbid = _names(pf["forbid"], probes) if "forbid" in pf else []
        if not require and not forbid:
            _invalid()
        for steps in flows.values():
            if not any(s.get("op") == "tap" and s.get("anchor") == after for s in steps):
                _invalid()
        preflight = {"after_anchor": after, "require": require, "forbid": forbid}
        if "thread" in pf:
            th = pf.get("thread")
            if not isinstance(th, dict) or set(th) - {"after_anchor", "require", "forbid", "note"}:
                _invalid()
            th_after = _name(th.get("after_anchor"))
            if th_after not in anchors:
                _invalid()
            th_require = _names(th["require"], probes) if "require" in th else []
            th_forbid = _names(th["forbid"], probes) if "forbid" in th else []
            if not th_require and not th_forbid:
                _invalid()
            if th_after not in _tap_anchors(flows.get("dm") or []):
                _invalid()
            preflight["thread"] = {"after_anchor": th_after, "require": th_require, "forbid": th_forbid}
    out: Dict[str, Any] = {"anchors": anchors, "flows": flows, "probes": probes}
    if preflight is not None:
        out["preflight"] = preflight
    return out


def _robust(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {"verify": False, "retries": 2, "jitter_ms": [0, 0]}
    if not isinstance(raw, dict) or set(raw) - {"verify", "retries", "jitter_ms", "note"}:
        _invalid()
    verify = False
    if "verify" in raw:
        if not isinstance(raw.get("verify"), bool):
            _invalid()
        verify = raw["verify"]
    retries = 2
    if "retries" in raw:
        retries = _as_int(raw.get("retries"), 0, MAX_RETRIES)
    jitter = [0, 0]
    if "jitter_ms" in raw:
        pair = raw.get("jitter_ms")
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            _invalid()
        lo, hi = _as_int(pair[0], 0, MAX_JITTER_MS), _as_int(pair[1], 0, MAX_JITTER_MS)
        if lo > hi:
            _invalid()
        jitter = [lo, hi]
    if "note" in raw and (not isinstance(raw.get("note"), str) or len(raw["note"]) > 400):
        _invalid()
    return {"verify": verify, "retries": retries, "jitter_ms": jitter}


def validate_ui_map(data: Any) -> Dict[str, Any]:
    """坐标文件 → 规范化结构。形状不对 → PhoneOpError('ui_map_invalid')。"""
    if not isinstance(data, dict) or set(data) - {"version", "apps", "note", "robust"}:
        _invalid()
    _as_int(data.get("version"), 1, 1000)
    apps = data.get("apps")
    if not isinstance(apps, dict) or set(apps) != set(SOCIAL_APPS):
        _invalid()
    return {
        "version": data["version"],
        "robust": _robust(data.get("robust") if "robust" in data else None),
        "apps": {app: _validate_app(apps[app]) for app in SOCIAL_APPS},
    }


def bundled_ui_map() -> Dict[str, Any]:
    return validate_ui_map(json.loads(_BUNDLED.read_text(encoding="utf-8")))


def default_ui_map_path(state_dir: Optional[Path] = None) -> Path:
    """Map beside this module, else ``phone_ui_map.json`` in the fleet state dir.

    An explicit agent.json path is not resolved here. Callers that have one use it
    even when the file is absent. A present bundled file wins over the state dir.
    """
    if _BUNDLED.is_file():
        return _BUNDLED
    if state_dir is not None:
        dropped = Path(state_dir) / "phone_ui_map.json"
        if dropped.is_file():
            return dropped
    return _BUNDLED


def _px(pm: int, size: int) -> int:
    v = pm * size // 1000
    if v < 0:
        v = 0
    if v >= size:
        v = size - 1
    if v > 9999:
        v = 9999
    return v


def _point(name: str, anchors: Dict[str, Tuple[int, int]], overrides: Dict[str, Dict[str, int]],
           width: int, height: int) -> Tuple[int, int]:
    if name in overrides:
        return overrides[name]["x"], overrides[name]["y"]
    if name not in anchors:
        raise PhoneOpError("unknown_anchor")
    ax, ay = anchors[name]
    return _px(ax, width), _px(ay, height)


def _repeat(step: Dict[str, Any], payload: Dict[str, Any]) -> int:
    if "repeat" not in step:
        return 1
    rep = step["repeat"]
    if rep == "scrolls":
        n = payload.get("scrolls", 0)
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 8:
            raise PhoneOpError("bad_scrolls")
        return n
    return int(rep)


def _label(step: Dict[str, Any]) -> str:
    op = step.get("op")
    if op == "tap":
        return str(step.get("anchor") or "")
    if op == "swipe":
        return str(step.get("from") or "")
    if op == "key":
        return str(step.get("key") or "")
    return str(step.get("field") or op or "")


def _blank_check(step: Dict[str, Any]) -> Dict[str, Any]:
    return {"change": False, "probe": "", "retry": None, "preflight": False, "label": _label(step),
            "require": (), "forbid": (), "thread": False, "thread_require": (), "thread_forbid": ()}


def _pick_slots(n: int, k: int) -> set:
    """把 k 次点赞均匀摊到 n 次滑动 / 观看上。同一组 n、k 永远同一组下标。"""
    if k <= 0 or n <= 0:
        return set()
    if k >= n:
        return set(range(n))
    out = set()
    for i in range(k):
        idx = int((i + 1) * n / (k + 1))
        if idx >= n:
            idx = n - 1
        while idx in out and idx + 1 < n:
            idx += 1
        out.add(idx)
    return out


def _likes_n(payload: Dict[str, Any]) -> int:
    if "likes" not in payload:
        return 0
    n = payload.get("likes")
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 8:
        raise PhoneOpError("bad_likes")
    return n


def _session_count(rep: Any, payload: Dict[str, Any]) -> int:
    if rep == "scrolls":
        n = payload.get("scrolls", 0)
        code = "bad_scrolls"
    elif rep == "watches":
        n = payload.get("watches", 0)
        code = "bad_watches"
    else:
        return int(rep)
    if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 8:
        raise PhoneOpError(code)
    return n


def _expand(steps: List[Dict[str, Any]], payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把 repeat 展开成具体步骤。点赞落在固定下标上；隔几条才停留的步骤按 every 取。"""
    out: List[Dict[str, Any]] = []
    for step in steps:
        if step.get("op") == "repeat":
            n = _session_count(step["count"], payload)
            slots = _pick_slots(n, _likes_n(payload))
            for i in range(n):
                for inner in step["steps"]:
                    when = inner.get("when")
                    if when == "like" and i not in slots:
                        continue
                    if when == "dwell" and (i + 1) % int(inner["every"]) != 0:
                        continue
                    out.append(inner)
            continue
        n = _repeat(step, payload) if step.get("op") == "swipe" else 1
        for _ in range(n):
            out.append(step)
    return out


def _check_for(step: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    exp = step.get("expect")
    if not exp:
        return None
    check = _blank_check(step)
    if exp == "change":
        check["change"] = True
        return check
    if isinstance(exp, dict):
        check["change"] = bool(exp.get("change"))
        check["probe"] = str(exp.get("probe") or "")
        check["retry"] = exp.get("retry") if "retry" in exp else None
        return check
    return None


def _compile(app: str, flow_name: str, payload: Dict[str, Any], width: int, height: int,
             ui_map: Dict[str, Any]) -> Tuple[List[Step], List[Optional[Dict[str, Any]]]]:
    if app not in SOCIAL_APPS or not isinstance(width, int) or isinstance(width, bool) or width < 1:
        raise PhoneOpError("ui_map_invalid")
    if not isinstance(height, int) or isinstance(height, bool) or height < 1:
        raise PhoneOpError("ui_map_invalid")
    try:
        spec = ui_map["apps"][app]
        anchors: Dict[str, Tuple[int, int]] = spec["anchors"]
        steps = spec["flows"][flow_name]
    except (KeyError, TypeError):
        raise PhoneOpError("ui_map_invalid")
    overrides = payload.get("anchors") if isinstance(payload.get("anchors"), dict) else {}
    for name in overrides:
        if name not in anchors:
            raise PhoneOpError("unknown_anchor")
    preflight = spec.get("preflight") if isinstance(spec.get("preflight"), dict) else None
    pf_anchor = str(preflight.get("after_anchor") or "") if preflight else ""
    thread = preflight.get("thread") if preflight and isinstance(preflight.get("thread"), dict) else None
    th_anchor = str(thread.get("after_anchor") or "") if thread else ""
    pf_used = False
    th_used = False
    out: List[Step] = []
    checks: List[Optional[Dict[str, Any]]] = []
    for step in _expand(steps, payload):
        op = step["op"]
        if op == "key":
            item: Step = (TASK_PHONE_KEY, {"key": step["key"]})
        elif op == "text":
            field = step["field"]
            text = payload.get(field)
            if not isinstance(text, str) or not text:
                raise PhoneOpError("ui_map_invalid")
            item = (TASK_PHONE_TEXT, {"text": text})
        elif op == "tap":
            x, y = _point(step["anchor"], anchors, overrides, width, height)
            if step.get("slot_field"):
                slot = payload.get(step["slot_field"], 0)
                if isinstance(slot, bool) or not isinstance(slot, int):
                    raise PhoneOpError("bad_media_slot")
                sx, sy = anchors[step["slot_stride"]]
                x = min(width - 1, max(0, x + slot * (sx * width // 1000)))
                y = min(height - 1, max(0, y + slot * (sy * height // 1000)))
                x = min(x, 9999)
                y = min(y, 9999)
            item = (TASK_PHONE_TAP, {"x": x, "y": y})
        elif op == "dwell":
            item = (_DWELL_KIND, {"lo_ms": int(step["lo_ms"]), "hi_ms": int(step["hi_ms"])})
        else:
            x1, y1 = _point(step["from"], anchors, overrides, width, height)
            x2, y2 = _point(step["to"], anchors, overrides, width, height)
            dur = step.get("duration_ms", 350)
            item = (TASK_PHONE_SWIPE, {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": dur})
        check = _check_for(step)
        if op == "tap" and pf_anchor and not pf_used and step.get("anchor") == pf_anchor:
            if check is None:
                check = _blank_check(step)
            check["preflight"] = True
            check["label"] = str(step.get("anchor") or "")
            check["require"] = tuple(preflight.get("require") or ())
            check["forbid"] = tuple(preflight.get("forbid") or ())
            pf_used = True
        if op == "tap" and th_anchor and not th_used and step.get("anchor") == th_anchor:
            if check is None:
                check = _blank_check(step)
            check["thread"] = True
            check["label"] = str(step.get("anchor") or "")
            check["thread_require"] = tuple(thread.get("require") or ())
            check["thread_forbid"] = tuple(thread.get("forbid") or ())
            th_used = True
        out.append(item)
        checks.append(None if check is None else dict(check))
    if not out:
        raise PhoneOpError("ui_map_invalid")
    return out, checks


def compile_flow(app: str, flow_name: str, payload: Dict[str, Any], width: int, height: int,
                 ui_map: Dict[str, Any]) -> List[Step]:
    """把一个应用的一条流程编成低层 (kind, payload) 列表。不含开头那张用来量屏幕的截图。"""
    steps, _checks = _compile(app, flow_name, payload, width, height, ui_map)
    return steps


def compile_flow_checked(app: str, flow_name: str, payload: Dict[str, Any], width: int, height: int,
                         ui_map: Dict[str, Any]) -> Tuple[List[Step], List[Optional[Dict[str, Any]]]]:
    """和 compile_flow 同序。checks[i] 为 None 表示这一步做完不截图复查。"""
    return _compile(app, flow_name, payload, width, height, ui_map)


def _plan_step(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if kind == TASK_PHONE_TAP:
        return {"op": "tap", "x": int(payload["x"]), "y": int(payload["y"])}
    if kind == TASK_PHONE_SWIPE:
        item = {"op": "swipe"}
        for key in ("x1", "y1", "x2", "y2", "duration_ms"):
            item[key] = int(payload[key])
        return item
    if kind == TASK_PHONE_KEY:
        return {"op": "key", "key": str(payload["key"])}
    if kind == TASK_PHONE_TEXT:
        text = str(payload["text"])
        return {"op": "text", "text": text, "chars": len(text)}
    if kind == _DWELL_KIND:
        return {"op": "dwell", "lo_ms": int(payload["lo_ms"]), "hi_ms": int(payload["hi_ms"])}
    raise PhoneOpError("ui_map_invalid")


def _status(err: PhoneOpError) -> str:
    return STATUS_FAILED if err.failed else STATUS_REJECTED


def _result(serial: str, app: str, flow: str, *, completed: Optional[int] = None,
            failed_step: Optional[int] = None, width: Optional[int] = None, height: Optional[int] = None,
            err: Optional[PhoneOpError] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"serial": serial, "app": app, "flow": flow}
    if isinstance(completed, int) and completed >= 0:
        out["completed_steps"] = completed
    if isinstance(failed_step, int) and failed_step >= 0:
        out["failed_step"] = failed_step
    if isinstance(width, int) and width > 0:
        out["device_width"] = width
    if isinstance(height, int) and height > 0:
        out["device_height"] = height
    stderr = getattr(err, "stderr", "") if err is not None else ""
    if stderr:
        out["stderr"] = stderr
    return out


class PhoneFlows:
    """节点上的社交动作执行器。``execute`` 永不抛。"""

    def __init__(self, *, enabled: bool = False, ui_map_path: str = "", ops: Any = None,
                 verify: Any = _UNSET, jitter_ms: Any = _UNSET, rng: Any = None,
                 state_dir: Optional[Path] = None, ocr: Any = _UNSET) -> None:
        """``state_dir`` is the fleet state directory. NodeAgent always passes it.

        ``ocr`` is an optional text reader ``(screencap bytes) -> boxes``. The
        default tries the optional rapidocr package. ``False`` skips text.
        Icon templates do not use this argument.
        """
        self.enabled = False
        self.ui_map_path = ""
        self.state_dir: Optional[Path] = None
        self.ops = ops
        self._cache: Optional[tuple] = None
        self._verify: Any = None
        self._jitter: Any = None
        self._rng = random.random
        self._ocr = None
        self.configure(enabled=enabled, ui_map_path=ui_map_path, ops=ops, verify=verify,
                       jitter_ms=jitter_ms, rng=rng, state_dir=state_dir)
        if ocr is not _UNSET:
            self._ocr = ocr

    @classmethod
    def from_agent_settings(cls, settings: Dict[str, Any], ops: Any = None) -> "PhoneFlows":
        """Construct the way ``NodeAgent`` does at process start.

        ``settings`` is ``_phone_flows_settings``. ``state_dir`` is one of those
        keys and is passed by name into ``__init__``.
        """
        if not isinstance(settings, dict):
            settings = {}
        return cls(
            enabled=settings.get("enabled") is True,
            ui_map_path=settings.get("ui_map_path") or "",
            ops=ops,
            verify=settings.get("verify", _UNSET),
            jitter_ms=settings.get("jitter_ms", _UNSET),
            state_dir=settings.get("state_dir"),
        )

    def configure(self, *, enabled: bool = False, ui_map_path: str = "", ops: Any = None,
                  verify: Any = _UNSET, jitter_ms: Any = _UNSET, rng: Any = None,
                  state_dir: Any = _UNSET) -> None:
        path = ui_map_path.strip() if isinstance(ui_map_path, str) else ("invalid" if ui_map_path else "")
        next_state = self.state_dir if state_dir is _UNSET else state_dir
        if path != self.ui_map_path or next_state != self.state_dir:
            self._cache = None
        self.ui_map_path = path
        self.state_dir = next_state
        self.enabled = bool(enabled)
        if ops is not None:
            self.ops = ops
        if verify is not _UNSET:
            self._verify = verify
        if jitter_ms is not _UNSET:
            self._jitter = jitter_ms
        if rng is not None:
            self._rng = rng

    def load_map(self) -> Dict[str, Any]:
        path = Path(self.ui_map_path) if self.ui_map_path else default_ui_map_path(self.state_dir)
        if not path.is_file():
            raise PhoneOpError("ui_map_missing")
        try:
            size = path.stat().st_size
        except OSError:
            raise PhoneOpError("ui_map_missing")
        if size > _MAX_MAP_BYTES or size <= 0:
            raise PhoneOpError("ui_map_invalid")
        try:
            token = (str(path.resolve()), path.stat().st_mtime_ns, size)
        except OSError:
            raise PhoneOpError("ui_map_missing")
        if self._cache is not None and self._cache[0] == token:
            return self._cache[1]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise PhoneOpError("ui_map_invalid")
        parsed = validate_ui_map(data)
        self._cache = (token, parsed)
        return parsed

    def _probe_hit(self, raw: bytes, probe: Any, anchors: Dict[str, Tuple[int, int]],
                   overrides: Dict[str, Any], width: int, height: int) -> bool:
        if not isinstance(probe, dict):
            return False
        x, y = _point(str(probe.get("at") or ""), anchors, overrides, width, height)
        return probe_matches(raw, x, y, probe.get("rgb") or (0, 0, 0), int(probe.get("tol") or 0))

    def _confirm(self, ops: Any, run: Any, serial: str, check: Dict[str, Any], spec: Dict[str, Any],
                 overrides: Dict[str, Any]) -> None:
        before = frame_token(ops.last_raw(serial))
        shot = run(TASK_PHONE_SCREENSHOT, {})
        raw = ops.last_raw(serial)
        dw, dh = shot.get("device_width"), shot.get("device_height")
        if isinstance(dw, bool) or not isinstance(dw, int) or isinstance(dh, bool) or not isinstance(dh, int):
            raise PhoneOpError("screencap_bad_frame", failed=True)
        if check.get("change") and frame_token(raw) == before:
            err = PhoneOpError("screen_not_reached", failed=True)
            err.stderr = f"anchor:{check.get('label') or ''}"[:160]  # type: ignore[attr-defined]
            raise err
        anchors = spec.get("anchors") or {}
        probes = spec.get("probes") or {}
        if check.get("thread"):
            for name in check.get("thread_forbid") or ():
                if self._probe_hit(raw, probes.get(name), anchors, overrides, dw, dh):
                    err = PhoneOpError("thread_not_open", failed=True)
                    err.stderr = f"probe:{name}"[:160]  # type: ignore[attr-defined]
                    raise err
            for name in check.get("thread_require") or ():
                if not self._probe_hit(raw, probes.get(name), anchors, overrides, dw, dh):
                    err = PhoneOpError("thread_not_open", failed=True)
                    err.stderr = f"probe:{name}"[:160]  # type: ignore[attr-defined]
                    raise err
        if check.get("preflight"):
            for name in check.get("forbid") or ():
                if self._probe_hit(raw, probes.get(name), anchors, overrides, dw, dh):
                    err = PhoneOpError("not_logged_in", failed=True)
                    err.stderr = f"probe:{name}"[:160]  # type: ignore[attr-defined]
                    raise err
            for name in check.get("require") or ():
                if not self._probe_hit(raw, probes.get(name), anchors, overrides, dw, dh):
                    err = PhoneOpError("app_not_ready", failed=True)
                    err.stderr = f"probe:{name}"[:160]  # type: ignore[attr-defined]
                    raise err
        probe_name = str(check.get("probe") or "")
        if probe_name and not self._probe_hit(raw, probes.get(probe_name), anchors, overrides, dw, dh):
            err = PhoneOpError("anchor_mismatch", failed=True)
            err.stderr = f"probe:{probe_name}"[:160]  # type: ignore[attr-defined]
            raise err

    def _text_boxes(self, raw: bytes) -> List[Dict[str, Any]]:
        from .like_locate import normalize_ocr_boxes, read_text_boxes

        reader = self._ocr
        if reader is False:
            return []
        if callable(reader):
            try:
                return normalize_ocr_boxes(reader(raw))
            except Exception:
                return []
        return read_text_boxes(raw)

    def _vision_ready(self) -> bool:
        from .like_locate import vision_stack_ready

        return vision_stack_ready(ocr=self._ocr)

    def _execute_facebook_like(self, serial: str, p: Dict[str, Any], ui: Dict[str, Any],
                               ops: Any) -> Tuple[str, Dict[str, Any], str]:
        """Launch Facebook, scroll the feed, tap Like only when the locator is sure.

        Login uses the existing feed_tab color probe. A miss is not retried.
        ``like_probe`` returns the row and does not tap it.
        """
        from .like_locate import DEFAULT_LIKE_SWIPES, like_state_changed, locate_like_row

        if not self._vision_ready():
            return STATUS_REJECTED, _result(serial, "facebook", "like"), "ocr_unavailable"
        spec = ui["apps"]["facebook"]
        anchors = spec["anchors"]
        probes = spec.get("probes") or {}
        overrides = p.get("anchors") or {}
        budget = DEFAULT_LIKE_SWIPES
        if isinstance(p.get("like_swipes"), int) and not isinstance(p.get("like_swipes"), bool):
            budget = int(p["like_swipes"])
        probe = p.get("like_probe") is True
        swipe = next((step for step in spec["flows"]["like"] if step.get("op") == "swipe"), None)
        dur = int(swipe.get("duration_ms", 350)) if isinstance(swipe, dict) else 350
        src_name = str(swipe.get("from") if isinstance(swipe, dict) else "") or "feed_swipe_from"
        dst_name = str(swipe.get("to") if isinstance(swipe, dict) else "") or "feed_swipe_to"
        t0 = ops._clock()
        swipes = 0
        width: Optional[int] = None
        height: Optional[int] = None
        try:
            with ops.session(serial) as run:
                shot = run(TASK_PHONE_SCREENSHOT, {})
                width, height = shot.get("device_width"), shot.get("device_height")
                if (isinstance(width, bool) or not isinstance(width, int)
                        or isinstance(height, bool) or not isinstance(height, int)):
                    return STATUS_FAILED, _result(serial, "facebook", "like", completed=1), "screencap_bad_frame"
                run(TASK_PHONE_KEY, {"key": "home"})
                ax, ay = _point("app_icon", anchors, overrides, width, height)
                run(TASK_PHONE_TAP, {"x": ax, "y": ay})
                run(TASK_PHONE_SCREENSHOT, {})
                raw = ops.last_raw(serial)
                if self._probe_hit(raw, probes.get("login_wall"), anchors, overrides, width, height):
                    err = PhoneOpError("not_logged_in", failed=True)
                    err.stderr = "probe:login_wall"  # type: ignore[attr-defined]
                    raise err
                if not self._probe_hit(raw, probes.get("logged_in"), anchors, overrides, width, height):
                    err = PhoneOpError("app_not_ready", failed=True)
                    err.stderr = "probe:logged_in"  # type: ignore[attr-defined]
                    raise err
                fx, fy = _point("feed_tab", anchors, overrides, width, height)
                run(TASK_PHONE_TAP, {"x": fx, "y": fy})
                found = None
                saw_post = False
                boxes: List[Dict[str, Any]] = []
                for attempt in range(budget + 1):
                    run(TASK_PHONE_SCREENSHOT, {})
                    raw = ops.last_raw(serial)
                    boxes = self._text_boxes(raw)
                    loc = locate_like_row(raw, boxes)
                    if loc.get("post"):
                        saw_post = True
                    if loc.get("target"):
                        found = loc["target"]
                        break
                    if attempt < budget:
                        x1, y1 = _point(src_name, anchors, overrides, width, height)
                        x2, y2 = _point(dst_name, anchors, overrides, width, height)
                        run(TASK_PHONE_SWIPE, {
                            "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": dur,
                        })
                        swipes += 1
                base: Dict[str, Any] = {
                    "serial": serial, "app": "facebook", "flow": "like", "swipes": swipes,
                    "device_width": width, "device_height": height,
                }
                if probe:
                    base["like_probe"] = True
                if found is None:
                    return STATUS_FAILED, base, "empty_feed" if not saw_post else "like_row_not_found"
                base["like_x"] = int(found["x"])
                base["like_y"] = int(found["y"])
                base["like_signals"] = str(found.get("signals") or "")
                label = str(found.get("label") or "")
                if label:
                    base["like_label"] = label[:32]
                if probe:
                    base["elapsed_ms"] = int(max(0.0, ops._clock() - t0) * 1000)
                    return STATUS_DONE, base, "like_probe"
                tx, ty = _px(int(found["x"]), width), _px(int(found["y"]), height)
                before = raw
                before_boxes = boxes
                run(TASK_PHONE_TAP, {"x": tx, "y": ty})
                run(TASK_PHONE_SCREENSHOT, {})
                after = ops.last_raw(serial)
                if not like_state_changed(before, after, tx, ty, before_boxes, self._text_boxes(after)):
                    return STATUS_FAILED, base, "not_verified"
                base["elapsed_ms"] = int(max(0.0, ops._clock() - t0) * 1000)
                return STATUS_DONE, base, "ok"
        except PhoneOpError as e:
            res = _result(serial, "facebook", "like", width=width, height=height, err=e)
            res["swipes"] = swipes
            return _status(e), res, e.code
        except subprocess.TimeoutExpired:
            return STATUS_FAILED, _result(serial, "facebook", "like", width=width, height=height), "adb_timeout"
        except Exception as e:  # noqa: BLE001 - never raise into the agent loop
            return STATUS_FAILED, _result(
                serial, "facebook", "like", width=width, height=height,
            ), f"error:{type(e).__name__}"

    def execute(self, kind: str, payload: Any, target: Any, ops: Any = None) -> Tuple[str, Dict[str, Any], str]:
        ops = self.ops if ops is None else ops
        if kind not in PHONE_FLOW_KINDS and kind not in PHONE_SESSION_KINDS:
            return STATUS_REJECTED, {}, f"unknown_kind:{kind}"
        if not self.enabled:
            return STATUS_REJECTED, {}, "phone_flows_disabled"
        if ops is None or not getattr(ops, "enabled", False):
            return STATUS_REJECTED, {}, "phone_ops_disabled"
        try:
            serial = check_target((target or {}).get("serial") if isinstance(target, dict) else "")
            if is_excluded(serial, getattr(ops, "excludes", ())):
                raise PhoneOpError("excluded_phone")
            p = validate_flow_payload(kind, payload)
            ui = self.load_map()
        except PhoneOpError as e:
            return STATUS_REJECTED, {}, e.code
        app = p["app"]
        flow = FLOW_BY_KIND[kind]
        map_flow = "post_media" if kind == TASK_PHONE_POST and p.get("media") else flow
        overrides = p.get("anchors") or {}
        try:
            known = ui["apps"][app]["anchors"]
            for name in overrides:
                if name not in known:
                    raise PhoneOpError("unknown_anchor")
        except PhoneOpError as e:
            return STATUS_REJECTED, {}, e.code
        except (KeyError, TypeError):
            return STATUS_REJECTED, {}, "ui_map_invalid"
        if p.get("dry_run") is True:
            try:
                steps = compile_flow(app, map_flow, p, _DRY_WIDTH, _DRY_HEIGHT, ui)
            except PhoneOpError as e:
                return _status(e), _result(serial, app, flow, err=e), e.code
            if len(steps) > _PLAN_MAX_STEPS:
                return STATUS_REJECTED, _result(serial, app, flow), "plan_too_long"
            plan = [_plan_step(sk, sp) for sk, sp in steps]
            body: Dict[str, Any] = {
                "serial": serial, "app": app, "flow": flow, "dry_run": True, "space": "permille",
                "steps": len(plan), "plan": plan,
            }
            if app == "facebook" and flow == "like":
                body["like_button_deprecated"] = True
            return STATUS_DONE, body, "dry_run"
        if app == "facebook" and flow == "like":
            return self._execute_facebook_like(serial, p, ui, ops)
        prog: Dict[str, Any] = {"done": 0, "step": None, "w": None, "h": None}
        t0 = ops._clock()
        chars = 0
        try:
            with ops.session(serial) as run:
                try:
                    shot = run(TASK_PHONE_SCREENSHOT, {})
                except PhoneOpError as e:
                    return _status(e), _result(serial, app, flow, completed=0, err=e), e.code
                w, h = shot.get("device_width"), shot.get("device_height")
                if isinstance(w, bool) or not isinstance(w, int) or isinstance(h, bool) or not isinstance(h, int):
                    return STATUS_FAILED, _result(serial, app, flow, completed=1), "screencap_bad_frame"
                prog.update(done=1, w=w, h=h)
                verify_on, jitter, retries = resolve_policy(ui, self._verify, self._jitter)
                try:
                    if verify_on or jitter != (0, 0):
                        steps, checks = compile_flow_checked(app, map_flow, p, w, h, ui)
                    else:
                        steps = compile_flow(app, map_flow, p, w, h, ui)
                        checks = [None] * len(steps)
                except PhoneOpError as e:
                    return _status(e), _result(serial, app, flow, completed=1, width=w, height=h, err=e), e.code
                spec = ui["apps"][app]
                for i, (sk, sp) in enumerate(steps):
                    prog["step"] = i
                    if jitter != (0, 0):
                        gap = jitter_seconds(jitter, self._rng)
                        if gap > 0:
                            ops.add_human_gap(serial, gap)
                    check = checks[i] if verify_on else None
                    tries = 1
                    if check is not None:
                        extra = retries if check.get("retry") is None else int(check["retry"])
                        if extra < 0:
                            extra = 0
                        if extra > MAX_RETRIES:
                            extra = MAX_RETRIES
                        tries = 1 + extra
                    res = None
                    for attempt in range(tries):
                        try:
                            if sk == _DWELL_KIND:
                                held = dwell_seconds((int(sp["lo_ms"]), int(sp["hi_ms"])), self._rng)
                                if held > 0:
                                    ops.add_dwell(serial, held)
                                res = {}
                            else:
                                res = run(sk, sp)
                        except PhoneOpError as e:
                            return _status(e), _result(
                                serial, app, flow, completed=prog["done"], failed_step=i, width=w, height=h, err=e,
                            ), e.code
                        if check is None:
                            break
                        try:
                            self._confirm(ops, run, serial, check, spec, overrides)
                            break
                        except PhoneOpError as e:
                            if e.code not in _CHECK_FAILS or attempt + 1 >= tries:
                                return _status(e), _result(
                                    serial, app, flow, completed=prog["done"], failed_step=i,
                                    width=w, height=h, err=e,
                                ), e.code
                    prog["done"] = int(prog["done"]) + 1
                    if sk == TASK_PHONE_TEXT and isinstance(res, dict):
                        chars += int(res.get("chars") or 0)
            elapsed = int(max(0.0, ops._clock() - t0) * 1000)
            out: Dict[str, Any] = {
                "serial": serial, "app": app, "flow": flow, "steps": int(prog["done"]), "elapsed_ms": elapsed,
                "device_width": w, "device_height": h,
            }
            if chars:
                out["chars"] = chars
            if flow in SESSION_FLOW_NAMES:
                for key in ("scrolls", "watches", "likes"):
                    if isinstance(p.get(key), int) and not isinstance(p.get(key), bool):
                        out[key] = p[key]
                out["dwells"] = sum(1 for sk, _sp in steps if sk == _DWELL_KIND)
            return STATUS_DONE, out, "ok"
        except PhoneOpError as e:
            return _status(e), _result(
                serial, app, flow, completed=int(prog["done"]), failed_step=prog["step"],
                width=prog["w"], height=prog["h"], err=e,
            ), e.code
        except subprocess.TimeoutExpired:
            return STATUS_FAILED, _result(
                serial, app, flow, completed=int(prog["done"]), failed_step=prog["step"],
                width=prog["w"], height=prog["h"],
            ), "adb_timeout"
        except Exception as e:  # noqa: BLE001 - never raise into the agent loop
            return STATUS_FAILED, _result(
                serial, app, flow, completed=int(prog["done"]), failed_step=prog["step"],
                width=prog["w"], height=prog["h"],
            ), f"error:{type(e).__name__}"


__all__ = [
    "PhoneFlows", "bundled_ui_map", "compile_flow", "compile_flow_checked", "default_ui_map_path",
    "validate_ui_map",
]
