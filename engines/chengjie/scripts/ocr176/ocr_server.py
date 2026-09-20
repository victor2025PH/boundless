# -*- coding: utf-8 -*-
"""PP-OCRv5 文字检测/识别微服务（176，CPU 推理）——图片翻译「译文图」的专用 OCR。

为什么存在：VLM（qwen2.5vl）近似 bbox 是「译文图漏块/盖偏」的结构性根因
（CVPR 2026 PP-OCRv5/v6 论文实测：通用 VLM 文字定位 Hmean 落后专用检测 ~39pp）。
本服务用 5M 级专用模型出**行级精确框**，供 src/ai/image_patch_translate 消费。

为什么 CPU：176 的 5090 显存长期 26/32GB（qwen3:30b + vl + hy-mt + bge + ASR/SER），
而本服务 QPS 极低（坐席点一次译一张）——mobile 检测/识别模型 CPU 单图百毫秒级
足够，且完全避开 Blackwell(sm_120) 的 paddle GPU 适配风险。

契约（客户端 build_ppocr_boxes_fn 消费，勿破坏）：
  GET  /health          → {ok, loaded, engine, ms}
  POST /v1/ocr          → body {"image_b64": "..."（可带 data URL 头）}
                        → {ok, lines: [{text, box: [x1,y1,x2,y2] 像素, conf}],
                           width, height, ms}
  失败一律 {ok: false, reason}（客户端回落 VLM，本服务绝不 5xx 化简单错误）。

兼容 paddleocr 3.x（predict → rec_texts/rec_polys/rec_scores）与 2.x
（ocr() → [[poly,(text,conf)],...]）两代 API；单推理锁（CPU 串行足够）。
"""
from __future__ import annotations

import base64
import io
import os
import threading
import time

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="aitr-ocr176", docs_url=None, redoc_url=None)

_LOCK = threading.Lock()
_OCR = None
_ENGINE = ""
_LOAD_ERR = ""


def _get_ocr():
    """懒加载 + 进程级单例。paddleocr 3.x 优先，缺参数时逐级降级到最简构造。"""
    global _OCR, _ENGINE, _LOAD_ERR
    if _OCR is not None:
        return _OCR
    from paddleocr import PaddleOCR
    attempts = (
        # 首档＝176 实测唯一可用组合（2026-08-19）：paddle 3.3.1 的 oneDNN 执行器
        # 对 PIR ArrayAttribute<DoubleAttribute> 未实现（NotImplementedError），
        # 必须 enable_mkldnn=False 走 plain CPU 内核；模型钉 PP-OCRv5_mobile
        # （det+rec 共 ~30MB，CPU 百毫秒级，QPS 足够）。关文档矫正/去弯曲
        # （聊天图片不需要，省两个模型），保文本行方向分类（照片常有斜排字）。
        dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
             use_textline_orientation=True, enable_mkldnn=False,
             text_detection_model_name="PP-OCRv5_mobile_det",
             text_recognition_model_name="PP-OCRv5_mobile_rec"),
        dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
             use_textline_orientation=True, enable_mkldnn=False),
        dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
             use_textline_orientation=True),
        # 2.x / 参数名不识别时的回落
        dict(use_angle_cls=True, lang="ch", show_log=False),
        dict(),
    )
    last = None
    for kw in attempts:
        try:
            _OCR = PaddleOCR(**kw)
            _ENGINE = f"paddleocr:{','.join(kw.keys()) or 'default'}"
            return _OCR
        except TypeError as ex:      # 参数名跨版本漂移 → 试下一档
            last = ex
            continue
    _LOAD_ERR = f"init_failed:{type(last).__name__ if last else 'unknown'}"
    raise RuntimeError(_LOAD_ERR)


def _poly_to_box(poly) -> list:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _extract_lines(result) -> list:
    """两代 paddleocr 结果 → [{text, box, conf}]。解析不出返回 []。"""
    lines = []
    if not result:
        return lines
    first = result[0]
    # 3.x：Result（dict-like）带 rec_texts / rec_polys / rec_scores
    try:
        texts = first["rec_texts"]
        polys = first["rec_polys"]
        scores = first.get("rec_scores") if hasattr(first, "get") else first["rec_scores"]
        for i, t in enumerate(texts):
            t = str(t or "").strip()
            if not t:
                continue
            lines.append({
                "text": t,
                "box": _poly_to_box(polys[i]),
                "conf": round(float(scores[i]) if scores is not None else -1.0, 4),
            })
        return lines
    except Exception:
        pass
    # 2.x：[[ [poly,(text,conf)], ... ]]
    try:
        for item in (first or []):
            poly, tc = item[0], item[1]
            t = str(tc[0] or "").strip()
            if not t:
                continue
            lines.append({"text": t, "box": _poly_to_box(poly),
                          "conf": round(float(tc[1]), 4)})
    except Exception:
        return []
    return lines


class OcrBody(BaseModel):
    image_b64: str = ""


@app.get("/health")
def health():
    return {"ok": True, "loaded": _OCR is not None, "engine": _ENGINE,
            "load_err": _LOAD_ERR}


@app.post("/v1/ocr")
def ocr(body: OcrBody):
    t0 = time.monotonic()
    raw = str(body.image_b64 or "")
    if raw.startswith("data:"):
        raw = raw.partition(",")[2]
    if not raw:
        return {"ok": False, "reason": "empty"}
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        return {"ok": False, "reason": "decode_failed"}
    if not data or len(data) > 12 * 1024 * 1024:
        return {"ok": False, "reason": "bad_size"}
    try:
        from PIL import Image
        import numpy as np
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        # PIL RGB → paddle 期望 BGR；::-1 是负步长视图，paddle C++ 桥直接
        # NotImplementedError（首次冒烟实锤）——必须 ascontiguousarray 落实体
        arr = np.ascontiguousarray(np.array(img)[:, :, ::-1])
    except Exception:
        return {"ok": False, "reason": "bad_image"}
    try:
        with _LOCK:
            engine = _get_ocr()
            if hasattr(engine, "predict"):
                result = engine.predict(arr)          # 3.x
            else:
                result = engine.ocr(arr, cls=True)    # 2.x
    except Exception as ex:  # noqa: BLE001
        return {"ok": False, "reason": f"infer_failed:{type(ex).__name__}"}
    lines = _extract_lines(result)
    return {"ok": True, "lines": lines, "width": w, "height": h,
            "ms": int((time.monotonic() - t0) * 1000)}


if os.environ.get("AITR_WARMUP", "1") == "1":
    def _warm():
        try:
            from PIL import Image, ImageDraw
            import numpy as np
            img = Image.new("RGB", (320, 96), (255, 255, 255))
            ImageDraw.Draw(img).text((12, 30), "warmup 预热 123", fill=(0, 0, 0))
            arr = np.ascontiguousarray(np.array(img)[:, :, ::-1])
            with _LOCK:
                engine = _get_ocr()
                (engine.predict(arr) if hasattr(engine, "predict")
                 else engine.ocr(arr, cls=True))
            print("[ocr176] warmup done", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"[ocr176] warmup failed: {ex}", flush=True)

    threading.Thread(target=_warm, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("AITR_OCR_PORT", "8766")))
