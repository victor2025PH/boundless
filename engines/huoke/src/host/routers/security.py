# -*- coding: utf-8 -*-
"""安全密钥管理与视觉自动化路由。"""
from fastapi import APIRouter, HTTPException, Depends
from .auth import verify_api_key

router = APIRouter(tags=["security"], dependencies=[Depends(verify_api_key)])


# ── Security API ──


@router.get("/security/secrets/keys")
def sec_list_keys():
    """List stored secret key names (not values)."""
    from src.observability.security import get_secure_store
    return {"keys": get_secure_store().list_keys()}


@router.post("/security/secrets")
def sec_set_secret(body: dict):
    """Store an encrypted secret."""
    key = body.get("key", "")
    value = body.get("value", "")
    if not key or not value:
        raise HTTPException(status_code=400, detail="key and value required")
    from src.observability.security import get_secure_store
    get_secure_store().set(key, value)
    return {"ok": True, "key": key}


@router.delete("/security/secrets/{key}")
def sec_delete_secret(key: str):
    """Delete a stored secret."""
    from src.observability.security import get_secure_store
    if not get_secure_store().delete(key):
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}


@router.post("/security/validate")
def sec_validate_config(body: dict):
    """Validate a configuration file."""
    config_type = body.get("type", "")
    config_data = body.get("config", {})
    from src.observability.security import ConfigValidator
    validators = {
        "devices": ConfigValidator.validate_devices,
        "compliance": ConfigValidator.validate_compliance,
        "ai": ConfigValidator.validate_ai,
    }
    fn = validators.get(config_type)
    if not fn:
        raise HTTPException(status_code=400, detail=f"Unknown config type: {config_type}")
    errors = fn(config_data)
    return {"valid": len(errors) == 0, "errors": errors}


# ── Vision & App Registry API ──


@router.get("/vision/apps")
def vision_list_apps():
    """List all registered apps (from YAML configs)."""
    from src.app_automation.app_registry import get_app_registry
    return get_app_registry().list_apps()


@router.post("/vision/apps/reload")
def vision_reload_apps():
    """Reload app configs from disk."""
    from src.app_automation.app_registry import get_app_registry
    get_app_registry().reload()
    return {"ok": True}


@router.get("/vision/selectors/{package}")
def vision_selector_stats(package: str):
    """Get learned selector stats for a package."""
    from src.vision.auto_selector import get_auto_selector
    return get_auto_selector().store.stats(package)


@router.get("/vision/selectors")
def vision_all_selectors():
    """List all packages with learned selectors."""
    from src.vision.auto_selector import get_auto_selector
    store = get_auto_selector().store
    return {"packages": store.list_packages()}


@router.delete("/vision/selectors/{package}")
def vision_invalidate_selectors(package: str, target: str = ""):
    """Invalidate (clear) learned selectors for a package or specific target."""
    from src.vision.auto_selector import get_auto_selector
    auto = get_auto_selector()
    auto.invalidate(package, target if target else None)
    return {"ok": True, "package": package,
            "target": target or "(all)"}


@router.post("/vision/execute")
def vision_execute_action(body: dict):
    """
    Execute an action on a registered app.

    Body: {"app": "facebook", "action": "send_message",
           "params": {"recipient": "John", "message": "Hi!"},
           "device_id": "ABC123"}
    """
    app_name = body.get("app", "")
    action_name = body.get("action", "")
    params = body.get("params", {})
    device_id = body.get("device_id", "")

    if not app_name or not action_name:
        raise HTTPException(status_code=400, detail="app and action required")

    from src.app_automation.app_registry import get_app_registry
    registry = get_app_registry()
    plugin = registry.get_plugin(app_name)
    if not plugin:
        raise HTTPException(status_code=404,
                            detail=f"App not found: {app_name}")

    if device_id:
        plugin.set_current_device(device_id)

    result = plugin.execute_action(action_name, params)
    return {
        "success": result.success,
        "flow": result.flow_name,
        "steps_completed": result.steps_completed,
        "total_elapsed": round(result.total_elapsed, 2),
        "error": result.error,
        "steps": [
            {"target": s.target, "action": s.action,
             "success": s.success, "elapsed": round(s.elapsed, 2)}
            for s in result.step_results
        ],
    }
