# -*- coding: utf-8 -*-
"""openapi 500 离线复现：从 faceswap_api.py 抽 pydantic 模型源码（不 import 整个
服务=不拉起模型），塞进干净 FastAPI app 生成 schema，拿到完整 traceback 定位根因。"""
import re
import sys
import traceback
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fastapi
import pydantic

print(f"fastapi={fastapi.__version__} pydantic={pydantic.VERSION} py={sys.version.split()[0]}")

src = Path(r"c:\模仿音色\faceswap_api.py").read_text(encoding="utf-8")

# 抽 class X(BaseModel) 块（到下一个顶级 def/class 为止）
models = re.findall(r"^class \w+\(BaseModel\):\n(?:(?:[ \t].*)?\n)+?(?=^\S)", src, re.M)
print(f"抽到 {len(models)} 个模型: {[m.split('(')[0][6:] for m in models]}")

ns: dict = {}
exec("from pydantic import BaseModel\n"
     "from typing import Optional, List, Dict, Any\n"
     "import numpy as np\n" + "\n".join(models), ns)

from fastapi import FastAPI

app = FastAPI()
model_classes = {k: v for k, v in ns.items()
                 if isinstance(v, type) and issubclass(v, ns["BaseModel"]) and k != "BaseModel"}

# 逐个模型建路由，二分定位坏模型
for name, cls in model_classes.items():
    a = FastAPI()

    def mk(c):
        def h(body: c):          # noqa
            return {}
        return h
    a.post(f"/x_{name}", response_model=None)(mk(cls))
    try:
        a.openapi()
        print(f"  [OK] {name}")
    except Exception as e:
        print(f"  [FAIL] {name}: {type(e).__name__}: {str(e)[:200]}")
        traceback.print_exc(limit=6)
