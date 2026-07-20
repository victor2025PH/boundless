# -*- coding: utf-8 -*-
"""生成/更新视觉回归基线：跑 capture 输出到 tools/ui_regress/baseline/（覆盖同名图）。

用法：
    python make_baseline.py [--base-url URL] [--token TOKEN]

退出码与 capture.py 一致：0=全部场景成功；2=部分跳过；3=全部失败。
"""

import argparse
import sys
from pathlib import Path

import capture

BASELINE_DIR = Path(__file__).resolve().parent / "baseline"


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="生成视觉回归基线")
    ap.add_argument("--base-url", default=capture.DEFAULT_BASE_URL)
    ap.add_argument("--token", default=capture.DEFAULT_TOKEN)
    args = ap.parse_args(argv)

    print(f"baseline -> {BASELINE_DIR}")
    results = capture.run(BASELINE_DIR, base_url=args.base_url, token=args.token)
    skipped = {k: v for k, v in results.items() if v != "ok"}
    if skipped:
        print("以下场景本次被跳过（基线不含这些图，修复后需重跑）：")
        for k, v in skipped.items():
            print(f"  {k}: {v}")
    n_ok = len(results) - len(skipped)
    if n_ok == len(results):
        return 0
    return 3 if n_ok == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
