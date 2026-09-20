# -*- coding: utf-8 -*-
"""这个实例是否必须跑在交互桌面会话里？打印 ``1``/``0``，退出码同值取反（0=需要）。

用法：``python needs_interactive_session.py <数据根>``

判据只有一个：实例是否要驱动**本机桌面上的 GUI**——目前即个人微信 PC 副驾
（``platform_login.wechat_pc`` 有档位，或 ``inbox.l2_autosend.desktop_bridge.enabled``）。
Windows 的窗口站按会话隔离：S4U/SYSTEM 计划任务拉起的引擎落在 session 0，
那里没有用户桌面，``EnumWindows``/UIA 永远枚举不到 session 1 的微信——
2026-09-20 实测同一段代码在 session 1 找到 14 个微信窗口、在 session 0 找到 0 个，
副驾于是一直报「微信未运行」。

**判据要窄**：交互会话是把双刃剑，用户注销会带走整个会话里的进程。不驱动桌面的
实例留在 session 0 更抗注销，所以这里宁可答「否」——拿不到配置、YAML 读不动、
键不存在，一律 ``0``，绝不抛。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict


def _load(path: Path) -> Dict[str, Any]:
    try:
        import yaml
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _dig(cfg: Dict[str, Any], *keys: str) -> Any:
    cur: Any = cfg
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def needs_interactive(cfg: Dict[str, Any]) -> bool:
    """配置 → 是否需要交互桌面（纯函数，可单测）。"""
    pc = _dig(cfg, "platform_login", "wechat_pc")
    if isinstance(pc, dict) and str(pc.get("tier") or "").strip():
        return True
    return bool(_dig(cfg, "inbox", "l2_autosend", "desktop_bridge", "enabled"))


def instance_needs_interactive(data_root: str) -> bool:
    """``<数据根>\\config\\`` 的 config.yaml + config.local.yaml（后者覆盖前者）。

    只浅合并到关心的两支：这里不是配置加载器，多一分通用就多一分与引擎口径漂移的风险。
    """
    cfg_dir = Path(data_root) / "config"
    base, local = _load(cfg_dir / "config.yaml"), _load(cfg_dir / "config.local.yaml")
    return needs_interactive(base) or needs_interactive(local)


if __name__ == "__main__":
    want = instance_needs_interactive(sys.argv[1] if len(sys.argv) > 1 else "")
    print("1" if want else "0")
    sys.exit(0 if want else 1)
