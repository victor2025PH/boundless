# -*- coding: utf-8 -*-
"""阶段10：发型显存自动归还线上触发复验（判定纯函数已单测，这里验线上闭环）。
前置侦察：8001 模型驻留态 + 当前空闲显存。驻留时才可能触发自动归还。"""
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

r = requests.get("http://127.0.0.1:8001/health", timeout=6)
print("hair health:", r.json())
try:
    import torch
    free, total = torch.cuda.mem_get_info()
    print(f"vram free={free / 1024**3:.1f}G / total={total / 1024**3:.1f}G")
except Exception as e:
    print("torch 不可用:", str(e)[:80])
