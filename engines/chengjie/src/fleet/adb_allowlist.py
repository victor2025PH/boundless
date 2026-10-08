"""Categorized adb argv allowlist (agent 0.3.18).

One catalog, two doors:

* **Open** (admitted with no extra flag): ``phone_control`` (the 0.3.7
  inventory / screencap / input forms, plus a 0.3.21 media-pause key and
  ``cmd media_session dispatch pause``), ``read_only`` diagnostics (including
  ``uiautomator dump`` to stdout or to one fixed file, and ``adb pull`` of
  that file only), and ``app_launch`` (``am start`` that only brings
  Facebook to the foreground).
* **Closed unless the caller passes a flag**: ``guarded_write`` (settings
  changes, radio toggles, reboot, uninstall/clear, force-stop of any package
  that is not Facebook) needs ``allow_guarded_writes=True``.
  ``experimental_ussd`` (one USSD dial shape) needs
  ``allow_experimental_ussd=True``. ``app_restart`` (``am force-stop`` of
  ``com.facebook.katana`` or ``com.facebook.lite`` only) needs
  ``allow_app_restart=True``. No fleet task passes the guarded-write flag.
  ``phone_app_restart`` is the only task that passes the app-restart flag,
  and only for those two packages. ``net_health`` passes the USSD flag only
  when ``net_health_ussd_enabled`` is JSON true, and it never passes either
  write flag.

Everything else is denied, including when both flags are on. Arguments are
matched as tuples. Callers must use ``shell=False`` so a matched form cannot
be glued into a shell string.

``CATALOG`` is the audit list: every example is a concrete argv, and tests
check that ``classify_adb_args`` agrees with ``category`` / ``family``.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Sequence, Tuple

from .phone_rules import KEYCODES, TEXT_ALLOWED, PhoneOpError, valid_serial
from .phones import is_protected

Category = str  # phone_control | read_only | app_launch | experimental_ussd | guarded_write | denied

_OPEN = frozenset({"phone_control", "read_only", "app_launch"})
_NS = frozenset({"global", "system", "secure"})
_FB = frozenset({"com.facebook.katana", "com.facebook.lite"})
_DUMPSYS = frozenset({
    "connectivity", "telephony.registry", "netstats", "activity", "package", "window", "notification",
})
_LIST_FLAGS = frozenset({"-f", "-d", "-e", "-s", "-3", "-i", "-u", "-U", "-a"})
_IP_READ = frozenset({
    ("ip", "addr"), ("ip", "address"), ("ip", "route"), ("ip", "link"), ("ip", "rule"),
    ("ip", "-4", "addr"), ("ip", "-4", "address"), ("ip", "-4", "route"), ("ip", "-4", "link"),
    ("ip", "-6", "addr"), ("ip", "-6", "address"), ("ip", "-6", "route"),
    ("ifconfig",), ("ifconfig", "-a"),
})
_REBOOT = frozenset({("reboot",), ("reboot", "recovery"), ("reboot", "bootloader")})
_SVC = frozenset({
    ("svc", "wifi", "enable"), ("svc", "wifi", "disable"),
    ("svc", "data", "enable"), ("svc", "data", "disable"),
})
_GENERATE_204 = frozenset({
    "http://connectivitycheck.gstatic.com/generate_204",
    "http://clients3.google.com/generate_204",
    "http://connectivitycheck.android.com/generate_204",
    "http://www.google.com/generate_204",
})
_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_VAL = re.compile(r"[A-Za-z0-9_.:@+\-]{1,80}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_PROP = re.compile(r"[A-Za-z0-9_.:-]{1,96}\Z")
_PKG = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z0-9_]+)+\Z")
_PKG_FILTER = re.compile(r"[a-zA-Z0-9_.*]{1,128}\Z")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\Z")
_REL_CLASS = re.compile(r"\.[A-Za-z0-9_.]+\Z")
# One fixed remote. The default ``window_dump.xml`` and any other path stay denied.
HIERARCHY_REMOTE = "/sdcard/chatx_like_hierarchy.xml"
_PULL_LOCAL = re.compile(r"[A-Za-z0-9_.:/\\-]{1,240}\Z")


def _digits(v: str, hi: int = 99999) -> bool:
    return v.isdigit() and len(v) <= 5 and int(v) <= hi


def _pkg(token: str) -> bool:
    return bool(_PKG.fullmatch(token or ""))


def _host_ok(host: str) -> bool:
    if not host or host.startswith("-") or ".." in host or host.endswith("."):
        return False
    return bool(_HOST.fullmatch(host))


class AdbClass:
    """How one argv was classified. ``allowed`` is always False here.

    ``admit_adb_args`` turns open categories (and closed ones, when the
    matching flag is set) into a pass. Denied stays a pass-never.
    """

    __slots__ = ("category", "family")

    def __init__(self, category: Category, family: str) -> None:
        self.category = category
        self.family = family

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, AdbClass) and (self.category, self.family) == (other.category, other.family)

    def __repr__(self) -> str:
        return f"AdbClass({self.category!r}, {self.family!r})"


_DENIED = AdbClass("denied", "")


def _input_ok(shell: Tuple[str, ...]) -> bool:
    if len(shell) < 2 or shell[0] != "input":
        return False
    op, vals = shell[1], shell[2:]
    if op == "tap" and len(vals) == 2 and all(_digits(v) for v in vals):
        return True
    if op == "swipe" and len(vals) == 5 and all(_digits(v) for v in vals):
        return True
    if op == "keyevent" and len(vals) == 1 and vals[0] in KEYCODES.values():
        return True
    if op == "text" and len(vals) == 1:
        plain = vals[0].replace("%s", " ")
        if "%" not in plain and TEXT_ALLOWED.match(plain) and not plain.startswith("-"):
            return True
    return False


def _dumpsys_ok(args: Tuple[str, ...]) -> bool:
    if len(args) < 2 or args[0] != "dumpsys" or args[1] not in _DUMPSYS:
        return False
    if len(args) == 2:
        return True
    return len(args) == 3 and bool(_TOKEN.fullmatch(args[2])) and not args[2].startswith("-")


def _settings_get(args: Tuple[str, ...]) -> bool:
    return (len(args) == 4 and args[0] == "settings" and args[1] == "get"
            and args[2] in _NS and bool(_KEY.fullmatch(args[3])))


def _portrait_lock(args: Tuple[str, ...]) -> bool:
    """Lock the phone to portrait. Any other settings write stays closed."""
    return args in {
        ("settings", "put", "system", "accelerometer_rotation", "0"),
        ("settings", "put", "system", "user_rotation", "0"),
    }


def portrait_lock_args() -> Tuple[Tuple[str, ...], ...]:
    """The only settings writes a flow may send to leave landscape."""
    return (
        ("settings", "put", "system", "accelerometer_rotation", "0"),
        ("settings", "put", "system", "user_rotation", "0"),
    )


def _settings_write(args: Tuple[str, ...]) -> bool:
    if len(args) == 4 and args[0] == "settings" and args[1] == "delete" and args[2] in _NS:
        return bool(_KEY.fullmatch(args[3]))
    if len(args) == 5 and args[0] == "settings" and args[1] == "put" and args[2] in _NS:
        return bool(_KEY.fullmatch(args[3]) and _VAL.fullmatch(args[4]))
    return False


def _list_packages(tokens: Tuple[str, ...]) -> bool:
    if len(tokens) < 2 or tokens[0] != "list" or tokens[1] != "packages":
        return False
    flags = 0
    filt = 0
    for token in tokens[2:]:
        if token.startswith("-"):
            if token not in _LIST_FLAGS or filt:
                return False
            flags += 1
        else:
            if filt or not _PKG_FILTER.fullmatch(token):
                return False
            filt += 1
    return flags <= 4


def _query_tail(args: Tuple[str, ...]) -> bool:
    if _list_packages(args):
        return True
    if len(args) == 2 and args[0] in ("path", "dump") and _pkg(args[1]):
        return True
    if len(args) == 3 and args[0] == "resolve-activity" and args[1] == "--brief" and _pkg(args[2]):
        return True
    launcher = ("resolve-activity", "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER")
    return len(args) == 6 and args[:5] == launcher and _pkg(args[5])


def _pm_query(args: Tuple[str, ...]) -> bool:
    return bool(args) and args[0] == "pm" and _query_tail(args[1:])


def _cmd_package_query(args: Tuple[str, ...]) -> bool:
    return len(args) >= 2 and args[0] == "cmd" and args[1] == "package" and _query_tail(args[2:])


def _pm_mutate(args: Tuple[str, ...]) -> bool:
    if len(args) < 2 or args[0] != "pm":
        return False
    op = args[1]
    if op == "uninstall":
        rest = args[2:]
        if len(rest) == 1 and _pkg(rest[0]):
            return True
        return len(rest) == 3 and rest[0] == "--user" and rest[1].isdigit() and _pkg(rest[2])
    if op in ("clear", "disable", "enable", "disable-user") and len(args) == 3:
        return _pkg(args[2])
    return False


def _cmd_package_write(args: Tuple[str, ...]) -> bool:
    if len(args) != 4 or args[0] != "cmd" or args[1] != "package":
        return False
    return args[2] in ("uninstall", "clear", "disable", "enable") and _pkg(args[3])


def _cmd_connectivity_read(args: Tuple[str, ...]) -> bool:
    if len(args) < 3 or args[0] != "cmd" or args[1] != "connectivity":
        return False
    if args[2:] == ("airplane-mode",):
        return True
    if args[2:] == ("get-network-watchlist",):
        return True
    if args[2] == "get-config" and len(args) in (3, 4):
        return len(args) == 3 or bool(_KEY.fullmatch(args[3]))
    return False


def _cmd_connectivity_write(args: Tuple[str, ...]) -> bool:
    return args in {
        ("cmd", "connectivity", "airplane-mode", "enable"),
        ("cmd", "connectivity", "airplane-mode", "disable"),
    }


def _cmd_activity_read(args: Tuple[str, ...]) -> bool:
    if len(args) < 3 or args[0] != "cmd" or args[1] != "activity":
        return False
    if args[2:] in (("get-config",), ("get-current-user",)):
        return True
    return len(args) == 4 and args[2] == "package-importance" and _pkg(args[3])


def _force_stop(args: Tuple[str, ...]) -> bool:
    if len(args) == 3 and args[0] == "am" and args[1] == "force-stop":
        return _pkg(args[2])
    return len(args) == 4 and args[:3] == ("cmd", "activity", "force-stop") and _pkg(args[3])


def _facebook_force_stop(args: Tuple[str, ...]) -> bool:
    """Force-stop of katana or lite only. Every other package stays guarded."""
    if len(args) == 3 and args[0] == "am" and args[1] == "force-stop":
        return args[2] in _FB
    return len(args) == 4 and args[:3] == ("cmd", "activity", "force-stop") and args[3] in _FB


def _component_ok(comp: str) -> bool:
    if comp.count("/") != 1:
        return False
    pkg, cls = comp.split("/", 1)
    if pkg not in _FB or not cls or ".." in cls:
        return False
    if cls.startswith("."):
        return bool(_REL_CLASS.fullmatch(cls))
    return cls.startswith(pkg + ".") and _pkg(cls)


def _am_start_launch(args: Tuple[str, ...]) -> bool:
    if len(args) < 4 or args[0] != "am" or args[1] != "start":
        return False
    rest = args[2:]
    if len(rest) == 2 and rest[0] == "-p" and rest[1] in _FB:
        return True
    if len(rest) == 2 and rest[0] == "-n" and _component_ok(rest[1]):
        return True
    return rest[:5] == ("-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-p") \
        and len(rest) == 6 and rest[5] in _FB


def _ussd(args: Tuple[str, ...]) -> bool:
    if args[:5] != ("am", "start", "-a", "android.intent.action.CALL", "-d") or len(args) != 6:
        return False
    return bool(re.fullmatch(r"tel:\*[0-9]{1,12}%23", args[5]))


def _ping_ok(args: Tuple[str, ...]) -> bool:
    if not args or args[0] != "ping":
        return False
    count: Optional[int] = None
    wait: Optional[int] = None
    host: Optional[str] = None
    i = 1
    while i < len(args):
        token = args[i]
        if token == "-c" and i + 1 < len(args) and args[i + 1].isdigit():
            count = int(args[i + 1])
            i += 2
            continue
        if token in ("-W", "-w") and i + 1 < len(args) and args[i + 1].isdigit():
            wait = int(args[i + 1])
            i += 2
            continue
        if token.startswith("-") or host is not None:
            return False
        host = token
        i += 1
    if count is None or not 1 <= count <= 4:
        return False
    if wait is not None and not 1 <= wait <= 10:
        return False
    return bool(host and _host_ok(host))


def _curl_ok(args: Tuple[str, ...]) -> bool:
    return (len(args) == 9 and args[0] == "curl"
            and args[1:7] == ("-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time")
            and args[7] == "8" and args[8] in _GENERATE_204)


def hierarchy_pull_local_ok(path: str) -> bool:
    """A pull destination with no spaces, no ``..``, and no shell metacharacters."""
    if not isinstance(path, str) or not path or len(path) > 240 or path.startswith("-"):
        return False
    if ".." in path:
        return False
    return bool(_PULL_LOCAL.fullmatch(path))


def _uiautomator_dump_ok(args: Tuple[str, ...]) -> bool:
    """Stdout or the one fixed file. ``--compressed`` only as the flag before the path."""
    if len(args) < 3 or args[0] != "uiautomator" or args[1] != "dump":
        return False
    body = args[2:]
    if body[:1] == ("--compressed",):
        body = body[1:]
    return len(body) == 1 and body[0] in ("/dev/tty", HIERARCHY_REMOTE)


def _hierarchy_pull_ok(rest: Tuple[str, ...]) -> bool:
    return (len(rest) == 3 and rest[0] == "pull" and rest[1] == HIERARCHY_REMOTE
            and hierarchy_pull_local_ok(rest[2]))


def _media_pause_ok(args: Tuple[str, ...]) -> bool:
    """Pause only. Play, play/pause, and the power key stay denied."""
    return args in {
        ("input", "keyevent", "127"),
        ("cmd", "media_session", "dispatch", "pause"),
    }


def _screen_wake(args: Tuple[str, ...]) -> bool:
    """KEYCODE_WAKEUP. The power key (26) stays denied because it toggles."""
    return args == ("input", "keyevent", "224")


def _getprop_ok(args: Tuple[str, ...]) -> bool:
    if args == ("getprop",):
        return True
    return len(args) == 2 and args[0] == "getprop" and bool(_PROP.fullmatch(args[1]))


def _classify_shell(args: Tuple[str, ...]) -> AdbClass:
    """Classify the argv that follows ``adb -s SERIAL shell``."""
    if not args:
        return _DENIED
    if _portrait_lock(args):
        return AdbClass("phone_control", "portrait_lock")
    if _settings_write(args):
        return AdbClass("guarded_write", "settings_put")
    if args in _SVC:
        return AdbClass("guarded_write", "svc_radio")
    if _pm_mutate(args):
        return AdbClass("guarded_write", "pm_mutate")
    if _cmd_package_write(args):
        return AdbClass("guarded_write", "cmd_package_mutate")
    if _facebook_force_stop(args):
        return AdbClass("app_restart", "facebook_force_stop")
    if _force_stop(args):
        return AdbClass("guarded_write", "force_stop")
    if args in _REBOOT:
        return AdbClass("guarded_write", "reboot")
    if _cmd_connectivity_write(args):
        return AdbClass("guarded_write", "airplane_mode")
    if _ussd(args):
        return AdbClass("experimental_ussd", "ussd_dial")
    if _am_start_launch(args):
        return AdbClass("app_launch", "am_start_facebook")
    if _dumpsys_ok(args):
        return AdbClass("read_only", "dumpsys")
    if _settings_get(args):
        return AdbClass("read_only", "settings_get")
    if _ping_ok(args):
        return AdbClass("read_only", "ping")
    if _curl_ok(args):
        return AdbClass("read_only", "connectivity_204")
    if _getprop_ok(args):
        return AdbClass("read_only", "getprop")
    if args in _IP_READ:
        return AdbClass("read_only", "ifconfig" if args[0] == "ifconfig" else "ip")
    if args in {("wm", "size"), ("wm", "density")}:
        return AdbClass("read_only", "wm")
    if _cmd_connectivity_read(args):
        return AdbClass("read_only", "cmd_connectivity")
    if _cmd_activity_read(args):
        return AdbClass("read_only", "cmd_activity")
    if _pm_query(args):
        return AdbClass("read_only", "pm_query")
    if _cmd_package_query(args):
        return AdbClass("read_only", "cmd_package")
    # Hierarchy: stdout, or one fixed file, optionally --compressed before the path.
    # A bare dump, window_dump.xml, any other path, or the flag after the path stay denied.
    if _screen_wake(args):
        return AdbClass("phone_control", "screen_wake")
    if _media_pause_ok(args):
        return AdbClass("phone_control", "media_pause")
    if _uiautomator_dump_ok(args):
        return AdbClass("read_only", "uiautomator")
    return _DENIED


def classify_adb_args(args: Sequence[str]) -> AdbClass:
    """Return the category and family. Denied forms use category ``denied``.

    A protected serial is denied before the rest of the argv is considered,
    so a read-only dumpsys against that phone is still denied.
    """
    a = tuple(str(x) for x in args)
    if a == ("devices", "-l"):
        return AdbClass("phone_control", "devices")
    if a == ("version",):
        return AdbClass("phone_control", "version")
    if a in _REBOOT:
        return AdbClass("guarded_write", "reboot")
    if len(a) >= 3 and a[0] == "-s":
        if valid_serial(a[1]) != a[1] or is_protected(a[1]):
            return AdbClass("denied", "protected" if is_protected(a[1]) else "serial")
        rest = a[2:]
        if rest == ("exec-out", "screencap"):
            return AdbClass("phone_control", "screencap")
        if rest in _REBOOT:
            return AdbClass("guarded_write", "reboot")
        if rest[:1] == ("shell",):
            shell = rest[1:]
            if _input_ok(shell):
                return AdbClass("phone_control", "input")
            return _classify_shell(shell)
        if _hierarchy_pull_ok(rest):
            return AdbClass("read_only", "uiautomator")
    return _DENIED


def admit_adb_args(args: Sequence[str], *, allow_guarded_writes: bool = False,
                   allow_experimental_ussd: bool = False,
                   allow_app_restart: bool = False) -> AdbClass:
    """Raise ``PhoneOpError('adb_args_not_allowed')`` unless ``args`` may run.

    Guarded writes, the USSD dial, and Facebook force-stop stay closed unless
    the matching flag is true. ``allow_app_restart`` does not open
    ``guarded_write``. ``allow_guarded_writes`` does not open Facebook
    force-stop. Flags do not admit forms that are not in the catalog.
    """
    found = classify_adb_args(args)
    if found.category in _OPEN:
        return found
    if found.category == "guarded_write" and allow_guarded_writes is True:
        return found
    if found.category == "experimental_ussd" and allow_experimental_ussd is True:
        return found
    if found.category == "app_restart" and allow_app_restart is True:
        return found
    raise PhoneOpError("adb_args_not_allowed")


def facebook_launch_args(package: str) -> Tuple[str, ...]:
    """Exact ``am start`` argv that foregrounds one Facebook package."""
    if package not in _FB:
        raise PhoneOpError("adb_args_not_allowed")
    return ("am", "start", "-a", "android.intent.action.MAIN",
            "-c", "android.intent.category.LAUNCHER", "-p", package)


def facebook_force_stop_args(package: str) -> Tuple[str, ...]:
    """Exact ``am force-stop`` argv for katana or lite. No other package."""
    if package not in _FB:
        raise PhoneOpError("adb_args_not_allowed")
    return ("am", "force-stop", package)


# Concrete argv examples. Tests require classify_adb_args(example) to match.
# ``note`` is the audit sentence: what the form does and why it is in that door.
CATALOG: Tuple[Dict[str, Any], ...] = (
    {"since": "0.3.7", "category": "phone_control", "family": "devices",
     "example": ("devices", "-l"),
     "note": "Lists devices. Pre-existing inventory command."},
    {"since": "0.3.7", "category": "phone_control", "family": "version",
     "example": ("version",),
     "note": "Prints the adb client version. Pre-existing."},
    {"since": "0.3.7", "category": "phone_control", "family": "screencap",
     "example": ("-s", "S1", "exec-out", "screencap"),
     "note": "Reads one raw framebuffer. Pre-existing. No -p, no other exec-out."},
    {"since": "0.3.7", "category": "phone_control", "family": "input",
     "example": ("-s", "S1", "shell", "input", "tap", "10", "20"),
     "note": "Tap. Pre-existing phone control, not a diagnostic."},
    {"since": "0.3.7", "category": "phone_control", "family": "input",
     "example": ("-s", "S1", "shell", "input", "keyevent", "3"),
     "note": "Home key only (3) or back (4). Pre-existing."},
    {"since": "0.3.18", "category": "read_only", "family": "uiautomator",
     "example": ("-s", "S1", "shell", "uiautomator", "dump", "/dev/tty"),
     "note": "Reads the window hierarchy to stdout for icon-only Like labels. This form does not write a file. An arbitrary path, including /sdcard/window_dump.xml, stays denied."},
    {"since": "0.3.21", "category": "read_only", "family": "uiautomator",
     "example": ("-s", "S1", "shell", "uiautomator", "dump", "--compressed", "/dev/tty"),
     "note": "Same stdout dump with --compressed before the path. The flag after the path stays denied."},
    {"since": "0.3.21", "category": "read_only", "family": "uiautomator",
     "example": ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/chatx_like_hierarchy.xml"),
     "note": "Writes the hierarchy to one fixed file so a later pull can read it when stdout never goes idle. No other remote path matches."},
    {"since": "0.3.21", "category": "read_only", "family": "uiautomator",
     "example": ("-s", "S1", "shell", "uiautomator", "dump", "--compressed", "/sdcard/chatx_like_hierarchy.xml"),
     "note": "Compressed dump to the same fixed file. --compressed must come before the path."},
    {"since": "0.3.21", "category": "read_only", "family": "uiautomator",
     "example": ("-s", "S1", "pull", "/sdcard/chatx_like_hierarchy.xml", "/tmp/chatx_like_x.xml"),
     "note": "Pulls only that fixed file to a local path with no spaces and no parent-directory segment. Other remotes stay denied."},
    {"since": "0.3.21", "category": "phone_control", "family": "media_pause",
     "example": ("-s", "S1", "shell", "input", "keyevent", "127"),
     "note": "KEYCODE_MEDIA_PAUSE. Used once to settle an autoplaying feed before a hierarchy dump. Not a phone_key. Play and play/pause stay denied."},
    {"since": "0.3.21", "category": "phone_control", "family": "media_pause",
     "example": ("-s", "S1", "shell", "cmd", "media_session", "dispatch", "pause"),
     "note": "Pauses the active media session. dispatch play does not match."},

    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "connectivity"),
     "note": "Reads ConnectivityService. Does not change networks or radios. Wi-Fi SSID in the dump is not copied into results."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "telephony.registry"),
     "note": "Reads SIM state, airplane flag, and signal level. Read-only."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "netstats"),
     "note": "Reads byte counters. An approximation of mobile use, not a carrier bill."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "activity", "activities"),
     "note": "Reads the resumed activity so we can tell Facebook login from the home screen. Read-only."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "activity", "top"),
     "note": "Shorter activity dump. Same read-only family, used if the long dump is denied."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "package", "com.facebook.katana"),
     "note": "Reads one package's dump. The optional argument must be a single token, not a shell expression."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "window", "windows"),
     "note": "Reads window focus for layout and which app is in front. Does not resize or dismiss windows."},
    {"since": "0.3.18", "category": "read_only", "family": "dumpsys",
     "example": ("-s", "S1", "shell", "dumpsys", "notification"),
     "note": "Reads notifications. net_health keeps only a clipped USSD/MMI line and drops everything else."},

    {"since": "0.3.18", "category": "read_only", "family": "cmd_connectivity",
     "example": ("-s", "S1", "shell", "cmd", "connectivity", "airplane-mode"),
     "note": "Prints airplane mode. No enable/disable argument, so it cannot toggle the radio."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_connectivity",
     "example": ("-s", "S1", "shell", "cmd", "connectivity", "get-config"),
     "note": "Reads connectivity config. set-config is not this form."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_connectivity",
     "example": ("-s", "S1", "shell", "cmd", "connectivity", "get-network-watchlist"),
     "note": "Reads the network watchlist. Does not register a provider."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_activity",
     "example": ("-s", "S1", "shell", "cmd", "activity", "get-config"),
     "note": "Reads activity manager config. Does not start or stop an app."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_activity",
     "example": ("-s", "S1", "shell", "cmd", "activity", "get-current-user"),
     "note": "Reads the current user id. Does not switch users."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_activity",
     "example": ("-s", "S1", "shell", "cmd", "activity", "package-importance", "com.facebook.katana"),
     "note": "Reads one package's importance number. The package argument must be a package name."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_package",
     "example": ("-s", "S1", "shell", "cmd", "package", "list", "packages", "com.facebook.katana"),
     "note": "Queries installed packages, optionally filtered. Does not install or remove anything."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_package",
     "example": ("-s", "S1", "shell", "cmd", "package", "path", "com.facebook.katana"),
     "note": "Prints an installed package path if present. net_health reduces this to yes/no and does not return the path."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_package",
     "example": ("-s", "S1", "shell", "cmd", "package", "dump", "com.facebook.katana"),
     "note": "Reads one package's record. One package-name argument only."},
    {"since": "0.3.18", "category": "read_only", "family": "cmd_package",
     "example": ("-s", "S1", "shell", "cmd", "package", "resolve-activity", "--brief", "com.facebook.katana"),
     "note": "Resolves the launcher activity. Read-only query."},

    {"since": "0.3.18", "category": "read_only", "family": "settings_get",
     "example": ("-s", "S1", "shell", "settings", "get", "global", "mobile_data"),
     "note": "Reads one global setting. put and delete are a different family and stay closed."},
    {"since": "0.3.18", "category": "read_only", "family": "settings_get",
     "example": ("-s", "S1", "shell", "settings", "get", "system", "screen_brightness"),
     "note": "Reads one system setting. The key is a single token with no shell metacharacters."},
    {"since": "0.3.18", "category": "read_only", "family": "settings_get",
     "example": ("-s", "S1", "shell", "settings", "get", "secure", "android_id"),
     "note": "Reads one secure setting. net_health does not request identifier keys and does not return setting values except the booleans it parses."},

    {"since": "0.3.18", "category": "read_only", "family": "pm_query",
     "example": ("-s", "S1", "shell", "pm", "list", "packages"),
     "note": "Lists packages. Read-only. Flags are only the list filters (-3, -s, -f, and the other list switches)."},
    {"since": "0.3.18", "category": "read_only", "family": "pm_query",
     "example": ("-s", "S1", "shell", "pm", "list", "packages", "-3", "com.facebook"),
     "note": "Filtered package list. The filter is one token, not a shell expression."},
    {"since": "0.3.18", "category": "read_only", "family": "pm_query",
     "example": ("-s", "S1", "shell", "pm", "path", "com.facebook.katana"),
     "note": "Prints the apk path when Facebook is installed. The result keeps only installed true/false."},
    {"since": "0.3.18", "category": "read_only", "family": "pm_query",
     "example": ("-s", "S1", "shell", "pm", "path", "com.facebook.lite"),
     "note": "Same path query for Facebook Lite."},
    {"since": "0.3.18", "category": "read_only", "family": "pm_query",
     "example": ("-s", "S1", "shell", "pm", "dump", "com.facebook.katana"),
     "note": "Reads one package dump. Does not clear or disable the package."},

    {"since": "0.3.18", "category": "read_only", "family": "ping",
     "example": ("-s", "S1", "shell", "ping", "-c", "1", "-W", "3", "8.8.8.8"),
     "note": "One to four ICMP echoes, wait 1-10s. Flood (-f) and a missing -c (infinite ping) do not match."},
    {"since": "0.3.18", "category": "read_only", "family": "ping",
     "example": ("-s", "S1", "shell", "ping", "-c", "1", "-W", "3", "connectivitycheck.gstatic.com"),
     "note": "Same bounded ping to a hostname, used as the DNS check. The host cannot start with a dash."},
    {"since": "0.3.18", "category": "read_only", "family": "connectivity_204",
     "example": ("-s", "S1", "shell", "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", "8", "http://connectivitycheck.gstatic.com/generate_204"),
     "note": "HTTP GET of a fixed generate_204 URL, 8s cap, body discarded. Other URLs do not match, so this is not an open proxy."},
    {"since": "0.3.18", "category": "read_only", "family": "connectivity_204",
     "example": ("-s", "S1", "shell", "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", "8", "http://clients3.google.com/generate_204"),
     "note": "Second captive-portal endpoint. Same fixed argv shape."},
    {"since": "0.3.18", "category": "read_only", "family": "connectivity_204",
     "example": ("-s", "S1", "shell", "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", "8", "http://connectivitycheck.android.com/generate_204"),
     "note": "Android captive-portal endpoint. Same fixed argv shape."},
    {"since": "0.3.18", "category": "read_only", "family": "connectivity_204",
     "example": ("-s", "S1", "shell", "curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}",
                 "--max-time", "8", "http://www.google.com/generate_204"),
     "note": "Fourth generate_204 endpoint. The probe itself tries gstatic then clients3."},

    {"since": "0.3.18", "category": "read_only", "family": "getprop",
     "example": ("-s", "S1", "shell", "getprop", "gsm.sim.state"),
     "note": "Reads one property. net_health asks only gsm.sim.state, and only if telephony.registry was denied. Raw getprop output is not returned."},
    {"since": "0.3.18", "category": "read_only", "family": "getprop",
     "example": ("-s", "S1", "shell", "getprop"),
     "note": "Reads the property list. Diagnostic family. net_health does not call the bare form, because it contains serial properties, and results are scrubbed."},
    {"since": "0.3.18", "category": "read_only", "family": "ip",
     "example": ("-s", "S1", "shell", "ip", "addr"),
     "note": "Reads addresses. link-set / addr-add / route-add do not match. Addresses are not copied into the result."},
    {"since": "0.3.18", "category": "read_only", "family": "ip",
     "example": ("-s", "S1", "shell", "ip", "-4", "route"),
     "note": "Reads IPv4 routes. Used only as a transport fallback when dumpsys connectivity is denied."},
    {"since": "0.3.18", "category": "read_only", "family": "ifconfig",
     "example": ("-s", "S1", "shell", "ifconfig"),
     "note": "Reads interface flags. Extra arguments that would bring an interface up or down do not match."},
    {"since": "0.3.18", "category": "read_only", "family": "ifconfig",
     "example": ("-s", "S1", "shell", "ifconfig", "-a"),
     "note": "Reads all interfaces. Same read-only family."},
    {"since": "0.3.18", "category": "read_only", "family": "wm",
     "example": ("-s", "S1", "shell", "wm", "size"),
     "note": "Prints the current size. A size argument would change it and does not match."},
    {"since": "0.3.18", "category": "read_only", "family": "wm",
     "example": ("-s", "S1", "shell", "wm", "density"),
     "note": "Prints density. No density value, so it cannot change the display."},

    {"since": "0.3.18", "category": "app_launch", "family": "am_start_facebook",
     "example": ("-s", "S1", "shell", "am", "start", "-a", "android.intent.action.MAIN",
                 "-c", "android.intent.category.LAUNCHER", "-p", "com.facebook.katana"),
     "note": "Brings Facebook (katana or lite) to the foreground. No extras, no data URI, no other package. net_health does this only when the task payload sets foreground_facebook to JSON true."},
    {"since": "0.3.18", "category": "app_launch", "family": "am_start_facebook",
     "example": ("-s", "S1", "shell", "am", "start", "-p", "com.facebook.lite"),
     "note": "Same launch, package flag only, Lite."},
    {"since": "0.3.18", "category": "app_launch", "family": "am_start_facebook",
     "example": ("-s", "S1", "shell", "am", "start", "-n", "com.facebook.katana/.LoginActivity"),
     "note": "Starts one component inside the Facebook package. The package must be katana or lite."},

    {"since": "0.3.18", "category": "experimental_ussd", "family": "ussd_dial",
     "example": ("-s", "S1", "shell", "am", "start", "-a", "android.intent.action.CALL", "-d", "tel:*123%23"),
     "note": "Dials one *digits# code. Closed unless allow_experimental_ussd is true. net_health sets that only when net_health_ussd_enabled is JSON true and the wallpaper has a matching code. Default off. This is not a balance API."},

    {"since": "0.3.18", "category": "guarded_write", "family": "settings_put",
     "example": ("-s", "S1", "shell", "settings", "put", "global", "mobile_data", "0"),
     "note": "Writes one setting. Closed unless allow_guarded_writes is true. No fleet task passes that flag."},
    {"since": "0.3.18", "category": "guarded_write", "family": "settings_put",
     "example": ("-s", "S1", "shell", "settings", "delete", "global", "mobile_data"),
     "note": "Deletes one setting. Same closed door as put."},
    {"since": "0.3.18", "category": "guarded_write", "family": "svc_radio",
     "example": ("-s", "S1", "shell", "svc", "data", "disable"),
     "note": "Toggles mobile data. Closed by default. net_health never calls svc."},
    {"since": "0.3.18", "category": "guarded_write", "family": "svc_radio",
     "example": ("-s", "S1", "shell", "svc", "wifi", "enable"),
     "note": "Toggles Wi-Fi. Closed by default."},
    {"since": "0.3.18", "category": "guarded_write", "family": "reboot",
     "example": ("reboot",),
     "note": "Reboots the device or host-side adb target. Closed by default. Recovery and bootloader are the same family."},
    {"since": "0.3.18", "category": "guarded_write", "family": "reboot",
     "example": ("-s", "S1", "reboot"),
     "note": "Reboots one device. Closed by default."},
    {"since": "0.3.18", "category": "guarded_write", "family": "pm_mutate",
     "example": ("-s", "S1", "shell", "pm", "clear", "com.example.app"),
     "note": "Clears an app's data. Closed by default."},
    {"since": "0.3.18", "category": "guarded_write", "family": "pm_mutate",
     "example": ("-s", "S1", "shell", "pm", "uninstall", "com.example.app"),
     "note": "Uninstalls a package. Closed by default."},
    {"since": "0.3.18", "category": "guarded_write", "family": "pm_mutate",
     "example": ("-s", "S1", "shell", "pm", "disable", "com.example.app"),
     "note": "Disables a package. Closed by default. enable and disable-user are the same family."},
    {"since": "0.3.18", "category": "guarded_write", "family": "cmd_package_mutate",
     "example": ("-s", "S1", "shell", "cmd", "package", "clear", "com.example.app"),
     "note": "Clears a package via cmd. Closed by default."},
    {"since": "0.3.18", "category": "guarded_write", "family": "force_stop",
     "example": ("-s", "S1", "shell", "am", "force-stop", "com.example.app"),
     "note": "Force-stops an app that is not Facebook. Closed unless allow_guarded_writes is true. No fleet task passes that flag."},
    {"since": "0.3.18", "category": "guarded_write", "family": "force_stop",
     "example": ("-s", "S1", "shell", "cmd", "activity", "force-stop", "com.example.app"),
     "note": "Force-stop via cmd activity for a non-Facebook package. Closed by default."},
    {"since": "0.3.23", "category": "app_restart", "family": "facebook_force_stop",
     "example": ("-s", "S1", "shell", "am", "force-stop", "com.facebook.katana"),
     "note": "Force-stops Facebook (katana or lite) only. Closed unless allow_app_restart is true. phone_app_restart is the only task that sets that flag. allow_guarded_writes does not open this form."},
    {"since": "0.3.23", "category": "app_restart", "family": "facebook_force_stop",
     "example": ("-s", "S1", "shell", "cmd", "activity", "force-stop", "com.facebook.lite"),
     "note": "Force-stop of Facebook Lite via cmd activity. Same closed app_restart door. Other packages stay guarded_write."},
    {"since": "0.3.26", "category": "phone_control", "family": "portrait_lock",
     "example": ("-s", "S1", "shell", "settings", "put", "system", "accelerometer_rotation", "0"),
     "note": "Turns off auto-rotate. Only this value. Other settings writes stay guarded_write."},
    {"since": "0.3.26", "category": "phone_control", "family": "portrait_lock",
     "example": ("-s", "S1", "shell", "settings", "put", "system", "user_rotation", "0"),
     "note": "Locks the user rotation to portrait (0). user_rotation 1, 2, and 3 stay closed."},
    {"since": "0.3.27", "category": "phone_control", "family": "screen_wake",
     "example": ("-s", "S1", "shell", "input", "keyevent", "224"),
     "note": "Wakes the screen (KEYCODE_WAKEUP). Does not toggle the panel off. Keyevent 26 stays denied."},
    {"since": "0.3.18", "category": "guarded_write", "family": "airplane_mode",
     "example": ("-s", "S1", "shell", "cmd", "connectivity", "airplane-mode", "enable"),
     "note": "Turns airplane mode on. The no-argument query is read-only; enable and disable are this closed family."},
)


# Forms that stay denied even if both closed-door flags are set. They are not
# in the catalog. Flags are not a shell escape hatch.
DENIED_FOREVER: Tuple[Tuple[str, ...], ...] = (
    ("kill-server",),
    ("start-server",),
    ("tcpip", "5555"),
    ("usb",),
    ("connect", "1.2.3.4:5555"),
    ("-s", "S1", "install", "x.apk"),
    ("-s", "S1", "shell", "rm", "-rf", "/sdcard"),
    ("-s", "S1", "shell", "sh", "-c", "id"),
    ("-s", "S1", "shell", "su", "-c", "id"),
    ("-s", "S1", "shell", "input", "keyevent", "26"),
    ("-s", "S1", "exec-out", "screencap", "-p"),
    ("-s", "S1", "shell", "uiautomator", "dump"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/dev/tty", "--compressed"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/chatx_like_hierarchy.xml", "--compressed"),
    ("-s", "S1", "shell", "uiautomator", "dump", "/sdcard/other.xml"),
    ("-s", "S1", "exec-out", "uiautomator", "dump", "/dev/tty"),
    ("-s", "S1", "pull", "/sdcard/window_dump.xml", "/tmp/chatx_like_x.xml"),
    ("-s", "S1", "pull", "/sdcard/chatx_like_hierarchy.xml", "/tmp/../x.xml"),
    ("-s", "S1", "pull", "/sdcard/chatx_like_hierarchy.xml", "/tmp/has space.xml"),
    ("-s", "S1", "shell", "input", "keyevent", "85"),
    ("-s", "S1", "shell", "cmd", "media_session", "dispatch", "play"),
    ("-s", "S1", "forward", "tcp:9000", "tcp:9000"),
    ("-s", "S1", "shell", "dumpsys", "wifi"),
    ("-s", "S1", "shell", "pm", "grant", "com.facebook.katana", "android.permission.CAMERA"),
    ("-s", "S1", "shell", "am", "start", "-p", "com.android.settings"),
    ("-s", "S1", "shell", "am", "start", "-a", "android.intent.action.MAIN", "-p", "com.facebook.katana", "--es", "x", "y"),
    ("-s", "S1", "shell", "ping", "-f", "8.8.8.8"),
    ("-s", "S1", "shell", "ping", "8.8.8.8"),
    ("-s", "S1", "shell", "curl", "http://example.com/"),
    ("-s", "S1", "shell", "ip", "link", "set", "wlan0", "down"),
    ("-s", "S1", "shell", "wm", "size", "1080x1920"),
    ("-s", "S1", "shell", "svc", "power", "shutdown"),
    ("-s", "S1", "shell", "settings", "put", "global", "mobile_data", "0;reboot"),
    ("-s", "3B1F4KE5MS140P4X", "shell", "dumpsys", "connectivity"),
)
