#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AutoSelector cache 健康扫描 CLI (Phase 8, 2026-04-24).

扫 data/selectors/*.yaml 找可疑条目 (MEMORY AutoSelector Pitfall 高发区),
按 severity 输出告警.

用法::

    python scripts/autoselector_cache_health.py
    python scripts/autoselector_cache_health.py --dir custom/selectors
    python scripts/autoselector_cache_health.py --json
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main():
    ap = argparse.ArgumentParser(description="AutoSelector cache 健康扫描")
    ap.add_argument("--dir", default="data/selectors",
                     help="selector YAML 所在目录 (默认 data/selectors)")
    ap.add_argument("--json", action="store_true",
                     help="JSON 输出")
    args = ap.parse_args()

    from src.host.autoselector_health import scan_all, format_text_report

    result = scan_all(Path(args.dir))

    if args.json:
        out = {
            "scanned_yamls": result.scanned_yamls,
            "scanned_keys": result.scanned_keys,
            "high_count": result.high_count,
            "medium_count": result.medium_count,
            "low_count": result.low_count,
            "warnings": [{
                "severity": w.severity,
                "package": w.package,
                "key": w.key,
                "issue": w.issue,
                "recommendation": w.recommendation,
            } for w in result.warnings],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(format_text_report(result))
    # 非零 exit 代码如果有 HIGH severity (用于 CI 检查)
    sys.exit(1 if result.high_count > 0 else 0)


if __name__ == "__main__":
    main()
