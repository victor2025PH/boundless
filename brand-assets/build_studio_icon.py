# -*- coding: utf-8 -*-
"""幻境 STUDIO（原 AvatarHub 客户端）图标生成 — 与九产品图标同管线同规格。

STUDIO 是「客户端级」图标（引擎壳，不是 brand.ts 九产品之一），刻意不进
build_brand_assets.PRODUCTS（进了会被组合标 / 头像 / 矩阵海报当第十款产品渲染，
破坏「九款产品 · 破八道边界」的品牌口径）。本脚本复用主管线的抠白 / 方形化函数，
产出：
  00_master/keyed/studio-keyed.png            透明底母版
  02_product-icons/studio/studio-{512,256,128}.png
  ../website/public/brand/products/studio.png  官网下载中心卡片图标（256，与其他产品同规格）
  ../website/public/brand/products/studio.webp

重跑：python build_studio_icon.py（源图 = 00_master/src/studio-white.png）
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from build_brand_assets import key_out_white, autocrop, boxed_square  # noqa: E402

SRC = os.path.join(ROOT, "00_master", "src", "studio-white.png")
KEYED = os.path.join(ROOT, "00_master", "keyed", "studio-keyed.png")
PICONS = os.path.join(ROOT, "02_product-icons", "studio")
WEB_PRODUCTS = os.path.normpath(os.path.join(ROOT, "..", "website", "public", "brand", "products"))


def main():
    keyed = key_out_white(SRC)
    keyed = autocrop(keyed, 0.02)
    os.makedirs(os.path.dirname(KEYED), exist_ok=True)
    keyed.save(KEYED, "PNG")
    print("[ok]", KEYED, keyed.size)

    os.makedirs(PICONS, exist_ok=True)
    for s in (512, 256, 128):
        icon = boxed_square(keyed, s, 0.08)
        p = os.path.join(PICONS, "studio-%d.png" % s)
        icon.save(p, "PNG")
        print("[ok]", p)

    os.makedirs(WEB_PRODUCTS, exist_ok=True)
    web = boxed_square(keyed, 256, 0.08)
    web_png = os.path.join(WEB_PRODUCTS, "studio.png")
    web.save(web_png, "PNG")
    print("[ok]", web_png)
    web_webp = os.path.join(WEB_PRODUCTS, "studio.webp")
    web.save(web_webp, "WEBP", quality=92)
    print("[ok]", web_webp)
    print("DONE.")


if __name__ == "__main__":
    main()
