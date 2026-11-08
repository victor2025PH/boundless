"""可选的天城文识别。只在 117 的隔离 venv 里加载，不进共享 OCR 服务。

共享的 ``scripts/ocr176/ocr_server.py`` 仍只收 ``image_b64``，模型仍是
``PP-OCRv5_mobile_rec``。那个进程没有天城文字符，也不要往里面加语言参数。

117 上单独建一个环境再打开开关::

    py -3 -m venv C:\\wa-ocr-hi\\venv
    C:\\wa-ocr-hi\\venv\\Scripts\\python -m pip install paddleocr pillow numpy
    set WA_DEVANAGARI_OCR=1

界面树已经读到气泡或输入框正文时不会调用这里。
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, List, Optional

DEVANAGARI_REC_MODEL = "devanagari_PP-OCRv5_mobile_rec"
DEVANAGARI_DET_MODEL = "PP-OCRv5_mobile_det"
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")

_ENGINE = None


def enabled() -> bool:
    return str(os.environ.get("WA_DEVANAGARI_OCR") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def engine_kwargs() -> dict:
    """和共享服务同一套检测模型，识别模型换成天城文。调用方自己决定在哪个 venv 里构造。"""
    return {
        "use_doc_orientation_classify": False,
        "use_doc_unwarping": False,
        "use_textline_orientation": True,
        "enable_mkldnn": False,
        "text_detection_model_name": DEVANAGARI_DET_MODEL,
        "text_recognition_model_name": DEVANAGARI_REC_MODEL,
    }


def has_devanagari(text: str) -> bool:
    return _DEVANAGARI.search(str(text or "")) is not None


def screen_has_devanagari(screen: Any) -> bool:
    parts = [
        getattr(screen, "header", ""),
        getattr(screen, "composer", ""),
        *list(getattr(screen, "outgoing", None) or []),
        *list(getattr(screen, "incoming", None) or []),
    ]
    return any(has_devanagari(part) for part in parts)


def _load_engine():
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from paddleocr import PaddleOCR
    _ENGINE = PaddleOCR(**engine_kwargs())
    return _ENGINE


def recognize(png: bytes) -> List[dict]:
    """没开开关、或这个 venv 里没有 paddleocr 时返回空列表，调用方继续用原来的 OCR。"""
    if not enabled() or not png:
        return []
    try:
        import io
        import numpy as np
        from PIL import Image
        image = Image.open(io.BytesIO(png)).convert("RGB")
        arr = np.ascontiguousarray(np.array(image)[:, :, ::-1])
        engine = _load_engine()
        result = engine.predict(arr) if hasattr(engine, "predict") else engine.ocr(arr, cls=True)
    except Exception:
        return []
    return _lines_from_result(result)


def _lines_from_result(result) -> List[dict]:
    lines: List[dict] = []
    if not result:
        return lines
    first = result[0]
    try:
        texts = first["rec_texts"]
        polys = first["rec_polys"]
        for index, text in enumerate(texts):
            shown = str(text or "").strip()
            if not shown:
                continue
            poly = polys[index]
            xs = [float(point[0]) for point in poly]
            ys = [float(point[1]) for point in poly]
            lines.append({"text": shown, "box": [min(xs), min(ys), max(xs), max(ys)]})
        return lines
    except Exception:
        return lines


def supplement_screen(screen: Any, png: bytes, recognize_fn: Optional[Callable[[bytes], List[dict]]] = None) -> Any:
    """界面树和共享 OCR 都没有天城文时，才用隔离模型补一行。"""
    if screen_has_devanagari(screen):
        return screen
    reader = recognize_fn
    if reader is None:
        if not enabled():
            return screen
        reader = recognize
    lines = list(reader(png) or [])
    if not any(has_devanagari(str(line.get("text") or "")) for line in lines if isinstance(line, dict)):
        return screen
    from src.integrations.whatsapp_rpa.phone_acceptance import lines_to_screen, png_size
    width, height = png_size(png)
    if width <= 0 or height <= 0:
        width = int(getattr(screen, "shot_size", [0, 0])[0] or 0)
        height = int(getattr(screen, "shot_size", [0, 0])[1] or 0)
    if width <= 0 or height <= 0:
        return screen
    parsed = lines_to_screen(lines, width, height, png)
    parsed.ocr_lines = lines
    parsed.source = "devanagari-ocr"
    parsed.ui_dump = str(getattr(screen, "ui_dump", "") or "")
    return parsed
