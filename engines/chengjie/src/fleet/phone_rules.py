"""远程手机操作的共享规则（主控路由 + 节点执行器用同一份）：操作名、参数校验、受保护手机、结果清洗。

纯函数，不碰 adb。受保护手机（直播用 OnePlus 13：serial 3B1F4KE5MS140P4X、无线地址 192.168.0.148，
见 ``phones.is_protected``）主控和节点两侧都硬拒，不看任何配置。

参数规则（两侧一致）：
* tap ``{x, y}``、swipe ``{x1, y1, x2, y2, duration_ms}``：设备像素整数，0 ≤ v < 10000；duration 50–3000 ms。
* text ``{text}``：1–200 个 ASCII 字符，只允许字母数字、空格和 ``. _ @ + - : / = ,``，不能以 ``-`` 开头。
  这些字符在设备 shell 里没有特殊含义；空格按 ``input text`` 的约定换成 ``%s``。中文等非 ASCII 一律拒绝
  （``input text`` 本身不支持，需要输入法 APK，不在本轮范围）。
* key ``{key}``：只有 ``home`` / ``back``。
"""

from __future__ import annotations

import re
from typing import Any, Dict

from .phones import is_protected
from .protocol import TASK_PHONE_KEY, TASK_PHONE_SCREENSHOT, TASK_PHONE_SWIPE, TASK_PHONE_TAP, TASK_PHONE_TEXT

OPS: Dict[str, str] = {
    "screenshot": TASK_PHONE_SCREENSHOT, "tap": TASK_PHONE_TAP, "swipe": TASK_PHONE_SWIPE,
    "text": TASK_PHONE_TEXT, "key": TASK_PHONE_KEY,
}
MAX_COORD = 10000
TEXT_MAX = 200
SWIPE_MS_MIN, SWIPE_MS_MAX, SWIPE_MS_DEFAULT = 50, 3000, 300
KEYCODES: Dict[str, str] = {"home": "3", "back": "4"}
TEXT_ALLOWED = re.compile(r"^[A-Za-z0-9 ._@+\-:/=,]{1,200}$")
MAX_PNG_B64 = 1_800_000
_SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_PNG_B64_PREFIX = "iVBORw0KGgo"
_WALL_RE = re.compile(r"^\d{1,4}$")
_BOUNDS_RE = re.compile(r"\[\d+,\d+\]\[\d+,\d+\]")
_DEVICE_QUOTED_RE = re.compile(r"(device\s+')([^']+)('\s+not\s+found)", re.IGNORECASE)
_DEVICE_BARE_RE = re.compile(
    r"(device\s+)([A-Za-z0-9][A-Za-z0-9._:\-]{3,63})(\s+not\s+found)", re.IGNORECASE,
)
_DIAG_DUMPS = {"ok", "empty", "bad"}
_DIAG_BY = {"content-desc", "text", "resource-id"}
_RESULT_INTS = ("width", "height", "device_width", "device_height", "scale", "x", "y", "x1", "y1", "x2", "y2",
                "duration_ms", "bytes", "chars", "elapsed_ms", "steps", "failed_step", "completed_steps",
                "scrolls", "likes", "watches", "dwells", "like_x", "like_y", "swipes")
_RESULT_STRS = ("serial", "key", "error", "stderr", "app", "flow", "like_label", "like_signals")


class PhoneOpError(ValueError):
    """校验 / 执行失败的原因码。``failed=True`` 表示动手后失败（任务记 failed），否则是拒绝（rejected）。"""

    def __init__(self, code: str, *, failed: bool = False) -> None:
        super().__init__(code)
        self.code = str(code)
        self.failed = bool(failed)


def valid_serial(value: Any) -> str:
    s = value.strip() if isinstance(value, str) else ""
    return s if _SERIAL_RE.match(s) else ""


def kind_for_op(op: Any) -> str:
    return OPS.get(str(op or "").strip().lower(), "")


def _int_field(payload: Dict[str, Any], key: str, lo: int, hi: int, code: str) -> int:
    v = payload.get(key)
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise PhoneOpError(code)
    return v


