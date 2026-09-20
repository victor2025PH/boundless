#!/usr/bin/env python3
"""predist 门禁：backend-dist 必须带新鲜源码指纹戳，否则拒打安装包。

用法（desktop/ 下）：
    python build/check_backend_freshness.py
    npm run check:backend-fresh

退出码：0=fresh / 1=stale 或缺戳 / 2=用法/环境错
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESKTOP = HERE.parent
REPO = DESKTOP.parent
OUT = HERE / "backend-dist"

if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from backend_source_fingerprint import STAMP_NAME, verify_stamp  # noqa: E402


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ok, reason, _cur, stamped = verify_stamp(REPO, OUT)
    if ok:
        agg = (stamped or {}).get("aggregate", "")[:16]
        n = (stamped or {}).get("file_count", "?")
        print(f"✓ backend-dist fresh ({STAMP_NAME} {agg}… files={n})")
        return 0
    print(f"✗ {reason}", file=sys.stderr)
    print("  hint: cd desktop && npm run build:backend", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
