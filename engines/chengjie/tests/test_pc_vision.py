# -*- coding: utf-8 -*-
"""小智 VLM grounding 坐标解析/换算门禁（实施91 P3-B，2026-08-31）。

语料用 `tools/pc_vision_probe` 对生产端点（176 qwen3-vl:8b-instruct）**实测**的原始
返回（[0,1000] 归一化，point 与 bbox 两种问法）+ 边界/夹紧/散文/回落/fail-closed。
坐标格式是探针实测判定，不是假设——换模型先重跑探针。
"""
from src.assistant import pc_vision as pv


# 探针实测真值（1280x800，窗口原点 (0,0)）：label -> (raw 返回文本, 屏幕真中心)
_REAL_POINT = [
    ('{"x": 187, "y": 202}', (240, 160)),
    ('{"x": 809, "y": 207}', (1040, 160)),
    ('{"x": 497, "y": 498}', (640, 400)),
    ('{"x": 498, "y": 817}', (640, 660)),
]
_REAL_BBOX = [
    ('```json\n{"bbox": [114, 163, 257, 243]}\n```', (240, 160)),
    ('```json\n{"bbox": [736, 164, 885, 244]}\n```', (1040, 160)),
    ('```json\n{"bbox": [425, 463, 570, 539]}\n```', (640, 400)),
    ('```json\n{"bbox": [425, 776, 573, 859]}\n```', (640, 660)),
]


def _dist(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def test_real_point_responses_map_within_tolerance():
    for raw, truth in _REAL_POINT:
        pt = pv.ground_to_screen(raw, 1280, 800, 0, 0)
        assert pt is not None, raw
        assert _dist(pt, truth) <= 12, (raw, pt, truth)


def test_real_bbox_responses_map_within_tolerance():
    for raw, truth in _REAL_BBOX:
        pt = pv.ground_to_screen(raw, 1280, 800, 0, 0)
        assert pt is not None, raw
        assert _dist(pt, truth) <= 12, (raw, pt, truth)


def test_parse_point_json():
    assert pv.parse_ground_point('{"x": 500, "y": 250}') == (500.0, 250.0)


def test_parse_point_object():
    assert pv.parse_ground_point('{"point": [500, 250]}') == (500.0, 250.0)


def test_parse_bbox_center():
    # 中心 = ((100+300)/2, (200+400)/2) = (200, 300)
    assert pv.parse_ground_point('{"bbox": [100, 200, 300, 400]}') == (200.0, 300.0)


def test_parse_prose_fallback_numbers():
    # 无 JSON、散文里带坐标 → 回落首数对
    assert pv.parse_ground_point("The center is x=497, y=498 in the image.") == (497.0, 498.0)


def test_parse_garbage_returns_none():
    assert pv.parse_ground_point("I cannot find that element.") is None
    assert pv.parse_ground_point("") is None
    assert pv.parse_ground_point(None) is None  # type: ignore[arg-type]


def test_to_image_point_scales_and_clamps():
    # 500/1000*1280=640, 250/1000*800=200
    assert pv.to_image_point(500, 250, 1280, 800) == (640, 200)
    # 越界夹紧：1200/1000*1280=1536→1279；-5→0
    assert pv.to_image_point(1200, -5, 1280, 800) == (1279, 0)


def test_to_image_point_bad_dims_none():
    assert pv.to_image_point(500, 250, 0, 800) is None
    assert pv.to_image_point(500, 250, 1280, -1) is None
    assert pv.to_image_point("x", 250, 1280, 800) is None


def test_window_origin_offset_added():
    # 窗口原点 (100,50)：图内 (640,200) → 屏幕 (740,250)
    pt = pv.ground_to_screen('{"x": 500, "y": 250}', 1280, 800, 100, 50)
    assert pt == (740, 250)


def test_ground_to_screen_fail_closed_on_no_point():
    assert pv.ground_to_screen("nope", 1280, 800, 0, 0) is None


def test_scale_is_thousand_per_probe():
    # 探针实测判定；变了先重跑 pc_vision_probe 再改，别默默动
    assert pv.GROUND_SCALE == 1000.0
