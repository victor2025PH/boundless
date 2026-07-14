# -*- coding: utf-8 -*-
"""阶段14 诊断C：分层定位。
① VAE 往返：encode→decode 应还原人像（坏=基座/精度问题）。
② transformer 单步：输出 NaN/数值范围检查（坏=DiT 前向问题）。"""
import sys
import time
from pathlib import Path

import numpy as np
import torch


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\模仿音色\tools")
sys.path.insert(0, r"C:\CatV2TON")
import _catv2ton_shim  # noqa: F401

import cv2
from PIL import Image

from modules.pipeline import V2TONPipeline  # noqa: E402

OUT = Path(r"c:\模仿音色\logs\catv2ton_poc")
W, H = 384, 512
person = Image.open(sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[10]).convert("RGB").resize((W, H))

pipe = V2TONPipeline(base_model_path=r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP",
                     finetuned_model_path=r"D:\models_catv2ton\CatV2TON\512-64K",
                     load_pose=False, torch_dtype=torch.bfloat16, device="cuda")
print("[diag] pipeline loaded", flush=True)

x = torch.from_numpy(np.array(person)).permute(2, 0, 1)[None, :, None].float() / 127.5 - 1
x = x.to("cuda", torch.bfloat16)                       # B,C,1,H,W

# ── ① VAE 往返 ──
lat = pipe._slice_vae(x)
print(f"[diag] latents shape={tuple(lat.shape)} std={lat.float().std():.3f} "
      f"nan={torch.isnan(lat).any().item()}", flush=True)
rec = pipe.decode_latents(lat)
rec_img = ((rec[0, :, 0].permute(1, 2, 0) * 0.5 + 0.5).clamp(0, 1) * 255).byte().numpy()
pair = np.hstack([np.array(person), rec_img])
ok, buf = cv2.imencode(".jpg", cv2.cvtColor(pair, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
(OUT / "vae_roundtrip.jpg").write_bytes(buf.tobytes())
err = np.abs(pair[:, :W].astype(float) - pair[:, W:].astype(float)).mean()
print(f"[diag] VAE 往返 L1={err:.1f}（<15 算正常）→ vae_roundtrip.jpg", flush=True)

# ── ② transformer 单步数值 ──
lat2 = torch.cat([lat, lat], dim=2)                    # 2 帧（衣+人）
mask_lat = torch.zeros_like(lat2)
inpaint = torch.cat([mask_lat, lat2], dim=1)           # B,2C,F,h,w
noisy = torch.randn_like(lat2)
tt = torch.tensor([500.0], device="cuda", dtype=torch.bfloat16)
added = pipe.prepare_dit_added_args(lat2.shape[3] * 8, lat2.shape[4] * 8, 1)
with torch.no_grad():
    out = pipe.transformer3d(noisy, tt, pose_emb=None, inpaint_latents=inpaint,
                             return_dict=False, **added)[0]
print(f"[diag] DiT out shape={tuple(out.shape)} std={out.float().std():.3f} "
      f"mean={out.float().mean():.3f} nan={torch.isnan(out).any().item()} "
      f"inf={torch.isinf(out).any().item()}", flush=True)
# v-pred 目标量级应 ~1；顺带看噪声不同 timestep 是否有响应差异
for t in (999.0, 100.0):
    with torch.no_grad():
        o2 = pipe.transformer3d(noisy, torch.tensor([t], device="cuda", dtype=torch.bfloat16),
                                pose_emb=None, inpaint_latents=inpaint,
                                return_dict=False, **added)[0]
    print(f"[diag] t={t:.0f} out std={o2.float().std():.3f} Δvs500={(o2 - out).abs().float().mean():.4f}",
          flush=True)
