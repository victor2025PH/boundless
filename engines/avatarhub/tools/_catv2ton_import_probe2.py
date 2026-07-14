# -*- coding: utf-8 -*-
"""阶段14 PoC：垫片后的 V2TONPipeline 导入探针（仍不加载权重）。"""
import sys


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\模仿音色\tools")
sys.path.insert(0, r"C:\CatV2TON")
import _catv2ton_shim  # noqa: F401  垫片先行

try:
    from modules.pipeline import V2TONPipeline
    print("[OK] V2TONPipeline import（垫片生效）")
except Exception as e:
    print(f"[NG] V2TONPipeline: {type(e).__name__}: {e}")
    raise SystemExit(1)

import inspect

sig = inspect.signature(V2TONPipeline.__init__)
print("init sig:", list(sig.parameters))
sig2 = inspect.signature(V2TONPipeline.video_try_on)
print("video_try_on sig:", list(sig2.parameters))
