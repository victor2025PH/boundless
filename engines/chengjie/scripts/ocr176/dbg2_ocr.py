# -*- coding: utf-8 -*-
"""176 兼容矩阵试验：oneDNN/PIR 开关 × v5/v6 模型（每配置独立进程跑）。"""
import os
import sys

mode = sys.argv[1] if len(sys.argv) > 1 else "v6"
if "nomkldnn" in mode:
    os.environ["FLAGS_use_mkldnn"] = "0"
if "nopir" in mode:
    os.environ["FLAGS_enable_pir_api"] = "0"
    os.environ["FLAGS_enable_pir_in_executor"] = "0"

from paddleocr import PaddleOCR  # noqa: E402

kw = dict(use_doc_orientation_classify=False, use_doc_unwarping=False,
          use_textline_orientation=False)
if "v5" in mode:
    kw.update(text_detection_model_name="PP-OCRv5_mobile_det",
              text_recognition_model_name="PP-OCRv5_mobile_rec")
if "kwnomkl" in mode:
    kw.update(enable_mkldnn=False)
try:
    o = PaddleOCR(**kw)
    r = o.predict(r"C:\aitr_ocr\dbg.png")
    print("RESULT", mode, "OK", list(r[0]["rec_texts"]))
except Exception as e:  # noqa: BLE001
    print("RESULT", mode, "FAIL", type(e).__name__, str(e)[:140])
