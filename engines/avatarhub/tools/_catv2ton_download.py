# -*- coding: utf-8 -*-
"""阶段14 PoC：下载 CatV2TON 512 权重 + EasyAnimateV4 基座（只取推理必需子目录）。

V2TONPipeline 实际只加载 vae/transformer/scheduler——文本编码器（两个 T5，~15G）
根本不 import，跳过。全部落 D:（C: 只剩 45G）。"""
import sys
import time

from huggingface_hub import snapshot_download


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DST = r"D:\models_catv2ton"
t0 = time.time()

print("[DL] EasyAnimateV4-XL-2-InP（vae+transformer+scheduler，~6G）...", flush=True)
p1 = snapshot_download(
    "alibaba-pai/EasyAnimateV4-XL-2-InP",
    local_dir=DST + r"\EasyAnimateV4-XL-2-InP",
    allow_patterns=["vae/*", "transformer/*", "scheduler/*", "model_index.json"],
    max_workers=4,
)
print(f"[DL] 基座完成 → {p1} ({time.time() - t0:.0f}s)", flush=True)

print("[DL] CatV2TON 512-64K 微调权重（~1.6G）...", flush=True)
p2 = snapshot_download(
    "zhengchong/CatV2TON",
    local_dir=DST + r"\CatV2TON",
    allow_patterns=["512-64K/*"],
    max_workers=4,
)
print(f"[DL] 微调权重完成 → {p2} ({time.time() - t0:.0f}s)", flush=True)
print("[DL] ALL DONE", flush=True)
