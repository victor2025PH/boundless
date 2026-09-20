# -*- coding: utf-8 -*-
"""平台注册表导出：把 ``src/integrations/platform_registry.py`` 渲成前端可读的 JSON。

与 ``scripts/platform_matrix.py`` 同族：那张表回答「哪个平台能做什么」（从 worker 代码推），
这份回答「有哪些平台、叫什么、什么颜色、哪些接法、落地了没」（从注册表声明）。

用法::

    python -m scripts.platform_registry_export                      # 打印到 stdout
    python -m scripts.platform_registry_export --out src/web/static/platform_registry.json

同步纪律：``src/web/static/platform_registry.json`` 由门禁 ``tests/test_platform_registry.py``
钉住与注册表逐字一致——改了注册表就重跑第二条命令，别手改生成物。刻意不带时间戳。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.integrations.platform_registry import export_json  # noqa: E402

JSON_DEFAULT = (Path(__file__).resolve().parent.parent
                / "src" / "web" / "static" / "platform_registry.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", nargs="?", const=str(JSON_DEFAULT), default=None,
                    help="写入路径（缺省只打印；不带值＝默认产物路径）")
    ns = ap.parse_args(argv)
    text = export_json()
    if ns.out:
        p = Path(ns.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        print(f"written {p} ({len(text)} bytes)")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
