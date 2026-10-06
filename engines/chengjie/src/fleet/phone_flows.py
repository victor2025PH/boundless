"""把发帖 / 点赞 / 评论 / 关注编译成已有的截图、点击、滑动、按键、输入。

坐标在 ``phone_ui_map.json``（或 agent.json ``phone_ui_map`` 指向的文件）里，按应用分开，
单位是屏幕千分比。每次动作先截一张图量屏幕，再把千分比换成像素。不新增 adb 动词：
打开应用是 HOME 再点该应用的 ``app_icon`` 锚点；带图是点相册里已经在手机上的那一格。

``phone_flows_enabled`` 缺省关。关着时不声明 phone_flows_v1，执行直接拒绝，不碰 adb。
回传只留应用名、动作、步数和字数，不带回帖子或评论原文。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .phone_flow_rules import FLOW_BY_KIND, validate_flow_payload
from .phone_rules import KEYCODES, SWIPE_MS_MAX, SWIPE_MS_MIN, PhoneOpError, check_target
from .phones import is_excluded
from .protocol import (
    PHONE_FLOW_KINDS, SOCIAL_APPS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED, TASK_PHONE_KEY,
    TASK_PHONE_POST, TASK_PHONE_SCREENSHOT, TASK_PHONE_SWIPE, TASK_PHONE_TAP, TASK_PHONE_TEXT,
)

_BUNDLED = Path(__file__).with_name("phone_ui_map.json")
_MAX_MAP_BYTES = 256 * 1024
_FLOW_KEYS = ("post", "post_media", "like", "comment", "follow")
_STEP_KEYS = {
    "tap": {"op", "anchor", "slot_field", "slot_stride"},
    "swipe": {"op", "from", "to", "duration_ms", "repeat"},
    "key": {"op", "key"},
    "text": {"op", "field"},
}
_NAME_RE_SRC = r"^[a-z][a-z0-9_]{0,31}$"

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


def _validate_app(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"anchors", "flows", "note"}:
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
    flows: Dict[str, List[Dict[str, Any]]] = {}
    for flow in _FLOW_KEYS:
        steps_raw = flows_raw.get(flow)
        if not isinstance(steps_raw, list) or not steps_raw or len(steps_raw) > 32:
            _invalid()
        steps: List[Dict[str, Any]] = []
        for step in steps_raw:
            if not isinstance(step, dict):
                _invalid()
            op = step.get("op")
            if op not in _STEP_KEYS or set(step) - _STEP_KEYS[op]:
                _invalid()
            clean: Dict[str, Any] = {"op": op}
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
            else:
                field = step.get("field")
                if field not in ("text", "handle"):
                    _invalid()
                clean["field"] = field
            steps.append(clean)
        flows[flow] = steps
    return {"anchors": anchors, "flows": flows}


def validate_ui_map(data: Any) -> Dict[str, Any]:
    """坐标文件 → 规范化结构。形状不对 → PhoneOpError('ui_map_invalid')。"""
    if not isinstance(data, dict) or set(data) - {"version", "apps", "note"}:
        _invalid()
    _as_int(data.get("version"), 1, 1000)
    apps = data.get("apps")
    if not isinstance(apps, dict) or set(apps) != set(SOCIAL_APPS):
        _invalid()
    return {"version": data["version"], "apps": {app: _validate_app(apps[app]) for app in SOCIAL_APPS}}


def bundled_ui_map() -> Dict[str, Any]:
    return validate_ui_map(json.loads(_BUNDLED.read_text(encoding="utf-8")))


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


def compile_flow(app: str, flow_name: str, payload: Dict[str, Any], width: int, height: int,
                 ui_map: Dict[str, Any]) -> List[Step]:
    """把一个应用的一条流程编成低层 (kind, payload) 列表。不含开头那张用来量屏幕的截图。"""
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
    out: List[Step] = []
    for step in steps:
        op = step["op"]
        n = _repeat(step, payload) if op == "swipe" else 1
        if n <= 0:
            continue
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
        else:
            x1, y1 = _point(step["from"], anchors, overrides, width, height)
            x2, y2 = _point(step["to"], anchors, overrides, width, height)
            dur = step.get("duration_ms", 350)
            item = (TASK_PHONE_SWIPE, {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration_ms": dur})
        for _ in range(n):
            out.append(item)
    if not out:
        raise PhoneOpError("ui_map_invalid")
    return out


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

    def __init__(self, *, enabled: bool = False, ui_map_path: str = "", ops: Any = None) -> None:
        self.enabled = False
        self.ui_map_path = ""
        self.ops = ops
        self._cache: Optional[tuple] = None
        self.configure(enabled=enabled, ui_map_path=ui_map_path, ops=ops)

    def configure(self, *, enabled: bool = False, ui_map_path: str = "", ops: Any = None) -> None:
        path = ui_map_path.strip() if isinstance(ui_map_path, str) else ("invalid" if ui_map_path else "")
        if path != self.ui_map_path:
            self._cache = None
        self.ui_map_path = path
        self.enabled = bool(enabled)
        if ops is not None:
            self.ops = ops

    def load_map(self) -> Dict[str, Any]:
        path = Path(self.ui_map_path) if self.ui_map_path else _BUNDLED
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

    def execute(self, kind: str, payload: Any, target: Any, ops: Any = None) -> Tuple[str, Dict[str, Any], str]:
        ops = self.ops if ops is None else ops
        if kind not in PHONE_FLOW_KINDS:
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
                try:
                    steps = compile_flow(app, map_flow, p, w, h, ui)
                except PhoneOpError as e:
                    return _status(e), _result(serial, app, flow, completed=1, width=w, height=h, err=e), e.code
                for i, (sk, sp) in enumerate(steps):
                    prog["step"] = i
                    try:
                        res = run(sk, sp)
                    except PhoneOpError as e:
                        return _status(e), _result(
                            serial, app, flow, completed=prog["done"], failed_step=i, width=w, height=h, err=e,
                        ), e.code
                    prog["done"] = int(prog["done"]) + 1
                    if sk == TASK_PHONE_TEXT:
                        chars += int(res.get("chars") or 0)
            elapsed = int(max(0.0, ops._clock() - t0) * 1000)
            out: Dict[str, Any] = {
                "serial": serial, "app": app, "flow": flow, "steps": int(prog["done"]), "elapsed_ms": elapsed,
                "device_width": w, "device_height": h,
            }
            if chars:
                out["chars"] = chars
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
    "PhoneFlows", "bundled_ui_map", "compile_flow", "validate_ui_map",
]
