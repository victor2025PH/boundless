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
_RESULT_INTS = ("width", "height", "device_width", "device_height", "scale", "x", "y", "x1", "y1", "x2", "y2",
                "duration_ms", "bytes", "chars", "elapsed_ms")
_RESULT_STRS = ("serial", "key", "error", "stderr")


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


def sanitize_phone_result(kind: str, result: Any) -> Dict[str, Any]:
    """主控入库前：只留已知的数字 / 短字符串；截图只认 PNG 的 base64（控制台当 <img src> 用）。"""
    r = result if isinstance(result, dict) else {}
    out: Dict[str, Any] = {}
    for k in _RESULT_INTS:
        v = r.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 9:
            out[k] = v
    for k in _RESULT_STRS:
        v = r.get(k)
        if isinstance(v, str) and v:
            out[k] = _clip(v, 160)
    if kind == TASK_PHONE_SCREENSHOT:
        png = r.get("png_b64")
        if (isinstance(png, str) and 0 < len(png) <= MAX_PNG_B64 and png.startswith(_PNG_B64_PREFIX)
                and _B64_RE.match(png)):
            out["png_b64"] = png
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
    "kind_for_op", "validate_payload", "check_target", "escape_input_text", "sanitize_phone_result", "strip_png",
]
