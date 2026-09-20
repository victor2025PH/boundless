# -*- coding: utf-8 -*-
"""platform/licensing/sku_registry.py — SKU 注册表读取器（闭环消费侧单一入口）。

定位：让 license 门控 / order 下单 / 后台计价 **都从这一个读取器** 取 SKU 事实，
而不是各自解析 JSON 或硬编码价格。数据源是同目录 `sku_registry.json`
（由 `tools/build_sku_registry.py` 从 `products/*/product.yaml` 生成）。

依赖铁律：本模块**只读自己这层的数据文件**，不 import products / engines / website，
也不 import 任何第三方包（纯 stdlib）——守住 "platform 不反向依赖"。
官网(TS)侧可直接 `import sku_registry.json`（Next.js 支持），共用同一份 id/价，无需本文件。

用法：
    from sku_registry import price_of, get_sku, public_skus, is_priced
    p = price_of("voicex-pro")          # -> {"price": "198", "currency": "USD"} 或 None
    row = get_sku("voicex-pro")         # -> 该 SKU 完整行(含 product/category/visibility)
    if not is_priced("voxx-selfhost"):  # -> True 表示价格还是 TBD/空
        ...
命令行：python platform/licensing/sku_registry.py   # 打印自检摘要
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_REGISTRY_PATH = Path(__file__).resolve().parent / "sku_registry.json"


@lru_cache(maxsize=1)
def load_registry(path: Optional[str] = None) -> Dict[str, Any]:
    """加载并缓存注册表。传 path 可覆盖默认位置（测试用）。"""
    p = Path(path) if path else _REGISTRY_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"sku_registry.json 不存在: {p}. 先跑 `python tools/build_sku_registry.py` 生成。"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def _flat(reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    r = reg or load_registry()
    return r.get("flat_skus", [])


def all_skus(reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """全部 SKU 行（拍平，含 product/brand_key/category/visibility/sku_id/price...）。"""
    return list(_flat(reg))


def get_sku(sku_id: str, reg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """按 sku_id 取整行；找不到返回 None。"""
    for row in _flat(reg):
        if row.get("sku_id") == sku_id:
            return dict(row)
    return None


def price_of(sku_id: str, reg: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, str]]:
    """取某 SKU 的 {price, currency}；找不到或未定价(TBD/空)返回 None。"""
    row = get_sku(sku_id, reg)
    if not row:
        return None
    price = str(row.get("price", "")).strip()
    if not price or price.upper() == "TBD":
        return None
    return {"price": price, "currency": row.get("currency", "USD")}


def is_priced(sku_id: str, reg: Optional[Dict[str, Any]] = None) -> bool:
    """该 SKU 是否已定价（False = TBD/空/不存在）。"""
    return price_of(sku_id, reg) is not None


def skus_for_product(product_id: str, reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """某产品(拼音 id，如 huansheng)下的全部 SKU 行。"""
    return [dict(r) for r in _flat(reg) if r.get("product") == product_id]


def skus_by_visibility(visibility: str, reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """按合规可见性筛选：public(主站直售) / gated(准入) / ..."""
    return [dict(r) for r in _flat(reg) if r.get("visibility") == visibility]


def public_skus(reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """合规主站可直售的 SKU（visibility=public）。"""
    return skus_by_visibility("public", reg)


def gated_skus(reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """需准入的 SKU（visibility=gated）。"""
    return skus_by_visibility("gated", reg)


def unpriced_skus(reg: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """仍是 TBD/空 价格的 SKU（供 doctor / 运营点名回填）。"""
    return [dict(r) for r in _flat(reg) if not is_priced(r.get("sku_id", ""), reg)]


def summary(reg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """注册表自带摘要（产品数/SKU数/TBD数/按系/按可见性）。"""
    r = reg or load_registry()
    return dict(r.get("summary", {}))


def _selftest() -> int:
    reg = load_registry()
    s = summary(reg)
    flat = all_skus(reg)
    print(f"[sku_registry] loaded: products={s.get('product_count')} "
          f"skus={s.get('sku_count')} tbd={s.get('tbd_price_count')}")
    print(f"  by_category={s.get('by_category')}  by_visibility={s.get('by_visibility')}")
    print(f"  flat rows={len(flat)}  public={len(public_skus(reg))}  gated={len(gated_skus(reg))}")
    unp = unpriced_skus(reg)
    if unp:
        print(f"  未定价(TBD) {len(unp)} 个: " + ", ".join(x.get("sku_id", "?") for x in unp))
    # 抽样验证 price_of
    sample = next((x.get("sku_id") for x in flat if is_priced(x.get("sku_id", ""), reg)), None)
    if sample:
        print(f"  sample price_of('{sample}') = {price_of(sample, reg)}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
