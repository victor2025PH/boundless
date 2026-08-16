# -*- coding: utf-8 -*-
"""Tier-1 GUI grounding A/B 评测器契约（2026-08-16 P1）。

守三件事，防评测框架自己出错却给出"看起来对"的结论：
  1) 命中判据（hit_test）bbox/point 红绿双向正确——判据是所有模型决策的地基。
  2) 坐标归一化（norm1000→px）正确——grounding 模型最常见的"坐标全偏"坑。
  3) 采集侧默认关闭 + schema/gold 标注判定正确——生产零影响、未标注不进分母。
以及评测器自带的 --selftest 端到端全绿（合成数据+mock 后端，零 GPU）。
"""
from __future__ import annotations

import os

from src.ai import grounding_dataset as gds
from tools import grounding_ab_eval as ab


def test_hit_test_bbox():
    gold = {"bbox": [10, 10, 100, 100]}
    assert ab.gds.hit_test((50, 50), gold)
    assert ab.gds.hit_test((10, 10), gold)      # 边界含
    assert ab.gds.hit_test((100, 100), gold)
    assert not ab.gds.hit_test((5, 50), gold)
    assert not ab.gds.hit_test((50, 200), gold)


def test_hit_test_point_radius():
    gold = {"point": [100, 100], "radius": 40}
    assert gds.hit_test((100, 100), gold)
    assert gds.hit_test((130, 100), gold)       # 距 30 ≤ 40
    assert not gds.hit_test((150, 100), gold)   # 距 50 > 40


def test_hit_test_rejects_none_and_bad():
    assert not gds.hit_test(None, {"bbox": [0, 0, 10, 10]})
    assert not gds.hit_test((5, 5), {})         # 无 gold
    assert not gds.hit_test((5, 5), None)


def test_uitars_coord_normalization():
    """norm1000 坐标必须按图像尺寸缩放回像素（否则坐标全偏）。"""
    ut = ab.UiTarsBackend("http://x/v1", "m", coord_space="norm1000")
    # 解析层
    assert ut._parse("click(point='500 800')") == (500.0, 800.0)
    assert ut._parse("<point>250 750</point>") == (250.0, 750.0)
    assert ut._parse("(360, 640)") == (360.0, 640.0)
    assert ut._parse("no coords here") is None


def test_collector_off_by_default(tmp_path, monkeypatch):
    """采集默认关闭——不设环境变量绝不写盘（生产零影响）。"""
    monkeypatch.delenv("HUOKE_GROUNDING_COLLECT", raising=False)
    assert gds.is_collecting() is False
    cid = gds.maybe_record("btn", "ctx", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    assert cid is None      # 未开采集 → 不记录


def test_schema_and_gold_labeling():
    valid = {"id": "x", "ts": "t", "source": "s", "app": "a", "target": "btn",
             "context": "c", "image": "images/x.png", "image_w": 720,
             "image_h": 1600, "gold": None}
    assert gds.case_schema_valid(valid)
    assert not gds.gold_is_labeled(valid)                       # gold=None 未标注
    valid["gold"] = {"bbox": [0, 0, 1, 1]}
    assert gds.gold_is_labeled(valid)
    # 缺字段即非法
    del valid["image"]
    assert not gds.case_schema_valid(valid)


def test_ab_verdict_delta():
    rg = {"backend": "general", "hit_rate": 0.60}
    rc = {"backend": "uitars", "hit_rate": 0.72}
    v = ab.ab_verdict([rg, rc])
    assert v["winner"] == "uitars"
    assert abs(v["delta_vs_general"] - 0.12) < 1e-9
    assert v["recommend_switch"] is True        # ≥5pt 建议切
    # 提升不足 5pt 不建议切
    v2 = ab.ab_verdict([{"backend": "general", "hit_rate": 0.60},
                        {"backend": "uitars", "hit_rate": 0.63}])
    assert v2["recommend_switch"] is False


def test_selftest_all_green():
    """评测器自带端到端自检必须全绿。"""
    assert ab.run_selftest() is True
