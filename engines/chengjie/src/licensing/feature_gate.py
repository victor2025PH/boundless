"""C0-3b 档位功能闸门（feature gate）——「谁的档位能用哪些功能」的单一事实源。

背景（融合实例 P1，2026-07）
==========================
智聊(companion 全家桶) 与 通译(翻译客服) 合并为单实例后，功能差异由授权档位
（community/basic/pro/flagship）表达。license_manager 只认 license 里显式的
``features`` 位（C0-1 注释里预留的 C0-3 gating 就是本模块），缺「档位 → 默认
功能位」映射——本模块补齐，并给四个接线点（nav 可见性 / API 路由守卫 /
worker 启动闸 / 状态快照）提供统一判定。

判定规则（``feature_enabled``）
==============================
1. 总开关 ``licensing.feature_gate.enabled``（默认 **false** = 全功能放行，
   零行为变更——与「新子系统默认关」纪律一致）；
2. 档位来源：``licensing.feature_gate.plan_override``（厂商自营/灰度用，
   无视 license.plan）> license.plan（active/grace）> community（未授权/
   过期超宽限/篡改）；
3. license 显式 ``features`` 位**优先于**档位默认（可越档单点授予、也可
   低于档位单点禁用；仅 licensed 状态下生效）；
4. 未注册进 ``FEATURE_MIN_PLAN`` 的功能名 → 恒放行（fail-open：新子系统
   不会因为忘了登记而被静默锁死）；
5. 本模块任何异常 → 放行（宁可漏锁不可锁死生产）。

接线点
======
- API 路由守卫：``src/web/admin.py::_feature_gate_guard``（前缀表
  ``API_FEATURE_PREFIXES``，403 + ``err.lic.feature_locked``）；
- nav 可见性：``src/web/nav_schema.py::get_nav_context(config)``（锁定项直接
  不渲染；P3 改为「锁标 + 升级引导」展示）；
- worker 启动闸：autosend（ai_autosend）/ proactive_topic（companion）/
  RPA 构建（rpa），见 bootstrap/*；
- 快照：``gate_snapshot()`` 供会员中心/状态 API 读数。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 档位顺序（低 → 高）。未知档位按 community 处理（fail-safe，见 plan_rank）。
PLAN_ORDER: Tuple[str, ...] = ("community", "basic", "pro", "flagship")
_PLAN_RANK = {p: i for i, p in enumerate(PLAN_ORDER)}

# 功能注册表：功能名 → 解锁所需最低档位。
# 与《融合方案》功能矩阵一一对应；改动请同步 会员中心矩阵展示 与 门禁测试。
FEATURE_MIN_PLAN: Dict[str, str] = {
    # basic：翻译套件（翻译记忆 / 术语表 / 多引擎 / 入站自动翻译配置面）
    "translation_suite": "basic",
    # pro：AI 拟稿 + 受控自动发送（L2-L4 人审链）与经营面
    "ai_autosend": "pro",
    "kb": "pro",
    "personas": "pro",
    "care": "pro",
    "analytics": "pro",
    # workflows 的强制在模块自身闸门（unified_inbox_workflow_routes._require_workflows，
    # 链域含 conv 级路径无法用前缀表表达且该闸已覆盖全部 10 端点 + NBA 链分支）；
    # 这里登记供档位判定与会员中心矩阵展示（同 white_label 的「登记+异地强制」先例）。
    "workflows": "pro",
    # flagship：陪伴全家桶 / 语音克隆 / RPA 真机 / 命理 / 变现 / 白标
    "companion": "flagship",
    "voice_clone": "flagship",
    "rpa": "flagship",
    "bazi": "flagship",
    "monetization": "flagship",
    # white_label 的真正强制在 branding（gate.feature_allowed，需 licensing.enforce）；
    # 这里登记仅供会员中心矩阵展示，不重复强制（防双源口径漂移）。
    "white_label": "flagship",
}

# API 前缀 → 功能名（最长前缀优先匹配；路由均直挂 @app 无 APIRouter prefix，
# 故用路径前缀表在中间件单点强制，避免逐路由散落装饰器）。
API_FEATURE_PREFIXES: Tuple[Tuple[str, str], ...] = (
    ("/api/line-rpa/", "rpa"),
    ("/api/messenger-rpa/", "rpa"),
    ("/api/whatsapp-rpa/", "rpa"),
    ("/api/companion/", "companion"),
    ("/api/voice/", "voice_clone"),
    ("/api/monetize/", "monetization"),
    ("/api/kb/", "kb"),
    ("/api/personas/", "personas"),
    ("/api/care/", "care"),
)


def plan_rank(plan: str) -> int:
    """档位 → 序数。未知档位按 community（0）——档位串只来自我方履约表，
    未知即笔误，宁可保守也不静默放大权限。"""
    return _PLAN_RANK.get(str(plan or "").strip().lower(), 0)


def _gate_cfg(config: Optional[dict]) -> dict:
    lic = ((config or {}).get("licensing") or {}) if isinstance(config, dict) else {}
    fg = lic.get("feature_gate") or {}
    return fg if isinstance(fg, dict) else {}


def gate_enabled(config: Optional[dict]) -> bool:
    """总开关：``licensing.feature_gate.enabled``，默认关（= 全功能，零变化）。"""
    try:
        return bool(_gate_cfg(config).get("enabled", False))
    except Exception:
        return False


def _load_status(status: Any = None) -> Any:
    if status is not None:
        return status
    from src.licensing.license_manager import get_license_manager

    return get_license_manager().status()


def effective_plan(config: Optional[dict] = None, status: Any = None) -> str:
    """当前生效档位：plan_override > license.plan（active/grace）> community。"""
    try:
        override = str(_gate_cfg(config).get("plan_override") or "").strip().lower()
        if override in _PLAN_RANK:
            return override
        st = _load_status(status)
        if getattr(st, "licensed", False):
            plan = str(getattr(st, "plan", "") or "").strip().lower()
            return plan if plan in _PLAN_RANK else "community"
        return "community"
    except Exception:
        logger.debug("[feature-gate] 档位解析失败，按 community", exc_info=True)
        return "community"


def feature_enabled(
    name: str, config: Optional[dict] = None, status: Any = None
) -> bool:
    """功能是否解锁（规则见模块 docstring；异常恒放行）。"""
    try:
        min_plan = FEATURE_MIN_PLAN.get(str(name or ""))
        if min_plan is None:
            return True  # 未注册功能 fail-open
        if not gate_enabled(config):
            return True
        st = None
        override = str(_gate_cfg(config).get("plan_override") or "").strip().lower()
        if override not in _PLAN_RANK:
            st = _load_status(status)
            # license 显式功能位优先（仅 licensed 状态；可越档授予/单点禁用）
            if getattr(st, "licensed", False):
                feats = getattr(st, "features", None) or {}
                if isinstance(feats, dict) and name in feats:
                    return bool(feats.get(name))
        else:
            st = status  # override 生效时无需读单例
        plan = effective_plan(config, st)
        return plan_rank(plan) >= plan_rank(min_plan)
    except Exception:
        logger.debug("[feature-gate] 功能判定异常，放行 %s", name, exc_info=True)
        return True


def locked_features(config: Optional[dict] = None, status: Any = None) -> List[str]:
    """当前被锁定的功能名列表（gate 关 → 空表）。"""
    try:
        if not gate_enabled(config):
            return []
        return sorted(
            n for n in FEATURE_MIN_PLAN
            if not feature_enabled(n, config, status)
        )
    except Exception:
        return []


def feature_for_api_path(path: str) -> Optional[str]:
    """API 路径 → 所属功能名（最长前缀优先）；不在表内 → None（不守卫）。"""
    p = str(path or "")
    best: Optional[Tuple[str, str]] = None
    for prefix, feat in API_FEATURE_PREFIXES:
        if p.startswith(prefix) or p == prefix.rstrip("/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, feat)
    return best[1] if best else None


def gate_snapshot(config: Optional[dict] = None, status: Any = None) -> Dict[str, Any]:
    """状态快照（会员中心 / ops 读数用，无敏感字段）。"""
    try:
        enabled = gate_enabled(config)
        override = str(_gate_cfg(config).get("plan_override") or "").strip().lower()
        st = _load_status(status)
        plan = effective_plan(config, st)
        source = (
            "override" if override in _PLAN_RANK
            else ("license" if getattr(st, "licensed", False) else "community")
        )
        feats = {
            n: {
                "allowed": (not enabled) or feature_enabled(n, config, st),
                "min_plan": FEATURE_MIN_PLAN[n],
            }
            for n in sorted(FEATURE_MIN_PLAN)
        }
        return {
            "enabled": enabled,
            "plan": plan,
            "plan_source": source,
            "license_state": str(getattr(st, "state", "") or ""),
            "locked": [n for n, v in feats.items() if not v["allowed"]],
            "features": feats,
            "plan_order": list(PLAN_ORDER),
        }
    except Exception:
        logger.debug("[feature-gate] 快照生成失败", exc_info=True)
        return {
            "enabled": False, "plan": "community", "plan_source": "error",
            "license_state": "", "locked": [], "features": {},
            "plan_order": list(PLAN_ORDER),
        }
