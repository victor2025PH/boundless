# -*- coding: utf-8 -*-
r"""avatarhub 人设授权软门控薄适配（PERSONA_BUS v1.2）。

包装 ``platform/identity/grant_gate.py``。本轮**不改**人设加载热路径——只交付检查器；
接入时在加载点加约 2 行即可。

接线指南（加载人设 / 切换角色调用点）::

    from grant_check import check_persona_grant
    r = check_persona_grant(source_key, product_id)  # product_id 如 huanying/huansheng
    if not r["allowed"]:
        # 仅 PERSONA_GRANT_ENFORCE=1 时会走到这里；默认 warn 恒 allowed=True
        raise PermissionError(f"persona grant denied: {r['reason']}")

默认缓存路径（可用 env PERSONA_GRANT_CACHE 覆盖）::

    <引擎根>/data/persona_grants_cache.json

拉取缓存（cron / 手动，建议挂在 deploy/cron export 之后）::

    python tools/persona_bus/fetch_grants.py --system avatarhub \
        --out engines/avatarhub/data/persona_grants_cache.json

契约全文：platform/identity/PERSONA_BUS.md §4.1。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

SOURCE_SYSTEM = "avatarhub"
_ENGINE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _ENGINE_ROOT.parents[1]
_GATE_PATH = _REPO_ROOT / "platform" / "identity" / "grant_gate.py"
_DEFAULT_CACHE = _ENGINE_ROOT / "data" / "persona_grants_cache.json"

_gate = None


def _load_gate():
    global _gate
    if _gate is not None:
        return _gate
    spec = importlib.util.spec_from_file_location("boundless_grant_gate", _GATE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load grant_gate from {_GATE_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _gate = mod
    return mod


def default_cache_path() -> Path:
    env = (os.environ.get("PERSONA_GRANT_CACHE") or "").strip()
    return Path(env) if env else _DEFAULT_CACHE


def check_persona_grant(
    source_key: str,
    product_id: str,
    *,
    enforce: bool | None = None,
    cache_path: str | Path | None = None,
) -> dict[str, Any]:
    """检查本引擎人设是否授权给 product_id。返回 grant_gate.check 同形 dict。"""
    gate = _load_gate()
    path = Path(cache_path) if cache_path is not None else default_cache_path()
    return gate.check(
        SOURCE_SYSTEM,
        source_key,
        product_id,
        enforce=enforce,
        cache_path=path,
    )


def main(argv: list[str] | None = None) -> int:
    """简易 CLI：python grant_check.py <source_key> <product_id>"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(
            "usage: python grant_check.py <source_key> <product_id>\n"
            "See module docstring for load-path wiring (2 lines).",
            file=sys.stderr,
        )
        return 2
    r = check_persona_grant(argv[0], argv[1])
    print(r)
    return 0 if r.get("allowed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
