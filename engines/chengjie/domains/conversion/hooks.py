"""Conversion / companion domain hooks — defaults only.

N-3 #241（2026-09-08）：同一个域包按业务域（``business_domain``）注册不同的 hook 类。
陪伴运营的机器上注册 :class:`CompanionDomainHook`，销售部署仍是
:class:`ConversionDomainHook`；两者都只是 DomainHook 缺省行为，区别在于启动日志
``Domain hook registered: …`` 如实反映这台机器做的是什么生意（skuio 3NVM7Q 实锤：
陪伴人设配 ConversionDomainHook 是域包错配的第一条线索）。
"""

from src.hooks.base import DomainHook


class ConversionDomainHook(DomainHook):
    """Sales / conversion business domain: base DomainHook defaults."""

    def __init__(self, config=None):
        self._config = config


class CompanionDomainHook(DomainHook):
    """Companion business domain: base DomainHook defaults."""

    def __init__(self, config=None):
        self._config = config


# DomainLoader._load_hooks 按 pack.business_domain 查表；缺省回落模块内第一个子类
HOOKS_BY_BUSINESS_DOMAIN = {
    "companion": CompanionDomainHook,
    "sales": ConversionDomainHook,
}
