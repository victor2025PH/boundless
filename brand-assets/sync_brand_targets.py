# -*- coding: utf-8 -*-
"""
把 brand-assets 新管线产物同步到所有消费方（官网 / 坐席工作台 / 桌面端）。
先跑 build_brand_assets.py（产出 00_master/keyed），再跑本脚本。

目标（与各处代码引用一一对应）：
  website ×2（ai-p0-integration=部署源, telegram-mtproto-ai=主干）
    public/brand/logos/boundless-mark.png            全尺寸透明母版
    public/brand/logos/boundless-mark-256.png        导航/页脚/OG（BrandMark.tsx / opengraph-image.tsx）
    public/brand/logos/pwa-192.png, pwa-512.png      manifest.ts
    public/brand/logos/boundless-avatar.png          群头像（tg-broadcast.ts）
    public/brand/logos/boundless-avatar-ring.png     频道头像·光环资源号版（tg-broadcast.ts，新增）
    app/favicon.ico / app/icon.png / app/apple-icon.png
    public/brand/products/{key}.png ×7               productMeta.ts（统一新管线重出）
    public/products/prod-overview.jpg                频道置顶概览图 → 矩阵海报
  ai-p0-integration 专有：
    public/proposal/assets/boundless-mark-256.png
  坐席工作台 ×2 + 桌面端 ×2：
    static/brand | renderer/brand: boundless-mark-256.png, chatx.png
"""

import os
import sys
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from build_brand_assets import (  # noqa: E402
    PRODUCTS, KEYED, DIR_AVATARS, DIR_BG, fit, vgrad, add_glow, DARK_TOP, DARK_BOT,
)

WS = r"D:\workspace"
SITES = [
    os.path.join(WS, "ai-p0-integration", "website"),
    os.path.join(WS, "telegram-mtproto-ai", "website"),
]
BRAND_STATIC = [
    os.path.join(WS, "telegram-mtproto-ai", "src", "web", "static", "brand"),
    os.path.join(WS, "ai-p0-integration", "src", "web", "static", "brand"),
    os.path.join(WS, "telegram-mtproto-ai", "desktop", "renderer", "brand"),
    os.path.join(WS, "ai-p0-integration", "desktop", "renderer", "brand"),
]

LOG = []

def keyed(name):
    return Image.open(os.path.join(KEYED, name + "-keyed.png")).convert("RGBA")

def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")
    LOG.append(path)
    print("[ok] " + os.path.relpath(path, WS))

def boxed(art, size, pad_ratio):
    cv = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    box = int(size * (1 - 2 * pad_ratio))
    m = fit(art, box, box)
    cv.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return cv

def dark_tile(art, size, pad_ratio):
    cv = vgrad(size, size, DARK_TOP, DARK_BOT)
    cv = add_glow(cv, size * 0.18, size * 0.10, size * 0.85, (30, 75, 200), 0.22)
    cv = add_glow(cv, size * 0.88, size * 0.92, size * 0.85, (200, 72, 0), 0.20)
    box = int(size * (1 - 2 * pad_ratio))
    m = fit(art, box, box)
    cv.paste(m, ((size - m.width) // 2, (size - m.height) // 2), m)
    return cv

def build_ico(art, path, sizes=(16, 32, 48, 64)):
    imgs = [boxed(art, s, 0.04) for s in sizes]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    imgs[-1].save(path, sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    LOG.append(path)
    print("[ok] " + os.path.relpath(path, WS))

def copy_png(src_path, dst_path):
    img = Image.open(src_path)
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    img.save(dst_path, "PNG")
    LOG.append(dst_path)
    print("[ok] " + os.path.relpath(dst_path, WS))

def main():
    mark = keyed("mark")
    icons = {k: keyed(k) for k in PRODUCTS}

    mark_256 = boxed(mark, 256, 0.06)
    pwa_192 = boxed(mark, 192, 0.06)
    pwa_512 = boxed(mark, 512, 0.06)
    icon_512 = boxed(mark, 512, 0.06)
    apple_180 = dark_tile(mark, 180, 0.14)
    avatar_brand = Image.open(os.path.join(DIR_AVATARS, "company", "avatar-brand-dark-512.png"))
    avatar_ring = Image.open(os.path.join(DIR_AVATARS, "company", "avatar-channel-ring-512.png"))
    poster = Image.open(os.path.join(DIR_BG, "matrix-poster-1920x1080.png")).convert("RGB")

    for site in SITES:
        logos = os.path.join(site, "public", "brand", "logos")
        save(mark, os.path.join(logos, "boundless-mark.png"))
        save(mark_256, os.path.join(logos, "boundless-mark-256.png"))
        save(pwa_192, os.path.join(logos, "pwa-192.png"))
        save(pwa_512, os.path.join(logos, "pwa-512.png"))
        avatar_brand.save(os.path.join(logos, "boundless-avatar.png"), "PNG")
        LOG.append(os.path.join(logos, "boundless-avatar.png"))
        print("[ok] " + os.path.relpath(os.path.join(logos, "boundless-avatar.png"), WS))
        avatar_ring.save(os.path.join(logos, "boundless-avatar-ring.png"), "PNG")
        LOG.append(os.path.join(logos, "boundless-avatar-ring.png"))
        print("[ok] " + os.path.relpath(os.path.join(logos, "boundless-avatar-ring.png"), WS))

        app = os.path.join(site, "app")
        build_ico(mark, os.path.join(app, "favicon.ico"))
        save(icon_512, os.path.join(app, "icon.png"))
        save(apple_180, os.path.join(app, "apple-icon.png"))

        prod = os.path.join(site, "public", "brand", "products")
        for k in PRODUCTS:
            save(boxed(icons[k], 256, 0.08), os.path.join(prod, k + ".png"))

        overview = os.path.join(site, "public", "products", "prod-overview.jpg")
        os.makedirs(os.path.dirname(overview), exist_ok=True)
        poster.save(overview, "JPEG", quality=92)
        LOG.append(overview)
        print("[ok] " + os.path.relpath(overview, WS))

    # proposal 独立副本（仅 ai-p0）
    prop = os.path.join(SITES[0], "public", "proposal", "assets", "boundless-mark-256.png")
    save(mark_256, prop)

    # 坐席工作台 / 桌面端
    chatx_256 = boxed(icons["chatx"], 256, 0.08)
    for d in BRAND_STATIC:
        save(mark_256, os.path.join(d, "boundless-mark-256.png"))
        save(chatx_256, os.path.join(d, "chatx.png"))

    print("DONE. %d files" % len(LOG))

if __name__ == "__main__":
    main()
