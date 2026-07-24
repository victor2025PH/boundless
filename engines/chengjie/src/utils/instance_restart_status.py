"""Dual-instance restart cooldown status (ops observability).

Reads the machine-shared JSON written by
``deploy/instances/_restart_cooldown.ps1`` (used by ``restart_instance.ps1``
and ``watchdog_instances.ps1``). Pure filesystem reads — no process coupling.

Canonical dir: ``D:\\chengjie-instances\\.ops\\restart_cooldown\\``
Fallbacks keep older LOCALAPPDATA / repo-local paths readable.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_INSTANCES = ("zhiliao", "tongyi")
_DEFAULT_COOLDOWN_MIN = 10


def _window_sla_sec() -> int:
    """Restart window SLA ceiling (seconds). A restart taking longer than this
    means the seat was unavailable too long — surface it as an SLA breach.

    Default 180s tracks these heavy instances' real cold start (Telegram login +
    voice preheat + KB serve /login only after full init, observed ~2-3.5min).
    A tighter ceiling would breach on every normal restart. Env-overridable via
    ``CHENGJIE_RESTART_WINDOW_SLA_SEC``."""
    raw = (os.environ.get("CHENGJIE_RESTART_WINDOW_SLA_SEC") or "").strip()
    try:
        v = int(raw)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    return 180


def current_instance_id() -> Optional[str]:
    """Which dual-instance this process belongs to (env), or None if unknown."""
    pid = (os.environ.get("CHENGJIE_PRODUCT_ID") or "").strip().lower()
    if pid in _INSTANCES:
        return pid
    data = (os.environ.get("AITR_DATA_DIR") or "").replace("\\", "/").lower()
    for name in _INSTANCES:
        # path segment match: .../zhiliao/... or .../zhiliao
        if f"/{name}/" in f"/{data}/":
            return name
    return None


def seat_restart_banner(*, now: Optional[float] = None) -> Dict[str, Any]:
    """Slim payload for workbench status bar (this product only; no paths)."""
    full = collect_restart_status(now=now)
    cur = current_instance_id()
    mine: Optional[Dict[str, Any]] = None
    if cur:
        for it in full.get("instances") or []:
            if it.get("id") == cur:
                mine = it
                break
    active = bool(mine and mine.get("cooldown_active"))
    flap = (mine or {}).get("flap") or {}
    return {
        "instance_id": cur,
        "cooldown_active": active,
        "cooldown_left_min": int((mine or {}).get("cooldown_left_min") or 0),
        "cooldown_left_sec": int((mine or {}).get("cooldown_left_sec") or 0),
        "reason": (mine or {}).get("reason") if active else None,
        "name": (mine or {}).get("name"),
        # Phase7: seat poll throttle signals (no paths)
        "flapping": bool(flap.get("flapping")),
        "http_phase": (mine or {}).get("http_phase"),
        "quiet_poll": bool(active or flap.get("flapping")),
    }


def ops_dirs() -> List[Path]:
    """Roots for last_status.json (watchdog snapshot)."""
    env = (os.environ.get("CHENGJIE_OPS_DIR") or "").strip()
    if env:
        return [Path(env)]
    roots: List[Path] = [Path(r"D:\chengjie-instances\.ops")]
    here = Path(__file__).resolve()
    try:
        boundless = here.parents[4]
        roots.append(boundless / "deploy" / "instances" / ".ops")
    except IndexError:
        pass
    out: List[Path] = []
    seen = set()
    for r in roots:
        key = str(r).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def status_snapshot_path() -> Optional[Path]:
    for root in ops_dirs():
        p = root / "last_status.json"
        if p.is_file():
            return p
    return None


def load_status_snapshot() -> Optional[Dict[str, Any]]:
    """Watchdog-written status_instances -Json snapshot (http_phase etc.)."""
    p = status_snapshot_path()
    if not p:
        return None
    data = _read_json(p)
    if data and isinstance(data.get("instances"), list):
        return data
    return None


def _events_paths() -> List[Path]:
    paths: List[Path] = []
    for root in cooldown_dirs():
        paths.append(root / "restart_events.jsonl")
    for root in ops_dirs():
        paths.append(root / "restart_cooldown" / "restart_events.jsonl")
    # de-dupe
    out: List[Path] = []
    seen = set()
    for p in paths:
        k = str(p).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def flap_for_instance(
    instance: str,
    *,
    now: Optional[float] = None,
    window_sec: int = 1800,
    threshold: int = 2,
) -> Dict[str, Any]:
    """Count restart_events.jsonl rows for instance in window."""
    now = time.time() if now is None else float(now)
    cut = now - max(60, int(window_sec))
    count = 0
    reasons: List[str] = []
    for path in _events_paths():
        if not path.is_file():
            continue
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            continue
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if str(obj.get("instance") or "") != instance:
                continue
            try:
                unix = float(obj.get("unix") or 0)
            except (TypeError, ValueError):
                unix = 0.0
            if unix >= cut:
                count += 1
                r = str(obj.get("reason") or "")
                if r:
                    reasons.append(r)
        break  # first existing events file wins
    return {
        "flapping": count >= threshold,
        "count": count,
        "threshold": threshold,
        "window_sec": window_sec,
        "reasons": reasons[-5:],
    }


def boot_timing_snapshot() -> Optional[Dict[str, Any]]:
    """Phase 12b: this process's last boot phase timing (from metrics_store).

    Written by ``main.initialize()`` via ``BootTimer.summary()``. Scope note:
    everything else in this module is machine-wide (cooldown files), but boot
    timing only exists inside the process that booted — so the payload is
    tagged with ``instance`` (which dual-instance this process is). Returns
    None when not yet populated (e.g. process started on pre-Phase11 code).
    """
    try:
        from src.monitoring.metrics_store import get_metrics_store

        summary = get_metrics_store().get_boot_timing()
        if not isinstance(summary, dict) or not summary:
            return None
        out = dict(summary)
        out.setdefault("instance", current_instance_id() or "self")
        return out
    except Exception:
        return None


def _boot_seat_ready_sec(summary: Dict[str, Any]) -> Optional[float]:
    """Cumulative seconds at the ``web_app`` mark = process start → /login serving."""
    try:
        for ph in summary.get("phases") or []:
            if isinstance(ph, dict) and ph.get("name") == "web_app":
                return float(ph.get("cumulative_sec"))
    except (TypeError, ValueError):
        pass
    return None


def dump_prom(*, now: Optional[float] = None) -> str:
    """Prometheus text for dual-instance restart cooldown (ops scrape)."""
    snap = collect_restart_status(now=now)
    lines = [
        "# HELP chengjie_instance_restart_cooldown_active_any 1 if any instance is in cooldown",
        "# TYPE chengjie_instance_restart_cooldown_active_any gauge",
        f"chengjie_instance_restart_cooldown_active_any "
        f"{1 if snap.get('cooldown_active_any') else 0}",
        "# HELP chengjie_instance_restart_flapping_any 1 if any instance restarted >=2x/30m",
        "# TYPE chengjie_instance_restart_flapping_any gauge",
        f"chengjie_instance_restart_flapping_any "
        f"{1 if snap.get('flapping_any') else 0}",
        "# HELP chengjie_instance_restart_window_sla_breach_any 1 if any last restart window exceeded SLA",
        "# TYPE chengjie_instance_restart_window_sla_breach_any gauge",
        f"chengjie_instance_restart_window_sla_breach_any "
        f"{1 if snap.get('window_sla_breach_any') else 0}",
        "# HELP chengjie_instance_restart_cooldown_active 1 if restart cooldown is active",
        "# TYPE chengjie_instance_restart_cooldown_active gauge",
        "# HELP chengjie_instance_restart_cooldown_left_seconds Seconds left in cooldown (0 if idle)",
        "# TYPE chengjie_instance_restart_cooldown_left_seconds gauge",
        "# HELP chengjie_instance_restart_age_seconds Seconds since last restart record (-1 if none)",
        "# TYPE chengjie_instance_restart_age_seconds gauge",
        "# HELP chengjie_instance_http_phase Phase code: ready=0 warming=1 unresponsive=2 other=3",
        "# TYPE chengjie_instance_http_phase gauge",
        "# HELP chengjie_instance_restart_flapping 1 if instance flapping in 30m window",
        "# TYPE chengjie_instance_restart_flapping gauge",
        "# HELP chengjie_instance_restart_window_seconds Last restart window duration (-1 if none)",
        "# TYPE chengjie_instance_restart_window_seconds gauge",
        "# HELP chengjie_instance_restart_window_sla_breach 1 if last restart window exceeded SLA",
        "# TYPE chengjie_instance_restart_window_sla_breach gauge",
    ]
    phase_code = {"ready": 0, "warming": 1, "unresponsive": 2}
    for it in snap.get("instances") or []:
        iid = str(it.get("id") or "unknown").replace('"', "")
        active = 1 if it.get("cooldown_active") else 0
        left = int(it.get("cooldown_left_sec") or 0)
        age = it.get("age_sec")
        age_n = int(age) if age is not None else -1
        phase = str(it.get("http_phase") or "other")
        pcode = phase_code.get(phase, 3)
        flap = 1 if (it.get("flap") or {}).get("flapping") else 0
        win = it.get("window_sec")
        try:
            win_n = int(win) if win is not None else -1
        except (TypeError, ValueError):
            win_n = -1
        sla_breach = 1 if it.get("window_sla_breach") else 0
        lines.append(
            f'chengjie_instance_restart_cooldown_active{{instance="{iid}"}} {active}'
        )
        lines.append(
            f'chengjie_instance_restart_cooldown_left_seconds{{instance="{iid}"}} {left}'
        )
        lines.append(
            f'chengjie_instance_restart_age_seconds{{instance="{iid}"}} {age_n}'
        )
        lines.append(
            f'chengjie_instance_http_phase{{instance="{iid}",phase="{phase}"}} {pcode}'
        )
        lines.append(
            f'chengjie_instance_restart_flapping{{instance="{iid}"}} {flap}'
        )
        lines.append(
            f'chengjie_instance_restart_window_seconds{{instance="{iid}"}} {win_n}'
        )
        lines.append(
            f'chengjie_instance_restart_window_sla_breach{{instance="{iid}"}} {sla_breach}'
        )
    # Phase 12b: boot phase timing of THIS process (per-instance scrape → the
    # instance label is implicit in which port Prometheus scraped; still tag it
    # so a shared dashboard can group). Optional: absent until first boot on
    # Phase11+ code — probes must treat these gauges as optional, not required.
    bt = snap.get("boot_timing")
    if isinstance(bt, dict) and bt:
        iid = str(bt.get("instance") or "self").replace('"', "")
        lines.append(
            "# HELP chengjie_instance_boot_total_seconds Last boot initialize() total wall seconds"
        )
        lines.append("# TYPE chengjie_instance_boot_total_seconds gauge")
        try:
            lines.append(
                f'chengjie_instance_boot_total_seconds{{instance="{iid}"}} '
                f"{float(bt.get('total_sec'))}"
            )
        except (TypeError, ValueError):
            pass
        seat = _boot_seat_ready_sec(bt)
        if seat is not None:
            lines.append(
                "# HELP chengjie_instance_boot_seat_ready_seconds Process start to /login serving (web_app cumulative)"
            )
            lines.append("# TYPE chengjie_instance_boot_seat_ready_seconds gauge")
            lines.append(
                f'chengjie_instance_boot_seat_ready_seconds{{instance="{iid}"}} {seat}'
            )
        phases = [p for p in (bt.get("phases") or []) if isinstance(p, dict)]
        if phases:
            lines.append(
                "# HELP chengjie_instance_boot_phase_seconds Per-phase boot delta seconds"
            )
            lines.append("# TYPE chengjie_instance_boot_phase_seconds gauge")
            for ph in phases:
                name = str(ph.get("name") or "").replace('"', "")
                if not name:
                    continue
                try:
                    delta = float(ph.get("delta_sec"))
                except (TypeError, ValueError):
                    continue
                lines.append(
                    f'chengjie_instance_boot_phase_seconds{{instance="{iid}",phase="{name}"}} {delta}'
                )
    return "\n".join(lines) + "\n"


def cooldown_dirs() -> List[Path]:
    """Ordered search roots (first existing file wins per instance).

    If ``CHENGJIE_RESTART_COOLDOWN_DIR`` is set, it is the **only** root
    (tests / alternate hosts must not leak into production paths).
    """
    env = (os.environ.get("CHENGJIE_RESTART_COOLDOWN_DIR") or "").strip()
    if env:
        return [Path(env)]
    roots: List[Path] = [Path(r"D:\chengjie-instances\.ops\restart_cooldown")]
    # Repo-relative: .../boundless/deploy/instances/.ops/restart_cooldown
    # Path: utils(0)/src(1)/chengjie(2)/engines(3)/boundless(4)
    here = Path(__file__).resolve()
    try:
        boundless = here.parents[4]
        roots.append(boundless / "deploy" / "instances" / ".ops" / "restart_cooldown")
    except IndexError:
        pass
    la = os.environ.get("LOCALAPPDATA") or ""
    if la:
        roots.append(Path(la) / "boundless-instance-restart")
    # de-dupe preserve order
    out: List[Path] = []
    seen = set()
    for r in roots:
        key = str(r).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        # PS 5.1 Set-Content -Encoding UTF8 writes a BOM; utf-8-sig strips it.
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw) if raw.strip() else None
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_instance_record(instance: str) -> Optional[Dict[str, Any]]:
    name = f"last_restart_{instance}.json"
    for root in cooldown_dirs():
        p = root / name
        if not p.is_file():
            continue
        data = _read_json(p)
        if not data:
            continue
        data = dict(data)
        data["_path"] = str(p)
        # Prefer file mtime if unix/ts missing
        try:
            if not data.get("unix") and not data.get("ts"):
                data["unix"] = int(p.stat().st_mtime)
        except OSError:
            pass
        return data
    return None


def summarize_record(
    rec: Optional[Dict[str, Any]],
    *,
    now: Optional[float] = None,
    default_cooldown_min: int = _DEFAULT_COOLDOWN_MIN,
) -> Dict[str, Any]:
    """Build a JSON-safe summary for one instance."""
    now = time.time() if now is None else float(now)
    if not rec:
        return {
            "has_record": False,
            "cooldown_active": False,
            "cooldown_left_sec": 0,
            "cooldown_left_min": 0,
            "age_sec": None,
            "reason": None,
            "ts": None,
            "port": None,
            "window_sec": None,
            "window_sla_sec": _window_sla_sec(),
            "window_sla_breach": False,
            "path": None,
        }
    started = 0.0
    unix = rec.get("unix")
    try:
        if unix is not None:
            started = float(unix)
    except (TypeError, ValueError):
        started = 0.0
    if not started:
        ts = rec.get("ts")
        if isinstance(ts, str) and ts:
            try:
                from datetime import datetime

                started = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            except Exception:
                started = 0.0
    if not started:
        try:
            started = float(Path(str(rec.get("_path") or "")).stat().st_mtime)
        except Exception:
            started = 0.0

    try:
        cd_min = int(rec.get("cooldown_min") or default_cooldown_min)
    except (TypeError, ValueError):
        cd_min = default_cooldown_min
    if cd_min < 0:
        cd_min = 0
    age = max(0.0, now - started) if started else None
    left = 0.0
    active = False
    if age is not None and cd_min > 0:
        left = max(0.0, cd_min * 60.0 - age)
        active = left > 0
    sla_sec = _window_sla_sec()
    window_sec = rec.get("window_sec")
    win_val: Optional[int] = None
    try:
        if window_sec is not None:
            win_val = int(window_sec)
    except (TypeError, ValueError):
        win_val = None
    sla_breach = win_val is not None and win_val > sla_sec
    return {
        "has_record": True,
        "cooldown_active": active,
        "cooldown_left_sec": int(left),
        "cooldown_left_min": int((left + 59) // 60) if left > 0 else 0,
        "cooldown_min": cd_min,
        "age_sec": int(age) if age is not None else None,
        "reason": rec.get("reason") or rec.get("source"),
        "ts": rec.get("ts"),
        "port": rec.get("port"),
        "window_sec": win_val if win_val is not None else window_sec,
        "window_sla_sec": sla_sec,
        "window_sla_breach": sla_breach,
        "host": rec.get("host"),
        "data_root": rec.get("data_root"),
        "path": rec.get("_path"),
    }


def collect_restart_status(
    *,
    instances: tuple = _INSTANCES,
    now: Optional[float] = None,
    default_cooldown_min: int = _DEFAULT_COOLDOWN_MIN,
) -> Dict[str, Any]:
    """Snapshot for ops card / API (cooldown + live http_phase + flap)."""
    now = time.time() if now is None else float(now)
    live_by_id: Dict[str, Dict[str, Any]] = {}
    snap = load_status_snapshot()
    snap_age = None
    sp = status_snapshot_path()
    if sp:
        try:
            snap_age = max(0.0, now - float(sp.stat().st_mtime))
        except OSError:
            snap_age = None
    if snap:
        for row in snap.get("instances") or []:
            if isinstance(row, dict) and row.get("id"):
                live_by_id[str(row["id"])] = row
    items = []
    any_active = False
    any_flap = False
    any_warming = False
    any_sla_breach = False
    for inst in instances:
        rec = load_instance_record(inst)
        summary = summarize_record(
            rec, now=now, default_cooldown_min=default_cooldown_min
        )
        summary["id"] = inst
        summary["name"] = "ChatX" if inst == "zhiliao" else (
            "LingoX" if inst == "tongyi" else inst
        )
        live = live_by_id.get(inst) or {}
        summary["http_phase"] = live.get("http_phase")
        summary["login_ready"] = bool(live.get("login_ready"))
        summary["login"] = live.get("login")
        summary["http"] = live.get("http")
        summary["proc_age_sec"] = live.get("proc_age_sec")
        # Prefer ASCII note_en (Phase8) so ops never depends on console codepage.
        summary["live_note"] = live.get("note_en") or live.get("note")
        summary["live_note_zh"] = live.get("note")
        flap = flap_for_instance(inst, now=now)
        summary["flap"] = flap
        if summary["cooldown_active"]:
            any_active = True
        if flap.get("flapping"):
            any_flap = True
        if summary.get("http_phase") == "warming":
            any_warming = True
        if summary.get("window_sla_breach"):
            any_sla_breach = True
        items.append(summary)
    bt = boot_timing_snapshot()
    return {
        "ok": True,
        "cooldown_active_any": any_active,
        "flapping_any": any_flap,
        "warming_any": any_warming,
        "window_sla_breach_any": any_sla_breach,
        "window_sla_sec": _window_sla_sec(),
        "status_snapshot_age_sec": int(snap_age) if snap_age is not None else None,
        "dirs": [str(p) for p in cooldown_dirs()],
        "instances": items,
        # Phase 12b: THIS process's boot phase timing (None until first boot on
        # Phase11+ code; other instance's boot lives in its own process/API).
        "boot_timing": bt,
        "boot_seat_ready_sec": _boot_seat_ready_sec(bt) if bt else None,
        "ts": now,
    }
