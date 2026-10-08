"""把发帖 / 点赞 / 评论 / 关注，以及养号 / 私信 / 看短视频，编译成已有的截图、点击、滑动、按键、输入。

坐标在 ``phone_ui_map.json``（或 agent.json ``phone_ui_map`` 指向的文件）里，按应用分开，
单位是屏幕千分比。agent.json 里的路径优先；路径为空时用本模块旁边的那份，那份不在时再用
fleet 状态目录里的 ``phone_ui_map.json``。每次动作先截一张图量屏幕，再把千分比换成像素。
Facebook 点赞按包名 ``am start`` 打开。启动前先唤醒并划开锁屏。``am start`` 之后轮询前台约 9 秒；
前台已经是 Facebook 时不回桌面。轮询结束前台仍是别的应用时，再按 Home 点 ``app_icon``（0.3.25
在这些 MIUI 上能进信息流的办法），两种都进不去才回执失败。解析不到包名回执
``fb_not_installed_or_store_redirect``，不再把 ``am start`` 的失败报成 ``adb_exit_1``。
其它应用仍是 HOME 再点 ``app_icon``。带图是点相册里已经在手机上的那一格。
停留（dwell）只在本机睡一会儿，不发 adb。

``phone_flows_enabled`` 缺省关。关着时不声明 phone_flows_v1 / phone_flows_v2，执行直接拒绝，不碰 adb。
回传只留应用名、动作、步数、字数和次数，不带回帖子、评论、私信原文或账号名。

dry_run (or predict_only) JSON true compiles the UI map at 1000x1000 permille and
returns the planned taps/text. It does not open an adb session, take a screenshot,
or send input. A missing flag is real execution, and only when flows are enabled.

Facebook ``post_media`` does not blind-tap ``media_next``. That control sits on
the same corner as Publish on some versions, so a stored tap can send the post
before the caption. The compiled plan drops ``media_next`` and any other pre-caption
tap inside the separation of ``post_submit``, and it keeps the caption before
Publish. A live run reads the window once and taps Next only when the label is
Next and not Post, Share, or Publish. No signal means skip, then caption, then
``post_submit``. ``verify_publish`` JSON true (post only) reads the window after
submit and fails with ``post_not_confirmed`` unless a success phrase is showing
and the publish control is gone. It does not run on dry_run. ``robust.verify``
still only checks that the frame changed and the login color; it does not prove
the post went out. Caption text stays the ASCII ``phone_text`` subset, at most
200 characters. Chinese is ``text_non_ascii_unsupported``.

Facebook ``phone_like`` does not tap the fixed ``like_button``. It screenshots,
finds the Like control (text row, thumbs-up template, action-bar slot), and taps
only when two signals agree or the template score is high. The fixed coordinate
stays in the dry_run plan and is marked deprecated. Warmup and watch still use it.
``like_probe`` launches and scrolls and does not tap Like. The result includes
``like_diag``: which signal fired, the template score, the shape score, the
action-bar row bounds, and the action-bar node attributes (no screenshot, no
serial). The hierarchy dump settles autoplay, writes one fixed file, and
retries; that transport is diagnostic and does not change the two-signal
rule. No posts after the swipe budget is ``empty_feed``.

Opening Facebook polls the feed for up to 9 seconds. One check at 0.5 seconds
is not enough: the app is still drawing, and a previous scroll hides the top
tab bar so the blue Home pixel is missing. If that pixel is still missing
after a few seconds, the flow scrolls up to bring the bar back, still inside
the same 9 seconds. Once the feed is up, it scrolls to the top and waits 3
seconds before looking for the action bar.

Counted Facebook like / comment / follow / post pass through ``social_pace``
before any tap. Over the per-account cap, outside local active hours, inside
the minimum gap, or with the kill switch off, the action is rejected and adb
is not opened. ``like_probe`` and ``dry_run`` are not counted. Instagram and
TikTok are unchanged. Warmup and watch are unchanged.

核对和随机间隔写在坐标文件的 ``robust`` 里，默认关。agent.json 的 ``phone_flow_verify`` /
``phone_flow_jitter_ms`` 可以盖过文件：只有 JSON ``true`` 才强制核对；抖动形状不对就仍用文件里的。
"""

from __future__ import annotations

