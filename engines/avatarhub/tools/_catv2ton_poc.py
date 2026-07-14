# -*- coding: utf-8 -*-
"""阶段14 PoC：CatV2TON 512 视频试衣在本机（fitdit 环境 + 垫片）的可行性实测。

链路（诊断E定论：DensePose 条件是画质命门，load_pose=False 出纯噪声）：
  ① 输入：Ditto 微动人像视频（真实头动/呼吸——正是未来 body_video 链的底片形态）
     + VITON-HD 服装平铺图。
  ② mask：逐帧跑 FitDiT 的人体解析(onnx, CPU) + DWpose → get_mask_location。
  ③ pose：vendored DensePose（fvcore+av 补齐后 Windows 纯 Python 可跑，免编译）
     逐帧灰度分割 → viridis 上色 → [-1,1]，与训练分布一致。
  ④ 推理：V2TONPipeline.video_try_on，load_pose=True，bf16 + AdaCN；384x512。
  ⑤ 观测：显存峰值 / 每帧耗时 / 输出落盘 logs/catv2ton_poc/。
显存护栏：free < 10G 直接拒跑（不打扰直播/其他服务）。"""
import importlib
import sys
import time
from pathlib import Path

import numpy as np
import torch


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\模仿音色\tools")
sys.path.insert(0, r"C:\CatV2TON")
import _catv2ton_shim  # noqa: F401  垫片先行（pkg_resources/MT5/torchvision.io/modules 骨架）

OUT = Path(r"c:\模仿音色\logs\catv2ton_poc")
OUT.mkdir(parents=True, exist_ok=True)
BASE = r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP"
FT = r"D:\models_catv2ton\CatV2TON\512-64K"
FITDIT_ROOT = r"C:\models\FitDiT"
FITDIT_CODE = r"C:\FitDiT"
W, H = 384, 512
PERSON_VIDEO = OUT / "person_motion.mp4"
CLOTH_IMG = Path(r"C:\datasets\viton_hd_test\cloth\00069_00.jpg")

free0 = torch.cuda.mem_get_info()[0] / 2**30
print(f"[PoC] free={free0:.1f}G", flush=True)
if free0 < 10:
    print("[PoC] 显存不足 10G，避让直播/其他任务，退出")
    sys.exit(2)

import cv2
from PIL import Image

# ── 双库同名顶层模块(utils/datasets)互相遮蔽的导入顺序舞步 ─────────────────
# CatV2TON 管线+DensePose 先在自家路径 import 完毕并持有引用 → 清 utils/datasets
# 缓存 → FitDiT 路径优先，导入解析/姿态预处理。两边绑定后互不再碰。
from modules.pipeline import V2TONPipeline            # noqa: E402

_dp_mod = importlib.import_module("modules.densepose")

sys.path.remove(r"C:\CatV2TON")
sys.path.insert(0, FITDIT_CODE := r"C:\FitDiT")
sys.path.insert(0, FITDIT_CODE + r"\preprocess\humanparsing")
for _n in [k for k in list(sys.modules) if k == "utils" or k.startswith("utils.")
           or k == "datasets" or k.startswith("datasets.")]:
    del sys.modules[_n]
from preprocess.dwpose import DWposeDetector          # noqa: E402
from preprocess.humanparsing.run_parsing import Parsing  # noqa: E402
from src.utils_mask import get_mask_location          # noqa: E402

