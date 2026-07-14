# -*- coding: utf-8 -*-
"""阶段14 PoC：fitdit 环境下 V2TONPipeline 导入探针（不加载权重，只验 API 兼容）。
CatV2TON 代码是 diffusers~0.30 时代写的，fitdit 是 0.38——drift 点在这一步全暴露。"""
import sys


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\CatV2TON")

try:
    from easyanimate.models.autoencoder_magvit import AutoencoderKLMagvit
    print("[OK] AutoencoderKLMagvit")
except Exception as e:
    print(f"[NG] AutoencoderKLMagvit: {type(e).__name__}: {e}")

try:
    from easyanimate.models.transformer3d import HunyuanTransformer3DModel
    print("[OK] HunyuanTransformer3DModel")
except Exception as e:
    print(f"[NG] HunyuanTransformer3DModel: {type(e).__name__}: {e}")

try:
    from easyanimate.pipeline.pipeline_easyanimate_multi_text_encoder import (
        get_2d_rotary_pos_embed, get_resize_crop_region_for_grid)
    print("[OK] pipeline_easyanimate_multi_text_encoder helpers")
except Exception as e:
    print(f"[NG] pipeline helpers: {type(e).__name__}: {e}")

try:
    from modules.pipeline import V2TONPipeline
    print("[OK] V2TONPipeline")
except Exception as e:
    print(f"[NG] V2TONPipeline: {type(e).__name__}: {e}")

try:
    from modules.cloth_masker import AutoMasker
    print("[OK] AutoMasker (densepose+SCHP)")
except Exception as e:
    print(f"[NG] AutoMasker: {type(e).__name__}: {e}")
