# -*- coding: utf-8 -*-
"""GUI grounding 评测案例采集（2026-08-16 P1）。

用途：Tier-1 A/B 评测需要真实的"选择器失效"截图集才能量模型命中率。
本模块提供两件事：
  1) 生产采集：`maybe_record(...)` 在 vision_fallback 触发时落一个案例
     （截图 + target + context + 屏幕尺寸）。**默认关闭**，只有显式开采集
     开关（环境变量 HUOKE_GROUNDING_COLLECT=1）才写盘——生产零影响、零风险。
  2) 评测消费：`load_dataset()` 读回已标注 gold 的案例给评测器。

数据集布局（data/grounding_cases/）：
  cases.jsonl        每行一个案例（schema 见 CASE_FIELDS）
  images/<id>.png    截图原图

gold 标注（人工离线补）两种形态，命中判据二选一：
  {"bbox": [x1, y1, x2, y2]}         预测点落框内即命中
  {"point": [x, y], "radius": 40}    预测点距 gold 点 ≤radius 即命中

采集侧不产 gold（gold 靠人工/半自动补标）；未标注案例不进评测分母。
纯标准库，零依赖，异常全吞——采集绝不拖垮生产 grounding。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 项目根：src/ai/grounding_dataset.py → parents[2]
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATASET_DIR = _PROJECT_ROOT / "data" / "grounding_cases"
_CASES_FILE = _DATASET_DIR / "cases.jsonl"
_IMAGES_DIR = _DATASET_DIR / "images"

_COLLECT_ENV = "HUOKE_GROUNDING_COLLECT"

# 案例 schema 字段（缺一即非法；gold 可为 None=未标注）。
CASE_FIELDS = ("id", "ts", "source", "app", "target", "context",
               "image", "image_w", "image_h", "gold")


def is_collecting() -> bool:
    """采集开关：环境变量 HUOKE_GROUNDING_COLLECT 为真值才采集。"""
    return os.environ.get(_COLLECT_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def dataset_dir() -> Path:
    return _DATASET_DIR


def cases_file() -> Path:
    return _CASES_FILE


def _png_dimensions(png_bytes: Optional[bytes]):
    """复用 vision_fallback 的零依赖 PNG 尺寸解析（这里独立实现避免循环导入）。"""
    if not png_bytes or len(png_bytes) < 24:
        return None, None
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None, None
    try:
        w = int.from_bytes(png_bytes[16:20], "big")
        h = int.from_bytes(png_bytes[20:24], "big")
        if 0 < w <= 100000 and 0 < h <= 100000:
            return w, h
    except Exception:
        pass
    return None, None


def maybe_record(target: str, context: str, screenshot_bytes: Optional[bytes],
                 img_w: Optional[int] = None, img_h: Optional[int] = None,
                 source: str = "vision_fallback", app: str = "") -> Optional[str]:
    """采集一个 grounding 案例。默认关闭；异常全吞，绝不影响调用方。

    Returns: 案例 id（采集成功）或 None（未开采集/失败/无截图）。
    """
    try:
        if not is_collecting() or not screenshot_bytes:
            return None
        if img_w is None or img_h is None:
            img_w, img_h = _png_dimensions(screenshot_bytes)
        _IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        case_id = f"{int(time.time()*1000):x}_{abs(hash((target, context))) & 0xffff:04x}"
        img_path = _IMAGES_DIR / f"{case_id}.png"
        with open(img_path, "wb") as f:
            f.write(screenshot_bytes)
        case = {
            "id": case_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "source": source,
            "app": app,
            "target": target or "",
            "context": context or "",
            "image": f"images/{case_id}.png",
            "image_w": img_w,
            "image_h": img_h,
            "gold": None,          # 人工离线补标
        }
        with open(_CASES_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
        return case_id
    except Exception:
        return None


def case_schema_valid(case: Dict[str, Any]) -> bool:
    """校验一个案例字段齐全 + 类型合理。"""
    if not isinstance(case, dict):
        return False
    for k in CASE_FIELDS:
        if k not in case:
            return False
    if not isinstance(case.get("target"), str):
        return False
    if not isinstance(case.get("image"), str) or not case["image"]:
        return False
    return True


def gold_is_labeled(case: Dict[str, Any]) -> bool:
    """该案例是否已标注 gold（否则不进评测分母）。"""
    gold = case.get("gold")
    if not isinstance(gold, dict):
        return False
    if isinstance(gold.get("bbox"), (list, tuple)) and len(gold["bbox"]) == 4:
        return True
    if isinstance(gold.get("point"), (list, tuple)) and len(gold["point"]) == 2:
        return True
    return False


def load_dataset(path: Optional[str] = None, labeled_only: bool = True
                 ) -> List[Dict[str, Any]]:
    """读回案例集。labeled_only=True 时只返回已标 gold 的（评测用）。"""
    p = Path(path) if path else _CASES_FILE
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not case_schema_valid(case):
                continue
            if labeled_only and not gold_is_labeled(case):
                continue
            out.append(case)
    return out


def hit_test(pred_xy, gold: Dict[str, Any]) -> bool:
    """预测点是否命中 gold（bbox 内 / point 半径内）。判据单一真相在此。"""
    if pred_xy is None or not isinstance(gold, dict):
        return False
    try:
        px, py = float(pred_xy[0]), float(pred_xy[1])
    except (TypeError, ValueError, IndexError):
        return False
    bbox = gold.get("bbox")
    if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
        x1, y1, x2, y2 = [float(v) for v in bbox]
        lo_x, hi_x = min(x1, x2), max(x1, x2)
        lo_y, hi_y = min(y1, y2), max(y1, y2)
        return lo_x <= px <= hi_x and lo_y <= py <= hi_y
    point = gold.get("point")
    if isinstance(point, (list, tuple)) and len(point) == 2:
        gx, gy = float(point[0]), float(point[1])
        radius = float(gold.get("radius", 40))
        return ((px - gx) ** 2 + (py - gy) ** 2) ** 0.5 <= radius
    return False
