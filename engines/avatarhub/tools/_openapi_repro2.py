# -*- coding: utf-8 -*-
"""openapi 500 全量复现：真 import faceswap_api（FACESWAP_CUDA=0 走 CPU 免占显存，
模型加载慢点但一次性诊断），调 app.openapi() 打完整 traceback → 定位坏路由/坏字段。"""
import os
import sys
import traceback

os.environ["FACESWAP_CUDA"] = "0"     # 诊断跑 CPU，不碰显存
os.environ.setdefault("FACESWAP_TRT", "0")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\模仿音色")

import faceswap_api  # noqa: E402  （模块级加载模型，CPU ~20s）

try:
    schema = faceswap_api.app.openapi()
    print("[OK] openapi 生成成功，paths:", len(schema.get("paths", {})))
except Exception:
    traceback.print_exc()
    # 二分：逐路由单独生成，找出坏的那个
    from fastapi import FastAPI
    from fastapi.openapi.utils import get_openapi
    print("\n== 逐路由定位 ==")
    for r in faceswap_api.app.routes:
        path = getattr(r, "path", "?")
        try:
            get_openapi(title="x", version="1", routes=[r])
        except Exception as e:
            print(f"  [BAD] {path}: {type(e).__name__}: {str(e)[:220]}")
