# -*- coding: utf-8 -*-
"""
把 brand-assets 产物同步到所有消费方（官网 / 坐席工作台 / 桌面端 / AvatarHub 开发仓）。
先跑 build_brand_assets.py（产出 00_master/keyed 与各产物），再跑本脚本。

用法：
  python sync_brand_targets.py             全量同步（目标组根目录不存在 → 整组跳过并明说）
  python sync_brand_targets.py --check     干跑：只列目标组与存在性，不写任何文件
  python sync_brand_targets.py --only 子串  只同步名字含子串的目标组（如 website / avatarhub / chengjie）

路径推导（2026-08-06 修复断链）：单仓根 = 本文件所在 brand-assets 的上一级。
此前硬编码 WS=D:\\workspace，仓库迁到 D:\\boundless 后全部落空——目标不存在时
os.makedirs 还会凭空造出幽灵目录树。现在：根随文件走；旧双仓路径存在才附加；
目标组根不存在 = 跳过并打印 [skip]，绝不静默造目录。

目标（与各处代码引用一一对应）：
  website（单仓 <root>/website）
    public/brand/logos/boundless-mark.png            全尺寸透明母版
    public/brand/logos/boundless-mark-256.png        导航/页脚/OG（BrandMark.tsx / opengraph-image.tsx）
    public/brand/logos/pwa-192.png, pwa-512.png      manifest.ts
    public/brand/logos/boundless-avatar.png          群头像（tg-broadcast.ts）
    public/brand/logos/boundless-avatar-ring.png     频道头像·光环资源号版（tg-broadcast.ts）
    app/favicon.ico / app/icon.png / app/apple-icon.png
    public/brand/products/{key}.png ×7               productMeta.ts
    public/products/prod-overview.jpg                频道置顶概览图 → 矩阵海报
  avatarhub（开发仓 assets/brand/，shutil 字节拷贝不重编码；来源=库内既有产物）
    boundless-mark-512/256.png · boundless.ico · avatar-brand-dark-512.png
    company-horizontal-white.png · company-stacked-tagline-white.png
    matrix-poster-1920x1080.png · story-1080x1920.png · {key}-128.png ×7
    注：单仓镜像 engines/avatarhub 不直接写——它由开发仓的打包/staging 流程刷新，
    这里再写一份就是双写漂移的温床。
  chengjie 坐席工作台 / 桌面端（目录存在才写）：boundless-mark-256.png, chatx.png
"""
import os
import shutil
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
BOUNDLESS = os.path.dirname(ROOT)          # 单仓根（brand-assets 的上一级，随文件走）
LEGACY_WS = r"D:\workspace"                # 旧双仓时代根：存在才附加，不存在不惦记

sys.path.insert(0, ROOT)
from build_brand_assets import (  # noqa: E402
    PRODUCTS, KEYED, DIR_AVATARS, DIR_BG, DIR_LOGOS, DIR_PICONS, DIR_LOCKUPS,
    fit, vgrad, add_glow, DARK_TOP, DARK_BOT,
)

# AvatarHub 开发仓（中枢机固定位；不存在则该组跳过）
AVATARHUB_DEV = r"D:\projects\模仿音色"

SITES = [p for p in (
    os.path.join(BOUNDLESS, "website"),
    os.path.join(LEGACY_WS, "ai-p0-integration", "website"),
    os.path.join(LEGACY_WS, "telegram-mtproto-ai", "website"),
) if os.path.isdir(p)]

BRAND_STATIC = [p for p in (
    os.path.join(BOUNDLESS, "engines", "chengjie", "src", "web", "static", "brand"),
    os.path.join(BOUNDLESS, "engines", "chengjie", "desktop", "renderer", "brand"),
    os.path.join(LEGACY_WS, "telegram-mtproto-ai", "src", "web", "static", "brand"),
    os.path.join(LEGACY_WS, "ai-p0-integration", "src", "web", "static", "brand"),
    os.path.join(LEGACY_WS, "telegram-mtproto-ai", "desktop", "renderer", "brand"),
    os.path.join(LEGACY_WS, "ai-p0-integration", "desktop", "renderer", "brand"),
) if os.path.isdir(os.path.dirname(p)) or os.path.isdir(p)]

LOG = []


def keyed(name):
    return Image.open(os.path.join(KEYED, name + "-keyed.png")).convert("RGBA")


def save(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, "PNG")
    LOG.append(path)
    print("[ok] " + os.path.relpath(path, BOUNDLESS) if path.startswith(BOUNDLESS) else "[ok] " + path)


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
    print("[ok] " + os.path.relpath(path, BOUNDLESS))


