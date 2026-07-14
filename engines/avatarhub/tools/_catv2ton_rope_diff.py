# -*- coding: utf-8 -*-
"""对比 diffusers 0.29(numpy 原版) 与 0.38(output_type='pt') 的 get_2d_rotary_pos_embed。
若数值不一致 → RoPE 漂移即衣区噪声根因（位置编码错 = 注意力全错）。"""
import sys

import numpy as np
import torch


sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def rope_029(embed_dim, crops_coords, grid_size):
    """0.29 原版（numpy linspace endpoint=False + outer + repeat_interleave）。"""
    def rope1d(dim, pos):
        freqs = 1.0 / (10000 ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
        t = torch.from_numpy(pos).to(freqs.device)
        freqs = torch.outer(t, freqs).float()
        return freqs.cos().repeat_interleave(2, dim=1), freqs.sin().repeat_interleave(2, dim=1)

    start, stop = crops_coords
    grid_h = np.linspace(start[0], stop[0], grid_size[0], endpoint=False, dtype=np.float32)
    grid_w = np.linspace(start[1], stop[1], grid_size[1], endpoint=False, dtype=np.float32)
    grid = np.meshgrid(grid_w, grid_h)
    grid = np.stack(grid, axis=0).reshape([2, 1, grid_size[0], grid_size[1]])
    emb_h = rope1d(embed_dim // 2, grid[0].reshape(-1))
    emb_w = rope1d(embed_dim // 2, grid[1].reshape(-1))
    return (torch.cat([emb_h[0], emb_w[0]], dim=1), torch.cat([emb_h[1], emb_w[1]], dim=1))


from diffusers.models.embeddings import get_2d_rotary_pos_embed
from diffusers.pipelines.hunyuandit.pipeline_hunyuandit import get_resize_crop_region_for_grid

# CatV2TON 512 档实参：inner_dim//num_heads=88(hunyuan-dit), grid 512/8/2=32, 384/8/2=24
for dim, gh, gw in ((88, 64, 48), (88, 32, 24)):
    crops = get_resize_crop_region_for_grid((gh, gw), 512 // 8 // 2)
    old_cos, old_sin = rope_029(dim, crops, (gh, gw))
    new = get_2d_rotary_pos_embed(dim, crops, (gh, gw), output_type="pt")
    new_cos, new_sin = new
    same_shape = old_cos.shape == new_cos.shape
    d_cos = (old_cos - new_cos).abs().max().item() if same_shape else float("nan")
    d_sin = (old_sin - new_sin).abs().max().item() if same_shape else float("nan")
    print(f"grid({gh},{gw}) crops={crops} old={tuple(old_cos.shape)} new={tuple(new_cos.shape)} "
          f"Δcos={d_cos:.2e} Δsin={d_sin:.2e}")
