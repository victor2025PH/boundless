"""社交动作的共享规则（主控入队 + 节点执行器用同一份）。

复合动作认七个种类：发帖 / 点赞 / 评论 / 关注，以及养号 / 私信 / 看短视频。
应用只认 facebook、instagram、tiktok。文字规则与低层 ``phone_text`` 相同（ASCII，不支持中文）。
坐标覆盖是绝对像素，名字必须是小写标识符；这个名字是否属于该应用的锚点表，到节点编译时才对
（主控没有那台机器的坐标文件）。
"""

from __future__ import annotations

import re
from typing import Any, Dict

from .phone_rules import MAX_COORD, TEXT_ALLOWED, PhoneOpError, validate_payload
from .protocol import (
    FLOW_NAMES, SESSION_FLOW_NAMES, SOCIAL_APPS, TASK_PHONE_COMMENT, TASK_PHONE_DM, TASK_PHONE_FOLLOW,
    TASK_PHONE_LIKE, TASK_PHONE_POST, TASK_PHONE_TEXT, TASK_PHONE_WARMUP, TASK_PHONE_WATCH,
)

_KIND_BY_FLOW = {
    "post": TASK_PHONE_POST, "like": TASK_PHONE_LIKE, "comment": TASK_PHONE_COMMENT, "follow": TASK_PHONE_FOLLOW,
    "warmup": TASK_PHONE_WARMUP, "dm": TASK_PHONE_DM, "watch": TASK_PHONE_WATCH,
}
FLOW_BY_KIND = {v: k for k, v in _KIND_BY_FLOW.items()}
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,64}$")
ANCHOR_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_ANCHORS = 16
MAX_SCROLLS = 8
MAX_MEDIA_SLOT = 5


def kind_for_flow(flow: Any) -> str:
    return _KIND_BY_FLOW.get(str(flow or "").strip().lower(), "")


def _int_field(payload: Dict[str, Any], key: str, lo: int, hi: int, code: str) -> int:
    v = payload.get(key)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise PhoneOpError(code)
    return v


def _caption(payload: Dict[str, Any]) -> str:
    try:
        return validate_payload(TASK_PHONE_TEXT, {"text": payload.get("text")})["text"]
    except PhoneOpError:
        raise


def _handle(payload: Dict[str, Any]) -> str:
    t = payload.get("handle")
    if not isinstance(t, str) or not HANDLE_RE.match(t) or not TEXT_ALLOWED.match(t) or t.startswith("-"):
        raise PhoneOpError("bad_handle")
    return t


def _media(payload: Dict[str, Any]) -> int:
    if "media" not in payload:
        return 0
    v = payload.get("media")
    if v is True or v == 1:
        return 1
    if v is False or v == 0:
        return 0
    raise PhoneOpError("bad_media")


def _anchors(payload: Dict[str, Any]) -> Dict[str, Dict[str, int]]:
    if "anchors" not in payload:
        return {}
    raw = payload.get("anchors")
    if not isinstance(raw, dict) or len(raw) > MAX_ANCHORS:
        raise PhoneOpError("bad_anchor")
    out: Dict[str, Dict[str, int]] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not ANCHOR_RE.match(name) or not isinstance(spec, dict):
            raise PhoneOpError("bad_anchor")
        out[name] = {
            "x": _int_field(spec, "x", 0, MAX_COORD - 1, "bad_anchor"),
            "y": _int_field(spec, "y", 0, MAX_COORD - 1, "bad_anchor"),
        }
    return out


def _json_bool(payload: Dict[str, Any], key: str) -> Any:
    if key not in payload:
        return None
    value = payload.get(key)
    if not isinstance(value, bool):
        raise PhoneOpError("bad_dry_run")
    return value