import json
import logging
import random
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .phone_flow_robust import (
    MAX_DWELL_MS, MAX_JITTER_MS, MAX_RETRIES, MIN_DWELL_MS, dwell_seconds, frame_token, jitter_seconds,
    probe_matches, resolve_policy,
)
from .post_publish import assess_publish, distinct_next_point, ocr_vetoes_next, points_collide
from .phone_flow_rules import FLOW_BY_KIND, validate_flow_payload
from .social_pace import PaceLedger
from .phone_rules import KEYCODES, SWIPE_MS_MAX, SWIPE_MS_MIN, PhoneOpError, check_target
from .phones import is_excluded
from .protocol import (
    PHONE_FLOW_KINDS, PHONE_SESSION_KINDS, SESSION_FLOW_NAMES, SOCIAL_APPS, STATUS_DONE, STATUS_FAILED,
    STATUS_REJECTED, TASK_PHONE_KEY, TASK_PHONE_POST, TASK_PHONE_SCREENSHOT, TASK_PHONE_SWIPE, TASK_PHONE_TAP,
    TASK_PHONE_TEXT,
)

logger = logging.getLogger("fleet.phone_flows")

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
# Facebook hides the top tab bar after a scroll, so the blue Home pixel is
# gone and a single 0.5s check reports app_not_ready. Poll, reveal the bar,
# then sit at the top of the feed before the action-bar search.
_FB_FEED_OPEN_BUDGET_SEC = 9.0
_FB_FEED_OPEN_QUIET_SEC = 3.0
_FB_FEED_TOP_SWIPES = 2
_FB_FEED_TOP_SETTLE_SEC = 3.0
# am start on these MIUI phones returns success while the old app is still in
# front. One read at 0.5s is not a decision. Poll, then fall back to Home+icon.
_FB_FOREGROUND_POLL_SEC = 9.0
_FB_FOREGROUND_POLL_STEP = 0.5

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
            item = (TASK_PHONE_TAP, {"x": x, "y": y, "_anchor": str(step.get("anchor") or "")})
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
    if app == "facebook" and flow_name in ("post", "post_media"):
        out, checks = _guard_facebook_caption(out, checks, width, height)
    if not out:
        raise PhoneOpError("ui_map_invalid")
    return out, checks


def _tap_anchor(step: Step) -> str:
    kind, payload = step
    if kind != TASK_PHONE_TAP:
        return ""
    name = payload.get("_anchor")
    return name if isinstance(name, str) else ""


def _strip_anchor(steps: List[Step]) -> List[Step]:
    """Public plans stay ``{x, y}`` taps. The anchor name is only for the executor."""
    out: List[Step] = []
    for kind, payload in steps:
        if kind == TASK_PHONE_TAP and "_anchor" in payload:
            payload = {key: value for key, value in payload.items() if key != "_anchor"}
        out.append((kind, payload))
    return out


def _guard_facebook_caption(steps: List[Step], checks: List[Optional[Dict[str, Any]]],
                            width: int, height: int) -> Tuple[List[Step], List[Optional[Dict[str, Any]]]]:
    """Never emit a blind Next, and never tap Publish before the caption.

    ``media_next`` is dropped even when its stored point is far from Publish:
    on a real Next screen that button occupies the Publish corner, so distance
    alone cannot tell them apart. Any other pre-caption tap inside the
    separation of ``post_submit`` is dropped too. If a map still lists Publish
    before the caption, the caption and the composer tap immediately before it
    move in front of Publish.
    """
    paired = [(step, check) for step, check in zip(steps, checks) if _tap_anchor(step) != "media_next"]
    steps = [step for step, _check in paired]
    checks = [check for _step, check in paired]
    if not steps:
        raise PhoneOpError("ui_map_invalid")
    submit_at = [i for i, step in enumerate(steps) if _tap_anchor(step) == "post_submit"]
    text_at = [i for i, (kind, _payload) in enumerate(steps) if kind == TASK_PHONE_TEXT]
    if not submit_at or not text_at:
        return steps, checks
    submit_i = submit_at[-1]
    text_i = text_at[0]
    sx = int(steps[submit_i][1]["x"])
    sy = int(steps[submit_i][1]["y"])
    kept_s: List[Step] = []
    kept_c: List[Optional[Dict[str, Any]]] = []
    for i, (step, check) in enumerate(zip(steps, checks)):
        if i < text_i and i != submit_i and step[0] == TASK_PHONE_TAP:
            if points_collide(int(step[1]["x"]), int(step[1]["y"]), sx, sy, width, height):
                continue
        kept_s.append(step)
        kept_c.append(check)
    steps, checks = kept_s, kept_c
    if not steps:
        raise PhoneOpError("ui_map_invalid")
    submit_at = [i for i, step in enumerate(steps) if _tap_anchor(step) == "post_submit"]
    text_at = [i for i, (kind, _payload) in enumerate(steps) if kind == TASK_PHONE_TEXT]
    if not submit_at or not text_at:
        return steps, checks
    submit_i = submit_at[-1]
    text_i = text_at[0]
    if text_i < submit_i:
        return steps, checks
    start = text_i
    if text_i > 0 and _tap_anchor(steps[text_i - 1]) == "composer_field":
        start = text_i - 1
    if start <= submit_i:
        return steps, checks
    block_s = steps[start:text_i + 1]
    block_c = checks[start:text_i + 1]
    rest_s = steps[:start] + steps[text_i + 1:]
    rest_c = checks[:start] + checks[text_i + 1:]
    return rest_s[:submit_i] + block_s + rest_s[submit_i:], rest_c[:submit_i] + block_c + rest_c[submit_i:]


