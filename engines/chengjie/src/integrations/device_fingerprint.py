"""每账号设备指纹（协议多开的防聚类连坐一环）。

为什么只发凭据不够：Telegram 封的往往不是单个号，而是**簇**。同一批号如果
device_model / system_version / app_version 完全一致（pyrogram 默认值就是全网
统一的那一串），即使各自换了 api_id 和 IP，仍会被判定为「同一套软件跑出来的」。
所以隔离要三件套齐上：独立凭据 + 独立出口 IP + **独立设备指纹**。

三条设计约束（都踩过坑才这么定）：

1. **必须按账号稳定**：指纹随 `device_seed` 确定性派生，同一账号每次连接给出
   完全相同的值。会话建立后忽然换设备型号，等于告诉服务端「这台机器变了」。
2. **必须内部自洽**：型号 / 系统版本 / 客户端版本三者要像同一台真机
   （Android 机型配 Android 版本、Windows 机型配 Windows 版本），
   混搭（iOS 机型 + Android 版本）反而是明显的伪造特征。
3. **只对新账号生效**：存量账号早已用 pyrogram 默认值建立会话，贸然改指纹
   是无谓的风险。仅当账号 meta 里有 `device_seed`（本功能上线后扫码的号）
   才应用——没有就保持原行为。

纯函数、零依赖、可单测。
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any, Dict, Optional

#: 账号注册表 meta 里存放指纹种子的字段名（跨版本契约，改名=存量账号丢指纹）
META_KEY = "device_seed"

#: 解析后的指纹本体。**种子不够**——机型表一旦刷新（版本号会变旧），同一种子
#: 派生的结果就变了，等于把所有存量账号的设备身份悄悄换掉，正好违反"必须恒定"
#: 这条铁律。故落库存的是**结果**，机型表从此可以自由演进而不影响已有账号。
META_FP_KEY = "device_fp"

_FP_FIELDS = ("device_model", "system_version", "app_version")

#: 桌面档位（协议登录走的是「关联桌面设备」，故只用桌面机型，与登录语义自洽）。
#: ⚠️ app_version 会随时间变旧——全网都在 5.x 时还报 4.x 本身就是一种特征。
#: 建议每半年对照官方客户端实际版本刷新一次本表。**刷新是安全的**：已登录账号的
#: 指纹本体存在 meta（见 META_FP_KEY），只有此后新扫码的号才会用到新表。
_DESKTOP_PROFILES = (
    ("Desktop", "Windows 10 x64", ("4.16.8", "5.1.7", "5.3.2")),
    ("Desktop", "Windows 11 x64", ("5.1.7", "5.3.2", "5.5.1")),
    ("PC 64bit", "Windows 10 x64", ("4.16.8", "5.1.7")),
    ("PC 64bit", "Windows 11 x64", ("5.3.2", "5.5.1")),
    ("MacBook Pro", "macOS 14.5", ("10.9.3", "11.2.1")),
    ("MacBook Air", "macOS 13.6", ("10.9.3", "11.0.1")),
    ("iMac", "macOS 14.4", ("10.9.3",)),
    ("Desktop", "Ubuntu 22.04", ("4.16.8", "5.1.7")),
)


def new_device_seed() -> str:
    """首次扫码时生成，写进账号 meta 后终生不变。"""
    return secrets.token_hex(8)


def _digest(seed: str) -> bytes:
    return hashlib.sha256(str(seed).encode("utf-8")).digest()


def build_fingerprint(seed: str) -> Dict[str, str]:
    """由种子确定性派生一套自洽的桌面客户端指纹。"""
    d = _digest(seed)
    model, system, app_versions = _DESKTOP_PROFILES[d[0] % len(_DESKTOP_PROFILES)]
    app_version = app_versions[d[1] % len(app_versions)]
    return {
        "device_model": model,
        "system_version": system,
        "app_version": app_version,
    }


def seed_of(account: Optional[Dict[str, Any]]) -> str:
    meta = (account or {}).get("meta") or {}
    return str(meta.get(META_KEY) or "")


def stored_fingerprint(account: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """读回落库的指纹；字段不全或脏数据一律视为没有（回落按种子派生）。"""
    meta = (account or {}).get("meta") or {}
    raw = meta.get(META_FP_KEY)
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k in _FP_FIELDS:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            return {}
        out[k] = v
    return out


def enabled(config: Dict[str, Any]) -> bool:
    """新子系统默认关；建议与中央凭据池一同开启（三隔离才完整）。"""
    pl = (config or {}).get("platform_login", {}) or {}
    tg = pl.get("telegram", {}) or {}
    return bool((tg.get("device_fingerprint", {}) or {}).get("enabled", False))


def client_kwargs(config: Dict[str, Any], seed: str) -> Dict[str, str]:
    """给 pyrogram Client 的指纹参数；未启用或无种子 → 空字典（保持原行为）。"""
    if not enabled(config) or not seed:
        return {}
    return build_fingerprint(seed)


def client_kwargs_for_account(
    config: Dict[str, Any], account: Optional[Dict[str, Any]]
) -> Dict[str, str]:
    """runner 侧：优先用落库的指纹本体，其次按种子派生。

    优先级不能反：落库的才是**这个账号建立会话时真正报给服务端的那一套**；
    种子派生只是没有落库时（早期账号 / meta 被截断）的兼容回落。
    """
    if not enabled(config):
        return {}
    stored = stored_fingerprint(account)
    if stored:
        return stored
    return client_kwargs(config, seed_of(account))