# ── ① 读人像视频帧 ────────────────────────────────────────────────────────
cap = cv2.VideoCapture(str(PERSON_VIDEO))
frames = []
while True:
    ok, f = cap.read()
    if not ok:
        break
    frames.append(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
fps = cap.get(cv2.CAP_PROP_FPS) or 24
cap.release()
frames = frames[:48]                       # PoC 截 48 帧（2s@24fps）
print(f"[PoC] 人像视频 {len(frames)} 帧 @{fps:.0f}fps {frames[0].shape}", flush=True)

# ── ② FitDiT 同款遮罩：解析+姿态一次/帧（CPU onnx，不占显存）────────────────
t0 = time.time()
parser = Parsing(model_root=FITDIT_ROOT, device="cpu")
dwpose = DWposeDetector(model_root=FITDIT_ROOT, device="cpu")
print(f"[PoC] 预处理器就绪 {time.time() - t0:.1f}s", flush=True)

t0 = time.time()
masks = []
for i, fr in enumerate(frames):
    pil = Image.fromarray(fr).resize((W, H))
    pose_img, _, _, candidate = dwpose(np.array(pil)[:, :, ::-1])
    candidate[candidate < 0] = 0
    candidate = candidate[0]
    candidate[:, 0] *= pil.width
    candidate[:, 1] *= pil.height
    parse, _ = parser(pil)
    m, _ = get_mask_location("Upper-body", parse, candidate,
                             parse.width, parse.height, 0, 0, 0, 0)
    masks.append(np.array(m.resize((W, H)).convert("L")))
mask_ms = (time.time() - t0) * 1000 / len(frames)
print(f"[PoC] 逐帧遮罩完成 {mask_ms:.0f}ms/帧", flush=True)

# ── ②b 逐帧 DensePose（GPU，~0.5s/帧）────────────────────────────────────
t0 = time.time()
dp = _dp_mod.DensePose(model_path=r"D:\models_catv2ton\CatVTON\DensePose", device="cuda")
poses = []
for fr in frames:
    g = dp(Image.fromarray(fr), resize=1024).resize((W, H))
    poses.append(np.array(_dp_mod.densepose_to_rgb(g, colormap=cv2.COLORMAP_VIRIDIS)))
pose_ms = (time.time() - t0) * 1000 / len(frames)
print(f"[PoC] 逐帧 DensePose 完成 {pose_ms:.0f}ms/帧", flush=True)

# ── ③ 组张量 [B,C,T,H,W] ────────────────────────────────────────────────
person = np.stack([cv2.resize(f, (W, H)) for f in frames])          # T,H,W,C
person_t = torch.from_numpy(person).permute(3, 0, 1, 2).unsqueeze(0).float() / 255 * 2 - 1
mask_t = torch.from_numpy(np.stack(masks)).unsqueeze(-1).repeat(1, 1, 1, 3)
mask_t = mask_t.permute(3, 0, 1, 2).unsqueeze(0).float() / 255
cloth = Image.open(CLOTH_IMG).convert("RGB").resize((W, H))
cloth_t = torch.from_numpy(np.array(cloth)).permute(2, 0, 1).unsqueeze(0).float() / 255 * 2 - 1
cloth_t = cloth_t.unsqueeze(2)                                       # B,C,1,H,W
pose_t = torch.from_numpy(np.stack(poses)).permute(3, 0, 1, 2).unsqueeze(0).float() / 255 * 2 - 1
# 帧数补到 4 的倍数（MagViT 时间压缩率）
if person_t.size(2) % 4:
    pad = 4 - person_t.size(2) % 4
    person_t = torch.cat([person_t, person_t[:, :, -1:].repeat(1, 1, pad, 1, 1)], 2)
    mask_t = torch.cat([mask_t, mask_t[:, :, -1:].repeat(1, 1, pad, 1, 1)], 2)
    pose_t = torch.cat([pose_t, pose_t[:, :, -1:].repeat(1, 1, pad, 1, 1)], 2)
n_frames = person_t.size(2)
print(f"[PoC] 张量就绪 person={tuple(person_t.shape)} mask={tuple(mask_t.shape)}", flush=True)

# ── ④ 加载管线 + 推理 ────────────────────────────────────────────────────
t0 = time.time()
pipe = V2TONPipeline(base_model_path=BASE, finetuned_model_path=FT,
                     load_pose=True, torch_dtype=torch.bfloat16, device="cuda")
load_s = time.time() - t0
torch.cuda.reset_peak_memory_stats()
print(f"[PoC] 管线加载 {load_s:.0f}s，驻留 "
      f"{torch.cuda.memory_allocated() / 2**30:.1f}G", flush=True)

STEPS = int(sys.argv[1]) if len(sys.argv) > 1 else 15
GUID = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
print(f"[PoC] steps={STEPS} guidance={GUID}", flush=True)
t0 = time.time()
with torch.no_grad():
    out = pipe.video_try_on(
        source_video=person_t, mask_video=mask_t, condition_image=cloth_t,
        pose_video=pose_t, num_inference_steps=STEPS, guidance_scale=GUID,
        slice_frames=24, pre_frames=8,
        generator=torch.Generator(device="cuda").manual_seed(42), use_adacn=True)
infer_s = time.time() - t0
peak = torch.cuda.max_memory_allocated() / 2**30
print(f"[PoC] 推理完成 {infer_s:.0f}s（{infer_s * 1000 / n_frames:.0f}ms/帧），"
      f"显存峰值 {peak:.1f}G", flush=True)

# ── ⑤ repaint（背景还原）+ 落盘 ─────────────────────────────────────────
res = out.permute(0, 4, 1, 2, 3).float().cpu()                       # B,C,T,H,W
mask_soft = torch.nn.functional.avg_pool2d(
    mask_t.squeeze(0).permute(1, 0, 2, 3), 11, stride=1, padding=5)
mask_soft = mask_soft.permute(1, 0, 2, 3).unsqueeze(0)
final = person_t * (1 - mask_soft) + res * mask_soft
final = ((final.squeeze(0).permute(1, 2, 3, 0) * 0.5 + 0.5).clamp(0, 1) * 255).byte().numpy()

vw = cv2.VideoWriter(str(OUT / "tryon_result.mp4"),
                     cv2.VideoWriter_fourcc(*"mp4v"), fps, (W, H))
for f in final[:len(frames)]:
    vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
vw.release()
cv2.imwrite(str(OUT / "frame_first.jpg"), cv2.cvtColor(final[0], cv2.COLOR_RGB2BGR))
cv2.imwrite(str(OUT / "frame_mid.jpg"), cv2.cvtColor(final[len(frames) // 2], cv2.COLOR_RGB2BGR))
cv2.imwrite(str(OUT / "frame_last.jpg"), cv2.cvtColor(final[len(frames) - 1], cv2.COLOR_RGB2BGR))

# 时序稳定性粗测：相邻帧衣区 L1（越小越稳；同时给源视频同区域作基准）
m = np.stack(masks)[:len(frames)] / 255.0
res_seq = final[:len(frames)].astype(np.float32)
src_seq = person[:len(frames)].astype(np.float32)
d_res = np.mean([np.abs(res_seq[i + 1] - res_seq[i])[m[i] > 0.5].mean()
                 for i in range(len(frames) - 1) if (m[i] > 0.5).any()])
d_src = np.mean([np.abs(src_seq[i + 1] - src_seq[i])[m[i] > 0.5].mean()
                 for i in range(len(frames) - 1) if (m[i] > 0.5).any()])
print(f"[PoC] 时序抖动(衣区相邻帧L1) 结果={d_res:.2f} vs 源视频={d_src:.2f} "
      f"（比值 {d_res / max(d_src, 1e-6):.2f}，≈1 即与源片同稳）", flush=True)
print(f"[PoC] 输出 → {OUT}", flush=True)
