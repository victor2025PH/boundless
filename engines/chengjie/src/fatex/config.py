"""FateX 配置门面：新命名空间 ``fatex.*`` 优先，旧 ``companion.bazi.*`` 兼容兜底。

产品分离后配置归属顶层 ``fatex``；但现网 overlay 已开 ``companion.bazi.enabled``，
迁移期两处都认（deep-merge，新键逐层覆盖旧键），任何一处开着都不回退——
运营改配置只动 ``fatex.*``，旧键留给存量部署自然过渡。
"""
from __future__ import annotations

from typing import Any, Dict


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = dict(base or {})
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def fatex_cfg(full_config: Any) -> Dict[str, Any]:
    """FateX 生效配置合并视图（``companion.bazi`` 为底、顶层 ``fatex`` 覆盖）。

    入参可为完整 config dict 或带 ``.config`` 属性的 Config 对象；异常返 ``{}``。
    """
    try:
        cfg = full_config
        if not isinstance(cfg, dict):
            cfg = getattr(full_config, "config", None) or {}
        legacy = ((cfg.get("companion") or {}).get("bazi") or {})
        modern = (cfg.get("fatex") or {})
        if not isinstance(legacy, dict):
            legacy = {}
        if not isinstance(modern, dict):
            modern = {}
        return _deep_merge(legacy, modern)
    except Exception:
        return {}


def fatex_enabled(full_config: Any) -> bool:
    """产品总开关（新旧命名空间合并后的 enabled）。"""
    return bool(fatex_cfg(full_config).get("enabled", False))
