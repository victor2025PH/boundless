# -*- coding: utf-8 -*-
"""阶段14 诊断E：带 DensePose 条件的单帧 image_try_on（假设验证：pose 是画质命门）。"""
import importlib
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

import modules  # noqa: F401  骨架包
from modules.pipeline import V2TONPipeline  # noqa: E402

dp_mod = importlib.import_module("modules.densepose")

OUT = Path(r"c:\模仿音色\logs\catv2ton_poc")
W, H = 384, 512
person = Image.open(sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[10]).convert("RGB").resize((W, H))
cloth = Image.open(r"C:\datasets\viton_hd_test\cloth\00069_00.jpg").convert("RGB").resize((W, H))

dp = dp_mod.DensePose(model_path=r"D:\models_catv2ton\CatVTON\DensePose", device="cuda")
pose_gray = dp(person, resize=1024).resize((W, H))
print("[pose] densepose ready", flush=True)

# FitDiT 遮罩（换到 FitDiT 路径前先把管线相关模块全部 import 完毕）
pipe = V2TONPipeline(base_model_path=r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP",
                     finetuned_model_path=r"D:\models_catv2ton\CatV2TON\512-64K",
                     load_pose=True, torch_dtype=torch.bfloat16, device="cuda")
print("[pose] pipeline loaded (posenet on)", flush=True)

sys.path.insert(0, r"C:\FitDiT")
sys.path.insert(0, r"C:\FitDiT\preprocess\humanparsing")
for _n in [k for k in list(sys.modules) if k == "utils" or k.startswith("utils.")]:
    del sys.modules[_n]
from preprocess.dwpose import DWposeDetector          # noqa: E402
from preprocess.humanparsing.run_parsing import Parsing  # noqa: E402
from src.utils_mask import get_mask_location          # noqa: E402

parser = Parsing(model_root=r"C:\models\FitDiT", device="cpu")
dwpose = DWposeDetector(model_root=r"C:\models\FitDiT", device="cpu")
_, _, _, candidate = dwpose(np.array(person)[:, :, ::-1])
candidate[candidate < 0] = 0
candidate = candidate[0]
candidate[:, 0] *= person.width
candidate[:, 1] *= person.height
parse, _ = parser(person)
mask_pil, _ = get_mask_location("Upper-body", parse, candidate,
                                parse.width, parse.height, 0, 0, 0, 0)
mask_pil = mask_pil.resize((W, H)).convert("L")
print("[pose] mask ready", flush=True)

t0 = time.time()
outs = pipe.image_try_on(
    source_image=person, source_mask=mask_pil, conditioned_image=cloth,
    pose_image=pose_gray, num_inference_steps=20, guidance_scale=2.5,
    generator=torch.Generator(device="cuda").manual_seed(42))
print(f"[pose] tryon done {time.time() - t0:.0f}s", flush=True)

raw = np.array(outs[0])
# repaint：遮罩外还原源像素
m = (np.array(mask_pil, dtype=np.float32) / 255.0)[..., None]
m = cv2.GaussianBlur(m, (11, 11), 0)[..., None] if m.ndim == 2 else m
blend = (np.array(person).astype(np.float32) * (1 - m) + raw.astype(np.float32) * m).astype(np.uint8)
quad = np.hstack([np.array(person), np.array(cloth), raw, blend])
ok, buf = cv2.imencode(".jpg", cv2.cvtColor(quad, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
(OUT / "img_tryon_pose.jpg").write_bytes(buf.tobytes())
print("[pose] saved img_tryon_pose.jpg（源|衣|原始|重绘）", flush=True)
