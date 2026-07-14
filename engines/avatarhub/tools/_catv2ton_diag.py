# -*- coding: utf-8 -*-
"""阶段14 诊断：衣区纯噪声的根因二分。
① attention 微调权重的 key 是否与 attn_blocks 命名对齐（错位=静默没加载=噪声）。
② posenet 检查点结构（判断 pose 条件是否训练必备）。
③ 单帧 image_try_on 无 pose 快测——图像路径干净则问题在视频/时序，反之在全局。"""
import sys


sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"c:\模仿音色\tools")
sys.path.insert(0, r"C:\CatV2TON")
import _catv2ton_shim  # noqa: F401

from pathlib import Path

from safetensors import safe_open


FT = Path(r"D:\models_catv2ton\CatV2TON\512-64K")

print("── ① 检查点 key 形态 ──")
with safe_open(FT / "attention" / "model.safetensors", framework="pt", device="cpu") as f:
    keys = list(f.keys())
print(f"attention ckpt: {len(keys)} keys")
print("  head:", keys[:4])
print("  tail:", keys[-2:])
with safe_open(FT / "posenet" / "model.safetensors", framework="pt", device="cpu") as f:
    pkeys = list(f.keys())
print(f"posenet ckpt: {len(pkeys)} keys; head: {pkeys[:3]}")

print("── ② attn_blocks 模块名对齐 ──")
import torch

from modules.pipeline import init_transformer3d_model  # noqa: E402

t3d = init_transformer3d_model(r"D:\models_catv2ton\EasyAnimateV4-XL-2-InP", str(FT),
                               device="cpu", weight_dtype=torch.float32)
attn_blocks = torch.nn.ModuleList()
for name, param in t3d.named_modules():
    if "attn1" in name:
        attn_blocks.append(param)
mod_keys = list(attn_blocks.state_dict().keys())
print(f"attn_blocks: {len(mod_keys)} keys; head: {mod_keys[:4]}")
inter = set(mod_keys) & set(keys)
print(f"交集 {len(inter)}/{len(mod_keys)}（=全量才算真加载）")

# 权重是否真的写进去了：抽查一个张量与检查点逐位对比
probe = mod_keys[0]
with safe_open(FT / "attention" / "model.safetensors", framework="pt", device="cpu") as f:
    if probe in set(keys):
        ckpt_t = f.get_tensor(probe)
        mod_t = attn_blocks.state_dict()[probe]
        same = torch.allclose(ckpt_t.float(), mod_t.float(), atol=0)
        print(f"抽查 {probe}: ckpt==module → {same}")
    else:
        print(f"抽查失败：{probe} 不在 ckpt")
