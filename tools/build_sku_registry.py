# -*- coding: utf-8 -*-
"""tools/build_sku_registry.py

从 products/*/product.yaml 生成**统一 SKU 注册表** platform/licensing/sku_registry.json。
这是"官网 ↔ SKU ↔ license ↔ 交付"闭环的单一 SKU 源：
  - website 定价读它、license 门控按 sku_id、order 下单引用同一 id —— 一份清单三处用。

依赖方向：**构建器放 tools/**（工具层可读任意源），产物落 platform/licensing/（数据）。
这样 platform 本身不 import products——保持"platform 不反向依赖产品/引擎"。
纯脚本运行，不 import 本仓任何 Python 包，故也不触发 stdlib `platform` 名冲突。
用法：<带 pyyaml 的 python> tools/build_sku_registry.py
"""
from __future__ import annotations
import json, sys, datetime
from pathlib import Path

try:
    import yaml
except Exception:
    print("NEED PyYAML: pip install pyyaml", file=sys.stderr); sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]          # tools -> repo root
PROD_DIR = ROOT / "products"
OUT = ROOT / "platform" / "licensing" / "sku_registry.json"


def _txt(v):
    if isinstance(v, dict):
        return v.get("zh") or v.get("en") or next(iter(v.values()), "")
    return v


def main() -> int:
    manifests = sorted(PROD_DIR.glob("*/product.yaml"))
    products, flat = [], []
    tbd = 0
    by_cat, by_vis = {}, {}
    for m in manifests:
        d = yaml.safe_load(m.read_text(encoding="utf-8")) or {}
        comp = d.get("compliance", {}) or {}
        cat = d.get("category", "?"); vis = comp.get("visibility", "?")
        by_cat[cat] = by_cat.get(cat, 0) + 1
        by_vis[vis] = by_vis.get(vis, 0) + 1
        skus = []
        for s in (d.get("skus") or []):
            price = str(s.get("price", ""))
            if price.upper() == "TBD" or price == "":
                tbd += 1
            sku = {
                "sku_id": s.get("id"),
                "name": _txt(s.get("name")),
                "unit": s.get("unit"),
                "price": s.get("price"),
                "currency": s.get("currency", "USD"),
                "note": s.get("note", ""),
            }
            skus.append(sku)
            flat.append({"product": d.get("id"), "brand_key": d.get("brand_key"),
                          "category": cat, "visibility": vis, **sku})
        products.append({
            "id": d.get("id"), "brand_key": d.get("brand_key"),
            "name": _txt(d.get("name")), "category": cat,
            "visibility": vis, "risk": comp.get("risk"),
            "engine": d.get("engine"),
            "landing": (d.get("website", {}) or {}).get("landing"),
            "skus": skus,
        })
    reg = {
        "generated": datetime.date.today().isoformat(),
        "source": "products/*/product.yaml",
        "note": "Single source of SKUs for website pricing + license gating + order. Regenerate via platform/licensing/build_sku_registry.py.",
        "summary": {
            "product_count": len(products),
            "sku_count": len(flat),
            "tbd_price_count": tbd,
            "by_category": by_cat,
            "by_visibility": by_vis,
        },
        "products": products,
        "flat_skus": flat,
    }
    OUT.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    s = reg["summary"]
    print(f"OK products={s['product_count']} skus={s['sku_count']} tbd={s['tbd_price_count']}")
    print(f"   by_category={s['by_category']}")
    print(f"   by_visibility={s['by_visibility']}")
    print(f"   -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
