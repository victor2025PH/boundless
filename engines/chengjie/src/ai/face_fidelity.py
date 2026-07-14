# -*- coding: utf-8 -*-
"""人脸身份保真度硬指标（角色 LoRA / PuLID 出图"像不像本人"的客观验收）。

对标 ``scripts/voice_similarity_probe.py`` 的**人脸版**：语音靠 campplus 声纹分抓音色漂移，
这里靠 ArcFace 人脸嵌入余弦抓身份漂移。把"这个 LoRA/锁脸配置能不能上线"从人眼主观
变成**可回归的数**。两个互补维度：
  - **保真度**（生成脸 vs ``face_ref`` 的余弦均值/最小/p10）：像不像**那个人**。
  - **自一致性**（N 张生成脸两两余弦均值）：N 张是不是**同一个人**（抓"多样但换人"）。
另加**无脸率**（insightface 检不到脸=构图崩/遮挡/狗图，硬失败信号，比低分更可诊断）。

本模块**纯判定逻辑 stdlib-only、立即可测**；人脸嵌入提取（``load_face_embedder``）用
insightface(CPU onnx，不违显存纪律，与 176 PuLID 同族)——**opt-in**：未装则返回 None，
探针优雅跳过（镜像 eval 的"缺嵌入就 skip"）。刻度为 provisional（见 ``classify_fidelity``），
攒够样本后可用 ``calibrate_fidelity_floor`` 收紧。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# ArcFace（insightface buffalo_l/antelopev2）余弦刻度（provisional）：真实同人照对常
# ≥0.5、验证阈 ~0.4、<0.28 基本判不同人。**生成脸 vs 参考照**通常略低于真人对真人，
# 故 ok 阈取 0.4 偏宽；先按此告警、攒数据后 calibrate 收紧。分数按"≥阈值"归档。
FIDELITY_BANDS: Dict[str, float] = {"strong": 0.50, "ok": 0.40, "weak": 0.28}
# 总判目标：均值达 ok 阈、p10（最差 10%）不塌、无脸率不高。
FIDELITY_TARGETS: Dict[str, float] = {
    "min_ref_mean": 0.40, "min_ref_p10": 0.30, "max_no_face_ratio": 0.15,
}


def cosine(a: Any, b: Any) -> float:
    """两向量余弦相似度（纯函数，stdlib）。空/长度不等/零向量 → 0.0。"""
    try:
        va = [float(x) for x in a]
        vb = [float(x) for x in b]
    except (TypeError, ValueError):
        return 0.0
    if not va or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def pairwise_mean_cosine(vectors: List[Any]) -> Optional[float]:
    """N 个嵌入两两余弦的均值（自一致性；纯函数）。<2 个向量 → None（无从比较）。"""
    vs = [v for v in (vectors or []) if v is not None]
    n = len(vs)
    if n < 2:
        return None
    total = 0.0
    cnt = 0
    for i in range(n):
        for j in range(i + 1, n):
            total += cosine(vs[i], vs[j])
            cnt += 1
    return (total / cnt) if cnt else None


def classify_fidelity(score: Any, *, bands: Optional[Dict[str, float]] = None) -> str:
    """余弦 → ``strong``/``ok``/``weak``/``mismatch``（纯函数）。非数值 → mismatch。"""
    b = bands or FIDELITY_BANDS
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "mismatch"
    if s >= b["strong"]:
        return "strong"
    if s >= b["ok"]:
        return "ok"
    if s >= b["weak"]:
        return "weak"
    return "mismatch"


def _percentile(sorted_vals: List[float], q: float) -> float:
    """已排序列表的分位数（q∈[0,1]，最近秩法；空→0.0）。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(round(q * (len(sorted_vals) - 1)))
    return float(sorted_vals[max(0, min(len(sorted_vals) - 1, idx))])


def summarize_fidelity(
    ref_scores: List[float],
    *,
    self_consistency: Optional[float] = None,
    no_face: int = 0,
    generated: int = 0,
    bands: Optional[Dict[str, float]] = None,
) -> Dict[str, Any]:
    """聚合保真度统计（纯函数）：均值/最小/p10 + 分级计数 + 无脸率 + 自一致性。

    ``ref_scores``＝每张检出脸与 face_ref 的余弦（无脸的不入列）；``no_face``＝检不到脸张数；
    ``generated``＝实际生成总张数（无脸率=no_face/generated）。
    """
    vals = sorted(float(s) for s in (ref_scores or []))
    n = len(vals)
    b = bands or FIDELITY_BANDS
    band_counts = {"strong": 0, "ok": 0, "weak": 0, "mismatch": 0}
    for s in vals:
        band_counts[classify_fidelity(s, bands=b)] += 1
    gen = int(generated) if generated else (n + int(no_face))
    return {
        "n": n,
        "generated": gen,
        "ref_mean": round(sum(vals) / n, 4) if n else 0.0,
        "ref_min": round(vals[0], 4) if n else 0.0,
        "ref_max": round(vals[-1], 4) if n else 0.0,
        "ref_p10": round(_percentile(vals, 0.10), 4) if n else 0.0,
        "self_consistency": (round(float(self_consistency), 4)
                             if self_consistency is not None else None),
        "no_face": int(no_face),
        "no_face_ratio": round(int(no_face) / gen, 4) if gen else 0.0,
        "bands": band_counts,
    }


