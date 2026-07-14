# -*- coding: utf-8 -*-
"""阶段14 诊断B：单帧 image_try_on 原始输出（无重绘）。
判别：非遮罩区能否还原人物 → 区分「全局数学坏」vs「服装条件注入坏」。"""
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

from modules.pipeline import V2TONPipeline  # noqa: E402  在 FitDiT 路径进场前导入

OUT = Path(r"c:\模仿音色\logs\catv2ton_poc")
W, H = 384, 512

sys.path.insert(0, r"C:\FitDiT")
sys.path.insert(0, r"C:\FitDiT\preprocess\humanparsing")
for _n in [k for k in list(sys.modules) if k == "utils" or k.startswith("utils.")]:
    del sys.modules[_n]
from preprocess.dwpose import DWposeDetector          # noqa: E402
from preprocess.humanparsing.run_parsing import Parsing  # noqa: E402
from src.utils_mask import get_mask_location          # noqa: E402

person_p = sorted(Path(r"C:\datasets\viton_hd_test\image").glob("*.jpg"))[10]
cloth_p = Path(r"C:\datasets\viton_hd_test\cloth\00069_00.jpg")
person = Image.open(person_p).convert("RGB").resize((W, H))
cloth = Image.open(cloth_p).convert("RGB").resize((W, H))

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
print("[imgtest] mask ready", flush=True)

pipe = V2TONPipeline(base_model_path=r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP",
                     finetuned_model_path=r"D:\models_catv2ton\CatV2TON\512-64K",
                     load_pose=False, torch_dtype=torch.bfloat16, device="cuda")
print("[imgtest] pipeline loaded", flush=True)

t0 = time.time()
outs = pipe.image_try_on(
    source_image=person, source_mask=mask_pil, conditioned_image=cloth,
    num_inference_steps=20, guidance_scale=2.5,
    generator=torch.Generator(device="cuda").manual_seed(42))
print(f"[imgtest] done {time.time() - t0:.0f}s", flush=True)

raw = np.array(outs[0])
trip = np.hstack([np.array(person), np.array(cloth), raw])
ok, buf = cv2.imencode(".jpg", cv2.cvtColor(trip, cv2.COLOR_RGB2BGR),
                       [cv2.IMWRITE_JPEG_QUALITY, 92])
(OUT / "img_tryon_raw.jpg").write_bytes(buf.tobytes())
print("[imgtest] saved img_tryon_raw.jpg（左源|中衣|右原始输出）", flush=True)
