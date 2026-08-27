# -*- coding: utf-8 -*-
"""speech_prints 数据区 overlay（P1-1，2026-08-18）。

问题：说话指纹唯一真相在 ``platform/spoken_style/data/speech_prints.json``——
开发机是 git 跟踪件（运营改=弄脏共享树），**打包态在只读安装目录**（客户想给
自建人设配指纹根本改不了）；备货面板（WP-6）点名了缺口却给不出自助出口。

方案（三条纪律的交汇解）：
- **出厂件不动**（avatarhub 线所有权 + 打包只读 + 升级覆盖）；
- 运营写入落 **实例数据区 overlay** ``config/speech_prints.local.json``
  （打包态可写、升级不丢、WP-8 备份自动覆盖）；
- 包内消费（L1 prompt_block / L4 role_enabled / 改写 sysprompt）**语义零复刻**：
  桥接装载包后把 ``colloquial_rewrite._PRINTS_PATH`` 重定向到数据区的
  **物化合并文件** ``speech_prints.runtime.json``（出厂 ∪ overlay，overlay 同键
  胜出）；包自带的 mtime 热加载对合并文件天然生效。双源任一手改（avatarhub
  线在开发机直改出厂件的既有工作流）由热路 ``ensure_runtime_file`` 双 stat
  保鲜——「改此文件即时生效」语义两侧都保住。

降级语义（全部软的）：包缺席=整链 no-op（overlay 无消费者，如实闲置）；
合并物化失败=不重定向（包继续读出厂件=旧行为）；avatarhub 升级若重命名
``_PRINTS_PATH``=重定向落空回出厂行为，专项门禁点名（见 test）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

OVERLAY_FILENAME = "speech_prints.local.json"
RUNTIME_FILENAME = "speech_prints.runtime.json"
#: 物化文件里的来源指纹键（"_" 前缀=元数据，包/消费方按角色名查询不受影响，
#: persona_stock 的键枚举也跳过 "_" 键——与出厂件 "_说明" 同惯例）
_META_KEY = "_overlay_meta"

_LOCK = threading.Lock()

#: 条目 schema（与出厂件真实条目对齐：print 必填，其余可选）
_FIELD_LIMITS = {"print": 600, "example": 800, "guide": 600}
_CATCH_MAX_ITEMS = 8
_CATCH_MAX_LEN = 60
_NAME_MAX_LEN = 80


def factory_path() -> Optional[Path]:
    """出厂件路径（包发现复用桥接的向上查找；缺包 None）。"""
    try:
        from src.ai.spoken_style_bridge import _find_platform_dir
        pdir = _find_platform_dir()
        if pdir is None:
            return None
        f = Path(pdir) / "spoken_style" / "data" / "speech_prints.json"
        return f if f.is_file() else None
    except Exception:
        return None


def overlay_path() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / OVERLAY_FILENAME


def runtime_path() -> Path:
    from src.licensing.data_paths import config_dir

    return config_dir() / RUNTIME_FILENAME


def _read_json(p: Optional[Path]) -> Dict[str, Any]:
    try:
        if p is None or not Path(p).is_file():
            return {}
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logger.debug("[speech_prints_overlay] 读取失败 %s", p, exc_info=True)
        return {}


def _sig(p: Optional[Path]) -> Tuple[int, int]:
    """来源指纹 (mtime_ns, size)；缺文件 (0, -1)。"""
    try:
        st = Path(p).stat()
        return (st.st_mtime_ns, st.st_size)
    except Exception:
        return (0, -1)


def merged_view(*, factory: Any = None, overlay: Any = None) -> Dict[str, Any]:
    """合并视图：出厂 ∪ overlay（overlay 同键胜出）。参数可注入=可单测。"""
    f = _read_json(Path(factory) if factory else factory_path())
    o = _read_json(Path(overlay) if overlay else overlay_path())
    out = dict(f)
    out.update(o)
    return out


def ensure_runtime_file(*, force: bool = False, factory: Any = None,
                        overlay: Any = None, runtime: Any = None
                        ) -> Optional[Path]:
    """物化合并文件（双源指纹保鲜；无任何源=返回 None 不物化）。

    热路可安全高频调用：新鲜时只花两次 stat + 一次小 JSON 读（meta 校验）。
    """
    fp = Path(factory) if factory else factory_path()
    op = Path(overlay) if overlay else overlay_path()
    rp = Path(runtime) if runtime else runtime_path()
    fsig, osig = _sig(fp), _sig(op)
    if fsig[1] < 0 and osig[1] < 0:
        return None
    with _LOCK:
        if not force and rp.is_file():
            meta = (_read_json(rp).get(_META_KEY) or {})
            if (list(meta.get("factory") or ()) == list(fsig)
                    and list(meta.get("overlay") or ()) == list(osig)):
                return rp
        data = merged_view(factory=fp, overlay=op)
        data[_META_KEY] = {"factory": list(fsig), "overlay": list(osig),
                           "built_at": int(time.time())}
        try:
            rp.parent.mkdir(parents=True, exist_ok=True)
            tmp = rp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, rp)
        except Exception:
            logger.warning("[speech_prints_overlay] 物化失败（保持旧行为）",
                           exc_info=True)
            return None
    return rp


def install_redirect() -> bool:
    """把包内 ``_PRINTS_PATH`` 指到物化合并文件（幂等；任何失败=不重定向）。

    只在包可装载且至少一个来源存在时生效；调用方＝spoken_style_bridge 的
    包装载收口（_load 成功后）。
    """
    try:
        rp = ensure_runtime_file()
        if rp is None:
            return False
        import sys
        mod = sys.modules.get("spoken_style.colloquial_rewrite")
        if mod is None:
            try:
                from spoken_style import colloquial_rewrite as mod  # type: ignore
            except Exception:
                return False
        if not hasattr(mod, "_PRINTS_PATH"):
            logger.warning("[speech_prints_overlay] 包已无 _PRINTS_PATH（升级重命名？）"
                           "——overlay 停用，回落出厂行为")
            return False
        if getattr(mod, "_PRINTS_PATH", None) != rp:
            mod._PRINTS_PATH = rp
            logger.info("[speech_prints_overlay] 指纹源已重定向: %s", rp)
        return True
    except Exception:
        logger.debug("[speech_prints_overlay] 重定向失败（保持旧行为）",
                     exc_info=True)
        return False


def validate_entry(entry: Any) -> Dict[str, Any]:
    """条目校验+净化（schema 对齐出厂件：print 必填 str，catch list[str]，
    example/guide 可选 str；超限/未知键抛 ValueError——路由层转 400）。"""
    if not isinstance(entry, dict):
        raise ValueError("entry must be an object")
    unknown = set(entry.keys()) - {"print", "catch", "example", "guide"}
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    out: Dict[str, Any] = {}
    pr = str(entry.get("print") or "").strip()
    if not pr:
        raise ValueError("field 'print' is required")
    for k, cap in _FIELD_LIMITS.items():
        v = str(entry.get(k) or "").strip()
        if len(v) > cap:
            raise ValueError(f"field '{k}' too long (>{cap})")
        if v:
            out[k] = v
    catch = entry.get("catch")
    if catch:
        if not isinstance(catch, list):
            raise ValueError("field 'catch' must be a list")
        if len(catch) > _CATCH_MAX_ITEMS:
            raise ValueError(f"too many catch phrases (>{_CATCH_MAX_ITEMS})")
        cleaned = []
        for c in catch:
            s = str(c or "").strip()
            if not s:
                continue
            if len(s) > _CATCH_MAX_LEN:
                raise ValueError(f"catch phrase too long (>{_CATCH_MAX_LEN})")
            cleaned.append(s)
        if cleaned:
            out["catch"] = cleaned
    return out


def save_entry(spoken_name: str, entry: Any, *, overlay: Any = None,
               factory: Any = None, runtime: Any = None) -> Dict[str, Any]:
    """写/覆写 overlay 单条目（原子落盘 + 立即重物化）。返回净化后的条目。"""
    name = str(spoken_name or "").strip()
    if not name or name.startswith("_"):
        raise ValueError("invalid spoken name")
    if len(name) > _NAME_MAX_LEN:
        raise ValueError(f"spoken name too long (>{_NAME_MAX_LEN})")
    clean = validate_entry(entry)
    op = Path(overlay) if overlay else overlay_path()
    with _LOCK:
        data = _read_json(op)
        data[name] = clean
        op.parent.mkdir(parents=True, exist_ok=True)
        tmp = op.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, op)
    ensure_runtime_file(force=True, factory=factory, overlay=op,
                        runtime=runtime)
    return clean


__all__ = [
    "OVERLAY_FILENAME",
    "RUNTIME_FILENAME",
    "factory_path",
    "overlay_path",
    "runtime_path",
    "merged_view",
    "ensure_runtime_file",
    "install_redirect",
    "validate_entry",
    "save_entry",
]
