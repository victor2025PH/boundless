# -*- coding: utf-8 -*-
"""人脸身份保真度纯函数门禁（cosine/自一致性/分级/聚合/总判/校准/嵌入器优雅降级）。"""

from __future__ import annotations

import importlib.util
import json

from src.ai import face_fidelity as ff


# ── 余弦 / 自一致性 ─────────────────────────────────────────────────────
def test_cosine():
    assert ff.cosine([1, 0, 0], [1, 0, 0]) == 1.0            # 同向
    assert ff.cosine([1, 0], [0, 1]) == 0.0                   # 正交
    assert round(ff.cosine([1, 0], [-1, 0]), 4) == -1.0       # 反向
    assert ff.cosine([], []) == 0.0                            # 空
    assert ff.cosine([1, 2], [1, 2, 3]) == 0.0                # 长度不等
    assert ff.cosine([0, 0], [1, 1]) == 0.0                   # 零向量


def test_pairwise_mean_cosine():
    assert ff.pairwise_mean_cosine([[1, 0]]) is None          # <2 无从比
    assert ff.pairwise_mean_cosine([]) is None
    assert ff.pairwise_mean_cosine([[1, 0], [1, 0], [1, 0]]) == 1.0   # 全同=1
    m = ff.pairwise_mean_cosine([[1, 0], [0, 1]])
    assert m == 0.0


# ── 分级 ────────────────────────────────────────────────────────────────
def test_classify_fidelity():
    assert ff.classify_fidelity(0.6) == "strong"
    assert ff.classify_fidelity(0.45) == "ok"
    assert ff.classify_fidelity(0.30) == "weak"
    assert ff.classify_fidelity(0.10) == "mismatch"
    assert ff.classify_fidelity("nan_str") == "mismatch"
    # 边界（>=阈归上一档）
    assert ff.classify_fidelity(0.50) == "strong"
    assert ff.classify_fidelity(0.40) == "ok"
    assert ff.classify_fidelity(0.28) == "weak"


# ── 聚合 + 总判 ─────────────────────────────────────────────────────────
def test_summarize_and_verdict_ok():
    s = ff.summarize_fidelity([0.6, 0.55, 0.52, 0.48, 0.30])
    assert s["n"] == 5 and s["generated"] == 5
    assert s["ref_mean"] == 0.49 and s["ref_min"] == 0.30
    assert s["bands"] == {"strong": 3, "ok": 1, "weak": 1, "mismatch": 0}
    assert s["no_face_ratio"] == 0.0
    assert ff.fidelity_verdict(s) == "ok"


def test_verdict_fail_on_no_face():
    s = ff.summarize_fidelity([0.6] * 5, no_face=3, generated=8)
    assert s["no_face_ratio"] == 0.375
    assert ff.fidelity_verdict(s) == "fail"      # 无脸率超标=硬失败


def test_verdict_warn_and_fail_by_mean():
    warn = ff.summarize_fidelity([0.36, 0.36, 0.36, 0.36])   # 0.34<=mean<0.40
    assert ff.fidelity_verdict(warn) == "warn"
    bad = ff.summarize_fidelity([0.20, 0.20, 0.20])          # mean 远低
    assert ff.fidelity_verdict(bad) == "fail"
    assert ff.fidelity_verdict(ff.summarize_fidelity([])) == "fail"   # 无样本=fail


def test_summarize_self_consistency_passthrough():
    s = ff.summarize_fidelity([0.5, 0.5], self_consistency=0.812345)
    assert s["self_consistency"] == 0.8123


# ── 历史/校准 ───────────────────────────────────────────────────────────
def test_load_history_and_calibrate(tmp_path):
    p = tmp_path / "lora_fidelity.jsonl"
    rows = [{"ref_mean": 0.5 + i * 0.01} for i in range(20)]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n" + "bad json\n",
                 encoding="utf-8")
    loaded = ff.load_history_rows(p)
    assert len(loaded) == 20                       # 坏行跳过
    floor = ff.calibrate_fidelity_floor(loaded)
    assert floor > 0                               # ≥15 样本→启用（p10-margin）
    # 样本不足 → 0.0（不告警，用默认阈）
    assert ff.calibrate_fidelity_floor(loaded[:5]) == 0.0
    assert ff.load_history_rows(tmp_path / "nope.jsonl") == []


# ── 训练集清洗判定 ──────────────────────────────────────────────────────
def test_curation_decision():
    keep, reason = ff.curation_decision(0.5)
    assert keep is True and reason == ""
    # 无脸 → 剔
    keep, reason = ff.curation_decision(None)
    assert keep is False and reason == "no_face"
    # 分低于阈 → 剔（identity_low）
    keep, reason = ff.curation_decision(0.20, min_score=0.35)
    assert keep is False and reason.startswith("identity_low")
    # 边界：等于阈值保留
    assert ff.curation_decision(0.35, min_score=0.35)[0] is True
    # 阈值可调：放宽后同分保留
    assert ff.curation_decision(0.30, min_score=0.28)[0] is True
    # 非数值当无脸处理
    assert ff.curation_decision("x")[0] is False


# ── 嵌入器优雅降级 ──────────────────────────────────────────────────────
def test_load_face_embedder_graceful():
    if importlib.util.find_spec("insightface") is None:
        assert ff.load_face_embedder() is None     # 缺依赖=优雅 None（探针 skip）
    else:
        assert callable(ff.load_face_embedder)     # 已装：仅验证可调用（不触发模型下载）
