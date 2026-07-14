# -*- coding: utf-8 -*-
"""跨 env 依赖完整度探针（配合 tryon_candidate_probe 选宿主环境用）。"""
import importlib
import platform

MODS = ["fastapi", "uvicorn", "pydantic", "cv2", "numpy", "onnxruntime",
        "huggingface_hub", "safetensors", "accelerate", "PIL", "einops",
        "scipy", "diffusers", "transformers", "torch"]

print("python:", platform.python_version())
for m in MODS:
    try:
        mod = importlib.import_module(m)
        print(f"  {m}: {getattr(mod, '__version__', 'ok')}")
    except Exception:
        print(f"  {m}: MISSING")