def compile_flow(app: str, flow_name: str, payload: Dict[str, Any], width: int, height: int,
                 ui_map: Dict[str, Any]) -> List[Step]:
    """把一个应用的一条流程编成低层 (kind, payload) 列表。不含开头那张用来量屏幕的截图。"""
    steps, _checks = _compile(app, flow_name, payload, width, height, ui_map)
    return _strip_anchor(steps)


def compile_flow_checked(app: str, flow_name: str, payload: Dict[str, Any], width: int, height: int,
                         ui_map: Dict[str, Any]) -> Tuple[List[Step], List[Optional[Dict[str, Any]]]]:
    """和 compile_flow 同序。checks[i] 为 None 表示这一步做完不截图复查。"""
    steps, checks = _compile(app, flow_name, payload, width, height, ui_map)
    return _strip_anchor(steps), checks


def _ocr_lines(reader: Any, raw: bytes) -> List[str]:
    """Optional caller-supplied OCR. Does not import the like locator."""
    if not callable(reader) or not raw:
        return []
    try:
        boxes = reader(raw)
    except Exception:
        logger.debug("post ocr unread", exc_info=True)
        return []
    if isinstance(boxes, str):
        return [boxes]
    if not isinstance(boxes, list):
        return []
    lines: List[str] = []
    for box in boxes:
        if isinstance(box, str):
            lines.append(box)
        elif isinstance(box, dict):
            text = box.get("text") or box.get("content") or ""
            if isinstance(text, str) and text:
                lines.append(text)
    return lines


def _window_xml(ops: Any, serial: str) -> str:
    reader = getattr(ops, "read_window_xml", None)
    if not callable(reader):
        return ""
    try:
        got = reader(serial)
    except Exception:
        logger.debug("post window unread", exc_info=True)
        return ""
    return got if isinstance(got, str) else ""


def _at_caption_gate(steps: List[Step], index: int, kind: str, payload: Dict[str, Any]) -> bool:
    """The step just before the caption is typed. Composer field when it sits there."""
    if kind == TASK_PHONE_TAP and payload.get("_anchor") == "composer_field":
        return index + 1 < len(steps) and steps[index + 1][0] == TASK_PHONE_TEXT
    if kind == TASK_PHONE_TEXT:
        if index == 0:
            return True
        return _tap_anchor(steps[index - 1]) != "composer_field"
    return False


def _maybe_next_tap(ops: Any, serial: str, ocr: Any, width: int, height: int) -> Optional[Tuple[int, int]]:
    """One quiet dump. A point only when the label is unambiguously Next."""
    xml = _window_xml(ops, serial)
    point = distinct_next_point(xml)
    if point is None:
        return None
    if callable(ocr):
        raw = b""
        last = getattr(ops, "last_raw", None)
        if callable(last):
            try:
                got = last(serial)
            except Exception:
                got = b""
            if isinstance(got, (bytes, bytearray)):
                raw = bytes(got)
        if ocr_vetoes_next(_ocr_lines(ocr, raw)):
            return None
    x = max(0, min(int(point[0]), width - 1, 9999))
    y = max(0, min(int(point[1]), height - 1, 9999))
    return x, y


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


