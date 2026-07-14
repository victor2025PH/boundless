# -*- coding: utf-8 -*-
"""阶段14 PoC：检查 fitdit 环境是否满足 CatV2TON(V2TONPipeline+easyanimate) 依赖。"""
import importlib
import sys


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
mods = ["torch", "diffusers", "accelerate", "einops", "cv2", "omegaconf",
        "transformers", "torchvision", "av", "imageio", "decord", "timm", "tqdm"]
missing = []
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f"{m}: {getattr(mod, '__version__', '?')}")
    except ImportError as e:
        print(f"{m}: MISSING ({str(e)[:60]})")
        missing.append(m)
import torch

print("cuda:", torch.cuda.is_available(), torch.version.cuda)
print("MISSING:", missing or "none")
