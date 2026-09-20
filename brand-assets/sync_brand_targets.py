# -*- coding: utf-8 -*-
"""
把 brand-assets 新管线产物同步到所有消费方（官网 / 坐席工作台 / 桌面端）。
先跑 build_brand_assets.py（产出 00_master/keyed），再跑本脚本。

目标（与各处代码引用一一对应）：
  website ×2（ai-p0-integration=部署源, telegram-mtproto-ai=主干）
    public/brand/logos/boundless-mark-256.png        导航/页脚/OG（BrandMark.tsx / opengraph-image.tsx）
    public/brand/logos/boundless-mark-512.png/.webp  开场主标宽版 512×300（IntroCover picture / BrandShowcase）
    （boundless-mark.png 全尺寸母版 2026-07-25 退役：官网零引用，母版留 brand-assets/00_master）
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
    PRODUCTS, KEYED, DIR_AVATARS, DIR_BG, DIR_PICONS,
    fit, vgrad, add_glow, boxed_square, PRODUCT_OPTICAL_SCALE,
    DARK_TOP, DARK_BOT, autocrop,
)

WS = r"D:\workspace"
# 单仓 boundless 为唯一部署源；旧双仓路径保留为可选兼容（目录存在才同步）。
SITES = [
    os.path.join(WS, "boundless", "website"),
]
for _legacy in (
    os.path.join(WS, "ai-p0-integration", "website"),
    os.path.join(WS, "telegram-mtproto-ai", "website"),
):
    if os.path.isdir(_legacy) and _legacy not in SITES:
        SITES.append(_legacy)

BRAND_STATIC = [
    p
    for p in (
        os.path.join(WS, "boundless", "engines", "chengjie", "src", "web", "static", "brand"),
        os.path.join(WS, "boundless", "engines", "chengjie", "desktop", "renderer", "brand"),
        os.path.join(WS, "telegram-mtproto-ai", "src", "web", "static", "brand"),
        os.path.join(WS, "ai-p0-integration", "src", "web", "static", "brand"),
        os.path.join(WS, "telegram-mtproto-ai", "desktop", "renderer", "brand"),
        os.path.join(WS, "ai-p0-integration", "desktop", "renderer", "brand"),
    )
    if os.path.isdir(os.path.dirname(p)) or os.path.isdir(p)
]

LOG = []

def keyed(name):
    return Image.open(os.path.join(KEYED, name + "-keyed.png")).convert("RGBA")

def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")
    LOG.append(path)
    print("[ok] " + os.path.relpath(path, WS))

def boxed(art, size, pad_ratio, optical_scale=1.0):
    return boxed_square(art, size, pad_ratio, optical_scale=optical_scale)

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

def save_webp(img, path, quality=88):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "WEBP", quality=quality, method=6)
    LOG.append(path)
    print("[ok] " + os.path.relpath(path, WS))

def wide_mark(art, w=512, h=300, fill=0.97):
    """∞ 主标宽版（IntroCover 开场主标 / BrandShowcase 品牌区用）。
    保持历史 512×300 画布与 ~97% bbox 占比（布局零位移）；由 keyed 母版直出，
    修复旧 build-boundless-marks.ps1「抠白抠穿图形内部」的洞（2026-07-25 退役）。"""
    cv = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    m = fit(autocrop(art), int(w * fill), int(h * fill))
    cv.paste(m, ((w - m.width) // 2, (h - m.height) // 2), m)
    return cv

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

    mark_512w = wide_mark(mark)

    for site in SITES:
        logos = os.path.join(site, "public", "brand", "logos")
        # boundless-mark.png（全尺寸母版 ~860KB）官网零引用已退役（2026-07-25）：
        # 运行时只用 -256/-512；母版单一真相留在 brand-assets/00_master，勿再同步大文件进 public。
        save(mark_256, os.path.join(logos, "boundless-mark-256.png"))
        save(mark_512w, os.path.join(logos, "boundless-mark-512.png"))
        save_webp(mark_512w, os.path.join(logos, "boundless-mark-512.webp"))
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
            # 优先用管线已烘焙光学补偿的 256 产物；缺失时按 keyed 现算（含 optical）
            baked = os.path.join(DIR_PICONS, k, "%s-256.png" % k)
            if os.path.isfile(baked):
                copy_png(baked, os.path.join(prod, k + ".png"))
            else:
                opt = PRODUCT_OPTICAL_SCALE.get(k, 1.0)
                save(boxed(icons[k], 256, 0.08, optical_scale=opt), os.path.join(prod, k + ".png"))

        overview = os.path.join(site, "public", "products", "prod-overview.jpg")
        os.makedirs(os.path.dirname(overview), exist_ok=True)
        poster.save(overview, "JPEG", quality=92)
        LOG.append(overview)
        print("[ok] " + os.path.relpath(overview, WS))

    # proposal 独立副本（目录存在才写）
    for site in SITES:
        prop_dir = os.path.join(site, "public", "proposal", "assets")
        if os.path.isdir(prop_dir) or os.path.isdir(os.path.join(site, "public", "proposal")):
            save(mark_256, os.path.join(prop_dir, "boundless-mark-256.png"))

    # 坐席工作台 / 桌面端
    import shutil
    chatx_256 = boxed(icons["chatx"], 256, 0.08)
    brand_css_src = os.path.join(WS, "boundless", "platform", "brand", "brand.css")
    for d in BRAND_STATIC:
        os.makedirs(d, exist_ok=True)
        save(mark_256, os.path.join(d, "boundless-mark-256.png"))
        save(chatx_256, os.path.join(d, "chatx.png"))
        # 品牌令牌 SSOT（--bl-*）：坐席端/桌面端令牌桥基础层，随图标一并分发
        # （platform/brand/brand.css 改后跑本脚本即同步，消除手动副本同步债）
        if os.path.isfile(brand_css_src):
            shutil.copyfile(brand_css_src, os.path.join(d, "brand.css"))
            LOG.append(os.path.join(d, "brand.css"))
            print("[ok] " + os.path.relpath(os.path.join(d, "brand.css"), WS))

    # 桌面壳打包图标（NSIS 安装包 / exe / 快捷方式）：用 ChatX 产品标生成 .ico，
    # 与 main.js 的 DEFAULT_BRAND_ICON（窗口/任务栏）保持同一枚产品标。
    # package.json build.win.icon = build/icon.ico；目录存在才写，不新建。
    for _dbuild in (
        os.path.join(WS, "boundless", "engines", "chengjie", "desktop", "build"),
        os.path.join(WS, "telegram-mtproto-ai", "desktop", "build"),
        os.path.join(WS, "ai-p0-integration", "desktop", "build"),
    ):
        if os.path.isdir(_dbuild):
            build_ico(icons["chatx"], os.path.join(_dbuild, "icon.ico"))

    print("DONE. %d files → sites=%d static=%d" % (len(LOG), len(SITES), len(BRAND_STATIC)))

if __name__ == "__main__":
    main()
