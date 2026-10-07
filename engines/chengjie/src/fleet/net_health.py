"""Read-only per-phone network health (agent 0.3.18).

The task ``net_health`` asks adb for reachability, transport, SIM and signal,
mobile byte counters, and whether Facebook is installed and on its login
screen. It does not change settings, toggle radios, reboot, or force-stop.

Android has no stable adb API for remaining carrier data or balance. That
field stays ``available: false`` with the note ``carrier-specific, not
available by default``. An optional USSD dial runs only when
``net_health_ussd_enabled`` is JSON true and that wallpaper has a ``*digits#``
code. It is labeled experimental and is off by default. The reply text is the
carrier's own message, not a parsed balance.

Every shell argv goes through ``check_adb_args``. A denied or missing command
sets that field to ``unavailable`` and the rest of the probe continues.
Results name phones by wallpaper number. Serials, SSIDs, raw dumps, and
package paths are not returned.

``foreground_facebook: true`` in the task payload is the only write this task
performs: ``am start`` of Facebook's launcher. It is off unless that flag is
JSON true, and it is skipped on a live-stream host.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .phones import is_protected
from .protocol import STATUS_DONE, STATUS_FAILED, STATUS_REJECTED

REMAINING_NOTE = "carrier-specific, not available by default"
UNAVAILABLE = "unavailable"
_MAX_PHONES = 32
_MAX_OUT = 2_000_000
_GSTATIC = "http://connectivitycheck.gstatic.com/generate_204"
_CLIENTS3 = "http://clients3.google.com/generate_204"
_FB = ("com.facebook.katana", "com.facebook.lite")
_CURL = ("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time", "8")
_STATUS_ZH = frozenset({
    "网络✓ wifi", "网络✗ 无流量", "移动数据弱信号", "网络✗ 飞行模式", "网络✓ 移动数据",
    "Facebook未安装", "Facebook未登录", "网络? wifi", "网络✓", "网络状态不可用",
})
_STATUS_EN = frozenset({
    "net ok wifi", "net down no data", "mobile weak signal", "net off airplane", "net ok mobile",
    "fb not installed", "fb logged out", "net unknown wifi", "net ok", "net unavailable",
})
_TRANSPORTS = frozenset({"wifi", "mobile", "none", UNAVAILABLE})
_SIM = frozenset({"present", "absent", "unknown", UNAVAILABLE})
_SIGNAL = frozenset({"none", "weak", "ok", "strong", UNAVAILABLE})
_FB_SCREEN = frozenset({"login", "app", "other", UNAVAILABLE})
_REACH_VIA = frozenset({"generate_204", "ping", "dns", "", UNAVAILABLE})
_ROW_KEYS = (
    "wallpaper_no", "unnumbered", "key", "reachable", "latency_ms", "reach_via", "dns_ok",
    "transport", "mobile_data", "sim", "signal", "signal_level", "airplane",
    "mobile_rx_bytes", "mobile_tx_bytes", "mobile_bytes", "usage",
    "fb_installed", "fb_screen", "screen", "status_zh", "status_en", "alert", "remaining_data",
)
_SECRET_KEYS = frozenset({
    "serial", "adb_serial", "device", "ssid", "raw", "dumpsys", "stdout", "stderr", "ip", "path",
})

Invoke = Callable[..., Any]


class _Out:
    __slots__ = ("text", "rc", "denied", "missing")

    def __init__(self, text: str = "", rc: int = 0, denied: bool = False, missing: bool = False) -> None:
        self.text = text
        self.rc = rc
        self.denied = denied
        self.missing = missing


def _binary_missing(blob: str, rc: int) -> bool:
    if rc == 0:
        return False
    low = (blob or "").lower()
    return rc == 127 or "not found" in low or "inaccessible or not found" in low


def _as_out(value: Any) -> _Out:
    if isinstance(value, _Out):
        return value
    if value is None:
        return _Out(denied=True, rc=1)
    if isinstance(value, tuple):
        text = str(value[0] if value else "")
        rc = int(value[1]) if len(value) > 1 else 0
        extra = str(value[2]) if len(value) > 2 else ""
        return _Out(text=text[:_MAX_OUT], rc=rc, missing=_binary_missing(text + "\n" + extra, rc))
    return _Out(text=str(value)[:_MAX_OUT], rc=0)


def _call(invoke: Invoke, args: Tuple[str, ...], timeout: int = 12) -> _Out:
    try:
        try:
            raw = invoke(args, timeout=timeout)
        except TypeError:
            raw = invoke(args)
    except Exception:
        return _Out(denied=True, rc=1)
    return _as_out(raw)


def _failed(out: _Out) -> bool:
    return out.denied or out.missing or out.rc != 0


# ── parsers (pure) ──────────────────────────────────────────────────────────
def parse_transport(text: str) -> str:
    """``wifi`` / ``mobile`` / ``none`` from a connectivity dump."""
    active = re.search(r"Active default network:\s*(\S+)", text or "", re.I)
    if active and active.group(1).lower() in {"none", "null"}:
        return "none"
    chunk = text or ""
    if active and active.group(1).isdigit():
        net_id = active.group(1)
        idx = chunk.find("network{" + net_id + "}")
        if idx < 0:
            idx = chunk.find("NetworkAgentInfo")
        if idx >= 0:
            chunk = chunk[idx:idx + 5000]
    return _transport_in(chunk) or ("none" if active else "none")


def _transport_in(chunk: str) -> str:
    match = re.search(r"Transports:\s*([A-Z0-9_|]+)", chunk)
    if match:
        kinds = set(match.group(1).split("|"))
        if "WIFI" in kinds:
            return "wifi"
        if "CELLULAR" in kinds:
            return "mobile"
    if re.search(r"\btype:\s*WIFI\b", chunk):
        return "wifi"
    if re.search(r"\btype:\s*MOBILE\b", chunk):
        return "mobile"
    return ""


def parse_sim(text: str) -> str:
    states = [item.upper() for item in re.findall(r"\bmSimState\s*=\s*([A-Za-z0-9_]+)", text or "")]
    if any(item in {"READY", "LOADED"} or item.startswith("READY") for item in states):
        return "present"
    if any("ABSENT" in item or item in {"CARD_ABSENT", "NOT_READY"} for item in states):
        return "absent"
    if re.search(r"SIM_STATE_READY", text or ""):
        return "present"
    if re.search(r"SIM_STATE_ABSENT|CARD_ABSENT", text or ""):
        return "absent"
    return "unknown"


def parse_sim_prop(text: str) -> str:
    val = (text or "").strip().upper()
    if not val or val in {"NULL", "UNKNOWN"}:
        return "unknown"
    if "READY" in val or "LOADED" in val:
        return "present"
    if "ABSENT" in val:
        return "absent"
    return "unknown"


def parse_airplane_dump(text: str) -> Optional[bool]:
    match = re.search(r"mAirplaneMode(?:On)?\s*=\s*(true|false)", text or "", re.I)
    if not match:
        return None
    return match.group(1).lower() == "true"


def parse_setting_bool(text: str) -> Optional[bool]:
    token = (text or "").strip().lower()
    if token in {"1", "true"}:
        return True
    if token in {"0", "false"}:
        return False
    return None


def _bucket_level(level: int) -> str:
    if level <= 0:
        return "none"
    if level == 1:
        return "weak"
    if level == 2:
        return "ok"
    return "strong"


def parse_signal(text: str) -> Tuple[Optional[str], Optional[int]]:
    levels: List[int] = []
    for match in re.finditer(r"CellSignalStrength\w*[^\n]*", text or ""):
        found = re.search(r"\blevel=(\d+)", match.group(0))
        if found:
            levels.append(int(found.group(1)))
    if levels:
        level = max(levels)
        return _bucket_level(min(level, 4)), level
    asu = [int(item) for item in re.findall(r"gsmSignalStrength[=:](\d+)", text or "") if int(item) != 99]
    if not asu:
        if re.search(r"gsmSignalStrength[=:]99\b", text or ""):
            return "none", None
        return None, None
    best = max(asu)
    if best <= 6:
        return "weak", best
    if best <= 14:
        return "ok", best
    return "strong", best


def parse_netstats(text: str) -> Optional[Tuple[int, int, int]]:
    """Mobile rx, tx, total. Wi-Fi counters are ignored. Ident blocks win over iface lines."""
    ident_rx = ident_tx = 0
    iface_rx = iface_tx = 0
    ident_found = iface_found = False
    mobile = False
    iface_mobile = False
    for line in (text or "").splitlines():
        if "ident=" in line:
            mobile = "MOBILE" in line and "WIFI" not in line.split("MOBILE")[0]
            if "type=WIFI" in line and "MOBILE" not in line:
                mobile = False
            iface_mobile = False
        elif line.strip().startswith("iface=") or re.match(r"\s*iface=", line):
            mobile = False
            iface_mobile = bool(re.search(r"\b(rmnet|ccmni|pdp|wwan)\d*", line, re.I))
        if mobile:
            rxs = [int(item) for item in re.findall(r"\brb=(\d+)", line)]
            txs = [int(item) for item in re.findall(r"\btb=(\d+)", line)]
            if rxs or txs:
                ident_found = True
                ident_rx += sum(rxs)
                ident_tx += sum(txs)
        if iface_mobile:
            rxs = [int(item) for item in re.findall(r"rxBytes[=:](\d+)", line)]
            txs = [int(item) for item in re.findall(r"txBytes[=:](\d+)", line)]
            if rxs or txs:
                iface_found = True
                iface_rx += sum(rxs)
                iface_tx += sum(txs)
    if ident_found:
        return ident_rx, ident_tx, ident_rx + ident_tx
    if iface_found:
        return iface_rx, iface_tx, iface_rx + iface_tx
    return None


def parse_curl(text: str) -> Tuple[bool, Optional[int]]:
    match = re.search(r"\b(\d{3})\s+([0-9]+(?:\.[0-9]+)?)", text or "")
    if not match or int(match.group(1)) != 204:
        return False, None
    return True, int(round(float(match.group(2)) * 1000))


def parse_ping(text: str) -> Tuple[bool, Optional[int]]:
    loss = re.search(r"(\d+)% packet loss", text or "")
    if loss and int(loss.group(1)) >= 100:
        return False, None
    received = re.search(r"(\d+)\s+(?:packets?\s+)?received", text or "", re.I)
    ok = bool(received and int(received.group(1)) >= 1) or bool(loss and int(loss.group(1)) == 0)
    if not ok:
        return False, None
    timing = re.search(r"time[=<]\s*([0-9.]+)\s*ms", text or "")
    if not timing:
        return True, None
    return True, int(round(float(timing.group(1))))


def parse_dns_ok(text: str, ping_ok: bool) -> Optional[bool]:
    low = (text or "").lower()
    if any(part in low for part in ("unknown host", "bad address", "not known", "no address associated",
                                    "temporary failure")):
        return False
    if ping_ok or re.search(r"\d+\.\d+\.\d+\.\d+", text or ""):
        return True
    return False


def parse_foreground(text: str) -> Tuple[str, bool]:
    """``login`` / ``app`` / ``other`` and whether a Facebook package is resumed."""
    lines = []
    for line in (text or "").splitlines():
        if re.search(r"mResumedActivity|topResumedActivity|ResumedActivity:|mCurrentFocus|mFocusedApp", line):
            lines.append(line)
    preferred = [line for line in lines if re.search(r"mResumedActivity|topResumedActivity|mCurrentFocus", line)]
    chosen = (preferred or lines)
    if not chosen:
        return "other", False
    line = chosen[-1]
    if not any(pkg in line for pkg in _FB):
        return "other", False
    if re.search(r"login|logged.?out|logout", line, re.I):
        return "login", True
    return "app", True


def parse_package_present(text: str, package: str) -> bool:
    blob = text or ""
    return ("package:" + package) in blob or ("package:" in blob and package in blob and "/" in blob)


def parse_ip_transport(text: str) -> str:
    wifi = mobile = False
    current = ""
    has_inet = False
    up = False

    def flush() -> None:
        nonlocal wifi, mobile, has_inet, up
        if current and (has_inet or up):
            if current.startswith("wlan") or current.startswith("wifi"):
                wifi = True
            if re.match(r"(rmnet|ccmni|pdp|wwan)", current):
                mobile = True
        has_inet = False
        up = False

    for line in (text or "").splitlines():
        match = re.match(r"\d+:\s+(\S+):", line) or re.match(r"^([A-Za-z][\w.]*)\s+Link", line)
        if match:
            flush()
            current = match.group(1).split("@")[0]
            up = "UP" in line.split(":")[0] or ",UP" in line or "<UP" in line or "UP," in line
            continue
        if "inet " in line:
            has_inet = True
    flush()
    if wifi:
        return "wifi"
    if mobile:
        return "mobile"
    return "none"


def parse_wm_size(text: str) -> Optional[str]:
    match = re.search(r"Physical size:\s*(\d{2,5})x(\d{2,5})", text or "")
    if not match:
        match = re.search(r"Override size:\s*(\d{2,5})x(\d{2,5})", text or "")
    if not match:
        return None
    return match.group(1) + "x" + match.group(2)


def extract_ussd(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if re.search(r"USSD|MMI", line, re.I)]
    blob = re.sub(r"\s+", " ", " ".join(lines))
    blob = re.sub(r"[A-Za-z0-9]{8,}", "", blob)
    return blob[:160].strip()


def compact_status(measured: Dict[str, Any]) -> Tuple[str, str, bool]:
    airplane = measured.get("airplane") is True
    reachable = measured.get("reachable")
    transport = measured.get("transport") or UNAVAILABLE
    signal = measured.get("signal")
    installed = measured.get("fb_installed")
    screen = measured.get("fb_screen")
    if airplane:
        return "网络✗ 飞行模式", "net off airplane", True
    if reachable is False or (transport == "none" and reachable is not True):
        return "网络✗ 无流量", "net down no data", True
    if reachable is True and installed is False:
        return "Facebook未安装", "fb not installed", True
    if reachable is True and screen == "login":
        return "Facebook未登录", "fb logged out", True
    if reachable is True and transport == "wifi":
        return "网络✓ wifi", "net ok wifi", False
    if reachable is True and transport == "mobile" and signal in ("none", "weak"):
        return "移动数据弱信号", "mobile weak signal", True
    if reachable is True and transport == "mobile":
        return "网络✓ 移动数据", "net ok mobile", False
    if transport == "wifi" and reachable == UNAVAILABLE:
        return "网络? wifi", "net unknown wifi", False
    if reachable is True:
        return "网络✓", "net ok", False
    return "网络状态不可用", "net unavailable", False


def _remaining(dialed: bool, denied: bool, text: str) -> Dict[str, Any]:
    if not dialed and not denied:
        return {"available": False, "experimental": False, "note": REMAINING_NOTE}
    out: Dict[str, Any] = {
        "available": False,
        "experimental": True,
        "note": REMAINING_NOTE if denied else "experimental carrier USSD; not a stable balance API",
    }
    if denied:
        out["ussd"] = UNAVAILABLE
        return out
    out["source"] = "ussd"
    if text:
        out["text"] = text[:160]
    return out


def _reach(invoke: Invoke) -> Tuple[Any, Optional[int], str, Optional[bool]]:
    measured = False
    for url in (_GSTATIC, _CLIENTS3):
        out = _call(invoke, _CURL + (url,), timeout=12)
        if out.denied or out.missing:
            continue
        measured = True
        ok, latency = parse_curl(out.text)
        if ok:
            return True, latency, "generate_204", None
    ping = _call(invoke, ("ping", "-c", "1", "-W", "3", "8.8.8.8"), timeout=8)
    if not ping.denied and not ping.missing:
        measured = True
        ok, latency = parse_ping(ping.text)
        if ok:
            return True, latency, "ping", None
    dns = _call(invoke, ("ping", "-c", "1", "-W", "3", "connectivitycheck.gstatic.com"), timeout=8)
    if dns.denied or dns.missing:
        if measured:
            return False, None, "", None
        return UNAVAILABLE, None, UNAVAILABLE, None
    ok, latency = parse_ping(dns.text)
    dns_ok = parse_dns_ok(dns.text, ok)
    if ok:
        return True, latency, "dns", dns_ok
    return False, None, "", dns_ok


def _tri(out: _Out, parse: Callable[[str], Any], empty_ok: bool = False) -> Any:
    if out.denied or out.missing or (out.rc != 0 and not empty_ok):
        return UNAVAILABLE
    return parse(out.text)


def probe_phone(invoke: Invoke, *, ussd_code: str = "", ussd_enabled: bool = False,
                foreground: bool = False) -> Dict[str, Any]:
    """Run the read-only checks. ``invoke`` returns text or ``_Out``.

    A denied command becomes ``unavailable`` for that field. The function
    does not raise for a denied command.
    """
    conn = _call(invoke, ("dumpsys", "connectivity"), timeout=15)
    transport: Any = UNAVAILABLE if _failed(conn) else (parse_transport(conn.text) or "none")
    tele = _call(invoke, ("dumpsys", "telephony.registry"), timeout=15)
    if _failed(tele):
        sim, signal, level, air_dump = UNAVAILABLE, UNAVAILABLE, None, None
    else:
        sim = parse_sim(tele.text)
        signal, level = parse_signal(tele.text)
        if signal is None:
            signal = None
        air_dump = parse_airplane_dump(tele.text)
    if sim == UNAVAILABLE:
        prop = _call(invoke, ("getprop", "gsm.sim.state"), timeout=8)
        if not _failed(prop):
            sim = parse_sim_prop(prop.text)
    mobile_out = _call(invoke, ("settings", "get", "global", "mobile_data"), timeout=8)
    mobile_data: Any = UNAVAILABLE if _failed(mobile_out) else parse_setting_bool(mobile_out.text)
    air_out = _call(invoke, ("settings", "get", "global", "airplane_mode_on"), timeout=8)
    if _failed(air_out):
        airplane: Any = UNAVAILABLE if air_dump is None else air_dump
    else:
        parsed = parse_setting_bool(air_out.text)
        airplane = air_dump if parsed is None else parsed
    if transport == UNAVAILABLE:
        ip = _call(invoke, ("ip", "-4", "addr"), timeout=8)
        if _failed(ip):
            ip = _call(invoke, ("ifconfig",), timeout=8)
        if not _failed(ip):
            transport = parse_ip_transport(ip.text)
    stats = _call(invoke, ("dumpsys", "netstats"), timeout=15)
    usage_tuple = None if _failed(stats) else parse_netstats(stats.text)
    if usage_tuple is None and _failed(stats):
        usage = UNAVAILABLE
        rx = tx = total = None
    elif usage_tuple is None:
        usage = "ok"
        rx = tx = total = None
    else:
        usage = "ok"
        rx, tx, total = usage_tuple
    reachable, latency, via, dns_ok = _reach(invoke)
    installed, found_pkg = _facebook_installed(invoke)
    activity = _call(invoke, ("dumpsys", "activity", "activities"), timeout=15)
    if _failed(activity):
        activity = _call(invoke, ("dumpsys", "activity", "top"), timeout=12)
    if _failed(activity):
        activity = _call(invoke, ("dumpsys", "window", "windows"), timeout=12)
    if _failed(activity):
        fb_screen: Any = UNAVAILABLE
    else:
        fb_screen, _front = parse_foreground(activity.text)
    if foreground and installed is True and found_pkg:
        launch = _call(invoke, _launch_args(found_pkg), timeout=12)
        if not launch.denied and not launch.missing:
            again = _call(invoke, ("dumpsys", "activity", "activities"), timeout=15)
            if not _failed(again):
                fb_screen, _front = parse_foreground(again.text)
    size_out = _call(invoke, ("wm", "size"), timeout=8)
    if _failed(size_out):
        screen: Any = UNAVAILABLE
    else:
        screen = parse_wm_size(size_out.text)
    dialed = False
    ussd_denied = False
    ussd_text = ""
    if ussd_enabled and ussd_code:
        dial = _ussd_args(ussd_code)
        if dial is None:
            ussd_denied = False
        else:
            sent = _call(invoke, dial, timeout=15)
            if sent.denied:
                ussd_denied = True
            else:
                dialed = True
                note = _call(invoke, ("dumpsys", "notification"), timeout=12)
                if not _failed(note):
                    ussd_text = extract_ussd(note.text)
    measured = {
        "reachable": reachable,
        "latency_ms": latency,
        "reach_via": via if via in _REACH_VIA else "",
        "dns_ok": dns_ok if isinstance(dns_ok, bool) or dns_ok is None else None,
        "transport": transport if transport in _TRANSPORTS else UNAVAILABLE,
        "mobile_data": mobile_data if isinstance(mobile_data, bool) or mobile_data is None or mobile_data == UNAVAILABLE else UNAVAILABLE,
        "sim": sim if sim in _SIM else "unknown",
        "signal": signal if signal in _SIGNAL or signal is None else None,
        "signal_level": level if isinstance(level, int) else None,
        "airplane": airplane if isinstance(airplane, bool) or airplane is None or airplane == UNAVAILABLE else UNAVAILABLE,
        "mobile_rx_bytes": rx,
        "mobile_tx_bytes": tx,
        "mobile_bytes": total,
        "usage": usage,
        "fb_installed": installed,
        "fb_screen": fb_screen if fb_screen in _FB_SCREEN else UNAVAILABLE,
        "screen": screen if isinstance(screen, str) or screen == UNAVAILABLE else None,
        "remaining_data": _remaining(dialed, ussd_denied, ussd_text),
    }
    zh, en, alert = compact_status(measured)
    measured["status_zh"] = zh
    measured["status_en"] = en
    measured["alert"] = alert
    return measured


def _launch_args(package: str) -> Tuple[str, ...]:
    from .adb_allowlist import facebook_launch_args

    return facebook_launch_args(package)


def _ussd_args(code: str) -> Optional[Tuple[str, ...]]:
    if not re.fullmatch(r"\*[0-9]{1,12}#", code or ""):
        return None
    return ("am", "start", "-a", "android.intent.action.CALL", "-d", "tel:*" + code[1:-1] + "%23")


def _facebook_installed(invoke: Invoke) -> Tuple[Any, str]:
    saw = False
    for package in _FB:
        out = _call(invoke, ("pm", "path", package), timeout=8)
        if out.denied or out.missing:
            out = _call(invoke, ("cmd", "package", "list", "packages", package), timeout=8)
        if out.denied or out.missing:
            continue
        saw = True
        if parse_package_present(out.text, package) or (out.rc == 0 and ("package:" + package) in out.text):
            return True, package
        if out.rc == 0 and "package:" in out.text and package in out.text:
            return True, package
    if not saw:
        return UNAVAILABLE, ""
    return False, ""


def _wall_number(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[0-9]{1,6}", text) else ""


def _safe_key(row: Dict[str, Any], fallback: str) -> str:
    key = str(row.get("key") or "")
    if re.fullmatch(r"[0-9]{1,6}(#\d+)?", key) or re.fullmatch(r"unnumbered:\d+(#\d+)?", key):
        return key
    return fallback


def popup_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Fields the on-device window may show. No serials."""
    number = _wall_number(row.get("wallpaper_no"))
    key = _safe_key(row, number or "unnumbered:0")
    measured = {
        "airplane": row.get("airplane"),
        "reachable": row.get("reachable"),
        "transport": row.get("transport"),
        "signal": row.get("signal"),
        "fb_installed": row.get("fb_installed"),
        "fb_screen": row.get("fb_screen"),
    }
    zh, en, alert = compact_status(measured)
    if row.get("status_zh") in _STATUS_ZH:
        zh = str(row["status_zh"])
    if row.get("status_en") in _STATUS_EN:
        en = str(row["status_en"])
    if isinstance(row.get("alert"), bool):
        alert = row["alert"]
    reachable = row.get("reachable")
    if not isinstance(reachable, bool) and reachable != UNAVAILABLE:
        reachable = UNAVAILABLE
    transport = row.get("transport") if row.get("transport") in _TRANSPORTS else UNAVAILABLE
    return {
        "key": key,
        "wallpaper_no": number,
        "unnumbered": bool(row.get("unnumbered")) or not number,
        "status_zh": zh,
        "status_en": en,
        "reachable": reachable,
        "transport": transport,
        "alert": bool(alert),
    }


