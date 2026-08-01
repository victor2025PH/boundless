# -*- coding: utf-8 -*-
"""Vendored 品牌令牌防腐检查（母品牌侧，2026-07-30）。

背景：独立部署的外部引擎（如智控 MatrixX / tgkz2026）构建期无法跨仓引用
platform/brand 的物理路径，按 vendoring 惯例把 brand.css 同步一份进自己仓。
vendored 副本的天敌是**静默过期**——母版改了令牌，副本没人记得覆盖，两端
悄悄分叉且不报错。本检查从母版侧盘点所有已知 vendored 副本：

  * 副本里出现的每个 --bl-* 键，值必须与母版逐字一致（允许**子集**——
    引擎只 vendoring 自己消费的色族是合理的，缺键不算错）；
  * 副本里不得有母版没有的 --bl-* 键（那是「借品牌命名空间私造令牌」，
    将来母版真加同名键时会静默打架）。

用法：
    python brand-assets/check_vendored_brand.py          # 报告；漂移 exit 1
改令牌后的完整流程：改 platform/brand/tokens.json → 派生 brand.css →
跑 sync_brand_targets.py（chengjie 系）→ 手动覆盖各 vendored 副本 → 跑本检查。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

WS = Path(r"D:\workspace\boundless")
MOTHER = WS / "platform" / "brand" / "brand.css"

# 已知 vendored 副本清单（新引擎 vendoring 时在此登记）
VENDORED = [
    WS / "tgkz2026" / "src" / "styles" / "brand-tokens.css",
]

_PROP = re.compile(r"(--bl-[\w-]+)\s*:\s*([^;]+);")
_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def props(path: Path) -> dict[str, str]:
    css = _COMMENT.sub("", path.read_text(encoding="utf-8"))
    return {k: " ".join(v.split()) for k, v in _PROP.findall(css)}


def main() -> int:
    mother = props(MOTHER)
    bad = 0
    for p in VENDORED:
        if not p.is_file():
            print(f"[skip] {p}（不存在——引擎可能已迁移，更新登记清单）")
            continue
        vend = props(p)
        drift = {k: (vend[k], mother[k]) for k in vend.keys() & mother.keys()
                 if vend[k] != mother[k]}
        foreign = sorted(vend.keys() - mother.keys())
        rel = p.as_posix()
        if not drift and not foreign:
            print(f"[ok] {rel}：{len(vend)} 键与母版一致（母版全集 {len(mother)}）")
            continue
        bad += 1
        for k, (v_old, v_new) in sorted(drift.items()):
            print(f"[DRIFT] {rel}: {k} = {v_old!r} ≠ 母版 {v_new!r}")
        for k in foreign:
            print(f"[FOREIGN] {rel}: {k} 不在母版中（勿借 --bl- 命名空间私造令牌）")
    if bad:
        print(f"\n{bad} 个副本漂移——按 vendored 头注流程用母版覆盖后重跑。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