def validate_payload(kind: str, payload: Any) -> Dict[str, Any]:
    """返回规范化后的 payload（只含该操作认识的键）；不合法 → PhoneOpError(原因码)。"""
    p = payload if isinstance(payload, dict) else {}
    if kind == TASK_PHONE_SCREENSHOT:
        return {}
    if kind == TASK_PHONE_TAP:
        return {k: _int_field(p, k, 0, MAX_COORD - 1, f"bad_{k}") for k in ("x", "y")}
    if kind == TASK_PHONE_SWIPE:
        out = {k: _int_field(p, k, 0, MAX_COORD - 1, f"bad_{k}") for k in ("x1", "y1", "x2", "y2")}
        if "duration_ms" in p:
            out["duration_ms"] = _int_field(p, "duration_ms", SWIPE_MS_MIN, SWIPE_MS_MAX, "bad_duration_ms")
        else:
            out["duration_ms"] = SWIPE_MS_DEFAULT
        return out
    if kind == TASK_PHONE_TEXT:
        t = p.get("text")
        if not isinstance(t, str) or not t:
            raise PhoneOpError("bad_text")
        if len(t) > TEXT_MAX:
            raise PhoneOpError("text_too_long")
        if not t.isascii():
            raise PhoneOpError("text_non_ascii_unsupported")
        if not TEXT_ALLOWED.match(t) or t.startswith("-"):
            raise PhoneOpError("text_bad_chars")
        return {"text": t}
    if kind == TASK_PHONE_KEY:
        k = str(p.get("key") or "").strip().lower()
        if k not in KEYCODES:
            raise PhoneOpError("bad_key")
        return {"key": k}
    raise PhoneOpError("bad_kind")


def check_target(serial: Any) -> str:
    """规范化目标 serial；不合法 → bad_serial，受保护手机 → protected_phone。"""
    s = valid_serial(serial)
    if not s:
        raise PhoneOpError("bad_serial")
    if is_protected(s):
        raise PhoneOpError("protected_phone")
    return s


def escape_input_text(text: str) -> str:
    """``input text`` 的参数：空格 → ``%s``。字符集已被 TEXT_ALLOWED 限死，设备 shell 不会解释任何字符。"""
    return text.replace(" ", "%s")


def _clip(value: Any, n: int) -> str:
    return "".join(ch for ch in str(value or "") if ch.isprintable())[:n]


_PLAN_MAX_STEPS = 128
_PLAN_COORD_MAX = 9999


