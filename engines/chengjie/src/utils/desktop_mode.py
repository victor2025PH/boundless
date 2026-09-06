"""桌面客户机形态的**单一判定**（L-6 B，2026-09-06）。

背景：桌面包里多处代码把办公室内网算力当缺省值（``192.168.0.140:7854`` STT、
``192.168.0.173`` 口语改写、看门狗 LAN GPU 巡检…）。客户机上这些地址永远探不通 →
每次调用白吃连接超时、看门狗每 30 分钟一条「LAN GPU 不可达 本地冗余归零」红 toast、
转录双级返空、翻译回落备点——是多项质量问题的共同上游（skuio 机 C6EFRS「上游」三条）。

原则：**桌面客户机只认官网网关**（``hosted_gateway`` 那一层）；代码级 LAN 缺省值在
桌面形态下一律不生效；配置里**显式写了**内网地址的（内部坐席机、内测种子的
``_lan_seed`` 混合形态）不受影响——那是运维的选择，由 hosted_gateway 的可达探测决定
直连还是网关接管。

判定口径与 ``hosted_gateway._wants_hosted`` / ``env_probe._is_desktop_mode`` 一致：
env ``AITR_DESKTOP_MODE`` 真值（backend-launcher.js 设）或 config ``app.desktop_mode``。
"""

from __future__ import annotations

import os
from typing import Any, Optional

_TRUTHY = ("1", "true", "yes", "on")


def is_desktop_client(config: Optional[Any] = None) -> bool:
    """当前进程是不是桌面客户机形态（详见模块头）。

    ``config`` 可为 None（只看 env）、配置 dict、或 ConfigManager 形态（带 ``.config`` dict）。
    """
    if str(os.environ.get("AITR_DESKTOP_MODE") or "").strip().lower() in _TRUTHY:
        return True
    root = config
    if root is not None and not isinstance(root, dict):
        root = getattr(root, "config", None)
    if isinstance(root, dict):
        app = root.get("app")
        return bool(isinstance(app, dict) and app.get("desktop_mode", False))
    return False


def lan_default_or_empty(explicit: Any, lan_default: str, *, config: Optional[Any] = None) -> str:
    """LAN 缺省值的桌面闸：显式配置原样返回；未配置时桌面态返回 ``""``（＝未配置），
    非桌面态返回内置 LAN 缺省。调用方对空串按「该能力未配置」处理，不再去探内网。"""
    val = str(explicit or "").strip()
    if val:
        return val
    return "" if is_desktop_client(config) else str(lan_default or "")