def _merge_dump_meta(diag: Dict[str, Any], meta: Any) -> None:
    """Copy dump transport into ``like_diag``. The text is scrubbed later."""
    if not isinstance(meta, dict):
        return
    uia = diag.get("uiautomator")
    if not isinstance(uia, dict):
        return
    attempts = meta.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        attempts = 0
    via = meta.get("via")
    error = meta.get("error")
    uia["attempts"] = attempts
    uia["via"] = via if via in ("file", "stdout") else ""
    uia["compressed"] = meta.get("compressed") is True
    uia["error"] = error if isinstance(error, str) else ""


_FB_PACKAGES = ("com.facebook.katana", "com.facebook.lite")
_STORE_PACKAGES = frozenset({
    "com.android.vending",
    "com.google.android.finsky",
    "com.android.market",
})
_PKG_RE = re.compile(r"[A-Za-z][\w.]*\Z")


def classify_facebook_open(package: str) -> str:
    """Failure code after an open attempt. Empty when Facebook is in front or unknown.

    An empty package is unknown (dumpsys missed), so the caller keeps the pixel
    probes. Play Store and a different app are explicit codes, never ``not_logged_in``.
    """
    pkg = str(package or "").strip()
    if not pkg or pkg in _FB_PACKAGES:
        return ""
    if not _PKG_RE.fullmatch(pkg) or len(pkg) > 80:
        return "wrong_app_launched:unknown"
    if pkg in _STORE_PACKAGES or "finsky" in pkg or pkg.endswith(".vending"):
        return "fb_not_installed_or_store_redirect"
    return "wrong_app_launched:" + pkg


def _foreground_package(ops: Any, serial: str) -> str:
    reader = getattr(ops, "read_foreground", None)
    if not callable(reader):
        return ""
    try:
        found = reader(serial)
    except Exception:
        logger.debug("foreground unread", exc_info=True)
        return ""
    if not isinstance(found, dict):
        return ""
    return str(found.get("package") or "")


def _sleep_forward(ops: Any, seconds: float) -> bool:
    """Sleep once. False when the clock did not move, so a poll cannot spin."""
    before = float(ops._clock())
    sleeper = getattr(ops, "_sleep", None)
    if callable(sleeper):
        sleeper(seconds)
    return float(ops._clock()) > before


def _poll_facebook_foreground(ops: Any, serial: str) -> str:
    """Foreground package after up to 9 seconds. Returns early when Facebook is up."""
    deadline = float(ops._clock()) + _FB_FOREGROUND_POLL_SEC
    last = ""
    while True:
        last = _foreground_package(ops, serial)
        if last in _FB_PACKAGES:
            return last
        if float(ops._clock()) >= deadline:
            return last
        if not _sleep_forward(ops, _FB_FOREGROUND_POLL_STEP):
            return _foreground_package(ops, serial)


def _needs_icon_fallback(package: str) -> bool:
    """A concrete other app. An empty dumpsys is unknown, not a failed launch."""
    pkg = str(package or "").strip()
    return bool(pkg) and pkg not in _FB_PACKAGES


def _tap_home_icon(run: Any, anchors: Dict[str, Any], overrides: Dict[str, Any],
                   width: int, height: int) -> None:
    """0.3.25 open: Home, then the Facebook icon on the launcher."""
    run(TASK_PHONE_KEY, {"key": "home"})
    x, y = _point("app_icon", anchors, overrides, width, height)
    run(TASK_PHONE_TAP, {"x": x, "y": y})


def _open_facebook(ops: Any, serial: str, run: Any, anchors: Dict[str, Any],
                   overrides: Dict[str, Any], width: int, height: int) -> str:
    """Bring Facebook forward. "" when it is in front or the foreground is unknown.

    Wake and unlock first. ``am start`` is polled for about 9 seconds. If the
    foreground is still another app, press Home and tap the icon, then poll
    again. Both misses return ``classify_facebook_open``. A missing package
    is ``fb_not_installed_or_store_redirect`` and does not run ``am start``.
    Already-in-front does not press Home.
    """
    package = _foreground_package(ops, serial)
    if package in _FB_PACKAGES:
        return ""
    launcher = getattr(ops, "launch_facebook", None)
    if not callable(launcher):
        return "app_not_ready"
    finder = getattr(ops, "facebook_package", None)
    if not callable(finder):
        return "app_not_ready"
    target = str(finder(serial) or "")
    if target not in _FB_PACKAGES:
        return "fb_not_installed_or_store_redirect"
    preparer = getattr(ops, "prepare_foreground", None)
    if callable(preparer):
        preparer(serial, width, height)
    launcher(serial, target)
    seen = _poll_facebook_foreground(ops, serial)
    if seen in _FB_PACKAGES:
        return ""
    if _needs_icon_fallback(seen):
        _tap_home_icon(run, anchors, overrides, width, height)
        seen = _poll_facebook_foreground(ops, serial)
        if seen in _FB_PACKAGES:
            return ""
    return classify_facebook_open(seen)