def _plan_int(value: Any, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def _sanitize_plan_step(step: Any) -> Any:
    if not isinstance(step, dict):
        return None
    op = step.get("op")
    if op == "tap" and _plan_int(step.get("x"), 0, _PLAN_COORD_MAX) and _plan_int(step.get("y"), 0, _PLAN_COORD_MAX):
        return {"op": "tap", "x": step["x"], "y": step["y"]}
    if op == "swipe":
        coords = ("x1", "y1", "x2", "y2")
        if not all(_plan_int(step.get(k), 0, _PLAN_COORD_MAX) for k in coords):
            return None
        if not _plan_int(step.get("duration_ms"), SWIPE_MS_MIN, SWIPE_MS_MAX):
            return None
        return {"op": "swipe", **{k: step[k] for k in coords}, "duration_ms": step["duration_ms"]}
    if op == "key" and step.get("key") in KEYCODES:
        return {"op": "key", "key": step["key"]}
    if op == "text":
        text = step.get("text")
        chars = step.get("chars")
        if (not isinstance(text, str) or not TEXT_ALLOWED.match(text) or text.startswith("-")
                or not _plan_int(chars, 1, TEXT_MAX) or chars != len(text)):
            return None
        return {"op": "text", "text": text, "chars": chars}
    if op == "dwell" and _plan_int(step.get("lo_ms"), 0, 12000) and _plan_int(step.get("hi_ms"), 0, 12000):
        if step["lo_ms"] > step["hi_ms"]:
            return None
        return {"op": "dwell", "lo_ms": step["lo_ms"], "hi_ms": step["hi_ms"]}
    return None


def _sanitize_plan(plan: Any) -> Any:
    if not isinstance(plan, list) or len(plan) > _PLAN_MAX_STEPS:
        return None
    out = []
    for step in plan:
        clean = _sanitize_plan_step(step)
        if clean is None:
            return None
        out.append(clean)
    return out


def _redaction_label(wallpaper: Any) -> str:
    text = str(wallpaper or "").strip()
    if _WALL_RE.fullmatch(text):
        try:
            if int(text) >= 1:
                return text
        except ValueError:
            pass
    return "[redacted]"


def scrub_phone_error_text(text: Any, *, serial: str = "", wallpaper: str = "") -> str:
    """Replace a raw adb serial in an error string with the wallpaper number.

    A known serial of 4 or more characters is replaced wherever it appears.
    ``device '…' not found`` is rewritten even when the serial was not passed
    in, which is the phrase adb prints when the phone is gone. Shorter tokens
    are left alone so a message such as ``device offline`` stays intact.
    """
    if not isinstance(text, str):
        return ""
    if not text:
        return text
    repl = _redaction_label(wallpaper)
    known = serial.strip() if isinstance(serial, str) else ""
    out = text
    if len(known) >= 4:
        out = re.sub(re.escape(known), repl, out, flags=re.IGNORECASE)

    def _swap(match: re.Match) -> str:
        token = match.group(2)
        if known and token.casefold() == known.casefold():
            return match.group(1) + repl + match.group(3)
        if len(token) >= 4:
            return match.group(1) + repl + match.group(3)
        return match.group(0)

    out = _DEVICE_QUOTED_RE.sub(_swap, out)
    return _DEVICE_BARE_RE.sub(_swap, out)


def scrub_phone_tree(value: Any, *, serial: str = "", wallpaper: str = "") -> Any:
    """Scrub serials inside a nested diagnostic. Keys and numbers stay put."""
    if isinstance(value, str):
        return scrub_phone_error_text(value, serial=serial, wallpaper=wallpaper)
    if isinstance(value, list):
        return [scrub_phone_tree(item, serial=serial, wallpaper=wallpaper) for item in value]
    if isinstance(value, dict):
        return {
            str(key): scrub_phone_tree(item, serial=serial, wallpaper=wallpaper)
            for key, item in value.items()
        }
    return value


def _diag_str(value: Any, n: int = 80) -> str:
    return "".join(ch for ch in str(value or "") if ch.isprintable() and ch not in "<>")[:n]


def _diag_list(value: Any) -> list:
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        text = _diag_str(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= 12:
            break
    return out


def _sanitize_like_diag(raw: Any) -> Any:
    """Keep the probe's per-signal report. Drop pixels, unknown keys, and long text."""
    if not isinstance(raw, dict):
        return None
    uia_in = raw.get("uiautomator") if isinstance(raw.get("uiautomator"), dict) else {}
    dump = uia_in.get("dump")
    uia: Dict[str, Any] = {
        "dump": dump if dump in _DIAG_DUMPS else "bad",
        "like_found": uia_in.get("like_found") is True,
    }
    match = uia_in.get("match")
    if uia["like_found"] and isinstance(match, dict):
        by = match.get("by")
        bounds = _diag_str(match.get("bounds"), 40)
        uia["match"] = {
            "label": _diag_str(match.get("label"), 32),
            "by": by if by in _DIAG_BY else "",
            "content_desc": _diag_str(match.get("content_desc")),
            "text": _diag_str(match.get("text")),
            "resource_id": _diag_str(match.get("resource_id")),
            "bounds": bounds if _BOUNDS_RE.fullmatch(bounds) else "",
        }
    bar_in = raw.get("action_bar") if isinstance(raw.get("action_bar"), dict) else {}
    score = raw.get("template_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        score_out = 0.0
    else:
        score_out = round(max(0.0, min(1.0, float(score))), 3)
    nodes = []
    raw_nodes = raw.get("nodes")
    if isinstance(raw_nodes, list):
        for item in raw_nodes:
            if not isinstance(item, dict):
                continue
            bounds = _diag_str(item.get("bounds"), 40)
            if not _BOUNDS_RE.fullmatch(bounds):
                continue
            nodes.append({
                "bounds": bounds,
                "class": _diag_str(item.get("class")),
                "content_desc": _diag_str(item.get("content_desc")),
                "resource_id": _diag_str(item.get("resource_id")),
                "text": _diag_str(item.get("text")),
            })
            if len(nodes) >= 24:
                break
    return {
        "uiautomator": uia,
        "action_bar": {
            "content_descs": _diag_list(bar_in.get("content_descs")),
            "texts": _diag_list(bar_in.get("texts")),
            "resource_ids": _diag_list(bar_in.get("resource_ids")),
        },
        "template_score": score_out,
        "template_matched": raw.get("template_matched") is True,
        "structure_matched": raw.get("structure_matched") is True,
        "position_matched": raw.get("position_matched") is True,
        "nodes": nodes,
    }


def sanitize_phone_result(kind: str, result: Any, *, serial: str = "", wallpaper: str = "") -> Dict[str, Any]:
    """主控入库前：只留已知的数字 / 短字符串；截图只认 PNG 的 base64（控制台当 <img src> 用）。

    A dry_run plan is kept only when dry_run is JSON true. Planned text stays inside
    that plan. The same plan without dry_run is dropped, and top-level text is never kept.
    ``like_probe`` may also keep ``like_diag`` (text attributes of the action bar).
    Error strings lose the raw adb serial.
    """
    r = result if isinstance(result, dict) else {}
    known_serial = serial.strip() if isinstance(serial, str) and serial.strip() else ""
    if not known_serial and isinstance(r.get("serial"), str):
        known_serial = r["serial"].strip()
    wall = wallpaper.strip() if isinstance(wallpaper, str) else ""
    if not wall:
        for key in ("wallpaper", "wallpaper_no"):
            if isinstance(r.get(key), str) and r.get(key).strip():
                wall = r[key].strip()
                break
    out: Dict[str, Any] = {}
    for k in _RESULT_INTS:
        v = r.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 9:
            out[k] = v
    for k in _RESULT_STRS:
        v = r.get(k)
        if isinstance(v, str) and v:
            clipped = _clip(v, 160)
            if k in ("error", "stderr"):
                clipped = scrub_phone_error_text(clipped, serial=known_serial, wallpaper=wall)
            out[k] = clipped
    if kind == TASK_PHONE_SCREENSHOT:
        png = r.get("png_b64")
        if (isinstance(png, str) and 0 < len(png) <= MAX_PNG_B64 and png.startswith(_PNG_B64_PREFIX)
                and _B64_RE.match(png)):
            out["png_b64"] = png
    if r.get("dry_run") is True:
        out["dry_run"] = True
        if r.get("space") == "permille":
            out["space"] = "permille"
        plan = _sanitize_plan(r.get("plan"))
        if plan is not None:
            out["plan"] = plan
    if r.get("like_probe") is True:
        out["like_probe"] = True
        diag = _sanitize_like_diag(r.get("like_diag"))
        if diag is not None:
            out["like_diag"] = scrub_phone_tree(diag, serial=known_serial, wallpaper=wall)
    if r.get("like_button_deprecated") is True:
        out["like_button_deprecated"] = True
    return out


def strip_png(result: Any) -> Dict[str, Any]:
    """列表接口不回截图本体，只标 has_png；要看图走 GET /api/fleet/tasks/{task_id}。"""
    if isinstance(result, dict) and result.get("png_b64"):
        out = dict(result)
        out["png_b64"] = ""
        out["has_png"] = True
        return out
    return result if isinstance(result, dict) else {}


__all__ = [
    "OPS", "MAX_COORD", "TEXT_MAX", "KEYCODES", "TEXT_ALLOWED", "MAX_PNG_B64", "PhoneOpError", "valid_serial",
    "kind_for_op", "validate_payload", "check_target", "escape_input_text", "sanitize_phone_result",
    "scrub_phone_error_text", "scrub_phone_tree", "strip_png",
]
