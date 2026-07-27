"""机器指纹（跨引擎共用的瘦实现，stdlib only）。

定位
====
「一机一试用 / 授权绑机」需要一个**跨重启、换网、换网卡都不变**的机器标识。本模块是
集团底座里那一份唯一实现，供各引擎经文件路径惰性加载（与 ``platform/credpool`` 同款，
不做包安装、不引第三方依赖）。

设计要点
========
- **锚点优先级**：显式 env > Windows ``MachineGuid`` > (GUID|MAC|node) 兜底。
  MAC 会随虚拟网卡/Docker/VPN 漂移，**不进主指纹**，只在兜底串里作盐。
- **不可逆**：对外只给 SHA256 派生的 ``XXXX-XXXX-XXXX-XXXX``，不吐原始 GUID/MAC。
  用户能看见、能报给客服，但拿不到硬件序列号明文——这条要写进隐私说明。
- **salt 参数化**：不同产品线可以各自命名空间（互不交叉关联），也可以传入历史 salt
  以复刻已签发授权的指纹。`engines/avatarhub/license.py` 用的是
  ``avatarhub-fp-v1``，把它作为参数传进来即可复用本模块而不失配存量授权。
- **候选集**：``machine_fingerprints()`` 返回可接受的指纹列表（主 + legacy）。
  授权匹配用它而非单值——换网后旧签发的授权仍能用，实现平滑迁移。

刻意不做的事
============
不做「防篡改」。指纹取自本机可读信息，客户端侧一定可伪造。它的作用是**去重与归属**
（服务端据此判断"这台机器是不是已经领过试用"），真正的防滥用必须在签发侧的台账上做。
把它当防破解手段用会得出错误的安全感。
"""
from __future__ import annotations

import hashlib
import os
import platform as _platform
from typing import Iterable, List, Optional

#: 集团默认命名空间。改它＝让所有已签发的绑机授权失配，等同于吊销。
DEFAULT_SALT = "boundless-fp-v1"

#: 默认显式锚点环境变量（运维给虚机/容器固定指纹用；按序取第一个非空）
DEFAULT_ENV_VARS = ("BOUNDLESS_MACHINE_ID",)

#: 站点授权通配：绑定为该值即「不限机器」
SITE_WILDCARD = "*"


def _explicit(env_vars: Iterable[str]) -> str:
    for name in env_vars:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""


def _machine_guid() -> str:
    """Windows MachineGuid —— 与网络无关、跨重启/换网稳定，指纹首选锚点。"""
    try:
        import winreg  # type: ignore[import-not-found]

        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(k, "MachineGuid")
        winreg.CloseKey(k)
        if guid:
            return str(guid).strip()
    except Exception:
        pass
    # 非 Windows：/etc/machine-id（systemd）/ ioreg 都可，此处取通用的前者
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
        except Exception:
            continue
    return ""


def _node_mac() -> str:
    try:
        import uuid as _uuid

        return "%012x" % _uuid.getnode()
    except Exception:
        return ""


def _fp_from_raw(raw: str, salt: str) -> str:
    """原始机器串 → 展示指纹（SHA256 前 16 位十六进制，四组分隔）。"""
    h = hashlib.sha256((salt + ":" + raw).encode("utf-8")).hexdigest().upper()
    s = h[:16]
    return "-".join(s[i:i + 4] for i in range(0, 16, 4))


def _raw_legacy_id() -> str:
    """兜底/兼容串 = MachineGuid|MAC（都拿不到则主机名）。

    MAC 在虚拟网卡环境会漂移，因此**只用于兜底与 legacy 候选**，不作主锚点。
    """
    parts: List[str] = []
    g = _machine_guid()
    if g:
        parts.append(g)
    m = _node_mac()
    if m:
        parts.append(m)
    if not parts:
        parts.append(_platform.node() or "unknown")
    return "|".join(parts)


def machine_fingerprint(
    *, salt: str = DEFAULT_SALT, env_vars: Optional[Iterable[str]] = None,
) -> str:
    """本机稳定指纹（展示/签发用）。"""
    envs = tuple(env_vars) if env_vars else DEFAULT_ENV_VARS
    ex = _explicit(envs)
    if ex:
        return _fp_from_raw("env:" + ex, salt)
    g = _machine_guid()
    if g:
        return _fp_from_raw(g, salt)
    return _fp_from_raw(_raw_legacy_id(), salt)


def machine_fingerprints(
    *, salt: str = DEFAULT_SALT, env_vars: Optional[Iterable[str]] = None,
) -> List[str]:
    """本机**可接受**的指纹候选集（授权匹配用），去重保序。

    主指纹在前；legacy 串派生的指纹在后——这样换网卡后新签发的授权永不失配，
    而按旧口径签发的授权在 MAC 未变时仍可继续用，无需立刻重签。
    """
    out = [machine_fingerprint(salt=salt, env_vars=env_vars)]
    legacy = _fp_from_raw(_raw_legacy_id(), salt)
    if legacy not in out:
        out.append(legacy)
    return out


def matches_this_machine(
    bound: str, *, salt: str = DEFAULT_SALT,
    env_vars: Optional[Iterable[str]] = None,
) -> bool:
    """``bound`` 指纹是否指向本机。空串＝未绑机（视为匹配）；``*``＝站点授权。"""
    b = str(bound or "").strip()
    if not b or b == SITE_WILDCARD:
        return True
    return b in machine_fingerprints(salt=salt, env_vars=env_vars)


def short(fingerprint: str, groups: int = 2) -> str:
    """指纹短码（日志/lic_id 用；默认前两组 = 8 个十六进制字符）。"""
    parts = str(fingerprint or "").split("-")
    return "".join(parts[:max(1, groups)])