def _coerce_portrait(ops: Any, run: Any, serial: str, width: int, height: int) -> Tuple[int, int, str]:
    """When the shot is landscape, lock portrait and shoot again.

    Returns ``(width, height, code)``. ``code`` is empty on a portrait frame,
    ``landscape_orientation`` when the second shot is still landscape, or
    ``screencap_bad_frame`` when the second shot has no size.
    """
    if width <= height:
        return width, height, ""
    restorer = getattr(ops, "restore_portrait", None)
    if callable(restorer):
        try:
            restorer(serial)
        except PhoneOpError:
            logger.debug("portrait restore was rejected", exc_info=True)
        except Exception:
            logger.debug("portrait restore failed", exc_info=True)
    shot = run(TASK_PHONE_SCREENSHOT, {})
    w, h = shot.get("device_width"), shot.get("device_height")
    if isinstance(w, bool) or not isinstance(w, int) or isinstance(h, bool) or not isinstance(h, int):
        return width, height, "screencap_bad_frame"
    if w > h:
        return w, h, "landscape_orientation"
    return w, h, ""


def _attach_foreground(ops: Any, serial: str, result: Dict[str, Any], code: str) -> None:
    """On a login, readiness, or wrong-app miss, record the resumed package and activity.

    The text is scrubbed later. A reader that fails leaves the result unchanged.
    """
    tracked = code in (
        "not_logged_in", "app_not_ready", "fb_not_installed_or_store_redirect", "landscape_orientation",
    ) or str(code).startswith("wrong_app_launched:")
    if not tracked or not isinstance(result, dict):
        return
    reader = getattr(ops, "read_foreground", None)
    if not callable(reader):
        return
    try:
        found = reader(serial)
    except Exception:
        logger.debug("foreground unread after login check", exc_info=True)
        return
    if not isinstance(found, dict):
        return
    package = str(found.get("package") or "")[:80]
    activity = str(found.get("activity") or "")[:120]
    if package or activity:
        result["foreground"] = {"package": package, "activity": activity}


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
                 state_dir: Optional[Path] = None, ocr: Any = _UNSET, pace: Any = None) -> None:
        """``state_dir`` is the fleet state directory. NodeAgent always passes it.

        ``ocr`` is an optional text reader ``(screencap bytes) -> boxes``. The
        default tries the optional rapidocr package. ``False`` skips text.
        Icon templates do not use this argument.

        ``pace`` is an optional ``PaceLedger``. The default loads
        ``config/compliance.yaml`` and, when ``state_dir`` is set, keeps a
        local copy of counted Facebook actions there.
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
        self._pace_injected = pace is not None
        self._pace = pace if isinstance(pace, PaceLedger) else PaceLedger()
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
        if getattr(self, "_pace", None) is not None and not getattr(self, "_pace_injected", False):
            self._pace.bind(self.state_dir)

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
        """Launch Facebook, scroll the feed, tap Like only when sure.

        If Facebook is already in front, this does not press Home. Otherwise
        it wakes the screen, ``am start``s katana or lite, and polls the
        foreground for about 9 seconds. A foreground that is still another
        app falls back to Home plus the icon. A missing package is
        ``fb_not_installed_or_store_redirect``. Landing on Telegram or the
        Play Store is an explicit code, not ``not_logged_in``. A landscape
        frame is locked back to portrait first. After a successful open, poll
        the feed for about 9 seconds. ``like_probe`` returns the row and does
        not tap it.
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
                width, height, orient = _coerce_portrait(ops, run, serial, width, height)
                if orient:
                    return STATUS_FAILED, _result(
                        serial, "facebook", "like", completed=1, width=width, height=height,
                    ), orient
                opened = _open_facebook(ops, serial, run, anchors, overrides, width, height)
                if opened:
                    err = PhoneOpError(opened, failed=True)
                    err.stderr = "foreground"  # type: ignore[attr-defined]
                    raise err
                up_x, up_y = _point(dst_name, anchors, overrides, width, height)
                down_x, down_y = _point(src_name, anchors, overrides, width, height)

                def _swipe_toward_top() -> None:
                    # Reverse of the feed swipe: the finger moves down, the feed
                    # returns to the top, and Facebook shows the tab bar again.
                    run(TASK_PHONE_SWIPE, {
                        "x1": up_x, "y1": up_y, "x2": down_x, "y2": down_y, "duration_ms": dur,
                    })

                opened_at = ops._clock()
                deadline = opened_at + _FB_FEED_OPEN_BUDGET_SEC
                reveals = 0
                ready = False
                while True:
                    run(TASK_PHONE_SCREENSHOT, {})
                    raw = ops.last_raw(serial)
                    if self._probe_hit(raw, probes.get("login_wall"), anchors, overrides, width, height):
                        err = PhoneOpError("not_logged_in", failed=True)
                        err.stderr = "probe:login_wall"  # type: ignore[attr-defined]
                        raise err
                    if self._probe_hit(raw, probes.get("logged_in"), anchors, overrides, width, height):
                        ready = True
                        break
                    now = ops._clock()
                    if now >= deadline:
                        break
                    # The app may still be drawing. Only after the quiet window
                    # do we scroll up, which is what brings a hidden tab bar back.
                    if now >= opened_at + _FB_FEED_OPEN_QUIET_SEC and reveals < _FB_FEED_TOP_SWIPES:
                        _swipe_toward_top()
                        reveals += 1
                if not ready:
                    err = PhoneOpError("app_not_ready", failed=True)
                    err.stderr = "probe:logged_in"  # type: ignore[attr-defined]
                    raise err
                # Land at the top before the action-bar search. Swipes already
                # spent revealing the tab bar count toward that.
                for _ in range(_FB_FEED_TOP_SWIPES - reveals):
                    _swipe_toward_top()
                ops.add_dwell(serial, _FB_FEED_TOP_SETTLE_SEC)
                fx, fy = _point("feed_tab", anchors, overrides, width, height)
                run(TASK_PHONE_TAP, {"x": fx, "y": fy})
                found = None
                saw_post = False
                last_diag: Optional[Dict[str, Any]] = None
                boxes: List[Dict[str, Any]] = []
                for attempt in range(budget + 1):
                    run(TASK_PHONE_SCREENSHOT, {})
                    raw = ops.last_raw(serial)
                    boxes = self._text_boxes(raw)
                    # Dump is read-only. A phone that refuses it still has the
                    # screenshot signals (structure + template / silhouette).
                    hierarchy = ""
                    dump_meta = None
                    reader = getattr(ops, "read_ui_hierarchy", None)
                    if callable(reader):
                        try:
                            got = reader(serial)
                        except Exception:
                            got = ""
                        if isinstance(got, dict):
                            hierarchy = str(got.get("xml") or "")
                            dump_meta = got
                        elif isinstance(got, str):
                            hierarchy = got
                    loc = locate_like_row(raw, boxes, hierarchy)
                    if isinstance(loc.get("diag"), dict):
                        last_diag = loc["diag"]
                        _merge_dump_meta(last_diag, dump_meta)
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
                    if isinstance(last_diag, dict):
                        base["like_diag"] = last_diag
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
            _attach_foreground(ops, serial, res, e.code)
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
            if app == "facebook" and map_flow == "post_media":
                body["media_next"] = "gated"
            return STATUS_DONE, body, "dry_run"
        if app == "facebook" and flow in ("like", "comment", "follow", "post"):
            decision = self._pace.allow(
                app=app, action=flow, serial=serial,
                account=str((target or {}).get("account") or "") if isinstance(target, dict) else "",
                wallpaper=str((target or {}).get("wallpaper") or (target or {}).get("wallpaper_no") or "")
                if isinstance(target, dict) else "",
                probe=p.get("like_probe") is True,
            )
            if not decision.allow:
                return STATUS_REJECTED, _result(serial, app, flow), decision.reason
        if app == "facebook" and flow == "like":
            return self._execute_facebook_like(serial, p, ui, ops)
        prog: Dict[str, Any] = {"done": 0, "step": None, "w": None, "h": None}
        t0 = ops._clock()
        chars = 0
        publish_verified = False
        try:
            with ops.session(serial) as run:
                try:
                    shot = run(TASK_PHONE_SCREENSHOT, {})
                except PhoneOpError as e:
                    res = _result(serial, app, flow, completed=0, err=e)
                    _attach_foreground(ops, serial, res, e.code)
                    return _status(e), res, e.code
                w, h = shot.get("device_width"), shot.get("device_height")
                if isinstance(w, bool) or not isinstance(w, int) or isinstance(h, bool) or not isinstance(h, int):
                    return STATUS_FAILED, _result(serial, app, flow, completed=1), "screencap_bad_frame"
                w, h, orient = _coerce_portrait(ops, run, serial, w, h)
                if orient:
                    return STATUS_FAILED, _result(serial, app, flow, completed=1, width=w, height=h), orient
                prog.update(done=1, w=w, h=h)
                verify_on, jitter, retries = resolve_policy(ui, self._verify, self._jitter)
                try:
                    steps, checks = _compile(app, map_flow, p, w, h, ui)
                    if not verify_on and jitter == (0, 0):
                        checks = [None] * len(steps)
                except PhoneOpError as e:
                    return _status(e), _result(serial, app, flow, completed=1, width=w, height=h, err=e), e.code
                spec = ui["apps"][app]
                next_gated = False
                for i, (sk, sp) in enumerate(steps):
                    prog["step"] = i
                    if app == "facebook" and map_flow == "post_media" and not next_gated and _at_caption_gate(steps, i, sk, sp):
                        next_gated = True
                        point = _maybe_next_tap(ops, serial, self._ocr, w, h)
                        if point is not None:
                            run(TASK_PHONE_TAP, {"x": point[0], "y": point[1]})
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
                            res = _result(
                                serial, app, flow, completed=prog["done"], failed_step=i, width=w, height=h, err=e,
                            )
                            _attach_foreground(ops, serial, res, e.code)
                            return _status(e), res, e.code
                        if check is None:
                            break
                        try:
                            self._confirm(ops, run, serial, check, spec, overrides)
                            break
                        except PhoneOpError as e:
                            if e.code not in _CHECK_FAILS or attempt + 1 >= tries:
                                res = _result(
                                    serial, app, flow, completed=prog["done"], failed_step=i,
                                    width=w, height=h, err=e,
                                )
                                _attach_foreground(ops, serial, res, e.code)
                                return _status(e), res, e.code
                    prog["done"] = int(prog["done"]) + 1
                    if sk == TASK_PHONE_TEXT and isinstance(res, dict):
                        chars += int(res.get("chars") or 0)
                if p.get("verify_publish") is True and flow == "post":
                    extra: List[str] = []
                    if callable(self._ocr):
                        raw = b""
                        last = getattr(ops, "last_raw", None)
                        if callable(last):
                            try:
                                got = last(serial)
                            except Exception:
                                got = b""
                            if isinstance(got, (bytes, bytearray)):
                                raw = bytes(got)
                        extra = _ocr_lines(self._ocr, raw)
                    if assess_publish(_window_xml(ops, serial), extra) != "confirmed":
                        err = PhoneOpError("post_not_confirmed", failed=True)
                        err.stderr = "publish:unproven"  # type: ignore[attr-defined]
                        raise err
                    publish_verified = True
            elapsed = int(max(0.0, ops._clock() - t0) * 1000)
            out: Dict[str, Any] = {
                "serial": serial, "app": app, "flow": flow, "steps": int(prog["done"]), "elapsed_ms": elapsed,
                "device_width": w, "device_height": h,
            }
            if chars:
                out["chars"] = chars
            if publish_verified:
                out["publish_verified"] = True
            if flow in SESSION_FLOW_NAMES:
                for key in ("scrolls", "watches", "likes"):
                    if isinstance(p.get(key), int) and not isinstance(p.get(key), bool):
                        out[key] = p[key]
                out["dwells"] = sum(1 for sk, _sp in steps if sk == _DWELL_KIND)
            return STATUS_DONE, out, "ok"
        except PhoneOpError as e:
            res = _result(
                serial, app, flow, completed=int(prog["done"]), failed_step=prog["step"],
                width=prog["w"], height=prog["h"], err=e,
            )
            _attach_foreground(ops, serial, res, e.code)
            return _status(e), res, e.code
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
