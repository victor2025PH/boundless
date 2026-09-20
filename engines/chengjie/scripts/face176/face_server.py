# -*- coding: utf-8 -*-
"""人脸嵌入微服务（#333 视觉身份层，2026-09-17；部署在 176，CPU onnx，端口 8767）。

只做三件事：检测人脸 → 对齐 → ArcFace 512 维嵌入（L2 归一，余弦=点积）。
**不做身份判定**——「像不像谁」的阈值与措辞在客户端 ``src/companion/visual_identity``
单一出口（与 ASR/SER「标签→语义映射只在客户端」同纪律）。

模型：复用 176 上 PuLID 已装的 InsightFace ``antelopev2``（``D:\\ComfyUI\\models\\insightface``，
scrfd_10g 检测 + glintr100 识别），**零新下载**；``AITR_FACE_MODEL`` / ``AITR_FACE_ROOT`` 可换
（出货前换 Apache-2.0 的 AuraFace：同目录布局，把 onnx 放进 ``models/auraface`` 即可）。
CPU provider 刻意钉死：5090 显存常年 26/32G，这里 QPS 极低（几十张/天），CPU 单张 ~100ms。

接口（JSON，base64 图 ≤ 8MB）：
- ``GET  /health``                 → ``{ok, loaded, model, provider, det_size, version}``
- ``POST /v1/face/embed``          → ``{image_base64, max_faces=5, min_det_score=0.5}`` →
  ``{ok, faces:[{bbox, det_score, embedding[512], gender, age}], width, height, latency_ms, model}``
- ``POST /v1/face/compare``        → ``{a_base64, b_base64}`` → ``{ok, cosine, faces_a, faces_b}``

运行：``AITR_FACE_PORT``（8767）``AITR_FACE_DET_SIZE``（640）``AITR_WARMUP``（1）。
依赖：insightface onnxruntime opencv-python-headless numpy fastapi uvicorn（见 deploy_face.ps1）。
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

VERSION = "face176 v1.0 (2026-09-17)"
MODEL_NAME = os.environ.get("AITR_FACE_MODEL", "antelopev2")
MODEL_ROOT = os.environ.get("AITR_FACE_ROOT", r"D:\ComfyUI\models\insightface")
DET_SIZE = int(os.environ.get("AITR_FACE_DET_SIZE", "640") or 640)
MAX_B64 = 8 * 1024 * 1024 + 1024 * 1024   # 8MB 二进制 ≈ 10.7MB base64；放宽到 ~11MB
MAX_LONG_SIDE = 1600                        # 大图先缩，检测器 640 输入用不上 4000px

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")
log = logging.getLogger("face176")

app = FastAPI(title="AITR face embed", version=VERSION)
_lock = threading.Lock()
_app_model: Any = None
_load_err: str = ""


def _load() -> Any:
    """懒加载 FaceAnalysis（CPU）。失败记 _load_err，/health 可见；请求侧 503。"""
    global _app_model, _load_err
    if _app_model is not None:
        return _app_model
    with _lock:
        if _app_model is not None:
            return _app_model
        try:
            from insightface.app import FaceAnalysis
            t0 = time.time()
            fa = FaceAnalysis(name=MODEL_NAME, root=MODEL_ROOT,
                              providers=["CPUExecutionProvider"],
                              allowed_modules=["detection", "recognition", "genderage"])
            fa.prepare(ctx_id=-1, det_size=(DET_SIZE, DET_SIZE))
            _app_model = fa
            _load_err = ""
            log.info("model loaded name=%s root=%s det=%d in %.1fs", MODEL_NAME, MODEL_ROOT,
                     DET_SIZE, time.time() - t0)
        except Exception as e:  # noqa: BLE001
            _load_err = f"{type(e).__name__}: {e}"
            log.error("model load failed: %s", _load_err)
            raise
    return _app_model


def _decode_image(b64: str) -> np.ndarray:
    import cv2
    s = str(b64 or "")
    if "," in s[:64] and s.lstrip().startswith("data:"):
        s = s.split(",", 1)[1]
    if len(s) > MAX_B64:
        raise HTTPException(413, "image too large (>8MB)")
    try:
        raw = base64.b64decode(s, validate=False)
    except Exception:
        raise HTTPException(400, "bad base64")
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "cannot decode image")
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > MAX_LONG_SIDE:
        scale = MAX_LONG_SIDE / float(long_side)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _faces(img: np.ndarray, *, max_faces: int, min_det_score: float) -> List[Dict[str, Any]]:
    fa = _load()
    with _lock:
        found = fa.get(img)
    out: List[Dict[str, Any]] = []
    for f in found or []:
        score = float(getattr(f, "det_score", 0.0) or 0.0)
        if score < min_det_score:
            continue
        emb = getattr(f, "normed_embedding", None)
        if emb is None:
            emb = getattr(f, "embedding", None)
            if emb is not None:
                n = float(np.linalg.norm(emb)) or 1.0
                emb = emb / n
        if emb is None:
            continue
        raw_bbox = getattr(f, "bbox", None)
        if raw_bbox is None:
            raw_bbox = [0, 0, 0, 0]
        bbox = [int(round(float(x))) for x in np.asarray(raw_bbox).ravel().tolist()[:4]]
        item: Dict[str, Any] = {
            "bbox": bbox, "det_score": round(score, 4),
            "embedding": [round(float(x), 6) for x in np.asarray(emb).ravel().tolist()],
        }
        g = getattr(f, "gender", None)
        a = getattr(f, "age", None)
        if g is not None:
            item["gender"] = "M" if int(g) == 1 else "F"
        if a is not None:
            item["age"] = int(a)
        out.append(item)
    # 大脸优先（主体多半是最大的那张脸）
    out.sort(key=lambda d: -((d["bbox"][2] - d["bbox"][0]) * (d["bbox"][3] - d["bbox"][1])))
    return out[: max(1, int(max_faces))]


class EmbedReq(BaseModel):
    image_base64: str
    max_faces: int = 5
    min_det_score: float = 0.5


class CompareReq(BaseModel):
    a_base64: str
    b_base64: str
    min_det_score: float = 0.5


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": _app_model is not None and not _load_err,
        "loaded": _app_model is not None,
        "error": _load_err or None,
        "model": MODEL_NAME, "root": MODEL_ROOT, "provider": "CPUExecutionProvider",
        "det_size": DET_SIZE, "version": VERSION,
    }


@app.post("/v1/face/embed")
def embed(req: EmbedReq) -> Dict[str, Any]:
    t0 = time.time()
    img = _decode_image(req.image_base64)
    try:
        faces = _faces(img, max_faces=req.max_faces, min_det_score=req.min_det_score)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("embed failed")
        raise HTTPException(503, f"model unavailable: {type(e).__name__}: {e}")
    h, w = img.shape[:2]
    return {"ok": True, "faces": faces, "count": len(faces), "width": int(w), "height": int(h),
            "latency_ms": int((time.time() - t0) * 1000), "model": MODEL_NAME}


@app.post("/v1/face/compare")
def compare(req: CompareReq) -> Dict[str, Any]:
    t0 = time.time()
    a = _faces(_decode_image(req.a_base64), max_faces=1, min_det_score=req.min_det_score)
    b = _faces(_decode_image(req.b_base64), max_faces=1, min_det_score=req.min_det_score)
    cos: Optional[float] = None
    if a and b:
        va = np.asarray(a[0]["embedding"], dtype=np.float32)
        vb = np.asarray(b[0]["embedding"], dtype=np.float32)
        cos = float(np.dot(va, vb))
    return {"ok": True, "cosine": cos, "faces_a": len(a), "faces_b": len(b),
            "latency_ms": int((time.time() - t0) * 1000), "model": MODEL_NAME}


def _warmup() -> None:
    try:
        _load()
        import cv2
        blank = np.zeros((DET_SIZE, DET_SIZE, 3), dtype=np.uint8)
        cv2.circle(blank, (DET_SIZE // 2, DET_SIZE // 2), 60, (200, 180, 160), -1)
        _faces(blank, max_faces=1, min_det_score=0.5)
        log.info("warmup done")
    except Exception as e:  # noqa: BLE001
        log.warning("warmup failed (lazy path will retry): %s", e)


if __name__ == "__main__":
    import uvicorn
    if (os.environ.get("AITR_WARMUP", "1") or "1") != "0":
        threading.Thread(target=_warmup, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AITR_FACE_PORT", "8767") or 8767),
                log_level="info")
