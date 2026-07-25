# -*- coding: utf-8 -*-
"""仅重出产品图标（含光学烘焙）并 sync 到 website/public/brand/products。"""
import os
import sys
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from build_brand_assets import (  # noqa: E402
    PRODUCTS, KEYED, MASTERS, DIR_PICONS,
    build_product_icons, PRODUCT_OPTICAL_SCALE, save_png, boxed_square,
)

def load_keyed_masters():
    for name in ["mark", *PRODUCTS.keys()]:
        path = os.path.join(KEYED, name + "-keyed.png")
        if not os.path.isfile(path):
            raise SystemExit("缺 keyed 母版: " + path)
        MASTERS[name] = Image.open(path).convert("RGBA")

def main():
    print("optical scales:", PRODUCT_OPTICAL_SCALE)
    load_keyed_masters()
    build_product_icons()
    # 直接同步到 boundless/website
    site_prod = os.path.normpath(os.path.join(ROOT, "..", "website", "public", "brand", "products"))
    os.makedirs(site_prod, exist_ok=True)
    for k in PRODUCTS:
        src = os.path.join(DIR_PICONS, k, "%s-256.png" % k)
        dst = os.path.join(site_prod, k + ".png")
        img = Image.open(src)
        img.save(dst, "PNG")
        # WebP 变体：IntroCover 星门粒子走原生 <img>（绕过 next/image），直接吃 webp 省 ~85% 体积
        img.save(os.path.join(site_prod, k + ".webp"), "WEBP", quality=82, method=6)
        print("[ok] synced", k, "→", dst, "(+.webp) optical=", PRODUCT_OPTICAL_SCALE.get(k, 1.0))
    print("DONE.")

if __name__ == "__main__":
    main()
