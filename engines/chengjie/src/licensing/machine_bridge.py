"""机器指纹桥：按文件路径惰性加载 ``platform/licensing/machine_id.py``。

与 ``src/integrations/credpool_bridge`` 同款做法——`platform/` 在引擎目录之外、不是
可 import 的包，故按路径加载；冻结态（桌面 `backend.exe`）它落在
``sys._MEIPASS/platform/licensing/``，打包清单必须显式带上（见
`desktop/build/build_backend.py` 的 DATAS，那里已有 credpool 的先例）。

加载失败一律软降级为「拿不到指纹」：绑机校验因此**放行**（见 `license_manager`），
体验档退化为不带机器归属。授权体系绝不能因为一个可选模块缺失就把用户锁在门外。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: 本产品的显式锚点 env（运维给虚机固定指纹用），外加集团默认名
ENV_VARS = ("AITR_MACHINE_ID", "BOUNDLESS_MACHINE_ID")

_MOD: Any = None
_LOAD_FAILED = False


def _path_candidates() -> List[Path]:
    import sys

    out: List[Path] = []
    override = (os.environ.get("BOUNDLESS_MACHINE_ID_PATH") or "").strip()
    if override:
        out.append(Path(override))
    rel = Path("platform") / "licensing" / "machine_id.py"
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:                                     # 冻结态（桌面版）
        out.append(Path(meipass) / rel)
    # 源码态：engines/chengjie/src/licensing/… → 仓库根
    out.append(Path(__file__).resolve().parents[4] / rel)
    out.append(Path(__file__).resolve().parents[2] / rel)   # 引擎内 vendor 兜底
    return out


def _module() -> Any:
    global _MOD, _LOAD_FAILED
    if _MOD is not None or _LOAD_FAILED:
        return _MOD
    path = next((p for p in _path_candidates() if p.is_file()), None)
    if path is None:
        _LOAD_FAILED = True
        logger.warning("[machine_id] 未找到 platform/licensing/machine_id.py，绑机校验将放行")
        return None
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_boundless_machine_id", str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load machine_id from {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _MOD = mod
    except Exception:  # noqa: BLE001
        _LOAD_FAILED = True
        logger.warning("[machine_id] 加载失败，绑机校验将放行", exc_info=True)
    return _MOD


def reset_machine_bridge() -> None:
    """测试钩子：清缓存，让下一次调用重新解析路径。"""
    global _MOD, _LOAD_FAILED
    _MOD = None
    _LOAD_FAILED = False


def machine_fingerprint() -> str:
    """本机指纹；模块不可用时返回空串。"""
    mod = _module()
    if mod is None:
        return ""
    try:
        return str(mod.machine_fingerprint(env_vars=ENV_VARS) or "")
    except Exception:  # noqa: BLE001
        logger.debug("[machine_id] 取指纹失败", exc_info=True)
        return ""


def machine_matches(bound: str) -> Optional[bool]:
    """绑定指纹是否指向本机。

    返回 ``None`` = 无法判定（模块缺失/异常）——调用方应据此**放行**，
    宁可漏拦也不能把正当用户锁死在「授权绑到别的机器」上。
    """
    b = str(bound or "").strip()
    if not b or b == "*":
        return True
    mod = _module()
    if mod is None:
        return None
    try:
        return bool(mod.matches_this_machine(b, env_vars=ENV_VARS))
    except Exception:  # noqa: BLE001
        logger.debug("[machine_id] 绑机匹配失败", exc_info=True)
        return None


def machine_short() -> str:
    """指纹短码（拼 lic_id / 日志用）；取不到时返回 ``unknown``。"""
    fp = machine_fingerprint()
    if not fp:
        return "unknown"
    mod = _module()
    try:
        return str(mod.short(fp)) if mod is not None else fp.replace("-", "")[:8]
    except Exception:  # noqa: BLE001
        return fp.replace("-", "")[:8]
