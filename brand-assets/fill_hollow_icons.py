# -*- coding: utf-8 -*-
"""母版构图加工：把「视觉过轻」的图标改到与同族等重（build 管线内建步骤）。

两类病因、两种药（2026-07-25 用户反馈：智聊/幻声/幻颜/幻影显得小）：

1. 空心描边型（智聊气泡 / 幻影播放框）＝粗描边环 + 内部全透空。按墨量归一时
   它们得把环撑到最大才凑够墨量，于是「外框最大却看着最轻」（智聊外框 211px
   全族第一，用户仍觉得小）。放大治不了——放大只是更大的空框。正解是内部补一层
   半透明玻璃底，把块感还回来。填充色按对角线(x+y)分桶采样图标自身描边色，
   与其冷→暖渐变天然同向，再叠径向暗角模拟玻璃厚度。

2. 主体+离散装饰型（幻声麦克风 + 两侧声波条）＝装饰条把外框撑宽，归一时真正的
   主体反被压小。正解是收掉最外侧装饰条，让主体占宽回升（实测 40% → 60%）。

⚠ 必须作为 build_brand_assets 抠白后的内建步骤跑：该脚本一旦只是「手工改一次
keyed」，下次 build 从 src 重新抠白就会静默冲掉（2026-07-25 已踩，幻声退回未裁
切的 1207×913 且无人察觉，直到光学门禁报 -22% 偏差）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage

ROOT = os.path.dirname(os.path.abspath(__file__))
KEYED = os.path.join(ROOT, "00_master", "keyed")

# 描边环内部补玻璃底：alpha=不透明度，dim=相对描边色的亮度（<1 保描边为视觉主角）
HOLLOW_FILL = {
    "chatx": {"alpha": 0.42, "dim": 0.62},
    "livex": {"alpha": 0.42, "dim": 0.62},
}

# 主体两侧各保留几根装饰条（更外侧的裁掉）；已满足则幂等不动
SIDE_BAR_KEEP = {"voicex": 2}

BANDS = 40          # 对角线采样桶数
DIAG_BLUR = 2.0     # 桶色曲线平滑，防条带

def diagonal_palette(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """按 (x+y) 对角线分桶取图形自身平均色 → (BANDS, 3)，空桶用邻桶补。"""
    h, w = mask.shape
    yy, xx = np.mgrid[0:h, 0:w]
    t = (xx + yy) / float(w + h - 2)
    idx = np.clip((t * BANDS).astype(int), 0, BANDS - 1)
    pal = np.zeros((BANDS, 3), dtype=np.float32)
    seen = np.zeros(BANDS, dtype=bool)
    for b in range(BANDS):
        sel = mask & (idx == b)
        if sel.sum() >= 24:
            pal[b] = rgb[sel].mean(axis=0)
            seen[b] = True
    known = np.where(seen)[0]
    if len(known) == 0:
        raise ValueError("empty palette")
    for b in range(BANDS):                      # 空桶取最近已知桶
        if not seen[b]:
            pal[b] = pal[known[np.argmin(np.abs(known - b))]]
    for c in range(3):                          # 平滑防色带
        pal[:, c] = ndimage.gaussian_filter1d(pal[:, c], DIAG_BLUR, mode="nearest")
    return pal, idx

def fill_interior(img: Image.Image, alpha: float, dim: float, vignette: float = 0.35):
    """给描边环内部补半透明玻璃底。

    alpha    填充不透明度（0.30~0.55 之间观感自然）
    dim      填充色相对描边色的亮度系数（<1 = 压暗，让描边仍是视觉主角）
    vignette 中心额外压暗强度（模拟玻璃厚度，0=平板）
    返回 (新图, 孔洞像素数)
    """
    arr = np.asarray(img, dtype=np.float32)
    rgb, a = arr[..., :3], arr[..., 3] / 255.0
    solid = a > 0.5

    filled = ndimage.binary_fill_holes(solid)
    holes = filled & ~solid
    # 只保留成规模的内部腔体，忽略描边内的针孔噪点
    lab, n = ndimage.label(holes)
    if n:
        sizes = ndimage.sum(holes, lab, range(1, n + 1))
        keep = {i + 1 for i, s in enumerate(sizes) if s >= holes.size * 0.004}
        holes = np.isin(lab, list(keep)) if keep else np.zeros_like(holes)

    if holes.sum() == 0:
        return img, 0

    pal, idx = diagonal_palette(rgb, solid)
    fill_rgb = pal[idx] * dim

    if vignette > 0:                            # 径向暗角：越靠腔体中心越暗
        dist = ndimage.distance_transform_edt(holes)
        if dist.max() > 0:
            k = 1.0 - vignette * (dist / dist.max())
            fill_rgb = fill_rgb * k[..., None]

    out = arr.copy()
    out[holes, :3] = np.clip(fill_rgb[holes], 0, 255)
    out[holes, 3] = alpha * 255.0

    res = Image.fromarray(out.astype(np.uint8), "RGBA")
    # 沿腔体边界羽化 alpha，消除填充与描边之间的硬接缝
    ea = res.getchannel("A").filter(ImageFilter.GaussianBlur(1.2))
    base = np.asarray(res.getchannel("A"), dtype=np.float32)
    blur = np.asarray(ea, dtype=np.float32)
    merged = np.where(solid, base, np.minimum(base, blur))
    res.putalpha(Image.fromarray(merged.astype(np.uint8), "L"))
    return res, int(holes.sum())

def column_blocks(img: Image.Image):
    """按整列 alpha 和切出「实体块 / 透明谷」，返回实体块 (x0, x1) 列表。"""
    a = np.asarray(img.getchannel("A"), dtype=np.float32)
    col = a.sum(axis=0)
    thr = col.max() * 0.001
    solid = col > thr
    blocks, s = [], None
    for x, v in enumerate(solid):
        if v and s is None:
            s = x
        elif not v and s is not None:
            blocks.append((s, x - 1))
            s = None
    if s is not None:
        blocks.append((s, len(solid) - 1))
    return blocks

def trim_side_bars(img: Image.Image, keep: int):
    """主体两侧各保留 keep 根装饰条，更外侧的裁掉（幂等：已满足则原样返回）。

    主体 = 最宽的实体块。裁切点取相邻透明谷中点（谷内整列全透明，绝不切到图形），
    裁后重新紧贴 alpha bbox（keyed 母版规范）。返回 (新图, 说明)。
    """
    blocks = column_blocks(img)
    if len(blocks) < 3:
        return img, "single block, skip"
    n = len(blocks)
    main = max(range(n), key=lambda i: blocks[i][1] - blocks[i][0])
    left, right = main, n - 1 - main
    cut_l, cut_r = max(0, left - keep), max(0, right - keep)
    if cut_l == 0 and cut_r == 0:
        return img, f"already {left}|{right} bars (keep={keep}), unchanged"

    x0 = 0 if cut_l == 0 else (blocks[cut_l - 1][1] + blocks[cut_l][0]) // 2
    x1 = (img.width if cut_r == 0
          else (blocks[n - cut_r - 1][1] + blocks[n - cut_r][0]) // 2 + 1)

    out = img.crop((x0, 0, x1, img.height))
    b = out.getchannel("A").getbbox()
    out = out.crop(b)
    nb = column_blocks(out)
    nm = max(range(len(nb)), key=lambda i: nb[i][1] - nb[i][0])
    share = (nb[nm][1] - nb[nm][0] + 1) / out.width
    return out, (f"bars {left}|{right} -> {nm}|{len(nb) - 1 - nm}  "
                 f"{img.size}->{out.size}  主体占宽 {share:.0%}")


def refine_master(key: str, img: Image.Image) -> Image.Image:
    """build 管线钩子：抠白 + autocrop 之后按 key 施加构图加工。未登记的图标原样返回。"""
    if key in HOLLOW_FILL:
        img = fill_interior(img, **HOLLOW_FILL[key])[0]
    if key in SIDE_BAR_KEEP:
        img = trim_side_bars(img, SIDE_BAR_KEEP[key])[0]
    return img

def main() -> int:
    """调参预览：读现存 keyed 跑一遍加工，产物写 _preview_*.png（不改母版）。

    正式产出一律走 `python build_brand_assets.py`（内建调用 refine_master）——
    本 CLI 只用来看参数效果，且注意现存 keyed 可能已被加工过（预览会叠加）。
    """
    alpha, dim = 0.42, 0.62
    for i, v in enumerate(sys.argv):
        if v == "--alpha":
            alpha = float(sys.argv[i + 1])
        if v == "--dim":
            dim = float(sys.argv[i + 1])

    for key in HOLLOW_FILL:
        src = Image.open(os.path.join(KEYED, f"{key}-keyed.png")).convert("RGBA")
        out, npx = fill_interior(src, alpha=alpha, dim=dim)
        dst = os.path.join(ROOT, f"_preview_fill_{key}.png")
        out.save(dst, "PNG")
        print(f"[preview] {key}: filled {npx} px  alpha={alpha} dim={dim}"
              f" -> {os.path.basename(dst)}")

    for key, keep in SIDE_BAR_KEEP.items():
        src = Image.open(os.path.join(KEYED, f"{key}-keyed.png")).convert("RGBA")
        out, note = trim_side_bars(src, keep)
        dst = os.path.join(ROOT, f"_preview_trim_{key}.png")
        out.save(dst, "PNG")
        print(f"[preview] {key}: {note} -> {os.path.basename(dst)}")

    print("\n预览模式。正式产出跑 build_brand_assets.py（refine_master 已内建）。")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
