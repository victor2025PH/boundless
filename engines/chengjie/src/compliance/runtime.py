"""合规开关的进程级读取面（WP-4）。

为什么存在：诚实身份要传导进 ``PersonaManager`` 的 prompt 组装（约束块 + 身份
硬锁），但 ``format_persona_block`` 的调用点散在多条链（skill_manager A/B 线等
高冲突热区）——逐点加参数＝把一个布尔穿透十几个签名。本模块用「注入而非全局
导入」的仓库惯例反过来收口：装配层（``create_app``）注册一个 **config provider**
（指向 ConfigManager 的实时合并配置，天然跟随 overlay 热重载），下游在消费点
惰性读取。provider 未注册（单测 / legacy 装配 / CLI）＝一律 False，行为与
合规模式不存在时逐字节一致。

**平台作用域**（实施97，2026-09-07）：微信客服等 ``FORCED_COMPLIANCE_PLATFORMS`` 上
披露/诚实身份**恒开**。同样为了不把 platform 穿透十几个签名，用 ``contextvars``：
出稿入口（``persona_reply.generate_persona_reply``）以 :func:`platform_scope` 打标，
链路内所有 ``honest_identity_active()`` / ``notice_active()`` 读到即生效——asyncio 任务
与 ``asyncio.to_thread`` 都会拷贝上下文，prompt 组装/出站守卫无需改签名。

绝不抛：任何异常按「开关关」处理——合规读取面自身故障不得影响出话链。
"""
from __future__ import annotations

import contextvars
import logging
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger(__name__)

_PROVIDER: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
_PLATFORM: contextvars.ContextVar[str] = contextvars.ContextVar("compliance_platform", default="")


def set_config_provider(fn: Callable[[], Optional[Dict[str, Any]]]) -> None:
    """装配层注册实时配置来源（幂等，后注册覆盖先注册）。"""
    global _PROVIDER
    _PROVIDER = fn


def runtime_config() -> Dict[str, Any]:
    """当前生效配置（provider 未注册/异常 → 空 dict）。"""
    try:
        if _PROVIDER is None:
            return {}
        cfg = _PROVIDER()
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("[compliance] config provider 异常（按空配置处理）",
                     exc_info=True)
        return {}


@contextmanager
def platform_scope(platform: Any) -> Iterator[None]:
    """把「当前在为哪个平台出稿/出站」放进上下文（嵌套安全；空值＝不改变现状）。"""
    p = str(platform or "").strip().lower()
    if not p:
        yield
        return
    token = _PLATFORM.set(p)
    try:
        yield
    finally:
        try:
            _PLATFORM.reset(token)
        except Exception:
            pass


def current_platform() -> str:
    try:
        return _PLATFORM.get()
    except Exception:
        return ""


def honest_identity_active() -> bool:
    """``compliance.disclosure.honest_identity`` 实时值（缺 provider 恒 False；强制平台恒 True）。"""
    from src.compliance import honest_identity_enabled

    return honest_identity_enabled(runtime_config(), platform=current_platform())


def notice_active() -> bool:
    """``compliance.disclosure.notice`` 实时值（缺 provider 恒 False；强制平台恒 True）。"""
    from src.compliance import notice_enabled

    return notice_enabled(runtime_config(), platform=current_platform())


def _reset_for_tests() -> None:
    global _PROVIDER
    _PROVIDER = None


__all__ = [
    "set_config_provider",
    "runtime_config",
    "platform_scope",
    "current_platform",
    "honest_identity_active",
    "notice_active",
]