def fidelity_verdict(
    summary: Dict[str, Any], *, targets: Optional[Dict[str, float]] = None,
) -> str:
    """保真度总判 ``ok``/``warn``/``fail``（纯函数）。

    - 无有效样本 / 无脸率超标 → ``fail``（配置基本坏了，比低分更硬）。
    - 均值达标 且 p10 不塌 → ``ok``。
    - 均值接近达标（≥85%）→ ``warn``（偏软，建议加样本/调参/重训）。
    - 否则 ``fail``。
    """
    t = targets or FIDELITY_TARGETS
    n = int(summary.get("n", 0) or 0)
    if n == 0:
        return "fail"
    if float(summary.get("no_face_ratio", 0) or 0) > float(t["max_no_face_ratio"]):
        return "fail"
    mean = float(summary.get("ref_mean", 0) or 0)
    p10 = float(summary.get("ref_p10", 0) or 0)
    if mean >= t["min_ref_mean"] and p10 >= t["min_ref_p10"]:
        return "ok"
    if mean >= t["min_ref_mean"] * 0.85:
        return "warn"
    return "fail"


def curation_decision(
    score: Optional[float], *, min_score: float = 0.35,
) -> tuple:
    """训练集清洗判定（纯函数）：某张生成图该不该留进 LoRA 训练集。

    返回 ``(keep: bool, reason: str)``。``score``＝该图脸与 face_ref 的余弦，``None``=未检出脸。
    - 无脸 → ``(False, "no_face")``（构图崩/遮挡，训练噪声）。
    - 分 < ``min_score`` → ``(False, "identity_low")``（是个人但不够像本人，会把 LoRA 带偏）。
    - 否则 → ``(True, "")``。
    ``min_score`` 默认 0.35（介于 weak 0.28 与 ok 0.40 之间，训练集偏严"宁缺勿滥"；
    因为脏样本对 LoRA 的毒害远大于少几张）。
    """
    if score is None:
        return (False, "no_face")
    try:
        s = float(score)
    except (TypeError, ValueError):
        return (False, "no_face")
    if s < float(min_score):
        return (False, f"identity_low({s:.3f}<{min_score})")
    return (True, "")


def load_history_rows(path: Any, *, max_rows: int = 400) -> List[dict]:
    """读 jsonl 尾部历史行（镜像 voice_similarity_probe）。失败 → []。"""
    try:
        p = Path(path)
        if not p.is_file():
            return []
        rows: List[dict] = []
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows[-max_rows:]
    except Exception:
        return []


def calibrate_fidelity_floor(
    rows: List[dict], *, min_n: int = 15, margin: float = 0.05,
) -> float:
    """由历史 jsonl 自动校准保真度告警下限（历史 ref_mean 的 p10 - margin）；纯函数。

    样本 < min_n → 返回 0.0（=用 provisional 默认阈、不因冷启动误报）。与语音自然度
    ``calibrate_naturalness_floor`` 同款"攒够再收紧"纪律：ArcFace 默认阈已可用，这里
    是**按你本地 LoRA 实际分布进一步收紧**的可选增强。
    """
    vals: List[float] = []
    for r in rows or []:
        try:
            v = r.get("ref_mean")
            if v is not None:
                vals.append(float(v))
        except (TypeError, ValueError, AttributeError):
            continue
    if len(vals) < max(2, int(min_n)):
        return 0.0
    vals.sort()
    return round(max(0.0, _percentile(vals, 0.10) - margin), 3)


def load_face_embedder(
    *, model_name: str = "buffalo_l", det_size: int = 640,
) -> Optional[Callable[[str], Optional[list]]]:
    """构造人脸嵌入提取器（insightface CPU，opt-in）；不可用 → None（探针优雅跳过）。

    返回 ``f(image_path) -> list[float] | None``（None=图中未检出脸）。取检出的**最大脸**的
    ArcFace normed_embedding（已 L2 归一，余弦=点积）。首次会下载模型（~百 MB，CPU）。
    启用：``pip install insightface onnxruntime``（刻意不进 requirements——与
    ``AITR_EMBED_LOCAL`` 同款 opt-in，避免默认 CI 背 onnx/模型下载）。
    """
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
        from insightface.app import FaceAnalysis
    except Exception:
        return None
    try:
        app = FaceAnalysis(name=model_name,
                           providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(int(det_size), int(det_size)))
    except Exception:
        return None

    def _embed(image_path: str) -> Optional[list]:
        try:
            import cv2
            img = cv2.imread(str(image_path))
            if img is None:
                return None
            faces = app.get(img)
            if not faces:
                return None
            # 最大脸（bbox 面积）——防背景路人小脸干扰主体判定。
            def _area(f: Any) -> float:
                x1, y1, x2, y2 = f.bbox
                return float((x2 - x1) * (y2 - y1))
            face = max(faces, key=_area)
            emb = getattr(face, "normed_embedding", None)
            if emb is None:
                return None
            return [float(x) for x in emb]
        except Exception:
            return None

    return _embed


__all__ = [
    "FIDELITY_BANDS",
    "FIDELITY_TARGETS",
    "cosine",
    "pairwise_mean_cosine",
    "classify_fidelity",
    "summarize_fidelity",
    "fidelity_verdict",
    "curation_decision",
    "load_history_rows",
    "calibrate_fidelity_floor",
    "load_face_embedder",
]
