"""Feature center API (P1): make shipped-but-disabled capabilities discoverable.

Backed by the single source of truth ``src.utils.feature_registry`` (same table
that drives the desktop seed gate and the desktop baseline reconcile). Endpoints:

- ``GET  /api/setup/features``         -- localized inventory with deterministic
  states: on | available | needs_dep | locked (config-level only, no network I/O)
- ``POST /api/setup/features/toggle``  -- flip an A/B-class flag via the overlay
  write guard (``ConfigManager.set_overlay_flag``: atomic file write + immediate
  in-memory merge). C-class entries are never self-service toggleable here.

The settings page card hides itself entirely when the GET endpoint is absent
(older backend without this route), keeping template hot-updates self-consistent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Body, Depends, HTTPException, Request

from src.utils.app_identity import app_version
from src.utils.feature_registry import (
    by_key,
    feature_state,
    missing_deps,
    ui_features,
)
from src.web.web_i18n import tr

logger = logging.getLogger("ai_chat_assistant.feature_center_routes")

_ROLE_VIEWER = "viewer"


def _upgrade_lock(f, cfg: Dict[str, Any]) -> str:
    """Return the min plan name locking this feature, or "" when not locked.

    Plan requirements live in ``feature_gate.FEATURE_MIN_PLAN`` (its own single
    source of truth); the registry only carries a family reference. Fail-open on
    any licensing error -- the overview must never break because of the gate
    (mirrors feature_gate's own resilience rule).
    """
    if not getattr(f, "gate_feature", ""):
        return ""
    try:
        from src.licensing.feature_gate import (
            FEATURE_MIN_PLAN,
            feature_enabled,
            gate_enabled,
        )
        if not gate_enabled(cfg):
            return ""
        if feature_enabled(f.gate_feature, cfg):
            return ""
        return FEATURE_MIN_PLAN.get(f.gate_feature, "")
    except Exception:
        logger.debug("feature gate lookup failed (fail-open)", exc_info=True)
        return ""


def _gate_meta(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Licensing meta for the card footer (empty plan when gate is off)."""
    try:
        from src.licensing.feature_gate import effective_plan, gate_enabled
        on = gate_enabled(cfg)
        return {"gate_enabled": on,
                "plan": effective_plan(cfg) if on else ""}
    except Exception:
        return {"gate_enabled": False, "plan": ""}


def register_feature_center_routes(app, auth_dep, config_manager=None):
    """Register the feature-center endpoints."""

    def _cfg_root() -> Dict[str, Any]:
        try:
            cfg = getattr(config_manager, "config", None)
            return cfg if isinstance(cfg, dict) else {}
        except Exception:
            return {}

    def _deny_viewer(request: Request):
        try:
            role = request.session.get("role", "")
        except Exception:
            role = ""
        if role == _ROLE_VIEWER:
            raise HTTPException(403, tr(request, "err.fc.readonly"))

    def _dep_labels(request: Request, codes) -> list:
        return [tr(request, f"fc_dep_{c}") for c in codes]

    def _item(request: Request, f, cfg: Dict[str, Any]) -> Dict[str, Any]:
        state = feature_state(f, cfg)
        extra = ""
        if state in ("available", "needs_dep"):
            # Plan wall is the harder one -- show it before dependency gaps.
            # An already-enabled feature honestly stays "on": runtime
            # enforcement belongs to the feature_gate wiring, not this list.
            lock_plan = _upgrade_lock(f, cfg)
            if lock_plan:
                state = "needs_upgrade"
                extra = tr(request, "fc_rsn_upgrade", plan=lock_plan)
        if state == "needs_dep":
            joined = ", ".join(_dep_labels(request, missing_deps(f, cfg)))
            prefix = tr(request, "fc_js_missing")
            extra = f"{prefix}{joined}"
        elif state == "locked":
            extra = tr(request, f"fc_rsn_{f.reason}")
        return {
            "key": f.key,
            "slug": f.slug,
            "cls": f.cls,
            "state": state,
            "state_label": tr(request, f"fc_state_{state}"),
            "enabled": state == "on",
            "name": tr(request, f"fc_f_{f.slug}"),
            "desc": tr(request, f"fc_f_{f.slug}_d"),
            "extra": extra,
            # C-class stays read-only even when it is currently on (deployment
            # overlays own those flags; flipping them off here would desync the
            # operator's LAN topology decisions from this UI). Plan-locked
            # entries hide the button too -- the fix is an upgrade, not a click.
            "toggleable": f.cls in ("A", "B") and state != "needs_upgrade",
        }

    @app.get("/api/setup/features")
    async def setup_features(request: Request, _auth=Depends(auth_dep)):
        cfg = _cfg_root()
        out = {
            "ok": True,
            "version": app_version(),
            "features": [_item(request, f, cfg) for f in ui_features()],
        }
        out.update(_gate_meta(cfg))
        return out

    @app.post("/api/setup/features/toggle")
    async def setup_features_toggle(
        request: Request,
        payload: Dict[str, Any] = Body(...),
        _auth=Depends(auth_dep),
    ):
        _deny_viewer(request)
        key = str((payload or {}).get("key") or "").strip()
        want = bool((payload or {}).get("enabled"))
        f = by_key(key)
        if f is None or not f.show:
            raise HTTPException(404, tr(request, "err.fc.unknown", name=key))
        if f.cls == "C":
            raise HTTPException(409, tr(request, "err.fc.locked"))
        cfg = _cfg_root()
        if want:
            lock_plan = _upgrade_lock(f, cfg)
            if lock_plan:
                raise HTTPException(
                    409, tr(request, "err.fc.needs_plan", plan=lock_plan))
            miss = missing_deps(f, cfg)
            if miss:
                raise HTTPException(
                    409,
                    tr(request, "err.fc.needs_dep",
                       deps=", ".join(_dep_labels(request, miss))))
        setter = getattr(config_manager, "set_overlay_flag", None)
        if not callable(setter):
            raise HTTPException(500, tr(request, "err.fc.write_failed"))
        ok, _msg = setter(f.key, want)
        if not ok:
            raise HTTPException(500, tr(request, "err.fc.write_failed"))
        logger.info("feature toggle: %s = %s", f.key, want)
        return {"ok": True, "item": _item(request, f, _cfg_root())}