def copy_file(src, dst):
    """字节拷贝（不经 PIL 重编码）：目标与库内产物哈希一致，brand_asset_lint 可对账。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    LOG.append(dst)
    print("[ok] " + dst)


def sync_site(site):
    mark = keyed("mark")
    icons = {k: keyed(k) for k in PRODUCTS}
    mark_256 = boxed(mark, 256, 0.06)
    logos = os.path.join(site, "public", "brand", "logos")
    save(mark, os.path.join(logos, "boundless-mark.png"))
    save(mark_256, os.path.join(logos, "boundless-mark-256.png"))
    save(boxed(mark, 192, 0.06), os.path.join(logos, "pwa-192.png"))
    save(boxed(mark, 512, 0.06), os.path.join(logos, "pwa-512.png"))
    copy_file(os.path.join(DIR_AVATARS, "company", "avatar-brand-dark-512.png"),
              os.path.join(logos, "boundless-avatar.png"))
    copy_file(os.path.join(DIR_AVATARS, "company", "avatar-channel-ring-512.png"),
              os.path.join(logos, "boundless-avatar-ring.png"))

    app = os.path.join(site, "app")
    build_ico(mark, os.path.join(app, "favicon.ico"))
    save(boxed(mark, 512, 0.06), os.path.join(app, "icon.png"))
    save(dark_tile(mark, 180, 0.14), os.path.join(app, "apple-icon.png"))

    prod = os.path.join(site, "public", "brand", "products")
    for k in PRODUCTS:
        save(boxed(icons[k], 256, 0.08), os.path.join(prod, k + ".png"))

    overview = os.path.join(site, "public", "products", "prod-overview.jpg")
    os.makedirs(os.path.dirname(overview), exist_ok=True)
    poster = Image.open(os.path.join(DIR_BG, "matrix-poster-1920x1080.png")).convert("RGB")
    poster.save(overview, "JPEG", quality=92)
    LOG.append(overview)
    print("[ok] " + os.path.relpath(overview, BOUNDLESS))

    # proposal 独立副本（目录存在才写）
    prop_dir = os.path.join(site, "public", "proposal", "assets")
    if os.path.isdir(prop_dir) or os.path.isdir(os.path.join(site, "public", "proposal")):
        save(mark_256, os.path.join(prop_dir, "boundless-mark-256.png"))


def sync_avatarhub(repo):
    """AvatarHub 开发仓 assets/brand/：库内产物字节拷贝（make_icon.py 的母版来源）。"""
    dst = os.path.join(repo, "assets", "brand")
    pairs = [
        (os.path.join(DIR_LOGOS, "mark", "boundless-mark-512.png"), "boundless-mark-512.png"),
        (os.path.join(DIR_LOGOS, "mark", "boundless-mark-256.png"), "boundless-mark-256.png"),
        (os.path.join(DIR_LOGOS, "favicon", "boundless.ico"), "boundless.ico"),
        (os.path.join(DIR_AVATARS, "company", "avatar-brand-dark-512.png"), "avatar-brand-dark-512.png"),
        (os.path.join(DIR_LOCKUPS, "company", "company-horizontal-white.png"), "company-horizontal-white.png"),
        (os.path.join(DIR_LOCKUPS, "company", "company-stacked-tagline-white.png"), "company-stacked-tagline-white.png"),
        (os.path.join(DIR_BG, "matrix-poster-1920x1080.png"), "matrix-poster-1920x1080.png"),
        (os.path.join(DIR_BG, "story-1080x1920.png"), "story-1080x1920.png"),
    ] + [(os.path.join(DIR_PICONS, k, "%s-128.png" % k), "%s-128.png" % k) for k in PRODUCTS]
    for src, name in pairs:
        copy_file(src, os.path.join(dst, name))


def sync_static(d):
    mark = keyed("mark")
    save(boxed(mark, 256, 0.06), os.path.join(d, "boundless-mark-256.png"))
    save(boxed(keyed("chatx"), 256, 0.08), os.path.join(d, "chatx.png"))


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="brand-assets 产物分发（见文件头注释）")
    ap.add_argument("--check", action="store_true", help="干跑：只列目标组与存在性，不写文件")
    ap.add_argument("--only", default="", help="只同步名字含此子串的目标组")
    args = ap.parse_args(argv)

    groups = []
    for site in SITES:
        groups.append(("website:" + site, True, lambda s=site: sync_site(s)))
    groups.append(("avatarhub:" + AVATARHUB_DEV, os.path.isdir(AVATARHUB_DEV),
                   lambda: sync_avatarhub(AVATARHUB_DEV)))
    for d in BRAND_STATIC:
        groups.append(("chengjie:" + d, True, lambda x=d: sync_static(x)))

    if not SITES:
        print("[warn] 没找到任何 website 目标（单仓 <root>/website 不存在？根=%s）" % BOUNDLESS)

    ran = skipped = 0
    for name, ok, fn in groups:
        if args.only and args.only not in name:
            continue
        if args.check:
            print(("[ok]  " if ok else "[MISS]") + " " + name)
            continue
        if not ok:
            print("[skip] 目标根不存在：" + name)
            skipped += 1
            continue
        fn()
        ran += 1
    if args.check:
        print("check done（未写任何文件）")
    else:
        print("DONE. %d 文件 → %d 组（跳过 %d 组）" % (len(LOG), ran, skipped))


if __name__ == "__main__":
    main()
