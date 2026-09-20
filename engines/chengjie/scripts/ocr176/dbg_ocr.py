# -*- coding: utf-8 -*-
"""176 本机诊断：paddleocr 3.x predict 对 path / ndarray 两种输入的真实行为。"""
import traceback

import numpy as np
from paddleocr import PaddleOCR
from PIL import Image, ImageDraw

img = Image.new("RGB", (420, 120), (255, 255, 255))
ImageDraw.Draw(img).text((12, 30), "Hello world 123 test", fill=(0, 0, 0))
img.save(r"C:\aitr_ocr\dbg.png")

o = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
              use_textline_orientation=True)

for label, inp in (
    ("path", r"C:\aitr_ocr\dbg.png"),
    ("ndarray_bgr_contig", np.ascontiguousarray(np.array(img)[:, :, ::-1])),
):
    try:
        r = o.predict(inp)
        first = r[0]
        try:
            print(label, "OK texts=", list(first["rec_texts"]))
        except Exception as e2:  # noqa: BLE001
            print(label, "extract-fail", type(e2).__name__, str(e2)[:150])
            print(label, "keys:", getattr(first, "keys", lambda: "?")())
    except Exception as e:  # noqa: BLE001
        print(label, "FAIL", type(e).__name__, str(e)[:200])
        traceback.print_exc()