def _clip_int(value: Any, lo: int, hi: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if lo <= value <= hi:
        return value
    return None


def _clean_remaining(value: Any) -> Dict[str, Any]:
    src = value if isinstance(value, dict) else {}
    out: Dict[str, Any] = {
        "available": False,
        "experimental": src.get("experimental") is True,
        "note": REMAINING_NOTE,
    }
    if src.get("experimental") is True and src.get("source") == "ussd":
        out["source"] = "ussd"
        out["note"] = "experimental carrier USSD; not a stable balance API"
    if src.get("ussd") == UNAVAILABLE:
        out["ussd"] = UNAVAILABLE
        out["experimental"] = True
    text = src.get("text")
    if isinstance(text, str) and text.strip():
        cleaned = re.sub(r"[A-Za-z0-9]{8,}", "", text)
        cleaned = cleaned.strip()[:160]
        if cleaned:
            out["text"] = cleaned
    return out


def public_row(row: Dict[str, Any], *, wallpaper_no: str = "", key: str = "",
               unnumbered: Optional[bool] = None) -> Dict[str, Any]:
    number = _wall_number(wallpaper_no or row.get("wallpaper_no"))
    if unnumbered is None:
        unnumbered = not number
    fallback = number or (key if re.fullmatch(r"unnumbered:\d+(#\d+)?", key or "") else "unnumbered:0")
    safe_key = _safe_key({"key": key or row.get("key"), "wallpaper_no": number}, fallback)
    reachable = row.get("reachable")
    if not isinstance(reachable, bool) and reachable != UNAVAILABLE:
        reachable = UNAVAILABLE
    signal = row.get("signal")
    if signal not in _SIGNAL and signal is not None:
        signal = None
    fb_installed = row.get("fb_installed")
    if not isinstance(fb_installed, bool) and fb_installed != UNAVAILABLE:
        fb_installed = UNAVAILABLE
    mobile_data = row.get("mobile_data")
    if not isinstance(mobile_data, bool) and mobile_data is not None and mobile_data != UNAVAILABLE:
        mobile_data = UNAVAILABLE
    airplane = row.get("airplane")
    if not isinstance(airplane, bool) and airplane is not None and airplane != UNAVAILABLE:
        airplane = UNAVAILABLE
    dns_ok = row.get("dns_ok")
    if not isinstance(dns_ok, bool):
        dns_ok = None
    screen = row.get("screen")
    if screen == UNAVAILABLE:
        screen_out: Any = UNAVAILABLE
    elif isinstance(screen, str) and re.fullmatch(r"\d{2,5}x\d{2,5}", screen):
        screen_out = screen
    else:
        screen_out = None
    out: Dict[str, Any] = {
        "wallpaper_no": number,
        "unnumbered": bool(unnumbered) or not number,
        "key": safe_key,
        "reachable": reachable,
        "latency_ms": _clip_int(row.get("latency_ms"), 0, 120000),
        "reach_via": row.get("reach_via") if row.get("reach_via") in _REACH_VIA else "",
        "dns_ok": dns_ok,
        "transport": row.get("transport") if row.get("transport") in _TRANSPORTS else UNAVAILABLE,
        "mobile_data": mobile_data,
        "sim": row.get("sim") if row.get("sim") in _SIM else "unknown",
        "signal": signal,
        "signal_level": _clip_int(row.get("signal_level"), 0, 99),
        "airplane": airplane,
        "mobile_rx_bytes": _clip_int(row.get("mobile_rx_bytes"), 0, 2**62),
        "mobile_tx_bytes": _clip_int(row.get("mobile_tx_bytes"), 0, 2**62),
        "mobile_bytes": _clip_int(row.get("mobile_bytes"), 0, 2**62),
        "usage": "ok" if row.get("usage") == "ok" else UNAVAILABLE,
        "fb_installed": fb_installed,
        "fb_screen": row.get("fb_screen") if row.get("fb_screen") in _FB_SCREEN else UNAVAILABLE,
        "screen": screen_out,
        "remaining_data": _clean_remaining(row.get("remaining_data")),
    }
    zh, en, alert = compact_status(out)
    if row.get("status_zh") in _STATUS_ZH:
        zh = str(row["status_zh"])
    if row.get("status_en") in _STATUS_EN:
        en = str(row["status_en"])
    if isinstance(row.get("alert"), bool):
        alert = row["alert"]
    out["status_zh"] = zh
    out["status_en"] = en
    out["alert"] = bool(alert)
    return {k: out[k] for k in _ROW_KEYS}


def _purge(value: Any, secrets: Sequence[str]) -> Any:
    folded = [item.upper() for item in secrets if item]
    if isinstance(value, str):
        up = value.upper()
        if any(secret and secret in up for secret in folded):
            return ""
        return value
    if isinstance(value, list):
        return [_purge(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _purge(item, secrets) for key, item in value.items() if key not in _SECRET_KEYS}
    return value


def sanitize_net_health_result(result: Any) -> Dict[str, Any]:
    """Controller-side gate. Drops serials and anything that is not a public field."""
    src = result if isinstance(result, dict) else {}
    phones = []
    for row in src.get("phones") or []:
        if isinstance(row, dict):
            phones.append(public_row(row))
    out: Dict[str, Any] = {
        "phones": phones,
        "remaining_data_note": REMAINING_NOTE,
        "ussd_enabled": src.get("ussd_enabled") is True,
    }
    if isinstance(src.get("error"), str) and src.get("error"):
        err = re.sub(r"[A-Za-z0-9][A-Za-z0-9:._-]{8,}", "", src["error"]).strip()[:80]
        out["error"] = err or "probe_error"
    return out


def _safe_error(exc: BaseException) -> str:
    text = str(exc or "").splitlines()[0].strip()[:80]
    if re.search(r"[A-Za-z0-9]{12,}", text):
        return "probe_error"
    return text or "probe_error"


def _ussd_for(codes: Any, number: str) -> str:
    if not isinstance(codes, dict) or not number:
        return ""
    raw = codes.get(number)
    if raw is None and number.isdigit():
        raw = codes.get(str(int(number)))
    if not isinstance(raw, str):
        return ""
    code = raw.strip()
    return code if re.fullmatch(r"\*[0-9]{1,12}#", code) else ""


def _blank_row(number: str, key: str, unnumbered: bool) -> Dict[str, Any]:
    measured = {
        "reachable": UNAVAILABLE, "transport": UNAVAILABLE, "sim": UNAVAILABLE,
        "signal": UNAVAILABLE, "airplane": UNAVAILABLE, "fb_installed": UNAVAILABLE,
        "fb_screen": UNAVAILABLE, "usage": UNAVAILABLE, "mobile_data": UNAVAILABLE,
        "remaining_data": _remaining(False, False, ""),
    }
    return public_row(measured, wallpaper_no=number, key=key, unnumbered=unnumbered)


def run_net_health(collector: Any, cfg_data: Any, target: Optional[Dict[str, Any]] = None,
                   payload: Optional[Dict[str, Any]] = None, *, live_stream: bool = False,
                   shell_for: Optional[Callable[[str], Invoke]] = None) -> Tuple[str, Dict[str, Any], str]:
    """Probe ``state=device`` phones. Never raises.

    ``shell_for(serial)`` supplies a test double. The real path uses
    ``prepare_adb`` with the collector's current ``manage_server`` flag and
    does not change it.
    """
    from .phone_ops import check_adb_args
    from .phone_rules import PhoneOpError

    cfg = cfg_data if isinstance(cfg_data, dict) else {}
    ussd_on = cfg.get("net_health_ussd_enabled") is True and not live_stream
    body: Dict[str, Any] = {
        "phones": [],
        "remaining_data_note": REMAINING_NOTE,
        "ussd_enabled": ussd_on,
    }
    try:
        phones, err = collector.collect()
    except Exception as exc:
        body["error"] = _safe_error(exc)
        return STATUS_FAILED, sanitize_net_health_result(body), body["error"]
    if err:
        body["error"] = _safe_error(RuntimeError(str(err)))
        return STATUS_FAILED, sanitize_net_health_result(body), body["error"]
    wanted = ""
    if isinstance(target, dict):
        wanted = str(target.get("serial") or "").strip()
    if wanted and is_protected(wanted):
        return STATUS_REJECTED, sanitize_net_health_result(body), "protected_phone"
    devices = [item for item in (phones or []) if isinstance(item, dict) and item.get("state") == "device"]
    if wanted:
        wanted_up = wanted.upper()
        devices = [item for item in devices if str(item.get("serial") or "").upper() == wanted_up]
        if not devices:
            present = [item for item in (phones or []) if isinstance(item, dict)
                       and str(item.get("serial") or "").upper() == wanted_up]
            detail = "phone_not_ready" if present else "device_not_found"
            return STATUS_FAILED, sanitize_net_health_result(body), detail
    devices = [item for item in devices if not is_protected(str(item.get("serial") or ""))][:_MAX_PHONES]
    from .operator_alert import parse_wallpaper_map

    wall = parse_wallpaper_map(cfg.get("wallpaper_map"))
    foreground = isinstance(payload, dict) and payload.get("foreground_facebook") is True and not live_stream
    adb = ""
    run = getattr(collector, "_run", None)
    if shell_for is None and devices:
        try:
            from .phones import prepare_adb

            adb, _server = prepare_adb(
                getattr(collector, "adb_path", ""),
                manage_server=bool(getattr(collector, "manage_server", False)),
                server_version=getattr(collector, "_server_version", None),
                locate=getattr(collector, "_locate", None),
                run=run,
                live_stream=getattr(collector, "_live_stream", None),
                state_dir=getattr(collector, "_state_dir", None),
            )
        except Exception as exc:
            body["error"] = _safe_error(exc)
            return STATUS_FAILED, sanitize_net_health_result(body), body["error"]
    rows: List[Dict[str, Any]] = []
    secrets: List[str] = []
    slot = 1
    used = set()
    for item in devices:
        serial = str(item.get("serial") or "")
        secrets.append(serial)
        number = wall.get(serial.upper(), "")
        if number:
            base = number
        else:
            base = f"unnumbered:{slot}"
            slot += 1
        key = base
        n = 2
        while key in used:
            key = f"{base}#{n}"
            n += 1
        used.add(key)
        code = _ussd_for(cfg.get("net_health_ussd_codes"), number) if ussd_on else ""
        allow_ussd = bool(ussd_on and code)
        try:
            invoke = _make_invoke(adb, serial, run, allow_ussd, shell_for, check_adb_args, PhoneOpError)
            measured = probe_phone(invoke, ussd_code=code, ussd_enabled=allow_ussd, foreground=foreground)
            row = public_row(measured, wallpaper_no=number, key=key, unnumbered=not number)
        except Exception:
            row = _blank_row(number, key, not number)
        rows.append(row)
    rows.sort(key=lambda row: (bool(row.get("unnumbered")), row.get("wallpaper_no") or "", row.get("key") or ""))
    body["phones"] = rows
    cleaned = sanitize_net_health_result(_purge(body, secrets))
    return STATUS_DONE, cleaned, "ok"


def _make_invoke(adb: str, serial: str, run: Any, allow_ussd: bool,
                 shell_for: Optional[Callable[[str], Invoke]], check_adb_args: Any,
                 phone_op_error: Any) -> Invoke:
    shell_fn = shell_for(serial) if shell_for is not None else None

    def invoke(args: Tuple[str, ...], timeout: int = 12) -> _Out:
        full = ("-s", serial, "shell", *tuple(args))
        try:
            check_adb_args(full, allow_experimental_ussd=allow_ussd)
        except phone_op_error:
            return _Out(denied=True, rc=1)
        if shell_fn is not None:
            try:
                try:
                    raw = shell_fn(args, timeout=timeout)  # type: ignore[call-arg]
                except TypeError:
                    raw = shell_fn(args)
            except Exception:
                return _Out(denied=True, rc=1)
            return _as_out(raw)
        return _exec_adb(adb, full, timeout, run)

    return invoke


def _exec_adb(adb: str, full: Tuple[str, ...], timeout: int, run: Any) -> _Out:
    cmd = [adb, *full]
    kw: Dict[str, Any] = {
        "capture_output": True, "timeout": timeout, "shell": False, "stdin": subprocess.DEVNULL,
    }
    if sys.platform.startswith("win"):
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = (run or subprocess.run)(cmd, **kw)
    except subprocess.TimeoutExpired:
        return _Out(rc=124, missing=True)
    except Exception:
        return _Out(denied=True, rc=1)
    out = _decode(getattr(proc, "stdout", b""))
    err = _decode(getattr(proc, "stderr", b""))
    rc = int(getattr(proc, "returncode", 1) or 0)
    return _Out(text=out[:_MAX_OUT], rc=rc, missing=_binary_missing(out + "\n" + err, rc))


def _decode(blob: Any) -> str:
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return str(blob or "")
