# -*- coding: utf-8 -*-
"""阶段14 诊断D：一步 x0 重建测试。
干净 latents 加噪到 t → DiT 预测 v → x0_pred = α·x_t − σ·v̂。
x0_pred≈x0 ⇒ 前向/权重/RoPE 全对，坏在采样循环配置；
x0_pred 烂 ⇒ 条件注入（如缺 pose）或前向语义仍有漂移。"""
import sys
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
cloth = Image.open(r"C:\datasets\viton_hd_test\cloth\00069_00.jpg").convert("RGB").resize((W, H))

pipe = V2TONPipeline(base_model_path=r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP",
                     finetuned_model_path=r"D:\models_catv2ton\CatV2TON\512-64K",
                     load_pose=False, torch_dtype=torch.bfloat16, device="cuda")
print("[x0] pipeline loaded", flush=True)

def enc(pil):
    x = torch.from_numpy(np.array(pil)).permute(2, 0, 1)[None, :, None].float() / 127.5 - 1
    return pipe._slice_vae(x.to("cuda", torch.bfloat16))

x0_person = enc(person)
x0_cloth = enc(cloth)
zeros_lat = pipe._slice_vae(torch.zeros(1, 3, 1, H, W, device="cuda", dtype=torch.bfloat16))

# 训练态输入组装：inpaint = [mask_lat, masked_lat]，序列 = [衣, 人]
# 全图可见（mask=全 0 图）→ masked=原图：模型只需「复读」输入
mask_full_visible = pipe._slice_vae(torch.zeros(1, 3, 1, H, W, device="cuda", dtype=torch.bfloat16))
masked_cat = torch.cat([x0_cloth, x0_person], dim=2)
mask_cat = torch.cat([mask_full_visible, mask_full_visible], dim=2)
inpaint = torch.cat([mask_cat, masked_cat], dim=1)

sched = pipe.noise_scheduler
x0 = torch.cat([x0_cloth, x0_person], dim=2)
added = pipe.prepare_dit_added_args(x0.shape[3] * 8, x0.shape[4] * 8, 1)

for t_int in (100, 500, 900):
    g = torch.Generator(device="cuda").manual_seed(0)
    noise = torch.randn(x0.shape, generator=g, device="cuda", dtype=torch.float32).to(torch.bfloat16)
    t = torch.tensor([t_int], device="cuda")
    xt = sched.add_noise(x0.float(), noise.float(), t).to(torch.bfloat16)
    with torch.no_grad():
        v = pipe.transformer3d(xt, t.to(torch.bfloat16), pose_emb=None,
                               inpaint_latents=inpaint, return_dict=False, **added)[0]
    v = v.chunk(2, dim=1)[0].float()
    a = sched.alphas_cumprod.to("cuda")[t_int]
    x0_pred = (a.sqrt() * xt.float() - (1 - a).sqrt() * v)
    err = (x0_pred - x0.float()).abs().mean().item()
    base = (xt.float() - x0.float()).abs().mean().item()
    print(f"[x0] t={t_int}: |x0_pred-x0|={err:.3f}  |x_t-x0|={base:.3f}  "
          f"（好模型应 err ≪ base）", flush=True)
    if t_int == 500:
        rec = pipe.decode_latents(x0_pred.to(torch.bfloat16))
        img = ((rec[0, :, 1].permute(1, 2, 0) * 0.5 + 0.5).clamp(0, 1) * 255).byte().numpy()
        pair = np.hstack([np.array(person), img])
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(pair, cv2.COLOR_RGB2BGR),
                               [cv2.IMWRITE_JPEG_QUALITY, 92])
        (OUT / "x0_recon_t500.jpg").write_bytes(buf.tobytes())
        print("[x0] saved x0_recon_t500.jpg", flush=True)
