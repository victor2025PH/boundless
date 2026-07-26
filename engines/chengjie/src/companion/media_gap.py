"""相册场景「供给 vs 需求」缺口报告（P1 图文一致性，2026-07-27）。

背景：P0 起点名场景是硬要求（挑不到同场景类相册图→如实回落，绝不发不相干的
图），P1-3 又把「客户点名了什么场景、多少次没兑现」记进 ``image_autosend``
的 ``scene_demand``/``scene_unmet`` 计数——本模块把这两侧对上：

- **供给侧** ``collect_scene_supply``：扫注册相册(DB) + 文件系统相册，按
  persona×场景类清点备货（meta/tags 标注优先，回落策展文件名前缀）；
- **需求侧**：调用方从 ``image_autosend.metrics_snapshot()`` 取（进程级计数，
  重启清零——报告口径如实标注 since restart）；
- **纯函数** ``scene_gap_report``：按场景类合并两侧 → 「哪个场景被要了多少次、
  几次没给出、哪些人设一张都没备」→ 运营照单补图/补 LoRA 场景即闭环
  （与语音侧「备货缺口 Top」同哲学：观测→照单补货）。

供给扫描带 TTL 缓存（300s）；全链软失败返回空（报告页宁可空态不崩）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ALBUM_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")

_SUPPLY_CACHE: Dict[str, tuple] = {}
_SUPPLY_LOCK = threading.Lock()
_SUPPLY_TTL_SEC = 300.0


def _fs_scene_counts(album_dir: str) -> Dict[str, Dict[str, int]]:
    """文件系统相册按 persona 分册清点场景类。

    根目录平铺图（单人设布局）记到 ``""`` 键（报告侧显示为共享池）。
    meta sidecar 的 ``scene`` 标注优先，回落策展文件名 ``<scene>_<series>_<nn>``。
    """
    out: Dict[str, Dict[str, int]] = {}
    try:
        root = Path(str(album_dir or ""))
        if not root.is_dir():
            return out
        from src.ai.companion_selfie import (
            album_scene_class_of_path, load_album_meta)
        from src.companion.persona_media import scene_class_of

        def _count_dir(d: Path, key: str) -> None:
            metas = load_album_meta(str(d))
            for f in d.iterdir():
                if not (f.is_file() and f.suffix.lower() in _ALBUM_IMG_EXT):
                    continue
                m = metas.get(f.name) if isinstance(metas, dict) else None
                cls = scene_class_of((m or {}).get("scene")) if m else ""
                if not cls:
                    cls = album_scene_class_of_path(f.name)
                bucket = out.setdefault(key, {})
                bucket[cls or "unknown"] = bucket.get(cls or "unknown", 0) + 1

        _count_dir(root, "")
        for sub in root.iterdir():
            if sub.is_dir():
                _count_dir(sub, sub.name)
    except Exception:
        return out
    return out


def _registry_scene_counts() -> Dict[str, Dict[str, int]]:
    """注册相册(DB) 按 persona 清点场景类（仅 enabled 图片条目=可发库存）。"""
    out: Dict[str, Dict[str, int]] = {}
    try:
        from src.companion.persona_media import row_scene_class
        from src.companion.persona_media_store import get_persona_media_store
        store = get_persona_media_store()
        if store is None:
            return out
        for row in store.list(enabled_only=True, media_type="photo"):
            pid = str(row.get("persona_id") or "").strip()
            cls = row_scene_class(row) or "unknown"
            bucket = out.setdefault(pid, {})
            bucket[cls] = bucket.get(cls, 0) + 1
    except Exception:
        return out
    return out


def collect_scene_supply(
    scfg: Any, *, now: Any = None, force: bool = False,
) -> Dict[str, Dict[str, int]]:
    """相册供给全景：``{persona_id: {场景类: 张数}}``（DB + FS 合并，TTL 缓存）。

    ``""`` persona 键 = 文件系统根目录平铺图（单人设布局/共享池）。
    """
    album_dir = ""
    try:
        prov = (scfg or {}).get("provider") if isinstance(scfg, dict) else None
        album_dir = str((prov or {}).get("album_dir") or "").strip()
    except Exception:
        album_dir = ""
    ts = float(now if isinstance(now, (int, float)) else time.time())
    ckey = album_dir or "-"
    with _SUPPLY_LOCK:
        ent = _SUPPLY_CACHE.get(ckey)
        if ent and not force and (ts - ent[0]) < _SUPPLY_TTL_SEC:
            return {k: dict(v) for k, v in ent[1].items()}
    supply = _registry_scene_counts()
    for pid, counts in _fs_scene_counts(album_dir).items():
        bucket = supply.setdefault(pid, {})
        for cls, n in counts.items():
            bucket[cls] = bucket.get(cls, 0) + n
    with _SUPPLY_LOCK:
        _SUPPLY_CACHE[ckey] = (ts, {k: dict(v) for k, v in supply.items()})
        if len(_SUPPLY_CACHE) > 16:
            _SUPPLY_CACHE.pop(next(iter(_SUPPLY_CACHE)))
    return supply


def invalidate_supply_cache() -> None:
    """清供给缓存（测试/相册批量变更后用）。"""
    with _SUPPLY_LOCK:
        _SUPPLY_CACHE.clear()


def scene_gap_report(
    supply: Optional[Dict[str, Dict[str, int]]],
    demand: Optional[Dict[str, int]],
    unmet: Optional[Dict[str, int]],
    *,
    top_n: int = 12,
) -> Dict[str, Any]:
    """供需对表（纯函数）：被点名过的场景类逐行出 需求/未兑现/库存/缺货人设。

    - ``rows`` 按 未兑现 ↓ → 需求 ↓ 排序，截断 ``top_n``；
    - ``missing`` = 有相册库存（任何场景）但该场景 0 张的人设——运营补货的
      直接靶子；共享池（"" persona）有货不算缺；
    - 需求计数是进程级（重启清零），报告只反映「最近一段真实流量」，
      不当历史全量看。
    """
    supply = supply if isinstance(supply, dict) else {}
    demand = demand if isinstance(demand, dict) else {}
    unmet = unmet if isinstance(unmet, dict) else {}
    personas = [p for p in supply.keys() if p]
    scenes = sorted(set(demand.keys()) | set(unmet.keys()))
    rows: List[Dict[str, Any]] = []
    for scene in scenes:
        d = int(demand.get(scene, 0) or 0)
        u = int(unmet.get(scene, 0) or 0)
        if d <= 0 and u <= 0:
            continue
        per = {p: int(counts.get(scene, 0) or 0)
               for p, counts in supply.items() if counts.get(scene)}
        total = sum(per.values())
        shared_ok = int((supply.get("") or {}).get(scene, 0) or 0) > 0
        missing = ([] if shared_ok else
                   sorted(p for p in personas if not (supply.get(p) or {}).get(scene)))
        rows.append({
            "scene": scene,
            "demand": d,
            "unmet": u,
            "unmet_rate": round(u / d, 3) if d > 0 else None,
            "supply": total,
            "missing_personas": missing,
        })
    rows.sort(key=lambda r: (-r["unmet"], -r["demand"], r["scene"]))
    supply_totals = {
        p: sum(counts.values()) for p, counts in supply.items()}
    return {
        "rows": rows[:max(0, int(top_n))],
        "supply_totals": supply_totals,
        "active": bool(rows) or bool(supply_totals),
    }
