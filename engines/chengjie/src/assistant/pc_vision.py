# -*- coding: utf-8 -*-
"""小智 VLM grounding 坐标解析/换算（实施91 P3-B，纯函数）。

**坐标格式已由只读探针实测判定，非假设**：`tools/pc_vision_probe` 2026-08-31 对生产
端点（176 `qwen3-vl:8b-instruct`）合成已知中心的按钮图打真图——point 问法与 bbox 问法
一致回 **[0,1000] 归一化坐标**（8/8 命中，误差 ≤7px @1280x800）。关键推论：[0,1000] 是
**分辨率无关**的分数×1000 → 换算只需**原图尺寸**，与 VisionClient 内部 resize 完全无关
（送多大都一样），彻底绕开缩放标定难题。

红线（方案 §4①）：坐标由**服务端**从真实截图经 VLM 导出，**绝非 LLM 生成**——LLM 只
给目标的自然语言描述。本模块只做「VLM 文本 → 原图像素 → 屏幕绝对点」的确定性换算，
任一步不确定即回 None（fail-closed，绝不瞎点）。

换模型/端点前先重跑 `pc_vision_probe` 复核 GROUND_SCALE，别凭记忆改。
门禁 `tests/test_pc_vision.py`（真探针语料 + 边界/夹紧/回落）。
"""
from __future__ import annotations

import json
import re
from typing import Optional, Tuple

# Qwen-VL 系归一化基（探针实测 [0,1000]）。换模型先重跑探针再动这里。
GROUND_SCALE = 1000.0


def parse_ground_point(text: str) -> Optional[Tuple[float, float]]:
    """从 VLM 文本抽一个中心点（[0,GROUND_SCALE] 归一化空间，未夹紧）。

    容忍代码围栏/散文包裹；识别 ``{"x","y"}`` / ``{"point":[x,y]}`` /
    ``{"bbox":[x1,y1,x2,y2]}``（bbox 取中心）；JSON 不成则回落裸数字（≥4 个按 bbox 中心、
    ≥2 个按 point）。无可用数回 None。"""
    if not text:
        return None
    s = str(text).strip()
    # 抓第一个 {...} 块（顺带越过 ```json 围栏/前后散文）
    m = re.search(r"\{.*\}", s, flags=re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
        except Exception:
            obj = None
        if isinstance(obj, dict):
            bbox = obj.get("bbox")
            if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                try:
                    return ((float(bbox[0]) + float(bbox[2])) / 2.0,
                            (float(bbox[1]) + float(bbox[3])) / 2.0)
                except (TypeError, ValueError):
                    pass
            point = obj.get("point")
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    return (float(point[0]), float(point[1]))
                except (TypeError, ValueError):
                    pass
            if "x" in obj and "y" in obj:
                try:
                    return (float(obj["x"]), float(obj["y"]))
                except (TypeError, ValueError):
                    pass
    nums = [float(x) for x in re.findall(r"-?\d+\.?\d*", s)]
    if len(nums) >= 4:
        return ((nums[0] + nums[2]) / 2.0, (nums[1] + nums[3]) / 2.0)
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return None


def to_image_point(raw_x, raw_y, img_w, img_h,
                   scale: float = GROUND_SCALE) -> Optional[Tuple[int, int]]:
    """[0,scale] 归一化 → 原图像素点，夹到 [0,img_w-1]×[0,img_h-1]。非法尺寸回 None。"""
    try:
        rx, ry = float(raw_x), float(raw_y)
        w, h = int(img_w), int(img_h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or scale <= 0:
        return None
    px = int(round(rx / scale * w))
    py = int(round(ry / scale * h))
    px = max(0, min(w - 1, px))
    py = max(0, min(h - 1, py))
    return (px, py)


def to_screen_point(img_x, img_y, win_left, win_top) -> Optional[Tuple[int, int]]:
    """原图（窗口截图）像素点 → 屏幕绝对坐标（窗口原点 + 图内偏移）。"""
    try:
        return (int(win_left) + int(img_x), int(win_top) + int(img_y))
    except (TypeError, ValueError):
        return None


def ground_to_screen(text: str, img_w, img_h, win_left, win_top,
                     scale: float = GROUND_SCALE) -> Optional[Tuple[int, int]]:
    """全链：VLM 文本 → 屏幕绝对点。任一步失败回 None（fail-closed）。

    win_left/win_top = 截图窗口在屏幕上的原点（窗口截图坐标是相对该窗口的）。全屏截图
    则传 (0,0)。返回的屏幕点交 runner 侧 _point_in_screen 再兜一层越界。"""
    pt = parse_ground_point(text)
    if pt is None:
        return None
    ip = to_image_point(pt[0], pt[1], img_w, img_h, scale)
    if ip is None:
        return None
    return to_screen_point(ip[0], ip[1], win_left, win_top)
