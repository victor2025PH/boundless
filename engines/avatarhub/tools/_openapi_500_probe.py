# -*- coding: utf-8 -*-
"""定位 faceswap /openapi.json 500 根因：本地直接构造 FastAPI schema，打印完整异常。
不起服务不吃 GPU——只 import faceswap_api 的 pydantic 模型层？import 会拉起模型加载，
所以改为：从运行中的 8000 抓 /openapi.json 的响应体（FastAPI 会回 traceback 摘要），
再对照本地源码里的模型定义找可疑字段。"""
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

r = requests.get("http://127.0.0.1:8000/openapi.json", timeout=10)
print("status:", r.status_code)
print("body[:800]:", r.text[:800])
