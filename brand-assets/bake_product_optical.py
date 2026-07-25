# -*- coding: utf-8 -*-
"""仅重出产品图标（含光学烘焙）并 sync 到 website/public/brand/products。

顺带写 website/vendor/brand/asset-rev.json：九张图标内容的短哈希，前端拼成
`?v=<rev>` 查询串。图标路径固定不变（多处硬引用、engines 静态目录也在用），
靠 rev 让 Cache-Control 的 1 天 max-age + 7 天 stale-while-revalidate 在改图当天
立刻失效——否则老访客最长一周还看着旧图，「改了没生效」的锅会甩给前端。
"""
import hashlib
import json
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
    digest = hashlib.sha1()
    for k in PRODUCTS:
        src = os.path.join(DIR_PICONS, k, "%s-256.png" % k)
        dst = os.path.join(site_prod, k + ".png")
        img = Image.open(src)
        img.save(dst, "PNG")
        # WebP 变体：IntroCover 星门粒子走原生 <img>（绕过 next/image），直接吃 webp 省 ~85% 体积
        img.save(os.path.join(site_prod, k + ".webp"), "WEBP", quality=82, method=6)
        with open(dst, "rb") as f:
            digest.update(k.encode("utf-8"))
            digest.update(f.read())
        print("[ok] synced", k, "→", dst, "(+.webp) optical=", PRODUCT_OPTICAL_SCALE.get(k, 1.0))

    rev = digest.hexdigest()[:10]
    payload = {
        "_note": ("产品图标内容指纹，由 brand-assets/bake_product_optical.py 生成。"
                  "前端 productMeta.ts 拼 `?v=rev` 破长缓存；勿手改。"),
        "rev": rev,
    }
    # 与 optical-scale.json 同构：platform/brand 是单一真相（进 git），website/vendor
    # 是自包含副本（gitignore，随部署包走）。两处同时写，保证 sync:brand --check 不报漂移。
    for rev_path in (
        os.path.normpath(os.path.join(ROOT, "..", "platform", "brand", "asset-rev.json")),
        os.path.normpath(os.path.join(ROOT, "..", "website", "vendor", "brand", "asset-rev.json")),
    ):
        os.makedirs(os.path.dirname(rev_path), exist_ok=True)
        with open(rev_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("[ok] asset rev = %s → %s" % (rev, rev_path))
    print("DONE.")

if __name__ == "__main__":
    main()
