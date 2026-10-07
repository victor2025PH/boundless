"""Per-account Facebook pace for the fleet social path.

A person scrolling a feed likes a handful of posts an hour and a few dozen
in a day, then stops. These ceilings keep one login inside that rhythm so
the account is not worn out. They are usage limits. They are not a way to
slip past a platform check.

Chat STOP / opt-out (``src/inbox/stop_contact.py``) and handoff keyword
checks (``config/handoff_compliance.yaml``) stay where they are. This module
does not invent a second opt-out. The kill switch here only pauses counted
Facebook taps (like / comment / follow / post).

``like_probe`` and ``dry_run`` locate or plan. They do not tap, so they are
not counted and are not refused by these ceilings.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .phone_rules import PhoneOpError

logger = logging.getLogger(__name__)

ACTIONS = ("like", "comment", "follow", "post")
ACTION_BY_KIND = {
    "phone_like": "like",
    "phone_comment": "comment",
    "phone_follow": "follow",
    "phone_post": "post",
}

REASON_CAPPED = "rate_capped"
REASON_HOURS = "outside_active_hours"
REASON_DISABLED = "compliance_disabled"
REASON_DELAY = "min_delay"

# Conservative per-account defaults. See config/compliance.yaml for why.
_DEFAULT_HOURLY = {"like": 6, "comment": 3, "follow": 3, "post": 1}
_DEFAULT_DAILY = {"like": 40, "comment": 15, "follow": 15, "post": 4}
_DEFAULT_MIN_DELAY = 60
_DEFAULT_JITTER = 20
_DEFAULT_TZ = 8
_DEFAULT_ACTIVE = (8, 22)
_HOUR_WINDOW = 3600
_KEEP_SEC = 3 * 86400

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._@+\-]{1,80}$")
_WALL_RE = re.compile(r"^[0-9]{1,6}$")

Event = Tuple[str, float]


def clock() -> float:
    """Wall clock used when a caller does not pass ``now``.

    Tests pin this function so existing fleet cases stay inside the default
    active window no matter when the runner starts. Production leaves it as
    ``time.time``.
    """
    return time.time()


class PaceDecision:
    """One allow/skip answer. ``window`` is ``hour`` or ``day`` for a cap skip."""

    __slots__ = ("allow", "reason", "window", "counted")

    def __init__(self, allow: bool, reason: str = "", window: str = "", counted: bool = False) -> None:
        self.allow = bool(allow)
        self.reason = str(reason or "")
        self.window = str(window or "")
        self.counted = bool(counted)

    def __repr__(self) -> str:
        return f"PaceDecision(allow={self.allow}, reason={self.reason!r}, window={self.window!r}, counted={self.counted})"


class FacebookPace:
    """Resolved ceilings. Built-in numbers match ``config/compliance.yaml``."""

    def __init__(self, *, enabled: bool = True, tz_offset_hours: int = _DEFAULT_TZ,
                 active_start: int = _DEFAULT_ACTIVE[0], active_end: int = _DEFAULT_ACTIVE[1],
                 min_delay_sec: int = _DEFAULT_MIN_DELAY, jitter_sec: int = _DEFAULT_JITTER,
                 hourly: Optional[Dict[str, int]] = None, daily: Optional[Dict[str, int]] = None) -> None:
        self.enabled = bool(enabled)
        self.tz_offset_hours = int(tz_offset_hours)
        self.active_start = int(active_start)
        self.active_end = int(active_end)
        self.min_delay_sec = int(min_delay_sec)
        self.jitter_sec = int(jitter_sec)
        self.hourly = {name: int((hourly or _DEFAULT_HOURLY)[name]) for name in ACTIONS}
        self.daily = {name: int((daily or _DEFAULT_DAILY)[name]) for name in ACTIONS}

    def public(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "tz_offset_hours": self.tz_offset_hours,
            "active_hours": [self.active_start, self.active_end],
            "min_delay_sec": self.min_delay_sec,
            "jitter_sec": self.jitter_sec,
            "actions": {
                name: {"hourly": self.hourly[name], "daily": self.daily[name]}
                for name in ACTIONS
            },
        }

    @classmethod
    def from_mapping(cls, raw: Any) -> "FacebookPace":
        if not isinstance(raw, dict):
            return cls()
        enabled = True if raw.get("enabled") is not False else False
        start, end = _DEFAULT_ACTIVE
        hours = raw.get("active_hours")
        if isinstance(hours, (list, tuple)) and len(hours) == 2:
            parsed_start = _as_int(hours[0], -1, 0, 23)
            parsed_end = _as_int(hours[1], -1, 1, 24)
            if 0 <= parsed_start < parsed_end <= 24:
                start, end = parsed_start, parsed_end
        hourly = dict(_DEFAULT_HOURLY)
        daily = dict(_DEFAULT_DAILY)
        actions = raw.get("actions") if isinstance(raw.get("actions"), dict) else {}
        for name in ACTIONS:
            spec = actions.get(name)
            if not isinstance(spec, dict):
                continue
            hourly[name] = _as_int(spec.get("hourly"), hourly[name], 0, 1000)
            daily[name] = _as_int(spec.get("daily"), daily[name], 0, 10000)
        return cls(
            enabled=enabled,
            tz_offset_hours=_as_int(raw.get("tz_offset_hours"), _DEFAULT_TZ, -12, 14),
            active_start=start,
            active_end=end,
            min_delay_sec=_as_int(raw.get("min_delay_sec"), _DEFAULT_MIN_DELAY, 0, 7200),
            jitter_sec=_as_int(raw.get("jitter_sec"), _DEFAULT_JITTER, 0, 600),
            hourly=hourly,
            daily=daily,
        )


def default_policy_path() -> Path:
    """``engines/chengjie/config/compliance.yaml`` next to this package."""
    return Path(__file__).resolve().parents[2] / "config" / "compliance.yaml"


def load_facebook_policy(path: Optional[Any] = None) -> FacebookPace:
    """Load the facebook section. A missing or unreadable file keeps the built-in caps."""
    target = Path(path) if path else default_policy_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return FacebookPace()
    data = _load_yaml_mapping(text)
    block = data.get("facebook") if isinstance(data, dict) else None
    if not isinstance(block, dict):
        return FacebookPace()
    return FacebookPace.from_mapping(block)


def local_day(ts: float, tz_offset_hours: int) -> int:
    return int(_shift(ts, tz_offset_hours) // 86400)


def local_hour(ts: float, tz_offset_hours: int) -> int:
    shifted = _shift(ts, tz_offset_hours)
    return int((shifted % 86400) // 3600)


def spacing_sec(policy: FacebookPace, account_key: str, last_ts: float) -> int:
    """Minimum gap before the next counted action on this account.

    The extra seconds are a stable hash of the account and the previous
    action time, so the gap is not a fixed metronome. The same inputs always
    wait the same amount. This only spreads usage; it is not a detection bypass.
    """
    base = max(0, int(policy.min_delay_sec))
    jitter = max(0, int(policy.jitter_sec))
    if jitter <= 0:
        return base
    digest = hashlib.sha256(f"{account_key}|{int(last_ts)}".encode("utf-8")).digest()
    extra = int.from_bytes(digest[:2], "big") % (jitter + 1)
    return base + extra


def account_key(node_id: str, app: str, serial: str = "", account: str = "", wallpaper: str = "") -> str:
    """Prefer an explicit account, then a wallpaper number, then the adb serial."""
    return f"{_clean(node_id, 80)}|{_clean(app, 32).lower()}|{identity_token(serial, account, wallpaper)}"


def identity_token(serial: str = "", account: str = "", wallpaper: str = "") -> str:
    acct = _match(account, _ACCOUNT_RE)
    if acct:
        return "acct:" + acct.lower()
    wall = _wallpaper(wallpaper)
    if wall:
        return "wall:" + wall
    return "serial:" + _clean(serial, 80)


def decide(policy: FacebookPace, *, app: str, action: str, events: Sequence[Event], now: float,
           account_key: str = "", probe: bool = False, dry_run: bool = False) -> PaceDecision:
    """Return whether this one action may run, and whether it should be counted.

    ``events`` are prior counted actions for this account only, as
    ``(action, timestamp)`` pairs. This function does not mutate them.
    """
    app_name = str(app or "").strip().lower()
    act = str(action or "").strip().lower()
    if app_name != "facebook" or act not in ACTIONS:
        return PaceDecision(True, counted=False)
    if probe or dry_run:
        return PaceDecision(True, counted=False)
    if not policy.enabled:
        return PaceDecision(False, REASON_DISABLED)
    hour = local_hour(now, policy.tz_offset_hours)
    if hour < policy.active_start or hour >= policy.active_end:
        return PaceDecision(False, REASON_HOURS)
    prior = [(a, float(ts)) for a, ts in events if float(ts) <= float(now)]
    hour_n = sum(1 for a, ts in prior if a == act and float(now) - _HOUR_WINDOW < ts)
    if hour_n >= policy.hourly[act]:
        return PaceDecision(False, REASON_CAPPED, "hour")
    day = local_day(now, policy.tz_offset_hours)
    day_n = sum(1 for a, ts in prior if a == act and local_day(ts, policy.tz_offset_hours) == day)
    if day_n >= policy.daily[act]:
        return PaceDecision(False, REASON_CAPPED, "day")
    if prior and policy.min_delay_sec > 0:
        last = max(ts for _a, ts in prior)
        if float(now) < last + spacing_sec(policy, account_key, last):
            return PaceDecision(False, REASON_DELAY)
    return PaceDecision(True, counted=True)


def counts(events: Iterable[Event], action: str, now: float, tz_offset_hours: int) -> Tuple[int, int]:
    """``(hour, day)`` totals for one action at ``now``."""
    act = str(action or "")
    day = local_day(now, tz_offset_hours)
    hour_n = 0
    day_n = 0
    for name, ts in events:
        if name != act or float(ts) > float(now):
            continue
        if local_day(ts, tz_offset_hours) == day:
            day_n += 1
        if float(now) - _HOUR_WINDOW < float(ts):
            hour_n += 1
    return hour_n, day_n


def read_optional_labels(body: Any) -> Tuple[str, str]:
    """``(account, wallpaper)`` from a social request. Absent keys are empty.

    A present but illegal value is ``bad_account`` / ``bad_wallpaper`` so the
    caller does not silently fall back to the serial and pace the wrong login.
    """
    if not isinstance(body, dict):
        return "", ""
    account = _optional_field(body, "account", _ACCOUNT_RE, "bad_account", allow_int=True)
    if "wallpaper" in body:
        wallpaper = _optional_field(body, "wallpaper", _WALL_RE, "bad_wallpaper", allow_int=True)
    elif "wallpaper_no" in body:
        wallpaper = _optional_field(body, "wallpaper_no", _WALL_RE, "bad_wallpaper", allow_int=True)
    else:
        wallpaper = ""
    return account, wallpaper


class PaceLedger:
    """In-process account ledger for the node. Optional JSON file under the state dir.

    The controller database is the source of truth for today's totals. This
    ledger is the check that runs on the phone PC before a tap, including when
    a task was queued earlier and the active window has since closed.
    """

    def __init__(self, policy: Optional[FacebookPace] = None) -> None:
        self.policy = policy if isinstance(policy, FacebookPace) else load_facebook_policy()
        self.path: Optional[Path] = None
        self._events: List[Dict[str, Any]] = []
        self._lock_impl = __import__("threading").Lock()

    def bind(self, state_dir: Any) -> None:
        if not state_dir:
            self.path = None
            return
        path = Path(state_dir) / "social_pace.json"
        self.path = path
        self._events = _read_events(path)

    def allow(self, *, app: str, action: str, serial: str, account: str = "", wallpaper: str = "",
              probe: bool = False, dry_run: bool = False, now: Optional[float] = None,
              node_id: str = "local") -> PaceDecision:
        ts = float(clock() if now is None else now)
        key = account_key(node_id, app, serial, account, wallpaper)
        with self._lock_impl:
            events = [(str(e.get("action") or ""), float(e.get("ts") or 0))
                      for e in self._events if e.get("key") == key]
            decision = decide(self.policy, app=app, action=action, events=events, now=ts,
                              account_key=key, probe=probe, dry_run=dry_run)
            if decision.allow and decision.counted:
                self._events.append({"key": key, "action": action, "ts": ts})
                if len(self._events) > 500:
                    self._events = self._events[-500:]
                _write_events(self.path, self._events)
            return decision


def _shift(ts: float, tz_offset_hours: int) -> float:
    return float(ts) + int(tz_offset_hours) * 3600


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int) and lo <= value <= hi:
        return value
    return default


def _clean(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _match(value: Any, pattern: re.Pattern) -> str:
    text = _clean(value, 80)
    if text and pattern.match(text):
        return text
    return ""


def _wallpaper(value: Any) -> str:
    text = _match(value, _WALL_RE)
    if not text:
        return ""
    trimmed = text.lstrip("0")
    return trimmed or "0"


def _optional_field(body: Dict[str, Any], key: str, pattern: re.Pattern, code: str, *,
                    allow_int: bool) -> str:
    if key not in body or body.get(key) in (None, ""):
        return ""
    value = body.get(key)
    if allow_int and isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or not pattern.match(value.strip()):
        raise PhoneOpError(code)
    if key in ("wallpaper", "wallpaper_no"):
        return _wallpaper(value)
    return value.strip()


def _load_yaml_mapping(text: str) -> Dict[str, Any]:
    try:
        import yaml
    except Exception:
        logger.warning("facebook pace config left at built-in caps (yaml unavailable)")
        return {}
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        logger.warning("facebook pace config left at built-in caps (yaml unreadable)")
        return {}
    return data if isinstance(data, dict) else {}


def _read_events(path: Path) -> List[Dict[str, Any]]:
    try:
        if not path.is_file() or path.stat().st_size <= 0 or path.stat().st_size > 256 * 1024:
            return []
        import json
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows[-500:]:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "")
        action = str(row.get("action") or "")
        try:
            ts = float(row.get("ts"))
        except (TypeError, ValueError):
            continue
        if key and action in ACTIONS:
            out.append({"key": key, "action": action, "ts": ts})
    return out


def _write_events(path: Optional[Path], events: List[Dict[str, Any]]) -> None:
    if path is None:
        return
    import json
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"events": events[-500:]}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.warning("facebook pace ledger was not saved")


__all__ = [
    "ACTIONS", "ACTION_BY_KIND", "REASON_CAPPED", "REASON_HOURS", "REASON_DISABLED", "REASON_DELAY",
    "PaceDecision", "FacebookPace", "PaceLedger", "clock", "default_policy_path", "load_facebook_policy",
    "local_day", "local_hour", "spacing_sec", "account_key", "identity_token", "decide", "counts",
    "read_optional_labels",
]
