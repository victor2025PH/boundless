"""可选的天城文识别。只在 117 的 zl_accept venv 里加载，不进共享 OCR 服务。

共享的 ``scripts/ocr176/ocr_server.py`` 仍只收 ``image_b64``，模型仍是
``PP-OCRv5_mobile_rec``。不要改那个进程，也不要往 :8766 加语言参数。

在 117 上只给 zl_accept 装模型::

    powershell -File engines\\chengjie\\scripts\\install_devanagari_zl_accept.ps1

装完后在跑验收的那个窗口里::

    set WA_DEVANAGARI_OCR=1
    set PADDLE_PDX_CACHE_HOME=<脚本打印出来的 venv 内缓存目录>

界面树已经读到文字时不会调用这里。打开开关后，只对气泡区域跑这个模型，
认出天城文才替换那一块。键盘区域不送去识别。
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, List, Optional, Sequence

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


def message_band_lines(screen: Any) -> List[dict]:
    """顶栏以下、键盘以上的文字框。键盘和占位符不拿去跑天城文模型。"""
    from src.integrations.whatsapp_rpa.phone_send import composer_is_placeholder, is_keycap

    size = getattr(screen, "shot_size", None) or [0, 0]
    height = float(size[1] or 0) if len(size) >= 2 else 0.0
    chosen: List[dict] = []
    for line in getattr(screen, "ocr_lines", None) or []:
        if not isinstance(line, dict):
            continue
        text = str(line.get("text") or "")
        box = line.get("box") or []
        if len(box) < 4 or composer_is_placeholder(text) or is_keycap(text):
            continue
        top = float(box[1])
        bottom = float(box[3])
        cy = (top + bottom) / 2.0
        if height:
            if cy < height * 0.18 or cy > height * 0.70:
                continue
        elif top >= 1200:
            continue
        chosen.append(line)
    return chosen


def crop_png(png: bytes, box: Sequence) -> bytes:
    import io
    from PIL import Image
    image = Image.open(io.BytesIO(png))
    x1, y1, x2, y2 = (int(float(box[0])), int(float(box[1])), int(float(box[2])), int(float(box[3])))
    x1 = max(0, min(x1, image.width - 1))
    y1 = max(0, min(y1, image.height - 1))
    x2 = max(x1 + 1, min(x2, image.width))
    y2 = max(y1 + 1, min(y2, image.height))
    buf = io.BytesIO()
    image.crop((x1, y1, x2, y2)).save(buf, format="PNG")
    return buf.getvalue()


def _recognize_crop_text(png: bytes) -> str:
    return "\n".join(
        str(line.get("text") or "")
        for line in recognize(png)
        if has_devanagari(str(line.get("text") or ""))
    )


def apply_devanagari_bubbles(
    screen: Any, png: bytes, recognize_crop: Optional[Callable[[bytes], str]] = None,
) -> Any:
    """只重认气泡区域。模型读出天城文才替换；中文气泡和键盘保持原样。"""
    if screen_has_devanagari(screen) and str(getattr(screen, "source", "") or "") == "uiautomator":
        return screen
    reader = recognize_crop
    if reader is None:
        if not enabled():
            return screen
        reader = _recognize_crop_text
    regions = message_band_lines(screen)
    if not regions or not png:
        return screen
    region_ids = {id(line) for line in regions}
    new_lines: List[dict] = []
    changed = False
    for line in getattr(screen, "ocr_lines", None) or []:
        if not isinstance(line, dict) or id(line) not in region_ids:
            new_lines.append(line)
            continue
        try:
            text = str(reader(crop_png(png, line.get("box") or [])) or "").strip()
        except Exception:
            text = ""
        if has_devanagari(text):
            new_lines.append({"text": text, "box": list(line.get("box") or [])})
            changed = True
        else:
            new_lines.append(line)
    if not changed:
        return screen
    from src.integrations.whatsapp_rpa.phone_acceptance import lines_to_screen, png_size
    width, height = png_size(png)
    if width <= 0 or height <= 0:
        size = getattr(screen, "shot_size", None) or [0, 0]
        width = int(size[0] or 0) if size else 0
        height = int(size[1] or 0) if len(size) >= 2 else 0
    if width <= 0 or height <= 0:
        return screen
    parsed = lines_to_screen(new_lines, width, height, png)
    parsed.ocr_lines = new_lines
    parsed.source = "devanagari-ocr"
    parsed.ui_dump = str(getattr(screen, "ui_dump", "") or "")
    return parsed


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