def _wants_dry_run(payload: Dict[str, Any]) -> bool:
    """JSON true on dry_run or predict_only. Disagreeing flags or non-bools are bad_dry_run."""
    dry = _json_bool(payload, "dry_run")
    pred = _json_bool(payload, "predict_only")
    if dry is not None and pred is not None and dry is not pred:
        raise PhoneOpError("bad_dry_run")
    chosen = dry if dry is not None else pred
    return chosen is True


def validate_flow_payload(kind: str, payload: Any) -> Dict[str, Any]:
    """返回规范化 payload（多余的键丢掉）。不合法 → PhoneOpError(原因码)。

    dry_run / predict_only: only JSON true is kept, normalized to dry_run true.
    JSON false or a missing flag is omitted (real execution). A non-bool, or the
    two flags disagreeing, is bad_dry_run.
    """
    p = payload if isinstance(payload, dict) else {}
    app = str(p.get("app") or "").strip().lower() if isinstance(p.get("app"), str) else ""
    if app not in SOCIAL_APPS:
        raise PhoneOpError("bad_app")
    flow = FLOW_BY_KIND.get(kind, "")
    if flow not in FLOW_NAMES and flow not in SESSION_FLOW_NAMES:
        raise PhoneOpError("bad_flow")
    out: Dict[str, Any] = {"app": app, "anchors": _anchors(p)}
    if not out["anchors"]:
        out.pop("anchors")
    if flow == "post":
        out["text"] = _caption(p)
        out["media"] = _media(p)
        out["media_slot"] = _int_field(p, "media_slot", 0, MAX_MEDIA_SLOT, "bad_media_slot") if "media_slot" in p else 0
    elif flow == "like":
        out["scrolls"] = _int_field(p, "scrolls", 0, MAX_SCROLLS, "bad_scrolls") if "scrolls" in p else 0
        # like_swipes / like_probe are the Facebook scan. Other apps keep the fixed coordinate.
        if "like_swipes" in p:
            if app != "facebook":
                raise PhoneOpError("bad_like_swipes")
            out["like_swipes"] = _int_field(p, "like_swipes", 0, MAX_SCROLLS, "bad_like_swipes")
        if "like_probe" in p:
            flag = p.get("like_probe")
            if not isinstance(flag, bool):
                raise PhoneOpError("bad_like_probe")
            if flag and app != "facebook":
                raise PhoneOpError("bad_like_probe")
            if flag:
                out["like_probe"] = True
    elif flow == "comment":
        out["text"] = _caption(p)
        out["scrolls"] = _int_field(p, "scrolls", 0, MAX_SCROLLS, "bad_scrolls") if "scrolls" in p else 0
    elif flow == "follow":
        out["handle"] = _handle(p)
    elif flow == "warmup":
        scrolls = _int_field(p, "scrolls", 1, MAX_SCROLLS, "bad_scrolls") if "scrolls" in p else 4
        likes = _int_field(p, "likes", 0, MAX_SCROLLS, "bad_likes") if "likes" in p else 0
        if likes > scrolls:
            raise PhoneOpError("bad_likes")
        out["scrolls"] = scrolls
        out["likes"] = likes
    elif flow == "watch":
        watches = _int_field(p, "watches", 1, MAX_SCROLLS, "bad_watches") if "watches" in p else 3
        likes = _int_field(p, "likes", 0, MAX_SCROLLS, "bad_likes") if "likes" in p else 0
        if likes > watches:
            raise PhoneOpError("bad_likes")
        out["watches"] = watches
        out["likes"] = likes
    elif flow == "dm":
        out["handle"] = _handle(p)
        out["text"] = _caption(p)
    else:
        raise PhoneOpError("bad_flow")
    if _wants_dry_run(p):
        out["dry_run"] = True
    return out


__all__ = [
    "FLOW_BY_KIND", "HANDLE_RE", "ANCHOR_RE", "MAX_ANCHORS", "MAX_SCROLLS", "MAX_MEDIA_SLOT",
    "kind_for_flow", "validate_flow_payload",
]
