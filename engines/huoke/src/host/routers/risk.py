# -*- coding: utf-8 -*-
"""风控与反检测路由。"""
from fastapi import APIRouter, HTTPException, Depends
from .auth import verify_api_key

router = APIRouter(prefix="/risk", tags=["risk"], dependencies=[Depends(verify_api_key)])


@router.get("/")
def risk_profiles():
    """Get risk profiles for all tracked devices."""
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()
    profiles = ac.all_profiles()
    return {
        "devices": profiles,
        "high_risk": [p for p in profiles if p["risk_level"] in ("high", "critical")],
    }


@router.get("/tuning")
def risk_tuning_status():
    """Get auto-tuning status from A/B experiment analysis."""
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()
    return ac.get_tuning_status()


@router.post("/tune")
def trigger_risk_tuning():
    """Manually trigger A/B-driven compliance auto-tuning."""
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()
    adjustments = ac.auto_tune_from_experiments()
    return {"adjustments": adjustments or {}, "status": "tuned"}


@router.get("/{device_id}")
def device_risk(device_id: str):
    """Get detailed risk profile for a specific device."""
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()
    return ac.get_risk_profile(device_id)


@router.post("/{device_id}/recover")
def trigger_recovery(device_id: str, reason: str = "manual"):
    """Manually trigger recovery mode for a device."""
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()
    ac.force_recovery(device_id, reason)
    return {"status": "recovery_started", "device_id": device_id}


@router.post("/{device_id}/exit-recovery")
def exit_recovery(device_id: str):
    """Manually exit recovery mode."""
    from src.behavior.adaptive_compliance import get_adaptive_compliance
    ac = get_adaptive_compliance()
    ac.force_exit_recovery(device_id)
    return {"status": "recovery_exited", "device_id": device_id}
